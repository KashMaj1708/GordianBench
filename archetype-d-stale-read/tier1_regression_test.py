"""Tier 1 regression oracle for archetype-d-stale-read.

Invariant (emergent, not named in source): a read issued AFTER an acknowledged
write must reflect that write (read-your-writes), even while the replica is
partitioned from the primary.

Deterministic reproduction: partition the replication stream, write to the
primary, wait past the read-after-write pin window, then read. On the broken
service the read is served by the frozen replica and returns the pre-write value.
"""

from __future__ import annotations

import time

import pytest

from tests.helpers import (
    PIN_TTL_MS,
    deposit,
    evaluate_read_your_writes,
    read_balance,
    reset_account,
    wait_for_api,
    wait_for_replica_value,
)
from tests.replication_chaos import (
    heal_replication,
    partition_replication,
    reset_replication,
)

TRIALS = 5
BASELINE = 1000
DELTA = 500


def _read_after_write_under_partition() -> tuple[int, dict]:
    """Return (written_value, balance_response) after a write under partition."""
    # Clean baseline: replica caught up to BASELINE, then cut replication.
    reset_replication()
    reset_account(BASELINE)
    assert wait_for_replica_value(BASELINE), "replica never reached baseline"

    partition_replication()
    try:
        ack = deposit(DELTA)
        written = int(ack["balance_cents"])
        # Wait past the read-after-write pin window so the read routes normally.
        time.sleep((PIN_TTL_MS / 1000.0) + 0.45)
        balance = read_balance()
        return written, balance
    finally:
        heal_replication()


@pytest.fixture(scope="module", autouse=True)
def _ready():
    wait_for_api()
    yield
    reset_replication()


def test_read_your_writes_under_partition():
    failures = []
    for trial in range(TRIALS):
        written, balance = _read_after_write_under_partition()
        ok, detail = evaluate_read_your_writes(
            written=written, observed=int(balance["balance_cents"])
        )
        if not ok:
            failures.append(f"trial {trial}: {detail} (source={balance.get('source')})")
        time.sleep(0.5)

    assert not failures, "read-your-writes violated:\n" + "\n".join(failures)
