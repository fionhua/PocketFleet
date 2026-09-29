"""Telegram Bot API Transport Implementation

Robust, crash-proof, zero 3rd-party dependency implementation using urllib.request.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from ..core import InboundMessage, OutboundMessage
from .base import BaseTransport

logger = logging.getLogger(__name__)


class TelegramTransport(BaseTransport):
    def __init__(self, bot_token: str, api_base_url: str = "https://api.telegram.org") -> None:
        self.bot_token = bot_token.strip()
        self.api_url = f"{api_base_url}/bot{self.bot_token}"
        self.last_update_id: int | None = None

    def poll_messages(self, timeout_sec: int = 10) -> Sequence[InboundMessage]:
        params = {"timeout": timeout_sec}
        if self.last_update_id is not None:
            params["offset"] = self.last_update_id + 1

        url = f"{self.api_url}/getUpdates?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=timeout_sec + 5) as resp:
                if resp.status != 200:
                    return []
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Telegram poll failed: %s", exc)
            return []

        if not payload.get("ok"):
            return []

        updates = payload.get("result", [])
        messages: list[InboundMessage] = []

        for upd in updates:
            upd_id = upd.get("update_id")
            if upd_id is not None:
                self.last_update_id = max(self.last_update_id or 0, upd_id)

            msg_obj = upd.get("message") or upd.get("channel_post")
            if not msg_obj:
                continue

            text = msg_obj.get("text") or msg_obj.get("caption") or ""
            from_obj = msg_obj.get("from") or {}
            chat_obj = msg_obj.get("chat") or {}

            inbound = InboundMessage(
                message_id=msg_obj.get("message_id", 0),
                chat_id=chat_obj.get("id", 0),
                sender_id=from_obj.get("id", 0),
                sender_name=from_obj.get("username") or from_obj.get("first_name") or "Unknown",
                text=text,
                is_bot=from_obj.get("is_bot", False),
                timestamp=float(msg_obj.get("date", 0)),
            )
            messages.append(inbound)

        return messages

    def send_message(self, message: OutboundMessage) -> bool:
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": message.chat_id,
            "text": message.text,
            "parse_mode": message.parse_mode,
        }
        if message.reply_to_message_id:
            payload["reply_to_message_id"] = message.reply_to_message_id

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    res_json = json.loads(resp.read().decode("utf-8"))
                    return bool(res_json.get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error("Failed to send Telegram message: %s", exc)
            # Markdown parse error fallback: retry in plain text
            if "can't parse entities" in str(exc).lower():
                payload["parse_mode"] = ""
                try:
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status == 200
                except Exception:
                    pass
            return False
        return False
