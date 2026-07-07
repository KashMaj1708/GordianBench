"""Shared investigation/tooling container for agent run_bash.

Phase 5 surfaced that the agent's run_bash ran on the Windows host shell, so
`env`, `ls`, curl idioms and Python one-liners broke — which made the Archetype D
legibility gate's Test 3 (does the bug force an investigation chain?) impossible
to interpret. The fix the critique demanded: give run_bash a real Linux shell
with in-network reach to the stack BEFORE building the next archetype.

This module manages a long-lived tooling container (`gordianbench-debug`) joined
to the running compose network. `ToolExecutor` routes run_bash through
`DebugContainerSession.exec`, so the agent can curl service names, run psql, and
launch concurrent load exactly as a Linux operator would.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from harness.archetype_spec import ArchetypeSpec

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_IMAGE = "gordianbench-debug:latest"
_DEBUG_CONTEXT = _REPO_ROOT / "harness" / "debug"


def _docker(*args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {detail[-2000:]}")
    return proc


def ensure_image() -> None:
    """Build the debug image if it is not present locally."""
    exists = _docker("image", "inspect", DEBUG_IMAGE, check=False)
    if exists.returncode == 0:
        return
    _docker("build", "-t", DEBUG_IMAGE, str(_DEBUG_CONTEXT), timeout=600)


def discover_network(spec: ArchetypeSpec) -> str:
    """Find the compose default network for a deployed archetype stack.

    docker compose (invoked with cwd=spec.root) names the project after the
    archetype directory, so the network is `<project>_default`. We verify it
    exists and fall back to a substring scan for robustness.
    """
    project = spec.root.name
    candidate = f"{project}_default"
    found = _docker("network", "ls", "--format", "{{.Name}}", check=False)
    names = [n for n in (found.stdout or "").splitlines() if n.strip()]
    if candidate in names:
        return candidate
    matches = [n for n in names if project in n]
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(
        f"could not resolve compose network for {project!r}; candidates={names}"
    )


@dataclass
class DebugContainerSession:
    """Long-lived tooling container on the stack network; exec is run_bash."""

    network: str
    mount_host_path: Path
    env: dict[str, str] = field(default_factory=dict)
    mount_target: str = "/workspace"
    name: str = ""
    _started: bool = False

    def __enter__(self) -> "DebugContainerSession":
        ensure_image()
        self.name = self.name or f"gordian-debug-{uuid.uuid4().hex[:8]}"
        run_args = [
            "run",
            "-d",
            "--name",
            self.name,
            "--network",
            self.network,
            "-v",
            f"{self.mount_host_path}:{self.mount_target}",
            "-w",
            self.mount_target,
        ]
        for k, v in self.env.items():
            run_args.extend(["-e", f"{k}={v}"])
        run_args.extend([DEBUG_IMAGE, "sleep", "infinity"])
        _docker(*run_args, timeout=120)
        self._started = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._started:
            _docker("rm", "-f", self.name, check=False)
            self._started = False
        return False

    def exec(self, command: str, *, timeout: float) -> tuple[int, str]:
        """Run a shell command inside the tooling container; return (rc, output)."""
        try:
            proc = subprocess.run(
                ["docker", "exec", "-w", self.mount_target, self.name, "bash", "-lc", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
            return 124, f"(timed out after {timeout}s)\n{out}"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out


def debug_container_for_spec(spec: ArchetypeSpec, mount_host_path: Path) -> DebugContainerSession:
    """Build a tooling-container session wired to the archetype's in-network URLs."""
    gateway = spec.internal_gateway_url or spec.gateway_url
    database = spec.internal_database_url or spec.database_url
    env = {
        "API_URL": gateway,
        "GATEWAY_URL": gateway,
        spec.api_url_env: gateway,
        "DATABASE_URL": database,
        "DEBIAN_FRONTEND": "noninteractive",
    }
    return DebugContainerSession(
        network=discover_network(spec),
        mount_host_path=mount_host_path,
        env=env,
    )
