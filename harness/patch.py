"""Apply arbitrary git diffs to broken src and build content-hashed images."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from harness.lifecycle import ARCHETYPE_ROOT

WORKSPACES_ROOT = ARCHETYPE_ROOT / ".grade-workspaces"
PATCHES_ROOT = ARCHETYPE_ROOT / "patches"
BROKEN_SRC = ARCHETYPE_ROOT / "src"


def _copy_src_normalized(dest: Path) -> None:
    """Copy broken src with LF line endings so git apply patches match on Windows."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(BROKEN_SRC, dest)
    for path in dest.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            if b"\r\n" in raw:
                path.write_bytes(raw.replace(b"\r\n", b"\n"))


@dataclass(frozen=True)
class PatchWorkspace:
    """Ephemeral patched src tree + generated compose overlay."""

    root: Path
    src_root: Path
    patch_hash: str
    compose_overlay: Path
    gateway_image: str
    upstream_image: str


def _run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc


def content_hash_src(src_root: Path) -> str:
    """SHA256 over sorted file paths + contents under src/."""
    h = hashlib.sha256()
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root).as_posix()
        h.update(rel.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def generate_fixed_model_patch() -> str:
    """Build SWE-bench-style diff: broken src → patches/fixed (for bridge test corpus)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        _copy_src_normalized(root / "src")
        _run(["git", "init"], cwd=root)
        _run(["git", "config", "core.autocrlf", "false"], cwd=root)
        _run(["git", "add", "-A"], cwd=root)
        _run(["git", "commit", "-m", "broken"], cwd=root)

        shutil.copy2(
            PATCHES_ROOT / "fixed" / "gateway" / "main.go",
            root / "src" / "gateway" / "main.go",
        )
        shutil.copy2(
            PATCHES_ROOT / "fixed" / "upstream-mock" / "main.go",
            root / "src" / "upstream-mock" / "main.go",
        )

        proc = _run(["git", "diff"], cwd=root)
        return proc.stdout


def apply_model_patch(model_patch: str) -> PatchWorkspace:
    """
    Copy broken src, git-apply model_patch, emit compose overlay with content-hash tags.

    Patch paths must be relative to repo root (e.g. src/gateway/main.go).
    """
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="patch-", dir=WORKSPACES_ROOT))
    src_root = work / "src"
    _copy_src_normalized(src_root)

    _run(["git", "init"], cwd=work)
    _run(["git", "config", "core.autocrlf", "false"], cwd=work)
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "broken"], cwd=work)

    patch_file = work / "model.patch"
    # Normalize line endings for cross-platform git apply (CRLF corpus, lone CR, etc.).
    normalized = model_patch.replace("\r\n", "\n").replace("\r", "\n")
    patch_file.write_text(normalized, encoding="utf-8", newline="\n")
    _run(["git", "apply", "--verbose", str(patch_file)], cwd=work)

    patch_hash = content_hash_src(src_root)
    gateway_image = f"archetype-a-gateway:patch-{patch_hash}"
    upstream_image = f"archetype-a-upstream-mock:patch-{patch_hash}"

    # Docker compose wants forward slashes; use absolute paths.
    gw_ctx = (src_root / "gateway").resolve().as_posix()
    up_ctx = (src_root / "upstream-mock").resolve().as_posix()

    overlay = work / "docker-compose.patch.yml"
    overlay.write_text(
        f"""services:
  upstream-mock:
    image: {upstream_image}
    build:
      context: {up_ctx}

  gateway:
    image: {gateway_image}
    build:
      context: {gw_ctx}
""",
        encoding="utf-8",
    )

    return PatchWorkspace(
        root=work,
        src_root=src_root,
        patch_hash=patch_hash,
        compose_overlay=overlay,
        gateway_image=gateway_image,
        upstream_image=upstream_image,
    )


def remove_workspace(workspace: PatchWorkspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)
