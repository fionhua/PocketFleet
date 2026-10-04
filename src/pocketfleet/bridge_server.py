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


MESSAGE_TTL_SECONDS: float = 300.0
DEDUP_WINDOW_SECONDS: float = 60.0

_CODEAI_LOCK = threading.Lock()
_CODEAI_QUEUE: collections.deque[dict[str, Any]] = collections.deque()
_CODEAI_IN_FLIGHT: dict[str, dict[str, Any]] = {}
_CODEAI_RECENT_HASHES: dict[str, float] = {}


def is_client_authorized_for_target(client_principal: Optional[str], target: str) -> bool:
    """Check if the requesting client principal is authorized to pull tasks for target.
    
    Protects multi-agent setups: prevents unauthorized platforms (e.g. Doubao) from
    stealing tasks dispatched to ChatGPT/Settlement Host.
    """
    if not client_principal:
        return True  # Fallback for internal / unannotated callers or unit tests

    p_clean = client_principal.lower().strip()
    t_clean = target.lower().strip()

    # Direct match or substring containment
    if t_clean in p_clean:
        return True

    # Role 'chat' mapping: by default in PocketFleet, chat seat maps to folded-host-chatgpt-web
    if t_clean in ("chat", "@aisoulsettlementbot", "settlement"):
        if "chatgpt" in p_clean or "folded-host" in p_clean:
            return True
        try:
            cfg_path = Path(__file__).resolve().parent.parent.parent / "pocketfleet.json"
            if cfg_path.is_file():
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                chat_engine = (cfg_data.get("seats", {}).get("chat", {}).get("engine", "")).lower()
                if "doubao" in chat_engine and "doubao" in p_clean:
                    return True
                if "claude" in chat_engine and "claude" in p_clean:
                    return True
        except Exception:
            pass
        return False

    if t_clean in ("doubao", "xinji_shu", "@aisouldoubaobot"):
        return "doubao" in p_clean

    return False


def cancel_stale_meeting_messages(concluded_before_ts: float) -> int:
    """Purge any pending or in-flight messages that originated before or at concluded_before_ts.

    Provides resilient, timestamp-watermark-based meeting cancellation without needing explicit meeting IDs.
    """
    canceled = 0
    with _CODEAI_LOCK:
        valid_items = collections.deque()
        while _CODEAI_QUEUE:
            item = _CODEAI_QUEUE.popleft()
            msg_ts = item.get("original_timestamp") or item.get("enqueued_at", 0)
            if msg_ts <= concluded_before_ts:
                canceled += 1
                logger.info(
                    "Canceled pending meeting message %s due to conclusion watermark (ts %.1f <= %.1f)",
                    item.get("delivery_id", "")[:8], msg_ts, concluded_before_ts
                )
            else:
                valid_items.append(item)
        _CODEAI_QUEUE.extend(valid_items)

        expired_inflight = [
            did for did, it in _CODEAI_IN_FLIGHT.items()
            if (it.get("original_timestamp") or it.get("enqueued_at", 0)) <= concluded_before_ts
        ]
        for did in expired_inflight:
            _CODEAI_IN_FLIGHT.pop(did, None)
            canceled += 1

    return canceled


