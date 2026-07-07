#!/usr/bin/env python3
"""
Legibility gate on archetype-d-stale-read (stale-read-after-partition prototype).

Mirrors the write-skew gate but with (a) a de-leaked prompt that does NOT name the
bug class, and (b) the Linux tooling-container executor so the agent can actually
run the investigation chain (curl, psql against primary AND replica, partition).

Per STALE_READ_DESIGN.md: if a vendor names the specific root cause and fix WITHOUT
runtime disambiguation (Test 2 SOURCE_LEGIBLE via classify_diagnosis_order.py),
redesign before building the full Tier-2 chaos corpus.

NOTE: this spends API credits. Run explicitly with a provider/model.
"""

from __future__ import annotations

import argparse
import contextlib
import json
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
from agent.provider.registry import get_provider
from agent.types import TextBlock, ToolCall, ToolResult
from harness.archetype_spec import ARCHETYPE_D_STALE
from harness.build_check import make_workspace_build_check
from harness.debug_container import debug_container_for_spec
from harness.lifecycle import stack_session
from harness.patch import check_patch_applies_fast
from harness.workspace import agent_workspace_session, assert_corpus_unchanged

# Symptom only — deliberately does NOT name stale-read / replication / read-your-writes.
SYSTEM = """You are debugging an intermittent data-consistency bug in a ledger HTTP API.

NOTE: This description deliberately does NOT name the root cause or bug class. Diagnose it
yourself from the source and/or by running the system.

Workspace (ephemeral copy):
- ledger-api/main.go — Go service with POST /deposit and GET /balance?account=<id>

Symptom: shortly after a successful POST /deposit for an account, a follow-up
GET /balance for the same account sometimes returns the older (pre-deposit) value.

Live stack env vars for run_bash (a real Linux shell with curl, psql, python):
- API_URL — HTTP base (POST /deposit, GET /balance?account=)
- TOXIPROXY_URL — http://toxiproxy:8474 — inject network faults between services
The service reads its datastore connection settings from its own environment; read
the source to see what it connects to and which env vars to use. Those same env
vars are available to you in run_bash.

Tools:
- read_file / write_file on workspace paths (e.g. ledger-api/main.go)
- run_bash for curl, python, or psql probes against the live API and datastores
- submit_patch: signal that your fix is ready. You do NOT provide a diff —
  edit ledger-api/main.go with write_file, then call submit_patch with no
  arguments. The harness computes the patch from your workspace edits.

After investigating, apply your fix with write_file and call submit_patch before turns run out."""

USER = (
    "After a successful POST /deposit to acct-1, a follow-up GET /balance?account=acct-1 "
    "sometimes returns the pre-deposit value. Investigate ledger-api/main.go and the running "
    "system, then fix ledger-api/main.go with write_file and call submit_patch."
)

VENDOR_MATRIX = [
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("openai", "gpt-4.1"),
    ("gemini", "gemini-2.5-pro"),
]

# Replica DSN handed to the agent's tooling container for cross-node investigation.
INTERNAL_REPLICA_URL = "postgresql://bench:bench@postgres-replica:5432/ledger"


def _set_replication(enabled: bool, toxiproxy_url: str) -> None:
    """Toggle the primary->replica replication proxy from the host.

    Sustained partition (enabled=False) freezes the standby's LSN, so the
    stale-read bug manifests deterministically for the WHOLE session — the agent
    observes it on the obvious deposit->read probe instead of having to inject the
    fault itself (the calm-environment confound). The seeded acct-1 already exists
    on the replica before partition, so reads return a stale VALUE (not an error).
    """
    import requests

    resp = requests.post(
        f"{toxiproxy_url.rstrip('/')}/proxies/replication",
        json={"enabled": enabled},
        timeout=10,
    )
    resp.raise_for_status()


def _add_replication_lag(latency_ms: int, toxiproxy_url: str) -> None:
    """Inject steady recovering lag (latency toxic) on the WAL stream.

    The toxic is on the *downstream* stream because PostgreSQL streams WAL from
    the primary back to the standby; this delays WAL delivery by latency_ms so the
    replica is perpetually behind for recent writes but DOES catch up — the
    canonical read-your-writes failure (transient lag, not permanent partition).
    """
    import requests

    base = toxiproxy_url.rstrip("/")
    requests.delete(f"{base}/proxies/replication/toxics/repl_lag", timeout=10)
    resp = requests.post(
        f"{base}/proxies/replication/toxics",
        json={
            "name": "repl_lag",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": 0},
        },
        timeout=10,
    )
    resp.raise_for_status()


