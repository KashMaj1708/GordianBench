"""Google Gemini adapter — reference LLMProvider implementation."""

from __future__ import annotations

import json
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


def _to_gemini_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def _serialize_contents(history: list[Message]) -> tuple[list[dict[str, Any]], str | None]:
    """Return (contents, system_instruction)."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in history:
        if msg.role == Role.TOOL:
            for block in msg.content:
                if hasattr(block, "tool_call_id"):
                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "function_response": {
                                        "name": block.tool_call_id,
                                        "response": {"output": block.output},
                                    }
                                }
                            ],
                        }
                    )
            continue

        role = "user" if msg.role == Role.USER else "model"
        parts: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text:
                parts.append({"text": block.text})
            elif isinstance(block, ToolCall):
                parts.append(
                    {
                        "function_call": {
                            "name": block.name,
                            "args": block.input,
                        }
                    }
                )
        if parts:
            contents.append({"role": role, "parts": parts})

    return contents, "\n".join(system_parts) if system_parts else None


class GeminiProvider:
    """Maps canonical types ↔ google-genai SDK."""

    def __init__(self, *, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    def complete(
        self,
        history: list[Message],
        tools: list[ToolDef],
        *,
        system: str = "",
    ) -> AssistantTurn:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        contents, _ = _serialize_contents(history)

        config = types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[types.Tool(function_declarations=_to_gemini_tools(tools))],
            max_output_tokens=4096,
        )

        response = client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for candidate in response.candidates or []:
            for part in candidate.content.parts or []:
                if part.text:
                    text_parts.append(part.text)
                if part.function_call:
                    fc = part.function_call
                    args = dict(fc.args) if fc.args else {}
                    tool_calls.append(
                        ToolCall(
                            id=fc.name or "gemini_tool",
                            name=fc.name or "",
                            input=args,
                        )
                    )

        usage = Usage()
        if response.usage_metadata:
            usage = Usage(
                input_tokens=response.usage_metadata.prompt_token_count or 0,
                output_tokens=response.usage_metadata.candidates_token_count or 0,
            )

        stop = StopReason.WANTS_TOOL if tool_calls else StopReason.DONE
        return AssistantTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
        )
