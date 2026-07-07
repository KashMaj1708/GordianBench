#!/usr/bin/env python3
"""
Tier-2 chaos oracle for archetype-d-stale-read.

The authoritative correctness signal for the stale-read archetype: under a real
replica partition, does a read issued after an acknowledged write reflect that
write (read-your-writes)? This is what turns a static "the patch contains LSN SQL"
guess into "the patch actually holds under partition," and it is what a Phase-6
resolution rate for this archetype must be graded against.

Discrimination the oracle asserts (per STALE_READ_DESIGN.md):

  broken                FAIL  — wall-clock pin + partition fallback -> stale after pin
  bandaid-ttl           FAIL  — bigger TTL; sustained partition outlasts any finite TTL
  bandaid-sleep         FAIL  — sleep-before-read; frozen replica never catches up
  bandaid-retry         FAIL  — retry the read; frozen replica returns same stale value
  bandaid-precommit-lsn FAIL  — correct LSN gate, but the watermark is captured
                                pre-commit (inside the UPDATE) so it is already
                                replayed -> gate is a no-op (the P3 model error).
                                LAG-ONLY: under sustained partition the frozen
                                replica never clears even the early watermark, so
                                it routes to primary and is indistinguishable from
                                the fix; the defect only manifests as the replica
                                recovers (asserted under the lag profile only).
  fixed (LSN)           PASS  — serve primary until replica has applied the write's LSN

A variant FAILS the invariant when it produces stale reads under partition; the
true fix PASSES (zero stale). The oracle is GREEN when every variant matches its
expectation deterministically (FAIL variants stale on >= THRESHOLD of trials,
the fix stale on none).

Runs Docker. From the repo root:
    .venv\\Scripts\\python.exe archetype-d-stale-read\\tier2_oracle.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ARCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ARCH))

# Host-mapped endpoints (compose publishes these ports).
os.environ.setdefault("API_URL", "http://localhost:8083")
os.environ.setdefault("PRIMARY_URL", "postgresql://bench:bench@localhost:5435/ledger")
os.environ.setdefault("REPLICA_URL", "postgresql://bench:bench@localhost:5436/ledger")
os.environ.setdefault("TOXIPROXY_URL", "http://localhost:8474")

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

BASE = "docker-compose.yml"
BASELINE = 1000
DELTA = 500
THRESHOLD = 0.8  # FAIL variants must be stale on >= 80% of trials (PHASE0 target)

# Recovering-lag profile: a latency toxic delays (does not cut) the replication
# stream, so the replica DOES catch up — after this delay. The delay must exceed
# every band-aid's fixed wait (max = bandaid-ttl's 5000ms pin), so that only
# verify-then-read (LSN / value catch-up) passes and blind-wait (sleep, TTL bump)
# still fails. This is the canonical manifestation of a read-your-writes
# violation (transient lag, not permanent partition) and it admits the natural
# correct-fix family that a sustained partition unfairly excludes.
DEFAULT_LAG_MS = 20000


@dataclass
class Variant:
    name: str
    overlays: list[str]
    pin_ttl_ms: int
    expect: str  # "FAIL" (invariant violated -> stale) or "PASS" (read-your-writes holds)
    # Profiles under which this variant's expectation is asserted. Most variants
    # are profile-agnostic, but the pre-commit-LSN fixture's defect is recovering-
    # lag-specific (see its definition below), so it is only meaningful under lag.
    profiles: tuple[str, ...] = ("partition", "lag")
    stale: int = 0
    trials: int = 0
    sources: set[str] = field(default_factory=set)

    @property
    def stale_rate(self) -> float:
        return self.stale / self.trials if self.trials else 0.0

    @property
    def observed(self) -> str:
        # The variant "FAILs" the invariant if it was stale on >= THRESHOLD of trials.
        if self.expect == "FAIL":
            return "FAIL" if self.stale_rate >= THRESHOLD else "PASS"
        return "PASS" if self.stale == 0 else "FAIL"

    @property
    def ok(self) -> bool:
        return self.observed == self.expect


VARIANTS = [
    Variant("broken", [], 250, "FAIL"),
    Variant("bandaid-ttl", ["docker-compose.bandaid-ttl.yml"], 5000, "FAIL"),
    Variant("bandaid-sleep", ["docker-compose.bandaid-sleep.yml"], 250, "FAIL"),
    Variant("bandaid-retry", ["docker-compose.bandaid-retry.yml"], 250, "FAIL"),
    # P3 regression fixture: the correct LSN gate defeated only by capturing the
    # watermark pre-commit. pin_ttl_ms=0 (no wall-clock pin) so the read happens
    # immediately and the only thing that could route to the primary is the LSN
    # gate — which this variant has rendered a no-op.
    #
    # This defect is RECOVERING-LAG-SPECIFIC and is asserted under lag only. Under
    # a sustained partition the replica is frozen *below* even the (too-early) pre-
    # commit watermark, so the gate never clears and routes to the primary -> reads
    # look fresh and it is indistinguishable from the true fix. The bug only bites
    # when the replica recovers and races *past* the pre-commit watermark before it
    # has applied the actually-committed row, which is precisely the lag profile.
    # (Validated empirically: A3 corpus run, partition PASS 1/10 vs lag FAIL 10/10.)
    Variant(
        "bandaid-precommit-lsn",
        ["docker-compose.bandaid-precommit-lsn.yml"],
        0,
        "FAIL",
        profiles=("lag",),
    ),
    Variant("fixed", ["docker-compose.fixed.yml"], 0, "PASS"),
]


def _compose(args: list[str], overlays: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", BASE]
    for o in overlays:
        cmd += ["-f", o]
    cmd += args
    return subprocess.run(cmd, cwd=ARCH, text=True, check=check)


def _inject_chaos(profile: str, lag_ms: int) -> None:
    if profile == "partition":
        partition_replication()
    else:  # lag
        add_replication_lag(lag_ms)


def _chaos_trial(pin_ttl_ms: int, profile: str, lag_ms: int) -> tuple[int, int, str] | None:
    """Inject chaos, write, wait past the pin window, read. Return (written, observed, source)."""
    reset_replication()
    reset_account(BASELINE)
    if not wait_for_replica_value(BASELINE):
        return None  # replica never reached baseline; skip (not counted)

    _inject_chaos(profile, lag_ms)
    try:
        ack = deposit(DELTA)
        written = int(ack["balance_cents"])
        time.sleep((pin_ttl_ms / 1000.0) + 0.45)
        bal = read_balance()
        return written, int(bal["balance_cents"]), str(bal.get("source", "?"))
    finally:
        # Clear toxics + re-enable so the replica recovers before the next trial.
        reset_replication()


def _run_variant(v: Variant, trials: int, *, profile: str, lag_ms: int) -> None:
    print(f"\n=== variant: {v.name} (expect {v.expect}) ===")
    # Recreate only ledger-api with this variant's image; keep the replication stack.
    up_args = ["up", "-d", "--build", "--no-deps", "ledger-api"]
    _compose(up_args, v.overlays)
    wait_for_api(timeout=120)
    # Settle: let the freshly-(re)started service establish pools.
    time.sleep(2)

    for i in range(trials):
        res = _chaos_trial(v.pin_ttl_ms, profile, lag_ms)
        if res is None:
            print(f"  trial {i}: skipped (replica baseline timeout)")
            continue
        written, observed, source = res
        v.trials += 1
        v.sources.add(source)
        stale = observed != written
        v.stale += int(stale)
        flag = "STALE" if stale else "fresh"
        print(f"  trial {i}: wrote={written} read={observed} src={source} -> {flag}")
        time.sleep(0.3)

    print(
        f"  -> {v.name}: stale {v.stale}/{v.trials} (rate {v.stale_rate:.0%}); "
        f"observed={v.observed} expected={v.expect} {'OK' if v.ok else 'MISMATCH'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier-2 chaos oracle (stale-read)")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument(
        "--profile",
        choices=["partition", "lag"],
        default="partition",
        help="partition = sustained cut; lag = recovering latency toxic (canonical)",
    )
    parser.add_argument("--lag-ms", type=int, default=DEFAULT_LAG_MS)
    parser.add_argument("--keep-up", action="store_true", help="leave the stack running at the end")
    parser.add_argument(
        "--only", nargs="*", help="restrict to named variants (default: all)"
    )
    args = parser.parse_args()

    variants = [v for v in VARIANTS if not args.only or v.name in args.only]
    # Drop variants whose defect is not meaningful under this profile (e.g. the
    # pre-commit-LSN fixture is recovering-lag-specific). An explicit --only keeps
    # the variant regardless, so the profile dependence can still be inspected.
    if not args.only:
        skipped = [v.name for v in variants if args.profile not in v.profiles]
        if skipped:
            print(f"(skipping profile-inapplicable variants under {args.profile}: {', '.join(skipped)})")
        variants = [v for v in variants if args.profile in v.profiles]

    print(
        f"Bringing up the replication stack (broken baseline); "
        f"profile={args.profile}"
        + (f" lag={args.lag_ms}ms" if args.profile == "lag" else "")
        + " ..."
    )
    _compose(["up", "-d", "--build"], [])
    try:
        wait_for_api(timeout=180)
        for v in variants:
            _run_variant(v, args.trials, profile=args.profile, lag_ms=args.lag_ms)
            reset_replication()
    finally:
        if not args.keep_up:
            print("\nTearing down...")
            _compose(["down", "-v", "--remove-orphans"], [], check=False)

    print("\n================ Tier-2 oracle summary ================")
    all_ok = True
    for v in variants:
        all_ok = all_ok and v.ok
        mark = "GREEN" if v.ok else "RED  "
        print(
            f"  [{mark}] {v.name:14s} expect={v.expect:4s} observed={v.observed:4s} "
            f"stale={v.stale}/{v.trials} sources={sorted(v.sources)}"
        )
    print("======================================================")
    if all_ok:
        print("Tier-2 oracle: GREEN — band-aids FAIL, LSN fix PASSES, deterministic.")
    else:
        print("Tier-2 oracle: RED — discrimination broken; see mismatches above.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
