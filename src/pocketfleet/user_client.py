"""PocketFleet MTProto User Client Manager (Path 2).

Enables PocketFleet to act as Commander's personal Telegram client via MTProto:
1. QR Code login directly through Telegram mobile app (Settings -> Devices -> Link Desktop Device).
2. Authenticated sessions persisted locally at ~/.pocketfleet/user_session.session.
3. Allows sending meeting convening commands directly from Commander's personal account.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Default Telegram Desktop / Android official API keys (safe for personal MTProto clients)
DEFAULT_API_ID = int(os.environ.get("POCKETFLEET_TG_API_ID", "2040"))
DEFAULT_API_HASH = os.environ.get("POCKETFLEET_TG_API_HASH", "b18441a1ff607e10a989891a5462e627")
DEFAULT_SESSION_DIR = Path.home() / ".pocketfleet"
DEFAULT_SESSION_NAME = "user_session"


class UserClientManager:
    """Thread-safe MTProto client manager for Commander's personal Telegram account."""

    _instance: Optional["UserClientManager"] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "UserClientManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(
        self,
        api_id: int = DEFAULT_API_ID,
        api_hash: str = DEFAULT_API_HASH,
        session_dir: Optional[Path] = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_dir = session_dir or DEFAULT_SESSION_DIR
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.session_dir / DEFAULT_SESSION_NAME

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._qr_login: Any = None
        self._is_ready = threading.Event()
        self._qr_wait_task: Optional[asyncio.Task] = None
        self._last_qr_base64: Optional[str] = None
        self._last_qr_url: Optional[str] = None

        self._start_background_loop()

    def _start_background_loop(self) -> None:
        """Start a dedicated asyncio event loop in a background thread."""
        def run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._init_client())
            self._is_ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run_loop, daemon=True, name="PocketFleet-MTProto")
        self._thread.start()
        self._is_ready.wait(timeout=5.0)

    async def _init_client(self) -> None:
        """Initialize the Telethon TelegramClient."""
        try:
            from telethon import TelegramClient
            self._client = TelegramClient(
                str(self.session_path),
                self.api_id,
                self.api_hash,
            )
            await self._client.connect()
            if await self._client.is_user_authorized():
                me = await self._client.get_me()
                logger.info("MTProto UserClient initialized: logged in as %s (id=%s)", getattr(me, "first_name", ""), getattr(me, "id", ""))
            else:
                logger.info("MTProto UserClient initialized: not authorized, ready for QR login")
        except Exception as e:
            logger.warning("Failed to initialize MTProto UserClient: %s", e)

    def is_authorized(self) -> bool:
        """Check if Commander is logged in and authorized."""
        if not self._loop or not self._client:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._check_authorized(), self._loop)
        try:
            return fut.result(timeout=3.0)
        except Exception:
            return False

    async def _check_authorized(self) -> bool:
        if not self._client:
            return False
        try:
            if not self._client.is_connected():
                await self._client.connect()
            return await self._client.is_user_authorized()
        except Exception:
            return False

    def get_user_info(self) -> Dict[str, Any]:
        """Get profile details of the logged-in Commander."""
        if not self._loop or not self._client:
            return {"authorized": False}
        fut = asyncio.run_coroutine_threadsafe(self._get_user_info_async(), self._loop)
        try:
            return fut.result(timeout=3.0)
        except Exception as e:
            return {"authorized": False, "error": str(e)}

    async def _get_user_info_async(self) -> Dict[str, Any]:
        if not await self._check_authorized():
            return {"authorized": False}
        try:
            me = await self._client.get_me()
            return {
                "authorized": True,
                "id": getattr(me, "id", None),
                "first_name": getattr(me, "first_name", ""),
                "last_name": getattr(me, "last_name", ""),
                "username": getattr(me, "username", ""),
                "phone": getattr(me, "phone", ""),
            }
        except Exception as e:
            return {"authorized": False, "error": str(e)}

    def start_qr_login(self) -> Dict[str, Any]:
        """Generate a Telegram login QR code for Commander to scan on phone."""
        if not self._loop or not self._client:
            return {"ok": False, "error": "Client not initialized"}
        fut = asyncio.run_coroutine_threadsafe(self._start_qr_login_async(), self._loop)
        try:
            return fut.result(timeout=8.0)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _start_qr_login_async(self) -> Dict[str, Any]:
        try:
            if not self._client.is_connected():
                await self._client.connect()

            if await self._client.is_user_authorized():
                me = await self._client.get_me()
                return {
                    "ok": True,
                    "already_authorized": True,
                    "user": {
                        "id": getattr(me, "id", None),
                        "first_name": getattr(me, "first_name", ""),
                        "username": getattr(me, "username", ""),
                    }
                }

            self._qr_login = await self._client.qr_login()
            self._last_qr_url = self._qr_login.url

            # Generate QR Image Base64
            import qrcode
            img = qrcode.make(self._qr_login.url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_img = base64.b64encode(buf.getvalue()).decode("utf-8")
            self._last_qr_base64 = f"data:image/png;base64,{b64_img}"

            # Start waiting in background
            if self._qr_wait_task and not self._qr_wait_task.done():
                self._qr_wait_task.cancel()

            self._qr_wait_task = asyncio.create_task(self._wait_for_qr())

            return {
                "ok": True,
                "url": self._last_qr_url,
                "qr_image": self._last_qr_base64,
                "expires_in": 120,
            }
        except Exception as e:
            logger.error("Error creating QR login: %s", e)
            return {"ok": False, "error": str(e)}

    async def _wait_for_qr(self) -> None:
        """Background task waiting for Commander to approve the login on mobile."""
        if not self._qr_login:
            return
        try:
            user = await self._qr_login.wait(timeout=120)
            logger.info("MTProto QR Login successful! Authorized as %s", getattr(user, "first_name", ""))
            self._qr_login = None
            self._last_qr_base64 = None
            self._last_qr_url = None
        except asyncio.TimeoutError:
            logger.info("MTProto QR Login expired after 120s")
            self._qr_login = None
        except Exception as e:
            logger.warning("MTProto QR Login wait terminated: %s", e)
            self._qr_login = None

    def send_message_as_user(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> bool:
        """Send message to a Telegram group directly as Commander's personal account."""
        if not self._loop or not self._client:
            return False
        fut = asyncio.run_coroutine_threadsafe(
            self._send_message_async(chat_id, text, reply_to_message_id),
            self._loop,
        )
        try:
            return fut.result(timeout=10.0)
        except Exception as e:
            logger.error("Failed to send message as user: %s", e)
            return False

    async def _send_message_async(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> bool:
        if not await self._check_authorized():
            logger.warning("Cannot send message as user: not authorized")
            return False
        try:
            target: Any = chat_id
            if isinstance(chat_id, str):
                try:
                    target = int(chat_id)
                except ValueError:
                    target = chat_id

            await self._client.send_message(
                target,
                text,
                reply_to=reply_to_message_id,
                parse_mode="md",
            )
            logger.info("Successfully sent message as Commander to chat %s", chat_id)
            return True
        except Exception as e:
            logger.error("Error in MTProto send_message: %s", e)
            return False

    def logout(self) -> bool:
        """Log out and remove stored session."""
        if not self._loop or not self._client:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._logout_async(), self._loop)
        try:
            return fut.result(timeout=5.0)
        except Exception:
            return False

    async def _logout_async(self) -> bool:
        try:
            if await self._check_authorized():
                await self._client.log_out()
            session_file = Path(f"{self.session_path}.session")
            if session_file.exists():
                session_file.unlink()
            return True
        except Exception as e:
            logger.warning("Error during logout: %s", e)
            return False
