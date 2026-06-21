#!/usr/bin/env python3
"""
Phase 2 determinism gate: run Tier 2 chaos pytest N times per variant.

True fix (--variant fixed --expect-pass): every run must PASS.
Band-aids (--expect-pass omitted): every run must FAIL (assertion), not ERROR.
"""

from __future__ import annotations

import argparse
import json
import os
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


def run_pytest_once(
    host: bool,
    compose_files: list[str],
    trace: bool = False,
    env: dict | None = None,
) -> tuple[int, str]:
    run_env = (env or os.environ).copy()

    if host:
        cmd = [sys.executable, "-m", "pytest", "tier2_chaos_test.py", "-q"]
        proc = subprocess.run(
            cmd, cwd=ARCHETYPE_ROOT, capture_output=True, text=True, env=run_env
        )
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
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, capture_output=True, text=True, env=run_env)
    state = "error" if "ERROR" in proc.stdout or "ERROR" in proc.stderr else (
        "pass" if proc.returncode == 0 else "fail"
    )
    return proc.returncode, state


def summarize_trace_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        return None

    rtts = [r["round_trip_sec"] for r in rows]
    drains = [r["drain"]["elapsed_sec"] for r in rows]
    late = sum(1 for r in rows if r["drain"].get("late_insert"))
    near_miss = sum(
        1
        for r in rows
        if r["drain"].get("first_stable_count") is not None
        and r["drain"]["first_stable_count"] != r["drain"]["final_count"]
    )
    multi_step = sum(1 for r in rows if len(r["drain"].get("trajectory", [])) > 2)
    statuses = [r["http_status"] for r in rows]

    return {
        "runs": len(rows),
        "http_statuses": statuses,
        "round_trip_sec": {
            "min": round(min(rtts), 3),
            "max": round(max(rtts), 3),
            "avg": round(sum(rtts) / len(rtts), 3),
        },
        "drain_sec": {
            "min": round(min(drains), 3),
            "max": round(max(drains), 3),
            "avg": round(sum(drains) / len(drains), 3),
        },
        "late_insert_runs": late,
        "near_miss_runs": near_miss,
        "multi_step_trajectory_runs": multi_step,
        "trajectories": [r["drain"]["trajectory"] for r in rows],
    }


def run_gate(
    variant: str,
    runs: int,
    expect_pass: bool,
    host: bool,
    deploy: bool,
    trace: bool,
) -> dict:
    files = VARIANT_FILES[variant]
    if deploy:
        deploy_variant(variant)

    gate_env = os.environ.copy()
    if trace:
        gate_env["TIER2_TRACE"] = "1"
        trace_file = ARCHETYPE_ROOT / "tier2_chaos_trace.jsonl"
        if trace_file.exists():
            trace_file.unlink()
        gate_env["TIER2_TRACE_FILE"] = str(trace_file)

    outcomes: list[dict] = []
    for _ in range(runs):
        code, state = run_pytest_once(
            host=host, compose_files=files, trace=trace, env=gate_env
        )
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

    result = {
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
    if trace:
        summary = summarize_trace_file(ARCHETYPE_ROOT / "tier2_chaos_trace.jsonl")
        if summary:
            result["chaos_trace"] = summary
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 2 chaos determinism gate")
    parser.add_argument("--variant", choices=list(VARIANT_FILES), default="fixed")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--expect-pass", action="store_true")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--no-deploy", action="store_true")
    parser.add_argument("--trace", action="store_true", help="Emit per-run chaos trace summary")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_gate(
        variant=args.variant,
        runs=args.runs,
        expect_pass=args.expect_pass,
        host=args.host,
        deploy=not args.no_deploy,
        trace=args.trace,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Tier 2 gate ({result['variant']}, expect_pass={result['expect_pass']})")
        print(f"  runs:        {result['runs']}")
        print(f"  pass/fail/err: {result['pass_count']}/{result['fail_count']}/{result['error_count']}")
        print(f"  flipped:     {result['flipped']}")
        print(f"  PASSED:      {result['passed']}")
        if "chaos_trace" in result:
            ct = result["chaos_trace"]
            print(f"  trace:       {ct['runs']} runs, rtt {ct['round_trip_sec']}, drain {ct['drain_sec']}")
            print(f"               late_insert={ct['late_insert_runs']} near_miss={ct['near_miss_runs']} multi_step={ct['multi_step_trajectory_runs']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