def _reset_replication(toxiproxy_url: str) -> None:
    """Clear toxics and re-enable the proxy (best effort)."""
    import requests

    base = toxiproxy_url.rstrip("/")
    with contextlib.suppress(Exception):
        requests.delete(f"{base}/proxies/replication/toxics/repl_lag", timeout=10)
    with contextlib.suppress(Exception):
        requests.post(f"{base}/proxies/replication", json={"enabled": True}, timeout=10)


def _serialize_history(result) -> list[dict]:
    out = []
    for msg in result.history:
        blocks = []
        for b in msg.content:
            if isinstance(b, TextBlock):
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
    chaos_mode: str | None = None,
    chaos_lag_ms: int = 20000,
    build_check: bool = True,
    max_build_retries: int = 2,
) -> Path:
    spec = ARCHETYPE_D_STALE
    provider = get_provider(provider_name, model=model)  # type: ignore[arg-type]
    toxiproxy_url = spec.toxiproxy_url or "http://localhost:8474"

    with stack_session("broken", spec=spec) as session, agent_workspace_session(spec=spec) as workspace:
        if linux_exec:
            debug_cm = debug_container_for_spec(spec, workspace.root)
            # Match the env var names the source reads, so an agent that learns
            # them from main.go can connect without the prompt naming topology.
            debug_cm.env["PRIMARY_URL"] = spec.internal_database_url
            debug_cm.env["REPLICA_URL"] = INTERNAL_REPLICA_URL
            debug_cm.env["TOXIPROXY_URL"] = "http://toxiproxy:8474"
        else:
            debug_cm = contextlib.nullcontext(None)

        if chaos_mode:
            # Let the initial seed replicate, then inject chaos for the whole
            # session so the stale read is actively manifesting (not calm).
            import time as _time

            _time.sleep(3)
            if chaos_mode == "partition":
                _set_replication(False, toxiproxy_url)
                print("[chaos] replication partitioned for the session")
            else:  # lag — canonical recovering-lag manifestation
                _add_replication_lag(chaos_lag_ms, toxiproxy_url)
                print(f"[chaos] replication lag {chaos_lag_ms}ms (downstream) for the session")

        try:
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
                # A delivered patch must compile (same Dockerfile the oracle
                # builds), else its build stderr is fed back for a bounded retry.
                # Closes the "non-compiling patch scored TRUE_FIX" hole (report 7).
                build_fn = (
                    make_workspace_build_check(spec, workspace.root) if build_check else None
                )
                result = run_agent_loop(
                    provider,
                    executor,
                    system=SYSTEM,
                    initial_user=USER,
                    max_turns=max_turns,
                    pipeline=pipeline,
                    build_check=build_fn,
                    max_build_retries=max_build_retries,
                )
        finally:
            if chaos_mode:
                _reset_replication(toxiproxy_url)

    patch = sanitize_model_patch(result.submitted_patch or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = log_root / f"{stamp}_{provider_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    pipeline.dump(run_dir / "patch_pipeline.jsonl")

    # Grade-time determinism guard: the saved artifact MUST re-apply cleanly, so
    # what is graded is byte-identical to what was delivered. If it does not
    # re-apply, a save/normalization step corrupted it (cf. the sanitize-strip
    # bug that truncated a trailing blank context line) — record + warn loudly
    # here instead of letting it surface as silent "ungradeable" attrition in a
    # Phase 6 sweep.
    patch_reapplies = None
    if patch:
        patch_reapplies = check_patch_applies_fast(patch, spec=spec) is None
        if not patch_reapplies:
            print(f"  [WARN] saved model_patch.diff does NOT re-apply for {run_dir.name}")

    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "gate": "archetype-d-stale-read-legibility",
                "provider": provider_name,
                "model": model,
                "turns_used": result.turns_used,
                "patch_bytes": len(patch),
                "patch_source": result.patch_source,
                "patch_death_stage": pipeline.death_stage(),
                "patch_reapplies": patch_reapplies,
                "chaos_mode": chaos_mode,
                "chaos_lag_ms": chaos_lag_ms if chaos_mode == "lag" else None,
                "build_check": build_check,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "trajectory.json").write_text(
        json.dumps(_serialize_history(result), indent=2), encoding="utf-8"
    )
    if patch:
        # Write LF-faithful bytes (no platform CRLF translation) so the saved
        # artifact is byte-identical to what applied at gate time.
        (run_dir / "model_patch.diff").write_text(patch, encoding="utf-8", newline="\n")
    elif result.rejected_patch:
        # Stage-5 death: keep the bytes git apply rejected (e.g. "corrupt patch
        # at line N") so the failure is debuggable instead of a 0-byte mystery.
        (run_dir / "rejected_patch.diff").write_text(
            result.rejected_patch, encoding="utf-8", newline="\n"
        )

    # classify_legibility_temporal.py is the authoritative (correctness-aware,
    # verbalization-independent) Test-2 instrument; the others are kept for
    # continuity. diagnosis_order is deprecated (prose-based) but still emitted.
    for script in (
        "classify_legibility_temporal.py",
        "classify_legibility_gate.py",
        "classify_diagnosis_order.py",
    ):
        extra = ["--write"] if script in ("classify_diagnosis_order.py", "classify_legibility_temporal.py") else []
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), str(run_dir), *extra],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if proc.stdout:
            print(proc.stdout)

    print(f"run={run_dir} patch_bytes={len(patch)}")
    return run_dir


