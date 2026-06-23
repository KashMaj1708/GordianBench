"""Binary grading: Tier 1 (calm) + Tier 2 (chaos) must both pass."""

from __future__ import annotations

import os
import subprocess
import sys

from harness.cleanup import ensure_clean_state
from harness.hygiene import assert_resource_hygiene, list_active_toxics
from harness.lifecycle import (
    ARCHETYPE_ROOT,
    PatchStackSession,
    StackSession,
    patch_session,
    stack_session,
    teardown_stack,
)

PATCH_VARIANTS = {
    "broken",
    "fixed",
    "bandaid-timeout",
    "bandaid-retry",
    "bandaid-rewrite",
}


def _env_for_session(session: StackSession | PatchStackSession) -> dict[str, str]:
    env = os.environ.copy()
    env["GATEWAY_URL"] = session.gateway_url
    env["DATABASE_URL"] = session.database_url
    env["TOXIPROXY_URL"] = session.toxiproxy_url
    env["PYTHONPATH"] = str(ARCHETYPE_ROOT)
    return env


def _clear_toxics() -> None:
    sys.path.insert(0, str(ARCHETYPE_ROOT))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass


def run_tier1(session: StackSession | PatchStackSession) -> bool:
    _clear_toxics()
    if list_active_toxics(session.toxiproxy_url):
        return False
    cmd = [sys.executable, "-m", "pytest", "tier1_regression_test.py", "-q"]
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, env=_env_for_session(session))
    return proc.returncode == 0


def run_tier2(session: StackSession | PatchStackSession) -> bool:
    cmd = [sys.executable, "-m", "pytest", "tier2_chaos_test.py", "-q"]
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, env=_env_for_session(session))
    _clear_toxics()
    return proc.returncode == 0


def _finalize_hygiene(*, teardown: bool) -> None:
    if teardown:
        _clear_toxics()
    hygiene = assert_resource_hygiene(allow_running_stack=not teardown)
    if not hygiene.ok and teardown:
        raise RuntimeError(f"resource leak after grade: {hygiene.to_dict()}")


def grade(variant: str, *, teardown: bool = True) -> float:
    """
    Return 1.0 iff Tier 1 and Tier 2 both pass for a frozen corpus variant.

    Phase 3 path: selects pre-built compose overlay from VARIANT_COMPOSE.
    """
    if variant not in PATCH_VARIANTS:
        raise ValueError(f"unknown variant: {variant}")

    ensure_clean_state()
    try:
        with stack_session(variant) as session:
            if not run_tier1(session):
                return 0.0
            if not run_tier2(session):
                return 0.0
            return 1.0
    finally:
        _finalize_hygiene(teardown=teardown)


def grade_patch(model_patch: str, *, teardown: bool = True) -> float:
    """
    Return 1.0 iff Tier 1 and Tier 2 both pass for an arbitrary git diff.

    Phase 4 path: git apply → content-hash rebuild → same oracle as grade(variant).
    This is the path the agent will exercise; grade(variant) validates the oracle only.
    """
    if not model_patch or not model_patch.strip():
        raise ValueError("model_patch is empty")

    from agent.patch_util import sanitize_model_patch

    model_patch = sanitize_model_patch(model_patch)
    ensure_clean_state()
    try:
        with patch_session(model_patch) as session:
            if not run_tier1(session):
                return 0.0
            if not run_tier2(session):
                return 0.0
            return 1.0
    finally:
        _finalize_hygiene(teardown=teardown)
