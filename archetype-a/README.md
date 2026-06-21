# Archetype A — Phase 0 Manual Reproduction Guide

Archetype A demonstrates **Non-Idempotent Cascading Retry**: a Go payment gateway retries on client timeout while the upstream mock commits slowly to Postgres, producing duplicate ledger rows.

## Topology

```
Client → gateway:8080 → toxiproxy:8666 → upstream-mock:8081 → postgres:5432
                              ↓
                        toxiproxy API :8474
```

## Latency tuning (chosen values)

| Parameter | Value | Env var |
|-----------|-------|---------|
| Gateway client timeout | **2000 ms** | `CLIENT_TIMEOUT_MS` |
| Upstream commit latency | **2200 ms** | `COMMIT_LATENCY_MS` |
| Gateway max retries | **2** (3 total attempts) | hardcoded |

The 200 ms gap ensures the gateway times out before the upstream responds, triggering retries while each attempt still completes its commit.

## Quick start

```powershell
cd archetype-a
docker compose up --build
```

Postgres is exposed on **localhost:5433** (avoids conflict with a local Postgres on 5432).

### Fire a payment (broken src)

```powershell
curl -X POST http://localhost:8080/payment `
  -H "Content-Type: application/json" `
  -d '{"payment_id":"manual-test-1","amount":100}'
```

### Inspect the ledger

```powershell
docker compose exec postgres psql -U bench -d payments -c `
  "SELECT id, payment_id, amount, idempotency_key, created_at FROM ledger ORDER BY id;"
```

Expect **2–3 rows** for a single payment (cascading retries).

## Automated probe

```powershell
# Broken src — expect duplicate charges in ≥80% of runs
..\.venv\Scripts\python.exe scripts\probe.py --expect-double --runs 10

# Fixed patch (negative control) — expect exactly 1 ledger row every run
docker compose -f docker-compose.yml -f docker-compose.fixed.yml up -d --build
..\.venv\Scripts\python.exe scripts\probe.py --runs 10
```

## Hand patch (negative control)

The correct fix lives in `patches/fixed/`:

- **Gateway** sends `Idempotency-Key: <payment_id>` on every retry attempt.
- **Upstream** deduplicates by idempotency key (fast-path lookup before sleep, unique index on `idempotency_key`).

Deploy with:

```powershell
docker compose -f docker-compose.yml -f docker-compose.fixed.yml up -d --build
```

## Return to broken src

```powershell
docker compose down
docker compose up -d --build
```

## Phase 1 — Tier 1 regression gate

```powershell
# Host runner (stack must be up)
$env:GATEWAY_URL="http://localhost:8080"
$env:DATABASE_URL="postgresql://bench:bench@localhost:5433/payments"
..\.venv\Scripts\python.exe -m pytest tier1_regression_test.py -v

# In-container runner
docker compose -f docker-compose.yml -f docker-compose.tier1.yml run --rm tier1-runner

# Determinism gates (10 runs, zero flips)
..\.venv\Scripts\python.exe scripts\run_tier1_gate.py --runs 10 --host
..\.venv\Scripts\python.exe scripts\run_tier1_gate.py --runs 10 --expect-pass --host  # after fixed deploy
```

See `PHASE1_NOTES.md` for verification results and `instances/archetype-a.json` for SWE-bench F2P/P2P mapping.

## Phase 2 — Tier 2 chaos crucible

```powershell
$env:TOXIPROXY_URL="http://localhost:8474"

# Single run (stack must be up; fixed variant for pass)
docker compose -f docker-compose.yml -f docker-compose.fixed.yml up -d --build
..\.venv\Scripts\python.exe -m pytest tier2_chaos_test.py -v

# Determinism gates (10 runs, zero flips)
..\.venv\Scripts\python.exe scripts\run_tier2_gate.py --variant fixed --expect-pass --runs 10 --host
..\.venv\Scripts\python.exe scripts\run_tier2_gate.py --variant bandaid-timeout --runs 10 --host
```

See `PHASE2_NOTES.md` and `patches/BANDAID_CORPUS.md` for band-aid corpus and chaos profile.
