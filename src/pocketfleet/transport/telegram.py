"""Telegram Bot API Transport Implementation

Robust, crash-proof, zero 3rd-party dependency implementation using urllib.request.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Sequence

from ..core import InboundMessage, OutboundMessage
from ..state import StateStore
from .base import BaseTransport

logger = logging.getLogger(__name__)


class TelegramTransport(BaseTransport):
    def __init__(
        self,
        bot_token: str,
        api_base_url: str = "https://api.telegram.org",
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.api_url = f"{api_base_url}/bot{self.bot_token}"
        self.state_store = state_store
        self.last_update_id: int | None = self.state_store.get_watermark() if self.state_store else None

        # Token-based lock file (PF-03R7)
        import hashlib
        token_hash = hashlib.sha256(self.bot_token.encode("utf-8")).hexdigest()[:16]
        self._lock_file = Path.home() / ".pocketfleet" / f"broker_token_{token_hash}.lock"

    def acquire_transport_lock(self) -> bool:
        """Acquire atomic exclusive token lock for polling."""
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(self._lock_file)
        payload = json.dumps({"pid": os.getpid(), "created_at": time.time(), "seat": "daemon"})
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            return True
        except FileExistsError:
            try:
                data = json.loads(self._lock_file.read_text(encoding="utf-8"))
                old_pid = int(data.get("pid", 0))
                if old_pid == os.getpid():
                    return True
                from ..state import is_pid_alive
                if not is_pid_alive(old_pid):
                    self._lock_file.unlink(missing_ok=True)
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(payload)
                    return True
            except Exception:
                pass
            return False

    def release_transport_lock(self) -> None:
        try:
            if self._lock_file.is_file():
                data = json.loads(self._lock_file.read_text(encoding="utf-8"))
                if int(data.get("pid", 0)) == os.getpid():
                    self._lock_file.unlink(missing_ok=True)
        except Exception:
            pass

    def poll_messages(self, timeout_sec: int = 10) -> Sequence[InboundMessage]:
        if not self.acquire_transport_lock():
            logger.warning("TelegramTransport cannot poll: another process holds token lock.")
            return []

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
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                logger.error("HTTP 409 Conflict: Bot Token is occupied by an external consumer!")
                raise
            logger.warning("Telegram poll HTTP error: %s", exc)
            return []
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
                if self.state_store:
                    self.state_store.set_watermark(self.last_update_id)

            msg_obj = upd.get("message") or upd.get("channel_post")
            if not msg_obj:
                continue

            text = msg_obj.get("text") or msg_obj.get("caption") or ""
            from_obj = msg_obj.get("from") or {}
            chat_obj = msg_obj.get("chat") or {}
            chat_type = chat_obj.get("type", "group")
            chat_title = chat_obj.get("title", "")
            sender_chat_obj = msg_obj.get("sender_chat")
            sender_chat_id = sender_chat_obj.get("id") if sender_chat_obj else None
            is_anon = bool(sender_chat_id or (from_obj.get("id") == 1087968824))

            inbound = InboundMessage(
                message_id=msg_obj.get("message_id", 0),
                chat_id=chat_obj.get("id", 0),
                sender_id=from_obj.get("id", 0),
                sender_name=from_obj.get("username") or from_obj.get("first_name") or "Unknown",
                text=text,
                is_bot=from_obj.get("is_bot", False),
                timestamp=float(msg_obj.get("date", 0)),
                chat_type=chat_type,
                chat_title=chat_title,
                sender_chat_id=sender_chat_id,
                is_anonymous=is_anon,
            )
            messages.append(inbound)

        return messages

    def send_message(self, message: OutboundMessage, max_retries: int = 3) -> bool:
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": message.chat_id,
            "text": message.text,
            "parse_mode": message.parse_mode,
        }

        if message.reply_to_message_id:
            payload["reply_to_message_id"] = message.reply_to_message_id

        for attempt in range(max_retries + 1):
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
                        try:
                            raw = resp.read()
                            if isinstance(raw, (bytes, bytearray)):
                                res_json = json.loads(raw.decode("utf-8"))
                                return bool(res_json.get("ok", True))
                        except Exception:
                            pass
                        return True
            except urllib.error.HTTPError as exc:
                err_body = ""
                err_data: dict = {}
                try:
                    err_body = exc.read().decode("utf-8")
                    err_data = json.loads(err_body)
                    # Auto-heal Telegram Supergroup Migration (P0 Defense)
                    migrated_id = err_data.get("parameters", {}).get("migrate_to_chat_id")
                    if migrated_id:
                        logger.info("Auto-migrating Telegram chat ID %s -> %s", payload["chat_id"], migrated_id)
                        payload["chat_id"] = migrated_id
                        continue
                except Exception:
                    pass

                # Auto rate limit handling: HTTP 429 Too Many Requests with Jitter Backoff
                if exc.code == 429 and attempt < max_retries:
                    retry_after = 1.0
                    try:
                        retry_after = float(err_data.get("parameters", {}).get("retry_after", 1.0))
                    except Exception:
                        pass
                    backoff = min(8.0, 1.0 * (2 ** attempt))
                    jitter = random.uniform(0.1, 1.0)
                    wait_time = retry_after + backoff + jitter
                    logger.warning(
                        "Telegram 429 Rate Limit (attempt %d/%d): waiting %.2fs (retry_after=%.1fs, backoff=%.1fs, jitter=%.2fs)...",
                        attempt + 1, max_retries, wait_time, retry_after, backoff, jitter,
                    )
                    time.sleep(wait_time)
                    continue

                logger.error("Failed to send Telegram message: HTTP %s - %s", exc.code, err_body)
                # Automatic fallback: if parse_mode caused Bad Request, strip formatting and retry as plain text
                if exc.code == 400 and payload.get("parse_mode"):
                    logger.info("Retrying Telegram message delivery without parse_mode (plain-text fallback)...")
                    payload.pop("parse_mode", None)
                    continue
                return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.error("Failed to send Telegram message: %s", exc)
                return False

        return False


class BotVerificationResult(tuple):
    """4-tuple (ok, bot_id, username, error) supporting tuple unpacking, properties, and dict access."""

    def __new__(cls, ok: bool, bot_id: Optional[int] = None, username: Optional[str] = None, error: Optional[str] = None):
        return super().__new__(cls, (ok, bot_id, username, error))

    @property
    def ok(self) -> bool:
        return self[0]

    @property
    def id(self) -> Optional[int]:
        return self[1]

    @property
    def username(self) -> Optional[str]:
        return self[2]

    @property
    def error(self) -> Optional[str]:
        return self[3]

    def __bool__(self) -> bool:
        return bool(self[0])

    def get(self, key: str, default=None):
        mapping = {"ok": self[0], "id": self[1], "username": self[2], "error": self[3]}
        return mapping.get(key, default)

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.get(item)
        return super().__getitem__(item)


def verify_bot_token(
    bot_token: str,
    api_base_url: str = "https://api.telegram.org",
) -> BotVerificationResult:
    """Verify bot token by calling getMe.

    Returns BotVerificationResult(ok, id, username, error)
    which unpacks as (ok, bot_id, username, error) and supports dict access.
    """
    token = bot_token.strip()
    if not token:
        return BotVerificationResult(False, None, None, "Bot Token 不能为空")

    url = f"{api_base_url}/bot{token}/getMe"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                res = data.get("result", {})
                b_id = res.get("id")
                b_uname = res.get("username", "")
                return BotVerificationResult(True, int(b_id) if b_id is not None else None, b_uname, None)
            return BotVerificationResult(False, None, None, data.get("description", "Token 无效"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            return BotVerificationResult(False, None, None, body.get("description", f"HTTP {exc.code}"))
        except Exception:
            return BotVerificationResult(False, None, None, f"HTTP {exc.code}: {exc.reason}")
    except Exception as exc:
        return BotVerificationResult(False, None, None, f"网络连接失败: {exc}")


def verify_chat_member(
    bot_token: str,
    chat_id: int | str,
    bot_id: int,
    api_base_url: str = "https://api.telegram.org",
) -> tuple[bool, str]:
    """Verify if the bot is already an active member of the specified chat/group.

    Returns (True, status) or (False, error_message).
    """
    token = bot_token.strip()
    params = urllib.parse.urlencode({"chat_id": str(chat_id).strip(), "user_id": bot_id})
    url = f"{api_base_url}/bot{token}/getChatMember?{params}"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                status = data.get("result", {}).get("status", "")
                if status in ("member", "administrator", "creator"):
                    return True, status
                return False, f"Bot 当前状态为 '{status}'，请在 Telegram 客户端中将该 Bot 设为管理员或群成员。"
            return False, data.get("description", "无法读取群成员信息")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            desc = body.get("description", "")
            if "chat not found" in desc.lower() or "user not found" in desc.lower():
                return False, "Bot 尚未加入该群！请在 Telegram 客户端中将该 Bot 添加进协同群后重试。"
            return False, desc or f"HTTP {exc.code}"
        except Exception:
            return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, f"验证失败: {exc}"


def send_bot_checkin(
    bot_token: str,
    chat_id: int | str,
    bot_name: str,
    role_title: str,
    engine_name: str,
    api_base_url: str = "https://api.telegram.org",
) -> tuple[bool, str]:
    """Send formal communication channel connection message to the collaborative Telegram group."""
    token = bot_token.strip()
    url = f"{api_base_url}/bot{token}/sendMessage"
    text = (
        f"⚡ 【{bot_name} · {role_title}】通信链路已连接！\n"
        f"群组 ID：{chat_id}\n"
        f"（注：当前仅打通 Telegram 通信通道，执行引擎将在服务启动后上线）"
    )
    payload = json.dumps({"chat_id": str(chat_id).strip(), "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return True, "打卡报到成功"
            return False, data.get("description", "发送打卡消息失败")
    except Exception as exc:
        return False, f"发送打卡消息失败: {exc}"


def detect_recent_group_chat(
    bot_token: str,
    is_daemon_running: bool = False,
    api_base_url: str = "https://api.telegram.org",
) -> tuple[int | None, str | None, str | None]:
    """Safely detect the most recent Telegram group/supergroup ID from getUpdates.

    Prevents 409 Conflict if Telegram daemon is active.
    Returns (chat_id, chat_title, error_message).
    """
    if is_daemon_running:
        return (
            None,
            None,
            "【防冲突拦截】Telegram 守护进程正在后台运行！\n为防止 409 Conflict 冲突，请先在主界面点击【Stop All Services】停止服务后再进行群侦测。",
        )

    token = bot_token.strip()
    if not token:
        return None, None, "Bot Token 不能为空"

    allowed = json.dumps(["my_chat_member", "message", "channel_post"])
    params = urllib.parse.urlencode({"limit": 50, "allowed_updates": allowed})
    url = f"{api_base_url}/bot{token}/getUpdates?{params}"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return None, None, data.get("description", "获取更新失败")
            updates = data.get("result", [])
            for upd in reversed(updates):
                msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member")
                if msg:
                    chat = msg.get("chat", {})
                    chat_type = chat.get("type", "")
                    if chat_type in ("group", "supergroup"):
                        cid = chat.get("id")
                        title = chat.get("title") or f"群组 {cid}"
                        return cid, title, None
            return None, None, "未找到群消息！请确保已将 Bot 拉进 TG 群并在群里发送一条 /start 指令或 @Bot 消息，然后重试。"
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return None, None, "HTTP 409 Conflict: 存在另一个活跃的 Telegram 监听进程，请确认已停止后台服务。"
        return None, None, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return None, None, f"侦测失败: {exc}"


def scan_candidate_groups(
    bot_token: str,
    max_members: int = 10,
    is_daemon_running: bool = False,
    api_base_url: str = "https://api.telegram.org",
) -> tuple[list[dict], str | None]:
    """Scan recent Telegram updates for small collaborative groups (total members <= max_members).

    Returns (list_of_groups, error_message).
    Each group in list: {"id": str, "title": str, "member_count": int | None, "type": str}
    """
    if is_daemon_running:
        return (
            [],
            "【防冲突拦截】Telegram 守护进程正在后台运行！\n为防止 409 Conflict 冲突，请先在主界面点击【Stop All Services】停止服务后再进行群扫描。",
        )

    token = bot_token.strip()
    if not token:
        return [], "Bot Token 不能为空，请先填入 Bot Token 后再扫描群组。"

    allowed = json.dumps(["my_chat_member", "message", "channel_post"])
    params = urllib.parse.urlencode({"limit": 100, "allowed_updates": allowed})
    url = f"{api_base_url}/bot{token}/getUpdates?{params}"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return [], data.get("description", "获取 Telegram 更新失败")

            updates = data.get("result", [])
            candidate_map: dict[str, dict] = {}
            for upd in updates:
                for key in ("my_chat_member", "message", "channel_post"):
                    obj = upd.get(key)
                    if obj and isinstance(obj, dict):
                        chat = obj.get("chat") or {}
                        chat_type = chat.get("type", "")
                        if chat_type in ("group", "supergroup"):
                            cid = str(chat.get("id"))
                            title = chat.get("title") or f"群组 {cid}"
                            if cid not in candidate_map:
                                candidate_map[cid] = {
                                    "id": cid,
                                    "title": title,
                                    "type": chat_type,
                                    "member_count": None,
                                }

            if not candidate_map:
                return [], None

            filtered_groups: list[dict] = []
            for cid, ginfo in candidate_map.items():
                count = None
                try:
                    c_url = f"{api_base_url}/bot{token}/getChatMemberCount?chat_id={cid}"
                    c_req = urllib.request.Request(c_url, method="GET")
                    with urllib.request.urlopen(c_req, timeout=5.0) as c_resp:
                        c_data = json.loads(c_resp.read().decode("utf-8"))
                        if c_data.get("ok"):
                            count = int(c_data.get("result", 0))
                except Exception:
                    pass

                ginfo["member_count"] = count
                # Filter: include if member_count <= max_members or if member_count could not be queried
                if count is None or count <= max_members:
                    filtered_groups.append(ginfo)

            return filtered_groups, None

    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return [], "HTTP 409 Conflict: 存在另一个活跃的 Telegram 监听进程，请确认已停止后台服务。"
        return [], f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return [], f"扫描群组失败: {exc}"


def parse_telegram_chat_from_clipboard(
    text: str,
    bot_token: str = "",
    api_base_url: str = "https://api.telegram.org",
) -> tuple[int | None, str | None, str | None]:
    """Extract and resolve Telegram Chat ID from clipboard text or URLs.

    Supports:
    - Raw negative ID: -1004309197838 or -5520412929
    - Supergroup message links: https://t.me/c/4309197838/123
    - TG deep links: tg://privatepost?channel=4309197838
    - Public group usernames: https://t.me/my_fleet_group or @my_fleet_group
    Returns (chat_id, chat_title, error_message).
    """
    raw = (text or "").strip()
    if not raw:
        return None, None, "剪贴板内容为空，请先在 Telegram 中复制群消息链接或群 ID。"

    # 1. Direct standard negative Chat ID
    if re.match(r"^-100\d+$", raw) or re.match(r"^-\d+$", raw):
        cid = int(raw)
        title = ""
        if bot_token:
            try:
                url = f"{api_base_url}/bot{bot_token.strip()}/getChat?chat_id={cid}"
                with urllib.request.urlopen(url, timeout=5.0) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("ok"):
                        title = d.get("result", {}).get("title", "")
            except Exception:
                pass
        return cid, title, None

    # 2. Telegram Desktop / Web internal message links (https://t.me/c/1234567890/11)
    m = re.search(r"t\.me/c/(\d+)", raw)
    if m:
        cid = int(f"-100{m.group(1)}")
        title = ""
        if bot_token:
            try:
                url = f"{api_base_url}/bot{bot_token.strip()}/getChat?chat_id={cid}"
                with urllib.request.urlopen(url, timeout=5.0) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("ok"):
                        title = d.get("result", {}).get("title", "")
            except Exception:
                pass
        return cid, title, None

    # 3. tg://privatepost?channel=1234567890
    m2 = re.search(r"channel=(\d+)", raw)
    if m2:
        cid = int(f"-100{m2.group(1)}")
        return cid, "", None

    # 4. Public group username link (https://t.me/groupname or @groupname)
    m3 = re.search(r"(?:t\.me/|@)([a-zA-Z0-9_]{4,})", raw)
    if m3:
        username = f"@{m3.group(1)}"
        if bot_token:
            try:
                url = f"{api_base_url}/bot{bot_token.strip()}/getChat?chat_id={username}"
                with urllib.request.urlopen(url, timeout=5.0) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("ok"):
                        res = d.get("result", {})
                        return res.get("id"), res.get("title", username), None
                    return None, None, d.get("description", "无法通过 Bot 查询该公开群")
            except Exception as exc:
                return None, None, f"解析公开群失败: {exc}"
        return None, None, f"识别到公开群 {username}，请先提供 Bot Token 以向 Telegram 解析真实 ID。"

    return None, None, "剪贴板未包含有效的 Telegram 群链接或 Chat ID。\n\n💡 提示：在 Telegram 群内右键任意消息 -> 点击「复制消息链接 (Copy Link)」，然后再点击本按钮即可！"
