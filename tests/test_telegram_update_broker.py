"""Unit and Integration Tests for TelegramUpdateBroker (PF-03R7).

Validates:
1. Sender Authorization: from.id == authorized_user_id; anonymous admins and sender_chat strictly rejected.
2. Group Chat Type: group / supergroup accepted; private chat strictly rejected.
3. Ephemeral Binding PIN: single-use, TTL expiration, seat-bound, zero-log.
4. HTTP 409 Conflict: Fail-Loud without killing external processes or dropping updates.
5. Cross-process IPC ledger: broker_events table publication and consumption.
"""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.broker import (
    TelegramUpdateBroker,
    BrokerConflictError,
    hash_pin,
)
from pocketfleet.core import InboundMessage
from pocketfleet.state import StateStore


@pytest.fixture
def temp_store(tmp_path: Path):
    db_file = tmp_path / "test_state.sqlite3"
    return StateStore(db_path=db_file)


@pytest.fixture
def broker(temp_store: StateStore):
    return TelegramUpdateBroker(
        bot_token="123456:TEST_TOKEN",
        seat_role="lead",
        state_store=temp_store,
        authorized_user_ids=[10001],
    )


def test_ephemeral_pin_generation_and_single_use(broker: TelegramUpdateBroker, temp_store: StateStore):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001, ttl_seconds=60)
    assert len(pin) <= 64
    assert len(pin) >= 32

    # Verify pin is hashed in SQLite
    p_hash = hash_pin("lead", pin)
    record = temp_store.get_binding_pin(p_hash)
    assert record is not None
    assert record["status"] == "PENDING"
    assert record["bot_id"] == 999
    assert record["authorized_user_id"] == 10001

    # First consumption succeeds
    ok, reason, rec = broker.verify_and_consume_pin(plain_pin=pin, sender_id=10001, is_anonymous=False)
    assert ok is True
    assert "验证通过" in reason

    # Second consumption fails (Single-use guarantee)
    ok2, reason2, rec2 = broker.verify_and_consume_pin(plain_pin=pin, sender_id=10001, is_anonymous=False)
    assert ok2 is False
    assert "已被使用" in reason2


def test_sender_authorization_rejects_unauthorized_user(broker: TelegramUpdateBroker):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001)

    # Wrong user attempts to use valid PIN
    ok, reason, _ = broker.verify_and_consume_pin(plain_pin=pin, sender_id=99999, is_anonymous=False)
    assert ok is False
    assert "非授权指挥官" in reason


def test_sender_authorization_rejects_anonymous_admin(broker: TelegramUpdateBroker):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001)

    # Anonymous admin flag True
    ok, reason, _ = broker.verify_and_consume_pin(plain_pin=pin, sender_id=10001, is_anonymous=True)
    assert ok is False
    assert "匿名管理员" in reason

    # Channel sender_chat_id provided
    ok2, reason2, _ = broker.verify_and_consume_pin(plain_pin=pin, sender_id=10001, is_anonymous=False, sender_chat_id=-100123)
    assert ok2 is False
    assert "匿名管理员或频道身份" in reason2


def test_pin_expiration(broker: TelegramUpdateBroker):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001, ttl_seconds=-1)
    ok, reason, _ = broker.verify_and_consume_pin(plain_pin=pin, sender_id=10001, is_anonymous=False)
    assert ok is False
    assert "已过期" in reason


def test_process_inbound_message_rejects_private_chat(broker: TelegramUpdateBroker):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001)

    msg = InboundMessage(
        message_id=10,
        chat_id=10001,
        sender_id=10001,
        sender_name="Commander",
        text=f"/fleet_bind {pin}",
        chat_type="private",
    )

    with patch.object(broker, "_send_text") as mock_send:
        handled, is_bound = broker.process_inbound_message(msg)
        assert handled is True
        assert is_bound is False
        mock_send.assert_called_once()
        assert "私聊无法绑定" in mock_send.call_args[0][1]


