"""Unit tests for PF-01 Three-Seat Configuration Layer

Decoupled pure-logic tests verifying FleetSeatsConfig as the sole configuration authority.
No graphical or GUI dependencies (Tkinter/Pillow) imported.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.core import (
    ALLOWED_CHAT_ENGINES,
    ALLOWED_CODE_ENGINES,
    FleetSeatsConfig,
    SeatConfig,
    SeatRole,
    get_default_seats_config,
    validate_seats_config,
)


class TestThreeSeatsConfig(unittest.TestCase):
    def test_default_seats_preset(self):
        """Verify the centralized default experimental triad preset."""
        cfg = get_default_seats_config()
        self.assertEqual(cfg.context_window, 20)
        self.assertTrue(cfg.no_situ)
        self.assertEqual(len(cfg.seats), 3)

        # 1. Chat Seat
        chat = cfg.seats[SeatRole.CHAT.value]
        self.assertEqual(chat.role, "chat")
        self.assertEqual(chat.name, "地球Sandbox")
        self.assertEqual(chat.engine, "gemini")
        self.assertEqual(chat.bot_token_env, "TELEGRAM_BOT_SANDBOX_TOKEN")
        self.assertEqual(chat.bot_username, "@AiSoulAlphaSandboxBot")
        self.assertIn("推演", chat.description)
        self.assertEqual(chat.command, "gemini")
        self.assertEqual(chat.read_watermark, 0)

        # 2. Lead Seat
        lead = cfg.seats[SeatRole.LEAD.value]
        self.assertEqual(lead.role, "lead")
        self.assertEqual(lead.name, "裁决者")
        self.assertEqual(lead.engine, "antigravity")
        self.assertEqual(lead.bot_token_env, "TELEGRAM_BOT_JUDGE_TOKEN")
        self.assertEqual(lead.bot_username, "@AiSoulJudgeBot")
        self.assertEqual(lead.command, "agy")
        self.assertEqual(lead.read_watermark, 0)

        # 3. Builder Seat
        builder = cfg.seats[SeatRole.BUILDER.value]
        self.assertEqual(builder.role, "builder")
        self.assertEqual(builder.name, "泥蛇")
        self.assertEqual(builder.engine, "codex")
        self.assertEqual(builder.bot_token_env, "TELEGRAM_BOT_MUDSNAKE_TOKEN")
        self.assertEqual(builder.bot_username, "@AiSoulMudSnakeBot")
        self.assertEqual(builder.command, "codex")
        self.assertEqual(builder.read_watermark, 0)

        # No errors raised
        validate_seats_config(cfg)

    def test_save_and_reload_round_trip(self):
        """Verify configuration persists and restores losslessly via FleetSeatsConfig."""
        cfg = get_default_seats_config()
        cfg.context_window = 25
        cfg.no_situ = True
        cfg.seats["chat"].command = "gemini-cli --stream"
        cfg.seats["chat"].read_watermark = 1001
        cfg.seats["lead"].read_watermark = 1002
        cfg.seats["builder"].read_watermark = 1003

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "pocketfleet_test.json"
            cfg.save_to_file(file_path)

            loaded = FleetSeatsConfig.load_from_file(file_path)
            self.assertEqual(loaded.context_window, 25)
            self.assertTrue(loaded.no_situ)
            self.assertEqual(loaded.seats["chat"].command, "gemini-cli --stream")
            self.assertEqual(loaded.seats["chat"].read_watermark, 1001)
            self.assertEqual(loaded.seats["lead"].read_watermark, 1002)
            self.assertEqual(loaded.seats["builder"].read_watermark, 1003)
            self.assertEqual(loaded.seats["chat"].name, "地球Sandbox")
            self.assertEqual(loaded.seats["lead"].name, "裁决者")
            self.assertEqual(loaded.seats["builder"].name, "泥蛇")

    def test_missing_seat_fails_loud(self):
        """Configuration missing any of the 3 required seats must raise ValueError."""
        cfg = get_default_seats_config()
        del cfg.seats["chat"]
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Missing required seat(s)", str(ctx.exception))
        self.assertIn("chat", str(ctx.exception))

    def test_invalid_chat_engine_fails_loud(self):
        """Chat seat cannot use code engines or arbitrary unknown engines."""
        cfg = get_default_seats_config()
        cfg.seats["chat"].engine = "codex"  # Code engine in chat seat
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Invalid chat engine 'codex'", str(ctx.exception))

        cfg.seats["chat"].engine = "unknown_llm"
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Invalid chat engine 'unknown_llm'", str(ctx.exception))

    def test_invalid_code_engine_fails_loud(self):
        """Code seats (lead/builder) cannot use chat engines or unknown engines."""
        cfg = get_default_seats_config()
        cfg.seats["lead"].engine = "gemini"  # Chat engine in code seat
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Invalid code engine 'gemini'", str(ctx.exception))

        cfg.seats["lead"].engine = "antigravity"
        cfg.seats["builder"].engine = "my_custom_script"
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Invalid code engine 'my_custom_script'", str(ctx.exception))

    def test_plain_text_token_fails_loud(self):
        """Plain-text Telegram tokens containing ':' or spaces are strictly forbidden."""
        cfg = get_default_seats_config()
        # Mock placeholder token that is impossible to be valid
        cfg.seats["lead"].bot_token_env = "MOCK_INVALID_PREFIX:MOCK_TOKEN_VALUE_FORBIDDEN_123"
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("must be a valid environment variable name, not a plain token", str(ctx.exception))

        cfg.seats["lead"].bot_token_env = "INVALID ENV NAME WITH SPACES"
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("not a plain token", str(ctx.exception))

    def test_duplicate_bot_token_env_fails_loud(self):
        """Reusing the same bot token env across different seats must fail loudly."""
        cfg = get_default_seats_config()
        cfg.seats["builder"].bot_token_env = cfg.seats["lead"].bot_token_env
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Duplicate bot_token_env", str(ctx.exception))

    def test_empty_seat_name_or_token_env_fails_loud(self):
        """Empty names or missing token envs must fail immediately."""
        cfg = get_default_seats_config()
        cfg.seats["chat"].name = ""
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("has empty name", str(ctx.exception))

        cfg = get_default_seats_config()
        cfg.seats["chat"].bot_token_env = ""
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("must specify a bot_token_env", str(ctx.exception))

    def test_context_window_validation_fails_loud(self):
        """context_window (最近消息条数) must be a positive integer."""
        cfg = get_default_seats_config()
        cfg.context_window = 0
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("context_window (最近消息条数) must be a positive integer", str(ctx.exception))

        cfg.context_window = -10
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("must be a positive integer", str(ctx.exception))

    def test_seat_role_key_mismatch_fails_loud(self):
        """seat.role must strictly match its dictionary key."""
        cfg = get_default_seats_config()
        cfg.seats["chat"].role = "lead"
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("does not match dictionary key 'chat'", str(ctx.exception))

    def test_bot_username_validation(self):
        """Bot username must be non-empty and unique across seats."""
        cfg = get_default_seats_config()
        cfg.seats["chat"].bot_username = ""
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("must specify a non-empty bot_username", str(ctx.exception))

        cfg = get_default_seats_config()
        cfg.seats["builder"].bot_username = cfg.seats["lead"].bot_username
        with self.assertRaises(ValueError) as ctx:
            validate_seats_config(cfg)
        self.assertIn("Duplicate bot_username", str(ctx.exception))

    def test_corrupted_json_file_fails_loud(self):
        """Loading a corrupted/invalid JSON config must raise an error, never silently downgrade."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "corrupted_config.json"
            file_path.write_text("{ this is corrupted invalid json }}}", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                FleetSeatsConfig.load_from_file(file_path)

    def test_missing_file_raises_not_found(self):
        """load_from_file raises FileNotFoundError if target does not exist."""
        with self.assertRaises(FileNotFoundError):
            FleetSeatsConfig.load_from_file(Path("non_existent_pocketfleet_config.json"))

    def test_auto_migration_legacy_config_without_seats(self):
        """Verify legacy pocketfleet.json without 'seats' is smoothly auto-migrated."""
        from pocketfleet.control_panel import FleetManager
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_file = Path(tmpdir) / "pocketfleet.json"
            # Write old-format JSON missing 'seats'
            legacy_data = {
                "context_window": 30,
                "no_situ": False,
                "authorized_user_ids": [123456],
                "bots": {
                    "antigravity": {"name": "Old Bot"}
                }
            }
            legacy_file.write_text(json.dumps(legacy_data), encoding="utf-8")

            with mock.patch("pocketfleet.control_panel.CONFIG_FILE", legacy_file):
                mgr = FleetManager(log_cb=lambda msg: None)
                # Must NOT raise ValueError; must smoothly auto-migrate and return valid FleetSeatsConfig
                migrated = mgr.load_seats_config()
                self.assertEqual(migrated.context_window, 30)
                self.assertFalse(migrated.no_situ)
                self.assertEqual(migrated.authorized_user_ids, [123456])
                self.assertEqual(len(migrated.seats), 3)
                self.assertIn("lead", migrated.seats)
                self.assertIn("builder", migrated.seats)
                self.assertIn("chat", migrated.seats)

                # Verify written back to disk
                disk_data = json.loads(legacy_file.read_text(encoding="utf-8"))
                self.assertIn("seats", disk_data)

    def test_engine_command_auto_binding(self):
        """Verify get_default_command_for_engine correctly maps engines to executables and binds automatically."""
        from pocketfleet.core import get_default_command_for_engine, SeatConfig

        self.assertEqual(get_default_command_for_engine("antigravity"), "agy")
        self.assertEqual(get_default_command_for_engine("gemini"), "gemini")
        self.assertEqual(get_default_command_for_engine("codex"), "codex")
        self.assertEqual(get_default_command_for_engine("claude_code"), "claude")
        self.assertEqual(get_default_command_for_engine("aider"), "aider")

        # SeatConfig from_dict with empty command automatically binds default
        seat = SeatConfig.from_dict({
            "role": "lead",
            "name": "裁决者",
            "engine": "antigravity",
            "bot_token_env": "TELEGRAM_BOT_JUDGE_TOKEN",
            "bot_username": "@bot",
            "description": "施工指挥",
            "command": "",
        })
        self.assertEqual(seat.command, "agy")


if __name__ == "__main__":
    unittest.main()
