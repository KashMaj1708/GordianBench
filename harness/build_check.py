"""Compile/build gate for delivered patches.

A correct-looking patch that does not compile is not a fix. The legibility/
resolution gates historically did NOT build delivered patches, so a non-compiling
patch was delivered and the classifier still scored it by mechanism-token
presence (see phase_5_report_7.md: an Opus value-catch-up patch was labeled
TRUE_FIX but failed to build on an unused `time` import). This factory produces a
``BuildCheck`` (``Callable[[str], str | None]``) that the agent loop calls on
``submit_patch``; on failure the compiler stderr is fed back to the model with a
bounded number of retries (see ``agent/loop.py``).

It builds the *same* service Dockerfile the grader/deploy path uses, so "builds
in the gate" is equivalent to "builds under oracle grading" — the check cannot
disagree with deployment. The workspace edits on disk are the source of truth
(consistent with the workspace-diff-as-patch contract), so the patch text passed
by the loop is intentionally ignored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from harness.archetype_spec import ArchetypeSpec

BuildCheck = Callable[[str], "str | None"]


def make_workspace_build_check(spec: ArchetypeSpec, repo_root: Path) -> BuildCheck:
    """Build every patch service from the workspace and report the first failure.

    Returns a ``BuildCheck`` that runs ``docker build`` on each
    ``spec.patch_services`` source dir under ``<repo_root>/src``. Returns ``None``
    when all services build, otherwise the build stderr (the loop truncates it for
    model feedback).
    """

    def _build_check(_patch_text: str) -> str | None:
        errors: list[str] = []
        for svc in spec.patch_services:
            ctx = repo_root / "src" / svc.src_dir
            if not (ctx / "Dockerfile").exists():
                continue
            # --progress=plain so the Go compiler error lands in stderr verbatim;
            # a fixed tag (overwritten each call) avoids dangling buildcheck images.
            proc = subprocess.run(
                [
                    "docker",
                    "build",
                    "--progress=plain",
                    "-t",
                    f"{svc.image_basename}:buildcheck",
                    str(ctx),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "docker build failed").strip()
                errors.append(f"[{svc.compose_name}] {detail}")
        return "\n\n".join(errors) if errors else None

    return _build_check
