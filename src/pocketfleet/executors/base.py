import os
import shutil
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Any, Tuple


class BaseExecutor(ABC):
    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the CLI tool is installed and executable in PATH."""
        raise NotImplementedError

    @abstractmethod
    def execute(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_sec: int = 300,
        cancel_event: Any | None = None,
    ) -> Tuple[int, str, str]:
        """Execute a coding task. Returns (exit_code, stdout, stderr)."""
        raise NotImplementedError


def check_executable(cmd_name: str) -> bool:
    """Helper to check if binary is in PATH."""
    return shutil.which(cmd_name) is not None


def kill_proc_tree(proc: subprocess.Popen) -> None:
    """Safely terminate a subprocess and all of its descendants."""
    is_win = sys.platform == "win32"
    if is_win:
        try:
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5, creationflags=no_win)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_safe_process_tree(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int = 300,
    cancel_event: Any | None = None,
) -> Tuple[int, str, str]:
    """Execute command in isolated process group with tree termination on timeout or cancellation."""
    is_win = sys.platform == "win32"
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP | no_win) if is_win else 0
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

        # Watchdog for heartbeat cancellation event
        if cancel_event is not None:
            def _watchdog():
                import time
                while proc.poll() is None:
                    if cancel_event.is_set():
                        kill_proc_tree(proc)
                        break
                    time.sleep(0.02)

            import threading
            threading.Thread(target=_watchdog, daemon=True).start()

        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            if cancel_event is not None and cancel_event.is_set():
                return -2, "", "Task cancelled: session lease was lost or heartbeat failed."
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            kill_proc_tree(proc)
            return -1, "", f"Task timed out after {timeout_sec} seconds (Process tree terminated)."
    except OSError as exc:
        return 1, "", f"Execution failed: {exc}"
