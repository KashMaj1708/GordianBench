"""Binary grading: Tier 1 (calm) + Tier 2 (chaos) must both pass."""

from __future__ import annotations

import os
import subprocess
import sys

from harness.archetype_spec import ArchetypeSpec, default_spec
from harness.cleanup import ensure_clean_state
from harness.hygiene import assert_resource_hygiene, list_active_toxics
from harness.lifecycle import (
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
    spec = session.spec
    env = os.environ.copy()
    env[spec.api_url_env] = session.gateway_url
    env["GATEWAY_URL"] = session.gateway_url
    env["API_URL"] = session.gateway_url
    env["DATABASE_URL"] = session.database_url
    if session.toxiproxy_url:
        env["TOXIPROXY_URL"] = session.toxiproxy_url
    env["PYTHONPATH"] = str(spec.root)
    return env


def _clear_toxics(spec: ArchetypeSpec) -> None:
    if not spec.toxiproxy_url:
        return
    sys.path.insert(0, str(spec.root))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass


def run_tier1(session: StackSession | PatchStackSession) -> bool:
    spec = session.spec
    _clear_toxics(spec)
    if session.toxiproxy_url and list_active_toxics(session.toxiproxy_url):
        return False
    cmd = [sys.executable, "-m", "pytest", spec.tier1_pytest_target, "-q"]
    proc = subprocess.run(cmd, cwd=spec.root, env=_env_for_session(session))
    return proc.returncode == 0


def run_tier2(session: StackSession | PatchStackSession) -> bool:
    spec = session.spec
    cmd = [sys.executable, "-m", "pytest", spec.tier2_pytest_target, "-q"]
    proc = subprocess.run(cmd, cwd=spec.root, env=_env_for_session(session))
    _clear_toxics(spec)
    return proc.returncode == 0


def _finalize_hygiene(*, teardown: bool, spec: ArchetypeSpec) -> None:
    if teardown:
        _clear_toxics(spec)
    hygiene = assert_resource_hygiene(allow_running_stack=not teardown)
    if not hygiene.ok and teardown:
        raise RuntimeError(f"resource leak after grade: {hygiene.to_dict()}")


def grade(variant: str, *, spec: ArchetypeSpec | None = None, teardown: bool = True) -> float:
    """
    Return 1.0 iff Tier 1 and Tier 2 both pass for a frozen corpus variant.
    """
    s = spec or default_spec()
    if variant not in s.variant_compose:
        raise ValueError(f"unknown variant: {variant}")

    ensure_clean_state()
    try:
        with stack_session(variant, spec=s) as session:
            if not run_tier1(session):
                return 0.0
            if not run_tier2(session):
                return 0.0
            return 1.0
    finally:
        _finalize_hygiene(teardown=teardown, spec=s)


def grade_patch(
    model_patch: str,
    *,
    spec: ArchetypeSpec | None = None,
    teardown: bool = True,
) -> float:
    """
    Return 1.0 iff Tier 1 and Tier 2 both pass for an arbitrary git diff.
    """
    s = spec or default_spec()
    if not model_patch or not model_patch.strip():
        raise ValueError("model_patch is empty")

    from agent.patch_util import repair_model_patch

    model_patch = repair_model_patch(model_patch)
    ensure_clean_state()
    try:
        with patch_session(model_patch, spec=s) as session:
            if not run_tier1(session):
                return 0.0
            if not run_tier2(session):
                return 0.0
            return 1.0
    finally:
        _finalize_hygiene(teardown=teardown, spec=s)


def grade_patch_tier(
    model_patch: str,
    tier: int,
    *,
    spec: ArchetypeSpec | None = None,
    teardown: bool = True,
) -> float:
    """Return 1.0 iff the given tier passes for an arbitrary git diff (informational)."""
    s = spec or default_spec()
    if tier not in (1, 2):
        raise ValueError("tier must be 1 or 2")
    if not model_patch or not model_patch.strip():
        raise ValueError("model_patch is empty")

    from agent.patch_util import repair_model_patch

    model_patch = repair_model_patch(model_patch)
    ensure_clean_state()
    try:
        with patch_session(model_patch, spec=s) as session:
            ok = run_tier1(session) if tier == 1 else run_tier2(session)
            return 1.0 if ok else 0.0
    finally:
        _finalize_hygiene(teardown=teardown, spec=s)