def test_process_inbound_message_successful_group_binding(broker: TelegramUpdateBroker, temp_store: StateStore):
    pin = broker.create_ephemeral_pin(bot_id=999, bot_username="TestBot", authorized_user_id=10001)

    msg = InboundMessage(
        message_id=20,
        chat_id=-100987654321,
        sender_id=10001,
        sender_name="Commander",
        text=f"/fleet_bind@{broker.seat_role} {pin}",
        chat_type="supergroup",
        chat_title="Fleet WarRoom Alpha",
    )

    with patch.object(broker, "_send_text") as mock_send, patch.object(broker, "_persist_chat_binding") as mock_persist:
        handled, is_bound = broker.process_inbound_message(msg)
        assert handled is True
        assert is_bound is True
        mock_persist.assert_called_once_with(-100987654321, "Fleet WarRoom Alpha", commander_id=10001)
        mock_send.assert_called_once()

        # Check honest arrival message: NOT claiming engine online
        sent_text = mock_send.call_args[0][1]
        assert "通信链路已连接" in sent_text
        assert "执行引擎将在后台服务启动后正式上线" in sent_text
        assert "在线就绪" not in sent_text

    # Verify cross-process event published to SQLite ledger
    events = temp_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "CHAT_BOUND"
    assert events[0]["payload"]["chat_id"] == -100987654321
    assert events[0]["payload"]["chat_title"] == "Fleet WarRoom Alpha"


def test_fail_loud_on_http_409_conflict(broker: TelegramUpdateBroker):
    import urllib.error
    # Real HTTP 409 error raised by urllib
    err = urllib.error.HTTPError(
        url="https://api.telegram.org/bot/getUpdates",
        code=409,
        msg="Conflict",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"ok":false,"description":"Conflict: terminated by other getUpdates request"}'),
    )

    with patch("urllib.request.urlopen", side_effect=err):
        # Directly call broker's real poll_once method - MUST fail loud and publish CONFLICT_409
        with pytest.raises(BrokerConflictError) as exc_info:
            broker.poll_once()
        assert "409 Conflict" in str(exc_info.value)

    events = broker.state_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "CONFLICT_409"
    assert "未知外部消费者" in events[0]["payload"]["error"]


def test_true_inbound_e2e_pipeline(tmp_path: Path):
    """True Inbound E2E Pipeline:
    Raw TG getUpdates -> Broker parsing -> Sender Auth & PIN verification ->
    SQLite persistence -> Cross-process CHAT_BOUND event -> Replay protection.
    """
    import json
    import io

    db_file = tmp_path / "e2e_state.sqlite3"
    state_store = StateStore(db_path=db_file)
    cfg_dir = tmp_path / "fleet_workspace"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    broker = TelegramUpdateBroker(
        bot_token="987654:TEST_E2E_TOKEN",
        seat_role="lead",
        state_store=state_store,
        authorized_user_ids=[88888],  # Only Commander 88888 authorized
        repo_root=cfg_dir,
    )

    # 1. Commander generates PIN
    pin = broker.create_ephemeral_pin(
        bot_id=123456,
        bot_username="E2EBot",
        authorized_user_id=88888,
        ttl_seconds=300,
    )

    # 2. Raw Telegram update: Stranger (ID: 99999) attempts to bind with the PIN
    stranger_update = {
        "ok": True,
        "result": [
            {
                "update_id": 10001,
                "message": {
                    "message_id": 1,
                    "date": 1720000000,
                    "chat": {"id": -100111222, "title": "Rogue Group", "type": "supergroup"},
                    "from": {"id": 99999, "is_bot": False, "username": "StrangerHacker"},
                    "text": f"/fleet_bind {pin}",
                },
            }
        ],
    }

    def make_response(data: dict):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        resp.read.return_value = json.dumps(data).encode("utf-8")
        return resp

    # Run poll_once with stranger update -> Broker MUST reject
    with patch("urllib.request.urlopen", return_value=make_response(stranger_update)), \
         patch.object(broker, "_send_text") as mock_send:
        new_upd, is_bound = broker.poll_once()
        assert is_bound is False
        assert new_upd == 10001
        # Sent rejection message to group
        assert mock_send.called
        assert "非授权指挥官" in mock_send.call_args[0][1]

    # Verify BIND_FAILED event published in SQLite
    events = state_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "BIND_FAILED"
    assert events[0]["payload"]["sender_id"] == 99999

    # Verify PIN is still PENDING (not consumed by stranger)
    pin_rec = state_store.get_binding_pin(hash_pin("lead", pin))
    assert pin_rec is not None
    assert pin_rec["status"] == "PENDING"

    # 3. Raw Telegram update: Authorized Commander (ID: 88888) sends valid binding in WarRoom
    commander_update = {
        "ok": True,
        "result": [
            {
                "update_id": 10002,
                "message": {
                    "message_id": 2,
                    "date": 1720000005,
                    "chat": {"id": -100777888, "title": "Fleet Command Center", "type": "supergroup"},
                    "from": {"id": 88888, "is_bot": False, "username": "FleetCommander"},
                    "text": f"/fleet_bind@lead {pin}",
                },
            }
        ],
    }

    with patch("urllib.request.urlopen", return_value=make_response(commander_update)), \
         patch.object(broker, "_send_text") as mock_send:
        new_upd, is_bound = broker.poll_once(last_update_id=10001)
        assert is_bound is True
        assert new_upd == 10002
        assert mock_send.called
        assert "通信链路已连接" in mock_send.call_args[0][1]

    # Verify CHAT_BOUND event published in SQLite
    events_after = state_store.get_broker_events(after_event_id=events[0]["event_id"])
    assert len(events_after) == 1
    assert events_after[0]["event_type"] == "CHAT_BOUND"
    assert events_after[0]["payload"]["chat_id"] == -100777888
    assert events_after[0]["payload"]["chat_title"] == "Fleet Command Center"

    # Verify PIN is now CONSUMED in SQLite
    pin_rec = state_store.get_binding_pin(hash_pin("lead", pin))
    assert pin_rec["status"] == "CONSUMED"

    # 4. Replay Attack: Someone sends the same PIN again
    replay_update = {
        "ok": True,
        "result": [
            {
                "update_id": 10003,
                "message": {
                    "message_id": 3,
                    "date": 1720000010,
                    "chat": {"id": -100777888, "title": "Fleet Command Center", "type": "supergroup"},
                    "from": {"id": 88888, "is_bot": False, "username": "FleetCommander"},
                    "text": f"/fleet_bind {pin}",
                },
            }
        ],
    }

    with patch("urllib.request.urlopen", return_value=make_response(replay_update)), \
         patch.object(broker, "_send_text") as mock_send:
        new_upd, is_bound = broker.poll_once(last_update_id=10002)
        assert is_bound is False
        assert mock_send.called
        assert "已被使用" in mock_send.call_args[0][1]