def enqueue_codeai_message(
    content: str,
    filename: str = "Telegram_collab.txt",
    raw: bool = True,
    channel: str = "duty-wake",
    source: str = "telegram",
    target: str = "chat",
    ttl_seconds: float = MESSAGE_TTL_SECONDS,
    original_timestamp: Optional[float] = None,
) -> str:
    """Thread-safe enqueue a message for the browser extension to pull into ChatGPT Web.

    Guarantees:
    1. Strict 300s TTL: messages older than 300 seconds are auto-dropped.
    2. Deduplication: Identical message within 60s is dropped idempotently.
    3. Original Timestamp Validation: Messages sent >300s ago on Telegram are immediately rejected.
    """
    now = time.time()
    if original_timestamp and original_timestamp > 0:
        orig_age = now - original_timestamp
        if orig_age > ttl_seconds:
            logger.warning(
                "Refused to enqueue stale CodeAI message (original message age %.1fs > %ss TTL)",
                orig_age, int(ttl_seconds)
            )
            return "stale_skipped"

    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with _CODEAI_LOCK:
        # Clean expired recent hashes
        expired_hashes = [h for h, ts in _CODEAI_RECENT_HASHES.items() if (now - ts) > DEDUP_WINDOW_SECONDS]
        for h in expired_hashes:
            _CODEAI_RECENT_HASHES.pop(h, None)

        # Check for duplicate within dedup window
        if sha in _CODEAI_RECENT_HASHES:
            logger.info("Dropped duplicate CodeAI message (sha256=%s... within %ds dedup window)", sha[:8], int(DEDUP_WINDOW_SECONDS))
            return "duplicate_skipped"

        # Purge expired items from queue (>300s)
        valid_items = collections.deque()
        while _CODEAI_QUEUE:
            old_item = _CODEAI_QUEUE.popleft()
            old_ts = old_item.get("original_timestamp") or old_item.get("enqueued_at", 0)
            if (now - old_ts) <= old_item.get("ttl_seconds", MESSAGE_TTL_SECONDS):
                valid_items.append(old_item)
            else:
                logger.warning(
                    "Purged stale CodeAI message %s (age %.1fs > %ss TTL) before enqueue",
                    old_item["delivery_id"][:8],
                    now - old_ts,
                    int(old_item.get("ttl_seconds", MESSAGE_TTL_SECONDS)),
                )
        _CODEAI_QUEUE.extend(valid_items)

        # Purge expired in-flight
        expired_inflight = [
            did for did, it in _CODEAI_IN_FLIGHT.items()
            if (now - (it.get("original_timestamp") or it.get("in_flight_at", it.get("enqueued_at", 0)))) > MESSAGE_TTL_SECONDS
        ]
        for did in expired_inflight:
            _CODEAI_IN_FLIGHT.pop(did, None)

        delivery_id = str(uuid.uuid4())
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
            "enqueued_at": now,
            "original_timestamp": original_timestamp or now,
            "ttl_seconds": ttl_seconds,
        }
        _CODEAI_QUEUE.append(item)
        _CODEAI_RECENT_HASHES[sha] = now
    logger.info("Enqueued CodeAI message %s (length %d bytes, TTL %ds, target=%s)", delivery_id[:8], len(content), int(ttl_seconds), target)
    return delivery_id


