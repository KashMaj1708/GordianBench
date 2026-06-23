#!/usr/bin/env python3
"""Phase 4 exit gate — runs all remaining validation scripts."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _run(script: str, extra: list[str] | None = None) -> int:
    cmd = [PY, str(ROOT / "scripts" / script)] + (extra or [])
    print(f"\n=== {script} ===")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 exit gate")
    parser.add_argument("--skip-live", action="store_true", help="Skip Anthropic live agent (NOT for sign-off)")
    parser.add_argument("--skip-crash", action="store_true", help="Skip crash recovery (~2 min)")
    parser.add_argument("--skip-bridge", action="store_true", help="Skip bridge test (~3 min)")
    args = parser.parse_args()

    failures = 0

    if not args.skip_bridge:
        if _run("validate_patch_bridge.py", ["--skip-overlay"]) != 0:
            failures += 1

    if _run("run_mock_agent.py", ["--agent", "correct"]) != 0:
        failures += 1
    if _run("run_mock_agent.py", ["--agent", "bandaid"]) != 0:
        failures += 1

    if _run("validate_tool_faithfulness.py") != 0:
        failures += 1

    if not args.skip_crash:
        if _run("validate_crash_recovery.py") != 0:
            failures += 1

    if _run("validate_agent_trajectory.py") != 0:
        failures += 1

    if not args.skip_live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        if os.environ.get("ANTHROPIC_API_KEY"):
            if _run("run_live_agent.py", ["--max-turns", "15"]) != 0:
                failures += 1
        else:
            print("\n=== run_live_agent.py ===")
            print("FAIL: ANTHROPIC_API_KEY not set — required for Phase 4 sign-off")
            print("  Add to .env and run: scripts/run_live_agent.py")
            failures += 1

    print(f"\nPhase 4 gate: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
