"""Tests for Decoupled Echo-Proof DAG Loop and P0 Hardening"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.core import InboundMessage, TaskStatus, WorkerType
from pocketfleet.loop import DispatchLoop
from pocketfleet.state import StateStore
from pocketfleet.transport.base import BaseTransport


class MockTransport(BaseTransport):
    def __init__(self) -> None:
        self.inbound_queue: list[InboundMessage] = []
        self.sent_messages: list[any] = []
        self.should_fail: bool = False

    def poll_messages(self, timeout_sec: int = 10):
        res = list(self.inbound_queue)
        self.inbound_queue.clear()
        return res

    def send_message(self, message):
        if self.should_fail:
            return False
        self.sent_messages.append(message)
        return True


class TestDispatchLoop(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_state.sqlite3"
        self.state_store = StateStore(self.db_path)
        self.transport = MockTransport()
        self.loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.CLAUDE_CODE,
            state_store=self.state_store,
        )

    def tearDown(self):
        self.loop.stop()
        self.tmp_dir.cleanup()

    def test_echo_proof_gating_ignores_bot(self):
        bot_msg = InboundMessage(
            message_id=901,
            chat_id=111,
            sender_id=999,
            sender_name="SpamBot",
            text="/fix do something",
            is_bot=True,
        )

        task = self.loop.handle_message(bot_msg)
        self.assertIsNone(task)
        self.assertEqual(len(self.transport.sent_messages), 0)

    def test_instant_status_response_when_idle(self):
        status_msg = InboundMessage(
            message_id=902,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/status",
            is_bot=False,
        )

        task = self.loop.handle_message(status_msg)
        self.assertIsNone(task)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertIn("PocketFleet Status: IDLE", self.transport.sent_messages[0].text)

    def test_instant_status_response_when_busy(self):
        # Simulate currently running task
        self.loop.current_running_task = {
            "task_id": "task-test-1",
            "prompt": "fix issue #42 JWT error",
            "worker": "claude_code",
            "start_time": time.time() - 15,
        }

        status_msg = InboundMessage(
            message_id=903,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/status",
            is_bot=False,
        )

        self.loop.handle_message(status_msg)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertIn("PocketFleet Status: BUSY", self.transport.sent_messages[0].text)
        self.assertIn("fix issue #42", self.transport.sent_messages[0].text)

    def test_deduplication_prevents_duplicate_run(self):
        # Mark message 904 as already finished in SQLite
        self.state_store.record_message_start(904, 111, "some prompt", "claude_code")
        self.state_store.record_message_finish(904, "COMPLETED", 0)

        repeat_msg = InboundMessage(
            message_id=904,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/claude fix repeat bug",
            is_bot=False,
        )

        task = self.loop.handle_message(repeat_msg)
        self.assertIsNone(task)
        self.assertEqual(self.loop.work_queue.qsize(), 0)

    def test_outbox_retry_on_network_failure(self):
        self.transport.should_fail = True

        # Send message when transport is down
        self.loop._send_immediate_or_outbox(
            chat_id=111,
            text="Crucial Final Report",
            reply_to_message_id=905,
        )

        # Immediate send failed -> Enqueued into Outbox
        pending = self.state_store.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["text"], "Crucial Final Report")

        # Network recovers
        self.transport.should_fail = False
        sent = self.loop.flush_outbox()
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertEqual(self.transport.sent_messages[0].text, "Crucial Final Report")

        # Second flush has 0 pending
        self.assertEqual(self.loop.flush_outbox(), 0)

    def test_group_chat_authorized_by_sender(self):
        # Security whitelist: sender is authorized commander even if group chat ID is negative
        self.loop.allowed_chat_ids = {6801810539}
        self.loop.default_worker = WorkerType.SIMULATION
        group_msg = InboundMessage(
            message_id=906,
            chat_id=-100123456789,
            sender_id=6801810539,
            sender_name="Commander",
            text="构建自动化支付结算网关",
            is_bot=False,
        )

        task = self.loop.handle_message(group_msg)
        self.assertIsNotNone(task)
        self.assertEqual(self.loop.work_queue.qsize(), 1)

    def test_warroom_role_binding_uses_real_executors(self):
        from pocketfleet.core import RoleAssignment
        self.loop.bots_config = {
            "antigravity": {"name": "Google Antigravity agent"},
            "codex": {"name": "OpenAI Codex worker"},
        }
        self.loop.set_role_assignment(RoleAssignment(lead="antigravity", builder="codex"))

        lead, builder, lead_name, builder_name = self.loop.get_role_bindings()
        self.assertIs(lead, self.loop.executors[WorkerType.ANTIGRAVITY])
        self.assertIs(builder, self.loop.executors[WorkerType.CODEX])
        self.assertEqual(lead_name, "Google Antigravity agent")
        self.assertEqual(builder_name, "OpenAI Codex worker")

        # Swap roles
        self.loop.set_role_assignment(RoleAssignment(lead="codex", builder="antigravity"))
        lead2, builder2, _, _ = self.loop.get_role_bindings()
        self.assertIs(lead2, self.loop.executors[WorkerType.CODEX])
        self.assertIs(builder2, self.loop.executors[WorkerType.ANTIGRAVITY])

    def test_unavailable_worker_never_falls_back_to_simulation(self):
        with mock.patch.object(self.loop.executors[WorkerType.CODEX], "is_available", return_value=False):
            selected = self.loop.select_worker(WorkerType.CODEX)
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
