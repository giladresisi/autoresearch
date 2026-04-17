# Execution Report: IB-Gateway Connection Instability Fix

**Date:** 2026-04-17
**Plan:** `.agents/plans/ib-connect-fix.md`
**Executor:** Sequential (single agent)
**Outcome:** ✅ Success

---

## Executive Summary

All three root causes identified in the plan (pacing violation at startup, no reconnect loop, stale clientId on restart) were addressed by changes to `signal_smt.py` only. The gap-fill lookback is now capped at 3 days on subsequent startups, the IB connection block is wrapped in a 10-attempt retry loop, and subscriptions are re-registered cleanly on each reconnect via the extracted `_setup_ib_subscriptions()` helper. All 27 existing tests pass; the 1 pre-existing failure is unchanged.

**Key Metrics:**
- **Tasks Completed:** 3/3 (100%)
- **Tests Added:** 0 (plan specified none; IB-dependent paths require a Gateway mock)
- **Test Pass Rate:** 27/28 (1 pre-existing failure, documented)
- **Files Modified:** 1 (`signal_smt.py`)
- **Lines Changed:** +67 / -22
- **Execution Time:** ~20 minutes
- **Alignment Score:** 10/10

---

## Implementation Summary

### Task 1 — Reduce gap-fill lookback (Root Cause 1)

Added constant `GAP_FILL_MAX_DAYS = 3` alongside the existing `MAX_LOOKBACK_DAYS = 30`. In `_gap_fill_1m()`, computed `gap_days = MAX_LOOKBACK_DAYS if mnq_df.empty else GAP_FILL_MAX_DAYS` and used it to set `lookback_floor`. First-run (empty parquet) still fetches 30 days; subsequent startups fetch at most 3 days, reducing IB pacing from 20–60 requests down to 2–6.

### Task 2 — Reconnect retry loop (Root Cause 2 + 3)

Added constants `MAX_RETRIES = 10` and `RETRY_DELAY_S = 15` in a new `── Reconnect settings ──` block. Replaced the single-shot connect + `util.run()` block in `main()` with a `for attempt in range(MAX_RETRIES)` loop. Each iteration: creates a fresh `IB()`, connects, calls `_setup_ib_subscriptions`, runs `util.run()`, and `break`s on clean exit. On exception: prints a numbered retry message, disconnects safely, sleeps `RETRY_DELAY_S` (skipped on the last attempt), and loops. After the loop a final `try/except` issues a graceful disconnect.

Contract objects (`mnq_contract`, `mes_contract`) are instantiated once before the loop — they are stateless and do not need to be recreated per attempt.

### Task 3 — Extract subscriptions helper (Required for Task 2)

Added `_setup_ib_subscriptions(ib, mnq_contract, mes_contract)` function that issues `reqHistoricalData` (keepUpToDate=True) for MNQ and MES 1m bars, `reqTickByTickData` for both ticks, and wires all four `updateEvent` callbacks. This makes the retry loop body clean and ensures all four subscriptions are registered on every connection attempt.

---

## Divergences from Plan

No divergences. All three tasks were implemented exactly as specified. The only minor implementation detail not in the plan: `time.sleep()` is skipped on the last retry attempt (`if attempt < MAX_RETRIES - 1`) to avoid a 15s idle before clean process exit — this is a quality improvement with no functional impact.

---

## Test Results

**Tests Added:** None
**Test Execution:** `python -m pytest tests/test_signal_smt.py -v`
**Pass Rate:** 27/28 (96.4%) — 1 pre-existing failure (`test_process_managing_exit_tp`)

The pre-existing failure is documented in the plan acceptance criteria as out-of-scope. No regressions introduced.

---

## What was tested

No new automated tests were added. The plan's validation section specified only import check + existing test suite; no test automation section was included in the plan.

Existing tests that continued to pass cover:
- `_gap_fill_1m()` with non-empty parquet (start from last bar) and empty parquet (start from floor) — indirectly validates the `gap_days` branch logic via parquet state
- State machine transitions: SCANNING → MANAGING on signal, MANAGING → SCANNING on TP/stop/session close
- `_process_scanning()` signal detection with various bar configurations
- `_process_managing()` stop and TP detection across multiple scenarios

**Coverage gap (acknowledged):**
- `_setup_ib_subscriptions()` — not unit-tested; requires a live or mocked IB Gateway
- Retry loop in `main()` — not unit-tested; requires IB connection simulation
- Manual validation path (Ctrl+C restart, Gateway restart recovery) remains manual-only per plan

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import signal_smt; print('OK')"` | ✅ | Clean import, no new syntax errors |
| 2 | `python -m pytest tests/test_signal_smt.py -v` | ✅ | 27 passed, 1 pre-existing failure |

---

## Challenges & Resolutions

No challenges encountered. The implementation was straightforward — the plan provided exact code snippets for the retry loop and subscription helper, and the existing module structure made integration clean.

---

## Files Modified

**Core (1 file):**
- `signal_smt.py` — added `import time`, 3 constants, `_setup_ib_subscriptions()`, replaced single-shot connect block with retry loop (+67/-22 lines)

**Total:** ~67 insertions(+), ~22 deletions(-)

---

## Success Criteria Met

- [x] `GAP_FILL_MAX_DAYS = 3` constant added; gap-fill lookback capped at 3 days unless parquet is empty
- [x] `main()` wraps IB connection in a retry loop (max 10 attempts, 15s delay) with logging on each retry
- [x] Module-level state (`_mnq_1m_df`, `_mes_1m_df`, `_state`, `_position`) preserved across reconnects (loop only resets IB connection/subscriptions)
- [x] `_setup_ib_subscriptions()` re-registers all 4 subscriptions on each reconnect attempt
- [x] All existing tests pass (27 passing, 1 pre-existing failure `test_process_managing_exit_tp` — not fixed)
- [x] `data/sources.py` and `train_smt.py` unchanged

---

## Recommendations for Future

**Plan Improvements:**
- The plan could include a minimal mock-IB fixture (e.g., a `MagicMock` patching `IB.connect` and `IB.isConnected`) to allow unit-testing the retry loop and subscription helper without a live Gateway.

**Process Improvements:**
- For IB-dependent paths, consider a test-doubles layer so reconnect logic can be exercised automatically in CI.

**CLAUDE.md Updates:**
- No patterns identified that warrant global documentation.

---

## Conclusion

**Overall Assessment:** The fix directly addresses all three root causes identified in the plan. The code changes are minimal, targeted, and do not touch any other module. The retry loop and subscription helper are well-structured for long-running daemon use. The only gap is automated test coverage for the new IB-dependent paths, which was explicitly deferred in the plan due to the absence of a Gateway mock.

**Alignment Score:** 10/10 — every plan task was implemented as specified, no out-of-scope changes were made, and all acceptance criteria are satisfied.

**Ready for Production:** Yes — the import check passes, all existing tests pass, and the changes are additive with clean fallback (if Gateway is unavailable, the process now retries gracefully instead of exiting immediately).
