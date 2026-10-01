"""Unit tests for Antigravity track discovery, import detection, and CLI track binding."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pocketfleet.antigravity_tracks import (
    DEFAULT_TRACK_MIN_BYTES,
    AntigravityTrackController,
    AntigravityTrackError,
    ImportDetectionError,
    ImportRequiredError,
    TrackCandidate,
    bind_conversation_id_to_env,
    bind_track_candidate,
    clone_ide_track_to_cli,
    detect_new_imported_track,
    inspect_track_candidate,
    is_valid_uuid,
    notify_ide_handover,
    safe_snapshot_sqlite,
    scan_all_candidates,
    scan_candidates,
    snapshot_cli_track_ids,
    write_handover_dossier,
)
from pocketfleet.executors.antigravity import AntigravityExecutor


class TestAntigravityTracks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ide_root = self.root / "antigravity-ide"
        self.cli_root = self.root / "antigravity-cli"
        self.ide_root.mkdir(parents=True)
        self.cli_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_uuid_validation(self) -> None:
        valid_uuid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.assertTrue(is_valid_uuid(valid_uuid))
        self.assertTrue(is_valid_uuid(valid_uuid.upper()))

        # Invalid cases
        self.assertFalse(is_valid_uuid(""))
        self.assertFalse(is_valid_uuid("not-a-uuid"))
        self.assertFalse(is_valid_uuid("56b7c33a-d1df-4b11-9363-78bb32e32c2g"))  # invalid hex
        self.assertFalse(is_valid_uuid("56b7c33a-d1df-4b11-9363"))
        self.assertFalse(is_valid_uuid("12345"))
        self.assertFalse(is_valid_uuid(None))  # type: ignore

    def test_comprehensive_storage_and_shm_exclusion(self) -> None:
        cid = "11111111-2222-3333-4444-555555555555"
        conv_dir = self.ide_root / "conversations"
        conv_dir.mkdir(parents=True)
        brain_dir = self.ide_root / "brain" / cid
        brain_dir.mkdir(parents=True)

        # 1. db (100KB)
        db_file = conv_dir / f"{cid}.db"
        db_file.write_bytes(b"A" * 102400)

        # 2. wal (50KB)
        wal_file = conv_dir / f"{cid}.db-wal"
        wal_file.write_bytes(b"B" * 51200)

        # 3. shm (20KB) - MUST BE EXCLUDED
        shm_file = conv_dir / f"{cid}.db-shm"
        shm_file.write_bytes(b"S" * 20480)

        # 4. brain recursive files (30KB + 20KB)
        nested_dir = brain_dir / "nested"
        nested_dir.mkdir(parents=True)
        (brain_dir / "file1.bin").write_bytes(b"C" * 30720)
        (nested_dir / "file2.bin").write_bytes(b"D" * 20480)

        candidate = inspect_track_candidate(self.ide_root, cid, source="ide")
        self.assertIsNotNone(candidate)
        assert candidate is not None

        expected_durable_bytes = 102400 + 51200 + 30720 + 20480
        self.assertEqual(candidate.total_bytes, expected_durable_bytes)
        self.assertEqual(candidate.source, "ide")
        self.assertEqual(candidate.conversation_id, cid)
        self.assertEqual(candidate.db_path, db_file)
        self.assertEqual(candidate.brain_path, brain_dir)

    def test_min_bytes_filtering_and_show_all(self) -> None:
        cid_large = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        cid_small = "ffffffff-0000-1111-2222-333333333333"

        conv_dir = self.cli_root / "conversations"
        conv_dir.mkdir(parents=True)

        # Large track: 600 KB (> 512 KB threshold)
        (conv_dir / f"{cid_large}.db").write_bytes(b"X" * 614400)

        # Small track: 100 KB (< 512 KB threshold)
        (conv_dir / f"{cid_small}.db").write_bytes(b"Y" * 102400)

        # Default filter: only large track
        cands_default = scan_candidates(self.cli_root, source="cli")
        self.assertEqual(len(cands_default), 1)
        self.assertEqual(cands_default[0].conversation_id, cid_large)

        # show_all=True: both tracks
        cands_all = scan_candidates(self.cli_root, source="cli", show_all=True)
        self.assertEqual(len(cands_all), 2)
        found_ids = {c.conversation_id for c in cands_all}
        self.assertEqual(found_ids, {cid_large, cid_small})

    def test_mtime_ordering_and_max_durable_mtime(self) -> None:
        conv_dir = self.ide_root / "conversations"
        conv_dir.mkdir(parents=True)

        cid_older = "11111111-1111-1111-1111-111111111111"
        cid_newer = "22222222-2222-2222-2222-222222222222"

        db_older = conv_dir / f"{cid_older}.db"
        db_older.write_bytes(b"1" * (DEFAULT_TRACK_MIN_BYTES + 10))

        db_newer = conv_dir / f"{cid_newer}.db"
        db_newer.write_bytes(b"2" * (DEFAULT_TRACK_MIN_BYTES + 10))

        t_now = time.time()
        os.utime(db_older, (t_now - 100, t_now - 100))
        os.utime(db_newer, (t_now - 10, t_now - 10))

        cands = scan_candidates(self.ide_root, source="ide")
        self.assertEqual(len(cands), 2)
        # Ordered descending by activity mtime
        self.assertEqual(cands[0].conversation_id, cid_newer)
        self.assertEqual(cands[1].conversation_id, cid_older)

    def test_fault_tolerance_missing_or_corrupt_files(self) -> None:
        conv_dir = self.ide_root / "conversations"
        conv_dir.mkdir(parents=True)

        # Non-UUID file ignored
        (conv_dir / "random_notes.db").write_bytes(b"dummy")
        # Missing db file returns None
        candidate = inspect_track_candidate(
            self.ide_root, "99999999-9999-9999-9999-999999999999", source="ide"
        )
        self.assertIsNone(candidate)

        # Valid db but no wal, no brain is fine
        cid_only_db = "33333333-3333-3333-3333-333333333333"
        (conv_dir / f"{cid_only_db}.db").write_bytes(b"Z" * (DEFAULT_TRACK_MIN_BYTES + 1))
        cands = scan_candidates(self.ide_root, source="ide")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].conversation_id, cid_only_db)
        self.assertIsNone(cands[0].brain_path)

    def test_ide_track_binding_forbidden(self) -> None:
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        env_file = self.root / ".env"

        # Direct low-level call with raw source='ide' must raise ImportRequiredError
        with self.assertRaises(ImportRequiredError):
            bind_conversation_id_to_env(cid, env_file, source="ide")

        # Via TrackCandidate with source='ide', bind_track_candidate auto-clones to cli and binds
        src_db = self.root / "src_ide.db"
        src_db.write_bytes(b"mock db content")
        ide_candidate = TrackCandidate(
            source="ide",
            conversation_id=cid,
            db_path=src_db,
            brain_path=None,
            last_activity=time.time(),
            total_bytes=1000,
        )
        bound_env = bind_track_candidate(ide_candidate, env_file, cli_root=self.cli_root)
        self.assertTrue(bound_env.is_file())
        self.assertIn(f"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID={cid}", bound_env.read_text(encoding="utf-8"))

    def test_env_preserved_updating(self) -> None:
        cid = "a18d05b9-6838-4b9b-9b06-2295f59bdd69"
        env_file = self.root / ".env"
        initial_content = (
            "# Existing header\n"
            "FOO=bar\n"
            "# Track config\n"
            "POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID=old-id-value\n"
            "OTHER_VAR=keep_me\n"
        )
        env_file.write_text(initial_content, encoding="utf-8")

        bind_conversation_id_to_env(cid, env_file, source="cli")

        updated_text = env_file.read_text(encoding="utf-8")
        self.assertIn("# Existing header", updated_text)
        self.assertIn("FOO=bar", updated_text)
        self.assertIn("# Track config", updated_text)
        self.assertIn("OTHER_VAR=keep_me", updated_text)
        self.assertIn(f"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID={cid}", updated_text)
        self.assertNotIn("old-id-value", updated_text)
        self.assertEqual(os.environ.get("POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID"), cid)

    def test_detect_new_imported_cli_track_scenarios(self) -> None:
        conv_dir = self.cli_root / "conversations"
        conv_dir.mkdir(parents=True)

        cid_existing = "11111111-1111-1111-1111-111111111111"
        (conv_dir / f"{cid_existing}.db").write_bytes(b"initial")

        initial_snapshot = snapshot_cli_track_ids(self.cli_root)
        self.assertEqual(initial_snapshot, {cid_existing})

        # Scenario 1: 0 new tracks -> fail-loud
        with self.assertRaises(ImportDetectionError) as ctx:
            detect_new_imported_track(initial_snapshot, cli_root=self.cli_root)
        self.assertIn("No new CLI conversation track detected", str(ctx.exception))

        # Scenario 2: >1 new tracks -> fail-loud (refuse to guess latest)
        cid_new_1 = "22222222-2222-2222-2222-222222222222"
        cid_new_2 = "33333333-3333-3333-3333-333333333333"
        (conv_dir / f"{cid_new_1}.db").write_bytes(b"new1")
        (conv_dir / f"{cid_new_2}.db").write_bytes(b"new2")

        with self.assertRaises(ImportDetectionError) as ctx:
            detect_new_imported_track(initial_snapshot, cli_root=self.cli_root)
        self.assertIn("Ambiguous import", str(ctx.exception))

        # Scenario 3: Exactly 1 new track -> succeeds
        (conv_dir / f"{cid_new_2}.db").unlink()
        candidate = detect_new_imported_track(initial_snapshot, cli_root=self.cli_root)
        self.assertEqual(candidate.conversation_id, cid_new_1)
        self.assertEqual(candidate.source, "cli")

    def test_executor_command_generation_and_fail_loud(self) -> None:
        # 1. Unset conversation_id -> defaults to new session without --conversation
        with patch.dict(os.environ, {}, clear=True):
            executor = AntigravityExecutor(binary_path="agy")
            self.assertIsNone(executor.conversation_id)
            # Check execute command generation
            with patch("pocketfleet.executors.antigravity.run_safe_process_tree") as mock_run:
                mock_run.return_value = (0, "ok", "")
                with patch.object(executor, "is_available", return_value=True):
                    executor.execute("Hello")
                    cmd = mock_run.call_args[0][0]
                    self.assertNotIn("--conversation", cmd)
                    self.assertIn("--print=Hello", cmd)

        # 2. Configured valid conversation_id via constructor
        valid_cid = "bd9d4ded-c233-44a2-9cde-7247c5da3283"
        executor_bound = AntigravityExecutor(binary_path="agy", conversation_id=valid_cid)
        self.assertEqual(executor_bound.conversation_id, valid_cid)
        with patch("pocketfleet.executors.antigravity.run_safe_process_tree") as mock_run:
            mock_run.return_value = (0, "ok", "")
            with patch.object(executor_bound, "is_available", return_value=True):
                executor_bound.execute("Followup prompt")
                cmd = mock_run.call_args[0][0]
                self.assertIn("--conversation", cmd)
                idx = cmd.index("--conversation")
                self.assertEqual(cmd[idx + 1], valid_cid)

        # 3. Configured valid conversation_id via env var
        with patch.dict(os.environ, {"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID": valid_cid}):
            executor_env = AntigravityExecutor(binary_path="agy")
            self.assertEqual(executor_env.conversation_id, valid_cid)

        # 4. Configured invalid conversation_id -> fail-loud ValueError
        with self.assertRaises(ValueError):
            AntigravityExecutor(binary_path="agy", conversation_id="not-a-valid-uuid")

        with patch.dict(
            os.environ, {"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID": "malicious-track-id"}
        ):
            with self.assertRaises(ValueError):
                AntigravityExecutor(binary_path="agy")



class TestDialogueExtractionAndBacktracking(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.brain_path = Path(self.temp_dir.name)
        self.logs_dir = self.brain_path / ".system_generated" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.jsonl_file = self.logs_dir / "transcript.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_backtracking_over_large_tool_call_gap(self) -> None:
        import json
        from pocketfleet.antigravity_tracks import extract_last_dialogue

        with open(self.jsonl_file, "w", encoding="utf-8") as f:
            # 1. Real user prompt
            f.write(json.dumps({
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>启动修改。当你这版改完后，我还可以进一步收敛到更简单。</USER_REQUEST><ADDITIONAL_METADATA>meta</ADDITIONAL_METADATA>"
            }) + "\n")
            # 2. Assistant response
            f.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "报告指挥官：遵照您的指示已全面落地"
            }) + "\n")
            # 3. Simulate 150KB of tool calls and empty responses
            for _ in range(80):
                f.write(json.dumps({"type": "RUN_COMMAND", "content": "x" * 1500}) + "\n")
                f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": ""}) + "\n")
                f.write(json.dumps({"type": "SYSTEM_MESSAGE", "content": "<SYSTEM_MESSAGE>task status</SYSTEM_MESSAGE>"}) + "\n")

        u, m = extract_last_dialogue(self.brain_path)
        self.assertEqual(u, "启动修改。当你这版改完后，我还可以进一步收敛到更简单。")
        self.assertEqual(m, "报告指挥官：遵照您的指示已全面落地")

    def test_backtracking_skips_scaffolding_only_user_input(self) -> None:
        import json
        from pocketfleet.antigravity_tracks import extract_last_dialogue

        with open(self.jsonl_file, "w", encoding="utf-8") as f:
            # 1. Genuine human instruction
            f.write(json.dumps({
                "type": "USER_INPUT",
                "content": "帮我看看这个函数怎么优化"
            }) + "\n")
            f.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "好的，正在分析"
            }) + "\n")
            # 2. Injected scaffold-only pseudo user input
            f.write(json.dumps({
                "type": "USER_INPUT",
                "content": "<SYSTEM_MESSAGE>Background hook ping</SYSTEM_MESSAGE><ADDITIONAL_METADATA>test</ADDITIONAL_METADATA>"
            }) + "\n")
            f.write(json.dumps({
                "type": "RUN_COMMAND",
                "content": "ok"
            }) + "\n")

        u, m = extract_last_dialogue(self.brain_path)
        # Must skip the scaffold-only input and backtrack to genuine human instruction
        self.assertEqual(u, "帮我看看这个函数怎么优化")
        self.assertEqual(m, "好的，正在分析")

    def test_format_row_snippet_fallback(self) -> None:
        from pocketfleet.antigravity_tracks import AntigravityTrackController, TrackCandidate

        controller = AntigravityTrackController(workspace_cwd=Path("."))

        # 1. Human prompt present
        cand_user = TrackCandidate(
            source="cli",
            conversation_id="11111111-2222-3333-4444-555555555555",
            db_path=Path("/tmp/d.db"),
            brain_path=None,
            last_activity=time.time(),
            total_bytes=1000,
            last_user_prompt="查询系统状态",
            last_model_response="系统正常",
        )
        row = controller.format_row(cand_user)
        self.assertEqual(row["snippet"], "查询系统状态")

        # 2. No human prompt, but has assistant response -> Fallback to assistant snippet
        cand_ai_only = TrackCandidate(
            source="ide",
            conversation_id="22222222-3333-4444-5555-666666666666",
            db_path=Path("/tmp/d.db"),
            brain_path=None,
            last_activity=time.time(),
            total_bytes=1000,
            last_user_prompt="",
            last_model_response="自动巡检发现 0 个异常",
        )
        row_ai = controller.format_row(cand_ai_only)
        self.assertIn("🤖", row_ai["snippet"])
        self.assertIn("自动巡检发现 0 个异常", row_ai["snippet"])

        # 3. Completely blank track -> Clear blank indication
        cand_blank = TrackCandidate(
            source="cli",
            conversation_id="33333333-4444-5555-6666-777777777777",
            db_path=Path("/tmp/d.db"),
            brain_path=None,
            last_activity=0.0,
            total_bytes=0,
            last_user_prompt="",
            last_model_response="",
        )
        row_blank = controller.format_row(cand_blank)
        self.assertEqual(row_blank["snippet"], "（空白新对话轨）")


class TestAntigravityHandoverAndSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ide_root = self.root / "antigravity-ide"
        self.cli_root = self.root / "antigravity-cli"
        self.ide_root.mkdir(parents=True)
        self.cli_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_safe_snapshot_sqlite_real_db(self) -> None:
        import sqlite3

        src_db = self.root / "real_src.db"
        dst_db = self.root / "real_dst.db"

        # Create real sqlite database with WAL mode and tables
        conn = sqlite3.connect(src_db)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE test_tab (id INT PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO test_tab VALUES (1, 'antigravity_checkpoint');")
        conn.commit()
        conn.close()

        # Run safe snapshot
        safe_snapshot_sqlite(src_db, dst_db)
        self.assertTrue(dst_db.is_file())

        # Verify destination DB is valid and contains our data
        dst_conn = sqlite3.connect(dst_db)
        cursor = dst_conn.cursor()
        cursor.execute("SELECT val FROM test_tab WHERE id=1;")
        res = cursor.fetchone()
        dst_conn.close()
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "antigravity_checkpoint")

    def test_clone_ide_track_to_cli_incremental(self) -> None:
        cid = "44444444-5555-6666-7777-888888888888"
        src_conv = self.ide_root / "conversations"
        src_conv.mkdir(parents=True, exist_ok=True)
        src_db = src_conv / f"{cid}.db"
        src_db.write_bytes(b"dummy db")

        src_brain = self.ide_root / "brain" / cid
        src_brain.mkdir(parents=True, exist_ok=True)
        (src_brain / "artifact.md").write_text("# Test Artifact", encoding="utf-8")

        # Destination already has an existing file
        dst_brain = self.cli_root / "brain" / cid
        dst_brain.mkdir(parents=True, exist_ok=True)
        existing_local_file = dst_brain / "local_custom.txt"
        existing_local_file.write_text("keep_me", encoding="utf-8")

        # Destination has stale wal
        dst_conv = self.cli_root / "conversations"
        dst_conv.mkdir(parents=True, exist_ok=True)
        stale_wal = dst_conv / f"{cid}.db-wal"
        stale_wal.write_bytes(b"stale")

        cand = TrackCandidate(
            source="ide",
            conversation_id=cid,
            db_path=src_db,
            brain_path=src_brain,
            last_activity=time.time(),
            total_bytes=1000,
        )

        cloned = clone_ide_track_to_cli(cand, cli_root=self.cli_root)
        self.assertEqual(cloned.source, "cli")
        self.assertFalse(stale_wal.exists(), "Stale WAL should be unlinked after backup")
        self.assertTrue((dst_brain / "artifact.md").is_file())
        self.assertTrue(existing_local_file.is_file(), "Non-destructive copy must preserve existing local files")

    def test_handover_dossier_and_controller(self) -> None:
        cid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        ws = self.root / "my_workspace"
        ws.mkdir(parents=True, exist_ok=True)

        dossier_path = write_handover_dossier(
            workspace_cwd=ws,
            conversation_id=cid,
            cli_root=self.cli_root,
            note="Test note for handover",
        )
        self.assertTrue(dossier_path.is_file())
        content = dossier_path.read_text(encoding="utf-8")
        self.assertIn("PocketFleet 外勤施工交接公文", content)
        self.assertIn(cid, content)
        self.assertIn("Test note for handover", content)
        self.assertIn("view_file path:", content)

        # Test controller method with bound ID
        env_file = ws / ".env"
        env_file.write_text(f"POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID={cid}\n", encoding="utf-8")
        controller = AntigravityTrackController(workspace_cwd=ws, env_path=env_file)
        res_path, notified = controller.create_handover(note="Run from controller", notify_ide=False)
        self.assertTrue(res_path.is_file())
        self.assertFalse(notified)

    def test_notify_ide_handover_soft_fail(self) -> None:
        # When agentapi is not in test environment or mock fails, returns False without crashing
        res = notify_ide_handover("test-cid", self.root / ".fleet_handover.md")
        self.assertIsInstance(res, bool)


if __name__ == "__main__":
    unittest.main()
