"""ScriptedProvider — canned tool calls for mock-correct / mock-band-aid tests."""

from __future__ import annotations

from agent.provider.base import LLMProvider
from agent.tools import ToolDef
from agent.types import AssistantTurn, Message, StopReason, ToolCall


class ScriptedProvider:
    """Returns a fixed sequence of AssistantTurns; proves the provider seam."""

    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self._idx = 0

    def complete(
        self,
        history: list[Message],
        tools: list[ToolDef],
        *,
        system: str = "",
    ) -> AssistantTurn:
        if self._idx >= len(self._turns):
            return AssistantTurn(stop_reason=StopReason.DONE)
        turn = self._turns[self._idx]
        self._idx += 1
        return turn


def mock_correct_provider(model_patch: str) -> ScriptedProvider:
    """Replay: submit known-good patch in one turn."""
    return ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="call_submit", name="submit_patch", input={"model_patch": model_patch})
                ],
                stop_reason=StopReason.WANTS_TOOL,
            )
        ]
    )


def mock_bandaid_provider(model_patch: str) -> ScriptedProvider:
    """Replay: submit band-aid patch in one turn."""
    return ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="call_submit", name="submit_patch", input={"model_patch": model_patch})
                ],
                stop_reason=StopReason.WANTS_TOOL,
            )
        ]
    )
