#!/usr/bin/env python3
"""Grade a saved model_patch on Tier 1 and/or Tier 2 independently."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.patch_util import repair_model_patch
from harness.grade import grade_patch, grade_patch_tier


def main() -> int:
    parser = argparse.ArgumentParser(description="Grade model_patch by tier")
    parser.add_argument("patch", type=Path, help="Path to model_patch.diff")
    parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2],
        help="Run only this tier (default: both)",
    )
    args = parser.parse_args()

    patch = repair_model_patch(args.patch.read_text(encoding="utf-8"))
    print(f"patch: {len(patch)} bytes from {args.patch}")

    if args.tier:
        score = grade_patch_tier(patch, args.tier)
        print(f"Tier {args.tier}: score={score}")
        return 0 if score == 1.0 else 1

    score = grade_patch(patch)
    print(f"grade_patch (Tier 1 + Tier 2): score={score}")
    return 0 if score == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
