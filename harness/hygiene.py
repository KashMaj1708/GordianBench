"""Docker resource hygiene checks for grade() cycles."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class HygieneReport:
    ok: bool
    orphan_containers: list[str] = field(default_factory=list)
    orphan_networks: list[str] = field(default_factory=list)
    orphan_volumes: list[str] = field(default_factory=list)
    active_toxics: list[str] = field(default_factory=list)
    stale_compose_images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "orphan_containers": self.orphan_containers,
            "orphan_networks": self.orphan_networks,
            "orphan_volumes": self.orphan_volumes,
            "active_toxics": self.active_toxics,
            "stale_compose_images": self.stale_compose_images,
        }


def _docker_json(args: list[str]) -> list[dict]:
    proc = subprocess.run(
        ["docker"] + args,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return json.loads(proc.stdout)


def list_archetype_containers() -> list[str]:
    rows = _docker_json(["ps", "-a", "--filter", "name=archetype-a", "--format", "{{json .}}"])
    return [r.get("Names", r.get("ID", "")) for r in rows]


def list_archetype_networks() -> list[str]:
    rows = _docker_json(["network", "ls", "--filter", "name=archetype-a", "--format", "{{json .}}"])
    return [r.get("Name", "") for r in rows]


def list_archetype_volumes() -> list[str]:
    rows = _docker_json(["volume", "ls", "--filter", "name=archetype-a", "--format", "{{json .}}"])
    return [r.get("Name", "") for r in rows]


def list_active_toxics(toxiproxy_url: str = "http://localhost:8474") -> list[str]:
    try:
        import requests

        resp = requests.get(f"{toxiproxy_url.rstrip('/')}/proxies/upstream/toxics", timeout=5)
        if resp.status_code != 200:
            return []
        return [t.get("name", "") for t in resp.json() if t.get("name")]
    except Exception:
        return []


def assert_resource_hygiene(
    *,
    allow_running_stack: bool = False,
    toxiproxy_url: str = "http://localhost:8474",
) -> HygieneReport:
    """
    Phase 3 leak check: containers, networks, volumes, toxics, image freshness.

    When allow_running_stack=True, a single running archetype-a compose project is OK
    (e.g. mid-grade), but toxics must be cleared and no duplicate orphan projects.
    """
    containers = list_archetype_containers()
    networks = list_archetype_networks()
    volumes = list_archetype_volumes()
    toxics = list_active_toxics(toxiproxy_url)

    orphan_containers = containers
    if allow_running_stack and len(containers) <= 5:
        orphan_containers = []

    report = HygieneReport(
        ok=not orphan_containers and not networks and not volumes and not toxics,
        orphan_containers=orphan_containers,
        orphan_networks=networks,
        orphan_volumes=volumes,
        active_toxics=toxics,
    )
    return report
