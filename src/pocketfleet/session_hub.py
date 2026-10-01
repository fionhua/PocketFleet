"""Unified Session Hub, Durable FIFO Queue, and Single-Writer Consumer (PF-03R3 Final).

Strict CTO Red Team R3 Compliance:
1. Strict Fencing: When lease is lost, old worker CANNOT overwrite event_ledger.
   Only appends immutable lease_lost audit transition. record_event_finish enforces
   active lease check; returns False and fails loud on stolen leases.
2. True resident worker: SessionWorker provides persistent FIFO drain loop.
   Tasks remain queued until processed; new enqueue wakes worker; restarts drain legacy queued.
3. Hard process-tree kill on heartbeat loss: cancel_event watchdog terminates entire
   process tree within milliseconds, preventing zombie workers from modifying workspace.
4. Legacy ghost task reconciliation: Running events with NULL owner_token or stale leases
   are atomically reconciled to failed/interrupted on startup.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .antigravity_tracks import is_valid_uuid
from .executors.antigravity import AntigravityExecutor
from .executors.base import BaseExecutor
from .state import (
    DEFAULT_DB_PATH,
    IdempotentConflictError as StateIdempotentConflictError,
    IdempotentPayloadMismatchError as StateIdempotentPayloadMismatchError,
    StateStore,
    StateStoreError,
    is_pid_alive,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_DURATION = 60.0  # seconds


class SessionHubError(Exception):
    """Base exception for Session Hub operations."""


class SessionNotFoundError(SessionHubError):
    """Raised when the specified seat session is not found or unconfigured."""


class SessionBusyError(SessionHubError):
    """Raised when an operation conflicts with an active single-writer lease."""


IdempotentConflictError = StateIdempotentConflictError
IdempotentPayloadMismatchError = StateIdempotentPayloadMismatchError


class IDEAccessForbiddenError(SessionHubError):
    """Raised when attempting forbidden runtime access to IDE internal state."""


class LeaseLostError(SessionHubError):
    """Raised when a worker loses its session lease during execution."""


@dataclass
class SessionRecord:
    seat_id: str
    engine: str
    conversation_id: str
    workspace: str
    role: str
    source_ide_conversation_id: Optional[str] = None
    owner_pid: Optional[int] = None
    owner_token: Optional[str] = None
    lease_expires_at: float = 0.0
    status: str = "idle"  # "idle" | "busy" | "quarantined"
    last_activity: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        return cls(
            seat_id=data["seat_id"],
            engine=data["engine"],
            conversation_id=data["conversation_id"],
            workspace=data["workspace"],
            role=data["role"],
            source_ide_conversation_id=data.get("source_ide_conversation_id"),
            owner_pid=data.get("owner_pid"),
            owner_token=data.get("owner_token"),
            lease_expires_at=float(data.get("lease_expires_at", 0.0)),
            status=data.get("status", "idle"),
            last_activity=float(data.get("last_activity", 0.0)),
        )


@dataclass
class FleetEvent:
    event_id: str
    idempotency_key: str
    payload_hash: str
    source: str  # "web" | "telegram" | "system"
    seat_id: str
    conversation_id: str
    prompt: str
    response: Optional[str] = None
    status: str = "queued"  # "queued" | "running" | "completed" | "failed"
    exit_code: Optional[int] = None
    error: Optional[str] = None
    retry_count: int = 0
    owner_token: Optional[str] = None
    reply_chat_id: Optional[int] = None
    reply_message_id: Optional[int] = None
    created_at: float = 0.0
    completed_at: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetEvent:
        return cls(
            event_id=data["event_id"],
            idempotency_key=data["idempotency_key"],
            payload_hash=data.get("payload_hash", ""),
            source=data["source"],
            seat_id=data["seat_id"],
            conversation_id=data["conversation_id"],
            prompt=data["prompt"],
            response=data.get("response"),
            status=data.get("status", "queued"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            retry_count=int(data.get("retry_count", 0)),
            owner_token=data.get("owner_token"),
            reply_chat_id=data.get("reply_chat_id"),
            reply_message_id=data.get("reply_message_id"),
            created_at=float(data.get("created_at", 0.0)),
            completed_at=float(data["completed_at"]) if data.get("completed_at") is not None else None,
        )


def compute_payload_hash(
    seat_id: str,
    source: str,
    prompt: str,
    conversation_id: str = "",
) -> str:
    """Compute deterministic SHA-256 fingerprint for request payload validation.

    Includes conversation_id to prevent replaying tasks onto new tracks if seat is rebound.
    """
    raw = f"{seat_id.strip()}:{conversation_id.strip().lower()}:{source.strip()}:{prompt.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SessionWorker:
    """Resident background worker that continuously drains the FIFO task queue for a seat."""

    def __init__(
        self,
        hub: SessionHub,
        seat_id: str,
        poll_interval: float = 0.05,
        executor_override: Optional[BaseExecutor] = None,
    ) -> None:
        self.hub = hub
        self.seat_id = seat_id
        self.poll_interval = poll_interval
        self.executor_override = executor_override
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, cancel_active: bool = True, timeout: float = 5.0) -> None:
        """Stop worker loop and optionally cancel in-flight active tasks."""
        # 1. 阻止领取新任务
        self._stop_event.set()
        self._wake_event.set()

        # 2. 设置当前 cancel_event，杀灭在途子进程树
        if cancel_active:
            self.hub.cancel_active_task(self.seat_id)

        # 3. 等待任务与租约安全收口
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            # 4. 超时后 Fail-Loud，不得静默返回仍存活线程
            if self._thread.is_alive():
                raise RuntimeError(
                    f"SessionWorker for seat '{self.seat_id}' failed to stop within {timeout}s (worker thread still alive)."
                )

    def wake(self) -> None:
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Attempt to process next task
            try:
                processed = self.hub.process_next_task(
                    seat_id=self.seat_id,
                    executor_override=self.executor_override,
                )
            except Exception as exc:
                logger.exception("Unexpected error in worker loop for seat '%s': %s", self.seat_id, exc)
                time.sleep(self.poll_interval)
                continue

            if processed is not None:
                # Successfully processed a task; immediately continue draining queue
                continue

            # Queue empty or seat busy -> wait for wake event or timeout
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()


class SessionHub:
    """Coordinates session registry, durable FIFO task queue, and single-writer consumers."""

    def __init__(self, state_store: Optional[StateStore] = None) -> None:
        self.store = state_store or StateStore()
        self._workers: Dict[str, SessionWorker] = {}
        self._active_cancels: Dict[str, threading.Event] = {}
        # Precise startup orphan reconciliation: never kills running tasks with valid active leases
        self.reconcile_orphans()

    def get_active_cancel_event(self, seat_id: str) -> Optional[threading.Event]:
        return self._active_cancels.get(seat_id)

    def cancel_active_task(self, seat_id: str) -> bool:
        """Set cancel_event for any active task on seat_id."""
        evt = self._active_cancels.get(seat_id)
        if evt:
            evt.set()
            return True
        return False

    def reconcile_orphans(self) -> List[dict[str, Any]]:
        """Reconcile orphaned tasks and sessions left by crashed or killed processes."""
        recovered = self.store.reconcile_orphans()
        if recovered:
            logger.warning("Reconciled %d orphaned items on startup: %s", len(recovered), recovered)
        return recovered

    def register_or_update_session(
        self,
        seat_id: str,
        engine: str,
        conversation_id: str,
        workspace: Path | str,
        role: str,
        source_ide_conversation_id: Optional[str] = None,
    ) -> SessionRecord:
        """Register or update an authorized authoritative CLI session.
        
        Strict defense:
        - Fails loud if seat is actively running under an active lease.
        - Forbids pointing workspace into IDE private directories.
        - Validates all UUIDs.
        """
        cid = str(conversation_id).strip().lower()
        if not is_valid_uuid(cid):
            raise ValueError(f"Invalid conversation_id '{conversation_id}'. Must be a valid UUID.")

        src_ide: Optional[str] = None
        if source_ide_conversation_id:
            src_ide = str(source_ide_conversation_id).strip().lower()
            if not is_valid_uuid(src_ide):
                raise ValueError(
                    f"Invalid source_ide_conversation_id '{source_ide_conversation_id}'. Must be a valid UUID."
                )

        ws_path = Path(workspace).resolve()
        ws_str = str(ws_path).lower()
        if "antigravity-ide" in ws_str and ("conversations" in ws_str or "brain" in ws_str):
            raise IDEAccessForbiddenError(
                f"Forbidden workspace path '{ws_path}'. "
                "Runtime direct access to IDE internal state is prohibited by architecture."
            )

        try:
            self.store.register_session(
                seat_id=seat_id,
                engine=engine,
                conversation_id=cid,
                workspace=str(ws_path),
                role=role,
                source_ide_conversation_id=src_ide,
                status="idle",
                owner_pid=None,
            )
        except RuntimeError as re:
            raise SessionBusyError(str(re)) from re

        return self.get_session(seat_id)

    def get_session(self, seat_id: str) -> SessionRecord:
        row = self.store.get_session(seat_id)
        if not row:
            raise SessionNotFoundError(f"Session for seat '{seat_id}' not found.")
        return SessionRecord.from_dict(row)

    def list_sessions(self) -> List[SessionRecord]:
        rows = self.store.list_sessions()
        return [SessionRecord.from_dict(r) for r in rows]

    def get_event_by_idempotency_key(self, idempotency_key: str) -> Optional[FleetEvent]:
        row = self.store.get_event_by_idempotency_key(idempotency_key)
        return FleetEvent.from_dict(row) if row else None

    def get_event(self, event_id: str) -> Optional[FleetEvent]:
        row = self.store.get_event(event_id)
        return FleetEvent.from_dict(row) if row else None

    def get_recent_events(self, limit: int = 50, seat_id: Optional[str] = None) -> List[FleetEvent]:
        rows = self.store.get_recent_events(limit=limit, seat_id=seat_id)
        return [FleetEvent.from_dict(r) for r in rows]

    def get_undelivered_tg_events(self, limit: int = 50) -> List[FleetEvent]:
        rows = self.store.get_undelivered_tg_events(limit=limit)
        return [FleetEvent.from_dict(r) for r in rows]

    def try_record_event_delivery(self, event_id: str, destination: str) -> bool:
        return self.store.try_record_event_delivery(event_id, destination)

    def is_event_delivered(self, event_id: str, destination: str) -> bool:
        return self.store.is_event_delivered(event_id, destination)

    def get_event_transitions(self, event_id: str) -> List[dict[str, Any]]:
        return self.store.get_event_transitions(event_id)

    def create_worker(
        self,
        seat_id: str,
        poll_interval: float = 0.05,
        executor_override: Optional[BaseExecutor] = None,
    ) -> SessionWorker:
        """Create and register a resident FIFO worker for a seat."""
        worker = SessionWorker(
            hub=self,
            seat_id=seat_id,
            poll_interval=poll_interval,
            executor_override=executor_override,
        )
        self._workers[seat_id] = worker
        return worker

    # --- P0: True Durable Asynchronous Queue ---

    def enqueue_task(
        self,
        seat_id: str,
        prompt: str,
        source: str = "web",
        idempotency_key: Optional[str] = None,
        reply_chat_id: Optional[int] = None,
        reply_message_id: Optional[int] = None,
    ) -> FleetEvent:
        """Enqueue task persistently into event_ledger without blocking the caller."""
        session = self.get_session(seat_id)
        idem_key = idempotency_key or f"evt_{uuid.uuid4().hex}"
        payload_hash = compute_payload_hash(
            seat_id=seat_id,
            source=source,
            prompt=prompt,
            conversation_id=session.conversation_id,
        )

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        event_dict, is_new_or_retried = self.store.atomic_enqueue_or_retry_event(
            event_id=event_id,
            idempotency_key=idem_key,
            payload_hash=payload_hash,
            source=source,
            seat_id=seat_id,
            conversation_id=session.conversation_id,
            prompt=prompt,
            reply_chat_id=reply_chat_id,
            reply_message_id=reply_message_id,
        )

        # Wake up resident worker if newly enqueued or retried
        if is_new_or_retried and seat_id in self._workers:
            self._workers[seat_id].wake()

        return FleetEvent.from_dict(event_dict)

    # --- P0: Single-Writer Consumer Execution with Fencing ---

    def process_next_task(
        self,
        seat_id: str,
        timeout_sec: int = 300,
        executor_override: Optional[BaseExecutor] = None,
        force_heartbeat_failure: bool = False,
    ) -> Optional[FleetEvent]:
        """Atomically pull earliest queued task and execute under CAS lease.
        
        Strict Fencing Constraints:
        - If seat is busy, does NOT fail the task; task remains 'queued' for later execution.
        - Exactly max_active=1 per seat.
        - Heartbeat failure immediately sets cancel_event to terminate the child process tree.
        - If lease is lost, old worker is forbidden from touching event_ledger snapshot!
        """
        queued_dict = self.store.fetch_next_queued_task(seat_id)
        if not queued_dict:
            return None

        event_id = queued_dict["event_id"]
        session = self.get_session(seat_id)
        current_pid = os.getpid()
        owner_token = f"{current_pid}:{threading.get_ident()}:{uuid.uuid4().hex[:6]}"

        # 1. Attempt to acquire cross-process atomic CAS lease
        acquired = self.store.acquire_session_lease(
            seat_id=seat_id,
            owner_token=owner_token,
            owner_pid=current_pid,
            lease_duration=float(timeout_sec + 30),
        )
        if not acquired:
            # Seat is busy executing another task: keep queued, do NOT mark failed!
            logger.debug("Seat '%s' is currently busy; task '%s' remains queued.", seat_id, event_id)
            return None

        # 2. Transition event status to running under owner_token
        self.store.record_event_start(event_id, owner_token=owner_token, owner_pid=current_pid)

        # 3. Cancellation watchdog & background lease renewer
        cancel_event = threading.Event()
        self._active_cancels[seat_id] = cancel_event
        stop_renew = threading.Event()
        lease_lost = threading.Event()

        if force_heartbeat_failure:
            lease_lost.set()
            cancel_event.set()

        def _lease_heartbeat():
            while not stop_renew.wait(5.0):
                if force_heartbeat_failure:
                    lease_lost.set()
                    cancel_event.set()
                    break
                try:
                    renewed = self.store.renew_session_lease(seat_id, owner_token, lease_duration=60.0)
                    if not renewed:
                        lease_lost.set()
                        cancel_event.set()
                        break
                except Exception:
                    lease_lost.set()
                    cancel_event.set()
                    break

        heartbeat_thread = threading.Thread(target=_lease_heartbeat, daemon=True)
        heartbeat_thread.start()

        response_text: Optional[str] = None
        exit_code: int = 0
        error_msg: Optional[str] = None

        try:
            # 4. Resolve executor
            if executor_override:
                executor = executor_override
            else:
                if session.engine == "antigravity":
                    executor = AntigravityExecutor(conversation_id=session.conversation_id)
                else:
                    raise NotImplementedError(f"Engine '{session.engine}' executor not implemented.")

            # 5. Execute turn with cancel_event watchdog
            exit_code, stdout_str, stderr_str = executor.execute(
                prompt=queued_dict["prompt"],
                cwd=session.workspace,
                timeout_sec=timeout_sec,
                cancel_event=cancel_event,
            )
            response_text = stdout_str if exit_code == 0 else ""
            if exit_code != 0:
                error_msg = stderr_str or f"Executor exited with non-zero code {exit_code}."
        except Exception as exc:
            exit_code = -1
            error_msg = f"Task execution failed with exception: {exc}"
            logger.exception("Task execution exception for seat '%s'", seat_id)
        finally:
            stop_renew.set()
            self._active_cancels.pop(seat_id, None)

            # P0: Check if lease was lost or cancelled during execution
            if lease_lost.is_set() or cancel_event.is_set():
                reason = (
                    "Interrupted: session lease was lost (heartbeat failure)"
                    if lease_lost.is_set()
                    else "Interrupted: task execution was cancelled"
                )
                logger.error("Execution terminated for event '%s': %s", event_id, reason)
                self.store.abandon_event_if_owner(
                    event_id=event_id,
                    seat_id=seat_id,
                    owner_token=owner_token,
                    owner_pid=current_pid,
                    error_msg=reason,
                )
                # release_session_lease 始终可以安全尝试，由 token 条件防止释放新租约
                self.store.release_session_lease(seat_id, owner_token)
            else:
                # 6. Record final state with owner_token fencing
                written = self.store.record_event_finish(
                    event_id=event_id,
                    response=response_text,
                    exit_code=exit_code,
                    error=error_msg,
                    owner_token=owner_token,
                    owner_pid=current_pid,
                )
                if not written:
                    logger.error("Fencing check failed for event '%s': lease expired before finish write", event_id)
                    self.store.abandon_event_if_owner(
                        event_id=event_id,
                        seat_id=seat_id,
                        owner_token=owner_token,
                        owner_pid=current_pid,
                        error_msg="Interrupted: lease expired before finish write",
                    )

                # 7. Release lease only if still held
                self.store.release_session_lease(seat_id, owner_token)

        updated_row = self.store.get_event(event_id)
        return FleetEvent.from_dict(updated_row) if updated_row else None

    def dispatch_task_sync(
        self,
        seat_id: str,
        prompt: str,
        source: str = "web",
        idempotency_key: Optional[str] = None,
        timeout_sec: int = 300,
        executor_override: Optional[BaseExecutor] = None,
        force_heartbeat_failure: bool = False,
    ) -> FleetEvent:
        """Enqueue task and wait for it to be processed (for tests & synchronous CLI)."""
        evt = self.enqueue_task(
            seat_id=seat_id,
            prompt=prompt,
            source=source,
            idempotency_key=idempotency_key,
        )
        if evt.status == "completed":
            return evt

        # Process from queue
        start_t = time.time()
        while time.time() - start_t < timeout_sec:
            try:
                processed = self.process_next_task(
                    seat_id=seat_id,
                    timeout_sec=timeout_sec,
                    executor_override=executor_override,
                    force_heartbeat_failure=force_heartbeat_failure,
                )
                if processed and processed.event_id == evt.event_id:
                    return processed
            except Exception:
                cur = self.get_event(evt.event_id)
                if cur and cur.status in ("completed", "failed"):
                    return cur
                raise

            cur = self.get_event(evt.event_id)
            if cur and cur.status in ("completed", "failed"):
                return cur

            time.sleep(0.05)

        raise TimeoutError(f"Task '{evt.event_id}' timed out after {timeout_sec}s.")

    def dispatch_task(
        self,
        seat_id: str,
        prompt: str,
        source: str = "web",
        idempotency_key: Optional[str] = None,
        timeout_sec: int = 300,
        executor_override: Optional[BaseExecutor] = None,
    ) -> FleetEvent:
        """Backwards-compatible wrapper delegating to dispatch_task_sync."""
        return self.dispatch_task_sync(
            seat_id=seat_id,
            prompt=prompt,
            source=source,
            idempotency_key=idempotency_key,
            timeout_sec=timeout_sec,
            executor_override=executor_override,
        )
