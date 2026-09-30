"""Tests for Coding Agent Executors (Mocked)"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from pocketfleet.executors.aider import AiderExecutor
from pocketfleet.executors.antigravity import AntigravityExecutor
from pocketfleet.executors.claude_code import ClaudeCodeExecutor
from pocketfleet.executors.codex import CodexExecutor


class TestExecutors(unittest.TestCase):
    def test_antigravity_requires_real_cli(self):
        executor = AntigravityExecutor(r"Z:\missing\agy.exe")
        self.assertFalse(executor.is_available())
        code, stdout, stderr = executor.execute("do work")
        self.assertEqual(code, 127)
        self.assertEqual(stdout, "")
        self.assertIn("Antigravity CLI", stderr)

    def test_antigravity_uses_real_noninteractive_cli(self):
        executor = AntigravityExecutor("agy")
        with mock.patch.object(executor, "is_available", return_value=True), mock.patch(
            "pocketfleet.executors.antigravity.run_safe_process_tree",
            return_value=(0, "real final", ""),
        ) as run:
            code, stdout, stderr = executor.execute("inspect the repo", cwd="repo", timeout_sec=60)

        self.assertEqual((code, stdout, stderr), (0, "real final", ""))
        command = run.call_args.args[0]
        self.assertEqual(command[0], "agy")
        self.assertIn("--mode", command)
        self.assertIn("accept-edits", command)
        self.assertIn("--print-timeout=55s", command)
        self.assertEqual(command[-1], "--print=inspect the repo")

    def test_codex_uses_real_noninteractive_exec_syntax(self):
        executor = CodexExecutor("codex")
        with mock.patch.object(executor, "is_available", return_value=True), mock.patch(
            "pocketfleet.executors.codex.run_safe_process_tree",
            return_value=(0, "completed", ""),
        ) as run:
            code, stdout, _ = executor.execute("Fix the bug", cwd="repo")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "completed")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--approve-for-me", cmd)
        self.assertEqual(cmd[-1], "Fix the bug")

    @mock.patch("pocketfleet.executors.claude_code.check_executable", return_value=True)
    def test_claude_code_executor_available(self, _mock_check):
        executor = ClaudeCodeExecutor("claude")
        self.assertTrue(executor.is_available())

        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ("Code fixed", "")
        mock_proc.returncode = 0

        with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            code, stdout, stderr = executor.execute("Fix bug in main.py")
            self.assertEqual(code, 0)
            self.assertEqual(stdout, "Code fixed")
            self.assertEqual(mock_popen.call_args[0][0], ["claude", "-p", "Fix bug in main.py"])
            self.assertEqual(mock_popen.call_args[1]["stdin"], subprocess.DEVNULL)

    @mock.patch("pocketfleet.executors.aider.check_executable", return_value=True)
    def test_aider_executor_execution(self, _mock_check):
        executor = AiderExecutor("aider")
        self.assertTrue(executor.is_available())

        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ("Aider committed diff", "")
        mock_proc.returncode = 0

        with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            code, stdout, stderr = executor.execute("Refactor module")
            self.assertEqual(code, 0)
            self.assertIn("Aider committed diff", stdout)
            self.assertEqual(mock_popen.call_args[0][0], ["aider", "--message", "Refactor module", "--yes"])
            self.assertEqual(mock_popen.call_args[1]["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
