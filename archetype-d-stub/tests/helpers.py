"""Observation helpers for archetype-d-stub (write-skew prototype)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API_URL = os.environ.get("API_URL", os.environ.get("GATEWAY_URL", "http://localhost:8082"))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://bench:bench@localhost:5434/ledger"
)

POOL_ACCOUNTS = ("pool-a", "pool-b")
INITIAL_EACH = 5000
INITIAL_TOTAL = 10000
POOL_RESERVE_CENTS = 8000


def wait_for_api(*, timeout: float = 120.0, url: str | None = None) -> None:
    base = (url or API_URL).rstrip("/")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base}/health", timeout=3)
            if resp.status_code == 200:
                return
            last_err = f"status {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(1)
    raise TimeoutError(f"API not healthy at {base}: {last_err}")


def fetch_balances(*, api_url: str | None = None) -> dict[str, int]:
    base = (api_url or API_URL).rstrip("/")
    resp = requests.get(f"{base}/balances", timeout=10)
    resp.raise_for_status()
    return {k: int(v) for k, v in resp.json().items()}


def reset_pool(*, each_cents: int = INITIAL_EACH) -> None:
    import psycopg2

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            for acct in POOL_ACCOUNTS:
                cur.execute(
                    "UPDATE accounts SET balance_cents = %s WHERE id = %s",
                    (each_cents, acct),
                )
        conn.commit()
    finally:
        conn.close()


def concurrent_transfers(
    *,
    amount_cents: int = 1500,
    api_url: str | None = None,
    trials: int = 1,
) -> tuple[int, int, list[int]]:
    """
    Two concurrent transfers: pool-a and pool-b each withdraw amount_cents.

    Returns (max_success_count, min_total_balance, last_status_codes).
    """
    base = (api_url or API_URL).rstrip("/")
    max_success = 0
    min_total = 10**9
    last_codes: list[int] = []

    for _ in range(trials):
        reset_pool()
        specs = [
            ("pool-a", amount_cents),
            ("pool-b", amount_cents),
        ]

        def _one(from_account: str, amount: int) -> int:
            resp = requests.post(
                f"{base}/transfer",
                json={"from_account": from_account, "amount_cents": amount},
                timeout=15,
            )
            return resp.status_code

        codes: list[int] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_one, acct, amt) for acct, amt in specs]
            for fut in as_completed(futures):
                codes.append(fut.result())

        balances = fetch_balances(api_url=api_url)
        total = sum(balances.get(a, 0) for a in POOL_ACCOUNTS)
        success = sum(1 for c in codes if c == 200)
        max_success = max(max_success, success)
        min_total = min(min_total, total)
        last_codes = codes
        if success >= 2 and total < POOL_RESERVE_CENTS:
            break

    return max_success, min_total, last_codes


def evaluate_write_skew_outcome(
    *,
    success_count: int,
    total_balance: int,
    pool_reserve: int = POOL_RESERVE_CENTS,
) -> tuple[bool, str]:
    """Pass when combined pool stays at or above the application reserve."""
    if total_balance < pool_reserve:
        return (
            False,
            f"pool reserve breached: total={total_balance} < reserve={pool_reserve} "
            f"(success_count={success_count})",
        )
    return True, "ok"
