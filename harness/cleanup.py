"""Entry-side recovery from crashed grade runs (kill -9, no finally block)."""

from __future__ import annotations

import subprocess

from harness.hygiene import assert_resource_hygiene
from harness.lifecycle import ARCHETYPE_ROOT, VARIANT_COMPOSE, teardown_stack
from harness.workspace import teardown_agent_workspaces


def _teardown_patch_workspaces() -> None:
    """Down stacks started with dynamic patch overlays (not covered by VARIANT_COMPOSE)."""
    ws_root = ARCHETYPE_ROOT / ".grade-workspaces"
    if not ws_root.exists():
        return
    for overlay in ws_root.glob("patch-*/docker-compose.patch.yml"):
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(ARCHETYPE_ROOT / "docker-compose.yml"),
                "-f",
                str(overlay),
                "down",
                "--remove-orphans",
            ],
            cwd=ARCHETYPE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


def _compose_down_base() -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(ARCHETYPE_ROOT / "docker-compose.yml"), "down", "--remove-orphans"],
        cwd=ARCHETYPE_ROOT,
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

    for variant in VARIANT_COMPOSE:
        teardown_stack(variant)

    _compose_down_base()

    report = assert_resource_hygiene(toxiproxy_url=toxiproxy_url)
    if not report.ok:
        _force_cleanup(report)
        report = assert_resource_hygiene(toxiproxy_url=toxiproxy_url)
        if not report.ok:
            raise RuntimeError(f"cannot reach clean state: {report.to_dict()}")
