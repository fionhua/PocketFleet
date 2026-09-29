"""Tests for PocketFleet Core Models"""
from __future__ import annotations

from pocketfleet.core import InboundMessage, OutboundMessage, Task, TaskStatus, WorkerType


def test_task_lifecycle():
    task = Task(prompt="Refactor auth", worker=WorkerType.CLAUDE_CODE)
    assert task.status == TaskStatus.PENDING
    assert task.started_at is None
    assert task.completed_at is None

    task.mark_running()
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None

    task.mark_completed("Diff applied successfully", exit_code=0)
    assert task.status == TaskStatus.COMPLETED
    assert task.result_text == "Diff applied successfully"
    assert task.exit_code == 0
    assert task.completed_at is not None


def test_task_failed():
    task = Task(prompt="Fix build")
    task.mark_failed("SyntaxError on line 42", exit_code=1)
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "SyntaxError on line 42"
    assert task.exit_code == 1


def test_messages():
    inbound = InboundMessage(
        message_id=101,
        chat_id=202,
        sender_id=303,
        sender_name="Alice",
        text="/fix something",
        is_bot=False,
    )
    assert inbound.is_bot is False
    assert inbound.sender_name == "Alice"

    outbound = OutboundMessage(
        chat_id=202,
        text="Task running",
        reply_to_message_id=101,
    )
    assert outbound.reply_to_message_id == 101
