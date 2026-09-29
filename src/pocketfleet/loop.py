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
    ) -> None:
        self.transport = transport
        self.workspace_cwd = workspace_cwd
        self.default_worker = default_worker
        self.allowed_chat_ids = allowed_chat_ids
        self.state_store = state_store or StateStore()
        self.running = False

        # Registered executors
        self.executors: Dict[WorkerType, BaseExecutor] = {
            WorkerType.FLEET_TRIAD: FleetTriadExecutor(),
            WorkerType.SIMULATION: SimulationExecutor(),
            WorkerType.CODEX: CodexExecutor(),
            WorkerType.ANTIGRAVITY: AntigravityExecutor(),
            WorkerType.CLAUDE_CODE: ClaudeCodeExecutor(),
            WorkerType.AIDER: AiderExecutor(),
        }


        # Decoupled Work Queue (P0-1 Fix)
        # Holds: tuple[Task, BaseExecutor, InboundMessage]
        self.work_queue: queue.Queue[Optional[tuple[Task, BaseExecutor, InboundMessage]]] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None

        # Lock-protected status tracking
        self._status_lock = threading.Lock()
        self.current_running_task: Optional[dict[str, Any]] = None

    def get_available_workers(self) -> List[WorkerType]:
        available = []
        for w_type, executor in self.executors.items():
            if executor.is_available():
                available.append(w_type)
        return available

    def select_worker(self, requested: WorkerType) -> Optional[BaseExecutor]:
        if requested in self.executors and self.executors[requested].is_available():
            return self.executors[requested]

        # Fallback to any available
        for w_type in [self.default_worker, WorkerType.FLEET_TRIAD, WorkerType.SIMULATION, WorkerType.CODEX, WorkerType.ANTIGRAVITY, WorkerType.CLAUDE_CODE, WorkerType.AIDER]:
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

    def handle_message(self, msg: InboundMessage) -> Optional[Task]:
        # --- [IRON GATE 2: Role-based Gating] ---
        # Never process messages originating from bots
        if msg.is_bot:
            logger.debug("Ignored bot message ID %s from %s", msg.message_id, msg.sender_name)
            return None

        # --- [IRON GATE 3: Security Whitelist Check] ---
        if self.allowed_chat_ids and msg.chat_id not in self.allowed_chat_ids:
            logger.warning("Blocked message ID %s from unauthorized chat ID %s", msg.message_id, msg.chat_id)
            return None

        text = msg.text.strip()
        if not text:
            return None

        # --- Instant Non-Blocking System Commands (P0-1 Fix) ---
        if text == "/status":
            available = self.get_available_workers()
            with self._status_lock:
                running_info = self.current_running_task

            if running_info:
                elapsed = int(time.time() - running_info["start_time"])
                status_text = (
                    "🤖 *PocketFleet Status: BUSY*\n"
                    f"• Active Worker: `{running_info['worker']}`\n"
                    f"• Running Task: `{running_info['prompt'][:60]}...`\n"
                    f"• Elapsed Time: {elapsed}s\n"
                    f"• Queued Tasks: {self.work_queue.qsize()} pending\n"
                    f"• Workspace: `{self.workspace_cwd or 'Default'}`"
                )
            else:
                status_text = (
                    "🤖 *PocketFleet Status: IDLE & READY*\n"
                    f"• Workspace: `{self.workspace_cwd or 'Default'}`\n"
                    f"• Available Workers: {', '.join([w.value for w in available]) or 'None'}\n"
                    f"• Queued Tasks: {self.work_queue.qsize()} pending\n"
                    f"• Architecture: Decoupled Echo-Proof DAG"
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
                    self.default_worker = mode_map[target]
                    resp = (
                        f"🎛️ *Fleet Engine Switched to: `{self.default_worker.value}`*\n"
                        f"• Active Mode: *{self.default_worker.value.upper()}*\n"
                        "Send any requirement to see it execute!"
                    )
                else:
                    resp = f"❓ Unknown mode `{target}`. Available: `fleet`, `codex`, `agy`, `sim`, `claude`, `aider`."
            else:
                resp = (
                    f"🎛️ *Current Default Fleet Engine*: `{self.default_worker.value}`\n"
                    "• `/mode fleet` — 🌟 Fleet Triad (2 Coders + 1 Architect + 1 QA Swarm)\n"
                    "• `/mode codex` — 🤖 Local OpenAI Codex Engine\n"
                    "• `/mode agy` — 🌈 Local Google Antigravity\n"
                    "• `/mode sim` — ⚡ Fast Simulation / Echo"
                )
            self._send_immediate_or_outbox(msg.chat_id, resp, reply_to_message_id=msg.message_id)
            return None

        if text in ("/help", "/start"):
            help_text = (
                "🚀 *PocketFleet Commands*\n"
                "• Send your prompt directly to dispatch to active engine.\n"
                "• `/mode fleet` — Switch to 🌟 Fleet Triad (2 Coders + 1 Architect + 1 QA)\n"
                "• `/mode codex` — Switch to 🤖 Local OpenAI Codex\n"
                "• `/mode agy` — Switch to 🌈 Local Google Antigravity\n"
                "• `/mode sim` — Switch to ⚡ Fast Simulation / Echo\n"
                "• `/status` — View real-time daemon & task progress"
            )
            self._send_immediate_or_outbox(msg.chat_id, help_text, reply_to_message_id=msg.message_id)
            return None


        # --- Deduplication Check via Persistent SQLite (P0-3 Fix) ---
        if self.state_store.is_message_processed(msg.message_id):
            logger.info("Skipped already processed message ID %s", msg.message_id)
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
                f"❌ *Worker Unavailable*: No suitable coding agent found for `{worker_type.value}`. "
                "Ensure `claude` or `aider` CLI is installed and in system PATH."
            )
            self._send_immediate_or_outbox(msg.chat_id, err_msg, reply_to_message_id=msg.message_id)
            task.mark_failed(err_msg, exit_code=127)
            self.state_store.record_message_start(msg.message_id, msg.chat_id, prompt, worker_type.value)
            self.state_store.record_message_finish(msg.message_id, "FAILED", 127)
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

    def _worker_loop(self) -> None:
        """Dedicated single-worker execution loop running in background thread."""
        logger.info("PocketFleet Worker Execution Thread started.")
        while self.running:
            try:
                item = self.work_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:
                break

            task, executor, msg = item
            prompt = task.prompt

            # Mark task running in memory and persistent database
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
            try:
                if hasattr(executor, "execute_with_phases"):
                    def _stream_phase(p_text: str):
                        self._send_immediate_or_outbox(
                            chat_id=msg.chat_id,
                            text=p_text,
                            reply_to_message_id=msg.message_id,
                        )
                    code, stdout, stderr = executor.execute_with_phases(prompt, cwd=self.workspace_cwd, on_phase=_stream_phase)
                else:
                    code, stdout, stderr = executor.execute(prompt, cwd=self.workspace_cwd)
            except Exception as run_err:
                code = 1
                stdout = ""
                stderr = f"Internal execution error: {run_err}"
            duration = time.time() - start_time

            # Clear running status
            with self._status_lock:
                self.current_running_task = None

            # --- [IRON GATE 1: Single-direction DAG Output] ---
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

            # Guaranteed Outbox delivery
            self._send_immediate_or_outbox(
                chat_id=msg.chat_id,
                text=reply_text,
                reply_to_message_id=msg.message_id,
            )
            self.work_queue.task_done()

        logger.info("PocketFleet Worker Execution Thread terminated.")

    def step(self) -> int:
        """Run one single poll tick and flush outbox."""
        # 1. Drain pending outbox retries (P0-2)
        try:
            self.flush_outbox()
        except Exception as outbox_err:
            logger.warning("Outbox flush error: %s", outbox_err)

        # 2. Poll incoming Telegram messages
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
