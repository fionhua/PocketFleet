"""Unit tests for Telegram Meeting Engine and Anti-Echo Defanging logic."""
import json
import tempfile
import unittest
from pathlib import Path

from pocketfleet.core import InboundMessage, WorkerType
from pocketfleet.loop import DispatchLoop, defang_telegram_mentions
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


class TestMeetingAndEcho(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_state.sqlite3"
        self.state_store = StateStore(self.db_path)
        self.primary_transport = MockTransport()
        self.judge_transport = MockTransport()
        self.mudsnake_transport = MockTransport()

        self.loop = DispatchLoop(
            transport=self.primary_transport,
            default_worker=WorkerType.CLAUDE_CODE,
            state_store=self.state_store,
            allowed_chat_ids={111},
        )
        self.loop.secondary_transports = {
            "judge": self.judge_transport,
            "mudsnake": self.mudsnake_transport,
        }

    def tearDown(self):
        self.loop.stop()
        self.tmp_dir.cleanup()

    def test_defang_telegram_mentions(self):
        text = "Hello @AiSoulJudgeBot and @AiSoulMudSnakeBot, please verify!"
        defanged = defang_telegram_mentions(text)
        self.assertNotIn("@AiSoulJudgeBot", defanged)
        self.assertNotIn("@AiSoulMudSnakeBot", defanged)
        self.assertIn("裁决者 (免回)", defanged)
        self.assertIn("泥蛇 (免回)", defanged)

    def test_meet_command_initiates_meeting(self):
        msg = InboundMessage(
            message_id=1,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/meet 验证 PocketFleet 零回声协议",
        )
        task = self.loop.handle_message(msg)
        # /meet handles routing directly via session_hub/web_bridge and returns None
        self.assertIsNone(task)
        # Check active meeting state in loop
        self.assertIsNotNone(self.loop._active_meeting)
        self.assertIn("验证 PocketFleet 零回声协议", self.loop._active_meeting["topic"])
        # Check that announcement was sent to Telegram
        self.assertTrue(len(self.primary_transport.sent_messages) > 0)
        announcement = self.primary_transport.sent_messages[0]
        self.assertIn("AI 星舰联席会议已召集", announcement.text)
        self.assertIn("验证 PocketFleet 零回声协议", announcement.text)

    def test_meetover_command(self):
        # First initiate a meeting
        start_msg = InboundMessage(
            message_id=1,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/meet 架构重构研讨",
        )
        self.loop.handle_message(start_msg)
        self.assertIsNotNone(self.loop._active_meeting)

        # Now close it with /meetover
        msg = InboundMessage(
            message_id=2,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/meetover 本次研讨会圆满结束，达成共识",
        )
        task = self.loop.handle_message(msg)
        self.assertIsNone(task)
        self.assertFalse(self.loop._active_meeting.get("active", True))
        # Should post meeting concluded message
        self.assertTrue(len(self.primary_transport.sent_messages) > 1)
        closure_msg = self.primary_transport.sent_messages[-1]
        self.assertIn("AI星舰联席会议 · 结案闭幕", closure_msg.text)
        self.assertIn("看门狗已安全撤除", closure_msg.text)

    def test_miniapp_json_submission(self):
        miniapp_data = {
            "action": "start_meeting",
            "topic": "重构 Telegram 研讨流",
            "host": "lead",
            "participants": ["lead", "judge", "mudsnake"],
            "watchdog_min": 5,
        }
        msg = InboundMessage(
            message_id=3,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text=json.dumps(miniapp_data),
        )
        task = self.loop.handle_message(msg)
        self.assertIsNone(task)
        self.assertIsNotNone(self.loop._active_meeting)
        self.assertEqual(self.loop._active_meeting["topic"], "重构 Telegram 研讨流")
        self.assertEqual(self.loop._active_meeting["host"], "lead")
        # Verify announcement
        announcement = self.primary_transport.sent_messages[-1]
        self.assertIn("重构 Telegram 研讨流", announcement.text)
        self.assertIn("5 分钟", announcement.text)


if __name__ == "__main__":
    unittest.main()
