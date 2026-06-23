#!/usr/bin/env python3
"""
Run a real LLM agent on Archetype A (broken stack).

Phase 4 exit criterion (plan line 146): a real model completes a full trajectory
end-to-end without harness errors. No score assertion.

Loads API keys from .env at repo root. Use scripts/probe_model_access.py to find
working model IDs per vendor.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agent.executor import ExecutorConfig, ToolExecutor
from agent.loop import run_agent_loop
from agent.patch_util import sanitize_model_patch
from agent.provider.registry import ProviderName, get_provider
from agent.types import Role, TextBlock, ToolCall, ToolResult
from harness.grade import grade_patch
from harness.lifecycle import stack_session
from harness.workspace import agent_workspace_session, assert_corpus_unchanged

SYSTEM = """You are debugging a payment gateway duplicate-charge bug (Archetype A).

You are working in an ephemeral copy of the task source tree (not the host corpus).
File paths are relative to the workspace root:
- gateway/main.go
- upstream-mock/main.go

Tools:
- read_file / write_file on the workspace above
- run_bash for curl/python probes against the live stack (gateway at GATEWAY_URL)
- run_tier1_test to check regression status
- submit_patch with model_patch: a raw unified git diff ONLY (no markdown fences).
  Patch paths must use src/gateway/... and src/upstream-mock/... (git diff format).

The fix requires idempotency: gateway must send Idempotency-Key header; upstream must
deduplicate on that key before inserting into the ledger.

After investigating, you MUST call submit_patch with a complete git diff before turns run out.
Do not use write_file to apply the fix — only submit_patch is graded."""

USER = (
    "Payments are being charged more than once under retry. Investigate gateway/main.go and "
    "upstream-mock/main.go, then call submit_patch with a unified git diff (paths like "
    "src/gateway/main.go). Do NOT use write_file for the final fix — only submit_patch counts."
)


def _serialize_history(result) -> list[dict]:
    out = []
    for msg in result.history:
        blocks = []
        for b in msg.content:
            if isinstance(b, TextBlock):
                blocks.append({"type": "text", "text": b.text[:500]})
            elif isinstance(b, ToolCall):
                blocks.append({"type": "tool_call", "id": b.id, "name": b.name})
            elif isinstance(b, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "id": b.tool_call_id,
                        "is_error": b.is_error,
                        "output_preview": b.output[:300],
                    }
                )
        out.append({"role": msg.role.value, "content": blocks})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Live LLM agent trajectory")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--grade", action="store_true", help="Run grade_patch (informational)")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "deepseek", "gemini"],
        default=os.environ.get("LLM_PROVIDER", "anthropic"),
    )
    parser.add_argument("--model", default=None, help="Override vendor default model")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "archetype-a" / "live_agent_runs")
    args = parser.parse_args()

    provider_name: ProviderName = args.provider  # type: ignore[assignment]

    try:
        provider = get_provider(provider_name, model=args.model)
    except RuntimeError as exc:
        print(f"BLOCKED: {exc}")
        return 2

    model_label = args.model or os.environ.get(f"{provider_name.upper()}_MODEL", "(default)")
    print(f"Live agent: provider={provider_name} model={model_label} max_turns={args.max_turns}")

    try:
        with stack_session("broken") as session, agent_workspace_session() as workspace:
            executor = ToolExecutor(
                config=ExecutorConfig(
                    workspace_root=workspace.src_root,
                    gateway_url=session.gateway_url,
                    database_url=session.database_url,
                )
            )
            result = run_agent_loop(
                provider,
                executor,
                system=SYSTEM,
                initial_user=USER,
                max_turns=args.max_turns,
            )
    except Exception as exc:
        print(f"FAIL: harness error during live trajectory: {exc}")
        return 1

    patch = sanitize_model_patch(result.submitted_patch or "")
    print(f"turns_used={result.turns_used}")
    print(f"submitted_patch={'yes' if patch else 'no'} ({len(patch)} bytes)")

    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.log_dir / stamp
    run_dir.mkdir()
    meta = {
        "provider": provider_name,
        "model": model_label,
        "turns_used": result.turns_used,
        "patch_bytes": len(patch),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (run_dir / "trajectory.json").write_text(
        json.dumps(_serialize_history(result), indent=2), encoding="utf-8"
    )
    if patch:
        (run_dir / "model_patch.diff").write_text(patch, encoding="utf-8")
    print(f"log: {run_dir}")

    if not patch:
        print("FAIL: no model_patch submitted")
        return 1

    if args.grade:
        try:
            score = grade_patch(patch)
            print(f"grade_patch score={score} (informational)")
            (run_dir / "grade.txt").write_text(f"score={score}\n", encoding="utf-8")
        except Exception as exc:
            print(f"grade_patch error (informational): {exc}")

    try:
        assert_corpus_unchanged()
        print("corpus integrity: OK (host src unchanged)")
    except RuntimeError as exc:
        print(f"WARN: {exc}")

    print("Live agent trajectory: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
