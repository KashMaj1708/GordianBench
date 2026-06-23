"""Anthropic Messages API adapter — reference LLMProvider implementation."""

from __future__ import annotations

import os
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


def _map_stop_reason(raw: str) -> StopReason:
    if raw in ("tool_use", "tool_calls"):
        return StopReason.WANTS_TOOL
    if raw in ("max_tokens", "length"):
        return StopReason.TRUNCATED
    return StopReason.DONE


def _to_anthropic_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def _serialize_messages(history: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in history:
        if msg.role == Role.TOOL:
            for block in msg.content:
                if hasattr(block, "tool_call_id"):
                    out.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_call_id,
                                    "content": block.output,
                                    "is_error": block.is_error,
                                }
                            ],
                        }
                    )
            continue
        content: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, str):
                content.append({"type": "text", "text": block})
            elif isinstance(block, ToolCall):
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        role = "user" if msg.role == Role.USER else "assistant"
        out.append({"role": role, "content": content})
    return out


class AnthropicProvider:
    """Maps canonical types ↔ Anthropic SDK at the boundary only."""

    def __init__(self, *, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def complete(
        self,
        history: list[Message],
        tools: list[ToolDef],
        *,
        system: str = "",
    ) -> AssistantTurn:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            tools=_to_anthropic_tools(tools),
            messages=_serialize_messages(history),
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=dict(block.input))
                )

        usage = Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
        )
        stop = _map_stop_reason(response.stop_reason or "end_turn")
        if tool_calls and stop == StopReason.DONE:
            stop = StopReason.WANTS_TOOL

        return AssistantTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
        )
