"""Base Transport Interface for PocketFleet"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..core import InboundMessage, OutboundMessage


class BaseTransport(ABC):
    @abstractmethod
    def poll_messages(self, timeout_sec: int = 10) -> Sequence[InboundMessage]:
        """Poll new inbound messages from the channel."""
        raise NotImplementedError

    @abstractmethod
    def send_message(self, message: OutboundMessage) -> bool:
        """Send an outbound message to the channel."""
        raise NotImplementedError
