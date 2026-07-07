"""Phase 6 resolution-rate rollup.

Turns a sweep of gate run-dirs into a resolution table, enforcing structurally
the lessons the project paid for:

- **Oracle verdicts, never classifier labels.** A run scored ``TRUE_FIX`` by the
  temporal classifier but ``FAIL`` by the Tier-2 oracle is a FAIL. Scoring reads
  ``oracle_grade.json``; the classifier label is only carried for an
  agreement statistic, never for the resolution rate. (Report 7: classifier↔oracle
  agreement was 1/5.)
- **Delivery-corrupted ≠ capability fail.** A delivered patch that does not
  re-apply at grade time (``meta.patch_reapplies is False``) is *excluded and
  flagged for re-run*, not bucketed as 0.0 — otherwise a harness slip is scored as
  model incapability (Report 9, the 014222Z class).
- **Build-fail is its own bucket**, distinct from capability fail (a typo is not an
  inability to fix). With ``build_check`` on in the gate this should be ~0.

Categories (mutually exclusive, one per run):

    PASS                oracle PASS (read-your-writes holds under chaos)
    CAPABILITY_FAIL     delivered + compiled + re-applies, oracle FAIL
    BUILD_FAIL          delivered + re-applies, does not compile
    NO_PATCH            model delivered no patch
    DELIVERY_CORRUPTED  delivered but does not re-apply -> EXCLUDE / re-run

Resolution rate = PASS / (PASS + CAPABILITY_FAIL + BUILD_FAIL + NO_PATCH).
DELIVERY_CORRUPTED is excluded from the denominator and surfaced separately so a
nonzero count loudly demands a re-run rather than silently deflating the rate.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PASS = "PASS"
CAPABILITY_FAIL = "CAPABILITY_FAIL"
BUILD_FAIL = "BUILD_FAIL"
NO_PATCH = "NO_PATCH"
DELIVERY_CORRUPTED = "DELIVERY_CORRUPTED"

#: Counted in the resolution-rate denominator (a real scoreable outcome).
SCORED = (PASS, CAPABILITY_FAIL, BUILD_FAIL, NO_PATCH)
#: Excluded from the denominator; a nonzero count means "re-run these".
EXCLUDED = (DELIVERY_CORRUPTED,)


def categorize_run(meta: dict, oracle: dict | None) -> str:
    """Bucket one run from its ``meta.json`` and ``oracle_grade.json`` dicts.

    The order matters: no-patch and delivery-corruption are decided from meta
    *before* any oracle verdict is consulted, so a corrupted/absent patch can
    never be mistaken for a capability fail.
    """
    if not meta.get("patch_bytes"):
        return NO_PATCH
    # `is False` (not falsy): None means "not checked" (legacy run) and should not
    # be silently treated as corrupted.
    if meta.get("patch_reapplies") is False:
        return DELIVERY_CORRUPTED
    if oracle is None:
        raise ValueError(
            "delivered, re-appliable run has no oracle_grade.json; oracle-grade it "
            "before rollup (resolution rates must be oracle-graded, not inferred)"
        )
    grade = str(oracle.get("grade", "")).upper()
    if grade == BUILD_FAIL:
        return BUILD_FAIL
    if grade == PASS:
        return PASS
    if grade == "FAIL":
        return CAPABILITY_FAIL
    raise ValueError(f"unknown oracle grade {oracle.get('grade')!r}")


@dataclass
class CellRollup:
    """Resolution stats for one matrix cell (e.g. a single model)."""

    key: str
    counts: Counter = field(default_factory=Counter)
    classifier_oracle_agree: int = 0
    classifier_oracle_total: int = 0

    @property
    def scored(self) -> int:
        return sum(self.counts[c] for c in SCORED)

    @property
    def excluded(self) -> int:
        return sum(self.counts[c] for c in EXCLUDED)

    @property
    def resolution_rate(self) -> float:
        return self.counts[PASS] / self.scored if self.scored else 0.0

    @property
    def needs_rerun(self) -> bool:
        return self.excluded > 0


@dataclass
class Rollup:
    cells: dict[str, CellRollup] = field(default_factory=dict)

    def add(
        self,
        cell_key: str,
        category: str,
        *,
        classifier_says_fix: bool | None = None,
    ) -> None:
        cell = self.cells.setdefault(cell_key, CellRollup(key=cell_key))
        cell.counts[category] += 1
        # Optional classifier↔oracle agreement bookkeeping (reporting only; never
        # affects scoring). "fix" claim agrees with oracle iff PASS<->says_fix.
        if classifier_says_fix is not None and category in SCORED:
            oracle_pass = category == PASS
            cell.classifier_oracle_total += 1
            if oracle_pass == classifier_says_fix:
                cell.classifier_oracle_agree += 1

    def total(self) -> CellRollup:
        agg = CellRollup(key="TOTAL")
        for c in self.cells.values():
            agg.counts.update(c.counts)
            agg.classifier_oracle_agree += c.classifier_oracle_agree
            agg.classifier_oracle_total += c.classifier_oracle_total
        return agg


_CLASSIFIER_FIX_LABELS = {"TRUE_FIX"}


def _load_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_run(run_dir: Path) -> tuple[dict, dict | None, bool | None]:
    """Load (meta, oracle, classifier_says_fix) for a run dir."""
    meta = _load_json(run_dir / "meta.json") or {}
    oracle = _load_json(run_dir / "oracle_grade.json")
    classifier = _load_json(run_dir / "legibility_temporal.json")
    says_fix = None
    if classifier is not None:
        says_fix = str(classifier.get("fix_mechanism", "")).upper() in _CLASSIFIER_FIX_LABELS
    return meta, oracle, says_fix


def rollup_dirs(run_dirs: list[Path], *, cell_key=lambda meta: meta.get("model", "?")) -> Rollup:
    r = Rollup()
    for d in run_dirs:
        meta, oracle, says_fix = load_run(d)
        category = categorize_run(meta, oracle)
        r.add(cell_key(meta), category, classifier_says_fix=says_fix)
    return r


def format_table(rollup: Rollup) -> str:
    cols = [PASS, CAPABILITY_FAIL, BUILD_FAIL, NO_PATCH, DELIVERY_CORRUPTED]
    header = f"{'cell':28s} {'resol':>7s} " + " ".join(f"{c[:9]:>9s}" for c in cols) + "  rerun?"
    lines = [header, "-" * len(header)]
    for key in sorted(rollup.cells):
        c = rollup.cells[key]
        cells = " ".join(f"{c.counts[col]:>9d}" for col in cols)
        rate = f"{c.resolution_rate:.0%}({c.counts[PASS]}/{c.scored})"
        lines.append(f"{key:28s} {rate:>7s} {cells}  {'YES' if c.needs_rerun else '-'}")
    t = rollup.total()
    cells = " ".join(f"{t.counts[col]:>9d}" for col in cols)
    rate = f"{t.resolution_rate:.0%}({t.counts[PASS]}/{t.scored})"
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':28s} {rate:>7s} {cells}  {'YES' if t.needs_rerun else '-'}")
    if t.classifier_oracle_total:
        lines.append(
            f"\nclassifier<->oracle agreement: "
            f"{t.classifier_oracle_agree}/{t.classifier_oracle_total} "
            f"(reporting only; scoring uses oracle verdicts)"
        )
    return "\n".join(lines)
