"""Observation helpers for archetype-d-stale-read (stale-read-after-failover)."""

from __future__ import annotations

import os
import time

import requests

API_URL = os.environ.get("API_URL", os.environ.get("GATEWAY_URL", "http://localhost:8083"))
PRIMARY_URL = os.environ.get("PRIMARY_URL", "postgresql://bench:bench@localhost:5435/ledger")
REPLICA_URL = os.environ.get("REPLICA_URL", "postgresql://bench:bench@localhost:5436/ledger")

ACCOUNT = "acct-1"
PIN_TTL_MS = int(os.environ.get("PIN_TTL_MS", "250"))


def wait_for_api(*, timeout: float = 180.0, url: str | None = None) -> None:
    base = (url or API_URL).rstrip("/")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base}/health", timeout=3)
            if resp.status_code == 200:
                return
            last_err = f"status {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(1)
    raise TimeoutError(f"API not healthy at {base}: {last_err}")


def deposit(amount_cents: int, *, account: str = ACCOUNT, api_url: str | None = None) -> dict:
    base = (api_url or API_URL).rstrip("/")
    resp = requests.post(
        f"{base}/deposit",
        json={"account": account, "amount_cents": amount_cents},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def read_balance(*, account: str = ACCOUNT, api_url: str | None = None) -> dict:
    base = (api_url or API_URL).rstrip("/")
    resp = requests.get(f"{base}/balance", params={"account": account}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _pg(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn)


def reset_account(value: int = 0, *, account: str = ACCOUNT) -> None:
    conn = _pg(PRIMARY_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, balance_cents) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET balance_cents = EXCLUDED.balance_cents",
                (account, value),
            )
        conn.commit()
    finally:
        conn.close()


def replica_balance(*, account: str = ACCOUNT) -> int:
    conn = _pg(REPLICA_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance_cents FROM accounts WHERE id = %s", (account,))
            row = cur.fetchone()
            return int(row[0]) if row else -1
    finally:
        conn.close()


def wait_for_replica_value(value: int, *, account: str = ACCOUNT, timeout: float = 20.0) -> bool:
    """Block until the replica has caught up to `value` (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if replica_balance(account=account) == value:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def evaluate_read_your_writes(*, written: int, observed: int) -> tuple[bool, str]:
    """Pass when a read issued after an acknowledged write reflects that write."""
    if observed != written:
        return (
            False,
            f"stale read: wrote {written} but read {observed} "
            f"(read-your-writes violated)",
        )
    return True, "ok"
