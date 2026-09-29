"""Tests for Persistent StateStore and Cockpit Telemetry"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pocketfleet.cockpit import CockpitTelemetry
from pocketfleet.state import StateStore


class TestStateAndCockpit(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_state.sqlite3"
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_watermark_persistence(self):
        self.assertIsNone(self.store.get_watermark())
        self.store.set_watermark(99901)
        self.assertEqual(self.store.get_watermark(), 99901)

        # Update again
        self.store.set_watermark(99905)
        self.assertEqual(self.store.get_watermark(), 99905)

    def test_message_lifecycle_and_deduplication(self):
        msg_id = 7788
        self.assertFalse(self.store.is_message_processed(msg_id))

        self.store.record_message_start(msg_id, 1001, "Fix memory leak", "claude_code")
        self.assertTrue(self.store.is_message_processed(msg_id))

        self.store.record_message_finish(msg_id, "COMPLETED", 0)
        recent = self.store.get_recent_tasks(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["message_id"], msg_id)
        self.assertEqual(recent[0]["status"], "COMPLETED")
        self.assertEqual(recent[0]["exit_code"], 0)

    def test_outbox_queue_and_retries(self):
        outbox_id = self.store.enqueue_outbox(
            chat_id=1001,
            text="Important report",
            parse_mode="Markdown",
            reply_to_message_id=55,
        )
        self.assertIsInstance(outbox_id, int)

        pending = self.store.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], outbox_id)
        self.assertEqual(pending[0]["retry_count"], 0)

        # Record failed attempt
        self.store.mark_outbox_failed_attempt(outbox_id)
        pending2 = self.store.get_pending_outbox()
        self.assertEqual(pending2[0]["retry_count"], 1)

        # Mark sent
        self.store.mark_outbox_sent(outbox_id)
        pending3 = self.store.get_pending_outbox()
        self.assertEqual(len(pending3), 0)

    def test_cockpit_telemetry(self):
        telem = CockpitTelemetry()
        telem.workspace = "/home/dev/app"
        telem.bot_username = "@MyFleetBot"
        telem.allowed_chat_ids = [1001, 2002]
        telem.available_workers = ["claude_code", "aider"]

        telem.record_task(
            prompt="Refactor database",
            worker="claude_code",
            status="COMPLETED",
            duration_sec=3.5,
            preview="Applied migrations",
        )

        d = telem.to_dict()
        self.assertEqual(d["workspace"], "/home/dev/app")
        self.assertEqual(d["bot_username"], "@MyFleetBot")
        self.assertTrue(d["whitelist_active"])
        self.assertEqual(len(d["tasks"]), 1)
        self.assertEqual(d["tasks"][0]["worker"], "claude_code")


if __name__ == "__main__":
    unittest.main()
