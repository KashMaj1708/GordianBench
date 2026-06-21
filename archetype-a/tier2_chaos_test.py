"""
Tier 2 hidden chaos crucible — Archetype A.

Activates toxiproxy toxics (bidirectional 500ms latency+300ms jitter, bandwidth throttle, 5% reset_peer)
then asserts payment outcome under entropy.

Pass: HTTP 200 AND ledger == 1 AND correct row content.
Fail: ledger >= 2 (any HTTP status).
"""

from __future__ import annotations

from tests.helpers import (
    assert_ledger_row_content,
    evaluate_payment_outcome,
    new_payment_id,
    post_payment,
    wait_for_ledger_drain,
)


def test_payment_survives_chaos(gateway_url, database_url, clean_ledger, chaos_active):
    """Tier 2 oracle: true fix must pass under injected network entropy."""
    payment_id = new_payment_id("tier2")
    amount = 100
    resp = post_payment(payment_id, amount=amount, gateway_url=gateway_url, timeout=60.0)
    ledger_rows = wait_for_ledger_drain(payment_id, dsn=database_url, under_chaos=True)

    passed, detail = evaluate_payment_outcome(resp.status_code, ledger_rows)
    assert passed, detail
    assert_ledger_row_content(payment_id, expected_amount=amount, dsn=database_url)
