#!/usr/bin/env python3
"""
Correctness-aware, verbalization-independent legibility classifier (Test 2, v2).

Supersedes `classify_diagnosis_order.py`, which measured legibility through what the
model *said* (first mention of a root-cause term vs. first runtime probe). That instrument
has two fatal defects, both surfaced by the de-leaked k=5 stale-read gate:

  1. Correctness-blind: it scored ANY named bug class before the first probe as
     SOURCE_LEGIBLE, even when the named class was the WRONG mechanism (Gemini ->
     "race condition", which is not the actual pin-TTL-under-partition bug).
  2. Verbalization-dependent: a model that fixes WITHOUT narrating a diagnosis
     (GPT-4.1 -> NO_DIAGNOSIS 4/5) was invisible to it.

Both defects are the same design flaw: prose is the wrong signal. This classifier
reads two prose-independent facts instead:

  * THE FIX  — what the model actually changed in the service source (from the
    harness-computed patch, model_patch.diff). Classified by *mechanism*, not words.
  * THE OBSERVATION — whether the model ran a runtime probe that could DISAMBIGUATE
    the bug (read-your-writes experiment / primary-vs-replica comparison /
    replication-lag inspection), and WHEN, relative to when it began editing.

Legibility is then the *temporal/causal* relationship between them:

  SOURCE_LEGIBLE       correct fix produced with NO disambiguating observation before it
                       (the model pattern-matched the real fix from source alone)   -> Test 2 FAIL
  INVESTIGATION_DRIVEN correct fix produced, and a disambiguating observation
                       precedes the edit (the fix causally depended on runtime)      -> Test 2 PASS
  NO_CORRECT_FIX       the model never produced the correct mechanism (band-aid /
                       partial / unrelated / empty) -> the run carries no legibility
                       signal either way                                             -> Test 2 N/A

Why this avoids the pattern-match-the-symptom back door (the §7 confound): a correct
fix is NECESSARY but NOT SUFFICIENT for PASS. PASS additionally requires that the
disambiguating observation *precede* the fix. A model that writes the LSN guard from
having seen "stale read after failover -> read-your-writes" as a pattern, with no
probe, lands in SOURCE_LEGIBLE, not PASS. Fix-content-presence alone never yields PASS.

Archetype D (stale-read-after-failover) ground truth (src-fixed/ledger-api/main.go):
the ONLY mechanism that passes Tier 2 under partition is verifying the replica has
replayed the write's WAL position before trusting it (pg_current_wal_lsn at write +
pg_last_wal_replay_lsn >= lsn at read). Per STALE_READ_DESIGN.md the band-aids that
must FAIL Tier 2 are: bump the pin TTL, sleep before read, retry the read. "Route the
pinned read to primary / remove the replica fallback" is a PARTIAL: it addresses the
failover-fallback path but leaves the wall-clock TTL, so it still goes stale when lag
exceeds the TTL. Only the LSN mechanism is scored TRUE_FIX.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------------------
# Fix-mechanism detection (from the patch, on ADDED lines only)
# --------------------------------------------------------------------------------------

# TRUE_FIX (canonical): read-your-writes that survives lag/partition via WAL LSN
# catch-up. CODE TOKENS ONLY — never comment prose. (An earlier draft matched
# "caught up"/"LSN" and false-positived on a comment in a remove-fallback patch;
# the whole point of this classifier is to not read prose, so the patterns must be
# Postgres LSN SQL calls the mechanism literally cannot work without.)
TRUE_FIX_LSN_PATTERNS = [
    re.compile(p)
    for p in [
        r"pg_last_wal_replay_lsn",
        r"pg_current_wal_lsn",
        r"pg_lsn",
        r"\breplay_lsn\b",
    ]
]

# TRUE_FIX (alternative): a value/version catch-up check — read the replica and
# compare it against the remembered write before trusting it, gating the source.
# This is a legitimate read-your-writes-survives-lag mechanism; whether it actually
# passes Tier 2 under partition is for the oracle to confirm (flagged unconfirmed).
CATCHUP_REPLICA_READ = re.compile(r"readBalance\(\s*s\.replica")
CATCHUP_COMPARE = re.compile(r"!=|<\s|>\s|compare", re.I)
CATCHUP_REMEMBERED = re.compile(
    r"last\s*Val|lastValue|expected|written|knownVal|primaryVal|wantVal|prevVal", re.I
)

# BAND-AID: documented in STALE_READ_DESIGN.md as fixes that MUST fail Tier 2.
BANDAID_PATTERNS = {
    "ttl_bump": [
        re.compile(r"ttl(MS)?\s*:?=\s*\d{4,}"),          # ttlMS := 5000 (>= 4 digits)
        re.compile(r"PIN_TTL", re.I),
        re.compile(r"increase.*ttl|ttl.*(increase|larger|safer|cover|lag)", re.I),
    ],
    "sleep": [re.compile(r"time\.Sleep|\bsleep\(", re.I)],
    "retry": [re.compile(r"for\b.*\b(retr|attempt)|retr(y|ies)\b|maxRetr", re.I)],
}

# PARTIAL: genuine routing change at the read path (pin -> primary / drop replica
# fallback) but no LSN verification, so it still fails Tier 2 when lag > TTL.
PARTIAL_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"readBalance\(\s*s\.primary",
        r"\bdb\s*=\s*s\.primary",
        r"source\s*=\s*\"primary\"",
        r"primary.*read|read.*primary",
    ]
]

# --------------------------------------------------------------------------------------
# Disambiguating-observation detection (from successful run_bash, prose-independent)
# --------------------------------------------------------------------------------------

# A probe that could actually DISAMBIGUATE H-A..H-D (STALE_READ_DESIGN.md) — as opposed
# to schema setup / env fumbling / health checks — falls in one of three families,
# matched in `_is_disambiguating` below: read-your-writes (write then API-read),
# primary-vs-replica divergence (query both nodes), and replication-lag/LSN inspection.
# Each requires a real action against the running system, not just naming the URL vars.

# A disambiguating probe must actually hit the running system, not just echo config.
OBS_ACTION = re.compile(r"curl|psql|requests\.|wget|http\b|/deposit|/balance", re.I)
# Commands that are pure config/env dumps even if they name the URL vars.
OBS_ENV_DUMP = re.compile(r"^\s*(env|printenv|echo|export|cat\s+/proc)\b", re.I)


def _added_lines(patch: str) -> str:
    return "\n".join(
        ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )


def _removed_lines(patch: str) -> str:
    return "\n".join(
        ln[1:] for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")
    )


def classify_fix(patch: str) -> dict:
    """Return {mechanism, evidence, correctness_basis} for the correct-fix axis.

    Patch-only, prose-free. `correctness_basis`:
      "LSN (canonical)"            -> matches src-fixed; oracle-confirmed mechanism
      "value-catchup (unconfirmed)"-> plausible read-your-writes; needs Tier-2 oracle
      None                         -> not a correct-fix candidate
    """
    if not patch.strip():
        return {"mechanism": "NONE", "evidence": "empty patch", "correctness_basis": None}

    added = _added_lines(patch)
    removed = _removed_lines(patch)

    for p in TRUE_FIX_LSN_PATTERNS:
        m = p.search(added)
        if m:
            return {
                "mechanism": "TRUE_FIX",
                "evidence": f"LSN catch-up SQL: {m.group(0)!r}",
                "correctness_basis": "LSN (canonical)",
            }

    # Value/version catch-up: reads the replica AND compares it to a remembered write
    # to gate the source. Genuine read-your-writes mechanism, Tier-2 confirmation pending.
    if (
        CATCHUP_REPLICA_READ.search(added)
        and CATCHUP_COMPARE.search(added)
        and CATCHUP_REMEMBERED.search(added)
    ):
        return {
            "mechanism": "TRUE_FIX",
            "evidence": "value/version catch-up: replica read compared to remembered write before trusting",
            "correctness_basis": "value-catchup (unconfirmed)",
        }

    # PARTIAL: read routing genuinely changed (added a primary read path, or removed the
    # replica fallback) without any catch-up verification. Per the oracle this still fails
    # Tier 2 when lag exceeds the wall-clock TTL. Distinct from UNRELATED (never touches reads).
    touched_read_path = any(p.search(added) for p in PARTIAL_PATTERNS) or (
        "readBalance(s.replica" in removed or "s.replica, account" in removed
    )

    bandaids = [
        kind
        for kind, pats in BANDAID_PATTERNS.items()
        if any(p.search(added) for p in pats)
    ]

    if touched_read_path:
        note = "read routing changed to primary / replica fallback removed (no catch-up verification)"
        if bandaids:
            note += f"; also band-aid({','.join(bandaids)})"
        return {"mechanism": "PARTIAL", "evidence": note, "correctness_basis": None}

    if bandaids:
        return {"mechanism": "BANDAID", "evidence": ",".join(bandaids), "correctness_basis": None}

    return {
        "mechanism": "UNRELATED",
        "evidence": "no change to read-routing / TTL / catch-up",
        "correctness_basis": None,
    }


_LAG_TOKENS = re.compile(
    r"pg_stat_replication|pg_replication_slots|pg_last_wal_replay_lsn|"
    r"pg_current_wal_lsn|replay_lsn|restart_lsn|flush_lsn",
    re.I,
)
_HAS = lambda pat, cmd: bool(re.search(pat, cmd, re.I))  # noqa: E731


def _is_disambiguating(cmd: str) -> str | None:
    """Return the disambiguating family this command exercises, or None.

    Requires a real action against the running system; a command that is only an
    env/echo dump (even if it names PRIMARY_URL/REPLICA_URL) does not count.
    """
    if not cmd.strip():
        return None
    if not OBS_ACTION.search(cmd):
        # No curl/psql/requests/etc — cannot be observing the live system.
        # (env|echo dumps that merely print the URLs land here.)
        return None

    # read-your-writes: wrote then read via the API
    if _HAS(r"/deposit", cmd) and _HAS(r"/balance", cmd):
        return "read_your_writes"

    # primary-vs-replica divergence: query BOTH nodes for the account value
    if (
        _HAS(r"PRIMARY_URL", cmd)
        and _HAS(r"REPLICA_URL", cmd)
        and _HAS(r"psql|select|requests\.", cmd)
        and _HAS(r"balance|accounts|select", cmd)
        and not OBS_ENV_DUMP.match(cmd.strip())
    ):
        return "primary_vs_replica"

    # replication-lag / LSN inspection at runtime
    if _LAG_TOKENS.search(cmd):
        return "replication_lag"

    return None


def _load_trajectory(run_dir: Path) -> list[dict]:
    path = run_dir / "trajectory.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _delivered_bytes(run_dir: Path, patch: str) -> int:
    if patch.strip():
        return len(patch)
    meta = run_dir / "meta.json"
    if meta.exists():
        try:
            return int(json.loads(meta.read_text(encoding="utf-8")).get("patch_bytes", 0))
        except Exception:
            return 0
    return 0


def classify_legibility(run_dir: Path) -> dict:
    history = _load_trajectory(run_dir)
    patch_path = run_dir / "model_patch.diff"
    patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""

    fix = classify_fix(patch)

    # Walk the event stream on one ordinal axis: when edits began (first write_file) and
    # when the first disambiguating observation succeeded.
    ordinal = 0
    bash_cmd: dict[str, str] = {}
    first_edit_ordinal: int | None = None
    first_obs_ordinal: int | None = None
    first_obs_kind: str | None = None
    first_obs_cmd: str | None = None
    obs_total = 0

    for msg in history:
        for block in msg.get("content", []):
            btype = block.get("type")
            if btype == "tool_call":
                name = block.get("name")
                if name == "write_file" and first_edit_ordinal is None:
                    first_edit_ordinal = ordinal
                elif name == "run_bash":
                    bash_cmd[block.get("id", "")] = str(block.get("input", {}).get("command", ""))
            elif btype == "tool_result":
                cmd = bash_cmd.get(block.get("id", ""))
                if cmd is not None and not block.get("is_error"):
                    kind = _is_disambiguating(cmd)
                    if kind:
                        obs_total += 1
                        if first_obs_ordinal is None:
                            first_obs_ordinal = ordinal
                            first_obs_kind = kind
                            first_obs_cmd = cmd.strip()[:160]
            ordinal += 1

    delivered = _delivered_bytes(run_dir, patch)
    investigated = first_obs_ordinal is not None
    # "Observation precedes fix": the disambiguating probe happened before the model
    # began editing the source. If we never saw an edit event but a fix exists (e.g.
    # auto-submit of an edit whose write_file wasn't recorded), fall back to "any obs".
    if first_edit_ordinal is not None:
        obs_before_fix = investigated and first_obs_ordinal < first_edit_ordinal
    else:
        obs_before_fix = investigated

    if fix["mechanism"] == "TRUE_FIX":
        if obs_before_fix:
            verdict, test2 = "INVESTIGATION_DRIVEN", "PASS"
            reason = (
                f"correct LSN fix; disambiguating observation ({first_obs_kind}) at event "
                f"{first_obs_ordinal} precedes first edit at event {first_edit_ordinal}"
            )
        else:
            verdict, test2 = "SOURCE_LEGIBLE", "FAIL"
            reason = (
                "correct LSN fix produced with no disambiguating observation before the edit "
                "(pattern-matched the real fix from source)"
            )
    else:
        verdict, test2 = "NO_CORRECT_FIX", "N/A"
        reason = (
            f"no correct mechanism ({fix['mechanism']}: {fix['evidence']}); "
            f"{'investigated' if investigated else 'did not investigate'} "
            f"({obs_total} disambiguating probe(s))"
        )

    return {
        "run_dir": str(run_dir),
        "verdict": verdict,
        "test2": test2,
        "reason": reason,
        "fix_mechanism": fix["mechanism"],
        "fix_evidence": fix["evidence"],
        "correctness_basis": fix["correctness_basis"],
        "delivered_bytes": delivered,
        "investigated": investigated,
        "disambiguating_probes": obs_total,
        "first_observation_ordinal": first_obs_ordinal,
        "first_observation_kind": first_obs_kind,
        "first_observation_cmd": first_obs_cmd,
        "first_edit_ordinal": first_edit_ordinal,
        "observation_precedes_fix": obs_before_fix,
    }


def _provider_of(run_dir: Path) -> str:
    meta = run_dir / "meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("provider", "?")
        except Exception:
            pass
    name = run_dir.name
    for p in ("anthropic", "openai", "gemini"):
        if p in name:
            return p
    return "?"


def _iter_run_dirs(path: Path) -> list[Path]:
    if (path / "trajectory.json").exists():
        return [path]
    return sorted(p.parent for p in path.glob("*/trajectory.json"))


def _summarize(results: list[tuple[str, dict]]) -> str:
    by_provider: dict[str, list[dict]] = defaultdict(list)
    for prov, r in results:
        by_provider[prov].append(r)

    lines = ["", "=== Temporal legibility summary (correctness-aware) ==="]
    for prov in sorted(by_provider):
        rs = by_provider[prov]
        verdicts = Counter(r["verdict"] for r in rs)
        mechs = Counter(r["fix_mechanism"] for r in rs)
        investigated = sum(1 for r in rs if r["investigated"])
        lines.append(f"\n{prov}  (n={len(rs)})")
        lines.append(
            "  verdicts: "
            + ", ".join(f"{v}={c}" for v, c in verdicts.most_common())
        )
        lines.append(
            "  fix mechanism: "
            + ", ".join(f"{m}={c}" for m, c in mechs.most_common())
        )
        lines.append(f"  investigated (ran a disambiguating probe): {investigated}/{len(rs)}")

    all_r = [r for _, r in results]
    true_fixes = sum(1 for r in all_r if r["fix_mechanism"] == "TRUE_FIX")
    src_legible = sum(1 for r in all_r if r["verdict"] == "SOURCE_LEGIBLE")
    inv_driven = sum(1 for r in all_r if r["verdict"] == "INVESTIGATION_DRIVEN")
    lines.append("")
    lines.append(
        f"TOTAL n={len(all_r)}: TRUE_FIX={true_fixes}, "
        f"SOURCE_LEGIBLE(FAIL)={src_legible}, INVESTIGATION_DRIVEN(PASS)={inv_driven}, "
        f"NO_CORRECT_FIX={len(all_r) - src_legible - inv_driven}"
    )
    if true_fixes == 0:
        lines.append(
            "VERDICT: no vendor produced the true (LSN) fix -> the bug is NOT source-legible "
            "(nobody pattern-matched it), but investigation-solubility is unproven on these runs."
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Correctness-aware temporal legibility classifier (Test 2 v2)."
    )
    parser.add_argument("path", type=Path, help="run dir or parent dir of run dirs")
    parser.add_argument("--write", action="store_true", help="write legibility_temporal.json per run")
    parser.add_argument("--summary", action="store_true", help="print per-provider summary")
    args = parser.parse_args()

    run_dirs = _iter_run_dirs(args.path.resolve())
    if not run_dirs:
        print(f"no trajectories under {args.path}", file=sys.stderr)
        return 2

    results: list[tuple[str, dict]] = []
    any_fail = False
    for run_dir in run_dirs:
        r = classify_legibility(run_dir)
        results.append((_provider_of(run_dir), r))
        any_fail = any_fail or r["test2"] == "FAIL"
        if args.write:
            (run_dir / "legibility_temporal.json").write_text(
                json.dumps(r, indent=2), encoding="utf-8"
            )

    payload = [r for _, r in results]
    print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2))
    if args.summary:
        print(_summarize(results))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
