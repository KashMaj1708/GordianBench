"""Provider-agnostic turn loop."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.executor import ToolExecutor
from agent.provider.base import LLMProvider
from agent.tools import AGENT_TOOLS, ToolDef
from agent.patch_util import sanitize_model_patch
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, ToolResult


@dataclass
class LoopResult:
    history: list[Message] = field(default_factory=list)
    submitted_patch: str | None = None
    turns_used: int = 0
    final_score: float | None = None


def run_agent_loop(
    provider: LLMProvider,
    executor: ToolExecutor,
    *,
    system: str,
    initial_user: str,
    tools: list[ToolDef] | None = None,
    max_turns: int = 30,
) -> LoopResult:
    """
    Turn cycle identical regardless of provider:
      complete → tool_calls? → execute → append → repeat
    """
    tool_defs = tools or AGENT_TOOLS
    history: list[Message] = [
        Message(role=Role.USER, content=[TextBlock(text=initial_user)])
    ]
    result = LoopResult(history=history)

    for turn in range(max_turns):
        assistant: AssistantTurn = provider.complete(history, tool_defs, system=system)
        result.turns_used = turn + 1

        assistant_blocks: list[TextBlock | ToolCall] = []
        if assistant.text:
            assistant_blocks.append(TextBlock(text=assistant.text))
        assistant_blocks.extend(assistant.tool_calls)
        if assistant_blocks:
            history.append(Message(role=Role.ASSISTANT, content=assistant_blocks))

        if not assistant.tool_calls:
            if assistant.stop_reason in (StopReason.DONE, StopReason.TRUNCATED):
                break
            continue

        for call in assistant.tool_calls:
            if call.name == "submit_patch":
                result.submitted_patch = sanitize_model_patch(
                    str(call.input.get("model_patch", ""))
                )
            tr = executor.dispatch(call)
            history.append(Message(role=Role.TOOL, content=[tr]))

        if result.submitted_patch is not None:
            break

    result.history = history
    return result
