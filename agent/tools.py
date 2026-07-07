"""Neutral tool definitions — adapters render vendor-specific schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


AGENT_TOOLS: list[ToolDef] = [
    ToolDef(
        name="read_file",
        description="Read a file from the task workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path"}},
            "required": ["path"],
        },
    ),
    ToolDef(
        name="write_file",
        description="Write or overwrite a file in the task workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    ),
    ToolDef(
        name="run_bash",
        description=(
            "Run a shell command in the task container. "
            "Use for curl, dig, tcpdump, etc. Non-interactive only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 120},
            },
            "required": ["command"],
        },
    ),
    ToolDef(
        name="run_tier1_test",
        description="Run Tier 1 regression tests against the live stack (calm, no chaos).",
        parameters={"type": "object", "properties": {}},
    ),
    ToolDef(
        name="submit_patch",
        description=(
            "Signal that your fix is complete and should be graded. You do NOT need to "
            "provide a diff: the harness computes the patch automatically from the edits "
            "you made to the source files with write_file. Just edit the files, then call "
            "submit_patch with no arguments. (Any model_patch/patch_path you pass is "
            "logged but ignored — the workspace edits are the source of truth.)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "model_patch": {
                    "type": "string",
                    "description": "Ignored. The harness reads your write_file edits instead.",
                },
                "patch_path": {
                    "type": "string",
                    "description": "Ignored. The harness reads your write_file edits instead.",
                },
            },
            "required": [],
        },
    ),
]
