#!/usr/bin/env python3
"""End-to-end Phase 6 (6a) pipeline dry-run with ScriptedProvider — no API.

Drives the ENTIRE scoring machinery with deterministic scripted agents so the
whole path is validated with no model nondeterminism and no API spend:

    scripted gate (build_check ON)  ->  sanitize + save (meta.json + model_patch.diff)
    ->  save-time re-apply guard (patch_reapplies)  ->  Tier-2 oracle grade
    ->  oracle_grade.json  ->  rollup aggregation

Two scripted agents deliver real, known patches:
  * "fixed"   -> the correct LSN fix (src-fixed)            -> must roll up PASS
  * "bandaid" -> the pre-commit-LSN band-aid (compiles,     -> must roll up
                 applies, oracle FAILs: the P3 error)          CAPABILITY_FAIL

Asserts the rollup buckets each correctly and the resolution rate is 1/2. If the
rollup ever miscategorized (e.g. a delivery-corrupt as a capability fail, or
scored by classifier instead of oracle), this fails here — for free.

Runs Docker (build_check + oracle deploy). From repo root:
    .venv\\Scripts\\python.exe scripts\\dryrun_phase6.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.loop import run_agent_loop
from agent.patch_pipeline import PipelineLogger
from agent.patch_util import sanitize_model_patch
from agent.provider import scripted
from harness.archetype_spec import ARCHETYPE_D_STALE as SPEC
from harness.build_check import make_workspace_build_check
from harness.patch import check_patch_applies_fast
from harness.rollup import (
    CAPABILITY_FAIL,
    PASS,
    categorize_run,
    format_table,
    rollup_dirs,
)
from harness.workspace import agent_workspace_session

SWEEP = SPEC.root / ".phase6_dryrun"
PROFILE = "lag"
LAG_MS = 20000
TRIALS = 10


def _content(rel_repo: str) -> str:
    return (SPEC.root / rel_repo).read_text(encoding="utf-8")


def _scripted_gate(label: str, new_main: str) -> Path:
    """Run the scripted gate for one agent; persist a run dir exactly like the gate."""
    rel = f"{SPEC.patch_services[0].src_dir}/main.go"  # ledger-api/main.go
    with agent_workspace_session(spec=SPEC) as ws:
        executor = ToolExecutor(
            ExecutorConfig(workspace_root=ws.root / "src", repo_root=ws.root, spec=SPEC)
        )
        logger = PipelineLogger()
        build_fn = make_workspace_build_check(SPEC, ws.root)  # real `docker build`
        result = run_agent_loop(
            scripted.edit_then_submit(rel, new_main),
            executor,
            system="dryrun",
            initial_user="fix it",
            max_turns=8,
            pipeline=logger,
            build_check=build_fn,
        )
        patch = sanitize_model_patch(result.submitted_patch or "")
        reapplies = check_patch_applies_fast(patch, spec=SPEC) is None if patch else None

        run_dir = SWEEP / f"scripted_{label}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "model": f"scripted-{label}",
                    "patch_bytes": len(patch),
                    "patch_source": result.patch_source,
                    "patch_reapplies": reapplies,
                    "build_check": True,
                },
                indent=2,
            ),
            encoding="utf-8",
            newline="\n",
        )
        if patch:
            (run_dir / "model_patch.diff").write_text(patch, encoding="utf-8", newline="\n")
        print(f"  [{label}] bytes={len(patch)} source={result.patch_source} reapplies={reapplies}")
        return run_dir


def _oracle_grade(run_dir: Path) -> None:
    """Invoke the real grader so it writes oracle_grade.json into the run dir."""
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "grade_stale_read_patch.py"),
        str(run_dir),
        "--profile",
        PROFILE,
        "--lag-ms",
        str(LAG_MS),
        "--trials",
        str(TRIALS),
    ]
    subprocess.run(cmd, cwd=ROOT, check=False)


def main() -> int:
    if SWEEP.exists():
        shutil.rmtree(SWEEP)
    SWEEP.mkdir(parents=True)

    print("=== 1) scripted gate (build_check on) + save + re-apply guard ===")
    fixed_dir = _scripted_gate("fixed", _content("src-fixed/ledger-api/main.go"))
    bandaid_dir = _scripted_gate(
        "bandaid", _content("patches/bandaid-precommit-lsn/ledger-api/main.go")
    )

    print("\n=== 2) Tier-2 oracle grade each delivered patch ===")
    for d in (fixed_dir, bandaid_dir):
        _oracle_grade(d)

    print("\n=== 3) rollup aggregation (consumes oracle verdicts) ===")
    r = rollup_dirs([fixed_dir, bandaid_dir])
    print(format_table(r))

    # Assertions against real artifacts.
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f" :: {detail}"))
        if not cond:
            failures.append(label)

    fixed_meta = json.loads((fixed_dir / "meta.json").read_text())
    fixed_oracle = json.loads((fixed_dir / "oracle_grade.json").read_text())
    bandaid_meta = json.loads((bandaid_dir / "meta.json").read_text())
    bandaid_oracle = json.loads((bandaid_dir / "oracle_grade.json").read_text())

    print("\n=== 4) assertions ===")
    check("fixed delivered + re-applies", fixed_meta.get("patch_reapplies") is True)
    check("bandaid delivered + re-applies", bandaid_meta.get("patch_reapplies") is True)
    check(
        "fixed oracle PASS",
        fixed_oracle.get("grade") == "PASS",
        f"grade={fixed_oracle.get('grade')} stale={fixed_oracle.get('stale')}",
    )
    check(
        "bandaid oracle FAIL",
        bandaid_oracle.get("grade") == "FAIL",
        f"grade={bandaid_oracle.get('grade')} stale={bandaid_oracle.get('stale')}",
    )
    check("rollup: fixed -> PASS", categorize_run(fixed_meta, fixed_oracle) == PASS)
    check(
        "rollup: bandaid -> CAPABILITY_FAIL",
        categorize_run(bandaid_meta, bandaid_oracle) == CAPABILITY_FAIL,
    )
    cell = r.total()
    check("rollup resolution rate = 1/2", abs(cell.resolution_rate - 0.5) < 1e-9, f"{cell.resolution_rate}")
    check("rollup: no delivery-corrupted", not cell.needs_rerun)

    if failures:
        print(f"\nPHASE 6 DRY-RUN: RED ({len(failures)} failed)")
        return 1
    print("\nPHASE 6 DRY-RUN: GREEN — full 6a scoring path validated end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
