"""Binary grading: Tier 1 (calm) + Tier 2 (chaos) must both pass.

SCAFFOLD ONLY — not validated. Phase 2 corpus frozen 2026-06-21.
Do not run grade() acceptance tests until PHASE3_CONSTRAINTS checklist permits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from harness.hygiene import assert_resource_hygiene
from harness.lifecycle import ARCHETYPE_ROOT, StackSession, stack_session, teardown_stack

# Variant names map to hand-supplied patch corpora for Phase 3 validation.
PATCH_VARIANTS = {
    "broken",
    "fixed",
    "bandaid-timeout",
    "bandaid-retry",
    "bandaid-rewrite",
}


def _env_for_session(session: StackSession) -> dict[str, str]:
    env = os.environ.copy()
    env["GATEWAY_URL"] = session.gateway_url
    env["DATABASE_URL"] = session.database_url
    env["TOXIPROXY_URL"] = session.toxiproxy_url
    env["PYTHONPATH"] = str(ARCHETYPE_ROOT)
    return env


def run_tier1(session: StackSession) -> bool:
    cmd = [sys.executable, "-m", "pytest", "tier1_regression_test.py", "-q"]
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, env=_env_for_session(session))
    return proc.returncode == 0


def run_tier2(session: StackSession) -> bool:
    cmd = [sys.executable, "-m", "pytest", "tier2_chaos_test.py", "-q"]
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, env=_env_for_session(session))
    return proc.returncode == 0


def grade(variant: str, *, teardown: bool = True) -> float:
    """
    Return 1.0 iff Tier 1 and Tier 2 both pass for the given patch variant.

    Phase 3: variant selects compose overlay (hand-supplied patch corpus).
    """
    if variant not in PATCH_VARIANTS:
        raise ValueError(f"unknown variant: {variant}")

    try:
        with stack_session(variant) as session:
            tier1_ok = run_tier1(session)
            if not tier1_ok:
                return 0.0
            tier2_ok = run_tier2(session)
            return 1.0 if tier2_ok else 0.0
    finally:
        if teardown:
            import sys

            sys.path.insert(0, str(ARCHETYPE_ROOT))
            try:
                from tests.toxiproxy_chaos import clear_chaos

                clear_chaos()
            except Exception:
                pass
        hygiene = assert_resource_hygiene(allow_running_stack=not teardown)
        if not hygiene.ok and teardown:
            raise RuntimeError(f"resource leak after grade: {hygiene.to_dict()}")
