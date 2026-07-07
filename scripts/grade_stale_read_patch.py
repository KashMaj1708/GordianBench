#!/usr/bin/env python3
"""
Oracle-grade a delivered stale-read patch against the Tier-2 chaos invariant.

The authoritative correctness signal: apply a model's patch, deploy the full
replication stack from the patched source, partition the replica, and check
read-your-writes across k trials. PASS = 0 stale (the fix holds under partition);
FAIL = stale reads (it does not). This turns the static classifier's TRUE_FIX /
PARTIAL guess into a verified pass/fail.

Usage (Docker; from repo root):
    .venv\\Scripts\\python.exe scripts\\grade_stale_read_patch.py <run_dir_or_diff> [--trials 10]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from harness.archetype_spec import ARCHETYPE_D_STALE
from harness.lifecycle import patch_session

# tests.helpers / tests.replication_chaos live under the archetype root.
import os

os.environ.setdefault("API_URL", ARCHETYPE_D_STALE.gateway_url)
os.environ.setdefault("PRIMARY_URL", "postgresql://bench:bench@localhost:5435/ledger")
os.environ.setdefault("REPLICA_URL", "postgresql://bench:bench@localhost:5436/ledger")
os.environ.setdefault("TOXIPROXY_URL", ARCHETYPE_D_STALE.toxiproxy_url or "http://localhost:8474")
sys.path.insert(0, str(ARCHETYPE_D_STALE.root))

from tests.helpers import (  # noqa: E402
    deposit,
    read_balance,
    reset_account,
    wait_for_api,
    wait_for_replica_value,
)
from tests.replication_chaos import (  # noqa: E402
    add_replication_lag,
    partition_replication,
    reset_replication,
)

BASELINE = 1000
DELTA = 500
# Wait long enough to outlast any plausible bumped pin TTL, so a TTL band-aid
# cannot false-pass by being read while still pinned to the primary.
POST_DEPOSIT_WAIT = 6.0


def _load_patch(target: Path) -> str:
    if target.is_dir():
        diff = target / "model_patch.diff"
        if not diff.exists():
            raise SystemExit(f"no model_patch.diff in {target}")
        return diff.read_text(encoding="utf-8")
    return target.read_text(encoding="utf-8")


def grade(patch_text: str, trials: int, profile: str, lag_ms: int, sources: set | None = None) -> int:
    stale = 0
    with patch_session(patch_text, spec=ARCHETYPE_D_STALE):
        wait_for_api(timeout=120)
        time.sleep(2)
        for i in range(trials):
            reset_replication()
            reset_account(BASELINE)
            if not wait_for_replica_value(BASELINE):
                print(f"  trial {i}: skipped (replica baseline timeout)")
                continue
            if profile == "partition":
                partition_replication()
            else:
                add_replication_lag(lag_ms)
            try:
                ack = deposit(DELTA)
                written = int(ack["balance_cents"])
                time.sleep(POST_DEPOSIT_WAIT)
                bal = read_balance()
                observed = int(bal["balance_cents"])
            finally:
                reset_replication()
            is_stale = observed != written
            stale += int(is_stale)
            if sources is not None:
                sources.add(str(bal.get("source", "?")))
            print(
                f"  trial {i}: wrote={written} read={observed} "
                f"src={bal.get('source')} -> {'STALE' if is_stale else 'fresh'}"
            )
            time.sleep(0.3)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle-grade a stale-read patch (Tier 2)")
    parser.add_argument("target", type=Path, help="run dir or .diff file")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--profile", choices=["partition", "lag"], default="partition")
    parser.add_argument("--lag-ms", type=int, default=20000)
    args = parser.parse_args()

    target = args.target.resolve()
    patch_text = _load_patch(target)
    print(
        f"Grading patch from {args.target} ({len(patch_text)} bytes), "
        f"{args.trials} trials, profile={args.profile}..."
    )

    sources: set[str] = set()
    stale: int | None = None
    build_err = ""
    try:
        stale = grade(patch_text, args.trials, args.profile, args.lag_ms, sources=sources)
        verdict = "PASS" if stale == 0 else "FAIL"
    except RuntimeError as exc:
        # A patch that does not compile cannot deploy: bucket it as BUILD_FAIL
        # (distinct from a capability FAIL) instead of crashing the grade. This
        # is what the rollup consumes to keep "shipped a typo" out of the
        # capability denominator.
        msg = str(exc)
        if any(
            tok in msg
            for tok in ("did not complete successfully", "failed to solve", "go build", "Building")
        ):
            verdict, build_err = "BUILD_FAIL", msg.strip().splitlines()[-1][:300]
            print(f"\nTier-2 oracle grade: BUILD_FAIL ({build_err})")
        else:
            raise

    if verdict != "BUILD_FAIL":
        print(f"\nTier-2 oracle grade: {verdict} (stale {stale}/{args.trials})")

    # Persist a structured verdict so the Phase 6 rollup consumes the ORACLE
    # result (never the classifier label). Written next to the run's meta.json.
    if target.is_dir():
        record = {
            "grade": verdict,
            "stale": stale,
            "trials": args.trials,
            "profile": args.profile,
            "lag_ms": args.lag_ms if args.profile == "lag" else None,
            "sources": sorted(sources),
            "build_error": build_err or None,
        }
        (target / "oracle_grade.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8", newline="\n"
        )

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
