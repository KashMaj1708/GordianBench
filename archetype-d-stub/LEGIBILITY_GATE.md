# Phase 5 — Write-Skew Stub Legibility Gate

**Archetype:** `archetype-d-stub`  
**Design reference:** `archetype-a/ARCHETYPE_D_DESIGN.md` Tests 1–3

---

## Purpose

Before building full Archetype D topology, determine whether the write-skew bug is **source-legible** (models fix from `read_file` alone) or requires the **investigation chain** (concurrent load → observe bad state → reason backward).

If any of three vendors **source-fast-path** on Test 2, redesign the stub before proceeding.

---

## Stub topology

| Service | Port | Role |
|---------|------|------|
| `postgres` | 5434 | `pool-a` + `pool-b`, $50 each |
| `ledger-api` | 8082 | `POST /transfer` — pool reserve check + per-account debit in default isolation |

**Bug:** Two concurrent $15 transfers (one per pool account) both pass the combined reserve check under READ COMMITTED write-skew; total drops to $70 (reserve $80). No cross-row DB constraint — invariant is application-only (`poolReserveCents`).

**Oracle:** `tier1_regression_test.py` — 20 concurrent trials, fails reliably on broken (~83% skew rate in probe).

---

## Gate protocol

1. Deploy broken stack (or let harness `stack_session` manage it)
2. Run `scripts/run_d_legibility_gate.py` per vendor
3. Classify with `scripts/classify_legibility_gate.py <run_dir>`
4. **Also read trajectory text** for Test 2 (diagnosis from source before runtime proof)

### Verdict rules (automated classifier)

| Verdict | Meaning |
|---------|---------|
| **PASS** | Investigative `run_bash` before `submit_patch` |
| **FAIL** | Patch without investigative bash, or textbook isolation fix without observation |
| **INCONCLUSIVE** | No patch submitted |

### Test 2 (manual read of trajectory)

> Could a model that reads all source and knows Postgres isolation semantics identify the fix **without** observing a runtime violation?

---

## Results (2026-06-24)

| Vendor | Run ID | Classifier | Test 2 (source diagnosis) | Notes |
|--------|--------|------------|---------------------------|-------|
| Anthropic Haiku | `20260624T034723Z_anthropic` | INCONCLUSIVE | **FAIL** | Named "write-skew" after `read_file`; 5 bash attempts (Windows shell friction); 0-byte patch |
| Google Gemini 2.5 Pro | `20260624T040931Z_gemini` | INCONCLUSIVE | **FAIL** | Code review → write-skew + `SERIALIZABLE` before curl repro; 7 bash attempts; 0-byte patch |
| OpenAI GPT-4.1 | _pending_ | | | Retry: `PYTHONUNBUFFERED=1 python -u scripts/run_d_legibility_gate.py --provider openai --model gpt-4.1` |

### Gate status: **FAIL (Test 2)** — redesign required

Two of two completed vendors diagnosed write-skew from source alone. Classifier INCONCLUSIVE only because Windows host `run_bash` blocked patch submission — not because investigation was required for diagnosis.

**Next:** Redesign stub (split logic across services, obscure reserve semantics, remove per-transaction readability) before full Archetype D topology. See `ARCHETYPE_D_DESIGN.md`.

---

## Automated Test 2 (post-gate tooling — 2026-06-24)

The patch-keyed classifier (`classify_legibility_gate.py`) returns INCONCLUSIVE whenever
the shell blocks patch delivery, so the real Test-2 signal had to be read by hand. That does
not scale to the Phase 6 k-shot sweep. `scripts/classify_diagnosis_order.py` now detects the
signal structurally — **ordinal of first named root cause vs. ordinal of first SUCCESSFUL
runtime probe** — and reproduces the manual verdict automatically:

| Vendor | `verdict` | `test2` | Detail |
|--------|-----------|---------|--------|
| Haiku | `SOURCE_LEGIBLE` | **FAIL** | named "write-skew" at event 4 (right after `read_file`); 0/2 probes succeeded |
| Gemini | `SOURCE_LEGIBLE` | **FAIL** | named "write-skew" at event **1 (opening plan, before any `read_file`)**; first successful probe only at event 15 |

Output written to `legibility_gate_runs/<run>/legibility_order.json`.

```powershell
.venv\Scripts\python.exe scripts\classify_diagnosis_order.py archetype-d-stub/legibility_gate_runs --write
```

### Gate-harness contamination (fixed)

The gate's own system prompt in `run_d_legibility_gate.py` named the bug class
("…ledger API **(write-skew prototype)**"). Gemini naming write-skew in its opening plan
*before reading any source* is therefore partly prompt leakage, not pure source-legibility.
The prompt has been de-leaked (no bug-class name; explicit "diagnose it yourself" note) and
assistant-text capture raised from 500→4000 chars so the automated classifier is reliable at
Phase 6 scale. The FAIL verdict still holds (Haiku named it post-`read_file` with no prompt
dependency), but any future write-skew evidence must come from the de-leaked prompt.

---

## Commands

```powershell
cd C:\Users\kashy\Desktop\GordianBench\GordianBench

# Verify broken oracle fails
$env:API_URL="http://localhost:8082"
$env:DATABASE_URL="postgresql://bench:bench@localhost:5434/ledger"
$env:PYTHONPATH="archetype-d-stub"
.venv\Scripts\python.exe -m pytest archetype-d-stub/tier1_regression_test.py -q

# Single vendor
$env:PYTHONUNBUFFERED="1"
.venv\Scripts\python.exe -u scripts/run_d_legibility_gate.py --provider anthropic --model claude-haiku-4-5-20251001

# All three vendors
.venv\Scripts\python.exe -u scripts/run_d_legibility_gate.py --all-vendors
```