def test_pf_tg_r8_startgroup_deep_link_generation(broker: TelegramUpdateBroker):
    """PF-TG-R8: Ephemeral param must be high-entropy, <=64 chars, and produce valid t.me deep link."""
    param = broker.create_ephemeral_param(
        bot_id=123456,
        bot_username="RainbowJudgeBot",
        authorized_user_id=10001,
        ttl_seconds=300,
    )
    assert len(param) <= 64
    assert len(param) >= 32
    # Verify deep link formatting
    link = broker.get_startgroup_deep_link("RainbowJudgeBot", param)
    assert link == f"https://t.me/RainbowJudgeBot?startgroup={param}"

    # Verify link with @ prefix stripped
    link_at = broker.get_startgroup_deep_link("@RainbowJudgeBot", param)
    assert link_at == f"https://t.me/RainbowJudgeBot?startgroup={param}"


def test_pf_tg_r8_startgroup_auto_commander_and_inbound_binding(tmp_path: Path):
    """PF-TG-R8: If authorized_user_ids is empty, first deep link sender becomes Commander."""
    db_file = tmp_path / "auto_cmd_state.sqlite3"
    state_store = StateStore(db_path=db_file)
    cfg_dir = tmp_path / "fleet_ws"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    broker = TelegramUpdateBroker(
        bot_token="111222:AUTO_CMD_TOKEN",
        seat_role="lead",
        state_store=state_store,
        authorized_user_ids=[],  # Unset commander!
        repo_root=cfg_dir,
    )

    param = broker.create_ephemeral_param(
        bot_id=111222,
        bot_username="FleetLeadBot",
        authorized_user_id=None,
        ttl_seconds=300,
    )

    # Inbound /start message triggered by Telegram deep link
    msg = InboundMessage(
        message_id=50,
        chat_id=-100888999000,
        sender_id=77777,  # First sender becomes commander
        sender_name="CommanderAlpha",
        text=f"/start@FleetLeadBot {param}",
        chat_type="supergroup",
        chat_title="All Stars Fleet",
    )

    with patch.object(broker, "_send_text") as mock_send, \
         patch.object(broker, "_persist_chat_binding") as mock_persist:
        handled, is_bound = broker.process_inbound_message(msg)
        assert handled is True
        assert is_bound is True
        mock_persist.assert_called_once_with(-100888999000, "All Stars Fleet", commander_id=77777)
        mock_send.assert_called_once()
        assert "通信链路已连接" in mock_send.call_args[0][1]

    # Verify CHAT_BOUND event contains commander_id
    events = state_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "CHAT_BOUND"
    assert events[0]["payload"]["chat_id"] == -100888999000
    assert events[0]["payload"]["commander_id"] == 77777

    # Subsequent attempt by a different user with a fresh param must be rejected!
    broker.authorized_user_ids = [77777]  # Now locked
    param2 = broker.create_ephemeral_param(bot_id=111222, bot_username="FleetLeadBot", ttl_seconds=300)
    msg_intruder = InboundMessage(
        message_id=51,
        chat_id=-100888999000,
        sender_id=99999,  # Intruder
        sender_name="Intruder",
        text=f"/start {param2}",
        chat_type="supergroup",
        chat_title="All Stars Fleet",
    )
    with patch.object(broker, "_send_text") as mock_send_intruder:
        handled2, is_bound2 = broker.process_inbound_message(msg_intruder)
        assert handled2 is True
        assert is_bound2 is False
        assert "非授权指挥官" in mock_send_intruder.call_args[0][1]


