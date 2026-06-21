"""Compose stack lifecycle for Archetype A grading."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1] / "archetype-a"

VARIANT_COMPOSE: dict[str, list[str]] = {
    "broken": ["docker-compose.yml"],
    "fixed": ["docker-compose.yml", "docker-compose.fixed.yml"],
    "bandaid-timeout": ["docker-compose.yml", "docker-compose.bandaid-timeout.yml"],
    "bandaid-retry": ["docker-compose.yml", "docker-compose.bandaid-retry.yml"],
    "bandaid-rewrite": ["docker-compose.yml", "docker-compose.bandaid-rewrite.yml"],
}


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


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ARCHETYPE_ROOT, check=check, capture_output=True, text=True)


def deploy_variant(variant: str) -> None:
    session = StackSession(variant=variant)
    _run(session.compose_cmd("up", "-d", "--build"))


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


class stack_session:
    """Context manager: deploy variant, wait healthy, teardown + clear toxics on exit."""

    def __init__(self, variant: str):
        self.variant = variant
        self.session = StackSession(variant=variant)

    def __enter__(self) -> StackSession:
        deploy_variant(self.variant)
        wait_for_healthy()
        return self.session

    def __exit__(self, exc_type, exc, tb) -> None:
        teardown_stack(self.variant)
        return False
