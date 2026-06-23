#!/usr/bin/env python3
"""
Adversarial 20-cycle grade() leak test (SCAFFOLD — dry-run by default).

Cycles variants in an order that forces teardown between mismatched states:
  broken → fixed → bandaid-timeout → fixed → bandaid-retry → broken → ...

Repeating one variant 20× would not catch stale-image or leftover-toxic bugs.

Usage:
  python scripts/run_grade_leak_test.py --dry-run          # print sequence only
  python scripts/run_grade_leak_test.py --cycles 20        # run grade() each step (slow)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Adversarial sequence: every step switches variant or chaos state
ADVERSARIAL_SEQUENCE: list[str] = [
    "broken",
    "fixed",
    "bandaid-timeout",
    "fixed",
    "bandaid-retry",
    "broken",
    "bandaid-rewrite",
    "fixed",
    "bandaid-timeout",
    "broken",
    "fixed",
    "bandaid-retry",
    "bandaid-rewrite",
    "fixed",
    "broken",
    "bandaid-timeout",
    "bandaid-retry",
    "fixed",
    "broken",
    "fixed",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Adversarial grade() leak test")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sequence = ADVERSARIAL_SEQUENCE[: args.cycles]
    if len(sequence) < args.cycles:
        # extend by cycling adversarial pattern
        i = 0
        while len(sequence) < args.cycles:
            sequence.append(ADVERSARIAL_SEQUENCE[i % len(ADVERSARIAL_SEQUENCE)])
            i += 1

    print(f"Adversarial grade leak test: {args.cycles} cycles")
    for i, variant in enumerate(sequence, 1):
        print(f"  {i:2d}. {variant}")

    if args.dry_run:
        print("\n(dry-run — grade() not invoked; scaffold only)")
        return 0

    from harness.grade import grade
    from harness.hygiene import assert_resource_hygiene

    failures = 0
    for i, variant in enumerate(sequence, 1):
        print(f"\n--- cycle {i}/{args.cycles}: {variant} ---")
        try:
            score = grade(variant, teardown=True)
            print(f"  score: {score}")
        except Exception as exc:
            print(f"  grade() error: {exc}")
            failures += 1
            continue
        hygiene = assert_resource_hygiene()
        if not hygiene.ok:
            print(f"  LEAK: {hygiene.to_dict()}")
            failures += 1

    print(f"\nDone: {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
