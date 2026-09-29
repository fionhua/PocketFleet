import os
import shutil
import signal
import subprocess
import sys
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


def run_safe_process_tree(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int = 300,
) -> Tuple[int, str, str]:
    """Execute command in isolated process group with tree termination on timeout."""
    is_win = sys.platform == "win32"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_win else 0
    preexec_fn = None if is_win else getattr(os, "setsid", None)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            # Terminate entire process tree (P1-1 Fix)
            if is_win:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                except Exception:
                    proc.kill()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
            return -1, "", f"Task timed out after {timeout_sec} seconds (Process tree terminated)."
    except OSError as exc:
        return 1, "", f"Execution failed: {exc}"
