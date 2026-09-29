"""Tests for Telegram Transport Layer (Offline & Mocked)"""
from __future__ import annotations

import json
import urllib.error
import unittest
from unittest import mock

from pocketfleet.core import OutboundMessage
from pocketfleet.transport.telegram import TelegramTransport


class TestTelegramTransport(unittest.TestCase):
    def test_telegram_poll_messages_and_offset(self):
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
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0].text, "Hello PocketFleet")
            self.assertFalse(messages[0].is_bot)
            self.assertEqual(messages[1].text, "Bot echo text")
            self.assertTrue(messages[1].is_bot)
            self.assertEqual(transport.last_update_id, 5002)

    def test_telegram_send_message_success(self):
        transport = TelegramTransport(bot_token="test_token_123")
        mock_payload = {"ok": True, "result": {"message_id": 2001}}

        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp

            msg = OutboundMessage(chat_id=999, text="Task Done", reply_to_message_id=1001)
            ok = transport.send_message(msg)
            self.assertTrue(ok)

    def test_telegram_send_message_http_400_plain_fallback(self):
        transport = TelegramTransport(bot_token="test_token_123")

        # First call fails with HTTP 400 Bad Request (can't parse entities)
        err_400 = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=mock.MagicMock(read=lambda: b'{"description":"Bad Request: can\'t parse entities"}'),
        )

        # Second call (fallback plain text) succeeds
        mock_success_resp = mock.MagicMock()
        mock_success_resp.status = 200
        mock_success_resp.__enter__.return_value = mock_success_resp

        with mock.patch("urllib.request.urlopen", side_effect=[err_400, mock_success_resp]) as mock_open:
            msg = OutboundMessage(
                chat_id=999,
                text="Complex *unclosed code `_block",
                reply_to_message_id=1001,
                parse_mode="Markdown",
            )
            ok = transport.send_message(msg)
            self.assertTrue(ok)
            self.assertEqual(mock_open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
