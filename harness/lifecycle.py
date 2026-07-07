"""Compose stack lifecycle — archetype injected via ArchetypeSpec."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness.archetype_spec import ARCHETYPE_A, ArchetypeSpec, default_spec

if TYPE_CHECKING:
    from harness.patch import PatchWorkspace

# Backward compatibility for archetype-a-only call sites.
ARCHETYPE_ROOT = ARCHETYPE_A.root
VARIANT_COMPOSE = ARCHETYPE_A.variant_compose

_last_variant: str | None = None
_last_fingerprints: dict | None = None


@dataclass
class StackSession:
    variant: str
    spec: ArchetypeSpec
    gateway_url: str
    database_url: str
    toxiproxy_url: str | None

    def __init__(self, variant: str, spec: ArchetypeSpec | None = None):
        s = spec or default_spec()
        self.variant = variant
        self.spec = s
        self.gateway_url = s.gateway_url
        self.database_url = s.database_url
        self.toxiproxy_url = s.toxiproxy_url

    def compose_files(self) -> list[str]:
        return self.spec.variant_compose[self.variant]

    def compose_cmd(self, *args: str) -> list[str]:
        cmd = ["docker", "compose"]
        for f in self.compose_files():
            cmd.extend(["-f", str(self.spec.compose_path(f))])
        cmd.extend(args)
        return cmd


@dataclass
class PatchStackSession:
    """Stack session for dynamically patched src (content-hash image tags)."""

    workspace: PatchWorkspace
    spec: ArchetypeSpec
    gateway_url: str
    database_url: str
    toxiproxy_url: str | None

    def __init__(self, workspace: PatchWorkspace, spec: ArchetypeSpec | None = None):
        s = spec or workspace.spec
        self.workspace = workspace
        self.spec = s
        self.gateway_url = s.gateway_url
        self.database_url = s.database_url
        self.toxiproxy_url = s.toxiproxy_url

    def compose_cmd(self, *args: str) -> list[str]:
        cmd = [
            "docker",
            "compose",
            "-f",
            str(self.spec.compose_path(self.spec.base_compose)),
            "-f",
            str(self.workspace.compose_overlay),
        ]
        cmd.extend(args)
        return cmd


def _run(cmd: list[str], *, cwd, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = "\n".join(
            s for s in [(proc.stderr or "").strip(), (proc.stdout or "").strip()] if s
        )
        msg = detail[-4000:] if detail else f"exit {proc.returncode}"
        raise RuntimeError(f"docker compose failed: {msg}")
    return proc


def _clear_toxics(spec: ArchetypeSpec) -> None:
    if not spec.toxiproxy_url:
        return
    sys.path.insert(0, str(spec.root))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass


def deploy_patch(workspace: PatchWorkspace, *, spec: ArchetypeSpec | None = None) -> PatchStackSession:
    """Deploy stack built from dynamically patched src."""
    from harness.hygiene import verify_patch_images

    s = spec or workspace.spec
    session = PatchStackSession(workspace=workspace, spec=s)
    _run(session.compose_cmd("up", "-d", "--build"), cwd=s.root)

    errors = verify_patch_images(session)
    if errors:
        raise RuntimeError(f"patch image mismatch: {errors}")

    return session


def teardown_patch(workspace: PatchWorkspace, *, spec: ArchetypeSpec | None = None) -> None:
    """Stop patch stack and clear toxics."""
    s = spec or workspace.spec
    _clear_toxics(s)
    session = PatchStackSession(workspace=workspace, spec=s)
    _run(session.compose_cmd("down", "--remove-orphans"), cwd=s.root, check=False)


def deploy_variant(variant: str, *, spec: ArchetypeSpec | None = None) -> None:
    """Deploy variant; verify image IDs match tags and variant switches refresh gateway."""
    global _last_variant, _last_fingerprints

    from harness.hygiene import (
        assert_variant_switch_fresh,
        get_running_image_fingerprints,
        verify_variant_images,
    )

    s = spec or default_spec()
    session = StackSession(variant=variant, spec=s)
    _run(session.compose_cmd("up", "-d", "--build"), cwd=s.root)

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


def teardown_stack(variant: str | None = None, *, spec: ArchetypeSpec | None = None) -> None:
    """Stop stack and clear chaos toxics."""
    s = spec or default_spec()
    _clear_toxics(s)

    if variant is not None:
        session = StackSession(variant=variant, spec=s)
        _run(session.compose_cmd("down", "--remove-orphans"), cwd=s.root, check=False)


def wait_for_healthy(*, spec: ArchetypeSpec | None = None, timeout: float = 120.0) -> None:
    s = spec or default_spec()
    sys.path.insert(0, str(s.root))
    helpers = s.import_observation_helpers()
    wait_fn = getattr(helpers, "wait_for_api", None) or helpers.wait_for_gateway
    wait_fn(timeout=timeout)
    time.sleep(1)


class patch_session:
    """Context manager: apply patch, deploy, wait healthy, teardown workspace on exit."""

    def __init__(self, model_patch: str, *, spec: ArchetypeSpec | None = None):
        from harness.patch import apply_model_patch, remove_workspace

        self.spec = spec or default_spec()
        self._remove_workspace = remove_workspace
        self.workspace = apply_model_patch(model_patch, spec=self.spec)
        self.session = PatchStackSession(workspace=self.workspace, spec=self.spec)
        self._deployed = False

    def __enter__(self) -> PatchStackSession:
        try:
            deploy_patch(self.workspace, spec=self.spec)
            self._deployed = True
            wait_for_healthy(spec=self.spec)
            return self.session
        except Exception:
            teardown_patch(self.workspace, spec=self.spec)
            self._remove_workspace(self.workspace)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._deployed:
            teardown_patch(self.workspace, spec=self.spec)
        self._remove_workspace(self.workspace)
        return False


class stack_session:
    """Context manager: deploy variant, wait healthy, teardown + clear toxics on exit."""

    def __init__(self, variant: str, *, spec: ArchetypeSpec | None = None):
        self.spec = spec or default_spec()
        self.variant = variant
        self.session = StackSession(variant=variant, spec=self.spec)
        self._deployed = False

    def __enter__(self) -> StackSession:
        try:
            deploy_variant(self.variant, spec=self.spec)
            self._deployed = True
            wait_for_healthy(spec=self.spec)
            return self.session
        except Exception:
            teardown_stack(self.variant, spec=self.spec)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._deployed:
            teardown_stack(self.variant, spec=self.spec)
        return False
