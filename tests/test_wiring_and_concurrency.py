"""E2E Verification for Unified Wiring, Concurrency, and Status responsiveness.

Strict Compliance with CTO Final Orders:
1. Console track binding: agy command uses authoritative bound conversation UUID.
2. Web & TG concurrent dispatch: strictly serialized on the same seat; second is queued; /status never hangs.
3. Daemon restart drains legacy queued tasks automatically.
4. Finished results delivered once and only once via event_id deduplication.
5. Stop All stops worker, cancels in-flight processes, and safely releases lease.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.core import InboundMessage, WorkerType
from pocketfleet.executors.antigravity import AntigravityExecutor
from pocketfleet.executors.base import BaseExecutor
from pocketfleet.loop import DispatchLoop
from pocketfleet.session_hub import SessionHub
from pocketfleet.state import StateStore
from pocketfleet.transport.base import BaseTransport


class MockTransport(BaseTransport):
    def __init__(self) -> None:
        self.inbound_queue: list[InboundMessage] = []
        self.sent_messages: list[any] = []

    def poll_messages(self, timeout_sec: int = 10):
        res = list(self.inbound_queue)
        self.inbound_queue.clear()
        return res

    def send_message(self, message):
        self.sent_messages.append(message)
        return True


class ControlledExecutor(BaseExecutor):
    """Executor with simulated controllable delays to test concurrency."""
    name: str = "antigravity"

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.call_history: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300, cancel_event=None):
        self.call_history.append(prompt)
        start = time.time()
        while time.time() - start < self.delay:
            if cancel_event and cancel_event.is_set():
                return -1, "", "Task cancelled"
            time.sleep(0.02)
        return 0, f"Executed: {prompt}", ""


class TestWiringAndConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_wiring.sqlite3"
        self.state_store = StateStore(self.db_path)
        self.session_hub = SessionHub(self.state_store)
        self.transport = MockTransport()
        self.test_uuid = "83472093-6c8c-4977-87cf-cb91104e8818"

        # Register lead authoritative session
        self.session_hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=self.test_uuid,
            workspace=self.tmp_dir.name,
            role="adjudicator",
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_agy_command_uses_bound_uuid(self):
        """1. Verify that AntigravityExecutor constructs command with --conversation <bound UUID>."""
        executor = AntigravityExecutor(conversation_id=self.test_uuid)
        self.assertEqual(executor.conversation_id, self.test_uuid)

        with mock.patch("pocketfleet.executors.antigravity.run_safe_process_tree") as mock_run:
            mock_run.return_value = (0, "ok", "")
            executor.execute(prompt="hello", cwd=self.tmp_dir.name)

            self.assertTrue(mock_run.called)
            cmd_called = mock_run.call_args[0][0]
            self.assertIn("--conversation", cmd_called)
            idx = cmd_called.index("--conversation")
            self.assertEqual(cmd_called[idx + 1], self.test_uuid)

    def test_web_and_tg_concurrent_dispatch_serial_and_status(self):
        """2. Web and TG dispatch concurrently: same seat serialized, second queued, /status instant."""
        controlled_exec = ControlledExecutor(delay=0.3)
        worker = self.session_hub.create_worker(seat_id="lead", executor_override=controlled_exec)
        worker.start()

        loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.ANTIGRAVITY,
            state_store=self.state_store,
            session_hub=self.session_hub,
            allowed_chat_ids={888},
        )

        try:
            # TG message 1
            msg1 = InboundMessage(
                message_id=101,
                chat_id=888,
                sender_id=888,
                sender_name="Commander",
                text="TG Task 1: Initialize Fleet",
                is_bot=False,
            )
            loop.handle_message(msg1)

            # Web dispatch 2 concurrently
            web_evt = self.session_hub.enqueue_task(
                seat_id="lead",
                prompt="Web Task 2: Build Subsystem",
                source="web",
                idempotency_key="web_002",
            )

            # Check that Task 2 is queued while Task 1 is being processed
            self.assertEqual(web_evt.status, "queued")

            # Immediately test /status command - must NOT hang and must show busy
            status_msg = InboundMessage(
                message_id=102,
                chat_id=888,
                sender_id=888,
                sender_name="Commander",
                text="/status",
                is_bot=False,
            )
            t0 = time.time()
            loop.handle_message(status_msg)
            t_status = time.time() - t0
            self.assertLess(t_status, 0.5, "Status response must be instant (< 500ms)")

            status_replies = [m.text for m in self.transport.sent_messages if "PocketFleet Status:" in m.text]
            self.assertTrue(status_replies)
            self.assertIn("BUSY", status_replies[-1])
            self.assertIn("83472093...8818", status_replies[-1])

            # Wait for both tasks to complete in order
            deadline = time.time() + 6.0
            while time.time() < deadline:
                e1 = self.session_hub.get_event_by_idempotency_key("tg:888:101")
                e2 = self.session_hub.get_event_by_idempotency_key("web_002")
                if e1 and e1.status == "completed" and e2 and e2.status == "completed":
                    break
                time.sleep(0.05)

            e1 = self.session_hub.get_event_by_idempotency_key("tg:888:101")
            e2 = self.session_hub.get_event_by_idempotency_key("web_002")
            self.assertEqual(e1.status, "completed")
            self.assertEqual(e2.status, "completed")

            # Execution was strictly serial
            self.assertEqual(controlled_exec.call_history, [
                "TG Task 1: Initialize Fleet",
                "Web Task 2: Build Subsystem",
            ])

            # Test delivery deduplication: only delivered once
            loop.step()
            self.assertTrue(self.state_store.is_event_delivered(e1.event_id, "telegram"))
            self.assertFalse(self.state_store.try_record_event_delivery(e1.event_id, "telegram"))

        finally:
            worker.stop()
            loop.stop()

    def test_daemon_restart_drains_legacy_queued(self):
        """3. Daemon restart drains legacy queued tasks automatically."""
        # Enqueue task when worker is offline
        evt = self.session_hub.enqueue_task(
            seat_id="lead",
            prompt="Legacy queued task before restart",
            source="telegram",
            idempotency_key="legacy_restart_01",
        )
        self.assertEqual(evt.status, "queued")

        # Start fresh worker simulating daemon restart
        controlled_exec = ControlledExecutor(delay=0.05)
        worker = self.session_hub.create_worker(seat_id="lead", executor_override=controlled_exec)
        worker.start()

        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                res = self.session_hub.get_event(evt.event_id)
                if res and res.status == "completed":
                    break
                time.sleep(0.05)

            res = self.session_hub.get_event(evt.event_id)
            self.assertEqual(res.status, "completed")
            self.assertIn("Legacy queued task before restart", controlled_exec.call_history)
        finally:
            worker.stop()

    def test_stop_all_stops_worker_cancels_and_releases_lease(self):
        """4. Stop All stops worker, cancels in-flight task, and safely releases lease."""
        slow_exec = ControlledExecutor(delay=5.0)
        worker = self.session_hub.create_worker(seat_id="lead", executor_override=slow_exec)
        worker.start()

        evt = self.session_hub.enqueue_task(
            seat_id="lead",
            prompt="Slow task to be cancelled",
            source="web",
            idempotency_key="cancel_test_01",
        )

        # Wait until task is running
        deadline = time.time() + 2.0
        while time.time() < deadline:
            rec = self.session_hub.get_event(evt.event_id)
            if rec and rec.status == "running":
                break
            time.sleep(0.02)

        # Verify busy lease
        sess_before = self.session_hub.get_session("lead")
        self.assertEqual(sess_before.status, "busy")

        # Stop worker with cancel_active=True
        worker.stop(cancel_active=True, timeout=2.0)

        # Event must be interrupted/failed and session must be released
        rec_after = self.session_hub.get_event(evt.event_id)
        self.assertEqual(rec_after.status, "failed")
        self.assertIn("cancelled", (rec_after.error or "").lower())

        sess_after = self.session_hub.get_session("lead")
        self.assertIn(sess_after.status, ("idle", "interrupted"))


if __name__ == "__main__":
    unittest.main()
