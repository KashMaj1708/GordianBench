"""Shared OpenAI-compatible Chat Completions adapter (OpenAI, DeepSeek).

Prompt caching is automatic on the OpenAI API: when the prefix of a request
(system prompt + tools + the earlier, unchanged turns) matches a recent request,
the shared prefix is served from cache at a discount and reported back as
``usage.prompt_tokens_details.cached_tokens``. There is no enable flag — the win
comes from keeping the prefix byte-stable, which the agent loop already does
(identical SYSTEM, identical tool schema, append-only history). We additionally
send a stable ``prompt_cache_key`` so multi-process / k-shot runs route to the
same cache, and we surface the cached-token count into ``Usage`` for reporting.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


def _is_reasoning_model(model: str) -> bool:
    """GPT-5.x and o-series are reasoning models with a different param surface.

    They reject ``max_tokens`` (require ``max_completion_tokens``), pin
    ``temperature`` to 1, and accept ``reasoning_effort``. Reasoning tokens also
    count against the completion budget, so the cap must be generous.
    """
    m = model.lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


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

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": _to_openai_tools(tools),
            "tool_choice": "auto",
        }
        if _is_reasoning_model(self.model):
            # Reasoning tokens are billed as completion tokens, so the cap must
            # leave room for the chain-of-thought AND the emitted patch.
            kwargs["max_completion_tokens"] = int(os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", "32000"))
            # VENDOR QUIRK (gpt-5.5, validated in smoke): function tools +
            # reasoning_effort are not supported together on /v1/chat/completions
            # ("use /v1/responses instead"). The agent always sends tools, so we
            # only honor an explicit effort when there are none — otherwise a
            # stray OPENAI_REASONING_EFFORT would 400 every cell of a sweep. With
            # tools present the model runs at the API's default effort.
            effort = os.environ.get("OPENAI_REASONING_EFFORT")
            if effort and not tools:
                kwargs["reasoning_effort"] = effort
        else:
            kwargs["max_tokens"] = 4096
        # Stable cache key so identical-prefix requests across turns/processes
        # route to the same prompt cache (OpenAI only; harmless if ignored).
        if self.provider_name == "openai":
            kwargs["prompt_cache_key"] = f"gordian-{self.model}"

        response = None
        for attempt in range(6):
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except Exception as exc:
                err = str(exc)
                # Defensive: if the API rejects an optional param (e.g. a model
                # that does not accept reasoning_effort with tools), strip it and
                # retry once rather than failing the cell.
                if "reasoning_effort" in err and "reasoning_effort" in kwargs:
                    kwargs.pop("reasoning_effort", None)
                    continue
                if "429" not in err and "rate_limit" not in err.lower():
                    raise
                if attempt >= 5:
                    raise
                wait = 10.0
                match = re.search(r"try again in (\d+(?:\.\d+)?)s", err)
                if match:
                    wait = float(match.group(1)) + 1.0
                time.sleep(wait)

        assert response is not None
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

        cached = 0
        details = getattr(response.usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
            cache_read_input_tokens=cached,
        )
        stop = _map_stop_reason(choice.finish_reason, has_tools=bool(tool_calls))

        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
        )
