"""Aider CLI Executor

Wrapper for the popular open-source pair programmer `aider`.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Tuple

from .base import BaseExecutor, check_executable, run_safe_process_tree

logger = logging.getLogger(__name__)


class AiderExecutor(BaseExecutor):
    name: str = "aider"

    def __init__(self, binary_path: str = "aider") -> None:
        self.binary_path = binary_path

    def is_available(self) -> bool:
        return check_executable(self.binary_path)

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        if not self.is_available():
            return 127, "", f"Executable '{self.binary_path}' not found in PATH."

        cmd = [self.binary_path, "--message", prompt, "--yes"]
        env = dict(os.environ)
        env["CI"] = "1"
        env["NONINTERACTIVE"] = "1"

        return run_safe_process_tree(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
