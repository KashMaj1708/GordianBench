"""Shared helpers for Tier 1/2 regression tests."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

import psycopg2
import requests

DEFAULT_GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://bench:bench@localhost:5433/payments"
)

SETTLE_TIMEOUT_SEC = float(os.environ.get("TIER1_SETTLE_TIMEOUT_SEC", "12"))
SETTLE_STABLE_SEC = float(os.environ.get("TIER1_SETTLE_STABLE_SEC", "1.0"))

CHAOS_SETTLE_TIMEOUT_SEC = float(os.environ.get("TIER2_SETTLE_TIMEOUT_SEC", "30"))
CHAOS_STABLE_SEC = float(os.environ.get("TIER2_SETTLE_STABLE_SEC", "2.0"))

CLIENT_TIMEOUT_MS = int(os.environ.get("CLIENT_TIMEOUT_MS", "2000"))
COMMIT_LATENCY_MS = int(os.environ.get("COMMIT_LATENCY_MS", "2200"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
CHAOS_LATENCY_MS = int(os.environ.get("CHAOS_LATENCY_MS", "500"))
CHAOS_JITTER_MS = int(os.environ.get("CHAOS_JITTER_MS", "300"))
DRAIN_MARGIN_MS = int(os.environ.get("DRAIN_MARGIN_MS", "2000"))


@dataclass(frozen=True)
class LedgerRow:
    payment_id: str
    amount: int


def wait_for_gateway(url: str = DEFAULT_GATEWAY_URL, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{url}/health", timeout=2)
            if resp.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError(f"gateway not healthy at {url}")


def clear_ledger(dsn: str = DEFAULT_DATABASE_URL) -> None:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ledger RESTART IDENTITY")


def count_ledger_rows(payment_id: str, dsn: str = DEFAULT_DATABASE_URL) -> int:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ledger WHERE payment_id = %s", (payment_id,))
            return int(cur.fetchone()[0])


def fetch_ledger_rows(payment_id: str, dsn: str = DEFAULT_DATABASE_URL) -> list[LedgerRow]:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payment_id, amount FROM ledger WHERE payment_id = %s ORDER BY id",
                (payment_id,),
            )
            return [LedgerRow(payment_id=row[0], amount=row[1]) for row in cur.fetchall()]


def assert_ledger_row_content(
    payment_id: str,
    expected_amount: int,
    dsn: str = DEFAULT_DATABASE_URL,
) -> None:
    rows = fetch_ledger_rows(payment_id, dsn=dsn)
    assert len(rows) == 1, f"expected 1 ledger row, got {len(rows)}"
    row = rows[0]
    assert row.payment_id == payment_id, f"ledger payment_id mismatch: {row.payment_id}"
    assert row.amount == expected_amount, f"ledger amount mismatch: {row.amount}"


def new_payment_id(prefix: str = "tier1") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def post_payment(
    payment_id: str,
    amount: int = 100,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout: float = 45.0,
) -> requests.Response:
    return requests.post(
        f"{gateway_url}/payment",
        json={"payment_id": payment_id, "amount": amount},
        timeout=timeout,
    )


def min_drain_seconds(
    client_timeout_ms: int = CLIENT_TIMEOUT_MS,
    commit_latency_ms: int = COMMIT_LATENCY_MS,
    max_retries: int = MAX_RETRIES,
    chaos_latency_ms: int = CHAOS_LATENCY_MS,
    chaos_jitter_ms: int = CHAOS_JITTER_MS,
    margin_ms: int = DRAIN_MARGIN_MS,
    under_chaos: bool = False,
) -> float:
    """Lower bound before late upstream handlers can be considered finished."""
    per_attempt_ms = client_timeout_ms
    if under_chaos:
        per_attempt_ms += chaos_latency_ms + chaos_jitter_ms
    attempts_ms = (max_retries + 1) * per_attempt_ms
    return (attempts_ms + commit_latency_ms + margin_ms) / 1000.0


def wait_for_ledger_settlement(
    payment_id: str,
    dsn: str = DEFAULT_DATABASE_URL,
    timeout: float = SETTLE_TIMEOUT_SEC,
    stable_for: float = SETTLE_STABLE_SEC,
    min_elapsed: float = 0.0,
) -> int:
    """Wait until ledger row count stops changing."""
    started = time.time()
    deadline = started + timeout
    last_count = -1
    stable_since: float | None = None

    while time.time() < deadline:
        count = count_ledger_rows(payment_id, dsn=dsn)
        elapsed = time.time() - started
        if count == last_count and count >= 0:
            if stable_since is None:
                stable_since = time.time()
            elif (
                time.time() - stable_since >= stable_for
                and elapsed >= min_elapsed
            ):
                return count
        else:
            stable_since = None
        last_count = count
        time.sleep(0.25)

    return count_ledger_rows(payment_id, dsn=dsn)


def wait_for_ledger_drain(
    payment_id: str,
    dsn: str = DEFAULT_DATABASE_URL,
    under_chaos: bool = False,
) -> int:
    """
    Chaos-aware drain: scaled timeout + minimum elapsed guard against late inserts.
    """
    min_elapsed = min_drain_seconds(under_chaos=under_chaos)
    timeout = max(CHAOS_SETTLE_TIMEOUT_SEC, min_elapsed + CHAOS_STABLE_SEC + 2.0)
    stable_for = CHAOS_STABLE_SEC if under_chaos else SETTLE_STABLE_SEC
    return wait_for_ledger_settlement(
        payment_id,
        dsn=dsn,
        timeout=timeout,
        stable_for=stable_for,
        min_elapsed=min_elapsed if under_chaos else 0.0,
    )


def evaluate_payment_outcome(http_status: int, ledger_rows: int) -> tuple[bool, str]:
    """
    Pass requires HTTP 200 AND exactly one ledger row.
    Fail if ledger >= 2 regardless of HTTP status.
    """
    if http_status == 200 and ledger_rows == 1:
        return True, "payment succeeded with single ledger row"
    if ledger_rows >= 2:
        return False, f"duplicate charge detected: {ledger_rows} ledger rows"
    if http_status != 200:
        return False, f"unexpected HTTP status {http_status} with {ledger_rows} ledger row(s)"
    return False, f"unexpected outcome: HTTP {http_status}, ledger_rows={ledger_rows}"


# Back-compat alias
evaluate_tier1_outcome = evaluate_payment_outcome
