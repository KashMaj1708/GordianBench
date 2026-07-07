#!/usr/bin/env python3
"""
Automated Test-2 detector: does diagnosis precede investigation?

The legibility-gate signal that matters (ARCHETYPE_D_DESIGN.md Test 2) is NOT
"was a patch submitted" (which `classify_legibility_gate.py` keys on, and which
goes INCONCLUSIVE whenever the host shell blocks patch delivery). It is:

    Did the model NAME the root cause from source + domain knowledge BEFORE it
    obtained any successful runtime observation of the violated invariant?

If yes, the bug is source-legible: the model pattern-matched a named bug class
without needing to run the system. This is the structural signal the Phase 6
k-shot sweep needs to detect automatically across many trajectories and
archetypes, because trajectory text cannot be read by hand at scale.

We operate on ordinal trajectory position (the event index of the first
root-cause mention vs. the event index of the first SUCCESSFUL runtime probe),
which is the durable structural proxy for "timestamp of diagnosis vs. timestamp
of first runtime evidence" — and is robust even when no patch is ever submitted.

Verdicts:
  SOURCE_LEGIBLE      diagnosis named before (or without) any successful probe  -> Test 2 FAIL
  INVESTIGATION_DRIVEN successful runtime probe precedes the named diagnosis     -> Test 2 PASS
  NO_DIAGNOSIS         no root cause ever named in assistant text                -> Test 2 N/A
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Named root-cause vocabulary across the distributed-systems bug classes the
# benchmark cares about. Extend this per archetype; the point is that a model
# "naming" any of these is pattern-matching a documented bug class, not
# discovering an emergent runtime fact. Keyed by class purely for reporting.
ROOT_CAUSE_PATTERNS: dict[str, list[str]] = {
    "write-skew": [r"write[\s-]?skew", r"lost\s+update", r"read[\s-]?check[\s-]?update"],
    "isolation": [
        r"read\s+committed",
        r"repeatable\s+read",
        r"serializable",
        r"isolation\s+level",
        r"\bSSI\b",
    ],
    "race": [r"race\s+condition", r"\bTOCTOU\b", r"time[\s-]?of[\s-]?check"],
    "stale-read": [r"stale\s+read", r"replication\s+lag", r"read[\s-]?your[\s-]?writes"],
    "ordering": [r"message\s+reorder", r"out[\s-]?of[\s-]?order", r"reordering"],
}
_ROOT_CAUSE_COMPILED = {
    cls: [re.compile(p, re.I) for p in pats] for cls, pats in ROOT_CAUSE_PATTERNS.items()
}

# A "runtime probe" is a shell command that actually observes live system state
# (the API or the DB), as opposed to environment fumbling or filesystem pokes.
RUNTIME_PROBE_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"curl\s+\S*\b(8080|8082|localhost|API_URL|%API_URL%|\$API_URL)",
        r"/balances|/transfer",
        r"psql|psycopg|requests\.(post|get)",
        r"select\b.*\b(balance|accounts)",
        r"\.post\(|\.get\(",
    ]
]
# Commands that look shell-ish but observe nothing about the running system.
PROBE_EXCLUDE_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"^\s*echo\b",
        r"^\s*set\s+\w+=",
        r"printenv|^\s*env\b",
        r"--version",
        r"^\s*(ls|dir|find|cd|type|cat|pwd)\b",
    ]
]


def _is_runtime_probe(cmd: str) -> bool:
    if not cmd.strip():
        return False
    if any(p.search(cmd) for p in PROBE_EXCLUDE_PATTERNS):
        return False
    return any(p.search(cmd) for p in RUNTIME_PROBE_PATTERNS)


def _match_root_cause(text: str) -> tuple[str, str] | None:
    for cls, pats in _ROOT_CAUSE_COMPILED.items():
        for p in pats:
            m = p.search(text)
            if m:
                return cls, m.group(0)
    return None


def _load_trajectory(run_dir: Path) -> list[dict]:
    path = run_dir / "trajectory.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def classify_diagnosis_order(run_dir: Path) -> dict:
    history = _load_trajectory(run_dir)

    # Flatten every content block into one ordered event stream so diagnosis
    # text and probe results live on the same ordinal axis.
    ordinal = 0
    bash_call_cmd: dict[str, str] = {}  # tool_call id -> command (run_bash only)

    diagnosis_ordinal: int | None = None
    diagnosis_class: str | None = None
    diagnosis_term: str | None = None
    diagnosis_role: str | None = None

    first_probe_ordinal: int | None = None
    first_probe_cmd: str | None = None

    probe_attempts = 0
    probe_successes = 0
    submitted_patch = False

    # Did the task prompt itself name the bug class? (gate contamination check)
    task_prompt_named: str | None = None

    for msg in history:
        role = msg.get("role", "")
        for block in msg.get("content", []):
            btype = block.get("type")

            if btype == "text":
                text = block.get("text", "")
                if role == "user" and task_prompt_named is None:
                    hit = _match_root_cause(text)
                    if hit:
                        task_prompt_named = hit[1]
                if role == "assistant" and diagnosis_ordinal is None:
                    hit = _match_root_cause(text)
                    if hit:
                        diagnosis_ordinal = ordinal
                        diagnosis_class, diagnosis_term = hit
                        diagnosis_role = role

            elif btype == "tool_call":
                if block.get("name") == "run_bash":
                    bash_call_cmd[block.get("id", "")] = str(
                        block.get("input", {}).get("command", "")
                    )
                elif block.get("name") == "submit_patch":
                    submitted_patch = True

            elif btype == "tool_result":
                cmd = bash_call_cmd.get(block.get("id", ""))
                if cmd is not None and _is_runtime_probe(cmd):
                    probe_attempts += 1
                    if not block.get("is_error"):
                        probe_successes += 1
                        if first_probe_ordinal is None:
                            first_probe_ordinal = ordinal
                            first_probe_cmd = cmd

            ordinal += 1

    # Verdict
    if diagnosis_ordinal is None:
        verdict = "NO_DIAGNOSIS"
        test2 = "N/A"
        reason = "no named root cause in assistant text"
    elif first_probe_ordinal is None:
        verdict = "SOURCE_LEGIBLE"
        test2 = "FAIL"
        reason = (
            f"named '{diagnosis_term}' ({diagnosis_class}) with no successful runtime probe "
            f"({probe_attempts} probe attempt(s), 0 succeeded)"
        )
    elif diagnosis_ordinal < first_probe_ordinal:
        verdict = "SOURCE_LEGIBLE"
        test2 = "FAIL"
        reason = (
            f"named '{diagnosis_term}' ({diagnosis_class}) at event {diagnosis_ordinal}, "
            f"before first successful runtime probe at event {first_probe_ordinal}"
        )
    else:
        verdict = "INVESTIGATION_DRIVEN"
        test2 = "PASS"
        reason = (
            f"first successful runtime probe at event {first_probe_ordinal} precedes "
            f"named diagnosis at event {diagnosis_ordinal}"
        )

    return {
        "run_dir": str(run_dir),
        "verdict": verdict,
        "test2": test2,
        "reason": reason,
        "diagnosis_ordinal": diagnosis_ordinal,
        "diagnosis_class": diagnosis_class,
        "diagnosis_term": diagnosis_term,
        "diagnosis_role": diagnosis_role,
        "first_probe_ordinal": first_probe_ordinal,
        "first_probe_cmd": first_probe_cmd,
        "probe_attempts": probe_attempts,
        "probe_successes": probe_successes,
        "submitted_patch": submitted_patch,
        "task_prompt_named_root_cause": task_prompt_named,
    }


def _iter_run_dirs(path: Path) -> list[Path]:
    if (path / "trajectory.json").exists():
        return [path]
    return sorted(p.parent for p in path.glob("*/trajectory.json"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect whether diagnosis precedes investigation (Test 2, automated)."
    )
    parser.add_argument("path", type=Path, help="run dir or parent dir of run dirs")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write legibility_order.json into each run dir",
    )
    args = parser.parse_args()

    run_dirs = _iter_run_dirs(args.path.resolve())
    if not run_dirs:
        print(f"no trajectories under {args.path}", file=sys.stderr)
        return 2

    results = []
    any_fail = False
    for run_dir in run_dirs:
        result = classify_diagnosis_order(run_dir)
        results.append(result)
        any_fail = any_fail or result["test2"] == "FAIL"
        if args.write:
            (run_dir / "legibility_order.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )

    print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
