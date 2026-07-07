#!/usr/bin/env python3
"""Unit lock for the Phase 6 rollup aggregation (no API, no Docker).

Feeds synthetic meta.json / oracle_grade.json dicts covering every category and
asserts the rollup buckets them correctly — in particular the two rules the
project paid for: (1) a classifier-TRUE_FIX that the oracle FAILs is scored FAIL,
and (2) a delivery-corrupted patch is EXCLUDED (re-run), never a capability fail
that deflates the rate.

Run:
    .venv\\Scripts\\python.exe scripts\\test_rollup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.rollup import (
    BUILD_FAIL,
    CAPABILITY_FAIL,
    DELIVERY_CORRUPTED,
    NO_PATCH,
    PASS,
    Rollup,
    categorize_run,
)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def eq(self, label: str, got, want) -> None:
        if got == want:
            self.passes += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: got {got!r} want {want!r}")
            print(f"  FAIL  {label} :: got {got!r} want {want!r}")

    def truthy(self, label: str, cond: bool, detail: str = "") -> None:
        self.eq(label, bool(cond), True) if cond else self.eq(label, detail or False, True)


def _meta(bytes_=1000, reapplies=True):
    return {"patch_bytes": bytes_, "patch_reapplies": reapplies, "model": "m"}


def test_categories(chk: Checker) -> None:
    print("\n=== categorize_run: every bucket ===")
    chk.eq("oracle PASS -> PASS", categorize_run(_meta(), {"grade": "PASS"}), PASS)
    chk.eq("oracle FAIL -> CAPABILITY_FAIL", categorize_run(_meta(), {"grade": "FAIL"}), CAPABILITY_FAIL)
    chk.eq("oracle BUILD_FAIL -> BUILD_FAIL", categorize_run(_meta(), {"grade": "BUILD_FAIL"}), BUILD_FAIL)
    chk.eq("no patch -> NO_PATCH", categorize_run(_meta(bytes_=0), None), NO_PATCH)
    chk.eq(
        "reapplies False -> DELIVERY_CORRUPTED (even with oracle PASS present)",
        categorize_run(_meta(reapplies=False), {"grade": "PASS"}),
        DELIVERY_CORRUPTED,
    )
    # legacy run with patch_reapplies absent (None) must NOT be treated as corrupted
    chk.eq(
        "reapplies None (legacy) still scored by oracle",
        categorize_run({"patch_bytes": 10, "model": "m"}, {"grade": "FAIL"}),
        CAPABILITY_FAIL,
    )


def test_classifier_does_not_score(chk: Checker) -> None:
    print("\n=== classifier label never overrides the oracle ===")
    # The headline guarantee: classifier says TRUE_FIX, oracle says FAIL -> FAIL.
    r = Rollup()
    r.add("m", categorize_run(_meta(), {"grade": "FAIL"}), classifier_says_fix=True)
    cell = r.cells["m"]
    chk.eq("classifier TRUE_FIX + oracle FAIL -> CAPABILITY_FAIL", cell.counts[CAPABILITY_FAIL], 1)
    chk.eq("...and NOT counted as PASS", cell.counts[PASS], 0)
    chk.eq("...resolution rate 0%", cell.resolution_rate, 0.0)
    chk.eq("...agreement recorded as disagreement", cell.classifier_oracle_agree, 0)
    chk.eq("...agreement total counts it", cell.classifier_oracle_total, 1)


def test_rate_and_exclusion(chk: Checker) -> None:
    print("\n=== resolution rate + exclusion arithmetic ===")
    r = Rollup()
    # 2 PASS, 1 CAPABILITY_FAIL, 1 BUILD_FAIL, 1 NO_PATCH, 1 DELIVERY_CORRUPTED
    for g in ("PASS", "PASS"):
        r.add("m", categorize_run(_meta(), {"grade": g}))
    r.add("m", categorize_run(_meta(), {"grade": "FAIL"}))
    r.add("m", categorize_run(_meta(), {"grade": "BUILD_FAIL"}))
    r.add("m", categorize_run(_meta(bytes_=0), None))
    r.add("m", categorize_run(_meta(reapplies=False), None))
    cell = r.cells["m"]
    # denominator excludes the corrupted one: 2/(2+1+1+1)=2/5=40%
    chk.eq("scored denominator excludes corrupted", cell.scored, 5)
    chk.eq("excluded count", cell.excluded, 1)
    chk.eq("resolution rate = PASS/scored", round(cell.resolution_rate, 3), 0.4)
    chk.truthy("needs_rerun flagged when corrupted present", cell.needs_rerun)
    # If the corrupted one had been miscounted as a capability fail it'd be 2/6=33%.
    chk.truthy("corruption did NOT deflate to 2/6", abs(cell.resolution_rate - (2 / 6)) > 1e-6)


def test_unknown_grade_raises(chk: Checker) -> None:
    print("\n=== guards ===")
    try:
        categorize_run(_meta(), {"grade": "MAYBE"})
        chk.truthy("unknown grade raises", False, "no exception")
    except ValueError:
        chk.truthy("unknown grade raises", True)
    try:
        categorize_run(_meta(), None)
        chk.truthy("delivered+reappliable w/o oracle raises", False, "no exception")
    except ValueError:
        chk.truthy("delivered+reappliable w/o oracle raises", True)


def main() -> int:
    chk = Checker()
    test_categories(chk)
    test_classifier_does_not_score(chk)
    test_rate_and_exclusion(chk)
    test_unknown_grade_raises(chk)
    print(f"\n{chk.passes} passed, {len(chk.failures)} failed")
    if chk.failures:
        for f in chk.failures:
            print(f"  - {f}")
        return 1
    print("ROLLUP LOCK: GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
