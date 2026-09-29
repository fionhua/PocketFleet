"""Base Executor Interface for Coding Agents"""
from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from typing import Tuple


class BaseExecutor(ABC):
    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the CLI tool is installed and executable in PATH."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        """Execute a coding task. Returns (exit_code, stdout, stderr)."""
        raise NotImplementedError


def check_executable(cmd_name: str) -> bool:
    """Helper to check if binary is in PATH."""
    return shutil.which(cmd_name) is not None
