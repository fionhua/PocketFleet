"""PocketFleet Launcher & Console Entrypoint

Provides singleton mutex guard and clean terminal dashboard.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
from pathlib import Path

from .core import WorkerType
from .loop import DispatchLoop
from .transport.telegram import TelegramTransport

MUTEX_NAME = "Local\\PocketFleet_Singleton_Mutex"


class SingleInstanceGuard:
    """Windows Named Mutex Guard preventing duplicate instances."""

    def __init__(self, mutex_name: str = MUTEX_NAME) -> None:
        self.mutex_name = mutex_name
        self.mutex_handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if os.name == "nt":
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.windll.kernel32
            self.mutex_handle = kernel32.CreateMutexW(None, False, self.mutex_name)
            last_err = kernel32.GetLastError()
            if last_err == ERROR_ALREADY_EXISTS:
                self.already_running = True
                return False
            self.already_running = False
            return True
        return True

    def release(self) -> None:
        if os.name == "nt" and self.mutex_handle:
            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None


def print_banner(workspace: str, workers: list[str]) -> None:
    print("\n" + "=" * 58)
    print(" 🚀  PocketFleet — Your AI Engineering Squad in Your Pocket")
    print("=" * 58)
    print(f" 📂 Workspace   : {workspace}")
    print(f" 🤖 Workers     : {', '.join(workers) if workers else 'None detected'}")
    print(" ⚡ Channel     : Telegram Bot (Listening...)")
    print(" 🛡️ Architecture: Echo-Proof Single-direction DAG")
    print("=" * 58)
    print(" Press Ctrl+C to stop.\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="PocketFleet — Telegram to Coding Agent Bridge")
    parser.add_argument("--token", type=str, default=None, help="Telegram Bot Token (or env POCKETFLEET_BOT_TOKEN)")
    parser.add_argument("--cwd", type=str, default=None, help="Workspace root path (default current dir)")
    parser.add_argument("--worker", type=str, default="claude_code", choices=["claude_code", "aider", "auto"])
    args = parser.parse_args(argv)

    # 1. Singleton guard
    guard = SingleInstanceGuard()
    if not guard.acquire():
        print("[!] Another instance of PocketFleet is already running on this machine.", file=sys.stderr)
        sys.exit(0)

    try:
        token = args.token or os.environ.get("POCKETFLEET_BOT_TOKEN")
        if not token:
            print("[!] Missing Bot Token. Pass --token <TOKEN> or set POCKETFLEET_BOT_TOKEN.", file=sys.stderr)
            sys.exit(1)

        workspace = str(Path(args.cwd).resolve()) if args.cwd else os.getcwd()
        transport = TelegramTransport(bot_token=token)
        loop = DispatchLoop(
            transport=transport,
            workspace_cwd=workspace,
            default_worker=WorkerType(args.worker),
        )

        available_workers = [w.value for w in loop.get_available_workers()]
        print_banner(workspace, available_workers)

        loop.run_forever(poll_interval=1.0)
    finally:
        guard.release()


if __name__ == "__main__":
    main()
