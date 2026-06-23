"""Ephemeral agent workspaces — host src corpus stays read-only."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from harness.patch import BROKEN_SRC, WORKSPACES_ROOT, _copy_src_normalized


@dataclass(frozen=True)
class AgentWorkspace:
    """Per-session copy of broken src for read_file / write_file / run_bash."""

    root: Path

    @property
    def src_root(self) -> Path:
        return self.root / "src"


def create_agent_workspace() -> AgentWorkspace:
    """Snapshot broken src into .grade-workspaces/agent-* (LF-normalized)."""
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="agent-", dir=WORKSPACES_ROOT))
    _copy_src_normalized(root / "src")
    return AgentWorkspace(root=root)


def remove_agent_workspace(workspace: AgentWorkspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)


def teardown_agent_workspaces() -> None:
    """Remove all ephemeral agent workspace dirs (crash recovery)."""
    if not WORKSPACES_ROOT.exists():
        return
    for path in WORKSPACES_ROOT.glob("agent-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


@contextmanager
def agent_workspace_session() -> Iterator[AgentWorkspace]:
    workspace = create_agent_workspace()
    try:
        yield workspace
    finally:
        remove_agent_workspace(workspace)


def assert_corpus_unchanged() -> None:
    """Raise if host broken src was modified (safety check for CI/sign-off)."""
    import subprocess

    repo_root = BROKEN_SRC.parents[1]
    rel = BROKEN_SRC.relative_to(repo_root)
    proc = subprocess.run(
        ["git", "diff", "--quiet", "--", str(rel)],
        cwd=repo_root,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"host corpus mutated: {BROKEN_SRC} — restore with "
            f"'git checkout HEAD -- {rel.as_posix()}/'"
        )
