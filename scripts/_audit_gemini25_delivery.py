#!/usr/bin/env python3
"""Audit gemini-2.5-pro delivery across saved stale-read gate runs."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    "legibility_gate_runs_deleaked",
    "legibility_gate_runs_lag",
    "legibility_gate_runs_chaos",
    "legibility_gate_runs",
]

rows = []
for suite in SUITES:
    base = ROOT / "archetype-d-stale-read" / suite
    if not base.exists():
        continue
    for d in sorted(base.glob("*_gemini")):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("model") != "gemini-2.5-pro":
            continue
        rows.append(
            {
                "suite": suite,
                "run": d.name,
                "bytes": meta.get("patch_bytes", 0),
                "reapplies": meta.get("patch_reapplies"),
                "build_check": meta.get("build_check"),
                "chaos": meta.get("chaos_mode"),
                "death": meta.get("patch_death_stage"),
                "turns": meta.get("turns_used"),
            }
        )

print(f"gemini-2.5-pro runs: {len(rows)}")
delivered = sum(1 for r in rows if r["bytes"] > 0)
print(f"delivered: {delivered}/{len(rows)}")
for r in rows:
    print(
        f"  {r['suite']:32s} {r['run']:32s} "
        f"bytes={r['bytes']:5d} reapplies={r['reapplies']} "
        f"build_check={r['build_check']} chaos={r['chaos']} death={r['death']}"
    )
