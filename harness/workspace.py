"""Ephemeral agent workspaces — host src corpus stays read-only."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from harness.archetype_spec import ArchetypeSpec, default_spec
from harness.patch import BROKEN_SRC, _copy_src_normalized


@dataclass(frozen=True)
class AgentWorkspace:
    """Per-session copy of broken src for read_file / write_file / run_bash."""

    root: Path

    @property
    def src_root(self) -> Path:
        return self.root / "src"


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_git_snapshot(repo_root: Path) -> None:
    """Baseline commit so the harness can compute `git diff` for patch submission.

    autocrlf/safecrlf are forced off so the LF-normalized corpus is not silently
    re-line-ended on Windows, which would corrupt the computed diff and break
    `git apply` downstream (a real patch-delivery death mode).
    """
    _git(repo_root, "init", "-q")
    # Persisted so later `git diff` (compute_workspace_diff) is CRLF-safe even if
    # the host has autocrlf=true globally.
    _git(repo_root, "config", "core.autocrlf", "false")
    _git(repo_root, "add", "-A")
    _git(
        repo_root,
        "-c", "core.safecrlf=false",
        "-c", "user.email=bench@test",
        "-c", "user.name=bench",
        "commit", "-q", "-m", "broken",
    )


def compute_workspace_diff(repo_root: Path, service_dirs: tuple[str, ...] | list[str]) -> str:
    """Diff the workspace against its baseline over service source dirs only.

    This is the single source of truth for a submitted patch: the model's edits
    already exist as files on disk, so we read them instead of trusting a
    transmission-prone inline diff. `git add -N` makes new files in service dirs
    appear in the diff; scratch files outside service dirs are excluded.

    Paths are repo-relative (src/<service>/...), which is exactly what
    apply_model_patch expects.
    """
    dirs = list(service_dirs) or ["src"]
    existing = [d for d in dirs if (repo_root / d).exists()]
    if not existing:
        existing = ["src"]
    # Stage then diff --cached: this yields canonical new-file sections
    # (`new file mode` + `--- /dev/null`) that git apply accepts, avoiding the
    # `git add -N` intent-to-add quirk that can emit unappliable diffs.
    _git(repo_root, "add", "-A", "--", *existing, check=False)
    proc = _git(repo_root, "diff", "--cached", "--no-color", "--", *existing, check=False)
    text = (proc.stdout or "").replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def find_workspace_patch_files(repo_root: Path) -> list[Path]:
    """Find *.diff / *.patch files anywhere in the workspace (model wrote a patch file)."""
    out: list[Path] = []
    for pattern in ("*.diff", "*.patch"):
        out.extend(p for p in repo_root.rglob(pattern) if p.is_file())
    return sorted(out, key=lambda p: p.stat().st_size, reverse=True)


def create_agent_workspace(*, spec: ArchetypeSpec | None = None) -> AgentWorkspace:
    """Snapshot broken src into .grade-workspaces/agent-* (LF-normalized)."""
    s = spec or default_spec()
    ws_root = s.workspaces_root
    ws_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="agent-", dir=ws_root))
    _copy_src_normalized(root / "src", spec=s)
    _init_git_snapshot(root)
    return AgentWorkspace(root=root)


def remove_agent_workspace(workspace: AgentWorkspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)


def teardown_agent_workspaces(*, spec: ArchetypeSpec | None = None) -> None:
    """Remove all ephemeral agent workspace dirs (crash recovery)."""
    s = spec or default_spec()
    ws_root = s.workspaces_root
    if not ws_root.exists():
        return
    for path in ws_root.glob("agent-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


@contextmanager
def agent_workspace_session(*, spec: ArchetypeSpec | None = None) -> Iterator[AgentWorkspace]:
    workspace = create_agent_workspace(spec=spec)
    try:
        yield workspace
    finally:
        remove_agent_workspace(workspace)


def assert_corpus_unchanged(*, spec: ArchetypeSpec | None = None) -> None:
    """Raise if host broken src was modified (safety check for CI/sign-off)."""
    s = spec or default_spec()
    broken = s.broken_src
    repo_root = broken.parents[1]
    rel = broken.relative_to(repo_root)
    proc = subprocess.run(
        ["git", "diff", "--quiet", "--", str(rel)],
        cwd=repo_root,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"host corpus mutated: {broken} — restore with "
            f"'git checkout HEAD -- {rel.as_posix()}/'"
        )