def _summarize(run_dirs: list[Path]) -> None:
    """Aggregate delivery + Test-2 across k runs per vendor (replicated view)."""
    from collections import defaultdict

    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for rd in run_dirs:
        try:
            meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        order = {}
        op = rd / "legibility_order.json"
        if op.exists():
            try:
                order = json.loads(op.read_text(encoding="utf-8"))
            except Exception:
                order = {}
        by_vendor[meta.get("provider", "?")].append({"meta": meta, "order": order})

    print("\n================ k-shot summary ================")
    for vendor, runs in sorted(by_vendor.items()):
        k = len(runs)
        delivered = sum(1 for r in runs if (r["meta"].get("patch_bytes") or 0) > 0)
        t2 = [r["order"].get("test2") for r in runs]
        t2_pass = t2.count("PASS")
        t2_fail = t2.count("FAIL")
        deaths = [r["meta"].get("patch_death_stage") for r in runs if (r["meta"].get("patch_bytes") or 0) == 0]
        print(
            f"{vendor:10s} k={k}  delivered={delivered}/{k}  "
            f"Test2 PASS={t2_pass} FAIL={t2_fail} other={k - t2_pass - t2_fail}  "
            f"deaths={[d for d in deaths if d] or '-'}"
        )
    print("===============================================")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stale-read prototype legibility gate")
    parser.add_argument("--provider", choices=["anthropic", "openai", "gemini"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--all-vendors", action="store_true")
    parser.add_argument("--k", type=int, default=1, help="repeats per vendor (k-shot)")
    parser.add_argument(
        "--chaos",
        action="store_true",
        help="legacy alias for --chaos-mode partition",
    )
    parser.add_argument(
        "--chaos-mode",
        choices=["partition", "lag"],
        default=None,
        help="partition = sustained cut; lag = recovering latency (canonical, fairer)",
    )
    parser.add_argument("--chaos-lag-ms", type=int, default=20000)
    parser.add_argument(
        "--no-build-check",
        action="store_true",
        help="disable the compile gate (a delivered patch must build, else "
        "the build stderr is fed back to the model for a bounded retry)",
    )
    parser.add_argument("--max-build-retries", type=int, default=2)
    parser.add_argument("--no-linux-exec", action="store_true")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=ROOT / "archetype-d-stale-read" / "legibility_gate_runs",
    )
    args = parser.parse_args()
    linux_exec = not args.no_linux_exec
    chaos_mode = args.chaos_mode or ("partition" if args.chaos else None)

    if args.all_vendors:
        matrix = VENDOR_MATRIX
    else:
        if not args.provider:
            parser.error("specify --provider or --all-vendors")
        model = args.model or dict(VENDOR_MATRIX).get(args.provider, "")
        if not model:
            parser.error("--model required for this provider")
        matrix = [(args.provider, model)]

    run_dirs: list[Path] = []
    failures = 0
    for vendor, model in matrix:
        for i in range(args.k):
            tag = f" [{i + 1}/{args.k}]" if args.k > 1 else ""
            print(f"\n=== {vendor} / {model}{tag} ===")
            try:
                rd = run_one(
                    vendor,
                    model,
                    max_turns=args.max_turns,
                    log_root=args.log_dir,
                    linux_exec=linux_exec,
                    chaos_mode=chaos_mode,
                    chaos_lag_ms=args.chaos_lag_ms,
                    build_check=not args.no_build_check,
                    max_build_retries=args.max_build_retries,
                )
                run_dirs.append(rd)
            except Exception as exc:
                print(f"BLOCKED: {vendor}: {exc}")
                failures += 1

    if len(run_dirs) > 1:
        _summarize(run_dirs)
        # Authoritative correctness-aware Test-2 rollup across the whole sweep.
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "classify_legibility_temporal.py"),
                str(args.log_dir),
                "--summary",
            ],
            cwd=ROOT,
        )

    assert_corpus_unchanged(spec=ARCHETYPE_D_STALE)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
