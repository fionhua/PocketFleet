# -*- coding: utf-8 -*-
"""PocketFleet Built-in Local HTTP Web Bridge (Port 18765)

Zero-dependency HTTP gateway providing REST endpoints for PocketFleet Web Bridge browser extension.
Enables instant handshake, zero-config token verification, and seamless bidirectional telegram routing.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketfleet.bridge_server")

DEFAULT_PORT = 18765
DEFAULT_HOST = "127.0.0.1"


def get_active_bridge_token() -> str:
    """SSOT: Read or generate active bridge token from canonical local appdata path."""
    import secrets

    runtime_root = Path(os.environ.get("LOCALAPPDATA", "")) / "FoldedHostLocalBridge"
    token_file = runtime_root / "bridge.token"
    if token_file.is_file():
        try:
            tok = token_file.read_text(encoding="utf-8").strip()
            if len(tok) >= 32:
                return tok
        except Exception:
            pass

    runtime_root.mkdir(parents=True, exist_ok=True)
    new_tok = secrets.token_urlsafe(32)
    try:
        token_file.write_text(new_tok, encoding="utf-8")
    except Exception:
        pass
    return new_tok


def seed_extension_token(repo_root: Optional[Path] = None, ext_path: Optional[Path] = None) -> str:
    """SSOT: Seed active bridge token into all known extension directories."""
    token = get_active_bridge_token()
    payload = json.dumps({"endpoint": f"http://{DEFAULT_HOST}:{DEFAULT_PORT}", "token": token}, indent=2)

    dirs_to_seed = []
    if repo_root:
        dirs_to_seed.extend([
            repo_root / "browser-extension",
            repo_root / "assets" / "browser-extension",
        ])
    if ext_path and ext_path not in dirs_to_seed:
        dirs_to_seed.append(ext_path)

    for d in dirs_to_seed:
        if d and d.is_dir():
            try:
                (d / "seed_token.json").write_text(payload, encoding="utf-8")
            except Exception:
                pass
    return token


_CODEAI_LOCK = threading.Lock()
_CODEAI_QUEUE: collections.deque[dict[str, Any]] = collections.deque()
_CODEAI_IN_FLIGHT: dict[str, dict[str, Any]] = {}


def enqueue_codeai_message(
    content: str,
    filename: str = "Telegram_collab.txt",
    raw: bool = True,
    channel: str = "duty-wake",
    source: str = "telegram",
    target: str = "chat",
) -> str:
    """Thread-safe enqueue a message for the browser extension to pull into ChatGPT Web."""
    delivery_id = str(uuid.uuid4())
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    item = {
        "ok": True,
        "pending": True,
        "delivery_id": delivery_id,
        "filename": filename,
        "content": content,
        "sha256": sha,
        "raw": raw,
        "channel": channel,
        "source": source,
        "target": target,
        "enqueued_at": time.time(),
    }
    with _CODEAI_LOCK:
        _CODEAI_QUEUE.append(item)
    logger.info("Enqueued CodeAI message %s (length %d bytes)", delivery_id[:8], len(content))
    return delivery_id


def pop_codeai_message() -> Optional[dict[str, Any]]:
    """Pop the next pending message and mark in-flight."""
    with _CODEAI_LOCK:
        if _CODEAI_QUEUE:
            item = _CODEAI_QUEUE.popleft()
            _CODEAI_IN_FLIGHT[item["delivery_id"]] = item
            return item
    return None


def ack_codeai_message(delivery_id: Optional[str]) -> bool:
    """Acknowledge receipt and finish lease for delivery_id."""
    if not delivery_id:
        return False
    with _CODEAI_LOCK:
        return _CODEAI_IN_FLIGHT.pop(delivery_id, None) is not None


class PocketFleetBridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for PocketFleet browser extension bridge protocol."""

    server_version = "PocketFleet-Bridge/1.0"

    def log_message(self, format: str, *args) -> None:
        # Suppress noisy default stdlib logging to stderr
        logger.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def _record_client_activity(self) -> None:
        principal = self.headers.get("X-Folded-Host-Principal", "").strip()
        if principal and hasattr(self.server, "active_clients"):
            self.server.active_clients[principal] = time.time()

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Folded-Host-Extension-ID, X-Folded-Host-Principal",
        )

    def _send_json_response(self, status_code: int, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _verify_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        received_token = auth_header[7:].strip()
        expected_token = getattr(self.server, "expected_token", "")
        if not expected_token:
            expected_token = get_active_bridge_token()
        return hmac.compare_digest(received_token, expected_token)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._record_client_activity()
        path = self.path.split("?")[0]
        if path in ("/api/v1/codeai/pull", "/api/v1/theta/pull"):
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            if getattr(self.server, "kill_switch_active", False):
                self._send_json_response(503, {"ok": False, "status": "PAUSED", "error": "Bridge execution paused"})
                return
            pending_item = pop_codeai_message()
            if pending_item:
                self._send_json_response(200, pending_item)
            else:
                self._send_json_response(200, {"ok": True, "pending": False, "status": "IDLE"})
            return

        if path == "/api/v1/status":
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            kill_switch = getattr(self.server, "kill_switch_active", False)
            self._send_json_response(
                200,
                {
                    "ok": True,
                    "principal": "PocketFleet-Local-Bridge",
                    "kill_switch_active": kill_switch,
                    "capabilities": {
                        "telegram_post": True,
                        "control_switch": True,
                        "codeai_pull": True,
                        "codeai_post": True,
                        "codeai_ack": True,
                    },
                    "codeai_allowed_recipients": [
                        "@AiSoulJudgeBot",
                        "@AiSoulMudSnakeBot",
                        "@AiSoulAlphaSandboxBot",
                        "@AiSoulSettlementBot",
                    ],
                    "codeai_allowed_types": ["CUSTOM", "DIRECTIVE", "REVIEW", "REPLY"],
                },
            )
            return

        if path == "/api/v1/sessions":
            active_map = getattr(self.server, "active_clients", {})
            now = time.time()
            active_list = [p for p, ts in active_map.items() if (now - ts) < 90]
            self._send_json_response(
                200,
                {
                    "ok": True,
                    "active_clients": active_list,
                    "sessions": active_list,
                    "count": len(active_list),
                    "last_seen_ts": max(active_map.values()) if active_map else None,
                },
            )
            return

        if path == "/api/v1/tools":
            self._send_json_response(200, {"ok": True, "tools": []})
            return

        self._send_json_response(404, {"error": f"Not found: {path}"})

    def do_POST(self) -> None:
        self._record_client_activity()
        path = self.path.split("?")[0]
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else b""
        body = {}
        if body_bytes:
            try:
                body = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                pass

        if path in ("/api/v1/codeai/ack", "/api/v1/theta/ack"):
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            delivery_id = body.get("delivery_id")
            ack_codeai_message(delivery_id)
            self._send_json_response(200, {"ok": True, "status": "ACKNOWLEDGED", "delivery_id": delivery_id})
            return

        if path in ("/api/v1/codeai/post", "/api/v1/theta/post"):
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            on_post = getattr(self.server, "on_telegram_post", None)
            if on_post and callable(on_post):
                try:
                    on_post(body)
                except Exception as ex:
                    logger.error("Error dispatching codeai post: %s", ex)
            self._send_json_response(200, {"ok": True, "delivered": True})
            return

        if path == "/api/v1/control/pause":
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            setattr(self.server, "kill_switch_active", True)
            self._send_json_response(200, {"ok": True, "kill_switch_active": True})
            return

        if path == "/api/v1/control/resume":
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            setattr(self.server, "kill_switch_active", False)
            self._send_json_response(200, {"ok": True, "kill_switch_active": False})
            return

        if path == "/api/v1/client-event":
            self._send_json_response(200, {"ok": True})
            return

        if path == "/api/v1/telegram/post":
            if not self._verify_auth():
                self._send_json_response(401, {"error": "bearer token denied"})
                return
            on_post = getattr(self.server, "on_telegram_post", None)
            if on_post and callable(on_post):
                try:
                    on_post(body)
                except Exception as ex:
                    logger.error("Error dispatching telegram post: %s", ex)
            self._send_json_response(200, {"ok": True, "delivered": True})
            return

        self._send_json_response(404, {"error": f"Not found: {path}"})


class PocketFleetBridgeServer:
    """Manager for the built-in local bridge HTTP server."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: Optional[str] = None,
        on_telegram_post: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or get_active_bridge_token()
        self.on_telegram_post = on_telegram_post
        self.active_clients: dict[str, float] = {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def has_active_client(self, max_idle_sec: float = 60.0) -> bool:
        """Check if any browser extension has checked in recently."""
        if not self.is_listening():
            return False
        now = time.time()
        for principal, ts in self.active_clients.items():
            if (now - ts) <= max_idle_sec:
                return True
        return False

    def is_listening(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.05)
                return s.connect_ex((self.host, self.port)) == 0
        except Exception:
            return False

    def start(self) -> bool:
        """Start the bridge server in a background thread if not already running."""
        if self.is_listening():
            logger.info("Port %d is already listening; reusing active bridge service.", self.port)
            return True

        try:
            self._server = HTTPServer((self.host, self.port), PocketFleetBridgeHandler)
            setattr(self._server, "expected_token", self.token)
            setattr(self._server, "kill_switch_active", False)
            setattr(self._server, "on_telegram_post", self.on_telegram_post)
            setattr(self._server, "active_clients", self.active_clients)
            self._running = True

            def _serve():
                logger.info("PocketFleet Local Web Bridge listening on http://%s:%d", self.host, self.port)
                while self._running and self._server:
                    try:
                        self._server.handle_request()
                    except Exception:
                        break

            self._thread = threading.Thread(target=_serve, name="PocketFleetBridgeThread", daemon=True)
            self._thread.start()
            return True
        except Exception as ex:
            logger.warning("Failed to start PocketFleetBridgeServer on %d: %s", self.port, ex)
            return False

    def stop(self) -> None:
        """Stop the bridge server and release resources."""
        self._running = False
        if self._server:
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None


_global_bridge: Optional[PocketFleetBridgeServer] = None


def get_global_bridge_server(
    token: Optional[str] = None,
    on_telegram_post: Optional[Callable[[dict], None]] = None,
) -> PocketFleetBridgeServer:
    """Get or create singleton PocketFleetBridgeServer."""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = PocketFleetBridgeServer(token=token, on_telegram_post=on_telegram_post)
    elif on_telegram_post and _global_bridge.on_telegram_post is None:
        _global_bridge.on_telegram_post = on_telegram_post
        if _global_bridge._server:
            setattr(_global_bridge._server, "on_telegram_post", on_telegram_post)
    return _global_bridge


def ensure_bridge_server_running(
    token: Optional[str] = None,
    on_telegram_post: Optional[Callable[[dict], None]] = None,
) -> bool:
    """Ensure port 18765 bridge server is active (starts built-in server if port is unallocated)."""
    bridge = get_global_bridge_server(token=token, on_telegram_post=on_telegram_post)
    return bridge.start()


def stop_global_bridge_server() -> None:
    """Stop and release singleton PocketFleetBridgeServer."""
    global _global_bridge
    if _global_bridge is not None:
        try:
            _global_bridge.stop()
        except Exception:
            pass
        _global_bridge = None


def is_web_bridge_connected(max_idle_sec: float = 60.0) -> tuple[bool, str]:
    """Check physical connection state of Web Extension Bridge.
    Returns (connected: bool, detail: str).
    """
    global _global_bridge
    bridge = _global_bridge
    if bridge is None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.08)
                if s.connect_ex((DEFAULT_HOST, DEFAULT_PORT)) != 0:
                    return False, "本地网桥端口 18765 离线（服务未启动）"
        except Exception:
            return False, "本地网桥端口 18765 离线"
        return True, "网桥端口 18765 正在监听"

    if not bridge.is_listening():
        return False, "本地网桥端口 18765 离线（服务未启动）"

    now = time.time()
    active_principals = [
        p for p, ts in bridge.active_clients.items()
        if (now - ts) <= max_idle_sec
    ]
    if not active_principals:
        return False, "网桥已在 18765 启动，但未检测到活跃的浏览器扩展连接（请打开 ChatGPT 网页端）"
    return True, f"网桥在线（活跃会话: {', '.join(active_principals)}）"


if __name__ == "__main__":
    import time
    server = PocketFleetBridgeServer()
    if server.start():
        print(f"PocketFleet Web Bridge running on http://{server.host}:{server.port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
    else:
        print("Failed to start server or port already in use.")


