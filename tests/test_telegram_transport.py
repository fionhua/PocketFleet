"""Tests for Telegram Transport Layer (Offline & Mocked)"""
from __future__ import annotations

import json
from unittest import mock

from pocketfleet.core import OutboundMessage
from pocketfleet.transport.telegram import TelegramTransport


def test_telegram_poll_messages_and_offset():
    transport = TelegramTransport(bot_token="test_token_123")
    
    mock_payload = {
        "ok": True,
        "result": [
            {
                "update_id": 5001,
                "message": {
                    "message_id": 1001,
                    "date": 1727600000,
                    "text": "Hello PocketFleet",
                    "from": {"id": 111, "first_name": "Developer", "is_bot": False},
                    "chat": {"id": 999},
                },
            },
            {
                "update_id": 5002,
                "message": {
                    "message_id": 1002,
                    "date": 1727600005,
                    "text": "Bot echo text",
                    "from": {"id": 222, "first_name": "SomeBot", "is_bot": True},
                    "chat": {"id": 999},
                },
            },
        ],
    }

    with mock.patch("urllib.request.urlopen") as mock_open:
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
        mock_open.return_value.__enter__.return_value = mock_resp

        messages = transport.poll_messages(timeout_sec=1)
        assert len(messages) == 2
        assert messages[0].text == "Hello PocketFleet"
        assert messages[0].is_bot is False
        assert messages[1].text == "Bot echo text"
        assert messages[1].is_bot is True
        assert transport.last_update_id == 5002


def test_telegram_send_message():
    transport = TelegramTransport(bot_token="test_token_123")
    mock_payload = {"ok": True, "result": {"message_id": 2001}}

    with mock.patch("urllib.request.urlopen") as mock_open:
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
        mock_open.return_value.__enter__.return_value = mock_resp

        msg = OutboundMessage(chat_id=999, text="Task Done", reply_to_message_id=1001)
        ok = transport.send_message(msg)
        assert ok is True
