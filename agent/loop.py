"""Provider-agnostic turn loop.

Patch-delivery contract (Phase 5 re-architecture): the model's edits already
exist as files in a git-initialized workspace, so the harness COMPUTES the patch
from `git diff` instead of trusting a transmission-prone inline diff. `submit_patch`
is a *signal* ("grade my edits"), not a transport. Inline patch content is logged
but never trusted. This removes the truncation / wrong-field / path-mismatch death
modes by construction. See agent/patch_pipeline.py for the six-stage trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agent.executor import ToolExecutor
from agent.patch_pipeline import PipelineLogger, content_fields
from agent.patch_util import looks_truncated_patch, repair_model_patch
from agent.provider.base import LLMProvider
from agent.tools import AGENT_TOOLS, ToolDef
from agent.types import AssistantTurn, Message, Role, StopReason, TextBlock, ToolCall, ToolResult, Usage

BuildCheck = Callable[[str], "str | None"]


@dataclass
class LoopResult:
    history: list[Message] = field(default_factory=list)
    submitted_patch: str | None = None
    patch_source: str | None = None
    turns_used: int = 0
    final_score: float | None = None
    pipeline: PipelineLogger | None = None
    # Token accounting summed across turns (cache_* populated when prompt caching
    # is active — lets a multi-turn / k-shot run confirm cache hits and savings).
    usage: Usage = field(default_factory=Usage)
    # Last workspace diff that failed to apply — persisted for debugging
    # Stage-5 deaths ("corrupt patch at line N") that would otherwise be lost.
    rejected_patch: str | None = None


def _spec_of(executor: ToolExecutor):
    return executor.config.spec


def resolve_workspace_patch(
    executor: ToolExecutor,
    logger: PipelineLogger,
) -> tuple[str | None, str | None, str]:
    """Resolve the patch from workspace state. Returns (patch, reject_reason, source).

    Order (never parses inline tool args as patch content):
      1. git diff over service src dirs  — captures write_file edits (primary)
      2. largest *.diff/*.patch file in the workspace — model wrote a patch file
    """
    from harness.patch import check_patch_applies_fast

    spec = _spec_of(executor)
    last_reason = "no workspace edits and no patch file"

    # 1. Primary: workspace git diff (the real edits on disk). This is canonical
    # git output — do NOT run it through repair_model_patch (that normalizer is
    # for model-authored patches and would mangle valid new-file sections).
    diff = executor.workspace_diff()
    if diff.strip():
        logger.log("content_check", {**content_fields(diff, source="workspace_git_diff"), "ok": True})
        err = check_patch_applies_fast(diff, spec=spec)
        logger.log(
            "git_apply",
            {"source": "workspace_git_diff", "ok": err is None, "stderr": err or ""},
        )
        if err is None:
            logger.log("extraction", {"resolved_from": "workspace_git_diff", "ok": True})
            return diff, None, "workspace_git_diff"
        last_reason = f"git apply failed: {err}"

    # 2. Fallback: a patch file the model wrote (it emitted a diff instead of editing).
    files = executor.workspace_patch_files()
    logger.log(
        "extraction",
        {
            "tried": ["workspace_git_diff", "workspace_patch_file"],
            "resolved_from": None,
            "workspace_files_present": [f.name for f in files],
        },
    )
    for pf in files:
        try:
            raw = pf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not raw.strip():
            continue
        repaired = repair_model_patch(raw)
        logger.log(
            "content_check",
            {**content_fields(repaired, source=f"workspace_patch_file:{pf.name}"), "ok": True},
        )
        if looks_truncated_patch(repaired):
            last_reason = "incomplete unified diff (truncated hunk)"
            continue
        err = check_patch_applies_fast(repaired, spec=spec)
        logger.log(
            "git_apply",
            {"source": f"workspace_patch_file:{pf.name}", "ok": err is None, "stderr": err or ""},
        )
        if err is None:
            return repaired, None, f"workspace_patch_file:{pf.name}"
        last_reason = f"git apply failed: {err}"

    return None, last_reason, "none"


_REJECT_GUIDANCE = (
    "Edit the source files directly with write_file (the harness computes the patch "
    "from your workspace edits — you do NOT need to hand over a diff). Then call "
    "submit_patch again."
)


def _handle_submit(
    call: ToolCall,
    executor: ToolExecutor,
    logger: PipelineLogger,
    *,
    turn: int,
    build_check: BuildCheck | None,
    build_attempts: list[int],
    max_build_retries: int,
) -> tuple[str | None, str | None, ToolResult]:
    """Process a submit_patch call. Returns (accepted_patch, source, tool_result)."""
    inline = str(call.input.get("model_patch", "") or "")
    logger.log(
        "submit_attempt",
        {
            "turn": turn,
            "tool_call_id": call.id,
            "raw_input_keys": list(call.input.keys()),
            "has_inline_patch": bool(inline.strip()),
            "inline_len": len(inline),
            "has_patch_path": bool(call.input.get("patch_path")),
            "patch_path_value": call.input.get("patch_path"),
        },
    )
    if inline.strip():
        # Logged so we can see what the model tried to transmit, but NOT trusted.
        logger.log("content_check", {**content_fields(inline, source="inline_IGNORED"), "trusted": False})

    accepted, reject_reason, source = resolve_workspace_patch(executor, logger)

    if accepted and build_check is not None:
        build_err = build_check(accepted)
        logger.log(
            "build",
            {"ok": build_err is None, "attempt": build_attempts[0] + 1, "stderr_tail": (build_err or "")[-2000:]},
        )
        if build_err is not None:
            build_attempts[0] += 1
            if build_attempts[0] <= max_build_retries:
                tr = ToolResult(
                    tool_call_id=call.id,
                    output=(
                        f"patch applied but the build FAILED:\n{build_err[-1500:]}\n\n"
                        "Fix the compile error with write_file, then call submit_patch again."
                    ),
                    is_error=True,
                )
                return None, None, tr
            # out of retries: surface but stop looping
            reject_reason = f"build failed after {max_build_retries} retries: {build_err[-300:]}"
            accepted = None

    if accepted:
        logger.log("accepted", {"source": source, "byte_len": len(accepted)})
        tr = ToolResult(
            tool_call_id=call.id,
            output=f"patch accepted from {source} ({len(accepted)} bytes); grading after loop.",
        )
        return accepted, source, tr

    logger.log("rejected", {"reason": reject_reason})
    tr = ToolResult(
        tool_call_id=call.id,
        output=f"patch rejected: {reject_reason}. {_REJECT_GUIDANCE}",
        is_error=True,
    )
    return None, None, tr


def run_agent_loop(
    provider: LLMProvider,
    executor: ToolExecutor,
    *,
    system: str,
    initial_user: str,
    tools: list[ToolDef] | None = None,
    max_turns: int = 30,
    pipeline: PipelineLogger | None = None,
    build_check: BuildCheck | None = None,
    max_build_retries: int = 2,
) -> LoopResult:
    """Turn cycle: complete → tool_calls? → execute → append → repeat."""
    tool_defs = tools or AGENT_TOOLS
    logger = pipeline or PipelineLogger()
    history: list[Message] = [Message(role=Role.USER, content=[TextBlock(text=initial_user)])]
    result = LoopResult(history=history, pipeline=logger)

    nudged = False
    build_attempts = [0]

    for turn in range(max_turns):
        turns_left = max_turns - turn

        assistant: AssistantTurn = provider.complete(history, tool_defs, system=system)
        result.turns_used = turn + 1

        u = assistant.usage
        result.usage.input_tokens += u.input_tokens
        result.usage.output_tokens += u.output_tokens
        result.usage.cache_read_input_tokens += u.cache_read_input_tokens
        result.usage.cache_creation_input_tokens += u.cache_creation_input_tokens
        # Per-turn usage so cache engagement can be charted across the run (e.g.
        # the implicit cache kicking in once the prefix exceeds the vendor floor).
        logger.log(
            "turn_usage",
            {
                "turn": turn + 1,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens,
                "cache_creation_input_tokens": u.cache_creation_input_tokens,
            },
        )

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
                accepted, source, tr = _handle_submit(
                    call,
                    executor,
                    logger,
                    turn=turn + 1,
                    build_check=build_check,
                    build_attempts=build_attempts,
                    max_build_retries=max_build_retries,
                )
                history.append(Message(role=Role.TOOL, content=[tr]))
                if accepted is not None:
                    result.submitted_patch = accepted
                    result.patch_source = source
                else:
                    # Keep the bytes that failed so Stage-5 deaths are debuggable.
                    diff = executor.workspace_diff()
                    if diff.strip():
                        result.rejected_patch = diff
                continue
            tr = executor.dispatch(call)
            history.append(Message(role=Role.TOOL, content=[tr]))

        if result.submitted_patch is not None:
            break

        # Turn-budget nudge: remind the model to make edits and submit before it
        # runs out, so an investigation-heavy run still delivers a patch (H4).
        if not nudged and result.submitted_patch is None and turns_left <= 3:
            nudged = True
            history.append(
                Message(
                    role=Role.USER,
                    content=[
                        TextBlock(
                            text=(
                                f"You have ~{turns_left - 1} turns left. Make your fix by editing "
                                "the source with write_file, then call submit_patch (no diff text "
                                "needed — the harness reads your edits)."
                            )
                        )
                    ],
                )
            )

    # Auto-submit (H4): edited correctly but never called submit_patch → don't
    # conflate "didn't call the tool" with "couldn't fix". Deliver the workspace
    # diff so it is gradeable instead of returning 0 bytes.
    if result.submitted_patch is None:
        accepted, reject_reason, source = resolve_workspace_patch(executor, logger)
        if accepted is not None:
            logger.log("auto_submit", {"source": source, "byte_len": len(accepted), "ok": True})
            result.submitted_patch = accepted
            result.patch_source = f"auto:{source}"
        else:
            logger.log("auto_submit", {"ok": False, "reason": reject_reason})
            diff = executor.workspace_diff()
            if diff.strip():
                result.rejected_patch = diff

    logger.log(
        "usage",
        {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_read_input_tokens": result.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": result.usage.cache_creation_input_tokens,
        },
    )

    result.history = history
    return result
