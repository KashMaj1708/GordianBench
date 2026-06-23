"""LLMProvider protocol — the only swappable part of the agent loop."""

from __future__ import annotations

from typing import Protocol

from agent.types import AssistantTurn, Message
from agent.tools import ToolDef


class LLMProvider(Protocol):
    def complete(
        self,
        history: list[Message],
        tools: list[ToolDef],
        *,
        system: str = "",
    ) -> AssistantTurn:
        """One model turn → canonical AssistantTurn."""
