"""Tests for Decoupled Echo-Proof DAG Loop and P0 Hardening"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.core import InboundMessage, TaskStatus, WorkerType
from pocketfleet.loop import DispatchLoop
from pocketfleet.state import StateStore
from pocketfleet.transport.base import BaseTransport


class MockTransport(BaseTransport):
    def __init__(self) -> None:
        self.inbound_queue: list[InboundMessage] = []
        self.sent_messages: list[any] = []
        self.should_fail: bool = False

    def poll_messages(self, timeout_sec: int = 10):
        res = list(self.inbound_queue)
        self.inbound_queue.clear()
        return res

    def send_message(self, message):
        if self.should_fail:
            return False
        self.sent_messages.append(message)
        return True


class TestDispatchLoop(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_state.sqlite3"
        self.state_store = StateStore(self.db_path)
        self.transport = MockTransport()
        self.loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.CLAUDE_CODE,
            state_store=self.state_store,
            allowed_chat_ids={111, 123},
        )

    def tearDown(self):
        self.loop.stop()
        self.tmp_dir.cleanup()

    def test_echo_proof_gating_ignores_bot(self):
        bot_msg = InboundMessage(
            message_id=901,
            chat_id=111,
            sender_id=999,
            sender_name="SpamBot",
            text="/fix do something",
            is_bot=True,
        )

        task = self.loop.handle_message(bot_msg)
        self.assertIsNone(task)
        self.assertEqual(len(self.transport.sent_messages), 0)

    def test_instant_status_response_when_idle(self):
        status_msg = InboundMessage(
            message_id=902,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/status",
            is_bot=False,
        )

        task = self.loop.handle_message(status_msg)
        self.assertIsNone(task)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertIn("PocketFleet Status: IDLE", self.transport.sent_messages[0].text)

    def test_instant_status_response_when_busy(self):
        # Simulate currently running task
        self.loop.current_running_task = {
            "task_id": "task-test-1",
            "prompt": "fix issue #42 JWT error",
            "worker": "claude_code",
            "start_time": time.time() - 15,
        }

        status_msg = InboundMessage(
            message_id=903,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/status",
            is_bot=False,
        )

        self.loop.handle_message(status_msg)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertIn("PocketFleet Status: BUSY", self.transport.sent_messages[0].text)
        self.assertIn("fix issue #42", self.transport.sent_messages[0].text)

    def test_deduplication_prevents_duplicate_run(self):
        # Mark message 904 as already finished in SQLite
        self.state_store.record_message_start(904, 111, "some prompt", "claude_code")
        self.state_store.record_message_finish(904, "COMPLETED", 0)

        repeat_msg = InboundMessage(
            message_id=904,
            chat_id=111,
            sender_id=123,
            sender_name="Alice",
            text="/claude fix repeat bug",
            is_bot=False,
        )

        task = self.loop.handle_message(repeat_msg)
        self.assertIsNone(task)
        self.assertEqual(self.loop.work_queue.qsize(), 0)

    def test_outbox_retry_on_network_failure(self):
        self.transport.should_fail = True

        # Send message when transport is down
        self.loop._send_immediate_or_outbox(
            chat_id=111,
            text="Crucial Final Report",
            reply_to_message_id=905,
        )

        # Immediate send failed -> Enqueued into Outbox
        pending = self.state_store.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["text"], "Crucial Final Report")

        # Network recovers
        self.transport.should_fail = False
        sent = self.loop.flush_outbox()
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertEqual(self.transport.sent_messages[0].text, "Crucial Final Report")

        # Second flush has 0 pending
        self.assertEqual(self.loop.flush_outbox(), 0)

    def test_group_chat_authorized_by_sender(self):
        # Security whitelist: sender is authorized commander even if group chat ID is negative
        self.loop.allowed_chat_ids = {123456789}
        self.loop.default_worker = WorkerType.SIMULATION
        group_msg = InboundMessage(
            message_id=906,
            chat_id=-100123456789,
            sender_id=123456789,
            sender_name="Commander",
            text="构建自动化支付结算网关",
            is_bot=False,
        )

        task = self.loop.handle_message(group_msg)
        self.assertIsNotNone(task)
        self.assertEqual(self.loop.work_queue.qsize(), 1)

    def test_warroom_role_binding_uses_real_executors(self):
        from pocketfleet.core import RoleAssignment
        self.loop.bots_config = {
            "antigravity": {"name": "Google Antigravity agent"},
            "codex": {"name": "OpenAI Codex worker"},
        }
        self.loop.set_role_assignment(RoleAssignment(lead="antigravity", builder="codex"))

        lead, builder, lead_name, builder_name = self.loop.get_role_bindings()
        self.assertIs(lead, self.loop.executors[WorkerType.ANTIGRAVITY])
        self.assertIs(builder, self.loop.executors[WorkerType.CODEX])
        self.assertEqual(lead_name, "Google Antigravity agent")
        self.assertEqual(builder_name, "OpenAI Codex worker")

        # Swap roles
        self.loop.set_role_assignment(RoleAssignment(lead="codex", builder="antigravity"))
        lead2, builder2, _, _ = self.loop.get_role_bindings()
        self.assertIs(lead2, self.loop.executors[WorkerType.CODEX])
        self.assertIs(builder2, self.loop.executors[WorkerType.ANTIGRAVITY])

    def test_unavailable_worker_never_falls_back_to_simulation(self):
        with mock.patch.object(self.loop.executors[WorkerType.CODEX], "is_available", return_value=False):
            selected = self.loop.select_worker(WorkerType.CODEX)
        self.assertIsNone(selected)

    def test_broker_pin_binding_and_fail_closed_on_unauthorized(self):
        """Verify DispatchLoop requires PIN for binding and fails closed on unpinned messages."""
        bound_events = []
        pending_loop = DispatchLoop(
            transport=self.transport,
            default_worker=WorkerType.SIMULATION,
            state_store=self.state_store,
            allowed_chat_ids=None,
            authorized_user_ids={777},
            workspace_cwd=self.tmp_dir.name,
            on_chat_bound=lambda cid, title: bound_events.append((cid, title)),
        )

        # 1. Plain ordinary message without PIN is blocked (Fail-Closed)
        unpinned_msg = InboundMessage(
            message_id=1000,
            chat_id=-100888999,
            sender_id=777,
            sender_name="NewCommander",
            text="/start",
            chat_type="supergroup",
            is_bot=False,
        )
        res = pending_loop.handle_message(unpinned_msg)
        self.assertIsNone(res)
        self.assertNotIn(-100888999, pending_loop.allowed_chat_ids)
        self.assertEqual(len(bound_events), 0)

        # 2. Ephemeral PIN binding via broker succeeds
        pin = pending_loop.broker.create_ephemeral_pin(
            bot_id=999,
            bot_username="SimulationBot",
            authorized_user_id=777,
        )
        bind_msg = InboundMessage(
            message_id=1001,
            chat_id=-100888999,
            sender_id=777,
            sender_name="NewCommander",
            text=f"/fleet_bind {pin}",
            chat_type="supergroup",
            chat_title="Fleet WarRoom",
            is_bot=False,
        )
        with mock.patch.object(pending_loop.broker, "_persist_chat_binding") as mock_persist:
            res_bind = pending_loop.handle_message(bind_msg)
            self.assertIsNone(res_bind)
            self.assertIn(-100888999, pending_loop.allowed_chat_ids)
            self.assertTrue(mock_persist.called)
            self.assertEqual(len(bound_events), 1)
            self.assertEqual(bound_events[0], (-100888999, "Fleet WarRoom"))

        # 3. Subsequent task from the bound group is accepted
        task_msg = InboundMessage(
            message_id=1002,
            chat_id=-100888999,
            sender_id=777,
            sender_name="NewCommander",
            text="/sim execute health check",
            chat_type="supergroup",
            is_bot=False,
        )
        task = pending_loop.handle_message(task_msg)
        self.assertIsNotNone(task)
        self.assertEqual(task.prompt, "execute health check")

        # 4. Message from another unauthorized chat is rejected (Fail-Closed)
        rogue_msg = InboundMessage(
            message_id=1003,
            chat_id=-100999999,
            sender_id=888,
            sender_name="Stranger",
            text="hello rogue",
            chat_type="supergroup",
            is_bot=False,
        )
        rejected = pending_loop.handle_message(rogue_msg)
        self.assertIsNone(rejected)

        pending_loop.stop()

    def test_statestore_get_set_meta(self) -> None:
        self.assertEqual(self.state_store.get_meta("custom_key", "default_val"), "default_val")
        self.state_store.set_meta("custom_key", "12345.67")
        self.assertEqual(self.state_store.get_meta("custom_key"), "12345.67")

    def test_format_telegram_envelope_with_roster(self) -> None:
        from pocketfleet.core import FleetSeatsConfig, SeatConfig
        seats_cfg = FleetSeatsConfig(
            seats={
                "lead": SeatConfig(
                    role="lead",
                    name="裁决者",
                    engine="antigravity",
                    bot_token_env="TOKEN_A",
                    bot_username="@AiSoulJudgeBot",
                    description="施工指挥",
                ),
                "builder": SeatConfig(
                    role="builder",
                    name="泥蛇",
                    engine="codex",
                    bot_token_env="TOKEN_B",
                    bot_username="@AiSoulMudSnakeBot",
                    description="主力程序员",
                ),
            }
        )
        self.loop.seats_config = seats_cfg
        env = self.loop._format_telegram_envelope(
            prompt="帮我构建网关",
            target_worker=WorkerType.CODEX,
            sender_name="指挥官",
            sender_id=777,
            chat_title="AI星舰战队·战役室",
        )
        self.assertIn("[来自TG多AI协作];[人类用户:指挥官];参与者:[@AiSoulJudgeBot;@AiSoulMudSnakeBot]", env)
        self.assertIn("回复格式要求:以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人）。", env)
        self.assertIn("帮我构建网关", env)

    def test_mention_routing_to_designated_worker(self) -> None:
        from pocketfleet.core import FleetSeatsConfig, SeatConfig
        seats_cfg = FleetSeatsConfig(
            seats={
                "lead": SeatConfig(
                    role="lead",
                    name="裁决者",
                    engine="antigravity",
                    bot_token_env="TOKEN_A",
                    bot_username="@AiSoulJudgeBot",
                    description="施工指挥",
                ),
                "builder": SeatConfig(
                    role="builder",
                    name="泥蛇",
                    engine="codex",
                    bot_token_env="TOKEN_B",
                    bot_username="@AiSoulMudSnakeBot",
                    description="主力程序员",
                ),
            }
        )
        self.loop.seats_config = seats_cfg
        # Default worker is claude_code, but user mentions @AiSoulMudSnakeBot
        self.loop.default_worker = WorkerType.CLAUDE_CODE
        msg = InboundMessage(
            message_id=1050,
            chat_id=111,
            sender_id=123,
            sender_name="Commander",
            text="@AiSoulMudSnakeBot 帮我写单元测试",
            is_bot=False,
        )
        task = self.loop.handle_message(msg)
        self.assertIsNotNone(task)
        self.assertEqual(task.worker, WorkerType.CODEX)
        self.assertEqual(task.raw_prompt, "帮我写单元测试")
        self.assertEqual(task.display_prompt, "帮我写单元测试")
        self.assertIn("[来自TG多AI协作];[人类用户:Commander];参与者:[@AiSoulJudgeBot;@AiSoulMudSnakeBot]", task.prompt)
        self.assertIn("回复格式要求:以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人）。", task.prompt)
        self.assertIn("帮我写单元测试", task.prompt)

    def test_fleet_bot_to_bot_message_allowed(self) -> None:
        from pocketfleet.core import FleetSeatsConfig, SeatConfig
        self.loop.seats_config = FleetSeatsConfig(
            seats={
                "lead": SeatConfig(
                    role="lead",
                    name="裁决者",
                    engine="antigravity",
                    bot_token_env="TOKEN_A",
                    bot_username="@AiSoulJudgeBot",
                    description="规划席",
                ),
                "builder": SeatConfig(
                    role="builder",
                    name="泥蛇",
                    engine="codex",
                    bot_token_env="TOKEN_B",
                    bot_username="@AiSoulMudSnakeBot",
                    description="执行席",
                ),
            }
        )
        # Message from internal fleet bot @AiSoulJudgeBot to @AiSoulMudSnakeBot
        bot_msg = InboundMessage(
            message_id=2001,
            chat_id=111,
            sender_id=999,
            sender_name="AiSoulJudgeBot",
            text="@AiSoulMudSnakeBot [Telegram][mailto:@AiSoulMudSnakeBot] 请开工",
            is_bot=True,
        )
        task = self.loop.handle_message(bot_msg)
        self.assertIsNotNone(task)
        self.assertEqual(task.worker, WorkerType.CODEX)

        # Message from external unknown bot is dropped
        ext_bot_msg = InboundMessage(
            message_id=2002,
            chat_id=111,
            sender_id=888,
            sender_name="UnknownSpamBot",
            text="@AiSoulMudSnakeBot spam",
            is_bot=True,
        )
        ext_task = self.loop.handle_message(ext_bot_msg)
        self.assertIsNone(ext_task)

    def test_chat_seat_routing_truthful_probe(self):
        """Verify chat seat routes truthfully: alerts when disconnected, never sends fake ACK."""
        chat_msg = InboundMessage(
            message_id=3001,
            chat_id=111,
            sender_id=12345,
            sender_name="Commander",
            text="@AiSoulSettlementBot 帮我对账",
            is_bot=False,
        )

        with mock.patch("pocketfleet.bridge_server.is_web_bridge_connected", return_value=(False, "Port 18765 离线")):
            res = self.loop.handle_message(chat_msg)
            self.assertIsNone(res)
            # Must send honest warning via primary transport
            self.assertEqual(len(self.transport.sent_messages), 1)
            sent = self.transport.sent_messages[0]
            self.assertIn("网桥提醒", sent.text)
            self.assertIn("未就绪", sent.text)
            self.assertNotIn("已递交至 ChatGPT 网页端网桥处理", sent.text)


    def test_defang_telegram_mentions(self):
        from pocketfleet.loop import defang_telegram_mentions
        sample = "[Telegram]re:@AiSoulJudgeBot[NoReply] 任务已完成，请对账 @AiSoulSettlementBot 抄送 @RandomBot"
        defanged = defang_telegram_mentions(sample)
        self.assertNotIn("@AiSoulJudgeBot", defanged)
        self.assertNotIn("@AiSoulSettlementBot", defanged)
        self.assertNotIn("@RandomBot", defanged)
        self.assertIn("裁决者 (免回)", defanged)
        self.assertIn("结算主机 (免回)", defanged)
        self.assertIn("[RandomBot · 免回]", defanged)

    def test_fleet_meeting_kickoff_and_briefing(self):
        meet_msg = InboundMessage(
            message_id=4001,
            chat_id=111,
            sender_id=12345,
            sender_name="Commander",
            text="/meet 招股书退市风险评估",
            is_bot=False,
        )
        with mock.patch("pocketfleet.bridge_server.enqueue_codeai_message") as mock_enqueue, \
             mock.patch("pocketfleet.bridge_server.is_web_bridge_connected", return_value=(True, "OK")):
            res = self.loop.handle_message(meet_msg)
            self.assertIsNone(res)
            # 1. Announcement sent to TG
            self.assertEqual(len(self.transport.sent_messages), 1)
            sent = self.transport.sent_messages[0]
            self.assertIn("【AI星舰联席会议已召开】", sent.text)
            self.assertIn("招股书退市风险评估", sent.text)
            # 2. Briefing enqueued for host
            mock_enqueue.assert_called_once()
            call_kwargs = mock_enqueue.call_args[1]
            self.assertIn("招股书退市风险评估", call_kwargs["content"])
            self.assertEqual(call_kwargs["channel"], "duty-wake")
            # 3. Active meeting registered
            self.assertIsNotNone(self.loop._active_meeting)
            self.assertTrue(self.loop._active_meeting["active"])
            self.assertEqual(self.loop._active_meeting["topic"], "招股书退市风险评估")

    def test_fleet_meeting_miniapp_json(self):
        miniapp_msg = InboundMessage(
            message_id=4002,
            chat_id=111,
            sender_id=12345,
            sender_name="Commander",
            text='{"action": "meet", "topic": "自研防回声网桥落地", "host": "@AiSoulSettlementBot", "watchdog_minutes": 3}',
            is_bot=False,
        )
        with mock.patch("pocketfleet.bridge_server.enqueue_codeai_message") as mock_enqueue, \
             mock.patch("pocketfleet.bridge_server.is_web_bridge_connected", return_value=(True, "OK")):
            self.loop.handle_message(miniapp_msg)
            self.assertIsNotNone(self.loop._active_meeting)
            self.assertTrue(self.loop._active_meeting["active"])
            self.assertEqual(self.loop._active_meeting["topic"], "自研防回声网桥落地")
            self.assertEqual(self.loop._active_meeting["watchdog_sec"], 180)

    def test_fleet_meeting_watchdog_and_meetover(self):
        meet_msg = InboundMessage(
            message_id=4003,
            chat_id=111,
            sender_id=12345,
            sender_name="Commander",
            text="/meet 性能调优",
            is_bot=False,
        )
        with mock.patch("pocketfleet.bridge_server.enqueue_codeai_message"), \
             mock.patch("pocketfleet.bridge_server.is_web_bridge_connected", return_value=(True, "OK")):
            self.loop.handle_message(meet_msg)
            self.assertTrue(self.loop._active_meeting["active"])

            # Fast forward time to trigger watchdog
            self.loop._last_meeting_activity_ts = time.time() - 360
            self.transport.sent_messages.clear()
            self.loop._check_meeting_watchdog()
            self.assertEqual(len(self.transport.sent_messages), 1)
            self.assertIn("进度推进看门狗", self.transport.sent_messages[0].text)

            # Send /meetover to close meeting
            over_msg = InboundMessage(
                message_id=4004,
                chat_id=111,
                sender_id=12345,
                sender_name="Commander",
                text="/meetover",
                is_bot=False,
            )
            self.transport.sent_messages.clear()
            self.loop.handle_message(over_msg)
            self.assertFalse(self.loop._active_meeting["active"])
            self.assertEqual(len(self.transport.sent_messages), 1)
            self.assertIn("圆满闭幕", self.transport.sent_messages[0].text)


if __name__ == "__main__":
    unittest.main()


