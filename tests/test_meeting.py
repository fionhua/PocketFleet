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
        announcement = self.primary_transport.sent_messages[0]
        self.assertIn("重构 Telegram 研讨流", announcement.text)
        self.assertIn("5 分钟", announcement.text)

    def test_stale_inbound_message_dropped_by_original_age(self):
        """Messages with original timestamp older than 300s must be dropped immediately."""
        import time
        now = time.time()
        stale_msg = InboundMessage(
            message_id=10,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/status",
            timestamp=now - 350.0,
        )
        task = self.loop.handle_message(stale_msg)
        self.assertIsNone(task)
        # Verify no response was sent
        self.assertEqual(len(self.primary_transport.sent_messages), 0)

    def test_meeting_concluded_watermark_drops_stale_residues(self):
        """After /meetover, in-flight residues with timestamps <= conclusion are dropped."""
        import time
        now = time.time()
        start_msg = InboundMessage(
            message_id=1,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/meet 验证水位线闭会",
            timestamp=now - 50.0,
        )
        self.loop.handle_message(start_msg)

        # Conclude meeting
        close_msg = InboundMessage(
            message_id=2,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="/meetover 闭会",
            timestamp=now - 20.0,
        )
        self.loop.handle_message(close_msg)
        self.assertTrue(self.loop.meeting_concluded_at > 0)

        # An in-flight reply that arrives late, but originated before meeting conclusion
        stale_reply = InboundMessage(
            message_id=3,
            chat_id=111,
            sender_id=456,
            sender_name="AiSoulSettlementBot",
            text="[Telegram]re:@AiSoulJudgeBot;[waitReply] 这是会议中途派生出的滞后回复",
            is_bot=True,
            timestamp=self.loop.meeting_concluded_at - 5.0,
        )
        task = self.loop.handle_message(stale_reply)
        self.assertIsNone(task)

    def test_forced_seat_chat_cleared_when_target_is_lead_or_builder(self):
        """When settlement host receives a message with an envelope directing to Judge, it must NOT be routed to chat queue."""
        import time
        from pocketfleet.bridge_server import _CODEAI_QUEUE, _CODEAI_LOCK
        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()

        msg = InboundMessage(
            message_id=20,
            chat_id=111,
            sender_id=123,
            sender_name="AiSoulSettlementBot",
            text="[Telegram]re:@AiSoulJudgeBot;[waitReply] 请裁决者复核防回声设计",
            is_bot=True,
            timestamp=time.time() - 5.0,
        )

        task = self.loop.handle_message(msg, forced_seat="chat")
        with _CODEAI_LOCK:
            chat_items = [it for it in _CODEAI_QUEUE if it.get("target") == "chat"]
            self.assertEqual(len(chat_items), 0, "Target @AiSoulJudgeBot was erroneously put into web chat queue!")

    def test_pop_codeai_message_principal_isolation(self):
        """Unauthorized clients like Doubao must not pull tasks intended for ChatGPT/Settlement."""
        from pocketfleet.bridge_server import (
            enqueue_codeai_message,
            pop_codeai_message,
            _CODEAI_QUEUE,
            _CODEAI_LOCK,
        )
        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()

        enqueue_codeai_message(
            content="结算主机专属任务公文",
            filename="Telegram_meet_test.txt",
            target="chat",
        )

        # Doubao tries to pull
        doubao_item = pop_codeai_message(client_principal="doubao-heart-web")
        self.assertIsNone(doubao_item, "Doubao should not be able to pull tasks targeted to chat/settlement!")

        # ChatGPT tries to pull
        chatgpt_item = pop_codeai_message(client_principal="folded-host-chatgpt-web")
        self.assertIsNotNone(chatgpt_item, "ChatGPT should be authorized to pull tasks targeted to chat!")
        self.assertIn("结算主机专属任务公文", chatgpt_item["content"])

    def test_cancel_stale_meeting_messages(self):
        """cancel_stale_meeting_messages purges queued and in-flight messages matching conclusion watermark."""
        import time
        from pocketfleet.bridge_server import (
            enqueue_codeai_message,
            cancel_stale_meeting_messages,
            _CODEAI_QUEUE,
            _CODEAI_LOCK,
        )
        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()

        t0 = time.time()
        enqueue_codeai_message("Early meeting task", original_timestamp=t0 - 30.0)
        enqueue_codeai_message("New non-meeting task", original_timestamp=t0 + 10.0)

        purged = cancel_stale_meeting_messages(concluded_before_ts=t0)
        self.assertEqual(purged, 1)

        with _CODEAI_LOCK:
            self.assertEqual(len(_CODEAI_QUEUE), 1)
            self.assertEqual(_CODEAI_QUEUE[0]["content"], "New non-meeting task")

    def test_pure_gateway_autonomous_dispatch_without_subprocess(self):
        """Autonomous seat dispatch routes to bridge_server and never puts into local work_queue."""
        import time
        from unittest.mock import patch
        from pocketfleet.core import FleetSeatsConfig, SeatConfig
        from pocketfleet.bridge_server import _CODEAI_QUEUE, _CODEAI_LOCK

        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()

        seats_cfg = FleetSeatsConfig(
            seats={
                "builder": SeatConfig(
                    role="builder",
                    name="泥蛇",
                    engine="codex",
                    bot_username="@AiSoulMudSnakeBot",
                    bot_token="test_tok",
                ),
            }
        )
        self.loop.seats_config = seats_cfg

        msg = InboundMessage(
            message_id=888,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="[Telegram]re:@AiSoulMudSnakeBot 请泥蛇给出算法定桩方案",
            is_bot=False,
            timestamp=time.time() - 2.0,
        )

        # Even if local codex executor is unavailable/absent, Pure Gateway succeeds
        with patch.object(self.loop.executors[WorkerType.CODEX], "is_available", return_value=False):
            task = self.loop.handle_message(msg)

        self.assertIsNotNone(task)
        # CRITICAL PURE GATEWAY INVARIANTS:
        # 1. Local work_queue must be empty (Zero local subprocess forking)
        self.assertEqual(self.loop.work_queue.qsize(), 0)
        # 2. Bridge queue has the message dispatched to target builder
        with _CODEAI_LOCK:
            self.assertEqual(len(_CODEAI_QUEUE), 1)
            self.assertEqual(_CODEAI_QUEUE[0]["target"], "builder")
            self.assertIn("请泥蛇给出算法定桩方案", _CODEAI_QUEUE[0]["content"])


if __name__ == "__main__":
    unittest.main()
