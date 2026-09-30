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
import re
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

_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_LIKE_PATTERN = re.compile(r"\b\d{6,}" + r":[A-Za-z0-9_-]{10,}\b")


def save_token_to_env(
    token: str,
    env_path: Path | None = None,
    var_name: str = "POCKETFLEET_BOT_TOKEN",
) -> Path:
    """Safely append or update the bot token in local .env and set in os.environ."""
    target = env_path or (Path.cwd() / ".env")
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    replaced = False
    if target.is_file():
        content = target.read_text(encoding="utf-8-sig")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var_name}=") or stripped.startswith(f"export {var_name}="):
                lines.append(f"{var_name}={token}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"{var_name}={token}")

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[var_name] = token
    return target


@dataclass
class FleetConfig:
    bot_token_env: str = "POCKETFLEET_BOT_TOKEN"
    allowed_chat_id: int = 0
    bot_username: str = ""
    default_worker: str = "auto"
    workspace_cwd: str = ""
    created_at: str = ""

    def resolve_token(self) -> str | None:
        """Resolve the real Telegram Bot Token from environment via bot_token_env."""
        token_env = (self.bot_token_env or "").strip()
        if not token_env:
            return None
        return os.environ.get(token_env)

    @property
    def bot_token(self) -> str | None:
        """Dynamic resolution of bot token from environment (read-only, never persisted)."""
        return self.resolve_token()

    def save(self, target_path: Path | None = None) -> Path:
        path = target_path or Path(CONFIG_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)

        token_env = (self.bot_token_env or "").strip()
        if not token_env or ":" in token_env or not _ENV_VAR_PATTERN.match(token_env):
            raise ValueError(
                f"bot_token_env must be a valid environment variable name, got '{self.bot_token_env}'"
            )

        data = {
            "bot_token_env": token_env,
            "allowed_chat_id": self.allowed_chat_id,
            "bot_username": self.bot_username,
            "default_worker": self.default_worker,
            "workspace_cwd": self.workspace_cwd,
            "created_at": self.created_at,
        }

        # Extra defense: ensure no token-like string ever gets serialized to JSON
        serialized = json.dumps(data, indent=2, ensure_ascii=False)
        if _TOKEN_LIKE_PATTERN.search(serialized):
            raise ValueError("Prohibited token-like value detected during FleetConfig serialization.")

        path.write_text(serialized, encoding="utf-8")
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
                    raw_text = p.read_text(encoding="utf-8")
                    data = json.loads(raw_text)
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                # P0 fail-loud security gate: forbid legacy plain-text bot_token in JSON
                if "bot_token" in data:
                    raise ValueError(
                        f"Legacy plain-text 'bot_token' detected in '{p}'. "
                        "Plain-text tokens in JSON configurations are prohibited. "
                        "Please migrate your token to your local .env file (e.g. POCKETFLEET_BOT_TOKEN=...) "
                        "and update the JSON config to specify 'bot_token_env' instead."
                    )

                # Skip seats config (which has "seats" dictionary rather than single-bot config)
                if "seats" in data and "bot_token_env" not in data and "allowed_chat_id" not in data:
                    continue

                token_env = str(data.get("bot_token_env", "POCKETFLEET_BOT_TOKEN")).strip()
                if ":" in token_env or not _ENV_VAR_PATTERN.match(token_env):
                    raise ValueError(
                        f"Invalid 'bot_token_env' in '{p}': '{token_env}' looks like a token or invalid env var name."
                    )

                return cls(
                    bot_token_env=token_env,
                    allowed_chat_id=int(data.get("allowed_chat_id", 0)),
                    bot_username=str(data.get("bot_username", "")),
                    default_worker=str(data.get("default_worker", "auto")),
                    workspace_cwd=str(data.get("workspace_cwd", "")),
                    created_at=str(data.get("created_at", "")),
                )
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


def run_interactive_onboarding(default_cwd: str | None = None) -> FleetConfig | None:
    """Run full interactive console wizard."""
    safe_print("\n" + "=" * 62)
    safe_print(" 🚀 Welcome to PocketFleet Setup Wizard (60-second Quick Start)")
    safe_print("=" * 62)
    safe_print(" Stop babysitting your CLI. Ship code from Telegram.")
    safe_print("=" * 62 + "\n")

    # Step 1: Detect AI Engines
    safe_print("🔍 [1/3] Detecting Local Coding Agents...")
    agents = detect_installed_agents()
    if agents["claude_code"]:
        safe_print("  ✅ Claude Code CLI detected (found 'claude' in PATH)")
    else:
        safe_print("  ⚠️ Claude Code not found. (Install with: npm install -g @anthropic-ai/claude-code)")

    if agents["aider"]:
        safe_print("  ✅ Aider detected (found 'aider' in PATH)")
    else:
        safe_print("  ⚠️ Aider not found. (Install with: pip install aider-chat)")

    if not agents["claude_code"] and not agents["aider"]:
        safe_print("\n  [!] Tip: You need at least one coding agent installed to execute tasks.")
        safe_print("      PocketFleet will still initialize and wait for your installation.\n")

    # Step 2: Telegram Bot Token
    safe_print("\n🔑 [2/3] Telegram Bot Token Setup:")
    safe_print("  Create a free bot on Telegram by talking to @BotFather, then paste your token here.")
    token = ""
    while not token:
        try:
            token = input("  Enter Telegram Bot Token: ").strip()
        except (EOFError, KeyboardInterrupt):
            safe_print("\nSetup cancelled.")
            return None
        if not token:
            safe_print("  [!] Token cannot be empty.")
            continue

        safe_print("  Verifying token with Telegram API...", end="", flush=True)
        bot_info = verify_bot_token(token)
        if bot_info:
            bot_username = bot_info.get("username", "UnknownBot")
            safe_print(f" Connected! (@{bot_username})\n")
            break
        else:
            safe_print(" Failed. Invalid token or network error. Please re-enter.\n")
            token = ""

    # Step 3: Pair Chat ID
    safe_print(f"📱 [3/3] Pairing Security Whitelist:")
    safe_print(f"  Open Telegram on your phone, find @{bot_username}, and send: /start")
    safe_print("  Waiting for your message (timeout 60s)...", end="", flush=True)

    chat_id = wait_for_user_chat_id(token, timeout_seconds=60)
    if chat_id:
        safe_print(f" Paired! (Your Chat ID: {chat_id})")
        safe_print("  🔒 Security: PocketFleet will ONLY accept orders from this Chat ID.")
    else:
        safe_print(" Timeout waiting for message.")
        manual_id = input("  Enter your Chat ID manually (or press Enter to skip): ").strip()
        chat_id = int(manual_id) if manual_id.isdigit() else 0

    workspace = default_cwd or os.getcwd()
    env_file = save_token_to_env(token, env_path=Path(workspace) / ".env")
    safe_print(f"  🔐 Token securely saved to: {env_file.resolve()} (env var: POCKETFLEET_BOT_TOKEN)")

    config = FleetConfig(
        bot_token_env="POCKETFLEET_BOT_TOKEN",
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

    safe_print(f"\n🎉 Setup complete! Configuration saved to: {saved_path.resolve()}")
    safe_print("Run `pocketfleet` anytime to start your 24/7 coding squad!\n")
    return config
