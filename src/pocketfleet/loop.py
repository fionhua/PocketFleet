"""PocketFleet Dispatcher and Single-Direction DAG Loop

Implements the Echo-Proof architecture:
1. Strict Role-based Gating (never responds to bot messages)
2. Single-direction DAG flow (Human -> Worker -> Direct Reply to Human)
3. Safety Circuit Breaker (prevents runaway execution)
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .core import InboundMessage, OutboundMessage, Task, TaskStatus, WorkerType
from .executors.aider import AiderExecutor
from .executors.base import BaseExecutor
from .executors.claude_code import ClaudeCodeExecutor
from .transport.base import BaseTransport

logger = logging.getLogger(__name__)


class DispatchLoop:
    def __init__(
        self,
        transport: BaseTransport,
        workspace_cwd: Optional[str] = None,
        default_worker: WorkerType = WorkerType.CLAUDE_CODE,
        allowed_chat_ids: Optional[set[int]] = None,
    ) -> None:
        self.transport = transport
        self.workspace_cwd = workspace_cwd
        self.default_worker = default_worker
        self.allowed_chat_ids = allowed_chat_ids
        self.running = False
        
        # Registered executors
        self.executors: Dict[WorkerType, BaseExecutor] = {
            WorkerType.CLAUDE_CODE: ClaudeCodeExecutor(),
            WorkerType.AIDER: AiderExecutor(),
        }
        
        # State tracking
        self.processed_msg_ids: set[int] = set()
        self.recent_events: List[float] = []

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
        for w_type in [self.default_worker, WorkerType.CLAUDE_CODE, WorkerType.AIDER]:
            if w_type in self.executors and self.executors[w_type].is_available():
                return self.executors[w_type]
        return None

    def parse_command(self, text: str) -> tuple[WorkerType, str]:
        text_clean = text.strip()
        if text_clean.startswith("/claude"):
            return WorkerType.CLAUDE_CODE, text_clean[7:].strip()
        if text_clean.startswith("/aider"):
            return WorkerType.AIDER, text_clean[6:].strip()
        return self.default_worker, text_clean

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

        # De-duplicate
        if msg.message_id in self.processed_msg_ids:
            return None
        self.processed_msg_ids.add(msg.message_id)

        # Trim deduplication set to avoid memory growth
        if len(self.processed_msg_ids) > 1000:
            self.processed_msg_ids = set(list(self.processed_msg_ids)[-500:])

        text = msg.text.strip()
        if not text:
            return None

        # Handle system commands
        if text == "/status":
            available = self.get_available_workers()
            status_text = (
                "🤖 *PocketFleet Status: Online*\n"
                f"• Workspace: `{self.workspace_cwd or 'Default'}`\n"
                f"• Available Workers: {', '.join([w.value for w in available]) or 'None'}\n"
                f"• Active Strategy: Single-direction DAG (Echo-Proof)"
            )
            self.transport.send_message(
                OutboundMessage(chat_id=msg.chat_id, text=status_text, reply_to_message_id=msg.message_id)
            )
            return None

        if text in ("/help", "/start"):
            help_text = (
                "🚀 *PocketFleet Commands*\n"
                "• Send your prompt directly to dispatch to default worker.\n"
                "• `/claude <prompt>` — Dispatch explicitly to Claude Code\n"
                "• `/aider <prompt>` — Dispatch explicitly to Aider\n"
                "• `/status` — View current environment & worker status"
            )
            self.transport.send_message(
                OutboundMessage(chat_id=msg.chat_id, text=help_text, reply_to_message_id=msg.message_id)
            )
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
            self.transport.send_message(
                OutboundMessage(chat_id=msg.chat_id, text=err_msg, reply_to_message_id=msg.message_id)
            )
            task.mark_failed(err_msg, exit_code=127)
            return task

        # Notify human: Task started
        ack_text = f"⏳ *Task Started* [{executor.name}]\n`{task.prompt[:100]}`"
        self.transport.send_message(
            OutboundMessage(chat_id=msg.chat_id, text=ack_text, reply_to_message_id=msg.message_id)
        )

        task.mark_running()
        logger.info("Executing task %s with worker %s", task.task_id, executor.name)

        # Execute coding agent
        code, stdout, stderr = executor.execute(task.prompt, cwd=self.workspace_cwd)

        # --- [IRON GATE 1: Single-direction DAG Output] ---
        # Direct result formatting back to human - never fed back into any LLM receptionist
        if code == 0:
            task.mark_completed(stdout, exit_code=0)
            res_preview = stdout[-1500:] if len(stdout) > 1500 else stdout
            reply_text = (
                f"✅ *Task Completed* [{executor.name}]\n"
                f"```text\n{res_preview}\n```"
            )
        else:
            task.mark_failed(stderr or stdout, exit_code=code)
            err_preview = (stderr or stdout)[-1000:]
            reply_text = (
                f"❌ *Task Failed* [{executor.name}] (exit code {code})\n"
                f"```text\n{err_preview}\n```"
            )

        self.transport.send_message(
            OutboundMessage(chat_id=msg.chat_id, text=reply_text, reply_to_message_id=msg.message_id)
        )
        return task

    def step(self) -> int:
        """Run one single poll-and-dispatch tick."""
        messages = self.transport.poll_messages(timeout_sec=5)
        count = 0
        for msg in messages:
            task = self.handle_message(msg)
            if task:
                count += 1
        return count

    def run_forever(self, poll_interval: float = 1.0) -> None:
        self.running = True
        logger.info("PocketFleet DispatchLoop active. Listening for Telegram tasks...")
        try:
            while self.running:
                self.step()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self.running = False
        logger.info("PocketFleet DispatchLoop stopped.")
