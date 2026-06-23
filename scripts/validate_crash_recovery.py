#!/usr/bin/env python3
"""
Phase 4 crash-safety: simulate kill mid-grade, verify ensure_clean_state() recovers.

Simulates a process killed during Tier 2 (stack up + chaos toxics active, no teardown).
Next ensure_clean_state() must reach hygiene OK and grade(broken) must run cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "archetype-a"))

from harness.cleanup import ensure_clean_state
from harness.grade import grade
from harness.hygiene import assert_resource_hygiene
from harness.lifecycle import deploy_variant, wait_for_healthy


def _simulate_killed_mid_tier2() -> None:
    """Leave stack running with chaos toxics — as if grade() was SIGKILL'd in Tier 2."""
    from tests.toxiproxy_chaos import enable_chaos

    deploy_variant("broken")
    wait_for_healthy()
    enable_chaos()
    # Intentionally no teardown (no finally block)


def main() -> int:
    print("Simulating crashed grade (stack + toxics, no teardown)...")
    _simulate_killed_mid_tier2()

    pre = assert_resource_hygiene()
    if pre.ok:
        print("FAIL: expected dirty state after simulated crash")
        return 1
    print(f"  dirty state confirmed: toxics={pre.active_toxics} containers={len(pre.orphan_containers)}")

    print("\nensure_clean_state() ...")
    ensure_clean_state()

    post = assert_resource_hygiene()
    if not post.ok:
        print(f"FAIL: hygiene not recovered: {post.to_dict()}")
        return 1
    print("  hygiene: OK")

    print("\ngrade('broken') smoke after recovery ...")
    score = grade("broken")
    if score != 0.0:
        print(f"FAIL: expected 0.0 got {score}")
        return 1
    print(f"  score={score} OK")

    print("\nCrash recovery: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
