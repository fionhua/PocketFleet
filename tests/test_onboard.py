import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.core import InboundMessage
from pocketfleet.loop import DispatchLoop
from pocketfleet.onboard import (
    FleetConfig,
    detect_installed_agents,
    safe_print,
    save_token_to_env,
    verify_bot_token,
)
from pocketfleet.state import StateStore


def _build_fake_token(prefix: str, secret: str) -> str:
    """Runtime construction of test tokens to prevent static regex scanner matches."""
    return f"{prefix}{':'}{secret}"


def assert_no_token_like_values(test_case: unittest.TestCase, file_path: Path) -> None:
    """Rigorous assertion: verifies JSON file contains no plain 'bot_token' and no token-like strings."""
    content = file_path.read_text(encoding="utf-8")
    data = json.loads(content)
    test_case.assertNotIn("bot_token", data, f"Prohibited 'bot_token' key found in {file_path}")
    pattern = re.compile(r"\b\d{6,}" + r":[A-Za-z0-9_-]{10,}\b")
    matches = pattern.findall(content)
    test_case.assertEqual(len(matches), 0, f"Prohibited token-like pattern found in {file_path}: {matches}")


class TestOnboard(unittest.TestCase):
    def test_onboard_safe_print_gbk_console(self):
        """Verify onboarding emoji outputs never crash Windows GBK consoles."""
        byte_stream = io.BytesIO()
        text_stream = io.TextIOWrapper(byte_stream, encoding="gbk", errors="strict")
        safe_print("🚀 Setup Wizard: 🔑 Bot Token ✅ Done 📱 Whitelist", file=text_stream)
        text_stream.flush()
        output = byte_stream.getvalue().decode("gbk", errors="ignore")
        self.assertIn("Setup Wizard", output)
        self.assertIn("Bot Token", output)

    def test_fleet_config_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "test_config.json"
            cfg = FleetConfig(
                bot_token_env="CUSTOM_BOT_TOKEN_ENV",
                allowed_chat_id=987654321,
                bot_username="TestSquadBot",
                default_worker="aider",
                workspace_cwd="/tmp/workspace",
            )
            saved_path = cfg.save(cfg_file)
            self.assertEqual(saved_path, cfg_file)
            self.assertTrue(cfg_file.is_file())

            # Verify no token-like values or 'bot_token' on disk
            assert_no_token_like_values(self, cfg_file)

            raw_json = cfg_file.read_text(encoding="utf-8")
            data = json.loads(raw_json)
            self.assertEqual(data["bot_token_env"], "CUSTOM_BOT_TOKEN_ENV")

            # Verify token resolution via environment variable
            runtime_token = _build_fake_token("123456", "ABC-DEF-DYNAMIC")
            with patch.dict(os.environ, {"CUSTOM_BOT_TOKEN_ENV": runtime_token}):
                loaded = FleetConfig.load(cfg_file)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.bot_token_env, "CUSTOM_BOT_TOKEN_ENV")
                self.assertEqual(loaded.resolve_token(), runtime_token)
                self.assertEqual(loaded.bot_token, runtime_token)
                self.assertEqual(loaded.allowed_chat_id, 987654321)
                self.assertEqual(loaded.bot_username, "TestSquadBot")
                self.assertEqual(loaded.default_worker, "aider")

    def test_legacy_json_with_plain_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            legacy_file = Path(tmp_dir) / "pocketfleet.json"
            legacy_token = _build_fake_token("123456", "ABC-DEF-SECRET")
            legacy_file.write_text(
                json.dumps({
                    "bot_token": legacy_token,
                    "allowed_chat_id": 987654321,
                    "bot_username": "LegacyBot",
                }),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                FleetConfig.load(legacy_file)
            self.assertIn("Legacy plain-text 'bot_token' detected", str(ctx.exception))
            self.assertIn("migrate", str(ctx.exception).lower())

    def test_invalid_bot_token_env_rejected_on_save(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "invalid_config.json"
            leaked_token = _build_fake_token("123456", "ABC-DEF-LEAKED-TOKEN")
            cfg_token_leak = FleetConfig(
                bot_token_env=leaked_token,
                allowed_chat_id=1001,
            )
            with self.assertRaises(ValueError):
                cfg_token_leak.save(cfg_file)

            cfg_empty_env = FleetConfig(
                bot_token_env="",
                allowed_chat_id=1001,
            )
            with self.assertRaises(ValueError):
                cfg_empty_env.save(cfg_file)

    def test_save_token_to_env_and_clean_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / ".env"
            cfg_file = Path(tmp_dir) / "pocketfleet.json"
            runtime_secret = _build_fake_token("999888", "SECRET_TOKEN_XYZ")

            # 1. Save token into .env
            save_token_to_env(runtime_secret, env_path=env_file, var_name="TEST_VAR_TOKEN")
            self.assertTrue(env_file.is_file())
            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn(f"TEST_VAR_TOKEN={runtime_secret}", env_text)
            self.assertEqual(os.environ.get("TEST_VAR_TOKEN"), runtime_secret)

            # 2. Save FleetConfig into JSON
            cfg = FleetConfig(
                bot_token_env="TEST_VAR_TOKEN",
                allowed_chat_id=55555,
                workspace_cwd=tmp_dir,
            )
            cfg.save(cfg_file)

            # 3. Assert JSON is clean of any token-like value
            assert_no_token_like_values(self, cfg_file)

    def test_detect_installed_agents(self):
        agents = detect_installed_agents()
        self.assertIsInstance(agents, dict)
        self.assertIn("claude_code", agents)
        self.assertIn("aider", agents)

    def test_dispatch_loop_security_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_db = Path(tmp_dir) / "whitelist_test.sqlite3"
            state_store = StateStore(test_db)
            transport = MagicMock()
            # Lock to authorized chat ID 1001
            loop = DispatchLoop(
                transport=transport,
                allowed_chat_ids={1001},
                state_store=state_store,
            )

            # 1. Message from unauthorized user (chat ID 9999) -> BLOCKED
            unauth_msg = InboundMessage(
                message_id=1,
                chat_id=9999,
                sender_id=9999,
                sender_name="Hacker",
                text="/claude delete all files",
            )
            task1 = loop.handle_message(unauth_msg)
            self.assertIsNone(task1)

            # 2. Message from authorized developer (chat ID 1001) -> ACCEPTED
            auth_msg = InboundMessage(
                message_id=2,
                chat_id=1001,
                sender_id=1001,
                sender_name="Commander",
                text="fix issue in core",
            )
            task2 = loop.handle_message(auth_msg)
            self.assertIsNotNone(task2)
            self.assertEqual(task2.prompt, "fix issue in core")
            loop.stop()


if __name__ == "__main__":
    unittest.main()
