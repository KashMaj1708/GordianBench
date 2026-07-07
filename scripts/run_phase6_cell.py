#!/usr/bin/env python3
"""Run one Phase 6 cell: k gate runs → oracle grade each → rollup.

Uses the frozen config in PRE_PHASE6.md. Scoring consumes oracle_grade.json
(never classifier labels). See harness/rollup.py.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_phase6_cell.py \\
        --provider openai --model gpt-5.5 --k 5 \\
        --log-dir archetype-d-stale-read\\phase6_cells\\gpt-5.5
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from harness.rollup import format_table, rollup_dirs
sys.path.insert(0, str(ROOT / "scripts"))
from run_stale_read_gate import run_one  # noqa: E402


def _grade(run_dir: Path, *, profile: str, lag_ms: int, trials: int) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "grade_stale_read_patch.py"),
        str(run_dir),
        "--profile",
        profile,
        "--lag-ms",
        str(lag_ms),
        "--trials",
        str(trials),
    ]
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6: one model cell (k runs + oracle + rollup)")
    parser.add_argument("--provider", required=True, choices=["anthropic", "openai", "gemini"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--chaos-mode", default="lag", choices=["partition", "lag"])
    parser.add_argument("--chaos-lag-ms", type=int, default=20000)
    parser.add_argument("--oracle-trials", type=int, default=10)
    parser.add_argument("--oracle-profile", default="lag", choices=["partition", "lag"])
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--max-build-retries", type=int, default=2)
    args = parser.parse_args()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    run_dirs: list[Path] = []
    failures = 0

    for i in range(args.k):
        tag = f" [{i + 1}/{args.k}]"
        print(f"\n=== {args.provider} / {args.model}{tag} — gate ===")
        try:
            rd = run_one(
                args.provider,
                args.model,
                max_turns=args.max_turns,
                log_root=args.log_dir,
                linux_exec=True,
                chaos_mode=args.chaos_mode,
                chaos_lag_ms=args.chaos_lag_ms,
                build_check=True,
                max_build_retries=args.max_build_retries,
            )
            run_dirs.append(rd)
        except Exception as exc:
            failures += 1
            print(f"BLOCKED: {exc}")

    print("\n=== oracle grade each run ===")
    for rd in run_dirs:
        meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
        if not meta.get("patch_bytes"):
            print(f"  skip oracle (no patch): {rd.name}")
            continue
        if meta.get("patch_reapplies") is False:
            print(f"  skip oracle (does not re-apply): {rd.name}")
            continue
        print(f"  grading {rd.name} ...")
        rc = _grade(
            rd,
            profile=args.oracle_profile,
            lag_ms=args.chaos_lag_ms,
            trials=args.oracle_trials,
        )
        if rc != 0 and rc != 1:
            failures += 1

    rollup = rollup_dirs(run_dirs, cell_key=lambda m: m.get("model", "?"))
    table = format_table(rollup)
    print("\n=== rollup ===")
    print(table)

    summary = {
        "cell": f"{args.provider}/{args.model}",
        "k": args.k,
        "run_dirs": [str(d.resolve().relative_to(ROOT.resolve())) for d in run_dirs],
        "rollup": {
            key: {
                "counts": dict(cell.counts),
                "resolution_rate": cell.resolution_rate,
                "scored": cell.scored,
                "excluded": cell.excluded,
                "needs_rerun": cell.needs_rerun,
            }
            for key, cell in rollup.cells.items()
        },
        "frozen_config": "PRE_PHASE6.md",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out = args.log_dir / "cell_summary.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8", newline="\n")
    print(f"\ncell_summary -> {out}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
