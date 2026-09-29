"""Tests for Coding Agent Executors (Mocked)"""
from __future__ import annotations

from unittest import mock

from pocketfleet.executors.aider import AiderExecutor
from pocketfleet.executors.claude_code import ClaudeCodeExecutor


def test_claude_code_executor_available(monkeypatch):
    monkeypatch.setattr("pocketfleet.executors.claude_code.check_executable", lambda _: True)
    executor = ClaudeCodeExecutor("claude")
    assert executor.is_available() is True

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="Code fixed", stderr="")
        code, stdout, stderr = executor.execute("Fix bug in main.py")
        assert code == 0
        assert stdout == "Code fixed"
        mock_run.assert_called_once_with(
            ["claude", "-p", "Fix bug in main.py"],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=300,
        )


def test_aider_executor_execution(monkeypatch):
    monkeypatch.setattr("pocketfleet.executors.aider.check_executable", lambda _: True)
    executor = AiderExecutor("aider")
    assert executor.is_available() is True

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="Aider committed diff", stderr="")
        code, stdout, stderr = executor.execute("Refactor module")
        assert code == 0
        assert "Aider committed diff" in stdout
        mock_run.assert_called_once_with(
            ["aider", "--message", "Refactor module", "--yes"],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=300,
        )
