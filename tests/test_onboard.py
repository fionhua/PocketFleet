import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketfleet.core import InboundMessage
from pocketfleet.loop import DispatchLoop
from pocketfleet.onboard import FleetConfig, detect_installed_agents, verify_bot_token


def test_fleet_config_save_and_load(tmp_path: Path):
    cfg_file = tmp_path / "test_config.json"
    cfg = FleetConfig(
        bot_token="123456:ABC-DEF",
        allowed_chat_id=987654321,
        bot_username="TestSquadBot",
        default_worker="aider",
        workspace_cwd="/tmp/workspace",
    )
    saved_path = cfg.save(cfg_file)
    assert saved_path == cfg_file
    assert cfg_file.is_file()

    loaded = FleetConfig.load(cfg_file)
    assert loaded is not None
    assert loaded.bot_token == "123456:ABC-DEF"
    assert loaded.allowed_chat_id == 987654321
    assert loaded.bot_username == "TestSquadBot"
    assert loaded.default_worker == "aider"


def test_detect_installed_agents():
    agents = detect_installed_agents()
    assert isinstance(agents, dict)
    assert "claude_code" in agents
    assert "aider" in agents


def test_dispatch_loop_security_whitelist():
    transport = MagicMock()
    # Lock to authorized chat ID 1001
    loop = DispatchLoop(
        transport=transport,
        allowed_chat_ids={1001},
    )

    # 1. Message from unauthorized user (chat ID 9999) -> BLOCKED
    unauth_msg = InboundMessage(
        message_id=1,
        chat_id=9999,
        sender_id=9999,
        sender_name="Hacker",
        text="/claude delete all files",
    )
    task1 = loop.handle_message(unauth_msg)
    assert task1 is None

    # 2. Message from authorized developer (chat ID 1001) -> ACCEPTED
    auth_msg = InboundMessage(
        message_id=2,
        chat_id=1001,
        sender_id=1001,
        sender_name="Commander",
        text="fix issue in core",
    )
    task2 = loop.handle_message(auth_msg)
    assert task2 is not None
    assert task2.prompt == "fix issue in core"