def pop_codeai_message(client_principal: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Pop the next pending message matching client_principal and mark in-flight.

    Strictly filters out and purges any message older than 300s TTL.
    Prevents unauthorized browser clients (e.g. Doubao) from stealing tasks meant for ChatGPT/Settlement Host.
    """
    now = time.time()
    with _CODEAI_LOCK:
        remaining_items = collections.deque()
        matched_item = None

        while _CODEAI_QUEUE:
            item = _CODEAI_QUEUE.popleft()
            msg_ts = item.get("original_timestamp") or item.get("enqueued_at", 0)
            age = now - msg_ts
            ttl = item.get("ttl_seconds", MESSAGE_TTL_SECONDS)
            if age > ttl:
                logger.warning(
                    "Dropped stale CodeAI message %s upon pull (age %.1fs > %ss TTL, discarded)",
                    item["delivery_id"][:8],
                    age,
                    int(ttl),
                )
                continue

            if matched_item is None:
                item_target = item.get("target", "chat")
                if is_client_authorized_for_target(client_principal, item_target):
                    matched_item = item
                    continue

            remaining_items.append(item)

        _CODEAI_QUEUE.extend(remaining_items)

        if matched_item:
            matched_item["in_flight_at"] = now
            matched_item["pulled_by_principal"] = client_principal
            _CODEAI_IN_FLIGHT[matched_item["delivery_id"]] = matched_item
            return matched_item

    return None


def purge_expired_codeai_messages(max_age_sec: float = MESSAGE_TTL_SECONDS) -> int:
    """Explicitly purge any pending or in-flight messages older than max_age_sec."""
    now = time.time()
    purged = 0
    with _CODEAI_LOCK:
        valid_items = collections.deque()
        while _CODEAI_QUEUE:
            item = _CODEAI_QUEUE.popleft()
            if (now - item.get("enqueued_at", 0)) <= max_age_sec:
                valid_items.append(item)
            else:
                purged += 1
        _CODEAI_QUEUE.extend(valid_items)

        expired_inflight = [
            did for did, it in _CODEAI_IN_FLIGHT.items()
            if (now - it.get("in_flight_at", it.get("enqueued_at", 0))) > max_age_sec
        ]
        for did in expired_inflight:
            _CODEAI_IN_FLIGHT.pop(did, None)
            purged += 1
    if purged > 0:
        logger.info("Explicitly purged %d stale CodeAI messages older than %ds", purged, int(max_age_sec))
    return purged


def ack_codeai_message(delivery_id: Optional[str]) -> bool:
    """Acknowledge receipt and finish lease for delivery_id."""
    if not delivery_id:
        return False
    with _CODEAI_LOCK:
        return _CODEAI_IN_FLIGHT.pop(delivery_id, None) is not None


def format_commander_kickoff_announcement(body: dict) -> str:
    topic = (body.get("topic") or "").strip()
    host = (body.get("host") or "@AiSoulSettlementBot").strip()
    if not host.startswith("@"):
        host = "@" + host
    participants = body.get("participants") or ["@AiSoulSettlementBot", "@AiSoulJudgeBot", "@AiSoulMudSnakeBot"]
    p_tags = [p if p.startswith("@") else f"@{p}" for p in participants]
    if host not in p_tags:
        p_tags.insert(0, host)

    wd = body.get("watchdog_minutes") or 15
    human = (body.get("human") or "ENTJ指挥官").strip()

    name_map = {
        "@aisoulsettlementbot": ("结算主机", "方案推演与会议对账"),
        "@aisouljudgebot": ("裁决者", "架构守门与防御审计"),
        "@aisoulmudsnakebot": ("泥蛇", "工程定桩与算法实现"),
    }

    host_info = name_map.get(host.lower(), (host, "会议主持与推进"))
    host_display_name = host_info[0]

    breakdown_lines = []
    other_nodes = []
    for tag in p_tags:
        info = name_map.get(tag.lower(), (tag, "席位协同"))
        d_name, d_role = info[0], info[1]
        if tag.lower() == host.lower():
            breakdown_lines.append(f"• 🎛️ 【主持席】{d_name} (`{tag}`)：负责把控研讨主轴、拆解分工、推进议程并最终汇总收敛。")
        elif "judge" in tag.lower():
            breakdown_lines.append(f"• ⚖️ 【审计席】{d_name} (`{tag}`)：负责架构守门、合规与防御型逻辑审计、防漏洞防崩塌。")
            other_nodes.append(f"【{d_name}】")
        elif "mudsnake" in tag.lower():
            breakdown_lines.append(f"• 🐍 【施工席】{d_name} (`{tag}`)：负责工程定桩、技术可行性验证、代码与落地实现。")
            other_nodes.append(f"【{d_name}】")
        else:
            breakdown_lines.append(f"• 🤖 【协同席】{d_name} (`{tag}`)：{d_role}。")
            other_nodes.append(f"【{d_name}】")

    seats_breakdown_str = "\n".join(breakdown_lines)
    other_nodes_str = "、".join(other_nodes) if other_nodes else "各参会节点"
    mailto_header = "[mailto] " + " ".join(p_tags)

    announcement = (
        f"{mailto_header}\n\n"
        f"🌾 【人类指挥官 · PocketFleet联席会议启幕指令】\n\n"
        f"📌 会议议题：{topic}\n"
        f"🌾 召集人：{human}\n"
        f"🎛️ 主持人：{host_display_name} (`{host}`)\n"
        f"⏱️ 监护推进：{wd} 分钟无有效进展自动静默提醒主持人\n\n"
        f"---\n\n"
        f"### 一、 参会席位与分工安排\n"
        f"{seats_breakdown_str}\n\n"
        f"---\n\n"
        f"### 二、 战队出站与防回声纪律（全员强制执行）\n\n"
        f"所有出站发言首行必须严格二选一：\n"
        f"• 需对方继续行动或作答（移交执行权）：`[Telegram]re:@TargetBot;[waitReply]`\n"
        f"• 仅同步进展、入席确认或结案（不移交执行权）：`[Telegram]re:{human};[NoReply]`\n\n"
        f"1. 🎯 【首轮入席就位】：\n"
        f"   - 主持人【{host_display_name}】收到本令，立即拆解议题并向具体节点派发第一批任务（使用 `[waitReply]`）；\n"
        f"   - 其余非主持席位（{other_nodes_str}），首次收到本令统一使用 `[Telegram]re:{human};[NoReply]` 仅做入席确认与初步研判，不擅自开启新任务链。\n\n"
        f"2. 🔄 【主持推进义务】：\n"
        f"   - 主持人负有推进到底的义务，严禁仅以“收到/知悉/同意”等纯 ACK 结束发言；每轮必须明确下一个受令节点，或在遇阻时明确向{human}报告。\n\n"
        f"3. 🏁 【闭门鉴权】：\n"
        f"   - 仅【{human}】与【主持人】有权发送 `/meetover` 闭会；主持人确认议题达成共识并给出最终会议总结后，方可发送 `/meetover` 结案归档。\n\n"
        f"---\n\n"
        f"👉 话筒现正式转交给会议主持人【{host_display_name}】(`{host}`)，请立即启动第一轮议题拆解与任务分派！"
    )
    return announcement


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
        self.send_header("Access-Control-Allow-Private-Network", "true")

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
            principal = self.headers.get("X-Folded-Host-Principal", "").strip()
            pending_item = pop_codeai_message(client_principal=principal)
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

        if path == "/api/v1/seats":
            seats_payload = []
            try:
                cfg_path = Path(__file__).resolve().parent.parent.parent / "pocketfleet.json"
                if cfg_path.is_file():
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    seats_dict = data.get("seats", {})
                    for rk in ["chat", "lead", "builder"]:
                        s = seats_dict.get(rk)
                        if s and s.get("bot_username"):
                            b_u = s.get("bot_username", "").strip()
                            if not b_u.startswith("@"):
                                b_u = "@" + b_u
                            s_name = s.get("name", rk)
                            s_desc = "方案推演与会议对账" if rk == "chat" else ("架构守门与审计" if rk == "lead" else "工程定桩与算法落地")
                            s_icon = "🎛️" if rk == "chat" else ("⚖️" if rk == "lead" else "🐍")
                            s_color = "#a855f7" if rk == "chat" else ("#00e5ff" if rk == "lead" else "#10b981")
                            seats_payload.append({
                                "bot": b_u,
                                "name": s_name,
                                "role": s_desc,
                                "icon": s_icon,
                                "color": s_color,
                                "is_host": (rk == "chat"),
                                "selected": True
                            })
            except Exception as e:
                logger.error("Error reading seats config: %s", e)
            if not seats_payload:
                seats_payload = [
                    {"bot": "@AiSoulSettlementBot", "name": "结算主机", "role": "方案推演与会议对账", "icon": "🎛️", "color": "#a855f7", "is_host": True, "selected": True},
                    {"bot": "@AiSoulJudgeBot", "name": "裁决者", "role": "架构守门与审计", "icon": "⚖️", "color": "#00e5ff", "is_host": False, "selected": True},
                    {"bot": "@AiSoulMudSnakeBot", "name": "泥蛇", "role": "工程定桩与算法落地", "icon": "🐍", "color": "#10b981", "is_host": False, "selected": True}
                ]
            self._send_json_response(200, {"ok": True, "seats": seats_payload})
            return

        if path == "/api/v1/user/status":
            from .user_client import UserClientManager
            mgr = UserClientManager.get_instance()
            self._send_json_response(200, mgr.get_user_info())
            return

        if path == "/api/v1/user/qr":
            from .user_client import UserClientManager
            mgr = UserClientManager.get_instance()
            self._send_json_response(200, mgr.start_qr_login())
            return

        if path in ("/", "/miniapp", "/miniapp/", "/miniapp/index.html"):
            for candidate in [
                Path(__file__).resolve().parent.parent.parent / "miniapp" / "index.html",
                Path(__file__).resolve().parent.parent.parent / "index.html",
            ]:
                if candidate.is_file():
                    content = candidate.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(content)
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
            principal = self.headers.get("X-Folded-Host-Principal", "").strip()
            if isinstance(body, dict) and "principal" not in body and principal:
                body["principal"] = principal
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
            principal = self.headers.get("X-Folded-Host-Principal", "").strip()
            if isinstance(body, dict) and "principal" not in body and principal:
                body["principal"] = principal
            on_post = getattr(self.server, "on_telegram_post", None)
            if on_post and callable(on_post):
                try:
                    on_post(body)
                except Exception as ex:
                    logger.error("Error dispatching telegram post: %s", ex)
            self._send_json_response(200, {"ok": True, "delivered": True})
            return

        if path == "/api/v1/user/logout":
            from .user_client import UserClientManager
            mgr = UserClientManager.get_instance()
            ok = mgr.logout()
            self._send_json_response(200, {"ok": ok})
            return

        if path == "/api/v1/meet/kickoff":
            announcement_text = format_commander_kickoff_announcement(body)
            body["announcement_text"] = announcement_text

            from .user_client import UserClientManager
            mgr = UserClientManager.get_instance()
            if mgr.is_authorized():
                chat_id = body.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "-1004309197838")
                ok = mgr.send_message_as_user(chat_id, announcement_text)
                if ok:
                    self._send_json_response(200, {
                        "ok": True,
                        "delivered": True,
                        "mode": "human_user",
                        "message": "Dispatched formal Starfleet meeting kickoff announcement directly as Commander personal account via MTProto"
                    })
                    return

            on_meet = getattr(self.server, "on_meet_kickoff", None)
            if on_meet and callable(on_meet):
                try:
                    on_meet(body)
                    self._send_json_response(200, {"ok": True, "delivered": True, "mode": "bot", "message": "Meeting kickoff dispatched"})
                    return
                except Exception as ex:
                    logger.error("Error dispatching meet kickoff: %s", ex)
                    self._send_json_response(500, {"error": str(ex)})
                    return
            self._send_json_response(400, {"error": "on_meet_kickoff not configured"})
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
        on_meet_kickoff: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or get_active_bridge_token()
        self.on_telegram_post = on_telegram_post
        self.on_meet_kickoff = on_meet_kickoff
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
            setattr(self._server, "on_meet_kickoff", self.on_meet_kickoff)
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
    on_meet_kickoff: Optional[Callable[[dict], None]] = None,
) -> PocketFleetBridgeServer:
    """Get or create singleton PocketFleetBridgeServer."""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = PocketFleetBridgeServer(token=token, on_telegram_post=on_telegram_post, on_meet_kickoff=on_meet_kickoff)
    else:
        if on_telegram_post and _global_bridge.on_telegram_post is None:
            _global_bridge.on_telegram_post = on_telegram_post
            if _global_bridge._server:
                setattr(_global_bridge._server, "on_telegram_post", on_telegram_post)
        if on_meet_kickoff and _global_bridge.on_meet_kickoff is None:
            _global_bridge.on_meet_kickoff = on_meet_kickoff
            if _global_bridge._server:
                setattr(_global_bridge._server, "on_meet_kickoff", on_meet_kickoff)
    return _global_bridge


def ensure_bridge_server_running(
    token: Optional[str] = None,
    on_telegram_post: Optional[Callable[[dict], None]] = None,
    on_meet_kickoff: Optional[Callable[[dict], None]] = None,
) -> bool:
    """Ensure port 18765 bridge server is active (starts built-in server if port is unallocated)."""
    bridge = get_global_bridge_server(token=token, on_telegram_post=on_telegram_post, on_meet_kickoff=on_meet_kickoff)
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


