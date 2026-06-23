"""Compose stack lifecycle for Archetype A grading."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.patch import PatchWorkspace

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1] / "archetype-a"

VARIANT_COMPOSE: dict[str, list[str]] = {
    "broken": ["docker-compose.yml"],
    "fixed": ["docker-compose.yml", "docker-compose.fixed.yml"],
    "bandaid-timeout": ["docker-compose.yml", "docker-compose.bandaid-timeout.yml"],
    "bandaid-retry": ["docker-compose.yml", "docker-compose.bandaid-retry.yml"],
    "bandaid-rewrite": ["docker-compose.yml", "docker-compose.bandaid-rewrite.yml"],
}

# Tracks last deploy for variant-switch image freshness checks.
_last_variant: str | None = None
_last_fingerprints: dict | None = None


@dataclass
class StackSession:
    variant: str
    gateway_url: str = "http://localhost:8080"
    database_url: str = "postgresql://bench:bench@localhost:5433/payments"
    toxiproxy_url: str = "http://localhost:8474"

    def compose_files(self) -> list[str]:
        return VARIANT_COMPOSE[self.variant]

    def compose_cmd(self, *args: str) -> list[str]:
        cmd = ["docker", "compose"]
        for f in self.compose_files():
            cmd.extend(["-f", str(ARCHETYPE_ROOT / f)])
        cmd.extend(args)
        return cmd


@dataclass
class PatchStackSession:
    """Stack session for dynamically patched src (content-hash image tags)."""

    workspace: PatchWorkspace
    gateway_url: str = "http://localhost:8080"
    database_url: str = "postgresql://bench:bench@localhost:5433/payments"
    toxiproxy_url: str = "http://localhost:8474"

    def compose_cmd(self, *args: str) -> list[str]:
        cmd = [
            "docker",
            "compose",
            "-f",
            str(ARCHETYPE_ROOT / "docker-compose.yml"),
            "-f",
            str(self.workspace.compose_overlay),
        ]
        cmd.extend(args)
        return cmd


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=ARCHETYPE_ROOT, check=False, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = "\n".join(
            s for s in [(proc.stderr or "").strip(), (proc.stdout or "").strip()] if s
        )
        msg = detail[-4000:] if detail else f"exit {proc.returncode}"
        raise RuntimeError(f"docker compose failed: {msg}")
    return proc


def deploy_patch(workspace: PatchWorkspace) -> PatchStackSession:
    """Deploy stack built from dynamically patched src."""
    from harness.hygiene import get_running_image_fingerprints, verify_patch_images

    session = PatchStackSession(workspace=workspace)
    _run(session.compose_cmd("up", "-d", "--build"))

    errors = verify_patch_images(session)
    if errors:
        raise RuntimeError(f"patch image mismatch: {errors}")

    return session


def teardown_patch(workspace: PatchWorkspace) -> None:
    """Stop patch stack and clear toxics."""
    import sys

    sys.path.insert(0, str(ARCHETYPE_ROOT))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass

    session = PatchStackSession(workspace=workspace)
    _run(session.compose_cmd("down", "--remove-orphans"), check=False)


def deploy_variant(variant: str) -> None:
    """Deploy variant; verify image IDs match tags and variant switches refresh gateway."""
    global _last_variant, _last_fingerprints

    from harness.hygiene import (
        assert_variant_switch_fresh,
        get_running_image_fingerprints,
        verify_variant_images,
    )

    session = StackSession(variant=variant)
    _run(session.compose_cmd("up", "-d", "--build"))

    current = get_running_image_fingerprints(session)
    switch_stale = assert_variant_switch_fresh(
        _last_fingerprints,
        current,
        prior_variant=_last_variant,
        current_variant=variant,
    )
    if switch_stale:
        raise RuntimeError(f"stale image after variant switch: {switch_stale}")

    tag_errors = verify_variant_images(session)
    if tag_errors:
        raise RuntimeError(f"image/tag mismatch: {tag_errors}")

    _last_variant = variant
    _last_fingerprints = current


def reset_deploy_tracking() -> None:
    global _last_variant, _last_fingerprints
    _last_variant = None
    _last_fingerprints = None


def teardown_stack(variant: str | None = None) -> None:
    """Stop stack and clear chaos toxics."""
    import sys

    sys.path.insert(0, str(ARCHETYPE_ROOT))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass

    if variant is not None:
        session = StackSession(variant=variant)
        _run(session.compose_cmd("down", "--remove-orphans"), check=False)


def wait_for_healthy(timeout: float = 120.0) -> None:
    import sys

    sys.path.insert(0, str(ARCHETYPE_ROOT))
    from tests.helpers import wait_for_gateway

    wait_for_gateway(timeout=timeout)
    time.sleep(1)


class patch_session:
    """Context manager: apply patch, deploy, wait healthy, teardown workspace on exit."""

    def __init__(self, model_patch: str):
        from harness.patch import apply_model_patch, remove_workspace

        self._remove_workspace = remove_workspace
        self.workspace = apply_model_patch(model_patch)
        self.session = PatchStackSession(workspace=self.workspace)
        self._deployed = False

    def __enter__(self) -> PatchStackSession:
        try:
            deploy_patch(self.workspace)
            self._deployed = True
            wait_for_healthy()
            return self.session
        except Exception:
            teardown_patch(self.workspace)
            self._remove_workspace(self.workspace)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._deployed:
            teardown_patch(self.workspace)
        self._remove_workspace(self.workspace)
        return False


class stack_session:
    """Context manager: deploy variant, wait healthy, teardown + clear toxics on exit."""

    def __init__(self, variant: str):
        self.variant = variant
        self.session = StackSession(variant=variant)
        self._deployed = False

    def __enter__(self) -> StackSession:
        try:
            deploy_variant(self.variant)
            self._deployed = True
            wait_for_healthy()
            return self.session
        except Exception:
            teardown_stack(self.variant)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._deployed:
            teardown_stack(self.variant)
        return False
