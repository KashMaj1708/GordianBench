#!/usr/bin/env python3
"""
SWE-bench harness spike — run in WSL2/Linux only.

Checks whether swebench.harness.grading.get_eval_report is importable and
documents extension points for k-shot / Tier 2 chaos grading.

Native Windows fails on `resource` module (see requirements.txt).

Usage (WSL2 / Debian — PEP 668 externally-managed; do NOT pip install system-wide):

  cd /mnt/c/Users/kashy/Desktop/GordianBench/GordianBench
  sudo apt install python3.12-venv   # once; provides ensurepip for venv
  python3 -m venv .venv-wsl
  source .venv-wsl/bin/activate
  pip install swebench==4.1.0        # spike only; full deps: pip install -r requirements.txt
  python scripts/spike_swebench.py
"""

from __future__ import annotations

import inspect
import sys


def main() -> int:
    print("SWE-bench harness spike")
    print(f"  python: {sys.version}")
    print(f"  platform: {sys.platform}")

    try:
        import resource  # noqa: F401 — Unix only
    except ImportError as exc:
        print(f"\nBLOCKED on native Windows: {exc}")
        print("  Run this script in WSL2/Linux before building grade() on SWE-bench utilities.")
        return 2

    try:
        from swebench.harness.grading import get_eval_report, get_resolution_status
    except ModuleNotFoundError:
        print("\nBLOCKED: swebench not installed in this environment.")
        print("  Debian/WSL (PEP 668 — no system pip install):")
        print("    sudo apt install python3.12-venv")
        print("    python3 -m venv .venv-wsl && source .venv-wsl/bin/activate")
        print("    pip install swebench==4.1.0")
        return 3

    sig = inspect.signature(get_eval_report)
    print("\nget_eval_report import: OK")
    print(f"  signature: {sig}")

    print("\nExtension assessment for Agent-DSBench:")
    print("  - get_eval_report is log-parser based (single pytest run → F2P/P2P flip)")
    print("  - k-shot / Tier 2 chaos: NOT built-in — wrap or post-process, do not assume extend")
    print("  - Reuse: docker_utils cleanup, run_evaluation container lifecycle")
    print("  - Agent-DSBench grade(): compose-based Tier1+Tier2 + optional get_eval_report for F2P mapping")

    print(f"\nget_resolution_status: {get_resolution_status}")
    print("\nSpike: PASS (imports OK on Unix host)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
