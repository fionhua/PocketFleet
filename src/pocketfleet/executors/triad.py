"""Evidence-backed orchestration for two real coding-agent executors.

The triad executor coordinates three real turns:
1. the lead produces an acceptance contract;
2. the builder changes the workspace and reports evidence;
3. the lead inspects the resulting workspace and returns a verdict.

It never fabricates agent output, test results, timings, or file changes.
"""
from __future__ import annotations

from typing import Callable, Tuple

from .base import BaseExecutor


PhaseCallback = Callable[[str, str], None] | Callable[[str], None]


def _clip(text: str, limit: int = 12_000) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "\n...[truncated by PocketFleet]"


class FleetTriadExecutor(BaseExecutor):
    """Coordinate two distinct, synchronous executors without impersonation."""

    name: str = "fleet_triad"

    def __init__(
        self,
        lead_executor: BaseExecutor | None = None,
        builder_executor: BaseExecutor | None = None,
        lead_name: str = "Lead",
        builder_name: str = "Builder",
    ) -> None:
        self.configure(lead_executor, builder_executor, lead_name, builder_name)

    def configure(
        self,
        lead_executor: BaseExecutor | None,
        builder_executor: BaseExecutor | None,
        lead_name: str,
        builder_name: str,
    ) -> None:
        self.lead_executor = lead_executor
        self.builder_executor = builder_executor
        self.lead_name = lead_name
        self.builder_name = builder_name

    def is_available(self) -> bool:
        return bool(
            self.lead_executor
            and self.builder_executor
            and self.lead_executor is not self.builder_executor
            and self.lead_executor.is_available()
            and self.builder_executor.is_available()
        )

    @staticmethod
    def _emit(callback: PhaseCallback | None, text: str, role: str) -> None:
        if not callback:
            return
        try:
            callback(text, role)
        except TypeError:
            callback(text)

    def execute_with_phases(
        self,
        prompt: str,
        cwd: str | None = None,
        on_phase: PhaseCallback | None = None,
        lead_name: str | None = None,
        builder_name: str | None = None,
        timeout_sec: int = 300,
    ) -> Tuple[int, str, str]:
        if not self.is_available():
            return (
                127,
                "",
                "Real fleet mode requires two distinct and available agent executors. "
                "No simulation fallback was used.",
            )

        assert self.lead_executor is not None
        assert self.builder_executor is not None
        lead = lead_name or self.lead_name
        builder = builder_name or self.builder_name
        phase_timeout = max(30, timeout_sec)

        contract_prompt = f"""You are the lead engineer for a real coding task.

Task:
{prompt.strip()}

Workspace: {cwd or 'current workspace'}

Do not modify files in this turn. Inspect the repository as needed and return a concise,
testable acceptance contract for the builder. Include scope, required behavior, tests,
and explicit non-goals. Do not claim that work or tests have already completed.
"""
        code, contract, error = self.lead_executor.execute(
            contract_prompt, cwd=cwd, timeout_sec=phase_timeout
        )
        if code != 0 or not contract.strip():
            return code or 1, "", f"Lead planning failed: {error or 'empty response'}"

        self._emit(
            on_phase,
            f"[REAL AGENT][LEAD: {lead}]\n{_clip(contract)}",
            "lead",
        )

        build_prompt = f"""You are the builder for a real coding task.

Original task:
{prompt.strip()}

Acceptance contract from {lead}:
{_clip(contract)}

Implement the task in the workspace. Run the relevant tests. Report the files changed,
commands run, exact test results, and any residual risks. Never invent evidence.
"""
        code, build_report, error = self.builder_executor.execute(
            build_prompt, cwd=cwd, timeout_sec=phase_timeout
        )
        if code != 0 or not build_report.strip():
            return code or 1, "", f"Builder execution failed: {error or 'empty response'}"

        self._emit(
            on_phase,
            f"[REAL AGENT][BUILDER: {builder}]\n{_clip(build_report)}",
            "builder",
        )

        audit_prompt = f"""You are the lead engineer performing final verification.

Original task:
{prompt.strip()}

Acceptance contract:
{_clip(contract)}

Builder report:
{_clip(build_report)}

Inspect the actual workspace and run the relevant verification commands yourself.
Do not trust the builder report without checking it. End with exactly one line:
VERDICT: PASS
or
VERDICT: FAIL
Use PASS only when the repository evidence satisfies the acceptance contract.
"""
        code, audit_report, error = self.lead_executor.execute(
            audit_prompt, cwd=cwd, timeout_sec=phase_timeout
        )
        if code != 0 or not audit_report.strip():
            return code or 1, "", f"Lead verification failed: {error or 'empty response'}"

        self._emit(
            on_phase,
            f"[REAL AGENT][VERIFICATION: {lead}]\n{_clip(audit_report)}",
            "lead",
        )

        passed = any(
            line.strip().upper() == "VERDICT: PASS"
            for line in audit_report.splitlines()
        )
        if not passed:
            return 2, "", "Lead verification did not return VERDICT: PASS.\n" + _clip(audit_report)

        summary = (
            "[REAL FLEET RESULT]\n"
            f"Lead: {lead}\n"
            f"Builder: {builder}\n"
            "Verification: PASS\n\n"
            + _clip(audit_report)
        )
        return 0, summary, ""

    def execute(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_sec: int = 300,
    ) -> Tuple[int, str, str]:
        return self.execute_with_phases(prompt, cwd=cwd, timeout_sec=timeout_sec)
