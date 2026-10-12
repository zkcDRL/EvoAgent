import os
import tempfile
import unittest
import json
import io
from dataclasses import replace

from evoagent.api import ApiHandler
from evoagent.config import Settings
from evoagent.evolution import AgentPolicyReplayReviewer, DEFAULT_PROMPT
from evoagent.evolution_policy import normalize_policy_candidate
from evoagent.reviewer import OpenAICompatibleReviewer
from evoagent.service import ReviewService
from evoagent.store import utc_now
from agentic_fake import enable_agentic_service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path, max_diff_bytes=10000,
            max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
            agent_token_budget=32768,
        )

    def tearDown(self):
        os.unlink(self.path)

    def test_end_to_end_review(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        service = enable_agentic_service(ReviewService(self.settings))
        result = service.create_review("org/repo", diff, 1)
        task = service.store.get(result["task_id"])
        service.queue.close()
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual("SEC-EVAL", result["report"]["findings"][0]["rule_id"])
        self.assertEqual("agentic", result["report"]["run_mode"]["effective"])
        self.assertEqual(
            ["lead", "security", "correctness-reliability", "critic"],
            result["report"]["collaboration"]["roles"],
        )
        self.assertEqual(5, result["report"]["execution"]["llm_calls"])
        self.assertEqual("normal", result["report"]["collaboration"]["risk_level"])
        role_calls = {}
        for call in result["report"]["execution"]["model_call_log"]:
            role_calls[call["role"]] = role_calls.get(call["role"], 0) + 1
        self.assertEqual({
            "lead": 2, "security": 1,
            "correctness-reliability": 1, "critic": 1,
        }, role_calls)
        self.assertEqual(0, result["report"]["collaboration"]["revision_rounds"])
        self.assertGreater(result["report"]["execution"]["tool_calls"], 0)
        self.assertEqual([], task["collaboration"])

    def test_rejects_large_diff(self):
        service = enable_agentic_service(ReviewService(self.settings))
        with self.assertRaises(ValueError):
            service.create_review("org/repo", "x" * 10001)

    def test_completed_review_feedback_is_persisted_and_listed_per_task(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        service = enable_agentic_service(ReviewService(self.settings))
        result = service.create_review("org/repo", diff, 1)
        task_id = result["task_id"]

        feedback = service.record_feedback(
            task_id, "false_positive", result["report"]["findings"][0], "不是实际风险",
        )

        self.assertEqual({"recorded": True, "category": "false_positive"}, feedback)
        cases = service.store.list_task_failure_cases(task_id, "default")
        self.assertEqual(1, len(cases))
        self.assertEqual("false_positive", cases[0]["category"])
        self.assertEqual("SEC-EVAL", cases[0]["payload"]["finding"]["rule_id"])
        service.queue.close()

    def test_production_policy_replay_reuses_review_dependencies(self):
        settings = replace(self.settings, llm_base_url="https://example.test/v1",
                           llm_api_key="test-key", llm_model="test-model",
                           agent_token_budget=4096, agent_time_budget_seconds=17)
        service = ReviewService(settings)
        self.addCleanup(service.queue.close)
        self.assertIsNotNone(service.evolution.policy_reviewer_factory)
        self.assertIsNotNone(service.evolution.reviewer_factory)
        policy = {"role_policies": {"security": {"prompt_additions": ["TRACE_INPUT"]}}}
        replay = service.evolution.policy_reviewer_factory({
            "prompt": DEFAULT_PROMPT, "structured_config": policy,
        })
        self.assertIsInstance(replay, AgentPolicyReplayReviewer)
        self.assertIs(replay.agentic.client, service.chat_client)
        self.assertEqual(replay.agentic.default_token_budget, 4096)
        self.assertEqual(replay.agentic.default_time_budget, 17)
        self.assertEqual(replay.agentic.structured_config, normalize_policy_candidate(policy))
        self.assertEqual([type(item) for item in replay.agentic.scanners],
                         [type(item) for item in service.registry.reviewers()
                          if not isinstance(item, OpenAICompatibleReviewer)])
        self.assertEqual(replay.agentic.skill_provider("default"),
                         service._active_agent_skills("default"))

    def test_no_model_keeps_prompt_evolution_deferred(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.queue.close)
        self.assertIsNone(service.evolution.policy_reviewer_factory)
        self.assertIsNone(service.evolution.reviewer_factory)
        result = service.evolution.propose("llm-review", DEFAULT_PROMPT)
        self.assertEqual(result["decision"], "deferred")
        self.assertEqual(result["gates"]["evaluation_mode"], "legacy-single-reviewer")

    def test_plain_propose_api_and_activation_reload_policy_bundle(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.queue.close)

        class Handler(ApiHandler):
            def log_message(self, _format, *args):
                pass

        def post(path, payload):
            encoded = json.dumps(payload).encode("utf-8")
            handler = Handler.__new__(Handler)
            handler.service = service
            handler.settings = self.settings
            handler.path = path
            handler.command = "POST"
            handler.request_version = "HTTP/1.1"
            handler.requestline = "POST %s HTTP/1.1" % path
            handler.headers = {"Content-Length": str(len(encoded)),
                               "Content-Type": "application/json"}
            handler.rfile = io.BytesIO(encoded)
            handler.wfile = io.BytesIO()
            handler.do_POST()
            header, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
            return int(header.split()[1]), json.loads(body)

        status, proposed = post("/v1/evolution/propose", {
            "skill_name": "llm-review", "prompt": DEFAULT_PROMPT,
        })
        self.assertEqual(status, 201)
        self.assertEqual(proposed["decision"], "deferred")
        self.assertTrue({"version", "decision", "reason", "candidate", "baseline",
                         "candidate_holdout", "baseline_holdout", "safety", "gates",
                         "run_id"}.issubset(proposed))
        self.assertEqual(proposed["gates"]["evaluation_mode"], "legacy-single-reviewer")
        self.assertEqual(service.store.get_evolution_run_for_version(
            "llm-review", proposed["version"]["version"])["metrics"]["evaluation_mode"],
            "legacy-single-reviewer")

        version = service.store.save_skill_version("llm-review", "activated overlay", 0.0, False)
        service.store.save_evolution_run({
            "id": "api-bundle", "skill_name": "llm-review", "candidate_version": version["version"],
            "baseline_version": proposed["version"]["version"], "decision": "rejected",
            "candidate_score": 0.0, "baseline_score": 0.0,
            "metrics": {"structured_candidate": {"candidate": {"role_policies": {
                "critic": {"prompt_additions": ["API_BUNDLE_MARKER"]}}}}},
            "created_at": utc_now(),
        })
        status, activated = post(
            "/v1/skills/llm-review/versions/%s/activate" % version["version"], {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(activated, {"activated": True})
        self.assertEqual(service.reviewer.prompt_overlay, "activated overlay")
        self.assertIn("API_BUNDLE_MARKER", service.reviewer._role_prompt("critic"))


if __name__ == "__main__":
    unittest.main()
