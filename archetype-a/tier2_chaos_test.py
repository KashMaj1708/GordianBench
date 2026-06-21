"""
Tier 2 hidden chaos crucible — Archetype A.

Chaos profile: bidirectional 2000+200ms latency, bandwidth throttle, 8% reset_peer.

Scope: state-synchronization correctness via ledger invariants under timeout-saturating
latency. HTTP status is not required for pass (502 + ledger=1 is valid for idempotent fix).

1. POST payment under chaos, drain ledger
2. POST same payment_id again (idempotency probe), drain again
3. POST same payment_id again (idempotency probe — primary for maxRetries hole)
4. Pass: final ledger == 1, correct content (peak tracked for trace evidence)

Set TIER2_TRACE=1 to emit per-run traces to tier2_chaos_trace.jsonl.
"""

from __future__ import annotations

import os

from tests.helpers import (
    assert_ledger_row_content,
    count_ledger_rows,
    emit_chaos_trace,
    evaluate_tier2_outcome,
    new_payment_id,
    post_payment,
    wait_for_ledger_drain,
)


def test_payment_survives_chaos(gateway_url, database_url, clean_ledger, chaos_active):
    """Tier 2 oracle: idempotent fix must hold under chaos + duplicate client POST."""
    payment_id = new_payment_id("tier2")
    amount = 100
    trace_enabled = os.environ.get("TIER2_TRACE", "").lower() in ("1", "true", "yes")

    resp1 = post_payment(payment_id, amount=amount, gateway_url=gateway_url, timeout=60.0)
    drain1 = wait_for_ledger_drain(
        payment_id, dsn=database_url, under_chaos=True, trace=True
    )
    from tests.helpers import DrainResult, PaymentTrace

    assert isinstance(drain1, DrainResult)
    peak = drain1.peak_count

    # Idempotency probe: same payment_id under continued chaos
    resp2 = post_payment(payment_id, amount=amount, gateway_url=gateway_url, timeout=60.0)
    drain2 = wait_for_ledger_drain(
        payment_id, dsn=database_url, under_chaos=True, trace=True
    )
    assert isinstance(drain2, DrainResult)
    peak = max(peak, drain2.peak_count)

    final_rows = count_ledger_rows(payment_id, dsn=database_url)
    http_status = resp2.status_code

    if trace_enabled:
        emit_chaos_trace(
            PaymentTrace(
                payment_id=payment_id,
                http_status=http_status,
                round_trip_sec=drain1.elapsed_sec + drain2.elapsed_sec,
                drain=DrainResult(
                    final_count=final_rows,
                    elapsed_sec=drain1.elapsed_sec + drain2.elapsed_sec,
                    min_elapsed_sec=drain1.min_elapsed_sec + drain2.min_elapsed_sec,
                    trajectory=drain1.trajectory + drain2.trajectory,
                    first_stable_count=drain1.first_stable_count,
                    first_stable_at_sec=drain1.first_stable_at_sec,
                    late_insert=drain1.late_insert or drain2.late_insert,
                ),
            )
        )

    passed, detail = evaluate_tier2_outcome(
        http_status, final_rows, peak_rows=peak
    )
    assert passed, detail
    assert_ledger_row_content(payment_id, expected_amount=amount, dsn=database_url)
