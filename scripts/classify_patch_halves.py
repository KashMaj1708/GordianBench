#!/usr/bin/env python3
"""Classify Archetype A patches: idempotency half vs calm-path (timeout/retry) half."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.grade import grade_patch_tier


def classify_patch_text(text: str) -> dict[str, bool]:
    added = [ln[1:] for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    added_blob = "\n".join(added).lower()
    idempotency = any(
        s in added_blob
        for s in (
            "idempotency-key",
            "idempotency_key",
            "on conflict",
            "lookupexisting",
            "duplicate",
        )
    )
    calm_path = any(
        s in added_blob
        for s in (
            "clienttimeout :=",
            "commitlatency :=",
            "client_timeout_ms",
            "commit_latency_ms",
            "successful200",
            "final idempotency probe",
        )
    ) or (
        "select id from ledger where idempotency_key" in added_blob
        and "time.sleep(commitlatency)" not in added_blob.split("select id")[0][-200:]
    )
    return {"idempotency_half": idempotency, "calm_path_half": calm_path}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch", type=Path)
    parser.add_argument("--grade", action="store_true")
    args = parser.parse_args()

    text = args.patch.read_text(encoding="utf-8")
    halves = classify_patch_text(text)
    print(f"patch: {args.patch} ({len(text)} bytes)")
    print(f"idempotency_half={halves['idempotency_half']} calm_path_half={halves['calm_path_half']}")

    if args.grade:
        try:
            t1 = grade_patch_tier(text, 1, teardown=True)
            t2 = grade_patch_tier(text, 2, teardown=True)
            print(f"tier1={t1} tier2={t2} full={1.0 if t1 == t2 == 1.0 else 0.0}")
        except Exception as exc:
            print(f"grade_error={exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
