from __future__ import annotations

import unittest

from pocketfleet.executors.base import BaseExecutor
from pocketfleet.executors.triad import FleetTriadExecutor


class StubExecutor(BaseExecutor):
    def __init__(self, name: str, responses: list[tuple[int, str, str]], available: bool = True):
        self.name = name
        self.responses = list(responses)
        self.available = available
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class TestFleetTriadExecutor(unittest.TestCase):
    def test_requires_two_distinct_real_executors(self):
        executor = FleetTriadExecutor()
        code, output, error = executor.execute("fix it")
        self.assertEqual(code, 127)
        self.assertEqual(output, "")
        self.assertIn("No simulation fallback", error)

    def test_runs_real_plan_build_and_verify_turns(self):
        lead = StubExecutor(
            "lead",
            [
                (0, "Acceptance: add test", ""),
                (0, "Checked repository and tests.\nVERDICT: PASS", ""),
            ],
        )
        builder = StubExecutor("builder", [(0, "Changed app.py; 4 tests passed", "")])
        phases: list[tuple[str, str]] = []
        executor = FleetTriadExecutor(lead, builder, "Judge", "MudSnake")

        code, output, error = executor.execute_with_phases(
            "fix the bug", cwd="repo", on_phase=lambda text, role: phases.append((role, text))
        )

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("Verification: PASS", output)
        self.assertEqual([role for role, _ in phases], ["lead", "builder", "lead"])
        self.assertEqual(len(lead.prompts), 2)
        self.assertEqual(len(builder.prompts), 1)
        self.assertIn("Acceptance: add test", builder.prompts[0])
        self.assertIn("Changed app.py", lead.prompts[1])

    def test_builder_failure_stops_before_verification(self):
        lead = StubExecutor("lead", [(0, "Acceptance", "")])
        builder = StubExecutor("builder", [(1, "", "build failed")])
        executor = FleetTriadExecutor(lead, builder)

        code, output, error = executor.execute("fix it")

        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Builder execution failed", error)
        self.assertEqual(len(lead.prompts), 1)

    def test_missing_pass_verdict_is_failure(self):
        lead = StubExecutor(
            "lead",
            [(0, "Acceptance", ""), (0, "Tests look suspicious.\nVERDICT: FAIL", "")],
        )
        builder = StubExecutor("builder", [(0, "Implemented", "")])
        executor = FleetTriadExecutor(lead, builder)

        code, output, error = executor.execute("fix it")

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("VERDICT: PASS", error)


if __name__ == "__main__":
    unittest.main()
