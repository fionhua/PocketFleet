"""Tests for Coding Agent Executors (Mocked)"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from pocketfleet.executors.aider import AiderExecutor
from pocketfleet.executors.claude_code import ClaudeCodeExecutor


class TestExecutors(unittest.TestCase):
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
