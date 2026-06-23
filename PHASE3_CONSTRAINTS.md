# Phase 3 Constraints — Lifecycle controller + grade()

**Prerequisite: Phase 2 GREEN** (corpus frozen 2026-06-21 — see `archetype-a/PHASE2_CONSTRAINTS.md`)

**Status: GREEN** — `grade()` smoke-validated; 20-cycle adversarial leak test passed 2026-06-22 (0 failures, ~40 min).

---

## 1. Tier 2 scope + append-only ledger (verified)

Two mechanisms: **chaos** (timing-luck band-aids) + **double-POST probe** (idempotency).

Peak trajectory is trace evidence only. Primary signal: **final ledger count after double-POST**.

### Append-only ledger — grep confirmed

| Path | Ledger mutation |
|------|-----------------|
| `src/upstream-mock`, `patches/fixed/upstream-mock` | `INSERT` only; fixed dedup is pre-insert `SELECT`, no UPDATE |
| `tests/helpers.py`, `scripts/probe.py` | `TRUNCATE` in test fixtures only (between tests, not mid-payment) |
| Gateway code | No SQL — ledger touched only via upstream |

**No `UPDATE` or `DELETE` on `ledger` during payment flow.** Fixed upstream never upserts; duplicate idempotency key returns existing row without mutation. Append-only holds → peak equals final; poll-rate concern evaporates. `TRUNCATE` is test-harness cleanup only.

Poll interval 250 ms; rows never disappear mid-test.

---

## 2. Drain floor — do not shrink for leak test speed

~29.4 s per drain, ~59 s per Tier 2 run (double-POST). The 20-cycle adversarial leak test will take **30+ minutes** with full Tier 1 + Tier 2. Budget for it; do not reduce `min_elapsed` to speed the test.

---

## 3. Resource hygiene (plan line 125)

| Resource | Check |
|----------|-------|
| Containers | No orphan `archetype-a` containers after teardown |
| Networks | No orphan `archetype-a` networks |
| Volumes | No orphan `archetype-a` volumes |
| **Toxics** | No active toxiproxy toxics on `upstream` |
| **Image freshness** | **Image ID changed after rebuild** when source changed — not tag-match |

Tag-match confirms you *requested* the right variant; it does **not** confirm the running container was rebuilt. Use `harness.hygiene.get_running_image_fingerprints()` + `assert_image_changed()` (docker image ID, content-addressed). Future: tag by patch hash.

---

## 4. 20-cycle leak test — adversarial variant sequence

**Not** 20× same variant. Cycle through variants so every step forces teardown of prior state:

```
broken → fixed → bandaid-timeout → fixed → bandaid-retry → broken → …
```

Script: `scripts/run_grade_leak_test.py` (`--dry-run` prints sequence; full run invokes `grade()` per step).

This catches: stale image after broken→fixed switch, leftover toxics after Tier 2→Tier 1.

---

## 5. SWE-bench WSL2 spike — parallel track (start now)

Unfalsified assumption: `get_eval_report` extends cleanly to k-shot stochastic verdict (plan lines 22, 116).

**Native Windows: blocked** (`resource` module). Run `scripts/spike_swebench.py` in **WSL2** before validating `grade()` on SWE-bench utilities.

**WSL2 setup (Debian PEP 668 — no system-wide pip):**

```bash
cd /mnt/c/Users/kashy/Desktop/GordianBench/GordianBench
sudo apt install python3.12-venv          # once; agent cannot enter sudo password
python3 -m venv .venv-wsl
source .venv-wsl/bin/activate
pip install swebench==4.1.0
python scripts/spike_swebench.py
```

Do **not** use `pip install --user` or `get-pip.py` on system Python — Debian marks it externally managed.

Preliminary finding (code read): `get_eval_report` is single-run log-parser based; k-shot / Tier 2 chaos requires a **wrapper**, not a plugin point. Reuse `docker_utils` + lifecycle; write k-shot loop ourselves.

---

## 6. Frozen band-aid corpus (verified)

| Band-aid | Tier 2 (double-POST, 2026-06-21) |
|----------|----------------------------------|
| timeout @ 5000ms | 0/10 pass |
| retry-reduction (`maxRetries=1`) | 0/10 pass |
| response-rewrite | 0/10 pass |
| fixed | 10/10 pass |

---

## Phase 3 exit checklist

- [x] Phase 2 corpus frozen
- [x] Ledger append-only confirmed (grep)
- [x] Adversarial leak-test sequence defined
- [x] Image ID freshness API in `harness/hygiene.py`
- [x] WSL2 spike executed (`scripts/spike_swebench.py`) — 2026-06-22, `.venv-wsl`, `get_eval_report` import OK
- [x] `grade()` validated with image-ID check on variant switch — broken→fixed, IDs differ, tags match
- [x] 20-cycle adversarial leak test, zero leaks — 2026-06-22, 20/20 cycles, scores correct, no hygiene leaks
- [x] grade(broken)=0.0, grade(fixed)=1.0, grade(band-aids)=0.0 — smoke validated 2026-06-21/22
