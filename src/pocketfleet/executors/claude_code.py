"""Claude Code CLI Executor

Wrapper for Anthropic's official `claude` CLI.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Tuple

from .base import BaseExecutor, check_executable

logger = logging.getLogger(__name__)


class ClaudeCodeExecutor(BaseExecutor):
    name: str = "claude_code"

    def __init__(self, binary_path: str = "claude") -> None:
        self.binary_path = binary_path

    def is_available(self) -> bool:
        return check_executable(self.binary_path)

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        if not self.is_available():
            return 127, "", f"Executable '{self.binary_path}' not found in PATH."

        # Non-interactive invocation for Claude Code
        # --print (-p) runs the query and prints the response without entering interactive TUI
        cmd = [self.binary_path, "-p", prompt]

        try:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Task timed out after {timeout_sec} seconds."
        except OSError as exc:
            return 1, "", f"Execution failed: {exc}"
