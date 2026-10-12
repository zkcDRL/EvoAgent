import json
import os
import tempfile
import unittest
from datetime import datetime

from evoagent.evolution import DEFAULT_PROMPT, EvolutionEngine
from evoagent.postgres_store import PostgresTaskStore
from evoagent.store import TaskStore


class PostgresReadCursor:
    """Execute read queries locally, returning psycopg's decoded dict rows."""

    def __init__(self, cursor):
        self.cursor = cursor

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        if "metrics_json" in value:
            value["metrics_json"] = json.loads(value["metrics_json"])
        value["created_at"] = datetime.fromisoformat(value["created_at"])
        return value

    def fetchone(self):
        return self.decode(self.cursor.fetchone())

    def fetchall(self):
        return [self.decode(row) for row in self.cursor.fetchall()]


class PostgresReadConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def execute(self, query, params):
        if "?" in query or query.count("%s") != len(params):
            raise AssertionError("PostgreSQL queries must bind values with %s placeholders")
        return PostgresReadCursor(self.connection.execute(query.replace("%s", "?"), params))


class PostgresEvolutionRunLookupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sqlite = TaskStore(os.path.join(self.directory.name, "store.db"))
        # Only replace the external connection; execute real PostgreSQL store methods.
        self.postgres = PostgresTaskStore.__new__(PostgresTaskStore)
        self.postgres._connect = lambda: PostgresReadConnection(self.sqlite._connect())

    def save_run(self, run_id="active", skill_name="llm-review", version=1,
                 created_at="2026-10-12T08:00:00+08:00", metrics=None):
        run = {"id": run_id, "skill_name": skill_name, "candidate_version": version,
               "baseline_version": None, "decision": "activated", "candidate_score": 1.0,
               "baseline_score": 0.0, "created_at": created_at,
               "metrics": metrics if metrics is not None else {"structured_candidate": {
                   "candidate": {"role_policies": {"critic": {
                       "prompt_additions": ["Verify changed-line evidence"]}}}}}}
        self.sqlite.save_evolution_run(run)
        return run

    def test_lookup_binds_exact_skill_and_version_outside_audit_window(self):
        skill = "team's-review'; --"
        self.save_run("old-match", skill, 7, "2026-10-10T08:00:00+08:00")
        expected = self.save_run("latest-match", skill, 7)
        self.save_run("wrong-version", skill, 8, "2026-10-13T08:00:00+08:00")
        for index in range(205):
            self.save_run("other-%s" % index, "other-skill", 7,
                          "2026-10-14T08:00:00+08:00")
        for store in (self.sqlite, self.postgres):
            with self.subTest(backend=type(store).__name__):
                self.assertEqual(store.get_evolution_run_for_version(skill, 7), expected)

    def test_lookup_returns_none_for_absent_skill_or_version(self):
        self.save_run()
        for store in (self.sqlite, self.postgres):
            with self.subTest(backend=type(store).__name__):
                self.assertIsNone(store.get_evolution_run_for_version("missing", 1))
                self.assertIsNone(store.get_evolution_run_for_version("llm-review", 2))

    def test_postgres_lookup_matches_audit_jsonb_timestamp_and_row_shape(self):
        for index, metrics in enumerate(({}, {"structured_candidate": {"candidate": {
                "prompt_additions": ["检查变更证据"]}}})):
            with self.subTest(metrics=metrics):
                expected = self.save_run("metrics-%s" % index, version=index + 1, metrics=metrics)
                actual = self.postgres.get_evolution_run_for_version("llm-review", index + 1)
                self.assertEqual(actual, expected)
                listed = {run["id"]: run for run in self.postgres.list_evolution_runs()}
                self.assertEqual(actual, listed[expected["id"]])
                self.assertNotIn("metrics_json", actual)
                self.assertEqual(actual["created_at"], "2026-10-12T08:00:00+08:00")

    def test_postgres_duplicate_timestamps_use_run_id_as_stable_tiebreaker(self):
        self.save_run("a-current")
        expected = self.save_run("z-current")
        self.save_run("zz-older", created_at="2026-10-11T08:00:00+08:00")
        self.assertEqual(self.postgres.get_evolution_run_for_version("llm-review", 1), expected)

    def test_plain_proposal_with_active_legacy_version_remains_a_noop(self):
        self.sqlite.save_skill_version("llm-review", DEFAULT_PROMPT, 1.0, True)
        for store in (self.sqlite, self.postgres):
            with self.subTest(backend=type(store).__name__):
                engine = EvolutionEngine(store, seed_defaults=False)
                result = engine.propose("llm-review", DEFAULT_PROMPT)
                self.assertEqual(result["decision"], "deferred")
                self.assertEqual(result["version"]["version"], 1)
                self.assertIsNone(result["run_id"])

    def test_postgres_active_structured_policy_is_used_for_bundle_identity(self):
        self.sqlite.save_skill_version("llm-review", DEFAULT_PROMPT, 1.0, True)
        run = self.save_run()
        engine = EvolutionEngine(self.postgres, seed_defaults=False)
        result = engine._propose("llm-review", DEFAULT_PROMPT, None,
                                 structured_candidate=run["metrics"]["structured_candidate"])
        self.assertEqual(result["decision"], "deferred")
        self.assertIsNone(result["run_id"])
        self.assertEqual(len(self.sqlite.list_skill_versions("llm-review")), 1)


if __name__ == "__main__":
    unittest.main()
