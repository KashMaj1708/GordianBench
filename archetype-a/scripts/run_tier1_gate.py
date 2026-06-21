#!/usr/bin/env python3
"""
Phase 1 determinism gate: run Tier 1 pytest N times and verify zero flips.

Broken src: every run must FAIL (pytest exit code 1).
Fixed src:  every run must PASS (pytest exit code 0).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1]


def run_pytest_once(host: bool, compose_files: list[str]) -> int:
    if host:
        cmd = [sys.executable, "-m", "pytest", "tier1_regression_test.py", "-q"]
        return subprocess.run(cmd, cwd=ARCHETYPE_ROOT, check=False).returncode

    base = ["docker", "compose"]
    for f in compose_files:
        base.extend(["-f", f])
    base.extend(["--profile", "tier1", "run", "--rm", "tier1-runner", "-q"])
    return subprocess.run(base, cwd=ARCHETYPE_ROOT, check=False).returncode


def run_gate(
    runs: int,
    expect_pass: bool,
    host: bool,
    compose_files: list[str],
) -> dict:
    outcomes: list[int] = []
    for _ in range(runs):
        code = run_pytest_once(host=host, compose_files=compose_files)
        outcomes.append(code)

    passes = sum(1 for c in outcomes if c == 0)
    fails = sum(1 for c in outcomes if c != 0)

    if expect_pass:
        flipped = fails > 0
        passed = fails == 0
    else:
        flipped = passes > 0
        passed = passes == 0

    return {
        "runs": runs,
        "expect_pass": expect_pass,
        "pass_count": passes,
        "fail_count": fails,
        "outcomes": outcomes,
        "flipped": flipped,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 1 determinism gate")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--expect-pass",
        action="store_true",
        help="Expect pytest to pass every run (fixed src)",
    )
    parser.add_argument(
        "--host",
        action="store_true",
        help="Run pytest on host instead of tier1-runner container",
    )
    parser.add_argument(
        "--compose-file",
        action="append",
        default=["docker-compose.yml", "docker-compose.tier1.yml"],
        dest="compose_files",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_gate(
        runs=args.runs,
        expect_pass=args.expect_pass,
        host=args.host,
        compose_files=args.compose_files,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        label = "fixed" if args.expect_pass else "broken"
        print(f"Tier 1 determinism gate ({label}, {'host' if args.host else 'container'})")
        print(f"  runs:        {result['runs']}")
        print(f"  pass/fail:   {result['pass_count']}/{result['fail_count']}")
        print(f"  flipped:     {result['flipped']}")
        print(f"  PASSED:      {result['passed']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
