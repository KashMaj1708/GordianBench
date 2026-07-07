#!/usr/bin/env python3
"""
3-vendor legibility gate on archetype-d-stub (write-skew prototype).

Per ARCHETYPE_D_DESIGN.md: if any vendor source-fast-paths from read_file alone,
redesign before building full Archetype D topology.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
from agent.types import TextBlock, ToolCall, ToolResult
from harness.archetype_spec import ARCHETYPE_D_STUB
from harness.debug_container import debug_container_for_spec
from harness.lifecycle import stack_session
from harness.workspace import agent_workspace_session, assert_corpus_unchanged

import contextlib

SYSTEM = """You are debugging an intermittent pool overdraft bug in a ledger API.

NOTE: The task description deliberately does NOT name the root cause or bug class. Diagnose it
yourself from the source and/or the running system.

Workspace (ephemeral copy):
- ledger-api/main.go — Go service with POST /transfer and GET /balances

Accounts pool-a and pool-b each start at $50.00 ($100.00 combined). The service must keep
a minimum combined pool reserve (see processTransfer in source).

Live stack env vars for run_bash:
- API_URL — HTTP base (GET /balances, POST /transfer)
- DATABASE_URL — Postgres connection string

Tools:
- read_file / write_file on workspace paths (e.g. ledger-api/main.go)
- run_bash for curl, python, or psql probes against the live API and database
- submit_patch: signal that your fix is ready. You do NOT provide a diff — edit
  ledger-api/main.go with write_file, then call submit_patch with no arguments.
  The harness computes the patch from your workspace edits.

When **two transfers from pool-a and pool-b run concurrently**, the combined pool sometimes
ends below the expected amount even though each transaction checks the pool total first.
Investigate and fix.

After investigating, apply your fix with write_file and call submit_patch before turns run out."""

USER = (
    "Under concurrent $15 transfers from pool-a and pool-b ($50 each), the combined pool "
    "sometimes ends below the required reserve. Investigate ledger-api/main.go and the "
    "running service, then fix ledger-api/main.go with write_file and call submit_patch."
)

VENDOR_MATRIX = [
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("openai", "gpt-4.1"),
    ("gemini", "gemini-2.5-pro"),
]


def _serialize_history(result) -> list[dict]:
    out = []
    for msg in result.history:
        blocks = []
        for b in msg.content:
            if isinstance(b, TextBlock):
                # Keep full reasoning text: the automated Test-2 classifier
                # (classify_diagnosis_order.py) needs the complete assistant text
                # to locate the first root-cause mention. Truncating to 500 chars
                # silently biases late-stated diagnoses toward "investigation-driven".
                blocks.append({"type": "text", "text": b.text[:4000]})
            elif isinstance(b, ToolCall):
                blocks.append(
                    {
                        "type": "tool_call",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input if b.name == "run_bash" else {},
                    }
                )
            elif isinstance(b, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "id": b.tool_call_id,
                        "is_error": b.is_error,
                        "output_preview": b.output[:2000],
                    }
                )
        out.append({"role": msg.role.value, "content": blocks})
    return out


def run_one(
    provider_name: str,
    model: str,
    *,
    max_turns: int,
    log_root: Path,
    linux_exec: bool = True,
) -> Path:
    spec = ARCHETYPE_D_STUB
    provider = get_provider(provider_name, model=model)  # type: ignore[arg-type]

    with stack_session("broken", spec=spec) as session, agent_workspace_session(spec=spec) as workspace:
        # run_bash inside a Linux tooling container on the stack network so the
        # agent can actually investigate (curl service names, psql, concurrent
        # load) — the prerequisite for a valid Test 3.
        debug_cm = (
            debug_container_for_spec(spec, workspace.root)
            if linux_exec
            else contextlib.nullcontext(None)
        )
        with debug_cm as debug:
            executor = ToolExecutor(
                config=ExecutorConfig(
                    workspace_root=workspace.src_root,
                    repo_root=workspace.root,
                    gateway_url=session.gateway_url,
                    database_url=session.database_url,
                    stack_variant="broken",
                    spec=spec,
                    debug=debug,
                )
            )
            pipeline = PipelineLogger(run_label=f"{provider_name}/{model}")
            result = run_agent_loop(
                provider,
                executor,
                system=SYSTEM,
                initial_user=USER,
                max_turns=max_turns,
                pipeline=pipeline,
            )

    patch = sanitize_model_patch(result.submitted_patch or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = log_root / f"{stamp}_{provider_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "gate": "archetype-d-stub-legibility",
        "provider": provider_name,
        "model": model,
        "turns_used": result.turns_used,
        "patch_bytes": len(patch),
        "patch_source": result.patch_source,
        "patch_death_stage": pipeline.death_stage(),
    }
    pipeline.dump(run_dir / "patch_pipeline.jsonl")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (run_dir / "trajectory.json").write_text(
        json.dumps(_serialize_history(result), indent=2), encoding="utf-8"
    )
    if patch:
        (run_dir / "model_patch.diff").write_text(patch, encoding="utf-8")

    classify = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "classify_legibility_gate.py"), str(run_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    (run_dir / "legibility.json").write_text(
        classify.stdout or classify.stderr or "{}", encoding="utf-8"
    )

    # Automated Test 2: does diagnosis precede investigation? This is the
    # patch-independent signal the Phase 6 sweep relies on (does not go
    # INCONCLUSIVE when the shell blocks patch delivery).
    order = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "classify_diagnosis_order.py"), str(run_dir), "--write"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    print(f"run={run_dir} patch_bytes={len(patch)} classify_exit={classify.returncode}")
    if classify.stdout:
        print(classify.stdout)
    if order.stdout:
        print(order.stdout)
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="D-stub 3-vendor legibility gate")
    parser.add_argument("--provider", choices=["anthropic", "openai", "gemini"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--all-vendors", action="store_true")
    parser.add_argument(
        "--no-linux-exec",
        action="store_true",
        help="run run_bash on the host shell instead of the Linux tooling container",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=ROOT / "archetype-d-stub" / "legibility_gate_runs",
    )
    args = parser.parse_args()
    linux_exec = not args.no_linux_exec

    if args.all_vendors:
        failures = 0
        for vendor, model in VENDOR_MATRIX:
            print(f"\n=== {vendor} / {model} ===")
            try:
                run_one(
                    vendor,
                    model,
                    max_turns=args.max_turns,
                    log_root=args.log_dir,
                    linux_exec=linux_exec,
                )
            except Exception as exc:
                print(f"BLOCKED: {vendor}: {exc}")
                failures += 1
        assert_corpus_unchanged(spec=ARCHETYPE_D_STUB)
        return 1 if failures else 0

    if not args.provider:
        parser.error("specify --provider or --all-vendors")
    model = args.model or dict(VENDOR_MATRIX).get(args.provider, "")
    if not model:
        parser.error("--model required for this provider")
    run_one(
        args.provider,
        model,
        max_turns=args.max_turns,
        log_root=args.log_dir,
        linux_exec=linux_exec,
    )
    assert_corpus_unchanged(spec=ARCHETYPE_D_STUB)
    return 0


if __name__ == "__main__":
    sys.exit(main())
