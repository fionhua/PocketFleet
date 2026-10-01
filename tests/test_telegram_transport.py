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

    def test_telegram_send_message_429_rate_limit_retry(self):
        transport = TelegramTransport(bot_token="test_token_123")

        err_429 = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=mock.MagicMock(read=lambda: b'{"ok":false,"error_code":429,"description":"Too Many Requests: retry after 1","parameters":{"retry_after":0.01}}'),
        )

        mock_success = mock.MagicMock()
        mock_success.status = 200
        mock_success.read.return_value = b'{"ok":true,"result":{"message_id":2002}}'
        mock_success.__enter__.return_value = mock_success

        with mock.patch("urllib.request.urlopen", side_effect=[err_429, mock_success]) as mock_open:
            with mock.patch("time.sleep") as mock_sleep:
                msg = OutboundMessage(chat_id=999, text="Rate limit test message")
                ok = transport.send_message(msg, max_retries=2)
                self.assertTrue(ok)
                self.assertEqual(mock_open.call_count, 2)
    def test_scan_candidate_groups_filtering(self):
        from pocketfleet.transport.telegram import scan_candidate_groups

        # Mock updates payload with two groups: one small group (3 members) and one large (25 members)
        updates_payload = {
            "ok": True,
            "result": [
                {
                    "update_id": 9001,
                    "my_chat_member": {
                        "chat": {"id": -100111222, "title": "Dev Team Small", "type": "supergroup"},
                    },
                },
                {
                    "update_id": 9002,
                    "message": {
                        "message_id": 10,
                        "chat": {"id": -100999888, "title": "Public Huge Group", "type": "supergroup"},
                        "text": "/start",
                    },
                },
            ],
        }

        # Mock getChatMemberCount responses
        def fake_urlopen(req, timeout=10.0):
            url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
            resp = mock.MagicMock()
            resp.status = 200
            if "getUpdates" in url:
                resp.read.return_value = json.dumps(updates_payload).encode("utf-8")
            elif "chat_id=-100111222" in url:
                resp.read.return_value = json.dumps({"ok": True, "result": 3}).encode("utf-8")
            elif "chat_id=-100999888" in url:
                resp.read.return_value = json.dumps({"ok": True, "result": 25}).encode("utf-8")
            else:
                resp.read.return_value = json.dumps({"ok": False}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            groups, err = scan_candidate_groups("test_token_123", max_members=10)
            self.assertIsNone(err)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["id"], "-100111222")
            self.assertEqual(groups[0]["title"], "Dev Team Small")
            self.assertEqual(groups[0]["member_count"], 3)

    def test_scan_candidate_groups_daemon_running_check(self):
        from pocketfleet.transport.telegram import scan_candidate_groups
        groups, err = scan_candidate_groups("test_token_123", is_daemon_running=True)
        self.assertEqual(groups, [])
        self.assertIn("防冲突拦截", err)

    def test_parse_telegram_chat_from_clipboard(self):
        from pocketfleet.transport.telegram import parse_telegram_chat_from_clipboard

        # 1. Direct Chat ID
        cid, title, err = parse_telegram_chat_from_clipboard("-1004309197838")
        self.assertEqual(cid, -1004309197838)
        self.assertIsNone(err)

        # 2. Telegram Desktop / Web message link
        cid2, title2, err2 = parse_telegram_chat_from_clipboard("https://t.me/c/4309197838/15")
        self.assertEqual(cid2, -1004309197838)
        self.assertIsNone(err2)

        # 3. tg:// deep link
        cid3, title3, err3 = parse_telegram_chat_from_clipboard("tg://privatepost?channel=4309197838")
        self.assertEqual(cid3, -1004309197838)
        self.assertIsNone(err3)

        # 4. Invalid text
        cid4, title4, err4 = parse_telegram_chat_from_clipboard("random non tg text")
        self.assertIsNone(cid4)
        self.assertIn("未包含有效的 Telegram 群链接", err4)


if __name__ == "__main__":
    unittest.main()
