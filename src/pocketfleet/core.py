"""PocketFleet Core Contracts and Data Models

Zero external dependency dataclasses defining tasks, messages, and state machines.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class WorkerType(str, Enum):
    CLAUDE_CODE = "claude_code"
    AIDER = "aider"
    AUTO = "auto"


@dataclass
class InboundMessage:
    message_id: int
    chat_id: int
    sender_id: int
    sender_name: str
    text: str
    is_bot: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class OutboundMessage:
    chat_id: int
    text: str
    reply_to_message_id: int | None = None
    parse_mode: str = "Markdown"


@dataclass
class Task:
    prompt: str
    worker: WorkerType = WorkerType.AUTO
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: TaskStatus = TaskStatus.PENDING
    chat_id: int = 0
    inbound_message_id: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result_text: str = ""
    error_message: str = ""
    exit_code: int | None = None

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()

    def mark_completed(self, result: str, exit_code: int = 0) -> None:
        self.status = TaskStatus.COMPLETED
        self.result_text = result
        self.exit_code = exit_code
        self.completed_at = time.time()

    def mark_failed(self, error: str, exit_code: int = 1) -> None:
        self.status = TaskStatus.FAILED
        self.error_message = error
        self.exit_code = exit_code
        self.completed_at = time.time()
