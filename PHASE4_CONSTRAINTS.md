# Phase 4 Constraints — Agent loop + dynamic patch grading

**Prerequisite: Phase 3 GREEN** (see `archetype-a/phase_3_report.md`)

**Status: GREEN** — engineering gates and live Anthropic run complete (2026-06-23).

---

## Exit discipline

| Layer | Meaning | Status |
|-------|---------|--------|
| **Engineering** | Bridge, mocks, tool-faithfulness, crash recovery, scripted trajectory | GREEN |
| **Plan exit** | Real model completes full trajectory without harness errors | **GREEN** `run_live_agent.py` (2026-06-23) |

`validate_agent_trajectory.py` (ScriptedProvider) proves loop plumbing only. Live run used `claude-haiku-4-5-20251001` (account-available model).

---

## Sign-off gate (run once at phase boundary)

```powershell
# Full bridge (do not --skip-bridge at sign-off)
.venv\Scripts\python.exe scripts\validate_patch_bridge.py

# Real model canary (~$0.50–1.00, requires .env ANTHROPIC_API_KEY)
.venv\Scripts\python.exe scripts\run_live_agent.py --max-turns 15
```

Fast iteration gate (not sign-off): `scripts/run_phase4_gate.py --skip-bridge --skip-live`

---

## Phase 4 exit checklist

- [x] Bridge test: `grade_patch(fixed diff)` = 1.0
- [x] Mock-correct / mock-bandaid agents
- [x] Tool-faithfulness
- [x] Crash-safety (`validate_crash_recovery.py`)
- [x] Scripted multi-turn trajectory (plumbing only)
- [x] **Real Anthropic model** — `run_live_agent.py` (plan line 146, 2026-06-23)

---

## Phase 5/6 design input: executor runs on host

`ToolExecutor.run_bash` executes on the **host**, not inside gateway/upstream containers. For Archetype A, mapped ports (`localhost:8080`, `5433`) make this survivable. By Phase 6, decide deliberately: agent investigates from **inside** the task container vs **host**, and ensure bugs are observable from that vantage. Otherwise resolution rates may reflect wrong-machine probes, not task difficulty.

---

## Patch sanitization

`agent/patch_util.sanitize_model_patch()` strips markdown fences before `git apply`. Applied in loop, executor, and `grade_patch()`.
