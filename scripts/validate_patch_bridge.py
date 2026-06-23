#!/usr/bin/env python3
"""
Bridge test: fixed patch as raw git diff must grade 1.0 via grade_patch().

Proves the dynamic apply path reaches the same verdict as grade('fixed') overlay path.
This is the load-bearing Phase 4 gate before any agent nondeterminism enters.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.grade import grade, grade_patch
from harness.patch import PATCHES_ROOT, generate_fixed_model_patch


def _load_or_generate_fixed_patch() -> str:
    corpus = PATCHES_ROOT / "fixed" / "model_patch.diff"
    if corpus.exists():
        return corpus.read_text(encoding="utf-8")
    patch = generate_fixed_model_patch()
    corpus.write_text(patch, encoding="utf-8")
    return patch


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch bridge: diff-apply path vs overlay path")
    parser.add_argument(
        "--skip-overlay",
        action="store_true",
        help="Skip grade('fixed') overlay check (~3 min)",
    )
    args = parser.parse_args()

    patch = _load_or_generate_fixed_patch()
    print(f"model_patch: {len(patch)} bytes from corpus")

    print("\ngrade_patch(fixed diff) ...")
    score_patch = grade_patch(patch)
    print(f"  score={score_patch} expected=1.0 {'OK' if score_patch == 1.0 else 'FAIL'}")
    if score_patch != 1.0:
        return 1

    if not args.skip_overlay:
        print("\ngrade('fixed') overlay sanity ...")
        score_overlay = grade("fixed")
        print(f"  score={score_overlay} expected=1.0 {'OK' if score_overlay == 1.0 else 'FAIL'}")
        if score_overlay != 1.0:
            return 1

    print("\nBridge test: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
