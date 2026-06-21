# Phase 3 Constraints — Lifecycle controller + grade()

**Prerequisite: Phase 2 GREEN** (corpus frozen 2026-06-21 — see `PHASE2_CONSTRAINTS.md` final matrix)

**Status: SCAFFOLD ONLY** — `harness/*.py` exists but `grade()` is **not validated**. Do not run 20-cycle leak tests or wire SWE-bench until corpus sign-off is explicit.

---

## 1. Tier 2 scope (reconciled with plan line 9)

Two mechanisms, two jobs:

1. **Chaos** — saturates timeouts; catches timing-luck band-aids under retry storms
2. **Double-POST probe** — adversarial idempotency test; closes single-POST false-pass hole

Tier 2 validates **idempotency correctness via ledger invariants + duplicate client POST**, under chaos that saturates client timeouts. Not an HTTP success oracle. Not purely "chaos resurfaces the bug" — the probe is deliberate and necessary.

Peak trajectory is **trace evidence** (append-only ledger; poll interval 250 ms ≪ commit latency 2200 ms). Primary fail signal: **final ledger count after double-POST**.

---

## 2. Drain floor — correctness vs throughput

`min_elapsed` ≈ **29.4 s** per drain (~59 s/Tier 2 run with double-POST). Rows settle in ~0.3 s; floor is wall-clock guard.

- Robustness: trace counters (`near_miss`, `late_insert`, RTT variance), not zero flips alone
- Phase 6 cost: flag now; handler-completion signal deferred

---

## 3. Resource hygiene (plan line 125)

After each `grade()` cycle, assert zero leaks:

| Resource | Check |
|----------|-------|
| Containers | No orphan `archetype-a` containers |
| Networks | No orphan `archetype-a` networks |
| Volumes | No orphan `archetype-a` volumes |
| **Toxics** | No active toxiproxy toxics on `upstream` |
| **Image freshness** | Deployed variant tag matches overlay |

---

## 4. Environmental state discipline

`grade()` must: deploy explicit variant → clear toxics → Tier 1 → enable toxics → Tier 2 → clear toxics → teardown.

---

## 5. Frozen band-aid corpus (verified)

| Band-aid | Tier 2 gate (double-POST, 2026-06-21) |
|----------|----------------------------------------|
| timeout @ 5000ms | 0/10 pass |
| retry-reduction (`maxRetries=1`) | 0/10 pass |
| response-rewrite | 0/10 pass |
| fixed | 10/10 pass |

---

## Phase 3 exit checklist

- [x] Phase 2 corpus frozen (prerequisite)
- [ ] `harness/lifecycle.py` — validated deploy/teardown
- [ ] `harness/grade.py` — validated binary score
- [ ] `harness/hygiene.py` — validated leak checks
- [ ] grade(broken)=0.0, grade(fixed)=1.0, grade(band-aids)=0.0
- [ ] 20 consecutive grade cycles, zero leaks
- [ ] SWE-bench WSL2 spike (parallel track)
