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

from .cockpit import CockpitServer, telemetry
from .config_env import load_env_file
from .core import WorkerType
from .loop import DispatchLoop
from .onboard import FleetConfig, run_interactive_onboarding
from .state import StateStore
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


def safe_print(*args, **kwargs) -> None:
    """Print helper preventing UnicodeEncodeError on Windows GBK / non-UTF-8 consoles."""
    file = kwargs.get("file", sys.stdout)
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(file, "encoding", "utf-8") or "utf-8"
        msg = sep.join(str(a) for a in args)
        safe_msg = msg.encode(encoding, errors="replace").decode(encoding)
        if file is not None and hasattr(file, "write"):
            file.write(safe_msg + end)
            if hasattr(file, "flush"):
                file.flush()


def print_banner(workspace: str, workers: list[str], cockpit_port: int | None = 8765) -> None:
    safe_print("\n" + "=" * 58)
    safe_print(" 🚀  PocketFleet — Your AI Engineering Squad in Your Pocket")
    safe_print("=" * 58)
    safe_print(f" 📂 Workspace   : {workspace}")
    safe_print(f" 🤖 Workers     : {', '.join(workers) if workers else 'None detected'}")
    if cockpit_port:
        safe_print(f" 🌐 Web Cockpit : http://127.0.0.1:{cockpit_port}")
    safe_print(" ⚡ Channel     : Telegram Bot (Listening...)")
    safe_print(" 🛡️ Architecture: Echo-Proof Single-direction DAG")
    safe_print("=" * 58)
    safe_print(" Press Ctrl+C to stop.\n")


def main(argv: list[str] | None = None) -> None:
    load_env_file(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description="PocketFleet — Telegram to Coding Agent Bridge")
    parser.add_argument("--init", action="store_true", help="Run interactive setup wizard to configure Bot Token & Chat ID")
    parser.add_argument("--ui", action="store_true", help="Launch Local Web Cockpit Dashboard in browser")
    parser.add_argument("--port", type=int, default=8765, help="Web Cockpit port (default: 8765)")
    parser.add_argument("--token", type=str, default=None, help="Telegram Bot Token (or env POCKETFLEET_BOT_TOKEN)")
    parser.add_argument("--cwd", type=str, default=None, help="Workspace root path (default current dir)")
    parser.add_argument(
        "--worker",
        type=str,
        default="auto",
        choices=["fleet_triad", "codex", "antigravity", "claude_code", "aider", "simulation", "auto"],
    )
    args = parser.parse_args(argv)

    if args.init:
        cfg = run_interactive_onboarding(args.cwd)
        if not cfg:
            sys.exit(1)
        print("Starting PocketFleet with newly generated config...")

    # 1. Singleton guard
    guard = SingleInstanceGuard()
    if not guard.acquire():
        print("[!] Another instance of PocketFleet is already running on this machine.", file=sys.stderr)
        sys.exit(0)

    try:
        # Load config from file or environment
        cfg_path = (Path(args.cwd) / "pocketfleet.json") if args.cwd else None
        try:
            saved_cfg = FleetConfig.load(cfg_path)
        except ValueError as exc:
            print(f"[!] Configuration error: {exc}", file=sys.stderr)
            sys.exit(1)

        if saved_cfg and saved_cfg.workspace_cwd:
            load_env_file(Path(saved_cfg.workspace_cwd) / ".env")

        env_var_name = saved_cfg.bot_token_env if (saved_cfg and saved_cfg.bot_token_env) else "POCKETFLEET_BOT_TOKEN"
        token = args.token or os.environ.get(env_var_name) or os.environ.get("POCKETFLEET_BOT_TOKEN")
        
        if not token:
            print("[!] No Bot Token found.", file=sys.stderr)
            if sys.stdin.isatty():
                print("    Launching setup wizard...\n")
                cfg = run_interactive_onboarding(args.cwd)
                if not cfg:
                    sys.exit(1)
                token = os.environ.get(cfg.bot_token_env)
                saved_cfg = cfg
            else:
                print("    Please run `pocketfleet --init` or pass `--token <TOKEN>`.", file=sys.stderr)
                sys.exit(1)

        allowed_chat_ids = {saved_cfg.allowed_chat_id} if (saved_cfg and saved_cfg.allowed_chat_id) else None
        workspace = str(Path(args.cwd).resolve()) if args.cwd else (
            saved_cfg.workspace_cwd if (saved_cfg and saved_cfg.workspace_cwd) else os.getcwd()
        )
        worker_str = args.worker if args.worker != "auto" else (
            saved_cfg.default_worker if (saved_cfg and saved_cfg.default_worker != "auto") else "auto"
        )

        state_store = StateStore()
        transport = TelegramTransport(bot_token=token, state_store=state_store)
        loop = DispatchLoop(
            transport=transport,
            workspace_cwd=workspace,
            default_worker=WorkerType(worker_str),
            allowed_chat_ids=allowed_chat_ids,
            state_store=state_store,
        )

        available_workers = [w.value for w in loop.get_available_workers()]

        # Initialize local telemetry & start Web Cockpit daemon
        telemetry.workspace = workspace
        telemetry.bot_username = (saved_cfg.bot_username if saved_cfg else "Configured")
        telemetry.allowed_chat_ids = list(allowed_chat_ids) if allowed_chat_ids else []
        telemetry.available_workers = available_workers

        cockpit = CockpitServer(port=args.port)
        cockpit.start(auto_open=args.ui)

        print_banner(workspace, available_workers, cockpit_port=args.port)
        if allowed_chat_ids:
            safe_print(f" 🔒 Security Whitelist: Chat ID {list(allowed_chat_ids)[0]} locked\n")

        loop.run_forever(poll_interval=1.0)
    finally:
        if 'cockpit' in locals():
            cockpit.stop()
        guard.release()


if __name__ == "__main__":
    main()
