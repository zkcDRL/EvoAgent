from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from evoagent.agentic_core import BoundedRole
from evoagent.runtime import AgentTool, RuntimeBudgetExceeded, ToolRegistry
from evoagent.telemetry import ExecutionLedger


class OutputHungryClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self):
        self.max_tokens = []

    def complete_json(self, role, _system, _user, ledger=None, max_tokens=None):
        self.max_tokens.append(max_tokens)
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 50, "completion_tokens": max_tokens}, 1,
            )
        return {"action": "final"}


class OverBudgetUsageClient:
    provider = "fake"
    model = "fake-model"

    def complete_json(self, role, _system, _user, ledger=None, max_tokens=None):
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 1000, "completion_tokens": 1}, 1,
            )
        return {"action": "final"}


class ConcurrentToolClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self):
        self.before_first_record = threading.Barrier(2)
        self.after_first_record = threading.Barrier(2)
        self.local = threading.local()

    def complete_json(self, role, _system, _user, ledger=None, max_tokens=None):
        call = getattr(self.local, "call", 0) + 1
        self.local.call = call
        if call == 1:
            self.before_first_record.wait(timeout=5)
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model,
                    {"prompt_tokens": 300, "completion_tokens": 100}, 1,
                )
            self.after_first_record.wait(timeout=5)
            return {
                "action": "tool", "tool": "noop", "arguments": {},
            }
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 10}, 1,
            )
        return {"action": "final"}


class RoleTokenBudgetTests(unittest.TestCase):
    def test_total_budget_reserves_input_before_setting_output_limit(self):
        client = OutputHungryClient()
        ledger = ExecutionLedger("test")
        role = BoundedRole("security", "Review safely.", client, 1000, 10, max_steps=1)

        result = role.run('{"phase":"worker"}', ToolRegistry(), ledger)

        self.assertEqual("final", result["action"])
        self.assertLess(client.max_tokens[0], 1000)
        self.assertLessEqual(ledger.summary()["total_tokens"], 1000)

    def test_actual_usage_cannot_complete_over_total_budget(self):
        role = BoundedRole(
            "security", "Review safely.", OverBudgetUsageClient(),
            1000, 10, max_steps=1,
        )

        with self.assertRaisesRegex(RuntimeBudgetExceeded, "security budget exhausted"):
            role.run('{"phase":"worker"}', ToolRegistry(), ExecutionLedger("test"))

    def test_parallel_same_role_runs_do_not_share_usage(self):
        client = ConcurrentToolClient()
        ledger = ExecutionLedger("test")
        registry = ToolRegistry([AgentTool(
            "noop", "Return an empty observation.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {},
        )])

        def run():
            role = BoundedRole(
                "security", "Review safely.", client, 750, 10, max_steps=2,
            )
            return role.run('{"phase":"worker"}', registry, ledger)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in [pool.submit(run), pool.submit(run)]]

        self.assertEqual(["final", "final"], [item["action"] for item in results])
        self.assertEqual(4, ledger.summary()["llm_calls"])
        self.assertEqual(840, ledger.summary()["total_tokens"])


if __name__ == "__main__":
    unittest.main()
