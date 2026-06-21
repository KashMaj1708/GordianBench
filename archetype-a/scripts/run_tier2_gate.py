#!/usr/bin/env python3
"""
Phase 2 determinism gate: run Tier 2 chaos pytest N times per variant.

True fix (--variant fixed --expect-pass): every run must PASS.
Band-aids (--expect-pass omitted): every run must FAIL (assertion), not ERROR.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1]

VARIANT_FILES: dict[str, list[str]] = {
    "fixed": ["docker-compose.yml", "docker-compose.fixed.yml"],
    "bandaid-timeout": ["docker-compose.yml", "docker-compose.bandaid-timeout.yml"],
    "bandaid-retry": ["docker-compose.yml", "docker-compose.bandaid-retry.yml"],
    "bandaid-rewrite": ["docker-compose.yml", "docker-compose.bandaid-rewrite.yml"],
}


def compose_cmd(files: list[str], *args: str) -> list[str]:
    cmd = ["docker", "compose"]
    for f in files:
        cmd.extend(["-f", f])
    cmd.extend(args)
    return cmd


def deploy_variant(variant: str) -> None:
    files = VARIANT_FILES[variant]
    subprocess.run(
        compose_cmd(files, "up", "-d", "--build"),
        cwd=ARCHETYPE_ROOT,
        check=True,
    )


def run_pytest_once(host: bool, compose_files: list[str]) -> tuple[int, str]:
    if host:
        cmd = [sys.executable, "-m", "pytest", "tier2_chaos_test.py", "-q"]
        proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, capture_output=True, text=True)
        state = "error" if "ERROR" in proc.stdout or "ERROR" in proc.stderr else (
            "pass" if proc.returncode == 0 else "fail"
        )
        return proc.returncode, state

    cmd = compose_cmd(
        compose_files + ["docker-compose.tier1.yml"],
        "--profile",
        "tier1",
        "run",
        "--rm",
        "tier1-runner",
        "tier2_chaos_test.py",
        "-q",
    )
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, capture_output=True, text=True)
    state = "error" if "ERROR" in proc.stdout or "ERROR" in proc.stderr else (
        "pass" if proc.returncode == 0 else "fail"
    )
    return proc.returncode, state


def run_gate(
    variant: str,
    runs: int,
    expect_pass: bool,
    host: bool,
    deploy: bool,
) -> dict:
    files = VARIANT_FILES[variant]
    if deploy:
        deploy_variant(variant)

    outcomes: list[dict] = []
    for _ in range(runs):
        code, state = run_pytest_once(host=host, compose_files=files)
        outcomes.append({"exit_code": code, "state": state})

    passes = sum(1 for o in outcomes if o["state"] == "pass")
    fails = sum(1 for o in outcomes if o["state"] == "fail")
    errors = sum(1 for o in outcomes if o["state"] == "error")

    if expect_pass:
        passed = passes == runs and errors == 0
        flipped = fails > 0 or errors > 0
    else:
        passed = fails == runs and errors == 0
        flipped = passes > 0 or errors > 0

    return {
        "variant": variant,
        "runs": runs,
        "expect_pass": expect_pass,
        "pass_count": passes,
        "fail_count": fails,
        "error_count": errors,
        "outcomes": outcomes,
        "flipped": flipped,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 2 chaos determinism gate")
    parser.add_argument("--variant", choices=list(VARIANT_FILES), default="fixed")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--expect-pass", action="store_true")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--no-deploy", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_gate(
        variant=args.variant,
        runs=args.runs,
        expect_pass=args.expect_pass,
        host=args.host,
        deploy=not args.no_deploy,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Tier 2 gate ({result['variant']}, expect_pass={result['expect_pass']})")
        print(f"  runs:        {result['runs']}")
        print(f"  pass/fail/err: {result['pass_count']}/{result['fail_count']}/{result['error_count']}")
        print(f"  flipped:     {result['flipped']}")
        print(f"  PASSED:      {result['passed']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
