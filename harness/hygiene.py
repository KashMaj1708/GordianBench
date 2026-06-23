"""Docker resource hygiene checks for grade() cycles."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.lifecycle import PatchStackSession, StackSession

# Expected tagged images per variant (content verified via docker inspect, not tag label alone).
VARIANT_IMAGE_TAGS: dict[str, dict[str, str]] = {
    "broken": {
        "gateway": "archetype-a-gateway:broken",
        "upstream-mock": "archetype-a-upstream-mock:broken",
    },
    "fixed": {
        "gateway": "archetype-a-gateway:fixed",
        "upstream-mock": "archetype-a-upstream-mock:fixed",
    },
    "bandaid-timeout": {
        "gateway": "archetype-a-gateway:bandaid-timeout",
        "upstream-mock": "archetype-a-upstream-mock:broken",
    },
    "bandaid-retry": {
        "gateway": "archetype-a-gateway:bandaid-retry",
        "upstream-mock": "archetype-a-upstream-mock:broken",
    },
    "bandaid-rewrite": {
        "gateway": "archetype-a-gateway:bandaid-rewrite",
        "upstream-mock": "archetype-a-upstream-mock:broken",
    },
}


@dataclass
class HygieneReport:
    ok: bool
    orphan_containers: list[str] = field(default_factory=list)
    orphan_networks: list[str] = field(default_factory=list)
    orphan_volumes: list[str] = field(default_factory=list)
    active_toxics: list[str] = field(default_factory=list)
    stale_images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "orphan_containers": self.orphan_containers,
            "orphan_networks": self.orphan_networks,
            "orphan_volumes": self.orphan_volumes,
            "active_toxics": self.active_toxics,
            "stale_images": self.stale_images,
        }


@dataclass
class ImageFingerprint:
    """Running container image ID for a service (content hash, not tag label)."""

    service: str
    image_id: str
    image_tag: str = ""


def _normalize_id(image_id: str) -> str:
    return image_id.replace("sha256:", "").strip().lower()


def _docker_run(args: list[str], *, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _image_id_for_tag(tag: str) -> str:
    proc = _docker_run(["image", "inspect", tag, "--format", "{{.Id}}"])
    if proc.returncode != 0:
        return ""
    return _normalize_id(proc.stdout.strip())


def verify_patch_images(session: "PatchStackSession") -> list[str]:
    """Confirm running patch-deploy containers match content-hash tags."""
    from harness.lifecycle import PatchStackSession

    if not isinstance(session, PatchStackSession):
        raise TypeError("expected PatchStackSession")
    expected = {
        "gateway": session.workspace.gateway_image,
        "upstream-mock": session.workspace.upstream_image,
    }
    running = _get_running_fingerprints(session)
    errors: list[str] = []
    for svc, tag in expected.items():
        fp = running.get(svc)
        if not fp or not fp.image_id:
            errors.append(f"{svc}: no running container")
            continue
        tagged_id = _image_id_for_tag(tag)
        if not tagged_id:
            errors.append(f"{svc}: tagged image {tag} not found")
            continue
        if fp.image_id != tagged_id:
            errors.append(
                f"{svc}: running image {fp.image_id[:12]} != tagged {tag} ({tagged_id[:12]})"
            )
    return errors


def _get_running_fingerprints(session) -> dict[str, ImageFingerprint]:
    """Return image IDs for running compose services."""
    from harness.lifecycle import ARCHETYPE_ROOT

    out: dict[str, ImageFingerprint] = {}
    for service in ("gateway", "upstream-mock"):
        proc = subprocess.run(
            session.compose_cmd("ps", service, "--format", "json"),
            cwd=ARCHETYPE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        row = json.loads(proc.stdout.strip().splitlines()[0])
        container_id = row.get("ID") or row.get("Id") or ""
        image_id = ""
        if container_id:
            insp = _docker_run(["inspect", container_id, "--format", "{{.Image}}"])
            if insp.returncode == 0:
                image_id = _normalize_id(insp.stdout.strip())
        out[service] = ImageFingerprint(
            service=service,
            image_id=image_id,
            image_tag=row.get("Image", ""),
        )
    return out


def get_running_image_fingerprints(session: StackSession) -> dict[str, ImageFingerprint]:
    """Return image IDs for running compose services using variant compose files."""
    return _get_running_fingerprints(session)


def verify_variant_images(session: StackSession) -> list[str]:
    """
    Confirm running container image IDs match the tagged images for this variant.

    Tag labels alone are insufficient; this compares docker image inspect IDs.
    """
    expected = VARIANT_IMAGE_TAGS.get(session.variant, {})
    running = get_running_image_fingerprints(session)
    errors: list[str] = []
    for svc, tag in expected.items():
        fp = running.get(svc)
        if not fp or not fp.image_id:
            errors.append(f"{svc}: no running container")
            continue
        tagged_id = _image_id_for_tag(tag)
        if not tagged_id:
            errors.append(f"{svc}: tagged image {tag} not found")
            continue
        if fp.image_id != tagged_id:
            errors.append(
                f"{svc}: running image {fp.image_id[:12]} != tagged {tag} ({tagged_id[:12]})"
            )
    return errors


def assert_variant_switch_fresh(
    prior: dict[str, ImageFingerprint] | None,
    current: dict[str, ImageFingerprint],
    *,
    prior_variant: str | None,
    current_variant: str,
) -> list[str]:
    """
    When variant changes, gateway image ID must change (catches stale broken→fixed bleed).

    Skipped when redeploying the same variant (fixed→fixed in leak sequence).
    """
    if not prior or prior_variant == current_variant:
        return []
    stale: list[str] = []
    for svc in ("gateway",):
        b = prior.get(svc)
        c = current.get(svc)
        if b and c and b.image_id and c.image_id and b.image_id == c.image_id:
            stale.append(
                f"{svc}: image unchanged after {prior_variant!r} → {current_variant!r} switch"
            )
    return stale


def list_archetype_containers() -> list[str]:
    proc = _docker_run(["ps", "-a", "--filter", "name=archetype-a", "--format", "{{.Names}}"])
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def list_archetype_networks() -> list[str]:
    proc = _docker_run(["network", "ls", "--filter", "name=archetype-a", "--format", "{{.Name}}"])
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def list_archetype_volumes() -> list[str]:
    proc = _docker_run(["volume", "ls", "--filter", "name=archetype-a", "--format", "{{.Name}}"])
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


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
    """Phase 3 leak check: containers, networks, volumes, toxics."""
    containers = list_archetype_containers()
    networks = list_archetype_networks()
    volumes = list_archetype_volumes()
    toxics = list_active_toxics(toxiproxy_url)

    orphan_containers = containers
    if allow_running_stack and len(containers) <= 5:
        orphan_containers = []

    return HygieneReport(
        ok=not orphan_containers and not networks and not volumes and not toxics,
        orphan_containers=orphan_containers,
        orphan_networks=networks,
        orphan_volumes=volumes,
        active_toxics=toxics,
    )
