"""ScriptedProvider — canned tool calls for delivery / mock tests (no API spend).

The scenario builders below simulate every patch-delivery failure mode so the
delivery pipeline can be regression-tested without a live model. Under the
workspace-diff-as-truth contract, a realistic agent edits files with write_file
and then signals submit_patch (no diff text), so these scenarios drive write_file
edits rather than inline diffs.
"""

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


# --- turn builders ----------------------------------------------------------

def _write(path: str, content: str, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="write_file", input={"path": path, "content": content})


def _submit(call_id: str = "submit", **extra) -> ToolCall:
    return ToolCall(id=call_id, name="submit_patch", input=dict(extra))


def _tool_turn(*calls: ToolCall) -> AssistantTurn:
    return AssistantTurn(tool_calls=list(calls), stop_reason=StopReason.WANTS_TOOL)


def _done() -> AssistantTurn:
    return AssistantTurn(text="done", stop_reason=StopReason.DONE)


# --- delivery scenarios -----------------------------------------------------

def edit_then_submit(path: str, content: str) -> ScriptedProvider:
    """Realistic happy path: edit the source, then signal submit (no diff)."""
    return ScriptedProvider([
        _tool_turn(_write(path, content, "w1")),
        _tool_turn(_submit("s1")),
    ])


def write_diff_file_then_submit(diff_path: str, diff_text: str) -> ScriptedProvider:
    """Model writes a .diff file instead of editing, then submits."""
    return ScriptedProvider([
        _tool_turn(_write(diff_path, diff_text, "w1")),
        _tool_turn(_submit("s1")),
    ])


def truncated_inline_then_submit(path: str, content: str, truncated_inline: str) -> ScriptedProvider:
    """Edits correctly but ALSO passes a truncated inline diff — must be ignored."""
    return ScriptedProvider([
        _tool_turn(_write(path, content, "w1")),
        _tool_turn(_submit("s1", model_patch=truncated_inline)),
    ])


def edit_no_submit(path: str, content: str) -> ScriptedProvider:
    """Edits correctly but never submits — loop must auto-submit the workspace diff."""
    return ScriptedProvider([
        _tool_turn(_write(path, content, "w1")),
        _done(),
    ])


def edit_plus_new_file_then_submit(
    path: str, content: str, new_path: str, new_content: str
) -> ScriptedProvider:
    """Modify a file AND create a new file in the service dir, then submit.

    Reproduces the GPT-4.1 "corrupt patch" death: the computed diff contains a
    `--- /dev/null` new-file section, which the old normalizer mangled.
    """
    return ScriptedProvider([
        _tool_turn(_write(path, content, "w1"), _write(new_path, new_content, "w2")),
        _tool_turn(_submit("s1")),
    ])


def edit_bad_then_fix(path: str, bad_content: str, good_content: str) -> ScriptedProvider:
    """First edit fails build; after build feedback, fix and resubmit."""
    return ScriptedProvider([
        _tool_turn(_write(path, bad_content, "w1")),
        _tool_turn(_submit("s1")),          # build_check fails -> feedback
        _tool_turn(_write(path, good_content, "w2")),
        _tool_turn(_submit("s2")),          # build_check passes
    ])


# --- legacy mock providers (now write a patch file, per the new contract) ----

def mock_correct_provider(model_patch: str) -> ScriptedProvider:
    """Replay: write known-good diff to a file, then submit."""
    return write_diff_file_then_submit("model_patch.diff", model_patch)


def mock_bandaid_provider(model_patch: str) -> ScriptedProvider:
    """Replay: write band-aid diff to a file, then submit."""
    return write_diff_file_then_submit("model_patch.diff", model_patch)
