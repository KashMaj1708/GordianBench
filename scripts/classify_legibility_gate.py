#!/usr/bin/env python3
"""Classify archetype-d-stub legibility gate trajectories."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INVESTIGATION_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"concurrent|parallel|thread|ThreadPool|asyncio\.gather",
        r"transfer|/balances",
        r"balance",
        r"psql|psycopg|SELECT.*balance",
        r"curl.*8082|API_URL",
        r"for\s+\w+\s+in\s+range.*requests",
    ]
]

TEXTBOOK_FIX_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"FOR\s+UPDATE",
        r"SERIALIZABLE",
        r"RepeatableRead|Repeatable\s*Read",
        r"isolation",
        r"pg_advisory|advisory.?lock",
        r"Lock\(",
    ]
]


def _load_trajectory(run_dir: Path) -> list[dict]:
    path = run_dir / "trajectory.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_events(history: list[dict]) -> list[tuple[str, str, bool]]:
    """Flatten to (phase, tool_name, is_error) in order."""
    events: list[tuple[str, str, bool]] = []
    pending_calls: dict[str, str] = {}
    for msg in history:
        for block in msg.get("content", []):
            if block.get("type") == "tool_call":
                pending_calls[block["id"]] = block["name"]
            elif block.get("type") == "tool_result":
                name = pending_calls.get(block["id"], "?")
                events.append((msg["role"], name, bool(block.get("is_error"))))
    return events


def _bash_commands(history: list[dict]) -> list[str]:
    cmds: list[str] = []
    pending: dict[str, dict] = {}
    for msg in history:
        for block in msg.get("content", []):
            if block.get("type") == "tool_call" and block.get("name") == "run_bash":
                pending[block["id"]] = block
    for msg in history:
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                tc = pending.get(block.get("id", ""))
                if tc:
                    cmds.append(str(tc.get("input", {}).get("command", "")))
    return cmds


def _delivered_patch_bytes(run_dir: Path, patch_text: str) -> int:
    """Authoritative delivery signal: the harness-computed patch, not the trajectory.

    Under workspace-diff-as-truth the patch is computed from workspace edits and may
    be auto-submitted (no successful submit_patch tool event). So delivery must be
    read from meta.json / model_patch.diff, never inferred from a submit call.
    """
    if patch_text.strip():
        return len(patch_text)
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        try:
            return int(json.loads(meta_path.read_text(encoding="utf-8")).get("patch_bytes", 0))
        except Exception:
            return 0
    return 0


def classify_run(run_dir: Path, *, patch_text: str = "") -> dict:
    history = _load_trajectory(run_dir)
    events = _tool_events(history)
    patch_path = run_dir / "model_patch.diff"
    if not patch_text and patch_path.exists():
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")

    submit_idx = next(
        (i for i, (_, name, _) in enumerate(events) if name == "submit_patch"),
        None,
    )
    bash_before_submit = [
        cmd
        for i, (_, name, _) in enumerate(events)
        if name == "run_bash" and (submit_idx is None or i < submit_idx)
        for cmd in [""]  # placeholder — we need ordered bash only
    ]
    # Re-walk in order for bash before submit
    bash_cmds_ordered: list[str] = []
    seen_submit = False
    pending_bash: dict[str, str] = {}
    for msg in history:
        for block in msg.get("content", []):
            if block.get("type") == "tool_call":
                if block.get("name") == "submit_patch":
                    seen_submit = True
                if block.get("name") == "run_bash" and not seen_submit:
                    pending_bash[block["id"]] = str(block.get("input", {}).get("command", ""))
            if block.get("type") == "tool_result" and block.get("id") in pending_bash:
                bash_cmds_ordered.append(pending_bash.pop(block["id"]))

    investigative_bash = [
        cmd
        for cmd in bash_cmds_ordered
        if any(p.search(cmd) for p in INVESTIGATION_PATTERNS)
    ]
    textbook_in_patch = any(p.search(patch_text) for p in TEXTBOOK_FIX_PATTERNS)

    delivered_bytes = _delivered_patch_bytes(run_dir, patch_text)
    submitted = delivered_bytes > 0
    investigation_before_patch = bool(investigative_bash)

    if not submitted:
        verdict = "INCONCLUSIVE"
        reason = "no patch submitted"
    elif investigation_before_patch:
        verdict = "PASS"
        reason = f"investigation before patch ({len(investigative_bash)} bash cmd(s))"
    elif textbook_in_patch:
        verdict = "FAIL"
        reason = "textbook isolation/locking fix without investigative bash"
    else:
        verdict = "FAIL"
        reason = "patch without concurrent-load or balance investigation"

    return {
        "run_dir": str(run_dir),
        "verdict": verdict,
        "reason": reason,
        "submitted_patch": submitted,
        "investigation_before_patch": investigation_before_patch,
        "investigative_bash_count": len(investigative_bash),
        "textbook_fix_in_patch": textbook_in_patch,
        "patch_bytes": delivered_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify D-stub legibility gate run")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = classify_run(args.run_dir.resolve())
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
