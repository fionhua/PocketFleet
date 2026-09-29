"""Tests for Echo-Proof Single-direction DAG Loop"""
from __future__ import annotations

from unittest import mock

from pocketfleet.core import InboundMessage, TaskStatus, WorkerType
from pocketfleet.loop import DispatchLoop
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


def test_echo_proof_gating_ignores_bot():
    transport = MockTransport()
    loop = DispatchLoop(transport=transport)

    bot_msg = InboundMessage(
        message_id=901,
        chat_id=111,
        sender_id=999,
        sender_name="SpamBot",
        text="/fix do something",
        is_bot=True,  # Bot flag
    )

    task = loop.handle_message(bot_msg)
    assert task is None
    assert len(transport.sent_messages) == 0  # Zero reply sent, echo prevented!


def test_loop_executes_human_task_end_to_end():
    transport = MockTransport()
    loop = DispatchLoop(transport=transport, default_worker=WorkerType.CLAUDE_CODE)

    # Mock claude executor
    mock_executor = mock.Mock()
    mock_executor.is_available.return_value = True
    mock_executor.name = "claude_code"
    mock_executor.execute.return_value = (0, "All tests passing!", "")
    loop.executors[WorkerType.CLAUDE_CODE] = mock_executor

    human_msg = InboundMessage(
        message_id=902,
        chat_id=111,
        sender_id=12345,
        sender_name="Alice",
        text="/claude fix authentication bug",
        is_bot=False,
    )

    task = loop.handle_message(human_msg)
    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    assert task.exit_code == 0
    assert task.result_text == "All tests passing!"

    # Sent messages: 1. Task Started, 2. Task Completed
    assert len(transport.sent_messages) == 2
    assert "Task Started" in transport.sent_messages[0].text
    assert "Task Completed" in transport.sent_messages[1].text
    assert "All tests passing!" in transport.sent_messages[1].text
