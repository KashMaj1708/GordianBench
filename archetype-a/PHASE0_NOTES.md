# Phase 0 Notes — Archetype A (Non-Idempotent Cascading Retry)

**Status: GREEN** — verified 2026-06-21

## Goal

One Archetype A stack that demonstrably exhibits the duplicate-charge bug on un-patched `src/`, and does not on the hand-patched negative control.

## Latency values chosen

| Knob | Value | Rationale |
|------|-------|-----------|
| `CLIENT_TIMEOUT_MS` (gateway → upstream) | **2000 ms** | Gateway gives up before upstream responds |
| `COMMIT_LATENCY_MS` (upstream sleep before Postgres insert) | **2200 ms** | Upstream commit completes *after* gateway timeout |
| Gateway `maxRetries` | **2** (3 total attempts) | Cascading retries amplify duplicate charges |

Gap: **200 ms**. This produced **100% reproduction** across 10 runs (well above the 80% threshold). No widening was required.

## Topology

```
curl → gateway:8080 → toxiproxy:8666 → upstream-mock:8081 → postgres
                              ↓
                     toxiproxy control :8474
```

Toxiproxy is wired but **no toxics active** in Phase 0 (transparent proxy only).

## Bug mechanism

1. Gateway POSTs `/charge` to upstream via toxiproxy with a **2.0 s client timeout**.
2. Upstream sleeps **2.2 s**, then inserts a ledger row.
3. Gateway times out and **retries** (no idempotency key).
4. Each retry spawns a concurrent upstream handler; all complete their sleep and insert.
5. Result: **3 ledger rows** for one payment (initial + 2 retries). Gateway returns HTTP 502 after exhausting retries.

## Broken src reproduction (10 runs)

Command:

```powershell
docker compose up -d --build
..\.venv\Scripts\python.exe scripts\probe.py --expect-double --runs 10
```

| Metric | Result |
|--------|--------|
| Runs | 10 |
| Double-charge hits (ledger ≥ 2) | **10 / 10** |
| Reproduction rate | **100%** |
| Ledger rows per payment | 3 (consistent) |
| Gateway HTTP status | 502 (all runs) |

**Verdict: PASS** (≥ 80% threshold met)

## Negative control — hand patch (10 runs)

Patch location: `patches/fixed/`

Changes:
- **Gateway** (`patches/fixed/gateway/main.go`): sends `Idempotency-Key: <payment_id>` header on every retry attempt.
- **Upstream** (`patches/fixed/upstream-mock/main.go`): fast-path dedup lookup before sleep; unique index on `idempotency_key`; returns cached result on duplicate.

Deploy:

```powershell
docker compose -f docker-compose.yml -f docker-compose.fixed.yml up -d --build
..\.venv\Scripts\python.exe scripts\probe.py --runs 10
```

| Metric | Result |
|--------|--------|
| Runs | 10 |
| Single-charge hits (ledger = 1, HTTP 200) | **10 / 10** |
| Double-charge hits | **0** |
| Success rate | **100%** |

**Verdict: PASS** — double charge eliminated on every run.

## Manual curl spot-check (broken src)

```powershell
curl -X POST http://localhost:8080/payment -H "Content-Type: application/json" -d "{\"payment_id\":\"manual-1\",\"amount\":100}"
docker compose exec postgres psql -U bench -d payments -c "SELECT * FROM ledger WHERE payment_id='manual-1';"
```

Observed: 3 ledger rows for `manual-1`.

## Exit criterion checklist

- [x] Bug reproduces reliably on un-patched `src/` (100% over 10 runs)
- [x] Double charge disappears on hand-patched src (0 duplicates over 10 runs)
- [x] Reproduction rate and latency values recorded
- [x] `docker-compose.yml` includes gateway + upstream-mock + postgres + toxiproxy

## Next phase

Phase 1: containerized Tier 1 regression test (`tier1_regression_test.py`) asserting HTTP 200 and exactly one ledger row.
