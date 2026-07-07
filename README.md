# GordianBench

A distributed-systems agent debugging benchmark with a **chaos-validated oracle** that distinguishes real invariant-preserving fixes from timing-luck band-aids. Scoring uses oracle grades only — classifier labels are diagnostic, never headline metrics.

**Published result:** [`archetype-d-stale-read/GordianBench_Final_Research_Report.pdf`](archetype-d-stale-read/GordianBench_Final_Research_Report.pdf)

---

## What this repo is

GordianBench measures whether LLM agents can **investigate, patch, and survive** bugs in replicated / laggy systems — not whether they can make a single calm-path test pass.

The v1 focus is **Archetype D (stale-read-after-failover)**: a ledger API that must preserve read-your-writes when a Postgres replica lags behind the primary. Patches are graded under a **recovering-lag** chaos profile (20s downstream toxic, 10 trials per patch).

The benchmark ships:

- An **agent loop** with multi-vendor providers (`agent/`)
- A **grading harness** with build checks and oracle rollout (`harness/`)
- A **frozen Phase 6 cell executor** (`scripts/run_phase6_cell.py`)
- A containerized stale-read topology (`archetype-d-stale-read/`)

Working phase reports and intermediate writeups live locally and are gitignored; the PDF above is the canonical published artifact.

---

## Headline finding (Phase 6)

Under identical frozen config (k=5, shared prompt, build_check on, oracle-graded), the stale-read archetype produced a **three-step capability gradient** on the same bug:

| Model | Tier | Resolution |
|-------|------|------------|
| Claude Haiku 4.5 | Mid-tier | 0% (0/5) |
| Gemini 2.5 Pro | Mid-tier | 0% (0/5) |
| GPT-4.1 | Mid-tier | 40% (2/5) |
| Claude Opus 4.8 | Frontier | 40% (2/5) |
| GPT-5.5 | Frontier | 100% (5/5) |

**Mechanistic contribution:** the ~60-point gap between GPT-5.5 and Opus on LSN-shaped attempts is explained by one transaction-ordering detail — capturing `pg_current_wal_lsn()` **after commit** vs **inside** the writing `UPDATE … RETURNING` (P3 pre-commit watermark). Patch inspection confirms this on both sides.

**Scope:** this is a finding about **this archetype and this mechanism**, not a general vendor leaderboard. GPT-5.5 saturated the task (5/5); harder archetypes are needed to discriminate above that level.

---

## Repository layout

```
agent/                  Agent loop, tools, multi-vendor providers
harness/                Grading, workspace, patch apply, rollup
scripts/                Gate runners, cell executor, validators
archetype-a/            Archetype A (idempotent retry) — Phase 4 foundations
archetype-d-stale-read/ Stale-read topology, oracle, Phase 6 cell artifacts
```

Key scripts:

| Script | Purpose |
|--------|---------|
| `scripts/run_phase6_cell.py` | Run one model cell (k gate runs → oracle → rollup) |
| `scripts/run_stale_read_gate.py` | Single stale-read gate trajectory |
| `scripts/grade_stale_read_patch.py` | Tier-2 oracle grading for a run directory |
| `scripts/run_phase4_gate.py` | Phase 4 engineering exit gate |
| `scripts/generate_final_report_pdf.py` | Regenerate paper-style PDF from rollup data |

Phase 6 per-cell artifacts: `archetype-d-stale-read/phase6_cells/{model}/`  
Headline rollup: `archetype-d-stale-read/phase6_cells/phase6_headline_rollup.json`

---

## Setup

**Requirements:** Python 3.10+, Docker running.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` patterns into `.env` and set API keys for providers you plan to run:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...   # Gemini
```

On native Windows, full harness development is easiest under **WSL2** (see `requirements.txt` note). Phase 6 stale-read cells have been run on Windows with Docker Desktop.

---

## Running a Phase 6 cell

Frozen config is documented in `PRE_PHASE6.md` (local, gitignored). Example:

```powershell
.venv\Scripts\python.exe scripts\run_phase6_cell.py `
  --provider openai --model gpt-5.5 --k 5 `
  --log-dir archetype-d-stale-read\phase6_cells\gpt-5.5
```

Scoring consumes `oracle_grade.json` per run via `harness/rollup.py`. Resolution rate = `PASS / scored runs`.

---

## Methodological commitments

- **Oracle-only scoring** — classifier agreement reported separately, never used for rates
- **Frozen config** across cells — any pipeline change invalidates prior cells
- **No capability re-runs** — only infra / delivery-corruption failures are re-run eligible
- **Held-out models stated explicitly** — e.g. Gemini 3.1 Pro excluded for delivery non-comparability under the shared prompt
- **Per-cell rates for claims** — pooled cross-tier rates are not headline metrics

---

## Regenerating the final PDF

```powershell
.venv\Scripts\python.exe scripts\generate_final_report_pdf.py `
  --out archetype-d-stale-read\GordianBench_Final_Research_Report.pdf
```

Requires `matplotlib` (in `requirements.txt`).

---

## License

Add license text here if applicable.
