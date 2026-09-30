"""Independent OpenAI Codex CLI worker."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

from .base import BaseExecutor, run_safe_process_tree

logger = logging.getLogger(__name__)


def find_codex_binary() -> str:
    # 1. VS Code extension location
    candidates = sorted(
        (Path.home() / ".vscode" / "extensions").glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )
    if candidates and candidates[0].is_file():
        return str(candidates[0])
    
    # 2. PATH resolution
    return shutil.which("codex.cmd") or shutil.which("codex") or "codex"


class CodexExecutor(BaseExecutor):
    name: str = "codex"

    def __init__(self, binary_path: str | None = None) -> None:
        self.binary_path = binary_path or find_codex_binary()

    def is_available(self) -> bool:
        if os.path.isabs(self.binary_path):
            return os.path.isfile(self.binary_path)
        return bool(shutil.which(self.binary_path))

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        if not self.is_available():
            return 127, "", f"Codex binary '{self.binary_path}' not found."

        cmd = [
            self.binary_path,
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            "workspace-write",
            "--approve-for-me",
            prompt,
        ]
        env = dict(os.environ)
        return run_safe_process_tree(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
