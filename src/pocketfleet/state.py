"""PocketFleet Persistent State, Lease Manager & Outbox Database (PF-03R2).

Zero-dependency SQLite store for:
1. Update watermarks (last_update_id) - prevents duplicate processing on restart
2. Processed messages & deduplication ledger
3. Outbox retry queue - guarantees delivery even across network drops
4. Schema migrations via PRAGMA user_version for non-destructive upgrades
5. Sessions table with atomic cross-process CAS leases (owner_token, owner_pid, lease_expires_at)
6. Durable tasks table with payload fingerprints and owner_token verification
7. Append-Only event_transitions table for immutable state audit
8. Precise orphan reconciliation: never kills running tasks with valid active leases
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".pocketfleet" / "state.sqlite3"


def is_pid_alive(pid: int | None) -> bool:
    """Check if process with given PID is alive across platforms."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            # STILL_ACTIVE = 259
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                kernel32.CloseHandle(handle)
                return exit_code.value == 259
            kernel32.CloseHandle(handle)
            return False
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class StateStoreError(Exception):
    """Base exception for StateStore operations."""


class IdempotentPayloadMismatchError(StateStoreError):
    """Raised when a duplicate idempotency key is submitted with different payload parameters."""


class IdempotentConflictError(StateStoreError):
    """Raised when an active task exists under the same idempotency key."""


class StateStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            self.db_path = DEFAULT_DB_PATH
        else:
            self.db_path = Path(db_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    parse_mode TEXT,
                    reply_to_message_id INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at REAL NOT NULL,
                    last_attempt REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);

                CREATE TABLE IF NOT EXISTS sessions (
                    seat_id TEXT PRIMARY KEY,
                    engine TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source_ide_conversation_id TEXT,
                    owner_pid INTEGER,
                    owner_token TEXT,
                    lease_expires_at REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_activity REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_ledger (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    seat_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    owner_token TEXT,
                    reply_chat_id INTEGER,
                    reply_message_id INTEGER,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_ledger_idempotency ON event_ledger(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_ledger_seat ON event_ledger(seat_id, created_at);

                CREATE TABLE IF NOT EXISTS event_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    seat_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    owner_pid INTEGER,
                    detail TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_transitions_event ON event_transitions(event_id, transition_id);

                CREATE TABLE IF NOT EXISTS event_deliveries (
                    event_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    delivered_at REAL NOT NULL,
                    PRIMARY KEY (event_id, destination)
                );

                CREATE TABLE IF NOT EXISTS binding_pins (
                    pin_hash TEXT PRIMARY KEY,
                    seat_role TEXT NOT NULL,
                    bot_id INTEGER NOT NULL,
                    bot_username TEXT NOT NULL,
                    authorized_user_id INTEGER,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS broker_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    seat_role TEXT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_broker_events_id ON broker_events(event_id);
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Safe non-destructive migration for upgrading older schemas."""
        cur = conn.execute("PRAGMA user_version")
        ver = cur.fetchone()[0]

        if ver < 1:
            # Check sessions table
            cur = conn.execute("PRAGMA table_info(sessions)")
            s_cols = {row[1] for row in cur.fetchall()}
            if "owner_token" not in s_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN owner_token TEXT")
            if "lease_expires_at" not in s_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0.0")

            # Check event_ledger table
            cur = conn.execute("PRAGMA table_info(event_ledger)")
            e_cols = {row[1] for row in cur.fetchall()}
            if "owner_token" not in e_cols:
                conn.execute("ALTER TABLE event_ledger ADD COLUMN owner_token TEXT")
            if "payload_hash" not in e_cols:
                conn.execute("ALTER TABLE event_ledger ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''")
            if "retry_count" not in e_cols:
                conn.execute("ALTER TABLE event_ledger ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")

            conn.execute("PRAGMA user_version = 1")

        if ver < 2:
            # 1. Check event_ledger reply columns
            cur = conn.execute("PRAGMA table_info(event_ledger)")
            e_cols = {row[1] for row in cur.fetchall()}
            if "reply_chat_id" not in e_cols:
                conn.execute("ALTER TABLE event_ledger ADD COLUMN reply_chat_id INTEGER")
            if "reply_message_id" not in e_cols:
                conn.execute("ALTER TABLE event_ledger ADD COLUMN reply_message_id INTEGER")

            # 2. Check processed_messages primary key (composite migration)
            cur = conn.execute("PRAGMA table_info(processed_messages)")
            pm_info = cur.fetchall()
            pk_cols = [r[1] for r in pm_info if r[5] > 0]
            if len(pk_cols) == 1 and pk_cols[0] == "message_id":
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS processed_messages_new (
                        chat_id INTEGER NOT NULL DEFAULT 0,
                        message_id INTEGER NOT NULL,
                        prompt TEXT NOT NULL,
                        worker TEXT NOT NULL,
                        status TEXT NOT NULL,
                        exit_code INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (chat_id, message_id)
                    );
                    INSERT OR IGNORE INTO processed_messages_new (chat_id, message_id, prompt, worker, status, exit_code, created_at, updated_at)
                    SELECT chat_id, message_id, prompt, worker, status, exit_code, created_at, updated_at FROM processed_messages;
                    DROP TABLE processed_messages;
                    ALTER TABLE processed_messages_new RENAME TO processed_messages;
                    """
                )

            conn.execute("PRAGMA user_version = 2")

    # --- Watermark Management ---

    def get_watermark(self, bot_id: Optional[str] = None) -> Optional[int]:
        key = f"last_update_id_{bot_id}" if bot_id else "last_update_id"
        with self._get_connection() as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return int(row["value"]) if row else None

    def set_watermark(self, update_id: int, bot_id: Optional[str] = None) -> None:
        key = f"last_update_id_{bot_id}" if bot_id else "last_update_id"
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(update_id)),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        """Retrieve a string metadata value by key from meta table."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        """Persist a string metadata key-value pair to meta table."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # --- Deduplication & Message State ---

    def is_message_processed(self, message_id: int, chat_id: Optional[int] = None) -> bool:
        with self._get_connection() as conn:
            if chat_id is not None:
                cur = conn.execute(
                    "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )
            else:
                cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
            return cur.fetchone() is not None

    def record_message_start(self, message_id: int, chat_id: int, prompt: str, worker: str) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO processed_messages (message_id, chat_id, prompt, worker, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET status = 'RUNNING', updated_at = excluded.updated_at
                """,
                (message_id, chat_id, prompt, worker, now, now),
            )

    def record_message_finish(
        self,
        message_id: int,
        status: str,
        exit_code: int,
        chat_id: Optional[int] = None,
    ) -> None:
        now = time.time()
        with self._get_connection() as conn:
            if chat_id is not None:
                conn.execute(
                    "UPDATE processed_messages SET status = ?, exit_code = ?, updated_at = ? WHERE chat_id = ? AND message_id = ?",
                    (status, exit_code, now, chat_id, message_id),
                )
            else:
                conn.execute(
                    "UPDATE processed_messages SET status = ?, exit_code = ?, updated_at = ? WHERE message_id = ?",
                    (status, exit_code, now, message_id),
                )

    def get_recent_tasks(self, limit: int = 50) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT message_id, chat_id, prompt, worker, status, exit_code, created_at, updated_at "
                "FROM processed_messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    # --- Outbox Management (Zero-Drop Delivery) ---

    def enqueue_outbox(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> int:
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO outbox (chat_id, text, parse_mode, reply_to_message_id, status, created_at, last_attempt)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (chat_id, text, parse_mode, reply_to_message_id, now, now),
            )
            return int(cur.lastrowid)

    def get_pending_outbox(self, max_retries: int = 5, limit: int = 10) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT id, chat_id, text, parse_mode, reply_to_message_id, retry_count
                FROM outbox
                WHERE status = 'PENDING' AND retry_count < ?
                ORDER BY id ASC LIMIT ?
                """,
                (max_retries, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_outbox_sent(self, outbox_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE outbox SET status = 'SENT' WHERE id = ?", (outbox_id,))

    def mark_outbox_failed_attempt(self, outbox_id: int) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE outbox SET retry_count = retry_count + 1, last_attempt = ? WHERE id = ?",
                (now, outbox_id),
            )

    # --- Session Registry Management (PF-03 Atomic CAS & Leases) ---

    def register_session(
        self,
        seat_id: str,
        engine: str,
        conversation_id: str,
        workspace: str,
        role: str,
        source_ide_conversation_id: Optional[str] = None,
        status: str = "idle",
        owner_pid: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        """Register or update session without stripping active owner lease."""
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT status, owner_pid, lease_expires_at FROM sessions WHERE seat_id = ?",
                (seat_id,),
            )
            existing = cur.fetchone()
            if existing and existing["status"] == "busy":
                pid = existing["owner_pid"]
                expires = existing["lease_expires_at"]
                if expires > now and pid and is_pid_alive(pid):
                    raise RuntimeError(
                        f"Cannot reconfigure seat '{seat_id}' while an active task is running (PID {pid})."
                    )

            conn.execute(
                """
                INSERT INTO sessions (
                    seat_id, engine, conversation_id, workspace, role,
                    source_ide_conversation_id, owner_pid, owner_token, lease_expires_at, status, last_activity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)
                ON CONFLICT(seat_id) DO UPDATE SET
                    engine = excluded.engine,
                    conversation_id = excluded.conversation_id,
                    workspace = excluded.workspace,
                    role = excluded.role,
                    source_ide_conversation_id = COALESCE(excluded.source_ide_conversation_id, sessions.source_ide_conversation_id),
                    owner_pid = CASE WHEN sessions.status = 'busy' THEN sessions.owner_pid ELSE excluded.owner_pid END,
                    owner_token = CASE WHEN sessions.status = 'busy' THEN sessions.owner_token ELSE excluded.owner_token END,
                    status = CASE WHEN sessions.status = 'busy' THEN sessions.status ELSE excluded.status END,
                    last_activity = excluded.last_activity
                """,
                (
                    seat_id,
                    engine,
                    conversation_id,
                    workspace,
                    role,
                    source_ide_conversation_id,
                    owner_pid,
                    owner_token,
                    status,
                    now,
                ),
            )

    def get_session(self, seat_id: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM sessions WHERE seat_id = ?", (seat_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_sessions(self) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM sessions ORDER BY seat_id ASC")
            return [dict(row) for row in cur.fetchall()]

    def acquire_session_lease(
        self,
        seat_id: str,
        owner_token: str,
        owner_pid: int,
        lease_duration: float = 60.0,
    ) -> bool:
        """Atomic Compare-And-Swap (CAS) session lease acquisition across processes & threads."""
        now = time.time()
        expires = now + lease_duration
        with self._get_connection() as conn:
            # 1. Clean up stale lease if owner process is dead or lease expired
            cur = conn.execute(
                "SELECT status, owner_pid, owner_token, lease_expires_at FROM sessions WHERE seat_id = ?",
                (seat_id,),
            )
            row = cur.fetchone()
            if not row:
                return False

            if row["status"] == "busy":
                cur_pid = row["owner_pid"]
                cur_exp = row["lease_expires_at"]
                if cur_exp < now or (cur_pid and not is_pid_alive(cur_pid)):
                    conn.execute(
                        "UPDATE sessions SET status = 'idle', owner_pid = NULL, owner_token = NULL, lease_expires_at = 0.0 WHERE seat_id = ?",
                        (seat_id,),
                    )

            # 2. Atomic CAS update strictly checking owner_token
            cur = conn.execute(
                """
                UPDATE sessions
                SET status = 'busy', owner_pid = ?, owner_token = ?, lease_expires_at = ?, last_activity = ?
                WHERE seat_id = ?
                  AND (
                      status = 'idle'
                      OR lease_expires_at < ?
                      OR owner_token IS NULL
                      OR owner_token = ?
                  )
                """,
                (owner_pid, owner_token, expires, now, seat_id, now, owner_token),
            )
            return cur.rowcount > 0

    def renew_session_lease(
        self,
        seat_id: str,
        owner_token: str,
        lease_duration: float = 60.0,
    ) -> bool:
        now = time.time()
        expires = now + lease_duration
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE sessions
                SET lease_expires_at = ?, last_activity = ?
                WHERE seat_id = ? AND owner_token = ? AND status = 'busy'
                """,
                (expires, now, seat_id, owner_token),
            )
            return cur.rowcount > 0

    def release_session_lease(self, seat_id: str, owner_token: str) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE sessions
                SET status = 'idle', owner_pid = NULL, owner_token = NULL, lease_expires_at = 0.0, last_activity = ?
                WHERE seat_id = ? AND owner_token = ?
                """,
                (now, seat_id, owner_token),
            )
            return cur.rowcount > 0

    def reconcile_orphans(self) -> List[dict[str, Any]]:
        """Atomically reconcile orphaned tasks and sessions left by crashed or killed processes.

        Strict constraint: NEVER kills a running event whose session lease is still active and valid.
        Cleans legacy ghost tasks (owner_token IS NULL) left from older schemas.
        """
        now = time.time()
        recovered: List[dict[str, Any]] = []
        with self._get_connection() as conn:
            # 1. Inspect sessions with status='busy'
            cur = conn.execute("SELECT seat_id, owner_pid, owner_token, lease_expires_at FROM sessions WHERE status = 'busy'")
            stale_sessions: list[tuple[str, Optional[str], Optional[int]]] = []
            for row in cur.fetchall():
                seat_id = row["seat_id"]
                pid = row["owner_pid"]
                token = row["owner_token"]
                expires = row["lease_expires_at"]
                is_stale = False
                if expires < now:
                    is_stale = True
                elif pid and not is_pid_alive(pid):
                    is_stale = True

                if is_stale:
                    stale_sessions.append((seat_id, token, pid))

            # 2. Reset only truly stale sessions and their specific orphaned events
            for seat_id, token, pid in stale_sessions:
                conn.execute(
                    "UPDATE sessions SET status = 'idle', owner_pid = NULL, owner_token = NULL, lease_expires_at = 0.0, last_activity = ? WHERE seat_id = ?",
                    (now, seat_id),
                )
                recovered.append({"type": "session", "seat_id": seat_id, "dead_pid": pid})

                # Fail running tasks that belonged to the expired/dead token
                if token:
                    cur_evt = conn.execute(
                        "SELECT event_id FROM event_ledger WHERE seat_id = ? AND status = 'running' AND owner_token = ?",
                        (seat_id, token),
                    )
                    for evt_row in cur_evt.fetchall():
                        eid = evt_row["event_id"]
                        conn.execute(
                            """
                            UPDATE event_ledger
                            SET status = 'failed', error = 'Orphaned task: process exited before completing', completed_at = ?
                            WHERE event_id = ?
                            """,
                            (now, eid),
                        )
                        conn.execute(
                            """
                            INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, detail, created_at)
                            VALUES (?, ?, 'running', 'failed', 'Orphan recovery: process dead or lease expired', ?)
                            """,
                            (eid, seat_id, now),
                        )
                        recovered.append({"type": "event", "event_id": eid, "seat_id": seat_id})

            # 3. P1: Reconcile legacy ghost running events where owner_token IS NULL or seat has no valid lease
            cur_ghosts = conn.execute(
                """
                SELECT event_id, seat_id FROM event_ledger
                WHERE status = 'running'
                  AND (
                      owner_token IS NULL
                      OR owner_token = ''
                      OR NOT EXISTS (
                          SELECT 1 FROM sessions
                          WHERE seat_id = event_ledger.seat_id
                            AND status = 'busy'
                            AND lease_expires_at >= ?
                      )
                  )
                """,
                (now,),
            )
            for g_row in cur_ghosts.fetchall():
                g_id = g_row["event_id"]
                g_seat = g_row["seat_id"]
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'failed', error = 'Interrupted: legacy orphan task without active lease', completed_at = ?
                    WHERE event_id = ? AND status = 'running'
                    """,
                    (now, g_id),
                )
                conn.execute(
                    """
                    INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, detail, created_at)
                    VALUES (?, ?, 'running', 'failed', 'Orphan recovery: legacy ghost task cleaned', ?)
                    """,
                    (g_id, g_seat, now),
                )
                recovered.append({"type": "ghost_event", "event_id": g_id, "seat_id": g_seat})

        return recovered

    # --- Unified Event Ledger & Immutable Transitions (PF-03) ---

    def atomic_enqueue_or_retry_event(
        self,
        event_id: str,
        idempotency_key: str,
        payload_hash: str,
        source: str,
        seat_id: str,
        conversation_id: str,
        prompt: str,
        reply_chat_id: Optional[int] = None,
        reply_message_id: Optional[int] = None,
    ) -> Tuple[dict[str, Any], bool]:
        """Atomically enqueue task, retry failed task, or return existing idempotent hit.

        Executed under a single atomic BEGIN IMMEDIATE transaction:
        - Strict payload_hash verification (raises IdempotentPayloadMismatchError if different).
        - If active/completed: returns existing event, NEVER writes phantom transition.
        - If failed: resets to queued with retry_count increment, records single transition.
        - If new: inserts into event_ledger and records single transition.

        Returns:
            (event_dict, is_new_or_retried)
        """
        now = time.time()
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "SELECT * FROM event_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            existing = cur.fetchone()

            if existing:
                existing_hash = existing["payload_hash"]
                # 1. Strict payload mismatch check
                if existing_hash and existing_hash != payload_hash:
                    raise IdempotentPayloadMismatchError(
                        f"Idempotency key '{idempotency_key}' was previously used with a different request payload. "
                        "Refusing to reuse key with mismatched payload."
                    )

                status = existing["status"]
                if status in ("completed", "running", "queued"):
                    # Idempotent hit: return existing event, DO NOT add phantom transition!
                    conn.execute("COMMIT")
                    return dict(existing), False

                if status == "failed":
                    # Retry previously failed task
                    conn.execute(
                        """
                        UPDATE event_ledger
                        SET source = ?,
                            prompt = ?,
                            payload_hash = ?,
                            status = 'queued',
                            error = NULL,
                            response = NULL,
                            exit_code = NULL,
                            retry_count = retry_count + 1,
                            owner_token = NULL,
                            reply_chat_id = COALESCE(?, reply_chat_id),
                            reply_message_id = COALESCE(?, reply_message_id),
                            created_at = ?,
                            completed_at = NULL
                        WHERE idempotency_key = ? AND status = 'failed'
                        """,
                        (source, prompt, payload_hash, reply_chat_id, reply_message_id, now, idempotency_key),
                    )
                    conn.execute(
                        """
                        INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, detail, created_at)
                        VALUES (?, ?, 'failed', 'queued', 'Retried by ' || ?, ?)
                        """,
                        (existing["event_id"], seat_id, source, now),
                    )
                    cur_updated = conn.execute(
                        "SELECT * FROM event_ledger WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    row = cur_updated.fetchone()
                    conn.execute("COMMIT")
                    return dict(row), True

            # 2. Brand new task insertion
            conn.execute(
                """
                INSERT INTO event_ledger (
                    event_id, idempotency_key, payload_hash, source, seat_id,
                    conversation_id, prompt, reply_chat_id, reply_message_id, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    event_id,
                    idempotency_key,
                    payload_hash,
                    source,
                    seat_id,
                    conversation_id,
                    prompt,
                    reply_chat_id,
                    reply_message_id,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, detail, created_at)
                VALUES (?, ?, NULL, 'queued', 'Enqueued by ' || ?, ?)
                """,
                (event_id, seat_id, source, now),
            )
            cur_new = conn.execute(
                "SELECT * FROM event_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = cur_new.fetchone()
            conn.execute("COMMIT")
            return dict(row), True

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def record_event_queued(
        self,
        event_id: str,
        idempotency_key: str,
        payload_hash: str,
        source: str,
        seat_id: str,
        conversation_id: str,
        prompt: str,
        reply_chat_id: Optional[int] = None,
        reply_message_id: Optional[int] = None,
    ) -> None:
        """Backwards-compatible wrapper delegating to atomic_enqueue_or_retry_event."""
        self.atomic_enqueue_or_retry_event(
            event_id=event_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            source=source,
            seat_id=seat_id,
            conversation_id=conversation_id,
            prompt=prompt,
            reply_chat_id=reply_chat_id,
            reply_message_id=reply_message_id,
        )

    def fetch_next_queued_task(self, seat_id: str) -> Optional[dict[str, Any]]:
        """Fetch the earliest queued task for a seat by FIFO order."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM event_ledger
                WHERE seat_id = ? AND status = 'queued'
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (seat_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def record_event_start(self, event_id: str, owner_token: str, owner_pid: Optional[int] = None) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE event_ledger SET status = 'running', owner_token = ? WHERE event_id = ?",
                (owner_token, event_id),
            )
            cur = conn.execute("SELECT seat_id FROM event_ledger WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            seat_id = row["seat_id"] if row else ""
            conn.execute(
                """
                INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, owner_pid, detail, created_at)
                VALUES (?, ?, 'queued', 'running', ?, 'Execution turn started under token ' || ?, ?)
                """,
                (event_id, seat_id, owner_pid, owner_token, now),
            )

    def record_event_finish(
        self,
        event_id: str,
        response: Optional[str],
        exit_code: int,
        error: Optional[str] = None,
        owner_token: Optional[str] = None,
        owner_pid: Optional[int] = None,
    ) -> bool:
        """Finish task strictly verifying fencing token.

        Refuses writes if owner_token is missing or no longer owns the active lease.
        """
        if not owner_token:
            logger.error("Refusing record_event_finish for event '%s' without owner_token", event_id)
            return False

        now = time.time()
        to_status = "completed" if exit_code == 0 and not error else "failed"
        with self._get_connection() as conn:
            # Strict fencing check: event and session must still be held by owner_token
            cur = conn.execute(
                """
                UPDATE event_ledger
                SET status = ?, response = ?, exit_code = ?, error = ?, completed_at = ?
                WHERE event_id = ?
                  AND owner_token = ?
                  AND status = 'running'
                  AND EXISTS (
                      SELECT 1 FROM sessions
                      WHERE seat_id = event_ledger.seat_id
                        AND owner_token = ?
                        AND lease_expires_at >= ?
                  )
                """,
                (to_status, response, exit_code, error, now, event_id, owner_token, owner_token, now),
            )
            if cur.rowcount == 0:
                # Fencing token rejected: lease was lost or already overtaken
                return False

            cur_seat = conn.execute("SELECT seat_id FROM event_ledger WHERE event_id = ?", (event_id,))
            row = cur_seat.fetchone()
            seat_id = row["seat_id"] if row else ""
            detail = f"Finished with code {exit_code}" if to_status == "completed" else f"Failed: {error}"
            conn.execute(
                """
                INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, owner_pid, detail, created_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?)
                """,
                (event_id, seat_id, to_status, owner_pid, detail, now),
            )
            return True

    def record_lease_lost_transition(
        self,
        event_id: str,
        seat_id: str,
        owner_token: str,
        owner_pid: Optional[int] = None,
        detail: str = "Execution aborted: session lease lost to another worker",
    ) -> None:
        """Record an append-only lease lost audit event without modifying event_ledger snapshot."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, owner_pid, detail, created_at)
                VALUES (?, ?, 'running', 'lease_lost', ?, ?, ?)
                """,
                (event_id, seat_id, owner_pid, detail, now),
            )

    def abandon_event_if_owner(
        self,
        event_id: str,
        seat_id: str,
        owner_token: str,
        owner_pid: Optional[int] = None,
        error_msg: str = "Interrupted: session lease was lost or cancelled",
    ) -> bool:
        """Atomically abandon an in-flight event if it is still owned by owner_token.

        Rules:
        - If event.owner_token == owner_token and event.status == 'running':
            - If session.owner_token == owner_token:
                - Mark event status = 'failed', error = error_msg, completed_at = now
                - Set session status = 'idle', owner_pid = NULL, owner_token = NULL, lease_expires_at = 0.0
                - Append transition to 'failed'
                - Return True
            - Else:
                # Session was already overtaken by a new worker!
                # Do NOT touch event_ledger snapshot!
                - Append 'lease_lost' transition to event_transitions
                - Return False
        - Else:
            return False
        """
        now = time.time()
        with self._get_connection() as conn:
            cur_evt = conn.execute(
                "SELECT status, owner_token FROM event_ledger WHERE event_id = ?",
                (event_id,),
            )
            evt_row = cur_evt.fetchone()
            if not evt_row or evt_row["status"] != "running" or evt_row["owner_token"] != owner_token:
                return False

            cur_sess = conn.execute(
                "SELECT owner_token FROM sessions WHERE seat_id = ?",
                (seat_id,),
            )
            sess_row = cur_sess.fetchone()
            session_token = sess_row["owner_token"] if sess_row else None

            if session_token == owner_token:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'failed', error = ?, completed_at = ?
                    WHERE event_id = ? AND owner_token = ? AND status = 'running'
                    """,
                    (error_msg, now, event_id, owner_token),
                )
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'idle', owner_pid = NULL, owner_token = NULL, lease_expires_at = 0.0, last_activity = ?
                    WHERE seat_id = ? AND owner_token = ?
                    """,
                    (now, seat_id, owner_token),
                )
                conn.execute(
                    """
                    INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, owner_pid, detail, created_at)
                    VALUES (?, ?, 'running', 'failed', ?, ?, ?)
                    """,
                    (event_id, seat_id, owner_pid, error_msg, now),
                )
                return True
            else:
                conn.execute(
                    """
                    INSERT INTO event_transitions (event_id, seat_id, from_status, to_status, owner_pid, detail, created_at)
                    VALUES (?, ?, 'running', 'lease_lost', ?, 'Session overtaken by another worker; write fenced out', ?)
                    """,
                    (event_id, seat_id, owner_pid, now),
                )
                return False

    def get_event_by_idempotency_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM event_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM event_ledger WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_recent_events(
        self,
        limit: int = 50,
        seat_id: Optional[str] = None,
    ) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            if seat_id:
                cur = conn.execute(
                    "SELECT * FROM event_ledger WHERE seat_id = ? ORDER BY created_at DESC LIMIT ?",
                    (seat_id, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM event_ledger ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cur.fetchall()]

    def get_event_transitions(self, event_id: str) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM event_transitions WHERE event_id = ? ORDER BY transition_id ASC",
                (event_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def try_record_event_delivery(self, event_id: str, destination: str) -> bool:
        """Atomically claim delivery right for (event_id, destination).
        Returns True if this is the first delivery attempt, False if already delivered.
        """
        now = time.time()
        with self._get_connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO event_deliveries (event_id, destination, delivered_at) VALUES (?, ?, ?)",
                    (event_id, destination, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def is_event_delivered(self, event_id: str, destination: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM event_deliveries WHERE event_id = ? AND destination = ?",
                (event_id, destination),
            )
            return cur.fetchone() is not None

    def get_undelivered_tg_events(self, limit: int = 50) -> List[dict[str, Any]]:
        """Query terminal-state ('completed', 'failed') telegram events that have not been delivered yet.

        Performs a LEFT JOIN with event_deliveries to ensure:
        - Delivered events are filtered out.
        - Undelivered events are NEVER missed, even if >30 newer events are enqueued.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT e.*
                FROM event_ledger e
                LEFT JOIN event_deliveries d
                  ON e.event_id = d.event_id AND d.destination = 'telegram'
                WHERE e.source = 'telegram'
                  AND e.status IN ('completed', 'failed')
                  AND d.event_id IS NULL
                ORDER BY e.created_at ASC, e.rowid ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def save_binding_pin(
        self,
        pin_hash: str,
        seat_role: str,
        bot_id: int,
        bot_username: str,
        authorized_user_id: int | None,
        expires_at: float,
    ) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO binding_pins
                (pin_hash, seat_role, bot_id, bot_username, authorized_user_id, expires_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (pin_hash, seat_role, bot_id, bot_username, authorized_user_id, expires_at, now),
            )

    def get_binding_pin(self, pin_hash: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM binding_pins WHERE pin_hash = ?", (pin_hash,))
            row = cur.fetchone()
            return dict(row) if row else None

    def consume_binding_pin(self, pin_hash: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE binding_pins SET status = 'CONSUMED' WHERE pin_hash = ? AND status = 'PENDING'",
                (pin_hash,),
            )
            return cur.rowcount > 0

    def expire_binding_pin(self, pin_hash: str) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE binding_pins SET status = 'EXPIRED' WHERE pin_hash = ?", (pin_hash,))

    def publish_broker_event(self, event_type: str, seat_role: str | None, payload: dict[str, Any]) -> int:
        now = time.time()
        payload_str = json.dumps(payload, ensure_ascii=False)
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO broker_events (event_type, seat_role, payload, created_at) VALUES (?, ?, ?, ?)",
                (event_type, seat_role, payload_str, now),
            )
            return cur.lastrowid or 0

    def get_broker_events(self, after_event_id: int = 0, limit: int = 50) -> List[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM broker_events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
                (after_event_id, limit),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    d["payload"] = json.loads(d["payload"])
                except Exception:
                    pass
                rows.append(d)
            return rows
