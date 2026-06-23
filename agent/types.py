"""Canonical message and tool types — no vendor SDK objects past adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(str, Enum):
    """Normalized across Anthropic / OpenAI / Gemini adapters."""

    DONE = "done"
    WANTS_TOOL = "wants_tool"
    TRUNCATED = "truncated"


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    output: str
    is_error: bool = False


ContentBlock = TextBlock | ToolCall | ToolResult


@dataclass
class Message:
    role: Role
    content: list[ContentBlock]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.DONE
    usage: Usage = field(default_factory=Usage)
