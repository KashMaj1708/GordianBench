"""Per-archetype configuration injected into harness lifecycle and grading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PatchService:
    """Docker compose service rebuilt from patched src."""

    compose_name: str
    src_dir: str
    image_basename: str


@dataclass(frozen=True)
class ArchetypeSpec:
    root: Path
    name: str
    tier1_pytest_target: str
    tier2_pytest_target: str
    observation_helpers: str
    variant_compose: dict[str, list[str]]
    variant_image_tags: dict[str, dict[str, str]]
    base_compose: str
    patch_services: tuple[PatchService, ...]
    gateway_url: str
    database_url: str
    toxiproxy_url: str | None = None
    api_url_env: str = "GATEWAY_URL"
    # In-network URLs for the tooling container (reach services by compose name).
    # Host-port URLs (gateway_url/database_url) are unreachable from inside the
    # compose network; these are used when run_bash runs in the debug container.
    internal_gateway_url: str = ""
    internal_database_url: str = ""

    @property
    def workspaces_root(self) -> Path:
        return self.root / ".grade-workspaces"

    @property
    def service_src_dirs(self) -> tuple[str, ...]:
        """Repo-relative source dirs for patch services (e.g. ('src/ledger-api',)).

        Used to compute the workspace diff over real service source only, so
        agent scratch files (repro.py, model_patch.diff, *.log) are excluded by
        construction. Archetype-agnostic: derived from patch_services.
        """
        return tuple(f"src/{svc.src_dir}" for svc in self.patch_services)

    @property
    def broken_src(self) -> Path:
        return self.root / "src"

    def compose_path(self, filename: str) -> Path:
        return self.root / filename

    def import_observation_helpers(self):
        import importlib

        return importlib.import_module(self.observation_helpers)


ARCHETYPE_A = ArchetypeSpec(
    root=_REPO_ROOT / "archetype-a",
    name="archetype-a",
    tier1_pytest_target="tier1_regression_test.py",
    tier2_pytest_target="tier2_chaos_test.py",
    observation_helpers="tests.helpers",
    base_compose="docker-compose.yml",
    variant_compose={
        "broken": ["docker-compose.yml"],
        "fixed": ["docker-compose.yml", "docker-compose.fixed.yml"],
        "bandaid-timeout": ["docker-compose.yml", "docker-compose.bandaid-timeout.yml"],
        "bandaid-retry": ["docker-compose.yml", "docker-compose.bandaid-retry.yml"],
        "bandaid-rewrite": ["docker-compose.yml", "docker-compose.bandaid-rewrite.yml"],
    },
    variant_image_tags={
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
    },
    patch_services=(
        PatchService("gateway", "gateway", "archetype-a-gateway"),
        PatchService("upstream-mock", "upstream-mock", "archetype-a-upstream-mock"),
    ),
    gateway_url="http://localhost:8080",
    database_url="postgresql://bench:bench@localhost:5433/payments",
    toxiproxy_url="http://localhost:8474",
    internal_gateway_url="http://gateway:8080",
    internal_database_url="postgresql://bench:bench@postgres:5432/payments",
)

ARCHETYPE_D_STUB = ArchetypeSpec(
    root=_REPO_ROOT / "archetype-d-stub",
    name="archetype-d-stub",
    tier1_pytest_target="tier1_regression_test.py",
    tier2_pytest_target="tier1_regression_test.py",
    observation_helpers="tests.helpers",
    base_compose="docker-compose.yml",
    variant_compose={
        "broken": ["docker-compose.yml"],
    },
    variant_image_tags={
        "broken": {
            "ledger-api": "archetype-d-stub-ledger-api:broken",
        },
    },
    patch_services=(
        PatchService("ledger-api", "ledger-api", "archetype-d-stub-ledger-api"),
    ),
    gateway_url="http://localhost:8082",
    database_url="postgresql://bench:bench@localhost:5434/ledger",
    toxiproxy_url=None,
    api_url_env="API_URL",
    internal_gateway_url="http://ledger-api:8080",
    internal_database_url="postgresql://bench:bench@postgres:5432/ledger",
)

ARCHETYPE_D_STALE = ArchetypeSpec(
    root=_REPO_ROOT / "archetype-d-stale-read",
    name="archetype-d-stale-read",
    tier1_pytest_target="tier1_regression_test.py",
    tier2_pytest_target="tier1_regression_test.py",  # Tier 2 chaos corpus: post-gate
    observation_helpers="tests.helpers",
    base_compose="docker-compose.yml",
    variant_compose={
        "broken": ["docker-compose.yml"],
        "fixed": ["docker-compose.yml", "docker-compose.fixed.yml"],
    },
    variant_image_tags={
        "broken": {"ledger-api": "archetype-d-stale-read-ledger-api:broken"},
        "fixed": {"ledger-api": "archetype-d-stale-read-ledger-api:fixed"},
    },
    patch_services=(
        PatchService("ledger-api", "ledger-api", "archetype-d-stale-read-ledger-api"),
    ),
    gateway_url="http://localhost:8083",
    database_url="postgresql://bench:bench@localhost:5435/ledger",
    toxiproxy_url="http://localhost:8474",
    api_url_env="API_URL",
    internal_gateway_url="http://ledger-api:8080",
    internal_database_url="postgresql://bench:bench@postgres-primary:5432/ledger",
)

_SPECS: dict[str, ArchetypeSpec] = {
    ARCHETYPE_A.name: ARCHETYPE_A,
    ARCHETYPE_D_STUB.name: ARCHETYPE_D_STUB,
    ARCHETYPE_D_STALE.name: ARCHETYPE_D_STALE,
}


def get_spec(name: str) -> ArchetypeSpec:
    if name not in _SPECS:
        raise KeyError(f"unknown archetype: {name!r} (known: {sorted(_SPECS)})")
    return _SPECS[name]


def default_spec() -> ArchetypeSpec:
    return ARCHETYPE_A
