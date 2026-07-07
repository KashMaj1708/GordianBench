# Pre–Phase 5 Requirements

**Status:** Superseded — Phase 4 CLOSED (`PHASE4_CLOSE.md`); Phase 5 started (`PHASE5.md`)  
**Canonical record:** `archetype-a/phase_4_report_7.md` (complete) · `phase_4_report_8.md` (forced-tier1 replication addendum)  
**Prerequisite:** Phase 4 GREEN

---

## 1. Ephemeral agent workspace (mandatory) — DONE

Host `archetype-a/src/` is the version-controlled **corpus**. Agent `read_file` / `write_file` / `run_bash` cwd must never mutate it.

| Component | Change |
|-----------|--------|
| `harness/workspace.py` | `agent_workspace_session()` — LF-normalized copy under `.grade-workspaces/agent-*` |
| `harness/cleanup.py` | `teardown_agent_workspaces()` on `ensure_clean_state()` |
| `scripts/run_live_agent.py` | Uses ephemeral workspace + `assert_corpus_unchanged()` post-run |
| `scripts/validate_agent_trajectory.py` | Same |

`grade_patch()` already copied src ephemerally; this closes the agent-side gap that broke k-shot independence.

---

## 2. Multi-vendor model access (mandatory before Phase 6)

Phase 6 resolution rates require frontier-tier models, not Haiku-only.

### Setup

Add to `.env` (gitignored):

```env
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001   # or probe result

OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o

DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-chat

GEMINI_API_KEY=...   # or GOOGLE_API_KEY
GEMINI_MODEL=gemini-2.0-flash
```

### Probe

```powershell
pip install -r requirements.txt   # adds openai, google-genai
.venv\Scripts\python.exe scripts\probe_model_access.py
```

### Live run (any vendor)

```powershell
.venv\Scripts\python.exe scripts\run_live_agent.py --provider openai --model gpt-4o --grade
.venv\Scripts\python.exe scripts\run_live_agent.py --provider deepseek
.venv\Scripts\python.exe scripts\run_live_agent.py --provider gemini
```

Providers: `agent/provider/registry.py` (`anthropic`, `openai`, `deepseek`, `gemini`).

---

## 3. Live Haiku patch oracle result (informational)

```powershell
.venv\Scripts\python.exe scripts\grade_patch_tiers.py archetype-a/live_agent_runs/20260623T021118Z/model_patch.diff --tier 1  # 0.0
.venv\Scripts\python.exe scripts\grade_patch_tiers.py archetype-a/live_agent_runs/20260623T021118Z/model_patch.diff --tier 2  # 1.0
```

| Tier | Score | Meaning |
|------|-------|---------|
| 1 (calm) | **0.0** | HTTP 502 + 1 row — incomplete fix (timeout mismatch) |
| 2 (chaos) | **1.0** | Ledger invariant preserved under double-POST + chaos |
| Full `grade_patch` | **0.0** | True positive — tiers are consistent (see `ORACLE_SEMANTICS.md`) |

### Gemini 2.5 Pro (2026-06-23)

See `archetype-a/phase_4_report_7.md`.

### Frontier patch pipeline (2026-06-24)

See `archetype-a/phase_4_report_7.md` §5–§7.

| Change | Status |
|--------|--------|
| Auto-`patch_path` (workspace-first, `git apply` authoritative) | **Done** (`agent/loop.py`) |
| Git snapshot + `git diff` fallback in agent workspace | **Done** |
| Frontier applyable score | **GPT-4.1** `20260624T011323Z` — 1972 B, T1=0.0, T2=1.0 |

```powershell
.venv\Scripts\python.exe scripts/run_live_agent.py --provider openai --model gpt-4.1 --grade
```

---

## 4. Deferred to Phase 5/6 (validity)

| Issue | Decision needed |
|-------|-----------------|
| `run_bash` on host vs in-container | Affects investigation fidelity |
| Windows host shell vs Linux topology | Benchmark runs should target Linux/WSL2 for Phase 6 |
| In-container investigation tools (`dig`, `psql`, `docker logs`) | Executor redesign |

---

## 5. Phase 5 may proceed when

- [x] Ephemeral agent workspace
- [x] Multi-vendor provider seam + probe script
- [x] Oracle Tier 1 vs Tier 2 semantics documented (`ORACLE_SEMANTICS.md`)
- [x] Haiku patch graded per-tier (Tier 1: 0.0, Tier 2: 1.0)
- [x] Archetype D design constraints drafted (`ARCHETYPE_D_DESIGN.md`) — includes three-test legibility gate
- [ ] Operator runs `probe_model_access.py` with all vendor keys and records working model IDs
- [x] Gemini 2.5 Pro live canary attempted (pre-fix; re-run recommended)
- [x] GPT-4o live canary attempted — source fast-path confirmed (`phase_4_report_7.md` §7.3)
- [x] **Patch pipeline validated on frontier model** — GPT-4.1 (`phase_4_report_7.md` §7.4)
- [x] Auto-`patch_path` + git-diff fallback landed (`agent/loop.py`, `harness/workspace.py`)
- [x] Forced tier1 replication (4/4 ambiguous) — `phase_4_report_7.md` §7.6
- [x] Explicit HTTP 200 variant — `20260624T020908Z` (inconclusive grade; calm-path attempt)
- [x] Harness seam map (`PHASE5_HARNESS_SEAM.md`)
- [ ] Archetype D generalization (`grade_patch` path) — **Phase 5 work**
- [ ] Archetype D prototype + 3-vendor legibility gate — **before full D topology**
