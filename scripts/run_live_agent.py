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
from agent.patch_pipeline import PipelineLogger
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
- submit_patch: signal that your fix is ready. You do NOT provide a diff —
  edit gateway/main.go and upstream-mock/main.go with write_file, then call
  submit_patch with no arguments. The harness computes the patch from your edits.

After investigating, apply your fix with write_file and call submit_patch before turns run out.

The fix requires idempotency: gateway must send Idempotency-Key header; upstream must
deduplicate on that key before inserting into the ledger.

A successful calm-path payment must return **HTTP 200** and charge **exactly once** — both
are required for a complete fix (matches Tier 1 oracle)."""

USER = (
    "Payments are being charged more than once under retry. A successful payment must "
    "return HTTP 200 and charge exactly once. Investigate gateway/main.go and "
    "upstream-mock/main.go, fix them with write_file, then call submit_patch (no diff "
    "needed — the harness reads your edits)."
)

FORCE_TIER1_APPEND_SYSTEM = """

**MANDATORY (this run):** You MUST call run_tier1_test at least once BEFORE submit_patch.
run_tier1_test deploys your **current workspace edits** and runs the calm regression oracle.
Read the output (HTTP status, ledger rows, assertion message). If Tier 1 fails, diagnose from
that runtime evidence and update your fix. Do not submit_patch until you have observed at least
one run_tier1_test result and addressed any failure it reports."""

FORCE_TIER1_USER = (
    "Payments are being charged more than once under retry. Investigate gateway/main.go and "
    "upstream-mock/main.go. **You must call run_tier1_test at least once before submit_patch** "
    "and use its output to guide your fix. Fix the source with write_file, then call "
    "submit_patch (no diff needed — the harness reads your edits)."
)

EXPLICIT_CALM_SUCCESS_APPEND_SYSTEM = """

**Success criteria (in scope):** A correct fix must ensure a calm-path payment returns **HTTP 200**
and charges **exactly once**. HTTP 502 or other non-200 client responses on the first successful
charge are failures you must fix — not a separate issue out of scope."""

EXPLICIT_CALM_SUCCESS_USER = (
    "Payments are being charged more than once under retry. **A successful payment must return "
    "HTTP 200 and charge exactly once** — both are required. Investigate gateway/main.go and "
    "upstream-mock/main.go. You must call run_tier1_test at least once before submit_patch and "
    "use its output to guide your fix. Fix the source with write_file, then call submit_patch."
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
    parser.add_argument(
        "--require-tier1-test",
        action="store_true",
        help="Force run_tier1_test before submit_patch (runtime-discoverability experiment)",
    )
    parser.add_argument(
        "--explicit-calm-success",
        action="store_true",
        help="Task states HTTP 200 + exactly-once are in-scope success criteria",
    )
    parser.add_argument(
        "--experiment",
        default="",
        help="Label stored in meta.json (e.g. forced_tier1_rep2)",
    )
    parser.add_argument("--log-dir", type=Path, default=ROOT / "archetype-a" / "live_agent_runs")
    args = parser.parse_args()

    provider_name: ProviderName = args.provider  # type: ignore[assignment]

    try:
        provider = get_provider(provider_name, model=args.model)
    except RuntimeError as exc:
        print(f"BLOCKED: {exc}")
        return 2

    model_label = args.model or os.environ.get(f"{provider_name.upper()}_MODEL", "(default)")
    system = SYSTEM
    if args.require_tier1_test:
        system += FORCE_TIER1_APPEND_SYSTEM
    if args.explicit_calm_success:
        system += EXPLICIT_CALM_SUCCESS_APPEND_SYSTEM

    if args.require_tier1_test and args.explicit_calm_success:
        user = EXPLICIT_CALM_SUCCESS_USER
    elif args.require_tier1_test:
        user = FORCE_TIER1_USER
    elif args.explicit_calm_success:
        user = EXPLICIT_CALM_SUCCESS_USER.replace(
            "You must call run_tier1_test at least once before submit_patch and ",
            "",
        )
    else:
        user = USER

    print(f"Live agent: provider={provider_name} model={model_label} max_turns={args.max_turns}")
    if args.require_tier1_test:
        print("mode: require-tier1-test (workspace-backed run_tier1_test)")
    if args.explicit_calm_success:
        print("mode: explicit-calm-success (HTTP 200 + exactly-once in scope)")
    if args.experiment:
        print(f"experiment: {args.experiment}")

    try:
        with stack_session("broken") as session, agent_workspace_session() as workspace:
            executor = ToolExecutor(
                config=ExecutorConfig(
                    workspace_root=workspace.src_root,
                    repo_root=workspace.root,
                    gateway_url=session.gateway_url,
                    database_url=session.database_url,
                    tier1_tests_workspace=args.require_tier1_test,
                    stack_variant="broken",
                    spec=session.spec,
                )
            )
            pipeline = PipelineLogger(run_label=f"{provider_name}/{model_label}")
            result = run_agent_loop(
                provider,
                executor,
                system=system,
                initial_user=user,
                max_turns=args.max_turns,
                pipeline=pipeline,
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
        "patch_source": result.patch_source,
        "patch_death_stage": pipeline.death_stage(),
        "require_tier1_test": args.require_tier1_test,
        "explicit_calm_success": args.explicit_calm_success,
        "experiment": args.experiment or None,
    }
    pipeline.dump(run_dir / "patch_pipeline.jsonl")
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
            grade_lines = [f"score={score}"]
            if args.require_tier1_test:
                grade_lines.append("require_tier1_test=true")
            if args.explicit_calm_success:
                grade_lines.append("explicit_calm_success=true")
            if args.experiment:
                grade_lines.append(f"experiment={args.experiment}")
            (run_dir / "grade.txt").write_text("\n".join(grade_lines) + "\n", encoding="utf-8")
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
