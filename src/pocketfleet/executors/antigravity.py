"""Independent Antigravity CLI worker."""
from __future__ import annotations

import os
import shutil
from typing import Tuple

from .base import BaseExecutor, run_safe_process_tree


class AntigravityExecutor(BaseExecutor):
    name: str = "antigravity"

    def __init__(self, binary_path: str | None = None) -> None:
        self.binary_path = (
            binary_path
            or os.environ.get("POCKETFLEET_ANTIGRAVITY_CLI")
            or shutil.which("agy")
            or "agy"
        )

    def is_available(self) -> bool:
        if os.path.isabs(self.binary_path):
            return os.path.isfile(self.binary_path)
        return bool(shutil.which(self.binary_path))

    def execute(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_sec: int = 300,
    ) -> Tuple[int, str, str]:
        if not self.is_available():
            return 127, "", f"Antigravity CLI '{self.binary_path}' not found."

        cli_timeout = max(1, timeout_sec - 5)
        cmd = [
            self.binary_path,
            "--mode",
            "accept-edits",
            "--output-format",
            "text",
            f"--print-timeout={cli_timeout}s",
            f"--print={prompt}",
        ]
        env = dict(os.environ)
        return run_safe_process_tree(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
