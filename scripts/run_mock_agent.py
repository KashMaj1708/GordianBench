#!/usr/bin/env python3
"""Mock agent smoke: scripted provider → submit_patch → grade_patch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.loop import run_agent_loop
from agent.provider.scripted import mock_bandaid_provider, mock_correct_provider
from harness.archetype_spec import ARCHETYPE_A
from harness.grade import grade_patch
from harness.patch import PATCHES_ROOT, generate_fixed_model_patch
from harness.workspace import agent_workspace_session


def _bandaid_timeout_patch() -> str:
    """Generate diff: broken gateway → bandaid-timeout (CLIENT_TIMEOUT_MS=5000)."""
    import shutil
    import tempfile

    from harness.patch import BROKEN_SRC, PATCHES_ROOT, _copy_src_normalized, _run

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        _copy_src_normalized(root / "src")
        _run(["git", "init"], cwd=root)
        _run(["git", "config", "core.autocrlf", "false"], cwd=root)
        _run(["git", "add", "-A"], cwd=root)
        _run(["git", "commit", "-m", "broken"], cwd=root)
        shutil.copy2(
            PATCHES_ROOT / "bandaid-timeout" / "gateway" / "main.go",
            root / "src" / "gateway" / "main.go",
        )
        proc = _run(["git", "diff"], cwd=root)
        return proc.stdout


def _fixed_patch() -> str:
    corpus = PATCHES_ROOT / "fixed" / "model_patch.diff"
    if corpus.exists():
        return corpus.read_text(encoding="utf-8")
    patch = generate_fixed_model_patch()
    corpus.write_text(patch, encoding="utf-8")
    return patch


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock agent → grade_patch smoke")
    parser.add_argument(
        "--agent",
        choices=["correct", "bandaid"],
        default="correct",
    )
    args = parser.parse_args()

    if args.agent == "correct":
        patch = _fixed_patch()
        provider = mock_correct_provider(patch)
        expected = 1.0
    else:
        patch = _bandaid_timeout_patch()
        provider = mock_bandaid_provider(patch)
        expected = 0.0

    with agent_workspace_session(spec=ARCHETYPE_A) as workspace:
        executor = ToolExecutor(
            ExecutorConfig(
                workspace_root=workspace.src_root,
                repo_root=workspace.root,
                spec=ARCHETYPE_A,
            )
        )
        result = run_agent_loop(
            provider,
            executor,
            system="Submit the patch when ready.",
            initial_user="Fix the duplicate charge bug.",
            max_turns=5,
        )

    if not result.submitted_patch:
        print("FAIL: no patch submitted")
        return 1

    score = grade_patch(result.submitted_patch)
    ok = score == expected
    print(f"agent={args.agent} score={score} expected={expected} {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
