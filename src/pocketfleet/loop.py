"""PocketFleet Dispatcher and Decoupled Asynchronous DAG Loop

Implements the Battle-Hardened Architecture:
1. P0-1 Decoupled Threading: Continuous Telegram polling + Single-worker Execution Queue.
   Instantaneous /status and /help response without blocking on 300s tasks.
2. P0-2 Outbox Delivery Guarantee: Messages enqueued in persistent SQLite outbox,
   re-attempted automatically upon network reconnection.
3. P0-3 Crash-Resilient Watermark & Deduplication: StateStore guarantees no duplicate
   code modifications across process crashes or restarts.
4. Echo-Proof DAG Gate: Never responds to bots, locks to allowed Chat IDs.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .cockpit import telemetry
from .core import (
    FleetSeatsConfig,
    InboundMessage,
    OutboundMessage,
    Task,
    TaskStatus,
    WorkerType,
    get_default_seats_config,
)
from .executors.aider import AiderExecutor
from .executors.antigravity import AntigravityExecutor
from .executors.base import BaseExecutor
from .executors.claude_code import ClaudeCodeExecutor
from .executors.codex import CodexExecutor
from .executors.simulation import SimulationExecutor
from .executors.triad import FleetTriadExecutor
from .session_hub import SessionHub
from .state import StateStore
from .transport.base import BaseTransport

logger = logging.getLogger(__name__)


def defang_telegram_mentions(text: str) -> str:
    """Transform active @BotName mentions into read-only display text so recipients don't re-trigger."""
    import re
    replacements = {
        r"@aisoulsettlementbot\b": "结算主机 (免回)",
        r"@aisouljudgebot\b": "裁决者 (免回)",
        r"@aisoulmudsnakebot\b": "泥蛇 (免回)",
    }
    defanged = text
    for pattern, rep in replacements.items():
        defanged = re.sub(pattern, rep, defanged, flags=re.IGNORECASE)
    # Generic fallback: defang any remaining @[name]bot into [name · 免回]
    defanged = re.sub(r"@([a-zA-Z0-9_]+bot)\b", r"[\1 · 免回]", defanged, flags=re.IGNORECASE)
    return defanged


