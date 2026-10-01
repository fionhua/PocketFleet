"""Unit tests for PF-03R3: Fencing token defense, resident SessionWorker, and process tree termination.

Strictly covers all 5 acceptance criteria demanded by CTO Red Team in PF-03R3:
1. Strict Fencing: New worker's completed result CANNOT be overwritten by a stale/lost worker.
2. Resident SessionWorker: Enqueue 3 tasks, start worker once, all 3 drain automatically in FIFO (max_active=1).
3. Hard Process Tree Kill: Real blocking child process (e.g. sleep 5) is killed immediately on heartbeat failure.
4. Legacy Ghost Task Cleanup: Running events with NULL owner_token are safely migrated to failed/interrupted.
5. Preserves all existing tests: UUID validation, IDE path defense, payload fingerprinting, failed retry.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.executors.base import BaseExecutor, run_safe_process_tree
from pocketfleet.session_hub import (
    FleetEvent,
    IDEAccessForbiddenError,
    IdempotentConflictError,
    IdempotentPayloadMismatchError,
    LeaseLostError,
    SessionBusyError,
    SessionHub,
    SessionNotFoundError,
    SessionRecord,
    SessionWorker,
)
from pocketfleet.state import StateStore


class MockExecutor(BaseExecutor):
    name: str = "mock"

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "OK response",
        stderr: str = "",
        delay_sec: float = 0.0,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.delay_sec = delay_sec
        self.call_count = 0
        self.call_history: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_sec: int = 300,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, str, str]:
        self.call_count += 1
        self.call_history.append(prompt)
        start_t = time.time()
        while time.time() - start_t < self.delay_sec:
            if cancel_event is not None and cancel_event.is_set():
                return -2, "", "Mock cancelled by cancel_event"
            time.sleep(0.01)
        return self.exit_code, self.stdout, self.stderr


class BlockingChildProcessExecutor(BaseExecutor):
    name: str = "blocking_child_proc"

    def __init__(self, sleep_sec: float = 5.0) -> None:
        self.sleep_sec = sleep_sec

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout_sec: int = 300,
        cancel_event: Any | None = None,
    ) -> tuple[int, str, str]:
        cmd = [sys.executable, "-c", f"import time; time.sleep({self.sleep_sec})"]
        return run_safe_process_tree(cmd, cwd=cwd, timeout_sec=timeout_sec, cancel_event=cancel_event)


class TestSessionHubR3(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.db_path = self.workspace / "state.sqlite3"
        self.store = StateStore(self.db_path)
        self.hub = SessionHub(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_session_registration_and_retrieval(self) -> None:
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        src_ide = "aaaaaaaa-1111-2222-3333-444444444444"

        record = self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead Architect",
            source_ide_conversation_id=src_ide,
        )

        self.assertEqual(record.seat_id, "lead")
        self.assertEqual(record.engine, "antigravity")
        self.assertEqual(record.conversation_id, cid)
        self.assertEqual(record.workspace, str(self.workspace.resolve()))
        self.assertEqual(record.role, "Lead Architect")
        self.assertEqual(record.source_ide_conversation_id, src_ide)
        self.assertEqual(record.status, "idle")
        self.assertIsNone(record.owner_pid)
        self.assertGreater(record.last_activity, 0)

        fetched = self.hub.get_session("lead")
        self.assertEqual(fetched.conversation_id, cid)

        with self.assertRaises(SessionNotFoundError):
            self.hub.get_session("non_existent")

    def test_uuid_validation_and_ide_runtime_access_forbidden(self) -> None:
        with self.assertRaises(ValueError):
            self.hub.register_or_update_session(
                seat_id="lead",
                engine="antigravity",
                conversation_id="not-a-uuid",
                workspace=self.workspace,
                role="Lead",
            )

        forbidden_ws = self.workspace / ".gemini" / "antigravity-ide" / "conversations"
        forbidden_ws.mkdir(parents=True)
        with self.assertRaises(IDEAccessForbiddenError):
            self.hub.register_or_update_session(
                seat_id="lead",
                engine="antigravity",
                conversation_id="56b7c33a-d1df-4b11-9363-78bb32e32c2a",
                workspace=forbidden_ws,
                role="Lead",
            )

    # --- CTO Red Team R3: Acceptance 1 ---
    def test_stale_worker_cannot_overwrite_new_worker_result(self) -> None:
        """Acceptance 1: Fencing Token Defense.
        
        When old worker loses lease, new worker takes over and writes completed.
        Old worker must NOT overwrite completed to failed or wipe response!
        """
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        # 1. Enqueue task
        evt = self.hub.enqueue_task(
            seat_id="lead",
            prompt="Fencing test task",
            source="telegram",
            idempotency_key="key_fencing_1",
        )

        # 2. Simulate worker 1 acquiring lease under token_old
        token_old = f"{os.getpid()}:thread_old:aaa"
        token_new = f"{os.getpid()}:thread_new:bbb"

        acquired_old = self.store.acquire_session_lease("lead", owner_token=token_old, owner_pid=os.getpid(), lease_duration=60.0)
        self.assertTrue(acquired_old)
        self.store.record_event_start(evt.event_id, owner_token=token_old, owner_pid=os.getpid())

        # 3. Simulate lease stealing / expiration: worker 2 acquires lease under token_new
        # Force expiration of token_old in DB
        with self.store._get_connection() as conn:
            conn.execute("UPDATE sessions SET lease_expires_at = 0.0 WHERE seat_id = 'lead'")

        acquired_new = self.store.acquire_session_lease("lead", owner_token=token_new, owner_pid=os.getpid(), lease_duration=60.0)
        self.assertTrue(acquired_new)

        # Worker 2 takes over and starts execution under token_new
        self.store.record_event_start(evt.event_id, owner_token=token_new, owner_pid=os.getpid())
        # Worker 2 successfully completes the task and writes new response
        written_new = self.store.record_event_finish(
            event_id=evt.event_id,
            response="NEW_WORKER_VALID_RESULT",
            exit_code=0,
            owner_token=token_new,
            owner_pid=os.getpid(),
        )
        self.assertTrue(written_new)

        # Verify task is completed by new worker
        final_evt1 = self.hub.get_event(evt.event_id)
        self.assertEqual(final_evt1.status, "completed")
        self.assertEqual(final_evt1.response, "NEW_WORKER_VALID_RESULT")

        # 4. Now old worker (token_old) wakes up and attempts to write finish (failed/aborted)
        # Fencing check MUST reject it!
        written_old = self.store.record_event_finish(
            event_id=evt.event_id,
            response=None,
            exit_code=137,
            error="Old worker late failure",
            owner_token=token_old,
            owner_pid=os.getpid(),
        )
        self.assertFalse(written_old, "CRITICAL: Old worker broke fencing token and overwrote finish!")

        # Verify new worker's result was 100% PRESERVED!
        final_evt2 = self.hub.get_event(evt.event_id)
        self.assertEqual(final_evt2.status, "completed")
        self.assertEqual(final_evt2.response, "NEW_WORKER_VALID_RESULT")

    # --- CTO Red Team R3: Acceptance 2 ---
    def test_resident_worker_automatic_drain_fifo(self) -> None:
        """Acceptance 2: Enqueue 3 tasks, start worker once, all 3 drain automatically in FIFO."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        mock_exec = MockExecutor(exit_code=0, stdout="Processed", delay_sec=0.03)

        # 1. Enqueue 3 tasks non-blockingly
        e1 = self.hub.enqueue_task("lead", "Task 1", idempotency_key="fifo_1")
        e2 = self.hub.enqueue_task("lead", "Task 2", idempotency_key="fifo_2")
        e3 = self.hub.enqueue_task("lead", "Task 3", idempotency_key="fifo_3")

        self.assertEqual(e1.status, "queued")
        self.assertEqual(e2.status, "queued")
        self.assertEqual(e3.status, "queued")

        # 2. Start worker ONCE
        worker = self.hub.create_worker("lead", poll_interval=0.02, executor_override=mock_exec)
        worker.start()

        # 3. Wait for worker to automatically drain all 3 tasks
        start_t = time.time()
        while time.time() - start_t < 5.0:
            events = [
                self.hub.get_event(e1.event_id),
                self.hub.get_event(e2.event_id),
                self.hub.get_event(e3.event_id),
            ]
            if all(ev and ev.status == "completed" for ev in events):
                break
            time.sleep(0.05)

        worker.stop()

        # All 3 completed automatically!
        ev1 = self.hub.get_event(e1.event_id)
        ev2 = self.hub.get_event(e2.event_id)
        ev3 = self.hub.get_event(e3.event_id)
        self.assertEqual(ev1.status, "completed")
        self.assertEqual(ev2.status, "completed")
        self.assertEqual(ev3.status, "completed")

        # Strict FIFO order verified!
        self.assertEqual(mock_exec.call_history, ["Task 1", "Task 2", "Task 3"])
        self.assertEqual(mock_exec.call_count, 3)

    # --- CTO Red Team R3: Acceptance 3 ---
    def test_real_blocking_child_process_killed_on_heartbeat_loss(self) -> None:
        """Acceptance 3: Real blocking child process (python sleep 5s) is killed immediately on heartbeat failure."""
        cancel_event = threading.Event()

        # Launch real blocking child process: python -c "import time; time.sleep(5)"
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]

        start_time = time.time()

        # Background thread triggers cancellation after 0.1s
        def _trigger_cancel():
            time.sleep(0.1)
            cancel_event.set()

        threading.Thread(target=_trigger_cancel, daemon=True).start()

        # Execute command with cancel_event watchdog
        code, stdout, stderr = run_safe_process_tree(cmd, timeout_sec=10, cancel_event=cancel_event)
        elapsed = time.time() - start_time

        # Process MUST be terminated quickly (well under 2 seconds, not the full 5 seconds)
        self.assertLess(elapsed, 2.0, f"Child process took {elapsed}s; was NOT killed immediately on cancellation!")
        self.assertEqual(code, -2)
        self.assertIn("Task cancelled", stderr)

    # --- CTO Red Team R3: Acceptance 4 ---
    def test_legacy_ghost_task_cleanup_without_token(self) -> None:
        """Acceptance 4: Running events with NULL owner_token from older schemas are cleaned on startup."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        # Inject legacy running task with owner_token IS NULL and idle session
        with self.store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO event_ledger (event_id, idempotency_key, payload_hash, source, seat_id, conversation_id, prompt, status, owner_token, created_at)
                VALUES ('ghost_1', 'key_ghost', 'hash_g', 'web', 'lead', ?, 'Legacy ghost prompt', 'running', NULL, ?)
                """,
                (cid, time.time() - 100),
            )

        # Simulate new SessionHub starting up
        recovering_hub = SessionHub(StateStore(self.db_path))

        # Legacy running event MUST be reconciled to failed with interrupted explanation
        ghost_evt = recovering_hub.get_event("ghost_1")
        self.assertIsNotNone(ghost_evt)
        assert ghost_evt is not None
        self.assertEqual(ghost_evt.status, "failed")
        self.assertIn("Interrupted: legacy orphan task", str(ghost_evt.error))

    # --- CTO Red Team R3: Acceptance 5 (Preserve Existing Defenses) ---
    def test_active_task_not_killed_by_second_hub_startup(self) -> None:
        """Preserved: Active task with valid lease is NOT killed by second hub startup."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        slow_exec = MockExecutor(exit_code=0, stdout="Slow result", delay_sec=0.25)
        task_started = threading.Event()
        task_done = threading.Event()

        def _run_task():
            task_started.set()
            self.hub.dispatch_task(
                seat_id="lead",
                prompt="Long active turn",
                source="telegram",
                idempotency_key="active_turn_key",
                executor_override=slow_exec,
            )
            task_done.set()

        t = threading.Thread(target=_run_task)
        t.start()
        task_started.wait()
        time.sleep(0.05)

        # Start second independent SessionHub
        hub2 = SessionHub(StateStore(self.db_path))

        # Check: Task in DB MUST STILL BE 'running'
        evt_after_hub2 = hub2.get_event_by_idempotency_key("active_turn_key")
        self.assertIsNotNone(evt_after_hub2)
        assert evt_after_hub2 is not None
        self.assertEqual(evt_after_hub2.status, "running")

        t.join()
        task_done.wait()

        final_evt = self.hub.get_event_by_idempotency_key("active_turn_key")
        self.assertEqual(final_evt.status, "completed")

    def test_in_place_schema_migration_without_data_loss(self) -> None:
        """Preserved: In-place migration from older schema without data loss."""
        old_db_path = self.workspace / "old_schema.sqlite3"
        conn = sqlite3.connect(str(old_db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    seat_id TEXT PRIMARY KEY,
                    engine TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source_ide_conversation_id TEXT,
                    owner_pid INTEGER,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_activity REAL NOT NULL
                );
                CREATE TABLE event_ledger (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL,
                    seat_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    error TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );
                INSERT INTO sessions VALUES ('lead', 'antigravity', '56b7c33a-d1df-4b11-9363-78bb32e32c2a', '/old', 'Lead', NULL, NULL, 'idle', 12345.0);
                INSERT INTO event_ledger VALUES ('e1', 'k1', 'web', 'lead', '56b7c33a-d1df-4b11-9363-78bb32e32c2a', 'Old prompt', 'Old resp', 'completed', 0, NULL, 100.0, 105.0);
                PRAGMA user_version = 0;
                """
            )
            conn.commit()
        finally:
            conn.close()

        upgraded_store = StateStore(old_db_path)
        upgraded_hub = SessionHub(upgraded_store)

        session = upgraded_hub.get_session("lead")
        self.assertEqual(session.conversation_id, "56b7c33a-d1df-4b11-9363-78bb32e32c2a")
        self.assertEqual(session.workspace, "/old")

        event = upgraded_hub.get_event("e1")
        self.assertEqual(event.prompt, "Old prompt")
        self.assertEqual(event.status, "completed")

    def test_idempotent_retry_and_fingerprint_mismatch(self) -> None:
        """Preserved: Failed task retries under same key; payload mismatch fails loud."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        fail_exec = MockExecutor(exit_code=1, stderr="Network error")
        evt_fail = self.hub.dispatch_task(
            seat_id="lead",
            prompt="Deploy contract",
            source="telegram",
            idempotency_key="key_retry_5",
            executor_override=fail_exec,
        )
        self.assertEqual(evt_fail.status, "failed")

        success_exec = MockExecutor(exit_code=0, stdout="Deploy OK")
        evt_retry = self.hub.dispatch_task(
            seat_id="lead",
            prompt="Deploy contract",
            source="telegram",
            idempotency_key="key_retry_5",
            executor_override=success_exec,
        )
        self.assertEqual(evt_retry.status, "completed")
        self.assertEqual(evt_retry.response, "Deploy OK")
        self.assertEqual(evt_retry.retry_count, 1)

        with self.assertRaises(IdempotentPayloadMismatchError):
            self.hub.enqueue_task(
                seat_id="lead",
                prompt="TAMPERED PROMPT",
                source="telegram",
                idempotency_key="key_retry_5",
            )

    # --- CTO Red Team R4: Acceptance 1 (Full Chain) ---
    def test_hub_full_chain_heartbeat_failure_kills_child_and_releases_busy(self) -> None:
        """P0 Full Chain: SessionHub -> Heartbeat failure -> real blocking child process killed -> no exception -> event closed -> session not busy."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        blocking_exec = BlockingChildProcessExecutor(sleep_sec=5.0)
        e = self.hub.enqueue_task("lead", "Blocking task for full chain test", idempotency_key="chain_key_1")

        start_time = time.time()
        # Call process_next_task with force_heartbeat_failure=True
        # MUST NOT raise unhandled exception!
        processed = self.hub.process_next_task(
            seat_id="lead",
            timeout_sec=10,
            executor_override=blocking_exec,
            force_heartbeat_failure=True,
        )
        elapsed = time.time() - start_time

        # 1. 真实阻塞子进程必须被快速杀死 (3.5秒内，而不是运行满5秒)
        self.assertLess(elapsed, 3.5, f"Child process took {elapsed}s; was not killed immediately on heartbeat failure!")

        # 2. event 收口 (status == 'failed', error 记录 Interrupted)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        self.assertIn("Interrupted", str(processed.error))

        # 3. session 不残留 busy
        sess = self.hub.get_session("lead")
        self.assertEqual(sess.status, "idle")
        self.assertIsNone(sess.owner_token)

    # --- CTO Red Team R4: Acceptance 2 (Worker.stop in-flight) ---
    def test_worker_stop_cancels_active_in_flight_task_and_releases_seat(self) -> None:
        """P1: Worker.stop(cancel_active=True) cancels active in-flight child process within 2s, closes event as failed/interrupted, and restores seat to idle."""
        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        self.hub.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        blocking_exec = BlockingChildProcessExecutor(sleep_sec=5.0)
        e = self.hub.enqueue_task("lead", "Long task to be stopped", idempotency_key="worker_stop_key_1")

        worker = self.hub.create_worker("lead", poll_interval=0.02, executor_override=blocking_exec)
        worker.start()

        # Wait until task is actively running
        start_wait = time.time()
        while time.time() - start_wait < 2.0:
            cur = self.hub.get_event(e.event_id)
            sess = self.hub.get_session("lead")
            if cur and cur.status == "running" and sess.status == "busy":
                break
            time.sleep(0.02)

        cur_check = self.hub.get_event(e.event_id)
        self.assertEqual(cur_check.status, "running")

        # Now call worker.stop(cancel_active=True)
        stop_start = time.time()
        worker.stop(cancel_active=True, timeout=2.0)
        stop_elapsed = time.time() - stop_start

        # 1. 2 秒内线程退出
        self.assertLess(stop_elapsed, 2.0, f"Worker.stop took {stop_elapsed}s; exceeded 2s limit!")
        self.assertFalse(worker._thread.is_alive())

        # 2. 事件为 interrupted/failed
        stopped_evt = self.hub.get_event(e.event_id)
        self.assertIsNotNone(stopped_evt)
        assert stopped_evt is not None
        self.assertEqual(stopped_evt.status, "failed")
        self.assertIn("Interrupted", str(stopped_evt.error))

        # 3. 席位恢复 idle
        sess_after = self.hub.get_session("lead")
        self.assertEqual(sess_after.status, "idle")
        self.assertIsNone(sess_after.owner_token)

    # --- CTO Red Team R5: Cross-Instance Atomic Idempotent Race Acceptance ---
    def test_cross_instance_concurrent_enqueue_race_barrier(self) -> None:
        """R5 Acceptance: Two independent SessionHub instances share same SQLite.
        Submit same key with different prompts via threading.Barrier simultaneously.
        Exactly one succeeds, other gets IdempotentPayloadMismatchError.
        event_ledger has exactly 1 row.
        event_transitions has NO orphaned rows without matching event_ledger record.
        """
        hub1 = SessionHub(StateStore(self.db_path))
        hub2 = SessionHub(StateStore(self.db_path))

        cid = "56b7c33a-d1df-4b11-9363-78bb32e32c2a"
        hub1.register_or_update_session(
            seat_id="lead",
            engine="antigravity",
            conversation_id=cid,
            workspace=self.workspace,
            role="Lead",
        )

        barrier = threading.Barrier(2)
        shared_key = "race_barrier_key_1"

        results: list[Any] = []
        errors: list[Exception] = []

        def _submit(hub_inst: SessionHub, prompt_text: str) -> None:
            barrier.wait()
            try:
                evt = hub_inst.enqueue_task(
                    seat_id="lead",
                    prompt=prompt_text,
                    source="web",
                    idempotency_key=shared_key,
                )
                results.append(evt)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_submit, args=(hub1, "PROMPT ALPHA"))
        t2 = threading.Thread(target=_submit, args=(hub2, "PROMPT BETA"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 1. 必须恰好一个成功，另一个 PayloadMismatch
        self.assertEqual(len(results), 1, f"Expected exactly 1 success, got {len(results)}")
        self.assertEqual(len(errors), 1, f"Expected exactly 1 error, got {len(errors)}")
        self.assertIsInstance(errors[0], IdempotentPayloadMismatchError)

        # 2. event_ledger 仅一行
        with StateStore(self.db_path)._get_connection() as conn:
            cur = conn.execute("SELECT * FROM event_ledger WHERE idempotency_key = ?", (shared_key,))
            ledger_rows = cur.fetchall()
            self.assertEqual(len(ledger_rows), 1)

            winning_event_id = ledger_rows[0]["event_id"]

            # 3. event_transitions 无任何找不到 ledger 主记录的孤立行
            cur_trans = conn.execute(
                """
                SELECT * FROM event_transitions
                WHERE event_id NOT IN (SELECT event_id FROM event_ledger)
                """
            )
            orphaned_transitions = cur_trans.fetchall()
            self.assertEqual(len(orphaned_transitions), 0, f"Found orphaned transitions: {orphaned_transitions}")

            # 确认该 seat 的 transition 严格归属于获胜的 event_id
            cur_this = conn.execute("SELECT * FROM event_transitions WHERE seat_id = 'lead'")
            for tr in cur_this.fetchall():
                self.assertEqual(tr["event_id"], winning_event_id)


if __name__ == "__main__":
    unittest.main()
