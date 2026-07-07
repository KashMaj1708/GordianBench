"""Google Gemini adapter — reference LLMProvider implementation."""

from __future__ import annotations

import uuid
from typing import Any

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, Usage


def _to_gemini_tools(tools: list[ToolDef]) -> list[Any]:
    from google.genai import types

    return [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters=t.parameters,
        )
        for t in tools
    ]


def _tool_name_for_id(history: list[Message], tool_call_id: str) -> str:
    for msg in reversed(history):
        if msg.role != Role.ASSISTANT:
            continue
        for block in msg.content:
            if isinstance(block, ToolCall) and block.id == tool_call_id:
                return block.name
    return tool_call_id


def _serialize_contents(history: list[Message]) -> list[Any]:
    from google.genai import types

    contents: list[Any] = []

    for msg in history:
        if msg.role == Role.TOOL:
            for block in msg.content:
                if not hasattr(block, "tool_call_id"):
                    continue
                fn_name = _tool_name_for_id(history, block.tool_call_id)
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=fn_name,
                                response={"output": block.output},
                            )
                        ],
                    )
                )
            continue

        role = "user" if msg.role == Role.USER else "model"
        parts: list[Any] = []
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text:
                parts.append(types.Part.from_text(text=block.text))
            elif isinstance(block, ToolCall):
                fc_part = types.Part.from_function_call(
                    name=block.name,
                    args=block.input,
                )
                # Gemini 3.x: replay the thought_signature captured from the
                # original response, else the API 400s ("Function call is missing
                # a thought_signature ...") on the next turn.
                sig = (block.provider_meta or {}).get("gemini_thought_signature")
                if sig is not None:
                    fc_part.thought_signature = sig
                parts.append(fc_part)
        if parts:
            contents.append(types.Content(role=role, parts=parts))

    return contents


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
        contents = _serialize_contents(history)

        config = types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[types.Tool(function_declarations=_to_gemini_tools(tools))],
            max_output_tokens=8192,
        )

        response = client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for candidate in response.candidates or []:
            if not candidate.content:
                continue
            for part in candidate.content.parts or []:
                if part.text:
                    text_parts.append(part.text)
                if part.function_call:
                    fc = part.function_call
                    args = dict(fc.args) if fc.args else {}
                    meta: dict[str, Any] = {}
                    sig = getattr(part, "thought_signature", None)
                    if sig is not None:
                        meta["gemini_thought_signature"] = sig
                    tool_calls.append(
                        ToolCall(
                            id=f"gemini_{uuid.uuid4().hex[:12]}",
                            name=fc.name or "",
                            input=args,
                            provider_meta=meta,
                        )
                    )

        usage = Usage()
        if response.usage_metadata:
            um = response.usage_metadata
            # Implicit caching is automatic on Gemini 2.5+/3.x: the API serves a
            # matching prompt prefix at a discount and reports the hit here as
            # cached_content_token_count (a subset of prompt_token_count). We do
            # NOT manage explicit CachedContent — we only surface the implicit hit
            # so the per-turn cache engagement (~turn 4, once main.go is in the
            # history) and the overall hit rate are measurable. cached -> the same
            # Usage.cache_read field the Anthropic/OpenAI adapters populate.
            usage = Usage(
                input_tokens=um.prompt_token_count or 0,
                output_tokens=um.candidates_token_count or 0,
                cache_read_input_tokens=getattr(um, "cached_content_token_count", 0) or 0,
            )

        stop = StopReason.WANTS_TOOL if tool_calls else StopReason.DONE
        return AssistantTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
        )
