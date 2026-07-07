"""Apply arbitrary git diffs to broken src and build content-hashed images."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from harness.archetype_spec import ARCHETYPE_A, ArchetypeSpec, default_spec

# Backward compatibility.
ARCHETYPE_ROOT = ARCHETYPE_A.root
WORKSPACES_ROOT = ARCHETYPE_A.workspaces_root
PATCHES_ROOT = ARCHETYPE_A.root / "patches"
BROKEN_SRC = ARCHETYPE_A.broken_src


def _workspaces_root(spec: ArchetypeSpec) -> Path:
    return spec.workspaces_root


def _broken_src(spec: ArchetypeSpec) -> Path:
    return spec.broken_src


def _copy_src_normalized(dest: Path, *, spec: ArchetypeSpec) -> None:
    """Copy broken src with LF line endings so git apply patches match on Windows."""
    src = _broken_src(spec)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
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
    spec: ArchetypeSpec
    service_images: dict[str, str]


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


def generate_fixed_model_patch(*, spec: ArchetypeSpec | None = None) -> str:
    """Build SWE-bench-style diff: broken src → patches/fixed (Archetype A bridge test)."""
    s = spec or ARCHETYPE_A
    patches_root = s.root / "patches"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        _copy_src_normalized(root / "src", spec=s)
        _run(["git", "init"], cwd=root)
        _run(["git", "config", "core.autocrlf", "false"], cwd=root)
        _run(["git", "add", "-A"], cwd=root)
        _run(["git", "commit", "-m", "broken"], cwd=root)

        shutil.copy2(
            patches_root / "fixed" / "gateway" / "main.go",
            root / "src" / "gateway" / "main.go",
        )
        shutil.copy2(
            patches_root / "fixed" / "upstream-mock" / "main.go",
            root / "src" / "upstream-mock" / "main.go",
        )

        proc = _run(["git", "diff"], cwd=root)
        return proc.stdout


def _compose_overlay_lines(spec: ArchetypeSpec, src_root: Path, patch_hash: str) -> str:
    lines = ["services:"]
    for svc in spec.patch_services:
        image = f"{svc.image_basename}:patch-{patch_hash}"
        ctx = (src_root / svc.src_dir).resolve().as_posix()
        lines.append(f"  {svc.compose_name}:")
        lines.append(f"    image: {image}")
        lines.append("    build:")
        lines.append(f"      context: {ctx}")
    return "\n".join(lines) + "\n"


def apply_model_patch(model_patch: str, *, spec: ArchetypeSpec | None = None) -> PatchWorkspace:
    """
    Copy broken src, git-apply model_patch, emit compose overlay with content-hash tags.

    Patch paths must be relative to repo root (e.g. src/gateway/main.go).
    """
    s = spec or default_spec()
    ws_root = _workspaces_root(s)
    ws_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="patch-", dir=ws_root))
    src_root = work / "src"
    _copy_src_normalized(src_root, spec=s)

    _run(["git", "init"], cwd=work)
    _run(["git", "config", "core.autocrlf", "false"], cwd=work)
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "broken"], cwd=work)

    patch_file = work / "model.patch"
    normalized = model_patch.replace("\r\n", "\n").replace("\r", "\n")
    patch_file.write_text(normalized, encoding="utf-8", newline="\n")
    _run(["git", "apply", "--verbose", str(patch_file)], cwd=work)

    patch_hash = content_hash_src(src_root)
    service_images = {
        svc.compose_name: f"{svc.image_basename}:patch-{patch_hash}"
        for svc in s.patch_services
    }

    overlay = work / "docker-compose.patch.yml"
    overlay.write_text(_compose_overlay_lines(s, src_root, patch_hash), encoding="utf-8")

    return PatchWorkspace(
        root=work,
        src_root=src_root,
        patch_hash=patch_hash,
        compose_overlay=overlay,
        spec=s,
        service_images=service_images,
    )


def remove_workspace(workspace: PatchWorkspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)


def check_model_patch_applies(model_patch: str, *, spec: ArchetypeSpec | None = None) -> str | None:
    """Return a short error if patch does not git-apply to broken src, else None.

    Heavyweight (full copy + git init + apply). Prefer check_patch_applies_fast
    for repeated checks in the loop / tests.
    """
    try:
        workspace = apply_model_patch(model_patch, spec=spec)
        remove_workspace(workspace)
        return None
    except Exception as exc:
        detail = str(exc).strip().split("\n")[-1]
        return detail or str(exc)


# Cached, read-only baseline repos for fast `git apply --check` (never mutated).
_BASELINE_CACHE: dict[str, Path] = {}


def _baseline_repo(spec: ArchetypeSpec) -> Path:
    import atexit

    key = spec.name
    cached = _BASELINE_CACHE.get(key)
    if cached and cached.exists():
        return cached
    base = Path(tempfile.mkdtemp(prefix=f"gb-baseline-{key}-"))
    _copy_src_normalized(base / "src", spec=spec)
    _run(["git", "init"], cwd=base)
    _run(["git", "config", "core.autocrlf", "false"], cwd=base)
    _run(["git", "config", "core.safecrlf", "false"], cwd=base)
    _run(["git", "add", "-A"], cwd=base)
    _run(["git", "commit", "-m", "broken"], cwd=base)
    _BASELINE_CACHE[key] = base
    atexit.register(lambda: shutil.rmtree(base, ignore_errors=True))
    return base


def check_patch_applies_fast(model_patch: str, *, spec: ArchetypeSpec | None = None) -> str | None:
    """`git apply --check` against a cached broken-src baseline (no mutation).

    Much cheaper than check_model_patch_applies for repeated validation: it reuses
    one baseline repo per spec and only runs `git apply --check`.
    """
    s = spec or default_spec()
    base = _baseline_repo(s)
    normalized = model_patch.replace("\r\n", "\n").replace("\r", "\n")
    patch_file = base / "candidate.patch"
    patch_file.write_text(normalized, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        ["git", "apply", "--check", "--verbose", str(patch_file)],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "git apply --check failed").strip()
    return detail.split("\n")[-1] if detail else "git apply --check failed"
