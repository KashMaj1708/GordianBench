# Pre–Phase 5 Requirements

**Status:** In progress (2026-06-23)  
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

`grade_patch()` on `live_agent_runs/20260623T021118Z/model_patch.diff` → **0.0**

Tier 1: HTTP 502 with 1 ledger row. Partial idempotency fix; not equivalent to corpus fix. First real-agent benchmark data point.

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
- [ ] Operator runs `probe_model_access.py` with all vendor keys and records working model IDs
- [ ] Archetype D generalization (`grade_patch` path)
