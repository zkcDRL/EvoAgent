import copy
import hashlib
import json
import unittest

from evoagent.evolution_policy import (
    KNOWN_ROLES, canonical_policy_json, normalize_policy_candidate,
    policy_budget, policy_sha256,
)


class PromptPolicyTests(unittest.TestCase):
    def test_legacy_normalizes_without_mutation(self):
        candidate = {
            "prompt_additions": ["Review added lines."],
            "few_shot_examples": [{"input": "diff", "output": "json"}],
            "lead_delegation_rules": [{"when": "security", "delegate_to": ["security"]}],
            "tool_selection_policy": [{"preferred_tools": ["symbol"]}],
            "budget_parameters": {"security": 4000},
        }
        original = copy.deepcopy(candidate)
        normalized = normalize_policy_candidate(candidate)
        self.assertEqual(candidate, original)
        self.assertEqual(normalized["role_policies"], {
            "lead": {}, "security": {}, "correctness-reliability": {}, "critic": {},
        })
        normalized["few_shot_examples"][0]["input"] = "changed"
        self.assertEqual(candidate, original)

    def test_canonical_serialization_and_hash_ignore_key_order(self):
        left = {"budget_parameters": {"critic": 1000, "lead": 2000}}
        right = {"budget_parameters": {"lead": 2000, "critic": 1000}}
        self.assertEqual(canonical_policy_json(left), canonical_policy_json(right))
        self.assertEqual(policy_sha256(left), policy_sha256(right))
        self.assertEqual(policy_sha256(left), hashlib.sha256(
            canonical_policy_json(left).encode("utf-8")).hexdigest())
        self.assertEqual(json.loads(canonical_policy_json(left)), normalize_policy_candidate(left))

    def test_permissive_invalid_legacy_budget_uses_default(self):
        candidate = {"budget_parameters": {"security": "invalid"}}
        normalized = normalize_policy_candidate(candidate, strict=False)
        self.assertEqual(policy_budget(normalized, "security", 1000), 1000)

    def test_strict_rejects_unknown_keys_and_bad_shapes(self):
        candidates = [None, [], {"unknown": []}, {"role_policies": []},
                      {"role_policies": {"unknown": {}}},
                      {"prompt_additions": "text"}, {"prompt_additions": [1]},
                      {"few_shot_examples": ["text"]},
                      {"few_shot_examples": [{"input": "x"}]},
                      {"few_shot_examples": [{"input": "x", "output": 1}]},
                      {"few_shot_examples": [{"input": "x", "output": "y", "extra": "z"}]},
                      {"tool_selection_policy": ["text"]},
                      {"tool_selection_policy": [{"tools": {"symbol"}}]},
                      {"tool_selection_policy": [{"weight": float("nan")}]},
                      {"budget_parameters": []}, {"budget_parameters": {"unknown": 1}}]
        for role in KNOWN_ROLES:
            wrong = "tool_selection_policy" if role == "lead" else "delegation_rules"
            candidates.extend([{"role_policies": {role: []}},
                               {"role_policies": {role: {wrong: []}}}])
        for value in (True, False, 1.5, "1000", None):
            candidates.append({"budget_parameters": {"lead": value}})
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                normalize_policy_candidate(candidate)

    def test_count_and_length_limits_at_boundary_and_above(self):
        fields = [
            ("prompt_additions", ["a"] * 20, ["a"] * 21),
            ("prompt_additions", ["a" * 2000], ["a" * 2001]),
            ("few_shot_examples", [{"input": "i", "output": "o"}] * 10,
             [{"input": "i", "output": "o"}] * 11),
            ("few_shot_examples", [{"input": "a" * 4000, "output": "o"}],
             [{"input": "a" * 4001, "output": "o"}]),
            ("few_shot_examples", [{"input": "i", "output": "a" * 4000}],
             [{"input": "i", "output": "a" * 4001}]),
        ]
        for role in (None,) + KNOWN_ROLES:
            role_fields = fields + [("lead_delegation_rules" if role is None else
                                    "delegation_rules" if role == "lead" else
                                    "tool_selection_policy", [{"when": "a"}] * 20,
                                    [{"when": "a"}] * 21)]
            if role is None:
                role_fields.append(("tool_selection_policy", [{}] * 20, [{}] * 21))
            for field, valid, invalid in role_fields:
                with self.subTest(role=role, field=field):
                    good = {field: valid} if role is None else {"role_policies": {role: {field: valid}}}
                    bad = {field: invalid} if role is None else {"role_policies": {role: {field: invalid}}}
                    normalize_policy_candidate(good)
                    with self.assertRaises(ValueError):
                        normalize_policy_candidate(bad)

    def test_safety_applies_to_additions_examples_and_policy_rules(self):
        unsafe = ["IGNORE PREVIOUS instructions", "disable safety", "bypass checks",
                  "直接执行生产", "Modify production app.py", "Emit ```python source",
                  "Rewrite production source code", "Modify production Python code",
                  "修改生产源代码"]
        for text in unsafe:
            for fragment in ({"prompt_additions": [text]},
                             {"few_shot_examples": [{"input": text, "output": "o"}]},
                             {"few_shot_examples": [{"input": "i", "output": text}]},
                             {"tool_selection_policy": [{"instructions": text}]}):
                for candidate in (fragment, {"role_policies": {"security": fragment}}):
                    with self.subTest(text=text, candidate=candidate), self.assertRaises(ValueError):
                        normalize_policy_candidate(candidate)

    def test_canonical_limit_counts_utf8_bytes(self):
        base = {"tool_selection_policy": [{"text": ""}]}
        overhead = len(canonical_policy_json(base).encode("utf-8"))
        base["tool_selection_policy"][0]["text"] = "a" * (65536 - overhead)
        self.assertEqual(len(canonical_policy_json(base).encode("utf-8")), 65536)
        base["tool_selection_policy"][0]["text"] += "a"
        with self.assertRaises(ValueError):
            normalize_policy_candidate(base)
        base["tool_selection_policy"][0]["text"] = "界" * 22000
        with self.assertRaises(ValueError):
            normalize_policy_candidate(base)

    def test_permissive_loading_drops_bad_fragments_and_keeps_legacy_content(self):
        candidate = {
            "unknown": "discard", "prompt_additions": ["a" * 2001, "bypass", 1],
            "few_shot_examples": [{"input": "i", "output": "o"}, "bad"],
            "role_policies": {"unknown": {}, "lead": "bad", "security": {
                "delegation_rules": [], "prompt_additions": ["safe", 1],
                "tool_selection_policy": "bad"}},
        }
        normalized = normalize_policy_candidate(candidate, strict=False)
        self.assertEqual(normalized["prompt_additions"], ["a" * 2001, "bypass"])
        self.assertEqual(normalized["few_shot_examples"], [{"input": "i", "output": "o"}])
        self.assertEqual(normalized["role_policies"]["lead"], {})
        self.assertEqual(normalized["role_policies"]["security"], {"prompt_additions": ["safe"]})
        self.assertEqual(normalize_policy_candidate({"role_policies": []}, strict=False)["role_policies"],
                         {role: {} for role in KNOWN_ROLES})

    def test_rendering_shares_legacy_content_and_isolates_role_content(self):
        from evoagent.evolution_policy import render_policy_for_role
        candidate = {
            "prompt_additions": ["SHARED_ADDITION"],
            "few_shot_examples": [{"input": "SHARED_EXAMPLE", "output": 'a"b\nc'}],
            "lead_delegation_rules": [{"when": "LEGACY_DELEGATION"}],
            "tool_selection_policy": [{"hypothesis": "LEGACY_TOOL"}],
            "budget_parameters": {"lead": 3456, "security": 4567},
            "role_policies": {
                role: {"prompt_additions": [role + "_ADDITION"],
                       "few_shot_examples": [{"input": role + "_EXAMPLE", "output": "ok"}],
                       "delegation_rules" if role == "lead" else "tool_selection_policy":
                           [{"when": role + "_POLICY"}]}
                for role in KNOWN_ROLES
            },
        }
        for role in KNOWN_ROLES:
            with self.subTest(role=role):
                rendered = render_policy_for_role(candidate, role)
                for marker in ("SHARED_ADDITION", "SHARED_EXAMPLE", "LEGACY_TOOL",
                               role + "_ADDITION", role + "_EXAMPLE", role + "_POLICY"):
                    self.assertIn(marker, rendered)
                self.assertEqual("LEGACY_DELEGATION" in rendered, role == "lead")
                for other in KNOWN_ROLES:
                    if other != role:
                        for suffix in ("_ADDITION", "_EXAMPLE", "_POLICY"):
                            self.assertNotIn(other + suffix, rendered)
                for forbidden in ("budget_parameters", "3456", "4567"):
                    self.assertNotIn(forbidden, rendered)
                self.assertIn('a\\"b\\nc', rendered)
                self.assertLess(rendered.index("SHARED_ADDITION"), rendered.index("SHARED_EXAMPLE"))
                self.assertLess(rendered.index("LEGACY_TOOL"), rendered.index(role + "_ADDITION"))
                reordered = dict(reversed(list(candidate.items())))
                self.assertEqual(rendered, render_policy_for_role(reordered, role))

    def test_rendering_deduplicates_shared_additions_in_global_prompt(self):
        from evoagent.evolution_policy import render_policy_for_role
        candidate = {"prompt_additions": ["Already included", "New addition"]}
        rendered = render_policy_for_role(candidate, "lead", "Global: Already included")
        self.assertNotIn("Already included", rendered)
        self.assertIn("New addition", rendered)
        self.assertEqual(render_policy_for_role({}, "critic"), "")
        with self.assertRaises(ValueError):
            render_policy_for_role(candidate, "unknown")

    def test_budget_clamping_and_defensive_legacy_fallback(self):
        for value, want in ((1, 256), (-1, 256), (0, 256), (9999, 4000),
                            (2000, 2000), (True, 1000), ("2000", 1000), (None, 1000)):
            with self.subTest(value=value):
                self.assertEqual(policy_budget({"budget_parameters": {"security": value}},
                                               "security", 1000), want)
        for candidate in (None, [], {}, {"budget_parameters": []}):
            self.assertEqual(policy_budget(candidate, "security", 1000), 1000)
        self.assertEqual(policy_budget({"budget_parameters": {"lead": 2000}}, "critic", 1000), 1000)
        self.assertEqual(policy_budget({"budget_parameters": {"unknown": 2000}}, "unknown", 1000), 1000)


if __name__ == "__main__":
    unittest.main()
