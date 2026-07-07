"""Anthropic Messages API adapter — reference LLMProvider implementation."""

from __future__ import annotations

import os
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


_CACHE_CONTROL = {"type": "ephemeral"}


def _mark_cache(block: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an Anthropic content/tool block with a cache breakpoint."""
    return {**block, "cache_control": _CACHE_CONTROL}


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

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        prompt_cache: bool | None = None,
    ):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Prompt caching is on by default (cheap, big win for multi-turn/k-shot
        # runs); disable with ANTHROPIC_PROMPT_CACHE=0 or prompt_cache=False.
        if prompt_cache is None:
            prompt_cache = os.environ.get("ANTHROPIC_PROMPT_CACHE", "1") not in ("0", "false", "False")
        self.prompt_cache = prompt_cache

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

        anthropic_tools = _to_anthropic_tools(tools)
        messages = _serialize_messages(history)
        system_param: Any = system

        if self.prompt_cache:
            # Two breakpoints (<=4 allowed):
            #  1) end of the static prefix (tools -> system): one cache_control on
            #     the last system block caches BOTH tools and system, reused on
            #     every turn and across runs within the TTL.
            #  2) end of the growing conversation: marking the last message block
            #     caches the prior turns so each turn only pays full price for the
            #     newest content (cache reads are 10% of input price).
            if system:
                system_param = [_mark_cache({"type": "text", "text": system})]
            if messages:
                content = messages[-1].get("content")
                if isinstance(content, list) and content and isinstance(content[-1], dict):
                    content[-1] = _mark_cache(content[-1])

        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_param,
            tools=anthropic_tools,
            messages=messages,
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
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
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
