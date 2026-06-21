"""
Tier 1 visible regression gate — Archetype A.

Pass condition (AND):
  - HTTP 200 on POST /payment
  - Exactly one ledger row for the payment_id

Fail condition:
  - ledger_rows >= 2 (duplicate charge), regardless of HTTP status
  - OR HTTP != 200 with ledger != 1

This test is the FAIL_TO_PASS oracle for the SWE-bench instance manifest.
"""

from __future__ import annotations

import requests

from tests.helpers import (
    assert_ledger_row_content,
    evaluate_payment_outcome,
    new_payment_id,
    post_payment,
    wait_for_ledger_settlement,
)


def test_payment_charged_exactly_once(gateway_url, database_url, clean_ledger):
    """F2P: symptom test — payment must commit exactly once with HTTP 200."""
    payment_id = new_payment_id()
    resp = post_payment(payment_id, gateway_url=gateway_url)
    ledger_rows = wait_for_ledger_settlement(payment_id, dsn=database_url)

    passed, detail = evaluate_payment_outcome(resp.status_code, ledger_rows)
    assert passed, detail
    assert_ledger_row_content(payment_id, expected_amount=100, dsn=database_url)


def test_gateway_health(gateway_url):
    """P2P: gateway health endpoint must stay available."""
    resp = requests.get(f"{gateway_url}/health", timeout=5)
    assert resp.status_code == 200


def test_rejects_invalid_payment_payload(gateway_url, clean_ledger):
    """P2P: invalid payloads are rejected without touching the ledger."""
    resp = requests.post(
        f"{gateway_url}/payment",
        json={"payment_id": "", "amount": 100},
        timeout=10,
    )
    assert resp.status_code == 400