class DispatchLoop:
    def __init__(
        self,
        transport: BaseTransport,
        workspace_cwd: Optional[str] = None,
        default_worker: WorkerType = WorkerType.FLEET_TRIAD,
        allowed_chat_ids: Optional[Set[int]] = None,
        state_store: Optional[StateStore] = None,
        bots_config: Optional[dict] = None,
        role_assignment: Optional[Any] = None,
        session_hub: Optional[SessionHub] = None,
        authorized_user_ids: Optional[Set[int]] = None,
        on_chat_bound: Optional[Any] = None,
        seats_config: Optional[FleetSeatsConfig] = None,
    ) -> None:
        self.transport = transport
        self.workspace_cwd = workspace_cwd
        self.default_worker = default_worker
        self.allowed_chat_ids = set(allowed_chat_ids) if allowed_chat_ids else set()
        self.authorized_user_ids = set(authorized_user_ids) if authorized_user_ids else set()
        self.on_chat_bound = on_chat_bound
        self.state_store = state_store or StateStore()
        self.session_hub = session_hub or SessionHub(self.state_store)
        self.bots_config = bots_config or {}
        self.role_assignment = role_assignment
        self.running = False
        self._pending_tg_events: Dict[str, tuple[int, int, str, float]] = {}
        self._active_meeting: Optional[dict[str, Any]] = None
        self._last_meeting_activity_ts: float = 0.0

        if seats_config:
            self.seats_config = seats_config
        else:
            try:
                base = Path(self.workspace_cwd).resolve() if self.workspace_cwd else Path.cwd().resolve()
                cfg_path = base / "pocketfleet.json"
                if cfg_path.is_file():
                    self.seats_config = FleetSeatsConfig.load_from_file(cfg_path)
                else:
                    self.seats_config = get_default_seats_config()
            except Exception:
                self.seats_config = get_default_seats_config()

        # Registered executors
        codex = CodexExecutor()
        antigravity = AntigravityExecutor()
        claude = ClaudeCodeExecutor()
        aider = AiderExecutor()
        self.executors: Dict[WorkerType, BaseExecutor] = {
            WorkerType.FLEET_TRIAD: FleetTriadExecutor(),
            WorkerType.SIMULATION: SimulationExecutor(),
            WorkerType.CODEX: codex,
            WorkerType.ANTIGRAVITY: antigravity,
            WorkerType.CLAUDE_CODE: claude,
            WorkerType.AIDER: aider,
        }
        self._configure_triad()

        # Decoupled Work Queue (P0-1 Fix)
        # Holds: tuple[Task, BaseExecutor, InboundMessage]
        self.work_queue: queue.Queue[Optional[tuple[Task, BaseExecutor, InboundMessage]]] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None

        # Lock-protected status tracking
        self._status_lock = threading.Lock()
        self.current_running_task: Optional[dict[str, Any]] = None

        # Integrated Update Broker for single-consumer lifecycle and onboarding
        from .broker import TelegramUpdateBroker
        raw_token = getattr(self.transport, "bot_token", "") if self.transport else ""
        bot_token = raw_token if isinstance(raw_token, str) else ""
        self.broker = TelegramUpdateBroker(
            bot_token=bot_token,
            seat_role="lead",
            state_store=self.state_store,
            authorized_user_ids=list(self.authorized_user_ids),
        )

        # Multi-seat secondary transports for seats with dedicated bot tokens (e.g. chat seat / 结算主机)
        self.secondary_transports: dict[str, Any] = {}
        primary_tok = bot_token
        if self.seats_config and getattr(self.seats_config, "seats", None):
            for role_k, s_cfg in self.seats_config.seats.items():
                s_tok = s_cfg.get_token() if hasattr(s_cfg, "get_token") else getattr(s_cfg, "bot_token", "")
                if s_tok and s_tok != primary_tok and role_k not in self.secondary_transports:
                    try:
                        from .transport.telegram import TelegramTransport
                        self.secondary_transports[role_k] = TelegramTransport(
                            bot_token=s_tok,
                            state_store=self.state_store,
                        )
                        logger.info("Initialized secondary Telegram transport for seat '%s' (%s)", role_k, s_cfg.bot_username or s_cfg.name)
                    except Exception as trans_err:
                        logger.warning("Could not initialize secondary transport for seat %s: %s", role_k, trans_err)

    def get_available_workers(self) -> List[WorkerType]:
        available = []
        for w_type, executor in self.executors.items():
            if executor.is_available():
                available.append(w_type)
        return available

    def _role_key(self, role: str) -> WorkerType | None:
        try:
            worker = WorkerType(role)
        except ValueError:
            return None
        if worker in (WorkerType.FLEET_TRIAD, WorkerType.SIMULATION, WorkerType.AUTO):
            return None
        return worker

    def get_role_bindings(self) -> tuple[BaseExecutor | None, BaseExecutor | None, str, str]:
        """Resolve the two configured roles to real executor instances and labels."""
        lead_key = getattr(self.role_assignment, "lead", "antigravity") if self.role_assignment else "antigravity"
        builder_key = getattr(self.role_assignment, "builder", "codex") if self.role_assignment else "codex"
        lead_worker = self._role_key(lead_key)
        builder_worker = self._role_key(builder_key)
        lead_executor = self.executors.get(lead_worker) if lead_worker else None
        builder_executor = self.executors.get(builder_worker) if builder_worker else None
        agents = self.bots_config or {}
        default_names = {
            "antigravity": "Google Antigravity agent",
            "codex": "OpenAI Codex worker",
            "claude_code": "Claude Code",
            "aider": "Aider",
        }
        lead_name = agents.get(lead_key, {}).get("name") or default_names.get(lead_key, lead_key.title())
        builder_name = agents.get(builder_key, {}).get("name") or default_names.get(builder_key, builder_key.title())
        return lead_executor, builder_executor, lead_name, builder_name

    def _configure_triad(self) -> None:
        triad = self.executors.get(WorkerType.FLEET_TRIAD)
        if not isinstance(triad, FleetTriadExecutor):
            return
        lead, builder, lead_name, builder_name = self.get_role_bindings()
        triad.configure(lead, builder, lead_name, builder_name)

    def set_role_assignment(self, role_assignment: Any) -> None:
        self.role_assignment = role_assignment
        self._configure_triad()

    def select_worker(self, requested: WorkerType) -> Optional[BaseExecutor]:
        if requested != WorkerType.AUTO:
            executor = self.executors.get(requested)
            return executor if executor and executor.is_available() else None

        # AUTO may choose another real executor, but never simulation.
        for w_type in [WorkerType.FLEET_TRIAD, WorkerType.CODEX, WorkerType.ANTIGRAVITY, WorkerType.CLAUDE_CODE, WorkerType.AIDER]:
            if w_type in self.executors and self.executors[w_type].is_available():
                return self.executors[w_type]
        return None

    def parse_command(self, text: str) -> tuple[WorkerType, str]:
        text_clean = text.strip()
        if text_clean.startswith("/fleet") or text_clean.startswith("/triad"):
            p_len = 6 if text_clean.startswith("/fleet") else 6
            return WorkerType.FLEET_TRIAD, text_clean[p_len:].strip()
        if text_clean.startswith("/claude"):
            return WorkerType.CLAUDE_CODE, text_clean[7:].strip()
        if text_clean.startswith("/aider"):
            return WorkerType.AIDER, text_clean[6:].strip()
        if text_clean.startswith("/codex"):
            return WorkerType.CODEX, text_clean[6:].strip()
        if text_clean.startswith("/agy") or text_clean.startswith("/antigravity"):
            prefix_len = 4 if text_clean.startswith("/agy") else 12
            return WorkerType.ANTIGRAVITY, text_clean[prefix_len:].strip()
        if text_clean.startswith("/sim"):
            return WorkerType.SIMULATION, text_clean[4:].strip()
        return self.default_worker, text_clean

    def _format_telegram_envelope(
        self,
        prompt: str,
        target_worker: WorkerType,
        sender_name: str = "",
        sender_id: int = 0,
        chat_title: str = "",
    ) -> str:
        """Wrap inbound Telegram prompt with concise Telegram collaboration header matching Fleet Memo."""
        if target_worker == WorkerType.SIMULATION:
            return prompt

        human = sender_name.strip() if sender_name else "人类用户"

        participants = []
        if self.seats_config and getattr(self.seats_config, "seats", None):
            for r_key in ["lead", "builder", "chat"]:
                seat = self.seats_config.seats.get(r_key)
                if seat:
                    bot_u = (seat.bot_username or "").strip()
                    if bot_u:
                        if not bot_u.startswith("@"):
                            bot_u = "@" + bot_u
                        if bot_u not in participants:
                            participants.append(bot_u)
                    elif seat.name and seat.name not in participants:
                        participants.append(seat.name)

        if not participants:
            participants = ["@AiSoulJudgeBot", "@AiSoulMudSnakeBot", "@AiSoulAlphaSandboxBot"]

        p_str = ";".join(participants)

        return (
            f"[来自TG多AI协作];[人类用户:{human}];参与者:[{p_str}]\n"
            "回复格式要求:以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人）。\n\n"
            f"{prompt}"
        )

    def _handle_fleet_meeting(
        self,
        msg: InboundMessage,
        meet_arg: str,
        host_override: Optional[str] = None,
        watchdog_minutes: int = 5,
        participants: Optional[list] = None,
    ) -> None:
        """Handle Starfleet Council meeting convening, participants briefing, and silent watchdog."""
        human = msg.sender_name.strip() if msg.sender_name else "人类指挥官"

        # Resolve all participants from seats_config
        seats_map = getattr(self.seats_config, "seats", {}) if self.seats_config else {}
        chat_seat = seats_map.get("chat")
        lead_seat = seats_map.get("lead")
        builder_seat = seats_map.get("builder")

        chat_u = (chat_seat.bot_username if chat_seat else "@AiSoulSettlementBot") or "@AiSoulSettlementBot"
        lead_u = (lead_seat.bot_username if lead_seat else "@AiSoulJudgeBot") or "@AiSoulJudgeBot"
        builder_u = (builder_seat.bot_username if builder_seat else "@AiSoulMudSnakeBot") or "@AiSoulMudSnakeBot"

        chat_name = (chat_seat.name if chat_seat else "结算主机") or "结算主机"
        lead_name = (lead_seat.name if lead_seat else "裁决者") or "裁决者"
        builder_name = (builder_seat.name if builder_seat else "泥蛇") or "泥蛇"

        import re
        topic = meet_arg.strip()
        host_bot = host_override
        m_host = re.search(r"@([a-zA-Z0-9_]+bot)\b", topic, re.IGNORECASE)
        if m_host:
            host_bot = "@" + m_host.group(1).lower()
            topic = re.sub(r"@([a-zA-Z0-9_]+bot)\b", "", topic, flags=re.IGNORECASE).strip()

        if not host_bot:
            host_bot = chat_u

        if not topic:
            # Build dynamic seats data for WebApp URL based on user's actual configured seats
            seats_data = []
            if self.seats_config and getattr(self.seats_config, "seats", None):
                for role_key in ["chat", "lead", "builder"]:
                    seat = self.seats_config.seats.get(role_key)
                    if seat and seat.bot_username:
                        b_u = seat.bot_username.strip()
                        if not b_u.startswith("@"):
                            b_u = "@" + b_u
                        s_name = seat.name or role_key
                        s_desc = "方案推演与会议对账" if role_key == "chat" else ("架构守门与审计" if role_key == "lead" else "工程定桩与算法落地")
                        s_icon = "🎛️" if role_key == "chat" else ("⚖️" if role_key == "lead" else "🐍")
                        s_color = "#a855f7" if role_key == "chat" else ("#00e5ff" if role_key == "lead" else "#10b981")
                        seats_data.append({
                            "bot": b_u,
                            "name": s_name,
                            "role": s_desc,
                            "icon": s_icon,
                            "color": s_color,
                            "is_host": (role_key == "chat"),
                        })

            import urllib.parse
            import json
            query_str = urllib.parse.urlencode({"data": json.dumps(seats_data)}) if seats_data else ""
            miniapp_url = f"https://pocketfleet.pages.dev/?{query_str}" if query_str else "https://pocketfleet.pages.dev/"

            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🏛️ 打开会议召集面板",
                            "web_app": {"url": miniapp_url}
                        }
                    ]
                ]
            }

            card = (
                "🏛️ *【AI战队联席会议中心】已就绪*\n\n"
                "参会席位已按战队配置自动就位：\n"
                f"• 🎛️ *默认主持*：{chat_name} (`{chat_u}`) — 方案推演与会议对账\n"
                f"• ⚖️ *审计席*：{lead_name} (`{lead_u}`) — 架构守门与防御审计\n"
                f"• 🐍 *施工席*：{builder_name} (`{builder_u}`) — 工程定桩与算法实现\n\n"
                f"👉 [点击打开专属会议召集面板]({miniapp_url})\n\n"
                "💬 *或直接在群里发送研讨议题*（例如：`/meet 招股书退市风险评估`），全员会议立即开席！"
            )
            self._send_immediate_or_outbox(
                chat_id=msg.chat_id,
                text=card,
                reply_to_message_id=msg.message_id,
                reply_markup=reply_markup,
            )
            return None

        # Record active meeting session
        wd_sec = max(60, int(watchdog_minutes * 60)) if watchdog_minutes else 300
        self._active_meeting = {
            "active": True,
            "topic": topic,
            "host": host_bot,
            "chat_id": msg.chat_id,
            "watchdog_sec": wd_sec,
            "start_ts": time.time(),
        }
        self._last_meeting_activity_ts = time.time()

        # Build dynamic seat display
        seat_lines = []
        if participants and isinstance(participants, list):
            for p in participants:
                p_tag = p if p.startswith("@") else f"@{p}"
                is_h = " (首发主持)" if p_tag.lower() == host_bot.lower() else ""
                seat_lines.append(f"• 🤖 `{p_tag}`{is_h}")
        else:
            seat_lines = [
                f"• 🎛️ *主持席*：{chat_name} (`{chat_u}`) — 方案推演与对账" + (" (首发主持)" if host_bot.lower() == chat_u.lower() else ""),
                f"• ⚖️ *审计席*：{lead_name} (`{lead_u}`) — 架构守门与防御审计" + (" (首发主持)" if host_bot.lower() == lead_u.lower() else ""),
                f"• 🐍 *施工席*：{builder_name} (`{builder_u}`) — 工程定桩与算法实现" + (" (首发主持)" if host_bot.lower() == builder_u.lower() else ""),
            ]
        seats_str = "\n".join(seat_lines)

        # 1. Telegram Group Kickoff Announcement
        announcement = (
            f"🏛️ *【AI星舰联席会议已召开】*\n"
            f"📌 *议题*：{topic}\n"
            f"🌾 *召集人*：{human}\n"
            f"⏱️ *推进看门狗*：每 {watchdog_minutes} 分钟静默监护\n\n"
            f"参会席位（基于战队配置自动就位）：\n"
            f"{seats_str}\n\n"
            f"📡 议题公文已分发至主持席（`{host_bot}`），正在展开第一手深度推演，请稍候..."
        )
        self._send_immediate_or_outbox(chat_id=msg.chat_id, text=announcement, reply_to_message_id=msg.message_id)

        # 2. Canonical Starfleet Meeting Briefing for Host (ChatGPT / Web Bridge)
        from .bridge_server import enqueue_codeai_message, is_web_bridge_connected
        connected, reason = is_web_bridge_connected(max_idle_sec=60.0)

        briefing_prompt = (
            f"# 🏛️ 【AI星舰战队联席会议公文】\n"
            f"📌 会议议题：{topic}\n"
            f"🌾 召集人：{human}\n\n"
            f"### 一、 参会席位与会议分工\n"
            f"1. 🎛️ 【主持席】{chat_name} ({chat_u})\n"
            f"   分工：牵头破题、方案推演、会议对账与综合结论。\n"
            f"2. ⚖️ 【审计席】{lead_name} ({lead_u})\n"
            f"   分工：架构守门、防崩兜底、代码与逻辑严密审计。\n"
            f"3. 🐍 【施工席】{builder_name} ({builder_u})\n"
            f"   分工：工程落地、核心算法实现与技术定桩。\n\n"
            f"### 二、 会议主持与发信规则\n"
            f"• 谁开始主持：由【{chat_name}】率先开场发言，就议题展开第一手深度剖析与推演；\n"
            f"• 战友交接：推演中如需代码或审计支持，文末以 [Telegram]re:{lead_u}[waitReply] 或 [Telegram]re:{builder_u}[waitReply] 点名交接；\n"
            f"• 结案/通知：纯同步信息请带上 [NoReply]；\n"
            f"• 出站发信：回复首行带 [Telegram]，网桥将自动实时投递至战队群！\n\n"
            f"请作为主持席立即开始就本次议题展开深入推演！"
        )

        enqueue_codeai_message(
            content=briefing_prompt,
            filename=f"Telegram_meet_{msg.message_id}.txt",
            raw=True,
            channel="duty-wake",
            source=f"telegram:{msg.chat_id}:{msg.message_id}",
            target="chat",
        )
        self.state_store.record_message_start(msg.message_id, msg.chat_id, topic, "chatgpt_web_meeting")

        if not connected:
            warning_text = (
                f"⚠️ [PocketFleet 网桥提醒] 结算主机网页未就绪（{reason}）。\n"
                f"📌 会议公文已在本地网桥队列安全待命，请在浏览器中打开 ChatGPT 网页，确认扩展显示 🟢 已就绪即可自动推进！"
            )
            self._send_immediate_or_outbox(chat_id=msg.chat_id, text=warning_text, reply_to_message_id=msg.message_id)

        return None

    def _handle_fleet_meeting_over(self, msg: InboundMessage) -> None:
        """Handle Starfleet Council meeting conclusion and watchdog termination."""
        topic = "本次会议"
        if self._active_meeting:
            topic = self._active_meeting.get("topic", "本次会议")
            self._active_meeting["active"] = False

        closing_card = (
            f"🏁 *【AI星舰联席会议 · 圆满闭幕】*\n\n"
            f"📌 *议题*：{topic}\n"
            f"✅ 会议看门狗已安全停止，各席位自动转入常规待命态。\n"
            f"🌾 战队协同推演结案，感谢指挥官与各席位的高效推进！"
        )
        self._send_immediate_or_outbox(chat_id=msg.chat_id, text=closing_card, reply_to_message_id=msg.message_id)
        return None

    def _check_meeting_watchdog(self) -> None:
        """Silent watchdog: if no activity detected for X minutes, ping host to advance agenda."""
        if not self._active_meeting or not self._active_meeting.get("active"):
            return
        now = time.time()
        timeout_sec = self._active_meeting.get("watchdog_sec", 300)
        chat_id = self._active_meeting.get("chat_id")
        if not chat_id:
            return
        if now - self._last_meeting_activity_ts >= timeout_sec:
            host = self._active_meeting.get("host", "@AiSoulSettlementBot")
            topic = self._active_meeting.get("topic", "本次议题")
            minutes = max(1, int(timeout_sec // 60))
            reminder = (
                f"⏰ *【星舰联席会议 · 进度推进看门狗】*\n"
                f"已超过 {minutes} 分钟未检测到会议新动态。\n"
                f"📌 *议题*：{topic}\n"
                f"👉 请主持席 `{host}` 推进议程分工；若议题讨论已完成，请发送 `/meetover` 正式闭幕。"
            )
            self._send_immediate_or_outbox(chat_id=chat_id, text=reminder)
            # Reset timestamp so next ping occurs in timeout_sec
            self._last_meeting_activity_ts = now


    def _send_immediate_or_outbox(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "Markdown",
        reply_markup: Optional[dict] = None,
    ) -> None:
        """Attempt immediate transport delivery; fallback to persistent outbox on failure."""
        msg = OutboundMessage(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        ok = False

        try:
            ok = self.transport.send_message(msg)
        except Exception as exc:
            logger.warning("Immediate send failed: %s. Enqueuing to outbox.", exc)

        if not ok:
            # Enqueue to persistent outbox for guaranteed delivery (P0-2 Fix)
            self.state_store.enqueue_outbox(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_message_id,
            )

    def flush_outbox(self) -> int:
        """Drain pending outbox items and retry delivery."""
        pending = self.state_store.get_pending_outbox(max_retries=5, limit=5)
        sent_count = 0
        for item in pending:
            outbound = OutboundMessage(
                chat_id=item["chat_id"],
                text=item["text"],
                parse_mode=item["parse_mode"],
                reply_to_message_id=item["reply_to_message_id"],
            )
            try:
                if self.transport.send_message(outbound):
                    self.state_store.mark_outbox_sent(item["id"])
                    sent_count += 1
                else:
                    self.state_store.mark_outbox_failed_attempt(item["id"])
            except Exception:
                self.state_store.mark_outbox_failed_attempt(item["id"])
        return sent_count

    def _persist_bound_chat(self, chat_id: int, chat_title: str = "") -> None:
        """Persist auto-bound Telegram chat ID to telemetry, .env and pocketfleet.json."""
        try:
            telemetry.allowed_chat_ids = [chat_id]
        except Exception:
            pass

        from pathlib import Path
        import os
        from .onboard import save_token_to_env

        # 1. Update .env
        try:
            base_dir = Path(self.workspace_cwd).resolve() if self.workspace_cwd else Path.cwd().resolve()
            env_file = base_dir / ".env"
            save_token_to_env(str(chat_id), env_path=env_file, var_name="TELEGRAM_GROUP_ID")
            os.environ["TELEGRAM_GROUP_ID"] = str(chat_id)
            logger.info("Persisted TELEGRAM_GROUP_ID=%s to %s", chat_id, env_file)
        except Exception as e:
            logger.warning("Failed to persist TELEGRAM_GROUP_ID to .env: %s", e)

        # 2. Update config file (pocketfleet.json)
        try:
            base_dir = Path(self.workspace_cwd).resolve() if self.workspace_cwd else Path.cwd().resolve()
            cfg_file = base_dir / "pocketfleet.json"
            if cfg_file.is_file():
                import json
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                data["telegram_chat_id"] = str(chat_id)
                if chat_title:
                    data["telegram_group_name"] = str(chat_title)
                cfg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info("Persisted telegram_chat_id=%s to %s", chat_id, cfg_file)
        except Exception as e:
            logger.warning("Failed to persist telegram_chat_id to config file: %s", e)

    def handle_message(self, msg: InboundMessage, forced_seat: Optional[str] = None) -> Optional[Task]:
        raw_text = (msg.text or "").strip()
        if not raw_text:
            return None

        # --- [ECHO-PROOF SHIELD 1: Drop System Status Notifications & Echoes] ---
        # Never process outbox echoes, acks, or task status notifications from any sender
        if raw_text.startswith(("⏳", "❌", "✅", "🤖", "🎛️")) or any(
            m in raw_text for m in ("Task Queued", "Task Failed", "Task Completed", "收到指令，已递交至", "PocketFleet Status:")
        ):
            logger.debug("Dropped system status echo/notification ID %s: %s...", msg.message_id, raw_text[:40])
            return None

        # --- [IRON GATE 2: Role-based Gating & Bot Echo Proofing] ---
        # Allow internal fleet bots ONLY if explicitly directed to a peer bot via @mention; drop external unknown bots
        if msg.is_bot:
            fleet_bot_handles = set()
            if self.seats_config and getattr(self.seats_config, "seats", None):
                for s in self.seats_config.seats.values():
                    if s.bot_username:
                        fleet_bot_handles.add(s.bot_username.lower().lstrip("@"))

            s_name = (msg.sender_name or "").lower().lstrip("@")
            if s_name not in fleet_bot_handles:
                logger.debug("Ignored external bot message ID %s from %s", msg.message_id, msg.sender_name)
                return None

            # Internal fleet bots MUST explicitly direct to another bot via @mention (e.g. @AiSoulMudSnakeBot)
            # This strictly prevents unaddressed bot chatter or echoes from entering the default worker queue!
            import re
            has_explicit_peer_mention = bool(re.search(r"@([a-zA-Z0-9_]+bot)\b", raw_text, flags=re.IGNORECASE))
            if not has_explicit_peer_mention:
                logger.debug("Dropped internal fleet bot broadcast without explicit peer mention from @%s", s_name)
                return None

        # --- [IRON GATE 2.5: TelegramUpdateBroker Onboarding & Ephemeral Binding] ---
        if hasattr(self, "broker"):
            handled, is_bound = self.broker.process_inbound_message(msg)
            if handled:
                if is_bound:
                    if self.allowed_chat_ids is None:
                        self.allowed_chat_ids = set()
                    self.allowed_chat_ids.add(msg.chat_id)
                    if self.on_chat_bound:
                        try:
                            self.on_chat_bound(msg.chat_id, getattr(msg, "chat_title", "") or msg.sender_name or "")
                        except Exception as cb_err:
                            logger.warning("on_chat_bound callback error: %s", cb_err)
                return None

        # --- [IRON GATE 3: Security Whitelist Check] ---
        # Allow if chat is in allowed_chat_ids OR sender is in allowed_chat_ids (legacy user whitelist compatibility)
        if self.allowed_chat_ids:
            if msg.chat_id not in self.allowed_chat_ids and msg.sender_id not in self.allowed_chat_ids:
                logger.warning(
                    "Blocked message ID %s from unauthorized chat ID %s (allowed: %s)",
                    msg.message_id, msg.chat_id, self.allowed_chat_ids
                )
                return None
        elif not self.authorized_user_ids:
            # If neither allowed_chat_ids nor authorized_user_ids is configured, fail-closed! (No auto-binding)
            logger.warning(
                "Blocked message ID %s from unconfigured chat ID %s (whitelist empty, PIN required)",
                msg.message_id, msg.chat_id
            )
            return None

        # Sender MUST be authorized if authorized_user_ids is configured
        if self.authorized_user_ids and msg.sender_id not in self.authorized_user_ids:
            logger.warning(
                "Blocked message ID %s from unauthorized sender ID %s in chat %s",
                msg.message_id, msg.sender_id, msg.chat_id
            )
            return None

        # Record last human sender name for seamless UI syncing
        if not msg.is_bot and msg.sender_name:
            try:
                self.state_store.set_meta("last_human_sender_name", msg.sender_name.strip())
            except Exception:
                pass

        raw_text = (msg.text or "").strip()
        if not raw_text:
            return None

        # Update meeting watchdog activity timer if meeting is currently in progress
        if self._active_meeting and self._active_meeting.get("active"):
            if msg.chat_id == self._active_meeting.get("chat_id"):
                self._last_meeting_activity_ts = time.time()

        clean_text = raw_text.strip()
        # Telegram Mini App submission (web_app_data)
        if clean_text.startswith("{") and ("topic" in clean_text or "meet" in clean_text or "action" in clean_text):
            try:
                import json
                data = json.loads(clean_text)
                if isinstance(data, dict) and (data.get("action") == "meet" or "topic" in data):
                    m_topic = data.get("topic", "").strip()
                    m_host = data.get("host", None)
                    m_watchdog = int(data.get("watchdog_minutes", 5))
                    m_participants = data.get("participants", None)
                    return self._handle_fleet_meeting(
                        msg=msg,
                        meet_arg=m_topic,
                        host_override=m_host,
                        watchdog_minutes=m_watchdog,
                        participants=m_participants,
                    )
            except Exception as e:
                logger.debug("Failed parsing inbound JSON as MiniApp meet data: %s", e)

        # /meetover or #MEET_OVER or #MEET_SUMMARY
        if clean_text.lower().startswith("/meetover") or clean_text.startswith("#MEET_OVER") or clean_text.startswith("#MEET_SUMMARY"):
            return self._handle_fleet_meeting_over(msg)

        # /meet or /meeting
        if clean_text.lower().startswith("/meet") or clean_text.lower().startswith("/meeting"):
            arg = ""
            if clean_text.lower().startswith("/meeting"):
                arg = clean_text[8:].strip()
            elif clean_text.lower().startswith("/meet"):
                arg = clean_text[5:].strip()
            return self._handle_fleet_meeting(msg=msg, meet_arg=arg)
        # Detect bot mention before stripping to auto-route to designated seat
        import re
        mentioned_worker: WorkerType | None = None
        match_bot = re.search(r"@([a-zA-Z0-9_]+bot)\b", raw_text, flags=re.IGNORECASE)
        if match_bot and self.seats_config and getattr(self.seats_config, "seats", None):
            bot_tag = "@" + match_bot.group(1).lower()
            for r_key, seat in self.seats_config.seats.items():
                if seat.bot_username and seat.bot_username.lower() == bot_tag:
                    s_eng = (seat.engine or "").lower().strip()
                    if s_eng == "codex":
                        mentioned_worker = WorkerType.CODEX
                    elif s_eng in ("antigravity", "agy"):
                        mentioned_worker = WorkerType.ANTIGRAVITY
                    elif s_eng == "claude_code":
                        mentioned_worker = WorkerType.CLAUDE_CODE
                    elif s_eng == "aider":
                        mentioned_worker = WorkerType.AIDER
                    break

        text = re.sub(r"@[a-zA-Z0-9_]+bot\b", "", raw_text, flags=re.IGNORECASE).strip()
        if not text:
            text = raw_text

        # Route Chat AI seat (e.g. 结算主机 / ChatGPT Web via Port 18765 Web Bridge)
        is_chat_seat = (forced_seat == "chat")
        if not is_chat_seat and self.seats_config and getattr(self.seats_config, "seats", None):
            chat_seat = self.seats_config.seats.get("chat")
            if chat_seat:
                c_uname = (chat_seat.bot_username or "").lower().strip()
                if c_uname and match_bot and ("@" + match_bot.group(1).lower()) == c_uname:
                    is_chat_seat = True
                elif match_bot and ("@" + match_bot.group(1).lower()) == "@aisoulsettlementbot":
                    is_chat_seat = True
                elif not match_bot and (text.startswith("/chat") or raw_text.startswith("/chat")):
                    is_chat_seat = True

        if is_chat_seat:
            if self.state_store.is_message_processed(msg.message_id, chat_id=msg.chat_id):
                logger.info("Skipped already processed message ID %s in chat %s", msg.message_id, msg.chat_id)
                return None

            clean_prompt = text[5:].strip() if text.startswith("/chat") else text
            if not clean_prompt:
                clean_prompt = raw_text

            from .bridge_server import enqueue_codeai_message, is_web_bridge_connected
            connected, reason = is_web_bridge_connected(max_idle_sec=60.0)

            enqueue_codeai_message(
                content=clean_prompt,
                filename=f"Telegram_to_chat_{msg.message_id}.txt",
                raw=True,
                channel="duty-wake",
                source=f"telegram:{msg.chat_id}:{msg.message_id}",
                target="chat",
            )
            self.state_store.record_message_start(msg.message_id, msg.chat_id, clean_prompt, "chatgpt_web")
            logger.info("Enqueued message %s for Chat AI / 结算主机 to Web Bridge (Port 18765), connected=%s", msg.message_id, connected)

            # 严禁本地擅自冒用结算主机身份签发假回执！
            # 真实结算主机的回复必须且只能由 ChatGPT 网页端生成后通过 Web Bridge 回传。
            # 若网桥物理未连通，通过主网关（self.transport）如实向指挥官告警，绝不自欺欺人。
            if not connected:
                warning_text = (
                    f"⚠️ [PocketFleet 网桥提醒] 结算主机网页端未就绪（{reason}）\n\n"
                    f"📌 任务已在本地网桥队列安全待命（ID: {msg.message_id}），但尚未送达 ChatGPT 页面。\n"
                    f"👉 请在浏览器中打开 ChatGPT 网页，并确认扩展浮窗显示 🟢「控制链在线，协同网桥已就绪」。"
                )
                self.transport.send_message(OutboundMessage(chat_id=msg.chat_id, text=warning_text, reply_to_message_id=msg.message_id))
            return None

        # --- Instant Non-Blocking System Commands (P0-1 Fix) ---
        if text == "/status":
            available = self.get_available_workers()
            sess = None
            try:
                sess = self.session_hub.get_session("lead")
            except Exception:
                pass

            recent = []
            try:
                recent = self.session_hub.get_recent_events(limit=10, seat_id="lead")
            except Exception:
                pass

            running_evt = next((e for e in recent if e.status == "running"), None)
            queued_count = sum(1 for e in recent if e.status == "queued")

            with self._status_lock:
                running_info = self.current_running_task

            if running_evt or (sess and sess.status == "busy") or running_info:
                active_prompt = (
                    running_evt.prompt
                    if running_evt
                    else (running_info["prompt"] if running_info else "Processing task")
                )
                active_worker = "antigravity" if running_evt else (running_info["worker"] if running_info else "lead")
                cid_str = f"`{sess.conversation_id[:8]}...{sess.conversation_id[-4:]}`" if sess else "`Default`"
                elapsed = int(time.time() - (running_evt.created_at if running_evt else (running_info["start_time"] if running_info else time.time())))
                status_text = (
                    "🤖 *PocketFleet Status: BUSY*\n"
                    f"• Active Worker: `{active_worker}`\n"
                    f"• CLI Track: {cid_str}\n"
                    f"• Running Task: `{active_prompt[:60]}...`\n"
                    f"• Elapsed Time: {elapsed}s\n"
                    f"• Queued Tasks: {queued_count} pending\n"
                    f"• Workspace: `{self.workspace_cwd or 'Default'}`"
                )
            else:
                cid_str = f"`{sess.conversation_id[:8]}...{sess.conversation_id[-4:]}`" if sess else "`Default`"
                status_text = (
                    "🤖 *PocketFleet Status: IDLE & READY*\n"
                    f"• CLI Track: {cid_str}\n"
                    f"• Workspace: `{self.workspace_cwd or 'Default'}`\n"
                    f"• Available Workers: {', '.join([w.value for w in available]) or 'None'}\n"
                    f"• Queued Tasks: {queued_count} pending\n"
                    f"• Architecture: Decoupled Single-Writer Session Hub"
                )
            self._send_immediate_or_outbox(msg.chat_id, status_text, reply_to_message_id=msg.message_id)
            return None

        if text.startswith("/mode"):
            parts = text.split()
            if len(parts) > 1:
                target = parts[1].lower().strip()
                mode_map = {
                    "fleet": WorkerType.FLEET_TRIAD,
                    "triad": WorkerType.FLEET_TRIAD,
                    "sim": WorkerType.SIMULATION,
                    "simulation": WorkerType.SIMULATION,
                    "codex": WorkerType.CODEX,
                    "agy": WorkerType.ANTIGRAVITY,
                    "antigravity": WorkerType.ANTIGRAVITY,
                    "claude": WorkerType.CLAUDE_CODE,
                    "aider": WorkerType.AIDER,
                }
                if target in mode_map:
                    selected = mode_map[target]
                    executor = self.executors.get(selected)
                    if executor and executor.is_available():
                        self.default_worker = selected
                        resp = (
                            f"🎛️ *Fleet Engine Switched to: `{self.default_worker.value}`*\n"
                            f"• Active Mode: *{self.default_worker.value.upper()}*\n"
                            "Send any requirement to execute it."
                        )
                    else:
                        resp = f"❌ Engine `{selected.value}` is not available. Mode was not changed."
                else:
                    resp = f"❓ Unknown mode `{target}`. Available: `fleet`, `codex`, `agy`, `sim`, `claude`, `aider`."
            else:
                resp = (
                    f"🎛️ *Current Default Fleet Engine*: `{self.default_worker.value}`\n"
                    "• `/mode fleet` — two real agents: plan, build, verify\n"
                    "• `/mode codex` — 🤖 Local OpenAI Codex Engine\n"
                    "• `/mode agy` — 🌈 Local Google Antigravity\n"
                    "• `/mode sim` — preview only; no agent or tests"
                )
            self._send_immediate_or_outbox(msg.chat_id, resp, reply_to_message_id=msg.message_id)
            return None

        if text in ("/help", "/start"):
            help_text = (
                "🚀 *PocketFleet Commands*\n"
                "• Send your prompt directly to dispatch to active engine.\n"
                "• `/mode fleet` — two real agents: plan, build, verify\n"
                "• `/mode codex` — Switch to 🤖 Local OpenAI Codex\n"
                "• `/mode agy` — Switch to 🌈 Local Google Antigravity\n"
                "• `/mode sim` — preview only; no agent or tests\n"
                "• `/status` — View real-time daemon & task progress"
            )
            self._send_immediate_or_outbox(msg.chat_id, help_text, reply_to_message_id=msg.message_id)
            return None


        # --- Deduplication Check via Persistent SQLite (P0-3 Fix) ---
        if self.state_store.is_message_processed(msg.message_id, chat_id=msg.chat_id):
            logger.info("Skipped already processed message ID %s in chat %s", msg.message_id, msg.chat_id)
            return None

        # Parse worker and prompt
        worker_type, prompt = self.parse_command(text)
        if mentioned_worker is not None and worker_type == self.default_worker:
            worker_type = mentioned_worker

        if not prompt:
            return None

        raw_prompt = prompt
        enveloped_prompt = self._format_telegram_envelope(
            prompt=raw_prompt,
            target_worker=worker_type,
            sender_name=msg.sender_name,
            sender_id=msg.sender_id,
            chat_title=msg.chat_title,
        )

        task = Task(
            prompt=enveloped_prompt,
            raw_prompt=raw_prompt,
            worker=worker_type,
            chat_id=msg.chat_id,
            inbound_message_id=msg.message_id,
        )

        executor = self.select_worker(worker_type)
        if not executor:
            err_msg = (
                f"❌ *Worker Unavailable*: `{worker_type.value}` is not ready. "
                "PocketFleet did not substitute a simulator or another identity. "
                "Check `/status` and configure the requested real executor."
            )
            self._send_immediate_or_outbox(msg.chat_id, err_msg, reply_to_message_id=msg.message_id)
            task.mark_failed(err_msg, exit_code=127)
            self.state_store.record_message_start(msg.message_id, msg.chat_id, raw_prompt, worker_type.value)
            self.state_store.record_message_finish(msg.message_id, "FAILED", 127, chat_id=msg.chat_id)
            return task

        # If targeting antigravity / lead seat, route exclusively through SessionHub (No second writer!)
        if executor.name == "antigravity":
            # 60-Minute Launch Handshake: notify active IDE session if interval elapsed
            try:
                from .antigravity_tracks import notify_ide_launch
                sess = self.session_hub.get_session("lead")
                cid = sess.conversation_id if sess else os.environ.get("POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID", "")
                if cid and self.workspace_cwd:
                    notify_ide_launch(
                        conversation_id=cid,
                        workspace_cwd=self.workspace_cwd,
                        initial_prompt=raw_prompt,
                        state_store=self.state_store,
                    )
            except Exception as notify_err:
                logger.debug("notify_ide_launch error: %s", notify_err)

            idemp_key = f"tg:{msg.chat_id}:{msg.message_id}"
            try:
                event = self.session_hub.enqueue_task(
                    seat_id="lead",
                    prompt=enveloped_prompt,
                    source="telegram",
                    idempotency_key=idemp_key,
                    reply_chat_id=msg.chat_id,
                    reply_message_id=msg.message_id,
                )
            except Exception as eq_err:
                logger.error("Failed to enqueue task to SessionHub: %s", eq_err)
                err_msg = f"❌ *Task Enqueue Failed*: {eq_err}"
                self._send_immediate_or_outbox(msg.chat_id, err_msg, reply_to_message_id=msg.message_id)
                task.mark_failed(str(eq_err), exit_code=1)
                self.state_store.record_message_start(msg.message_id, msg.chat_id, raw_prompt, "antigravity")
                self.state_store.record_message_finish(msg.message_id, "FAILED", 1, chat_id=msg.chat_id)
                # Fail Closed: Antigravity enqueue failure MUST return, never fall back to legacy work_queue!
                return task

            self._pending_tg_events[event.event_id] = (msg.chat_id, msg.message_id, raw_prompt, time.time())
            self.state_store.record_message_start(msg.message_id, msg.chat_id, raw_prompt, "antigravity")

            is_busy = False
            try:
                sess = self.session_hub.get_session("lead")
                if sess and sess.status == "busy":
                    is_busy = True
            except Exception:
                pass

            if is_busy or event.status == "queued":
                recent = self.session_hub.get_recent_events(limit=20, seat_id="lead")
                q_size = sum(1 for e in recent if e.status == "queued")
                queue_ack = (
                    f"⏳ *Task Queued* (#{max(1, q_size)} in line)\n"
                    f"Event: `{event.event_id[:8]}`\n"
                    f"席位 `lead` 单写者排队施工中，将严格串行执行。\n"
                    f"`{task.display_prompt[:60]}...`"
                )
                self._send_immediate_or_outbox(msg.chat_id, queue_ack, reply_to_message_id=msg.message_id)
            else:
                ack_text = f"⏳ *Task Started* [{executor.name}]\nEvent: `{event.event_id[:8]}`\n`{task.display_prompt[:100]}`"
                self._send_immediate_or_outbox(msg.chat_id, ack_text, reply_to_message_id=msg.message_id)

            # Antigravity is handled purely by the resident SessionWorker, DO NOT put into legacy work_queue!
            return task

        # Check if another task is currently executing
        with self._status_lock:
            is_busy = self.current_running_task is not None

        if is_busy:
            q_size = self.work_queue.qsize() + 1
            queue_ack = (
                f"⏳ *Task Queued* (#{q_size} in line)\n"
                f"Currently running: `{self.current_running_task['prompt'][:60]}...`\n"
                f"Your task `{task.display_prompt[:60]}...` will start immediately after."
            )
            self._send_immediate_or_outbox(msg.chat_id, queue_ack, reply_to_message_id=msg.message_id)
        else:
            ack_text = f"⏳ *Task Started* [{executor.name}]\n`{task.display_prompt[:100]}`"
            self._send_immediate_or_outbox(msg.chat_id, ack_text, reply_to_message_id=msg.message_id)

        # Enqueue to decoupled worker queue
        self.work_queue.put((task, executor, msg))
        return task


    def _poll_and_report_events(self) -> None:
        """Poll terminal events from event_ledger and deliver results to TG & Web (Atomic once-only)."""
        try:
            events = self.session_hub.get_undelivered_tg_events(limit=50)
        except Exception as exc:
            logger.debug("Error fetching undelivered TG events: %s", exc)
            return

        for ev in events:
            # 1. Retrieve persistent routing information
            chat_id = ev.reply_chat_id
            reply_to_id = ev.reply_message_id

            # Fallback for events enqueued before schema migration
            if not chat_id and ev.idempotency_key:
                if ev.idempotency_key.startswith("tg:"):
                    parts = ev.idempotency_key.split(":")
                    if len(parts) >= 3:
                        try:
                            chat_id = int(parts[1])
                            reply_to_id = int(parts[2])
                        except ValueError:
                            pass
                elif ev.idempotency_key.startswith("tg_"):
                    try:
                        reply_to_id = int(ev.idempotency_key[3:])
                    except ValueError:
                        pass

            if not chat_id:
                pending_info = self._pending_tg_events.pop(ev.event_id, None)
                if pending_info:
                    chat_id, reply_to_id, _, _ = pending_info

            is_noreply = False
            p_lower = (ev.prompt or "").lower()
            if "[noreply]" in p_lower or "【免回】" in (ev.prompt or "") or "mode:[noreply]" in p_lower:
                is_noreply = True

            if ev.status == "completed":
                res_preview = (ev.response or "")[-1500:] if len(ev.response or "") > 1500 else (ev.response or "Success")
                if is_noreply:
                    res_preview = defang_telegram_mentions(res_preview)
                    reply_text = (
                        f"📋 *【战队协同 · 执行结案（免回）】*\n"
                        f"🎯 *任务席位*：`{ev.seat_id or 'lead'}` (`{ev.event_id[:8]}`)\n\n"
                        f"```text\n{res_preview}\n```"
                    )
                else:
                    reply_text = (
                        f"✅ *Task Completed* [lead:antigravity]\n"
                        f"Event: `{ev.event_id[:8]}`\n"
                        f"```text\n{res_preview}\n```"
                    )
            else:
                err_preview = (ev.error or ev.response or "Unknown error")[-1000:]
                if is_noreply:
                    err_preview = defang_telegram_mentions(err_preview)
                reply_text = (
                    f"❌ *Task Failed* [lead:antigravity] (exit code {ev.exit_code or 1})\n"
                    f"Event: `{ev.event_id[:8]}`\n"
                    f"```text\n{err_preview}\n```"
                )

            # Strict Order: Only after successfully sending or writing to durable outbox, record delivery!
            if chat_id:
                try:
                    self._send_immediate_or_outbox(chat_id, reply_text, reply_to_message_id=reply_to_id)
                    # Atomically claim telegram delivery right via event_deliveries table
                    self.session_hub.try_record_event_delivery(ev.event_id, destination="telegram")
                    if reply_to_id:
                        st = "COMPLETED" if ev.status == "completed" else "FAILED"
                        ec = 0 if ev.status == "completed" else (ev.exit_code or 1)
                        self.state_store.record_message_finish(reply_to_id, st, ec, chat_id=chat_id)
                except Exception as send_err:
                    logger.error("Failed to dispatch report for event %s: %s", ev.event_id, send_err)
                    continue
            else:
                # No destination chat ID found, mark delivered to prevent endless retries
                self.session_hub.try_record_event_delivery(ev.event_id, destination="telegram")

            # Update Web Cockpit telemetry
            c_dur = round((ev.completed_at - ev.created_at), 2) if (ev.completed_at and ev.created_at) else 0.0
            telemetry.record_task(
                prompt=ev.prompt,
                worker=ev.seat_id,
                status=ev.status.upper(),
                duration_sec=c_dur,
                preview=(ev.response or ev.error or "")[:120],
            )

    def _worker_loop(self) -> None:
        """Dedicated execution / reporting loop running in background thread."""
        logger.info("PocketFleet Worker Execution Thread started.")
        while self.running:
            try:
                item = self.work_queue.get(timeout=0.5)
            except queue.Empty:
                self._poll_and_report_events()
                continue

            if item is None:
                break

            task, executor, msg = item
            prompt = task.prompt
            self._poll_and_report_events()

            # Non-antigravity fallback path (for test mocks / simulated workers)
            with self._status_lock:
                self.current_running_task = {
                    "task_id": task.task_id,
                    "prompt": prompt,
                    "worker": executor.name,
                    "start_time": time.time(),
                }

            task.mark_running()
            self.state_store.record_message_start(msg.message_id, msg.chat_id, prompt, executor.name)
            telemetry.record_task(
                prompt=prompt,
                worker=executor.name,
                status="RUNNING",
                duration_sec=0.0,
                preview="Executing agent in background...",
            )
            logger.info("Executing task %s with worker %s", task.task_id, executor.name)

            start_time = time.time()
            _lead_executor, _builder_executor, lead_name, builder_name = self.get_role_bindings()
            try:
                if hasattr(executor, "execute_with_phases"):
                    def _stream_phase(p_text: str, role: str = "lead"):
                        self._send_immediate_or_outbox(
                            chat_id=msg.chat_id,
                            text=p_text,
                            reply_to_message_id=msg.message_id,
                        )
                    code, stdout, stderr = executor.execute_with_phases(
                        prompt, cwd=self.workspace_cwd, on_phase=_stream_phase,
                        lead_name=f"任务负责人 · {lead_name}", builder_name=f"主力程序员 · {builder_name}"
                    )
                else:
                    code, stdout, stderr = executor.execute(prompt, cwd=self.workspace_cwd)
            except Exception as run_err:
                code = 1
                stdout = ""
                stderr = f"Internal execution error: {run_err}"
            duration = time.time() - start_time

            with self._status_lock:
                self.current_running_task = None

            is_noreply = False
            p_lower = (prompt or "").lower()
            if "[noreply]" in p_lower or "【免回】" in (prompt or "") or "mode:[noreply]" in p_lower:
                is_noreply = True

            if code == 0:
                task.mark_completed(stdout, exit_code=0)
                self.state_store.record_message_finish(msg.message_id, "COMPLETED", 0)
                if executor.name == "fleet_triad":
                    reply_text = defang_telegram_mentions(stdout) if is_noreply else stdout
                else:
                    res_preview = stdout[-1500:] if len(stdout) > 1500 else stdout
                    if is_noreply:
                        res_preview = defang_telegram_mentions(res_preview)
                        reply_text = (
                            f"📋 *【战队协同 · 执行结案（免回）】*\n"
                            f"🎯 *执行席*：[{executor.name}]\n\n"
                            f"```text\n{res_preview}\n```"
                        )
                    else:
                        reply_text = (
                            f"✅ *Task Completed* [{executor.name}]\n"
                            f"```text\n{res_preview}\n```"
                        )

                telemetry.record_task(
                    prompt=prompt,
                    worker=executor.name,
                    status="COMPLETED",
                    duration_sec=duration,
                    preview=stdout[-150:] if stdout else "Success",
                )
            else:
                task.mark_failed(stderr or stdout, exit_code=code)
                self.state_store.record_message_finish(msg.message_id, "FAILED", code)
                err_preview = (stderr or stdout)[-1000:]
                if is_noreply:
                    err_preview = defang_telegram_mentions(err_preview)
                reply_text = (
                    f"❌ *Task Failed* [{executor.name}] (exit code {code})\n"
                    f"```text\n{err_preview}\n```"
                )
                telemetry.record_task(
                    prompt=prompt,
                    worker=executor.name,
                    status="FAILED",
                    duration_sec=duration,
                    preview=(stderr or stdout)[-150:] if (stderr or stdout) else f"Exit code {code}",
                )

            self._send_immediate_or_outbox(
                chat_id=msg.chat_id,
                text=reply_text,
                reply_to_message_id=msg.message_id,
            )

            self.work_queue.task_done()

        logger.info("PocketFleet Worker Execution Thread terminated.")

    def step(self) -> int:
        """Run one single poll tick, flush outbox, and poll completed events."""
        # 1. Drain pending outbox retries (P0-2)
        try:
            self.flush_outbox()
        except Exception as outbox_err:
            logger.warning("Outbox flush error: %s", outbox_err)

        # 2. Check meeting watchdog
        try:
            self._check_meeting_watchdog()
        except Exception as wd_err:
            logger.debug("Meeting watchdog error: %s", wd_err)

        # 3. Poll completed events and deliver results
        try:
            self._poll_and_report_events()
        except Exception as rep_err:
            logger.warning("Event report error: %s", rep_err)

        # 3. Poll incoming Telegram messages
        messages = []
        try:
            messages = self.transport.poll_messages(timeout_sec=3)
        except Exception as exc:
            logger.warning("Error during transport poll tick: %s", exc)

        count = 0
        for msg in messages:
            try:
                task = self.handle_message(msg)
                if task:
                    count += 1
            except Exception as task_exc:
                logger.error("Error handling message %s: %s", getattr(msg, "message_id", "unknown"), task_exc)

        # 4. Poll secondary transports for other configured seats (e.g. chat seat / 结算主机)
        if getattr(self, "secondary_transports", None):
            for s_role, s_trans in self.secondary_transports.items():
                try:
                    s_msgs = s_trans.poll_messages(timeout_sec=1)
                    for sm in s_msgs:
                        try:
                            task = self.handle_message(sm, forced_seat=s_role)
                            if task:
                                count += 1
                        except Exception as s_task_exc:
                            logger.error("Error handling secondary message %s on seat %s: %s", getattr(sm, "message_id", "unknown"), s_role, s_task_exc)
                except Exception as s_exc:
                    logger.debug("Secondary transport %s poll error: %s", s_role, s_exc)

        return count

    def run_forever(self, poll_interval: float = 1.0) -> None:
        self.running = True
        logger.info("PocketFleet Decoupled DispatchLoop active. Listening for Telegram tasks...")

        # Start decoupled background worker thread (P0-1)
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        consecutive_errors = 0
        while self.running:
            try:
                self.step()
                consecutive_errors = 0
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Received interrupt signal. Shutting down gracefully...")
                self.stop()
                break
            except Exception as loop_exc:
                consecutive_errors += 1
                backoff = min(30, max(2, consecutive_errors * 2))
                logger.error(
                    "Unexpected loop error (consecutive: %d): %s. Backing off for %ds...",
                    consecutive_errors,
                    loop_exc,
                    backoff,
                )
                time.sleep(backoff)

    def stop(self) -> None:
        self.running = False
        self.work_queue.put(None)
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3.0)
        logger.info("PocketFleet DispatchLoop stopped.")
