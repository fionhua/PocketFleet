"""Tests for PocketFleet Launcher and Singleton Guard"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet import launcher
from pocketfleet.onboard import FleetConfig


def _build_fake_token(prefix: str, secret: str) -> str:
    """Runtime construction of test tokens to prevent static regex scanner matches."""
    return f"{prefix}{':'}{secret}"


def assert_no_token_like_values(test_case: unittest.TestCase, file_path: Path) -> None:
    """Verifies that the JSON file contains no 'bot_token' key and no token-like strings."""
    content = file_path.read_text(encoding="utf-8")
    data = json.loads(content)
    test_case.assertNotIn("bot_token", data, f"Prohibited 'bot_token' key found in {file_path}")
    pattern = re.compile(r"\b\d{6,}" + r":[A-Za-z0-9_-]{10,}\b")
    matches = pattern.findall(content)
    test_case.assertEqual(len(matches), 0, f"Prohibited token-like pattern found in {file_path}: {matches}")


class TestLauncher(unittest.TestCase):
    def test_single_instance_guard(self):
        guard1 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")
        guard2 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")

        self.assertTrue(guard1.acquire())
        if sys.platform == "win32":
            self.assertFalse(guard2.acquire())
            self.assertTrue(guard2.already_running)

        guard1.release()
        guard2.release()

    def test_launcher_main_missing_token(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            with mock.patch("sys.stdin.isatty", return_value=False):
                with mock.patch.dict(launcher.os.environ, {}, clear=True):
                    with self.assertRaises(SystemExit) as cm:
                        launcher.main(argv=["--cwd", empty_dir])
                    self.assertEqual(cm.exception.code, 1)

    def test_print_banner_gbk_console_does_not_crash(self):
        """Regression test: verify banner emojis print cleanly on Windows GBK console streams."""
        byte_stream = io.BytesIO()
        text_stream = io.TextIOWrapper(byte_stream, encoding="gbk", errors="strict")
        with mock.patch("sys.stdout", text_stream):
            launcher.print_banner(workspace="D:\\PocketFleet", workers=["antigravity", "codex"])
        text_stream.flush()
        output = byte_stream.getvalue().decode("gbk", errors="ignore")
        self.assertIn("PocketFleet", output)
        self.assertIn("Workspace", output)

    def test_launcher_resolves_token_via_bot_token_env(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "pocketfleet.json"
            cfg = FleetConfig(
                bot_token_env="CUSTOM_BOT_TOKEN_ENV",
                allowed_chat_id=123456,
                workspace_cwd=tmp_dir,
            )
            cfg.save(cfg_path)

            # Assert JSON on disk contains no token-like patterns or 'bot_token'
            assert_no_token_like_values(self, cfg_path)

            fake_token = _build_fake_token("987654", "VALID_TELEGRAM_TOKEN_RUNTIME")
            env_vars = {"CUSTOM_BOT_TOKEN_ENV": fake_token}
            with mock.patch.dict(launcher.os.environ, env_vars, clear=True):
                with mock.patch.object(launcher.SingleInstanceGuard, "acquire", return_value=True):
                    with mock.patch("pocketfleet.launcher.TelegramTransport") as mock_transport_cls:
                        with mock.patch("pocketfleet.launcher.DispatchLoop") as mock_loop_cls:
                            with mock.patch("pocketfleet.launcher.CockpitServer"):
                                mock_loop_inst = mock_loop_cls.return_value
                                mock_loop_inst.get_available_workers.return_value = []
                                mock_loop_inst.run_forever.side_effect = KeyboardInterrupt

                                try:
                                    launcher.main(argv=["--cwd", tmp_dir])
                                except KeyboardInterrupt:
                                    pass

                                mock_transport_cls.assert_called_once()
                                _, kwargs = mock_transport_cls.call_args
                                self.assertEqual(kwargs.get("bot_token"), fake_token)

    def test_launcher_rejects_legacy_plain_bot_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "pocketfleet.json"
            legacy_token = _build_fake_token("123456", "LEGACY_PLAIN_TOKEN_OFFENDING")
            cfg_path.write_text(
                json.dumps({
                    "bot_token": legacy_token,
                    "allowed_chat_id": 123456,
                }),
                encoding="utf-8",
            )
            with mock.patch.object(launcher.SingleInstanceGuard, "acquire", return_value=True):
                with self.assertRaises(SystemExit) as cm:
                    launcher.main(argv=["--cwd", tmp_dir])
                self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