def test_pf_tg_r8_startgroup_multiseat_group_conflict(tmp_path: Path):
    """PF-TG-R8 Section IV: Follower seat must join the same group; joining another group fails."""
    db_file = tmp_path / "multiseat_state.sqlite3"
    state_store = StateStore(db_path=db_file)
    cfg_dir = tmp_path / "fleet_ws"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Lead seat established warroom -100111111111
    broker_builder = TelegramUpdateBroker(
        bot_token="333444:BUILDER_TOKEN",
        seat_role="builder",
        state_store=state_store,
        authorized_user_ids=[77777],
        repo_root=cfg_dir,
        global_chat_id="-100111111111",
    )

    param = broker_builder.create_ephemeral_param(
        bot_id=333444,
        bot_username="BuilderBot",
        authorized_user_id=77777,
        ttl_seconds=300,
    )

    # User accidentally clicked deep link and chose WRONG group
    wrong_group_msg = InboundMessage(
        message_id=60,
        chat_id=-100999999999,  # Wrong group!
        sender_id=77777,
        sender_name="CommanderAlpha",
        text=f"/start@BuilderBot {param}",
        chat_type="supergroup",
        chat_title="Wrong Group",
    )

    with patch.object(broker_builder, "_send_text") as mock_send:
        handled, is_bound = broker_builder.process_inbound_message(wrong_group_msg)
        assert handled is True
        assert is_bound is False
        assert mock_send.called
        assert "不得加入其他群" in mock_send.call_args[0][1]

    # Verify BIND_FAILED published
    events = state_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "BIND_FAILED"
    assert "不得加入其他群" in events[0]["payload"]["error"]

    # Now user chooses the CORRECT group -100111111111
    correct_group_msg = InboundMessage(
        message_id=61,
        chat_id=-100111111111,  # Correct group!
        sender_id=77777,
        sender_name="CommanderAlpha",
        text=f"/start@BuilderBot {param}",
        chat_type="supergroup",
        chat_title="Fleet WarRoom",
    )

    with patch.object(broker_builder, "_send_text") as mock_send_ok, \
         patch.object(broker_builder, "_persist_chat_binding") as mock_persist:
        handled, is_bound = broker_builder.process_inbound_message(correct_group_msg)
        assert handled is True
        assert is_bound is True
        assert mock_send_ok.called
        assert "通信链路已连接" in mock_send_ok.call_args[0][1]


def test_direct_in_group_passive_binding(tmp_path: Path):
    """Verify sending /bind or /start in a group directly binds the chat without needing a parameter."""
    state_store = StateStore(db_path=tmp_path / "state.db")
    broker = TelegramUpdateBroker(
        bot_token="123456:DirectBindToken",
        seat_role="lead",
        state_store=state_store,
        repo_root=tmp_path,
        authorized_user_ids=[88888],
    )

    msg = InboundMessage(
        message_id=101,
        chat_id=-100999888,
        sender_id=88888,
        sender_name="Commander",
        text="/bind",
        chat_type="supergroup",
        chat_title="Auto Bound WarRoom",
    )

    with patch.object(broker, "_send_text") as mock_send, \
         patch.object(broker, "_persist_chat_binding") as mock_persist:
        handled, is_bound = broker.process_inbound_message(msg)
        assert handled is True
        assert is_bound is True
        assert mock_send.called
        assert "通信链路已连接" in mock_send.call_args[0][1]
        assert mock_persist.called
        assert mock_persist.call_args[0][0] == -100999888

    events = state_store.get_broker_events(after_event_id=0)
    assert len(events) == 1
    assert events[0]["event_type"] == "CHAT_BOUND"
    assert events[0]["payload"]["chat_id"] == -100999888



