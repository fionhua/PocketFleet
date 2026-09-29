"""Antigravity / AgentAPI Executor

Integrates with Google Antigravity IDE and agentapi / agy toolchain.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

from .base import BaseExecutor, run_safe_process_tree

logger = logging.getLogger(__name__)


def find_antigravity_binary() -> str:
    # 1. Antigravity IDE agentapi path
    agentapi_path = Path.home() / ".gemini" / "antigravity-ide" / "bin" / "agentapi.BAT"
    if agentapi_path.is_file():
        return str(agentapi_path)
    
    # 2. agy binary in local appdata
    agy_path = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.EXE"
    if agy_path.is_file():
        return str(agy_path)

    # 3. PATH
    return shutil.which("agentapi.BAT") or shutil.which("agentapi") or shutil.which("agy") or "agentapi"


class AntigravityExecutor(BaseExecutor):
    name: str = "antigravity"

    def __init__(self, binary_path: str | None = None) -> None:
        self.binary_path = binary_path or find_antigravity_binary()

    def is_available(self) -> bool:
        if os.path.isabs(self.binary_path):
            return os.path.isfile(self.binary_path)
        return bool(shutil.which(self.binary_path))

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        if not self.is_available():
            return 127, "", f"Antigravity binary '{self.binary_path}' not found."

        cmd = [self.binary_path, "send-message", prompt]
        env = dict(os.environ)
        return run_safe_process_tree(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
