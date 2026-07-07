"""Entry-side recovery from crashed grade runs (kill -9, no finally block)."""

from __future__ import annotations

import subprocess

from harness.archetype_spec import ARCHETYPE_A, ARCHETYPE_D_STUB, default_spec
from harness.hygiene import assert_resource_hygiene
from harness.lifecycle import teardown_stack
from harness.workspace import teardown_agent_workspaces

ARCHETYPE_ROOT = ARCHETYPE_A.root
VARIANT_COMPOSE = ARCHETYPE_A.variant_compose


def _teardown_patch_workspaces() -> None:
    """Down stacks started with dynamic patch overlays (not covered by VARIANT_COMPOSE)."""
    from harness.archetype_spec import _SPECS

    for spec in _SPECS.values():
        ws_root = spec.workspaces_root
        if not ws_root.exists():
            continue
        for overlay in ws_root.glob("patch-*/docker-compose.patch.yml"):
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(spec.compose_path(spec.base_compose)),
                    "-f",
                    str(overlay),
                    "down",
                    "--remove-orphans",
                ],
                cwd=spec.root,
                capture_output=True,
                text=True,
                check=False,
            )


def _compose_down_base() -> None:
    from harness.archetype_spec import _SPECS

    for spec in _SPECS.values():
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(spec.compose_path(spec.base_compose)),
                "down",
                "--remove-orphans",
            ],
            cwd=spec.root,
            capture_output=True,
            text=True,
            check=False,
        )


def _clear_toxics() -> None:
    import sys

    sys.path.insert(0, str(ARCHETYPE_ROOT))
    try:
        from tests.toxiproxy_chaos import clear_chaos

        clear_chaos()
    except Exception:
        pass


def _docker(args: list[str]) -> None:
    subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _force_cleanup(report) -> None:
    for name in report.orphan_containers:
        _docker(["rm", "-f", name])
    for net in report.orphan_networks:
        _docker(["network", "rm", net])
    for vol in report.orphan_volumes:
        _docker(["volume", "rm", vol])
    _clear_toxics()


def ensure_clean_state(*, toxiproxy_url: str = "http://localhost:8474") -> None:
    """
    Run before every grade() / patch deploy.

    Handles prior runs killed mid-Tier-2: leftover containers, networks, active toxics.
    Graceful stack_session teardown does not run on SIGKILL.
    """
    _clear_toxics()

    _teardown_patch_workspaces()
    teardown_agent_workspaces()

    from harness.archetype_spec import _SPECS

    for spec in _SPECS.values():
        for variant in spec.variant_compose:
            teardown_stack(variant, spec=spec)

    _compose_down_base()

    report = assert_resource_hygiene(toxiproxy_url=toxiproxy_url)
    if not report.ok:
        _force_cleanup(report)
        report = assert_resource_hygiene(toxiproxy_url=toxiproxy_url)
        if not report.ok:
            raise RuntimeError(f"cannot reach clean state: {report.to_dict()}")
