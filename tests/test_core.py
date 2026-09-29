"""Tests for PocketFleet Core Models"""
from __future__ import annotations

import unittest

from pocketfleet.core import InboundMessage, OutboundMessage, Task, TaskStatus, WorkerType


class TestCore(unittest.TestCase):
    def test_task_lifecycle(self):
        task = Task(prompt="Refactor auth", worker=WorkerType.CLAUDE_CODE)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.completed_at)

        task.mark_running()
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertIsNotNone(task.started_at)

        task.mark_completed("Diff applied successfully", exit_code=0)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result_text, "Diff applied successfully")
        self.assertEqual(task.exit_code, 0)
        self.assertIsNotNone(task.completed_at)

    def test_task_failed(self):
        task = Task(prompt="Fix build")
        task.mark_failed("SyntaxError on line 42", exit_code=1)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error_message, "SyntaxError on line 42")
        self.assertEqual(task.exit_code, 1)

    def test_messages(self):
        inbound = InboundMessage(
            message_id=101,
            chat_id=202,
            sender_id=303,
            sender_name="Alice",
            text="/fix something",
            is_bot=False,
        )
        self.assertFalse(inbound.is_bot)
        self.assertEqual(inbound.sender_name, "Alice")

        outbound = OutboundMessage(
            chat_id=202,
            text="Task running",
            reply_to_message_id=101,
        )
        self.assertEqual(outbound.reply_to_message_id, 101)


if __name__ == "__main__":
    unittest.main()
