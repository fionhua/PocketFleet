"""Tests for Antigravity Track Controller and GUI contract (PF-02B).

These tests run purely headless without requiring a physical Tk display,
without touching real .gemini state, and mocking subprocess.Popen.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.antigravity_tracks import (
    AntigravityTrackController,
    ImportDetectionError,
    ImportRequiredError,
    TrackCandidate,
    snapshot_cli_track_ids,
)


class TestAntigravityTracksController(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.env_file = self.workspace / ".env"
        self.controller = AntigravityTrackController(
            workspace_cwd=self.workspace,
            env_path=self.env_file,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_row_formatting_and_binding_highlight(self) -> None:
        """Verify candidate row formatting: source label, size, date, and bound mark."""
        cid = "33333333-4444-5555-6666-777777777777"
        now = 1774880000.0  # deterministic timestamp
        cli_cand = TrackCandidate(
            source="cli",
            conversation_id=cid,
            db_path=self.workspace / f"{cid}.db",
            brain_path=None,
            last_activity=now,
            total_bytes=1048576,  # 1 MB
        )

        # 1. Row formatted when NOT bound
        row_unbound = self.controller.format_row(cli_cand, bound_id="other-uuid")
        self.assertEqual(row_unbound["source"], "CLI 外勤轨")
        self.assertEqual(row_unbound["total_bytes"], "1.00 MB")
        self.assertEqual(row_unbound["uuid"], cid)
        self.assertEqual(row_unbound["bound"], "")
        self.assertEqual(row_unbound["is_bound"], "False")

        # 2. Row formatted when BOUND
        row_bound = self.controller.format_row(cli_cand, bound_id=cid)
        self.assertEqual(row_bound["bound"], "★ 当前绑定")
        self.assertEqual(row_bound["is_bound"], "True")

        # 3. IDE candidate source label
        ide_cand = TrackCandidate(
            source="ide",
            conversation_id=cid,
            db_path=self.workspace / f"{cid}.db",
            brain_path=None,
            last_activity=now,
            total_bytes=51200,  # 50 KB
        )
        row_ide = self.controller.format_row(ide_cand)
        self.assertEqual(row_ide["source"], "IDE 原生轨")
        self.assertEqual(row_ide["total_bytes"], "50.0 KB")

    def test_bind_ide_track_auto_clones_and_binds(self) -> None:
        """Verify seamless auto-cloning and binding of an IDE track."""
        src_db = self.workspace / "test_ide.db"
        src_db.write_bytes(b"mock ide sqlite db")
        ide_cand = TrackCandidate(
            source="ide",
            conversation_id="aaaaaaaa-1111-2222-3333-444444444444",
            db_path=src_db,
            brain_path=None,
            last_activity=time.time(),
            total_bytes=100000,
        )

        bound_path = self.controller.bind_track(
            ide_cand,
            is_daemon_running=False,
            cli_root=self.workspace / "cli_root",
        )
        self.assertTrue(bound_path.is_file())
        self.assertEqual(self.controller.get_current_bound_id(), "aaaaaaaa-1111-2222-3333-444444444444")

    def test_bind_when_daemon_running_rejected(self) -> None:
        """Enforce rejection when Telegram Bridge Daemon is running to prevent 409 conflict."""
        cli_cand = TrackCandidate(
            source="cli",
            conversation_id="bbbbbbbb-1111-2222-3333-444444444444",
            db_path=self.workspace / "test.db",
            brain_path=None,
            last_activity=time.time(),
            total_bytes=100000,
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.controller.bind_track(cli_cand, is_daemon_running=True)
        self.assertIn("Telegram Bridge Daemon 正在运行", str(ctx.exception))
        self.assertIn("409 Conflict", str(ctx.exception))

    def test_bind_cli_track_success_and_env_updated(self) -> None:
        """Verify successful binding writes to .env without corrupting existing keys or comments."""
        self.env_file.write_text(
            "# Custom configuration\n"
            "TELEGRAM_BOT_JUDGE_TOKEN=mock_lead_token\n"
            "EXISTING_CUSTOM_KEY=keep_this_intact\n",
            encoding="utf-8",
        )

        target_cid = "cccccccc-1111-2222-3333-444444444444"
        cli_cand = TrackCandidate(
            source="cli",
            conversation_id=target_cid,
            db_path=self.workspace / "test.db",
            brain_path=None,
            last_activity=time.time(),
            total_bytes=200000,
        )

        self.controller.bind_track(cli_cand, is_daemon_running=False)

        updated_env = self.env_file.read_text(encoding="utf-8")
        self.assertIn("# Custom configuration", updated_env)
        self.assertIn("TELEGRAM_BOT_JUDGE_TOKEN=mock_lead_token", updated_env)
        self.assertIn("EXISTING_CUSTOM_KEY=keep_this_intact", updated_env)
        self.assertIn(f"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID={target_cid}", updated_env)
        self.assertEqual(self.controller.get_current_bound_id(), target_cid)

    def test_launch_interactive_import_terminal_contract(self) -> None:
        """Verify Popen invocation contract: shell=False, correct cwd, array args, creationflags."""
        cli_dir = self.workspace / "mock_cli"
        cli_conv = cli_dir / "conversations"
        cli_conv.mkdir(parents=True)
        (cli_conv / "11111111-0000-0000-0000-000000000000.db").write_bytes(b"data")

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_popen.return_value = mock_proc

            proc = self.controller.launch_interactive_import_terminal(
                binary_path="agy",
                cli_root=cli_dir,
            )
            self.assertEqual(proc, mock_proc)
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args

            # Check contract
            self.assertEqual(args[0], ["agy"])
            self.assertFalse(kwargs.get("shell", True))
            self.assertEqual(kwargs.get("cwd"), str(self.workspace))

            if os.name == "nt":
                expected_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
                self.assertEqual(kwargs.get("creationflags"), expected_flag)

            # Check that snapshot was recorded
            self.assertEqual(
                self.controller.import_snapshot,
                {"11111111-0000-0000-0000-000000000000"},
            )

    def test_detect_import_result_scenarios(self) -> None:
        """Test import detection: uninitialized, 0 new tracks, >1 new tracks, exactly 1."""
        cli_dir = self.workspace / "mock_cli"
        cli_conv = cli_dir / "conversations"
        cli_conv.mkdir(parents=True)

        existing_id = "11111111-0000-0000-0000-000000000000"
        (cli_conv / f"{existing_id}.db").write_bytes(b"data")

        # 1. Unprepared: calling without snapshot
        self.controller.import_snapshot = None
        with self.assertRaises(ImportDetectionError) as ctx_unprep:
            self.controller.detect_import_result(cli_root=cli_dir)
        self.assertIn("未找到导入前快照", str(ctx_unprep.exception))

        # Prepare snapshot
        self.controller.prepare_official_import(cli_root=cli_dir)

        # 2. 0 new tracks
        with self.assertRaises(ImportDetectionError) as ctx_zero:
            self.controller.detect_import_result(cli_root=cli_dir)
        self.assertIn("No new CLI conversation track detected", str(ctx_zero.exception))

        # 3. >1 new tracks -> Ambiguous
        new_id_1 = "22222222-0000-0000-0000-000000000000"
        new_id_2 = "33333333-0000-0000-0000-000000000000"
        (cli_conv / f"{new_id_1}.db").write_bytes(b"new1")
        (cli_conv / f"{new_id_2}.db").write_bytes(b"new2")

        with self.assertRaises(ImportDetectionError) as ctx_multi:
            self.controller.detect_import_result(cli_root=cli_dir)
        self.assertIn("Ambiguous import", str(ctx_multi.exception))

        # 4. Exactly 1 new track -> Success
        (cli_conv / f"{new_id_2}.db").unlink()
        candidate = self.controller.detect_import_result(cli_root=cli_dir)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.conversation_id, new_id_1)
        self.assertEqual(candidate.source, "cli")


if __name__ == "__main__":
    unittest.main()
