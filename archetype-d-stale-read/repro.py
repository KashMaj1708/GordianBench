#!/usr/bin/env python3
"""Manual stale-read reproduction (Phase 0 discipline).

Demonstrates the read-your-writes violation on the broken stack and prints the
runtime evidence an agent would use to diagnose: API balance vs. direct primary
and replica balances, before and after partition.

Usage (stack must be up):
    $env:API_URL="http://localhost:8083"
    $env:PRIMARY_URL="postgresql://bench:bench@localhost:5435/ledger"
    $env:REPLICA_URL="postgresql://bench:bench@localhost:5436/ledger"
    .venv\\Scripts\\python.exe archetype-d-stale-read/repro.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.helpers import (  # noqa: E402
    PIN_TTL_MS,
    deposit,
    read_balance,
    replica_balance,
    reset_account,
    wait_for_api,
    wait_for_replica_value,
)
from tests.replication_chaos import (  # noqa: E402
    heal_replication,
    partition_replication,
    reset_replication,
)

BASELINE = 1000
DELTA = 500
RUNS = 10


def main() -> int:
    wait_for_api()
    stale = 0
    for i in range(RUNS):
        reset_replication()
        reset_account(BASELINE)
        if not wait_for_replica_value(BASELINE):
            print(f"run {i}: replica never reached baseline; skipping")
            continue

        partition_replication()
        try:
            ack = deposit(DELTA)
            written = int(ack["balance_cents"])
            time.sleep((PIN_TTL_MS / 1000.0) + 0.45)
            resp = read_balance()
            observed = int(resp["balance_cents"])
            repl = replica_balance()
            is_stale = observed != written
            stale += is_stale
            flag = "STALE" if is_stale else "fresh"
            print(
                f"run {i}: wrote={written} api_read={observed} "
                f"(source={resp['source']}) replica_direct={repl} -> {flag}"
            )
        finally:
            heal_replication()
        time.sleep(0.3)

    reset_replication()
    print(f"\nstale reads: {stale}/{RUNS}")
    return 0 if stale > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
