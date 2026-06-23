#!/usr/bin/env python3
"""Phase 4: assert write_file mutates workspace and run_bash reads it back."""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.types import ToolCall


def main() -> int:
    token = f"tool-faith-{uuid.uuid4().hex[:8]}"
    rel_path = "gateway/_tool_faith_probe.txt"

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td) / "src"
        workspace.mkdir()
        (workspace / "gateway").mkdir()

        executor = ToolExecutor(config=ExecutorConfig(workspace_root=workspace))

        write = executor.dispatch(
            ToolCall(
                id="w1",
                name="write_file",
                input={"path": rel_path, "content": token},
            )
        )
        if write.is_error:
            print(f"write_file failed: {write.output}")
            return 1

        read_tool = executor.dispatch(
            ToolCall(id="r1", name="read_file", input={"path": rel_path})
        )
        if read_tool.is_error or read_tool.output != token:
            print(f"read_file mismatch: {read_tool.output!r} != {token!r}")
            return 1

        # Cross-platform: python reads file via bash cwd = workspace_root
        bash = executor.dispatch(
            ToolCall(
                id="b1",
                name="run_bash",
                input={
                    "command": (
                        f'python -c "print(open(r\'{rel_path}\', encoding=\'utf-8\').read(), end=\'\')"'
                    ),
                    "timeout_sec": 30,
                },
            )
        )
        if bash.is_error or bash.output.strip() != token:
            print(f"run_bash mismatch: {bash.output!r} != {token!r}")
            return 1

    print("Tool-faithfulness: PASS (write_file, read_file, run_bash)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
