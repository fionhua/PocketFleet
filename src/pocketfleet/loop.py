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
from typing import Any, Dict, List, Optional, Set

from .cockpit import telemetry
from .core import InboundMessage, OutboundMessage, Task, TaskStatus, WorkerType
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


    def _send_immediate_or_outbox(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> None:
        """Attempt immediate transport delivery; fallback to persistent outbox on failure."""
        msg = OutboundMessage(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
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

    def handle_message(self, msg: InboundMessage) -> Optional[Task]:
        # --- [IRON GATE 2: Role-based Gating] ---
        # Never process messages originating from bots
        if msg.is_bot:
            logger.debug("Ignored bot message ID %s from %s", msg.message_id, msg.sender_name)
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

        text = msg.text.strip()
        # Clean bot mention in groups, e.g. /status@MyBot or @MyBot prompt
        import re
        text = re.sub(r"@[a-zA-Z0-9_]+bot\b", "", text, flags=re.IGNORECASE).strip()
        if not text:
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
        if not prompt:
            return None

        task = Task(
            prompt=prompt,
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
            self.state_store.record_message_start(msg.message_id, msg.chat_id, prompt, worker_type.value)
            self.state_store.record_message_finish(msg.message_id, "FAILED", 127, chat_id=msg.chat_id)
            return task

        # If targeting antigravity / lead seat, route exclusively through SessionHub (No second writer!)
        if executor.name == "antigravity":
            idemp_key = f"tg:{msg.chat_id}:{msg.message_id}"
            try:
                event = self.session_hub.enqueue_task(
                    seat_id="lead",
                    prompt=prompt,
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
                self.state_store.record_message_start(msg.message_id, msg.chat_id, prompt, "antigravity")
                self.state_store.record_message_finish(msg.message_id, "FAILED", 1, chat_id=msg.chat_id)
                # Fail Closed: Antigravity enqueue failure MUST return, never fall back to legacy work_queue!
                return task

            self._pending_tg_events[event.event_id] = (msg.chat_id, msg.message_id, prompt, time.time())
            self.state_store.record_message_start(msg.message_id, msg.chat_id, prompt, "antigravity")

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
                    f"`{task.prompt[:60]}...`"
                )
                self._send_immediate_or_outbox(msg.chat_id, queue_ack, reply_to_message_id=msg.message_id)
            else:
                ack_text = f"⏳ *Task Started* [{executor.name}]\nEvent: `{event.event_id[:8]}`\n`{task.prompt[:100]}`"
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
                f"Your task `{task.prompt[:60]}...` will start immediately after."
            )
            self._send_immediate_or_outbox(msg.chat_id, queue_ack, reply_to_message_id=msg.message_id)
        else:
            ack_text = f"⏳ *Task Started* [{executor.name}]\n`{task.prompt[:100]}`"
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

            if ev.status == "completed":
                res_preview = (ev.response or "")[-1500:] if len(ev.response or "") > 1500 else (ev.response or "Success")
                reply_text = (
                    f"✅ *Task Completed* [lead:antigravity]\n"
                    f"Event: `{ev.event_id[:8]}`\n"
                    f"```text\n{res_preview}\n```"
                )
            else:
                err_preview = (ev.error or ev.response or "Unknown error")[-1000:]
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

            if code == 0:
                task.mark_completed(stdout, exit_code=0)
                self.state_store.record_message_finish(msg.message_id, "COMPLETED", 0)
                if executor.name == "fleet_triad":
                    reply_text = stdout
                else:
                    res_preview = stdout[-1500:] if len(stdout) > 1500 else stdout
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

        # 2. Poll completed events and deliver results
        try:
            self._poll_and_report_events()
        except Exception as rep_err:
            logger.warning("Event report error: %s", rep_err)

        # 3. Poll incoming Telegram messages
        try:
            messages = self.transport.poll_messages(timeout_sec=5)
        except Exception as exc:
            logger.warning("Error during transport poll tick: %s", exc)
            return 0

        count = 0
        for msg in messages:
            try:
                task = self.handle_message(msg)
                if task:
                    count += 1
            except Exception as task_exc:
                logger.error("Error handling message %s: %s", getattr(msg, "message_id", "unknown"), task_exc)
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
