"""Six-stage patch-delivery instrumentation.

A submitted patch can die at any of six stages between "model decided to submit"
and "patched code builds". A 0-byte end state names none of them. This logger
records one structured event per stage attempt so that after any run the death
point is answerable by reading a single file (`patch_pipeline.jsonl`), never by
re-reading a trajectory.

Stages:
  1. submit_attempt   model emitted submit_patch (with which input keys)
  2. content_check    patch bytes present/intact (truncation visible here)
  3. extraction       which source the patch resolved from (workspace vs file vs ...)
  4. structural       has diff --git headers / LF / trailing newline
  5. git_apply        git apply succeeded against workspace src (+ stderr)
  6. build            docker/go build of the patched tree (+ stderr tail)

Plus loop-level events: turn_nudge, auto_submit, accepted, rejected.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PipelineLogger:
    """Accumulates stage records; dump to patch_pipeline.jsonl at run end."""

    records: list[dict[str, Any]] = field(default_factory=list)
    run_label: str = ""

    def log(self, stage: str, fields: dict[str, Any]) -> None:
        rec = {"ts": round(time.time(), 3), "stage": stage}
        if self.run_label:
            rec["run"] = self.run_label
        rec.update(fields)
        self.records.append(rec)

    def last_stage(self) -> str | None:
        return self.records[-1]["stage"] if self.records else None

    def death_stage(self) -> str | None:
        """Most recent stage that recorded a failure (ok=False), else None."""
        for rec in reversed(self.records):
            if rec.get("ok") is False:
                return rec["stage"]
        return None

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in self.records) + (
            "\n" if self.records else ""
        )

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8")


# Highest-value diagnostic helpers. A truncated diff ends mid-hunk with no
# trailing newline; a complete one ends on a clean context/'+' line.
def content_fields(patch_text: str, *, source: str) -> dict[str, Any]:
    text = patch_text or ""
    return {
        "source": source,
        "byte_len": len(text),
        "ends_with_newline": text.endswith("\n"),
        "has_diff_git_header": "diff --git" in text,
        "hunk_count": text.count("@@") // 2,
        "last_120_chars": text[-120:],
    }
