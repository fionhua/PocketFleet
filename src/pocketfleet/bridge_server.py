# -*- coding: utf-8 -*-
"""PocketFleet Built-in Local HTTP Web Bridge (Port 18765)

Zero-dependency HTTP gateway providing REST endpoints for PocketFleet Web Bridge browser extension.
Enables instant handshake, zero-config token verification, and seamless bidirectional telegram routing.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional

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
                        "codeai_pull": False,
                        "codeai_post": False,
                        "codeai_ack": False,
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


