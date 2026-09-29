import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.core import InboundMessage
from pocketfleet.loop import DispatchLoop
from pocketfleet.onboard import FleetConfig, detect_installed_agents, verify_bot_token
from pocketfleet.state import StateStore


class TestOnboard(unittest.TestCase):
    def test_fleet_config_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "test_config.json"
            cfg = FleetConfig(
                bot_token="123456:ABC-DEF",
                allowed_chat_id=987654321,
                bot_username="TestSquadBot",
                default_worker="aider",
                workspace_cwd="/tmp/workspace",
            )
            saved_path = cfg.save(cfg_file)
            self.assertEqual(saved_path, cfg_file)
            self.assertTrue(cfg_file.is_file())

            loaded = FleetConfig.load(cfg_file)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.bot_token, "123456:ABC-DEF")
            self.assertEqual(loaded.allowed_chat_id, 987654321)
            self.assertEqual(loaded.bot_username, "TestSquadBot")
            self.assertEqual(loaded.default_worker, "aider")

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
