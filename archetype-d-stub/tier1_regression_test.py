"""Calm-path regression: concurrent pool transfers must not write-skew."""

from __future__ import annotations

import pytest

from tests.helpers import (
    INITIAL_TOTAL,
    concurrent_transfers,
    evaluate_write_skew_outcome,
    reset_pool,
)


@pytest.fixture(autouse=True)
def _reset_db():
    reset_pool()
    yield
    reset_pool()


def test_concurrent_pool_transfers_no_write_skew():
    success, total, _codes = concurrent_transfers(amount_cents=1500, trials=20)
    ok, msg = evaluate_write_skew_outcome(
        success_count=success,
        total_balance=total,
    )
    assert ok, msg
