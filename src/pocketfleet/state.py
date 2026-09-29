"""PocketFleet Persistent State & Outbox Database

Zero-dependency SQLite store for:
1. Update watermarks (last_update_id) - prevents duplicate processing on restart
2. Processed messages & deduplication ledger
3. Outbox retry queue - guarantees delivery even across network drops
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".pocketfleet" / "state.sqlite3"


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
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
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
                    message_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
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
                """
            )

    # --- Watermark Management ---

    def get_watermark(self) -> Optional[int]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key = 'last_update_id'")
            row = cur.fetchone()
            return int(row["value"]) if row else None

    def set_watermark(self, update_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_update_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(update_id),),
            )

    # --- Deduplication & Message State ---

    def is_message_processed(self, message_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
            return cur.fetchone() is not None

    def record_message_start(self, message_id: int, chat_id: int, prompt: str, worker: str) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO processed_messages (message_id, chat_id, prompt, worker, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET status = 'RUNNING', updated_at = excluded.updated_at
                """,
                (message_id, chat_id, prompt, worker, now, now),
            )

    def record_message_finish(self, message_id: int, status: str, exit_code: int) -> None:
        now = time.time()
        with self._get_connection() as conn:
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
