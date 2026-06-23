"""Shared OpenAI-compatible Chat Completions adapter (OpenAI, DeepSeek)."""

from __future__ import annotations

import json
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


def _map_stop_reason(raw: str | None, *, has_tools: bool) -> StopReason:
    if raw == "tool_calls":
        return StopReason.WANTS_TOOL
    if raw in ("length", "max_tokens"):
        return StopReason.TRUNCATED
    if has_tools:
        return StopReason.WANTS_TOOL
    return StopReason.DONE


def _to_openai_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
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
                            "role": "tool",
                            "tool_call_id": block.tool_call_id,
                            "content": block.output,
                        }
                    )
            continue

        if msg.role == Role.ASSISTANT:
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
                elif isinstance(block, ToolCall):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "\n".join(text_parts)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        texts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, str):
                texts.append(block)
        out.append({"role": "user", "content": "\n".join(texts)})
    return out


class OpenAICompatProvider:
    """Maps canonical types ↔ OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        provider_name: str = "openai",
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.provider_name = provider_name

    def complete(
        self,
        history: list[Message],
        tools: list[ToolDef],
        *,
        system: str = "",
    ) -> AssistantTurn:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(_serialize_messages(history))

        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=_to_openai_tools(tools),
            tool_choice="auto",
            max_tokens=4096,
        )
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                parsed = json.loads(args) if isinstance(args, str) and args else {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, input=parsed)
                )

        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
        )
        stop = _map_stop_reason(choice.finish_reason, has_tools=bool(tool_calls))

        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
        )
