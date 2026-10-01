"""PF-03R6 Final Acceptance Test Suite.

Verifies all six counter-examples (A to F) specified by CTO Red Team:
A. Startup fails when no valid track is bound, with ZERO thread leaks.
B. Enqueue failure returns immediately and NEVER falls back to legacy work_queue.
C. Two different chats using the identical message_id execute and report independently.
D. Restart immediately after enqueue still replies to original chat and original message.
E. Older undelivered Telegram events are reliably delivered even after >30 newer events are enqueued.
F. Empty authorization whitelist strictly rejects daemon startup (fail-closed, not allow-all).
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.control_panel import FleetManager
from pocketfleet.core import InboundMessage, OutboundMessage, Task, TaskStatus, WorkerType
from pocketfleet.loop import DispatchLoop
from pocketfleet.session_hub import SessionHub
from pocketfleet.state import StateStore
from pocketfleet.transport.base import BaseTransport


class MockTransport(BaseTransport):
    def __init__(self):
        self.sent_messages: list[OutboundMessage] = []
        self.pending_inbound: list[InboundMessage] = []

    def poll_messages(self, timeout_sec: int = 5) -> list[InboundMessage]:
        msgs = list(self.pending_inbound)
        self.pending_inbound.clear()
        return msgs

    def send_message(self, message: OutboundMessage) -> bool:
        self.sent_messages.append(message)
        return True


class TestPF03R6Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "state.sqlite3"
        self.state_store = StateStore(self.db_path)
        self.session_hub = SessionHub(self.state_store)
        self.transport = MockTransport()

    def tearDown(self):
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_counter_example_a_unbound_track_rejects_and_no_thread_leak(self):
        """A. 无选轨启动失败且无线程残留。"""
        threads_before = threading.enumerate()
        mgr = FleetManager(log_cb=lambda msg: None)

        with patch("pocketfleet.control_panel.REPO_ROOT", Path(self.tmp_dir)):
            with patch.object(mgr, "_load_credentials", return_value=("MOCK_TOKEN", {12345}, "fleet_triad")):
                with patch("pocketfleet.antigravity_tracks.AntigravityTrackController.get_current_bound_id", return_value=None):
                    started = mgr.start_daemon()
                    self.assertFalse(started, "Daemon must reject startup when no CLI track is bound.")
                    self.assertIsNone(mgr.lead_worker, "No worker may be created or left running.")
                    self.assertIsNone(mgr.dispatch_loop, "DispatchLoop must not be running.")

        threads_after = threading.enumerate()
        # Verify no worker or daemon threads leaked
        leaked_workers = [t for t in threads_after if t not in threads_before and "Worker" in t.name]
        self.assertEqual(len(leaked_workers), 0, f"Thread leak detected: {leaked_workers}")

    def test_counter_example_b_enqueue_failure_never_enters_legacy_queue(self):
        """B. enqueue 异常不会进入旧队列，必须直接返回。"""
        loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=self.state_store,
            session_hub=self.session_hub,
            allowed_chat_ids={1001},
        )

        msg = InboundMessage(
            message_id=501,
            chat_id=1001,
            sender_id=1001,
            sender_name="Alice",
            text="Simulate enqueue failure",
            is_bot=False,
        )

        # Mock enqueue_task to raise an exception
        with patch.object(self.session_hub, "enqueue_task", side_effect=RuntimeError("Database disk full")):
            task = loop.handle_message(msg)
            self.assertIsNotNone(task)
            self.assertEqual(task.status, TaskStatus.FAILED)
            # CRITICAL: Legacy work_queue must remain empty!
            self.assertEqual(loop.work_queue.qsize(), 0, "Failed enqueue MUST NOT fall back to work_queue!")

        # Verify failure message sent to user
        self.assertTrue(any("Task Enqueue Failed" in m.text for m in self.transport.sent_messages))

    def test_counter_example_c_two_chats_same_message_id_independent(self):
        """C. 两个 chat 使用同一 message_id 均可独立执行回报。"""
        self.session_hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id="c0000000-0000-4000-8000-000000000001",
            workspace=self.tmp_dir,
            role="adjudicator",
        )

        loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=self.state_store,
            session_hub=self.session_hub,
            allowed_chat_ids={1001, 1002},
        )

        # Chat 1001 with message_id 777
        msg1 = InboundMessage(
            message_id=777,
            chat_id=1001,
            sender_id=1001,
            sender_name="User1",
            text="Task for Chat 1",
            is_bot=False,
        )
        task1 = loop.handle_message(msg1)
        self.assertIsNotNone(task1)

        # Chat 1002 with SAME message_id 777
        msg2 = InboundMessage(
            message_id=777,
            chat_id=1002,
            sender_id=1002,
            sender_name="User2",
            text="Task for Chat 2",
            is_bot=False,
        )
        task2 = loop.handle_message(msg2)
        self.assertIsNotNone(task2, "Message with same ID in different chat must NOT be dropped by deduplication!")

        # Both events must exist in ledger independently with distinct idempotency keys
        ev1 = self.session_hub.get_event_by_idempotency_key("tg:1001:777")
        ev2 = self.session_hub.get_event_by_idempotency_key("tg:1002:777")
        self.assertIsNotNone(ev1)
        self.assertIsNotNone(ev2)
        self.assertNotEqual(ev1.event_id, ev2.event_id)
        self.assertEqual(ev1.reply_chat_id, 1001)
        self.assertEqual(ev2.reply_chat_id, 1002)

    def test_counter_example_d_restart_after_enqueue_replies_to_original_chat_and_message(self):
        """D. 入队后立即模拟重启，仍回复原 chat/原消息。"""
        self.session_hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id="c0000000-0000-4000-8000-000000000002",
            workspace=self.tmp_dir,
            role="adjudicator",
        )

        loop1 = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=self.state_store,
            session_hub=self.session_hub,
            allowed_chat_ids={5555},
        )

        msg = InboundMessage(
            message_id=999,
            chat_id=5555,
            sender_id=5555,
            sender_name="Commander",
            text="Long building mission",
            is_bot=False,
        )
        loop1.handle_message(msg)

        # Simulate process restart: destroy loop1, instantiate loop2 with fresh in-memory state
        del loop1
        transport2 = MockTransport()
        state_store2 = StateStore(self.db_path)
        session_hub2 = SessionHub(state_store2)

        # Process the task in background via worker under new session hub
        evt = session_hub2.process_next_task(seat_id="lead")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.status, "completed")

        loop2 = DispatchLoop(
            transport=transport2,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=state_store2,
            session_hub=session_hub2,
            allowed_chat_ids={5555},
        )

        # Trigger reporting tick on new loop instance
        loop2._poll_and_report_events()

        # Verify reply was sent to exact original chat_id and original reply_to_message_id
        reported = [m for m in transport2.sent_messages if "Task Completed" in m.text]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0].chat_id, 5555)
        self.assertEqual(reported[0].reply_to_message_id, 999)

    def test_counter_example_e_older_events_delivered_even_after_many_newer_events(self):
        """E. 建立超过30个新事件后，旧未回报事件仍能送达。"""
        self.session_hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id="c0000000-0000-4000-8000-000000000003",
            workspace=self.tmp_dir,
            role="adjudicator",
        )

        # 1. Enqueue early Telegram event
        early_event = self.session_hub.enqueue_task(
            seat_id="lead",
            prompt="Early critical task",
            source="telegram",
            idempotency_key="tg:3333:101",
            reply_chat_id=3333,
            reply_message_id=101,
        )
        # Complete early event
        self.session_hub.process_next_task(seat_id="lead")

        # 2. Enqueue 35 newer Web events (saturating recent 30 query)
        for i in range(35):
            self.session_hub.enqueue_task(
                seat_id="lead",
                prompt=f"Web flood task #{i}",
                source="web",
                idempotency_key=f"web_flood_{i}",
            )

        loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=self.state_store,
            session_hub=self.session_hub,
            allowed_chat_ids={3333},
        )

        # Poll and report events
        loop._poll_and_report_events()

        # The early Telegram task must be found and reported!
        delivered = [m for m in self.transport.sent_messages if "Task Completed" in m.text]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].chat_id, 3333)
        self.assertEqual(delivered[0].reply_to_message_id, 101)

    def test_counter_example_f_empty_whitelist_rejects_startup(self):
        """F. 空授权列表拒绝启动。"""
        mgr = FleetManager(log_cb=lambda msg: None)

        with patch("pocketfleet.control_panel.REPO_ROOT", Path(self.tmp_dir)):
            # Empty whitelist returned
            with patch.object(mgr, "_load_credentials", return_value=("MOCK_TOKEN", None, "fleet_triad")):
                with patch("pocketfleet.antigravity_tracks.AntigravityTrackController.get_current_bound_id", return_value="c0000000-0000-4000-8000-000000000004"):
                    started = mgr.start_daemon()
                    self.assertFalse(started, "Daemon must fail closed when allowed_ids is empty!")
                    self.assertIsNone(mgr.dispatch_loop)
                    self.assertIsNone(mgr.lead_worker)


if __name__ == "__main__":
    unittest.main()
