#!/usr/bin/env python3
"""Patch-delivery regression lock (scripted, no API, no Docker).

Exercises every patch-delivery failure mode with ScriptedProvider against BOTH
archetype specs, asserting the workspace-diff-as-truth pipeline delivers a
gradeable patch in each case. This is the lock that keeps patch delivery from
silently breaking when a future archetype or loop change lands — the failure
mode that used to only surface in expensive live runs is now a unit test.

Run:
    .venv\\Scripts\\python.exe scripts\\test_patch_delivery.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.loop import run_agent_loop
from agent.patch_pipeline import PipelineLogger
from agent.patch_util import sanitize_model_patch
from agent.provider import scripted
from harness.archetype_spec import ARCHETYPE_A, ARCHETYPE_D_STALE, ArchetypeSpec
from harness.patch import check_patch_applies_fast
from harness.workspace import agent_workspace_session, compute_workspace_diff

MARKER = "// bench-delivery-test"
BAD_MARKER = "// BAD_MARKER does not compile"


def _fake_build_check(patch: str) -> str | None:
    """Stand-in for a real compiler: fails iff the bad marker is present."""
    if "BAD_MARKER" in patch:
        return "build error: BAD_MARKER token is not valid Go"
    return None


def _first_source(spec: ArchetypeSpec) -> tuple[str, str]:
    """Return (write_file-relative path, broken content) for the first service .go file."""
    src_dir = spec.patch_services[0].src_dir
    svc_root = spec.broken_src / src_dir
    go_files = sorted(svc_root.glob("*.go"))
    if not go_files:
        raise RuntimeError(f"no .go source under {svc_root}")
    f = go_files[0]
    rel = f"{src_dir}/{f.name}"
    return rel, f.read_text(encoding="utf-8")


def _make_valid_diff(spec: ArchetypeSpec, rel_path: str, new_content: str) -> str:
    """Produce a real git diff (broken → new_content) for the patch-file scenario."""
    with agent_workspace_session(spec=spec) as ws:
        target = ws.src_root / rel_path
        target.write_text(new_content, encoding="utf-8", newline="\n")
        return compute_workspace_diff(ws.root, spec.service_src_dirs)


def _executor(ws_root: Path, spec: ArchetypeSpec) -> ToolExecutor:
    return ToolExecutor(
        ExecutorConfig(
            workspace_root=ws_root / "src",
            repo_root=ws_root,
            spec=spec,
        )
    )


def _run(spec: ArchetypeSpec, provider, *, build_check=None, max_turns=8):
    with agent_workspace_session(spec=spec) as ws:
        executor = _executor(ws.root, spec)
        logger = PipelineLogger()
        result = run_agent_loop(
            provider,
            executor,
            system="test",
            initial_user="fix it",
            max_turns=max_turns,
            pipeline=logger,
            build_check=build_check,
        )
        return result, logger


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passes += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: {detail}")
            print(f"  FAIL  {label} :: {detail}")


def run_for_spec(spec: ArchetypeSpec, chk: Checker) -> None:
    print(f"\n=== {spec.name} ===")
    rel, broken = _first_source(spec)
    good = broken + "\n" + MARKER + "\n"
    bad = broken + "\n" + BAD_MARKER + "\n"
    valid_diff = _make_valid_diff(spec, rel, good)

    # 1. edit via write_file then submit empty -> computed diff
    result, log = _run(spec, scripted.edit_then_submit(rel, good))
    chk.check(
        "1 edit_then_submit delivers",
        bool(result.submitted_patch) and result.patch_source == "workspace_git_diff",
        f"source={result.patch_source} bytes={len(result.submitted_patch or '')}",
    )
    chk.check(
        "1 patch applies",
        bool(result.submitted_patch)
        and check_patch_applies_fast(result.submitted_patch, spec=spec) is None,
        "git apply --check failed",
    )

    # 2. write a .diff file and submit -> resolved from workspace file
    result, log = _run(spec, scripted.write_diff_file_then_submit("model_patch.diff", valid_diff))
    chk.check(
        "2 write_diff_file resolves from file",
        bool(result.submitted_patch)
        and str(result.patch_source).startswith("workspace_patch_file"),
        f"source={result.patch_source}",
    )

    # 3. truncated inline diff is ignored in favor of workspace
    truncated = "diff --git a/src/x b/src/x\n--- a/src/x\n+++ b/src/x\n@@ -1,9 +1,9 @@\n+incomplete"
    result, log = _run(spec, scripted.truncated_inline_then_submit(rel, good, truncated))
    inline_ignored = any(
        r.get("stage") == "content_check" and r.get("source") == "inline_IGNORED" and r.get("trusted") is False
        for r in log.records
    )
    chk.check(
        "3 truncated inline ignored, workspace used",
        bool(result.submitted_patch)
        and result.patch_source == "workspace_git_diff"
        and inline_ignored,
        f"source={result.patch_source} inline_ignored={inline_ignored}",
    )

    # 4. edits but never submits -> auto-submit on loop end
    result, log = _run(spec, scripted.edit_no_submit(rel, good))
    auto = any(r.get("stage") == "auto_submit" and r.get("ok") is True for r in log.records)
    chk.check(
        "4 auto-submit on forgotten submit",
        bool(result.submitted_patch) and str(result.patch_source).startswith("auto:") and auto,
        f"source={result.patch_source} auto_event={auto}",
    )

    # 6. modify a file AND add a new file in the service dir (GPT-4.1 corrupt-patch repro)
    src_dir = spec.patch_services[0].src_dir
    new_rel = f"{src_dir}/bench_helper.go"
    new_content = "package main\n\nfunc benchHelper() int { return 0 }\n"
    result, log = _run(spec, scripted.edit_plus_new_file_then_submit(rel, good, new_rel, new_content))
    new_file_in_patch = bool(result.submitted_patch) and "bench_helper.go" in (result.submitted_patch or "")
    chk.check(
        "6 modify + new-file delivers and applies",
        bool(result.submitted_patch)
        and result.patch_source == "workspace_git_diff"
        and new_file_in_patch
        and check_patch_applies_fast(result.submitted_patch, spec=spec) is None,
        f"source={result.patch_source} new_file_in_patch={new_file_in_patch}",
    )

    # 5. build failure -> feedback -> retry -> deliver good
    result, log = _run(spec, scripted.edit_bad_then_fix(rel, bad, good), build_check=_fake_build_check)
    builds = [r for r in log.records if r.get("stage") == "build"]
    had_fail = any(r.get("ok") is False for r in builds)
    had_pass = any(r.get("ok") is True for r in builds)
    delivered_good = bool(result.submitted_patch) and "BAD_MARKER" not in (result.submitted_patch or "")
    chk.check(
        "5 build feedback then retry delivers good",
        had_fail and had_pass and delivered_good,
        f"build_fail={had_fail} build_pass={had_pass} delivered_good={delivered_good}",
    )


def run_sanitize_regression(chk: Checker) -> None:
    """Lock the trailing-blank-context-line bug (14222Z re-grade attrition).

    A valid git diff whose final hunk ends on a blank context line (" ") must
    survive sanitize_model_patch byte-faithfully. The old raw.strip() ate that
    trailing space line, truncating the hunk -> "corrupt patch at line N" only at
    grade time. This guards against any future edge-trimming change reintroducing
    that silent patch corruption.
    """
    print("\n=== sanitize byte-faithfulness ===")
    # Final hunk intentionally ends on a blank context line.
    diff_with_trailing_blank = (
        "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-old\n+new\n ctx\n \n"
    )
    out = sanitize_model_patch(diff_with_trailing_blank)
    chk.check(
        "S trailing blank-context line preserved",
        out.endswith(" \n") and out.count("\n ") >= 1,
        f"out={out!r}",
    )
    # Fence wrapping must still be stripped without harming the body.
    fenced = "```diff\n" + diff_with_trailing_blank + "```\n"
    out_f = sanitize_model_patch(fenced)
    chk.check(
        "S fence stripped, trailing blank-context still preserved",
        "```" not in out_f and out_f.endswith(" \n"),
        f"out={out_f!r}",
    )


def main() -> int:
    chk = Checker()
    run_sanitize_regression(chk)
    for spec in (ARCHETYPE_A, ARCHETYPE_D_STALE):
        run_for_spec(spec, chk)

    print(f"\n{chk.passes} passed, {len(chk.failures)} failed")
    if chk.failures:
        for f in chk.failures:
            print(f"  - {f}")
        return 1
    print("PATCH DELIVERY LOCK: GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
