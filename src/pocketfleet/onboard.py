"""PocketFleet Interactive Onboarding & Environment Doctor

Guides developers through a 60-second setup:
1. Validates Telegram Bot Token with Telegram API (`getMe`)
2. Auto-discovers and pairs with developer's personal Chat ID (`/start`)
3. Checks local execution environment for Claude Code and Aider
4. Saves secured configuration profile
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "pocketfleet.json"
GLOBAL_CONFIG_DIR = Path.home() / ".pocketfleet"
GLOBAL_CONFIG_PATH = GLOBAL_CONFIG_DIR / "config.json"


@dataclass
class FleetConfig:
    bot_token: str
    allowed_chat_id: int
    bot_username: str = ""
    default_worker: str = "auto"
    workspace_cwd: str = ""
    created_at: str = ""

    def save(self, target_path: Path | None = None) -> Path:
        path = target_path or Path(CONFIG_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> FleetConfig | None:
        candidates = [
            path,
            Path(CONFIG_FILENAME),
            GLOBAL_CONFIG_PATH,
        ]
        for p in candidates:
            if p and p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    return cls(**data)
                except Exception:
                    continue
        return None


def verify_bot_token(token: str) -> dict[str, Any] | None:
    """Validate token via getMe endpoint."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PocketFleet-Doctor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", {})
    except Exception:
        return None
    return None


def wait_for_user_chat_id(token: str, timeout_seconds: int = 60) -> int | None:
    """Poll for incoming /start message to bind developer's Chat ID automatically."""
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=5"
    start_time = time.time()
    offset = None

    while time.time() - start_time < timeout_seconds:
        poll_url = f"{url}&offset={offset}" if offset else url
        try:
            req = urllib.request.Request(poll_url, headers={"User-Agent": "PocketFleet-Doctor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for update in data.get("result", []):
                    offset = update.get("update_id", 0) + 1
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    if chat_id:
                        return int(chat_id)
        except Exception:
            pass
        time.sleep(1.5)
    return None


def detect_installed_agents() -> dict[str, bool]:
    """Check if claude or aider CLI executables are discoverable in PATH."""
    return {
        "claude_code": shutil.which("claude") is not None,
        "aider": shutil.which("aider") is not None,
    }


def run_interactive_onboarding(default_cwd: str | None = None) -> FleetConfig | None:
    """Run full interactive console wizard."""
    print("\n" + "=" * 62)
    print(" 🚀 Welcome to PocketFleet Setup Wizard (60-second Quick Start)")
    print("=" * 62)
    print(" Stop babysitting your CLI. Ship code from Telegram.")
    print("=" * 62 + "\n")

    # Step 1: Detect AI Engines
    print("🔍 [1/3] Detecting Local Coding Agents...")
    agents = detect_installed_agents()
    if agents["claude_code"]:
        print("  ✅ Claude Code CLI detected (found 'claude' in PATH)")
    else:
        print("  ⚠️ Claude Code not found. (Install with: npm install -g @anthropic-ai/claude-code)")

    if agents["aider"]:
        print("  ✅ Aider detected (found 'aider' in PATH)")
    else:
        print("  ⚠️ Aider not found. (Install with: pip install aider-chat)")

    if not agents["claude_code"] and not agents["aider"]:
        print("\n  [!] Tip: You need at least one coding agent installed to execute tasks.")
        print("      PocketFleet will still initialize and wait for your installation.\n")

    # Step 2: Telegram Bot Token
    print("\n🔑 [2/3] Telegram Bot Token Setup:")
    print("  Create a free bot on Telegram by talking to @BotFather, then paste your token here.")
    token = ""
    while not token:
        try:
            token = input("  Enter Telegram Bot Token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            return None
        if not token:
            print("  [!] Token cannot be empty.")
            continue

        print("  Verifying token with Telegram API...", end="", flush=True)
        bot_info = verify_bot_token(token)
        if bot_info:
            bot_username = bot_info.get("username", "UnknownBot")
            print(f" Connected! (@{bot_username})\n")
            break
        else:
            print(" Failed. Invalid token or network error. Please re-enter.\n")
            token = ""

    # Step 3: Pair Chat ID
    print(f"📱 [3/3] Pairing Security Whitelist:")
    print(f"  Open Telegram on your phone, find @{bot_username}, and send: /start")
    print("  Waiting for your message (timeout 60s)...", end="", flush=True)

    chat_id = wait_for_user_chat_id(token, timeout_seconds=60)
    if chat_id:
        print(f" Paired! (Your Chat ID: {chat_id})")
        print("  🔒 Security: PocketFleet will ONLY accept orders from this Chat ID.")
    else:
        print(" Timeout waiting for message.")
        manual_id = input("  Enter your Chat ID manually (or press Enter to skip): ").strip()
        chat_id = int(manual_id) if manual_id.isdigit() else 0

    workspace = default_cwd or os.getcwd()
    config = FleetConfig(
        bot_token=token,
        allowed_chat_id=chat_id,
        bot_username=bot_username,
        default_worker="auto",
        workspace_cwd=workspace,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    saved_path = config.save()
    # Also save globally as fallback
    try:
        config.save(GLOBAL_CONFIG_PATH)
    except Exception:
        pass

    print(f"\n🎉 Setup complete! Configuration saved to: {saved_path.resolve()}")
    print("Run `pocketfleet` anytime to start your 24/7 coding squad!\n")
    return config
