#!/usr/bin/env python3
"""
Multi-turn scripted agent trajectory on live broken stack.

Validates loop plumbing beyond single-turn mock: read_file → run_bash → submit_patch.
Produces a gradeable patch without requiring ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.loop import run_agent_loop
from agent.provider.scripted import ScriptedProvider
from agent.types import AssistantTurn, Role, StopReason, ToolCall, ToolResult
from harness.lifecycle import stack_session
from harness.workspace import agent_workspace_session
from harness.patch import PATCHES_ROOT, generate_fixed_model_patch


def _fixed_patch() -> str:
    corpus = PATCHES_ROOT / "fixed" / "model_patch.diff"
    if corpus.exists():
        return corpus.read_text(encoding="utf-8")
    patch = generate_fixed_model_patch()
    corpus.write_text(patch, encoding="utf-8")
    return patch


def main() -> int:
    patch = _fixed_patch()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="t1", name="read_file", input={"path": "gateway/main.go"}),
                ],
                stop_reason=StopReason.WANTS_TOOL,
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="run_bash",
                        input={
                            "command": 'python -c "import urllib.request; print(urllib.request.urlopen(\'http://localhost:8080/health\').read().decode())"',
                            "timeout_sec": 30,
                        },
                    ),
                ],
                stop_reason=StopReason.WANTS_TOOL,
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="t3",
                        name="write_file",
                        input={"path": "model_patch.diff", "content": patch},
                    ),
                ],
                stop_reason=StopReason.WANTS_TOOL,
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="t4", name="submit_patch", input={}),
                ],
                stop_reason=StopReason.WANTS_TOOL,
            ),
        ]
    )

    with stack_session("broken") as session, agent_workspace_session() as workspace:
        executor = ToolExecutor(
            config=ExecutorConfig(
                workspace_root=workspace.src_root,
                repo_root=workspace.root,
                gateway_url=session.gateway_url,
                database_url=session.database_url,
                spec=session.spec,
            )
        )
        result = run_agent_loop(
            provider,
            executor,
            system="Investigate and fix.",
            initial_user="Debug duplicate charge.",
            max_turns=10,
        )

    if result.turns_used < 3:
        print(f"FAIL: expected 3 turns, got {result.turns_used}")
        return 1
    if not result.submitted_patch:
        print("FAIL: no patch submitted")
        return 1

    # Tool results should include gateway source and health check
    tool_outputs = [
        b.output
        for msg in result.history
        if msg.role == Role.TOOL
        for b in msg.content
        if isinstance(b, ToolResult)
    ]
    if not any("package main" in o for o in tool_outputs):
        print("FAIL: read_file did not return gateway source")
        return 1
    if not any("ok" in o.lower() for o in tool_outputs):
        print("FAIL: run_bash health check did not return ok")
        return 1

    print(f"trajectory: {result.turns_used} turns, patch={len(result.submitted_patch)} bytes")
    print("Agent trajectory: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
