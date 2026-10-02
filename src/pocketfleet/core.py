"""PocketFleet Core Contracts and Data Models

Zero external dependency dataclasses defining tasks, messages, and state machines.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class WorkerType(str, Enum):
    FLEET_TRIAD = "fleet_triad"
    SIMULATION = "simulation"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    CLAUDE_CODE = "claude_code"
    AIDER = "aider"
    AUTO = "auto"



@dataclass
class InboundMessage:
    message_id: int
    chat_id: int
    sender_id: int
    sender_name: str
    text: str
    is_bot: bool = False
    timestamp: float = field(default_factory=time.time)
    chat_type: str = "group"
    chat_title: str = ""
    sender_chat_id: int | None = None
    is_anonymous: bool = False


@dataclass
class RoleAssignment:
    lead: str = "antigravity"
    builder: str = "codex"


@dataclass
class OutboundMessage:
    chat_id: int
    text: str
    reply_to_message_id: int | None = None
    parse_mode: str = "Markdown"



@dataclass
class Task:
    prompt: str
    worker: WorkerType = WorkerType.AUTO
    raw_prompt: str = ""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: TaskStatus = TaskStatus.PENDING
    chat_id: int = 0
    inbound_message_id: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result_text: str = ""
    error_message: str = ""
    exit_code: int | None = None

    @property
    def display_prompt(self) -> str:
        return self.raw_prompt if self.raw_prompt else self.prompt

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()

    def mark_completed(self, result: str, exit_code: int = 0) -> None:
        self.status = TaskStatus.COMPLETED
        self.result_text = result
        self.exit_code = exit_code
        self.completed_at = time.time()

    def mark_failed(self, error: str, exit_code: int = 1) -> None:
        self.status = TaskStatus.FAILED
        self.error_message = error
        self.exit_code = exit_code
        self.completed_at = time.time()


# ==============================================================================
# PF-01: Three-Seat Configuration Layer (三席位配置层)
# ==============================================================================
import json
import re
from pathlib import Path


class SeatRole(str, Enum):
    CHAT = "chat"          # 对话AI
    LEAD = "lead"          # 施工指挥 (原任务负责人)
    BUILDER = "builder"    # 主力程序员


ALLOWED_CHAT_ENGINES: tuple[str, ...] = ("gemini", "chatgpt", "claude")
ALLOWED_CODE_ENGINES: tuple[str, ...] = ("codex", "antigravity", "claude_code", "aider", "copilot")

DEFAULT_ENGINE_COMMANDS: dict[str, str] = {
    "antigravity": "agy",
    "gemini": "gemini",
    "codex": "codex",
    "claude_code": "claude",
    "aider": "aider",
    "copilot": "copilot",
    "chatgpt": "chatgpt",
    "claude": "claude",
}


def get_default_command_for_engine(engine: str) -> str:
    """Return default CLI executable command name associated with an engine."""
    eng_clean = (engine or "").lower().strip()
    return DEFAULT_ENGINE_COMMANDS.get(eng_clean, eng_clean)


_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class SeatConfig:
    role: str
    name: str
    engine: str
    bot_token_env: str = ""
    bot_username: str = ""
    description: str = ""
    command: str = ""
    read_watermark: int = 0
    bot_token: str = ""

    def get_token(self) -> str:
        """Resolve the effective bot token, checking direct bot_token first, then environment variable."""
        if self.bot_token:
            return self.bot_token
        if self.bot_token_env:
            return (os.environ.get(self.bot_token_env) or "").strip()
        return ""

    def to_dict(self) -> dict:
        d = {
            "role": self.role,
            "name": self.name,
            "engine": self.engine,
            "bot_token_env": self.bot_token_env,
            "bot_username": self.bot_username,
            "description": self.description,
            "command": self.command,
            "read_watermark": self.read_watermark,
        }
        if self.bot_token:
            d["bot_token"] = self.bot_token
        return d

    @classmethod
    def from_dict(cls, data: dict) -> SeatConfig:
        engine = str(data.get("engine", "")).strip().lower()
        cmd = str(data.get("command", "")).strip() or get_default_command_for_engine(engine)
        return cls(
            role=str(data.get("role", "")).strip(),
            name=str(data.get("name", "")).strip(),
            engine=engine,
            bot_token_env=str(data.get("bot_token_env", "")).strip(),
            bot_username=str(data.get("bot_username", "")).strip(),
            description=str(data.get("description", "")).strip(),
            command=cmd,
            read_watermark=int(data.get("read_watermark", 0)),
            bot_token=str(data.get("bot_token") or data.get("token") or "").strip(),
        )


@dataclass
class FleetSeatsConfig:
    seats: dict[str, SeatConfig]
    context_window: int = 20
    no_situ: bool = True
    authorized_user_ids: list[int] = field(default_factory=list)
    telegram_chat_id: str = ""
    telegram_group_name: str = ""
    sync_context_window: bool = True
    chat_sentinel_enabled: bool = False
    chat_sentinel_interval: int = 15

    def to_dict(self) -> dict:
        return {
            "context_window": self.context_window,
            "no_situ": self.no_situ,
            "authorized_user_ids": self.authorized_user_ids,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_group_name": self.telegram_group_name,
            "sync_context_window": self.sync_context_window,
            "chat_sentinel_enabled": self.chat_sentinel_enabled,
            "chat_sentinel_interval": self.chat_sentinel_interval,
            "seats": {role: seat.to_dict() for role, seat in self.seats.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> FleetSeatsConfig:
        seats_raw = data.get("seats")
        if not isinstance(seats_raw, dict):
            raise ValueError("FleetSeatsConfig data must contain a 'seats' dictionary.")

        seats: dict[str, SeatConfig] = {}
        for role_key, seat_data in seats_raw.items():
            if not isinstance(seat_data, dict):
                raise ValueError(f"Seat '{role_key}' data must be a dictionary.")
            seat_obj = SeatConfig.from_dict(seat_data)
            if not seat_obj.role:
                seat_obj.role = role_key
            seats[role_key] = seat_obj

        uids = data.get("authorized_user_ids", [])
        if not isinstance(uids, list):
            uids = []
        uids_clean = [int(u) for u in uids if str(u).lstrip("-").isdigit()]

        config = cls(
            seats=seats,
            context_window=int(data.get("context_window", 20)),
            no_situ=bool(data.get("no_situ", True)),
            authorized_user_ids=uids_clean,
            telegram_chat_id=str(data.get("telegram_chat_id", "")).strip(),
            telegram_group_name=str(data.get("telegram_group_name", "")).strip(),
            sync_context_window=bool(data.get("sync_context_window", True)),
            chat_sentinel_enabled=bool(data.get("chat_sentinel_enabled", False)),
            chat_sentinel_interval=int(data.get("chat_sentinel_interval", 15)),
        )
        validate_seats_config(config)
        return config

    def save_to_file(self, path: Path | str) -> None:
        validate_seats_config(self)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load_from_file(cls, path: Path | str) -> FleetSeatsConfig:
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Configuration file not found: {target}")
        data = json.loads(target.read_text(encoding="utf-8"))
        return cls.from_dict(data)


def validate_seats_config(config: FleetSeatsConfig) -> None:
    """Rigorous validator ensuring fail-loud behavior without silent downgrades."""
    if not isinstance(config.context_window, int) or config.context_window <= 0:
        raise ValueError(
            f"context_window (最近消息条数) must be a positive integer, got: {config.context_window}"
        )

    required_roles = {SeatRole.CHAT.value, SeatRole.LEAD.value, SeatRole.BUILDER.value}
    actual_roles = set(config.seats.keys())

    missing = required_roles - actual_roles
    if missing:
        raise ValueError(f"Missing required seat(s): {', '.join(sorted(missing))}")

    seen_token_envs: dict[str, str] = {}
    seen_usernames: dict[str, str] = {}

    for role_name, seat in config.seats.items():
        is_extended_code_seat = role_name.startswith("code_") or role_name.startswith("builder_")
        if role_name not in required_roles and not is_extended_code_seat:
            raise ValueError(f"Unknown seat role '{role_name}'. Allowed: {', '.join(sorted(required_roles))} or code_*")

        if seat.role != role_name:
            raise ValueError(f"Seat role '{seat.role}' does not match dictionary key '{role_name}'")

        if not seat.name:
            raise ValueError(f"Seat '{role_name}' has empty name.")

        # Username validation
        username = seat.bot_username.strip()
        if not username:
            raise ValueError(f"Seat '{role_name}' must specify a non-empty bot_username.")

        if username in seen_usernames:
            prev_role = seen_usernames[username]
            raise ValueError(
                f"Duplicate bot_username '{username}' detected across seats: '{prev_role}' and '{role_name}'"
            )
        seen_usernames[username] = role_name

        # Engine validation
        if role_name == SeatRole.CHAT.value:
            if seat.engine not in ALLOWED_CHAT_ENGINES:
                raise ValueError(
                    f"Invalid chat engine '{seat.engine}' for seat '{role_name}'. Allowed: {', '.join(ALLOWED_CHAT_ENGINES)}"
                )
        else:
            if seat.engine not in ALLOWED_CODE_ENGINES:
                raise ValueError(
                    f"Invalid code engine '{seat.engine}' for seat '{role_name}'. Allowed: {', '.join(ALLOWED_CODE_ENGINES)}"
                )

        # Token ENV security validation: forbid plain-text tokens in bot_token_env
        token_env = (seat.bot_token_env or "").strip()
        if not token_env and not seat.bot_token:
            raise ValueError(f"Seat '{role_name}' must specify a bot_token_env.")

        if ":" in token_env or not _ENV_VAR_PATTERN.match(token_env):
            raise ValueError(
                f"bot_token_env for seat '{role_name}' must be a valid environment variable name, not a plain token! Offending: '{token_env}'"
            )

        if token_env in seen_token_envs:
            prev_role = seen_token_envs[token_env]
            raise ValueError(
                f"Duplicate bot_token_env '{token_env}' detected across seats: '{prev_role}' and '{role_name}'"
            )
        seen_token_envs[token_env] = role_name


def get_default_seats_config() -> FleetSeatsConfig:
    """Default trial triad preset: 地球Sandbox(Gemini) / 裁决者(Antigravity) / 泥蛇(Codex)."""
    return FleetSeatsConfig(
        context_window=20,
        no_situ=True,
        authorized_user_ids=[],
        seats={
            SeatRole.CHAT.value: SeatConfig(
                role=SeatRole.CHAT.value,
                name="地球Sandbox",
                engine="gemini",
                bot_token_env="TELEGRAM_BOT_SANDBOX_TOKEN",
                bot_username="@AiSoulAlphaSandboxBot",
                description="对话AI·推演与宏观对账",
                command="gemini",
                read_watermark=0,
            ),
            SeatRole.LEAD.value: SeatConfig(
                role=SeatRole.LEAD.value,
                name="裁决者",
                engine="antigravity",
                bot_token_env="TELEGRAM_BOT_JUDGE_TOKEN",
                bot_username="@AiSoulJudgeBot",
                description="施工指挥·架构守门与改卷验收",
                command="agy",
                read_watermark=0,
            ),
            SeatRole.BUILDER.value: SeatConfig(
                role=SeatRole.BUILDER.value,
                name="泥蛇",
                engine="codex",
                bot_token_env="TELEGRAM_BOT_MUDSNAKE_TOKEN",
                bot_username="@AiSoulMudSnakeBot",
                description="主力程序员·核心施工与算法定桩",
                command="codex",
                read_watermark=0,
            ),
        },
    )
