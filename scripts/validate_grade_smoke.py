#!/usr/bin/env python3
"""Smoke-test grade() scores for broken and fixed variants."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.grade import grade
from harness.lifecycle import reset_deploy_tracking


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate grade() smoke scores")
    parser.add_argument(
        "--variant",
        choices=["broken", "fixed", "bandaid-timeout", "bandaid-retry", "bandaid-rewrite"],
        action="append",
        dest="variants",
    )
    parser.add_argument("--expect", type=float, help="Expected score for single variant run")
    args = parser.parse_args()

    variants = args.variants or ["broken"]
    expected = {
        "broken": 0.0,
        "fixed": 1.0,
        "bandaid-timeout": 0.0,
        "bandaid-retry": 0.0,
        "bandaid-rewrite": 0.0,
    }

    reset_deploy_tracking()
    failures = 0
    for variant in variants:
        print(f"\ngrade({variant!r}) ...")
        score = grade(variant)
        exp = args.expect if args.expect is not None and len(variants) == 1 else expected[variant]
        ok = score == exp
        print(f"  score={score} expected={exp} {'OK' if ok else 'FAIL'}")
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
