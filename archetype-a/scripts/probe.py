#!/usr/bin/env python3
"""Phase 0 probe: fire payment requests and check ledger for double charges."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import psycopg2
import requests


def wait_for_gateway(url: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{url}/health", timeout=2)
            if resp.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError(f"gateway not healthy at {url}")


def clear_ledger(dsn: str) -> None:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ledger RESTART IDENTITY")


def count_ledger_rows(dsn: str, payment_id: str) -> int:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ledger WHERE payment_id = %s", (payment_id,))
            return int(cur.fetchone()[0])


def fire_payment(gateway_url: str, payment_id: str, amount: int = 100) -> int:
    resp = requests.post(
        f"{gateway_url}/payment",
        json={"payment_id": payment_id, "amount": amount},
        timeout=30,
    )
    return resp.status_code


def run_probe(
    gateway_url: str,
    dsn: str,
    runs: int,
    expect_double: bool,
) -> dict:
    wait_for_gateway(gateway_url)

    double_hits = 0
    single_hits = 0
    errors = 0
    details: list[dict] = []

    for i in range(runs):
        payment_id = f"pay-{uuid.uuid4().hex[:12]}"
        clear_ledger(dsn)
        status = fire_payment(gateway_url, payment_id)
        # Allow upstream retry window to finish.
        time.sleep(1)
        count = count_ledger_rows(dsn, payment_id)
        doubled = count >= 2
        if expect_double:
            if doubled:
                double_hits += 1
            elif count == 1:
                single_hits += 1
            else:
                errors += 1
        else:
            if status == 200 and count == 1:
                single_hits += 1
            elif doubled:
                double_hits += 1
            else:
                errors += 1

        details.append(
            {
                "run": i + 1,
                "payment_id": payment_id,
                "http_status": status,
                "ledger_rows": count,
                "double_charge": doubled,
            }
        )

    if expect_double:
        success_rate = double_hits / runs
        passed = success_rate >= 0.8
    else:
        success_rate = single_hits / runs
        passed = success_rate == 1.0 and errors == 0

    return {
        "runs": runs,
        "expect_double": expect_double,
        "double_hits": double_hits,
        "single_hits": single_hits,
        "errors": errors,
        "success_rate": success_rate,
        "passed": passed,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Archetype A Phase 0 probe")
    parser.add_argument("--gateway-url", default="http://localhost:8080")
    parser.add_argument(
        "--dsn",
        default="postgresql://bench:bench@localhost:5433/payments",
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--expect-double",
        action="store_true",
        help="Expect double-charge bug (broken src)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_probe(args.gateway_url, args.dsn, args.runs, args.expect_double)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        label = "broken" if args.expect_double else "fixed"
        print(f"Phase 0 probe ({label})")
        print(f"  runs:          {result['runs']}")
        print(f"  double hits:   {result['double_hits']}")
        print(f"  single hits:   {result['single_hits']}")
        print(f"  errors:        {result['errors']}")
        print(f"  success rate:  {result['success_rate']:.0%}")
        print(f"  PASSED:        {result['passed']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
