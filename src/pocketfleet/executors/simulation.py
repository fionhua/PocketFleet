"""PocketFleet Simulation / Instant Test Executor

Enables zero-account, zero-token instant testing of the entire Telegram-to-Local bridge.
"""
from __future__ import annotations

import logging
import time
from typing import Tuple

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class SimulationExecutor(BaseExecutor):
    name: str = "simulation"

    def is_available(self) -> bool:
        return True

    def execute(self, prompt: str, cwd: str | None = None, timeout_sec: int = 300) -> Tuple[int, str, str]:
        time.sleep(1.0)  # Brief simulated computation
        output = (
            "[SIMULATION - NO AGENT WAS CALLED]\n"
            f"Task preview: {prompt}\n"
            f"Workspace preview: {cwd or 'Current Workspace'}\n"
            "No files were changed. No commands or tests were run."
        )
        return 0, output, ""
