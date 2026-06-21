"""Shared helpers for Tier 1/2 regression tests."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field

import psycopg2
import requests

DEFAULT_GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://bench:bench@localhost:5433/payments"
)

SETTLE_TIMEOUT_SEC = float(os.environ.get("TIER1_SETTLE_TIMEOUT_SEC", "12"))
SETTLE_STABLE_SEC = float(os.environ.get("TIER1_SETTLE_STABLE_SEC", "1.0"))

CHAOS_SETTLE_TIMEOUT_SEC = float(os.environ.get("TIER2_SETTLE_TIMEOUT_SEC", "45"))
CHAOS_STABLE_SEC = float(os.environ.get("TIER2_SETTLE_STABLE_SEC", "2.0"))

CLIENT_TIMEOUT_MS = int(os.environ.get("CLIENT_TIMEOUT_MS", "2000"))
COMMIT_LATENCY_MS = int(os.environ.get("COMMIT_LATENCY_MS", "2200"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
CHAOS_LATENCY_MS = int(os.environ.get("CHAOS_LATENCY_MS", "2000"))
CHAOS_JITTER_MS = int(os.environ.get("CHAOS_JITTER_MS", "200"))
DRAIN_MARGIN_MS = int(os.environ.get("DRAIN_MARGIN_MS", "2000"))
DRAIN_POLL_INTERVAL_SEC = float(os.environ.get("DRAIN_POLL_INTERVAL_SEC", "0.25"))


@dataclass(frozen=True)
class LedgerRow:
    payment_id: str
    amount: int


@dataclass
class DrainResult:
    final_count: int
    elapsed_sec: float
    min_elapsed_sec: float
    trajectory: list[tuple[float, int]] = field(default_factory=list)
    first_stable_count: int | None = None
    first_stable_at_sec: float | None = None
    late_insert: bool = False

    @property
    def peak_count(self) -> int:
        if not self.trajectory:
            return self.final_count
        return max(count for _, count in self.trajectory)

    def to_dict(self) -> dict:
        return {
            "final_count": self.final_count,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "min_elapsed_sec": round(self.min_elapsed_sec, 3),
            "trajectory": [(round(t, 3), c) for t, c in self.trajectory],
            "first_stable_count": self.first_stable_count,
            "first_stable_at_sec": (
                round(self.first_stable_at_sec, 3) if self.first_stable_at_sec is not None else None
            ),
            "late_insert": self.late_insert,
        }


@dataclass
class PaymentTrace:
    payment_id: str
    http_status: int
    round_trip_sec: float
    drain: DrainResult

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "http_status": self.http_status,
            "round_trip_sec": round(self.round_trip_sec, 3),
            "drain": self.drain.to_dict(),
        }


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
    """
    Lower bound before late upstream handlers can be considered finished.

    Under chaos, biases toward over-waiting: includes reset_peer retry budget
    (one full client-timeout + commit cycle per attempt) on top of latency chain.
    """
    per_attempt_ms = client_timeout_ms
    reset_chain_ms = 0
    if under_chaos:
        per_attempt_ms += chaos_latency_ms + chaos_jitter_ms
        # reset_peer can abort after upstream started commit; budget worst-case
        # reset-and-retry chain, not just latency — false early drain >> slow drain
        reset_chain_ms = (max_retries + 1) * (client_timeout_ms + commit_latency_ms)
    attempts_ms = (max_retries + 1) * per_attempt_ms
    return (attempts_ms + commit_latency_ms + reset_chain_ms + margin_ms) / 1000.0


def wait_for_ledger_settlement(
    payment_id: str,
    dsn: str = DEFAULT_DATABASE_URL,
    timeout: float = SETTLE_TIMEOUT_SEC,
    stable_for: float = SETTLE_STABLE_SEC,
    min_elapsed: float = 0.0,
    trace: bool = False,
) -> int | DrainResult:
    """Wait until ledger row count stops changing."""
    started = time.time()
    deadline = started + timeout
    last_count = -1
    stable_since: float | None = None
    trajectory: list[tuple[float, int]] = []
    first_stable_count: int | None = None
    first_stable_at_sec: float | None = None
    late_insert = False

    while time.time() < deadline:
        count = count_ledger_rows(payment_id, dsn=dsn)
        elapsed = time.time() - started
        if trace:
            if not trajectory or trajectory[-1][1] != count:
                trajectory.append((elapsed, count))

        if count == last_count and count >= 0:
            if stable_since is None:
                stable_since = time.time()
                if first_stable_count is None:
                    first_stable_count = count
                    first_stable_at_sec = elapsed
            elif (
                time.time() - stable_since >= stable_for
                and elapsed >= min_elapsed
            ):
                if trace:
                    return DrainResult(
                        final_count=count,
                        elapsed_sec=elapsed,
                        min_elapsed_sec=min_elapsed,
                        trajectory=trajectory,
                        first_stable_count=first_stable_count,
                        first_stable_at_sec=first_stable_at_sec,
                        late_insert=late_insert,
                    )
                return count
        else:
            if (
                first_stable_count is not None
                and count != first_stable_count
                and count > first_stable_count
            ):
                late_insert = True
            stable_since = None
        last_count = count
        time.sleep(DRAIN_POLL_INTERVAL_SEC)

    final = count_ledger_rows(payment_id, dsn=dsn)
    elapsed = time.time() - started
    if trace:
        if not trajectory or trajectory[-1][1] != final:
            trajectory.append((elapsed, final))
        return DrainResult(
            final_count=final,
            elapsed_sec=elapsed,
            min_elapsed_sec=min_elapsed,
            trajectory=trajectory,
            first_stable_count=first_stable_count,
            first_stable_at_sec=first_stable_at_sec,
            late_insert=late_insert,
        )
    return final


def wait_for_ledger_drain(
    payment_id: str,
    dsn: str = DEFAULT_DATABASE_URL,
    under_chaos: bool = False,
    trace: bool = False,
) -> int | DrainResult:
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
        trace=trace,
    )


def post_payment_traced(
    payment_id: str,
    amount: int = 100,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout: float = 45.0,
    database_url: str = DEFAULT_DATABASE_URL,
    under_chaos: bool = False,
) -> PaymentTrace:
    """Post payment and drain ledger with optional chaos trace."""
    trace_enabled = os.environ.get("TIER2_TRACE", "").lower() in ("1", "true", "yes")
    started = time.time()
    resp = post_payment(payment_id, amount=amount, gateway_url=gateway_url, timeout=timeout)
    round_trip_sec = time.time() - started
    drain_result = wait_for_ledger_drain(
        payment_id,
        dsn=database_url,
        under_chaos=under_chaos,
        trace=trace_enabled,
    )
    if isinstance(drain_result, DrainResult):
        return PaymentTrace(
            payment_id=payment_id,
            http_status=resp.status_code,
            round_trip_sec=round_trip_sec,
            drain=drain_result,
        )
    return PaymentTrace(
        payment_id=payment_id,
        http_status=resp.status_code,
        round_trip_sec=round_trip_sec,
        drain=DrainResult(
            final_count=drain_result,
            elapsed_sec=0.0,
            min_elapsed_sec=0.0,
        ),
    )


def emit_chaos_trace(trace: PaymentTrace) -> None:
    """Append one JSON line to tier2_chaos_trace.jsonl when tracing is enabled."""
    if os.environ.get("TIER2_TRACE", "").lower() not in ("1", "true", "yes"):
        return
    path = os.environ.get("TIER2_TRACE_FILE", "tier2_chaos_trace.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace.to_dict()) + "\n")


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


def evaluate_tier2_outcome(
    http_status: int,
    ledger_rows: int,
    *,
    peak_rows: int | None = None,
) -> tuple[bool, str]:
    """
    Tier 2: ledger is authoritative under chaos.
    Single row passes even if the client sees 502 after retry exhaustion.

    peak_rows is tracked for trace/evidence. Ledger rows are append-only (no
    mid-test deletes), so peak >= 2 implies final >= 2 after drain completes.
    Primary discrimination for the maxRetries=0 hole is the double-POST probe
    (second client request), not peak trajectory alone.
    """
    peak = peak_rows if peak_rows is not None else ledger_rows
    if peak >= 2:
        return False, f"duplicate charge detected: peak {peak} ledger rows"
    if ledger_rows == 1:
        return True, f"single ledger row under chaos (HTTP {http_status})"
    if ledger_rows >= 2:
        return False, f"duplicate charge detected: {ledger_rows} ledger rows"
    return False, f"no charge recorded: HTTP {http_status}, ledger_rows={ledger_rows}"


# Back-compat alias
evaluate_tier1_outcome = evaluate_payment_outcome
