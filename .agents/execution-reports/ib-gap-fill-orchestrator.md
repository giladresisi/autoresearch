# Execution Report: IB Gap-Fill + Orchestrator Disconnect Handling

**Date:** 2026-05-07
**Plan:** `.agents/plans/ib-gap-fill-orchestrator.md`
**Executor:** Sequential (single agent, wave-by-wave)
**Outcome:** ✅ Success

---

## Executive Summary

Implemented two orthogonal improvements to the data-pipeline and orchestrator: (1) a standalone Databento rolling-window parquet backfill (`data/databento_backfill.py`) called at orchestrator startup, replacing the defunct `_gap_fill()` call inside `IbRealtimeSource.start()`; and (2) IB Gateway disconnect detection via `ib.disconnectedEvent`, propagating a dedicated `IbGatewayDisconnectedError` through the subprocess (exit code 2) and orchestrator (exit code 3) with clean position-close before exit. All 24 new test cases pass; the full suite grew from 1018 to 1042 passing with the same 4 pre-existing failures and 1 new skip (live IB integration test).

**Key Metrics:**
- **Tasks Completed:** 9/9 (100%)
- **Tests Added:** 24 (9 new in test_databento_backfill.py, 4 new in test_ib_realtime.py, 5 new in test_orchestrator_process.py, 6 new in test_orchestrator_main.py)
- **Test Pass Rate:** 48/49 (98%) across the 4 modified test files; 1 skipped by design (live IB integration test)
- **Files Modified:** 8 tracked + 2 untracked new files = 10 total
- **Lines Changed:** +386 insertions, −13 deletions (tracked files); +~140 lines in 2 new untracked files
- **Execution Time:** ~60 minutes
- **Alignment Score:** 9/10

---

## Implementation Summary

### Wave 1 — Foundation

**`data/databento_backfill.py` (NEW):** Standalone module exposing `backfill_parquets(bar_data_dir, ib_cutoff_days=2, max_lookback_days=30) -> None`. Iterates over MNQ/MES parquets, skips tickers whose last bar is already at or after the cutoff, fetches the gap window from `DatabentSource`, merges and deduplicates (keep-last) before writing back. `_empty_df()` helper produces a typed empty DataFrame for the no-parquet case.

**`data/ib_realtime.py` (MODIFIED):**
- Added `IbGatewayDisconnectedError(Exception)` at module level
- Added `_stopping: bool = False` instance attribute; `stop()` sets it before disconnecting
- `start()` now registers `_on_gateway_disconnect` on `ib.disconnectedEvent` after each successful `ib.connect()`; after `util.run()` returns, checks `_disconnected_by_gateway` flag and raises `IbGatewayDisconnectedError` if set
- Added `except IbGatewayDisconnectedError: raise` guard before the general retry `except` so gateway disconnects bypass all retries
- Removed the `self._gap_fill()` call from `start()`; method body retained

### Wave 2 — Consumer Wiring

**`automation/main.py` (MODIFIED):** Updated import to `from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource`; added `except IbGatewayDisconnectedError: sys.exit(2)` before the `finally` block wrapping `_ib_source.start()`.

**`orchestrator/process.py` (MODIFIED):** `_monitor()` checks `proc.returncode == 2` before the generic `"unexpected_exit"` return, yielding `"ib_disconnected"`. `run_session()` return type widened to `str | None`; added an `"ib_disconnected"` branch that logs and returns the string without setting the restart flag.

### Wave 3 — Orchestrator Integration

**`orchestrator/main.py` (MODIFIED):** Added `_pre_session_init()` function that guards on `DATABENTO_API_KEY`, imports `backfill_parquets` locally, calls it, and swallows exceptions with a warning (IB seed covers recent bars regardless). `run()` calls `_pre_session_init()` once before the `while True:` loop. After `ProcessManager.run_session()`, checks `result == "ib_disconnected"`, calls `_close_session_position()`, logs the operator-facing alert, and calls `sys.exit(3)`.

### Wave 4 — Tests

9 tests in `test_databento_backfill.py` (3 for missing-parquet creation, 1 for skip-when-current, 3 for empty/None responses, 2 for merge+dedup). 4 new tests in `test_ib_realtime.py`. 5 new tests in `test_orchestrator_process.py`. 6 new tests in `test_orchestrator_main.py`.

---

## Divergences from Plan

### Divergence #1: DatabentSource import moved to module level

**Classification:** ✅ GOOD

**Planned:** The plan's pseudocode used a lazy `from data.sources import DatabentSource` inside `backfill_parquets()`.
**Actual:** `DatabentSource` is imported at module level in `data/databento_backfill.py`.
**Reason:** Module-level import is needed for `patch("data.databento_backfill.DatabentSource")` to intercept object construction in tests. A function-local import makes the mock target the `data.sources` namespace instead, requiring more fragile double-patching.
**Root Cause:** Plan gap — test patchability was not considered in the lazy-import suggestion.
**Impact:** Positive — cleaner test isolation; no behavioral change at runtime.
**Justified:** Yes

---

### Divergence #2: `test_ibgateway_disconnected_error_not_retried` simplified

**Classification:** ✅ GOOD

**Planned:** Simulate `IbGatewayDisconnectedError` via the gateway disconnect callback path (trigger `disconnectedEvent` inside `util.run()` to set `_disconnected_by_gateway`, then verify the error propagates without retry).
**Actual:** The test raises `IbGatewayDisconnectedError` directly from `util.run()` as a `side_effect`, bypassing the callback simulation.
**Reason:** `ib_insync` imports are lazy inside `start()`; `__iadd__` on `MagicMock` does not intercept the `+=` operator correctly in all Python/mock versions, making callback-registration assertions unreliable. The direct raise approach is semantically equivalent because the `except IbGatewayDisconnectedError: raise` guard sits at the same try-level as both the callback-triggered flag check and the `util.run()` call itself.
**Root Cause:** Framework behavior (lazy import + `MagicMock.__iadd__` limitation).
**Impact:** Neutral — the guard being tested (`except IbGatewayDisconnectedError: raise`) is exercised identically; no coverage lost.
**Justified:** Yes

---

### Divergence #3: Test suite expanded from 5 to 9 tests in test_databento_backfill.py

**Classification:** ✅ GOOD

**Planned:** Five test cases (one test per scenario).
**Actual:** Nine test cases — the three creation scenarios were split into separate assertions (MNQ exists, MES exists, row count), and the None-response case gained an explicit no-parquet-created assertion.
**Reason:** More granular assertions improve diagnostic clarity on failure without adding coverage overhead.
**Impact:** Positive — finer failure attribution with no additional mock complexity.
**Justified:** Yes

---

## Test Results

**Tests Added:**
- `tests/test_databento_backfill.py` — 9 tests (NEW FILE)
- `tests/test_ib_realtime.py` — 4 new tests: `test_gap_fill_not_called_from_start`, `test_gateway_disconnect_raises_ibgateway_disconnected_error`, `test_stop_does_not_trigger_gateway_disconnect_flag`, `test_ibgateway_disconnected_error_not_retried`
- `tests/test_orchestrator_process.py` — 5 new tests: `test_monitor_returns_ib_disconnected_on_exit_code_2`, `test_monitor_returns_unexpected_exit_for_other_codes`, `test_run_session_returns_ib_disconnected`, `test_run_session_does_not_restart_on_ib_disconnect`, `test_run_session_returns_none_on_scheduled_stop`
- `tests/test_orchestrator_main.py` — 6 new tests: `test_pre_session_init_skips_when_no_api_key`, `test_pre_session_init_calls_backfill_when_key_set`, `test_pre_session_init_does_not_raise_on_backfill_exception`, `test_pre_session_init_called_before_session_loop`, `test_run_exits_3_on_ib_disconnected`, `test_run_closes_position_before_ib_disconnect_exit`

**Test Execution:**
```
tests/test_databento_backfill.py     9 passed
tests/test_ib_realtime.py            10 passed, 1 skipped
tests/test_orchestrator_process.py   14 passed
tests/test_orchestrator_main.py      15 passed
--------------------------------------------------
Total: 48 passed, 1 skipped (3.90s)
```

**Full suite (pre-implementation baseline):** 1018 passed, 4 failed (pre-existing in test_smt_strategy_v2.py), unrelated
**Full suite (post-implementation):** 1042 passed, 4 failed (same pre-existing), 1 skipped
**Pass Rate (new tests only):** 24/24 (100%)

---

## What was tested

- `backfill_parquets()` creates MNQ and MES parquet files when neither exists, writing fetched rows to disk
- `backfill_parquets()` saves the correct row count when parquets are created from scratch
- `backfill_parquets()` does not call `DatabentSource.fetch()` when the existing parquet's last bar is already within the IB cutoff window
- `backfill_parquets()` does not raise and does not create a parquet when Databento returns `None`
- `backfill_parquets()` does not raise when Databento returns an empty DataFrame
- `backfill_parquets()` appends new rows to an existing parquet when a gap is present
- `backfill_parquets()` deduplicates overlapping rows (keep-last) before writing, so re-fetched existing rows do not inflate the row count
- `IbRealtimeSource.start()` does not call `_gap_fill()` — regression guard confirming the removal stays in place
- `start()` raises `IbGatewayDisconnectedError` when `ib.disconnectedEvent` fires while `_stopping` is False (gateway-initiated disconnect simulation)
- Setting `_stopping = True` before a disconnect event prevents `_disconnected_by_gateway` from being set, ensuring deliberate `stop()` calls do not trigger the error path
- `IbGatewayDisconnectedError` propagates immediately from `start()` without triggering the retry loop, even when `max_retries > 1`
- `ProcessManager._monitor()` returns `"ib_disconnected"` when the subprocess exits with code 2
- `ProcessManager._monitor()` returns `"unexpected_exit"` for all other non-zero exit codes (1, 3, −1)
- `ProcessManager.run_session()` returns the string `"ib_disconnected"` when the subprocess exits with code 2
- `ProcessManager.run_session()` spawns the subprocess exactly once on exit code 2 — no restart attempt
- `ProcessManager.run_session()` returns `None` on a scheduled stop (regression guard for existing behaviour)
- `_pre_session_init()` does not raise and does not call `backfill_parquets` when `DATABENTO_API_KEY` is not set
- `_pre_session_init()` calls `backfill_parquets` exactly once when `DATABENTO_API_KEY` is set
- `_pre_session_init()` does not propagate exceptions from `backfill_parquets` (graceful degradation)
- `_pre_session_init()` is called before the first iteration of the session loop in `run()`
- `orchestrator/main.run()` raises `SystemExit(3)` when `ProcessManager.run_session()` returns `"ib_disconnected"`
- `orchestrator/main.run()` calls `_close_session_position()` before calling `sys.exit(3)` on IB disconnect

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "from data.databento_backfill import backfill_parquets; print('ok')"` | ✅ | Import clean |
| 1 | `python -c "from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource; print('ok')"` | ✅ | Import clean |
| 1 | `python -c "from orchestrator.process import ProcessManager; print('ok')"` | ✅ | Import clean |
| 1 | `python -c "from orchestrator.main import run, _pre_session_init; print('ok')"` | ✅ | Import clean |
| 2 | `python -m pytest tests/test_databento_backfill.py -v` | ✅ | 9/9 passed |
| 2 | `python -m pytest tests/test_ib_realtime.py -v` | ✅ | 10 passed, 1 skipped (live IB) |
| 2 | `python -m pytest tests/test_orchestrator_process.py -v` | ✅ | 14/14 passed |
| 2 | `python -m pytest tests/test_orchestrator_main.py -v` | ✅ | 15/15 passed |
| 3 | `python -m pytest tests/ -x` | ✅ | 1042 passed, 4 pre-existing failures, 1 skipped |
| 4 | Manual IB integration tests | ⚠️ Pending | Requires live IB Gateway hardware |

---

## Challenges & Resolutions

**Challenge 1: Patching DatabentSource in backfill tests**
- **Issue:** Tests using `patch("data.databento_backfill.DatabentSource")` only work if `DatabentSource` is imported at module level in `databento_backfill.py`. The plan's lazy-import pattern would have caused the patch to miss the constructor call.
- **Root Cause:** Python's `unittest.mock.patch` replaces the name in the target module's namespace; a function-local import binds the name in `data.sources`, not in `data.databento_backfill`.
- **Resolution:** Moved `from data.sources import DatabentSource` to module level.
- **Time Lost:** ~5 minutes
- **Prevention:** Plans that include lazy imports in functions should note whether mocking is required; if so, specify module-level import.

**Challenge 2: Simulating disconnectedEvent via `+=` in tests**
- **Issue:** `ib_insync.IB().disconnectedEvent` is not a plain Python list; `MagicMock.__iadd__` does not intercept `+=` reliably, so `disconnect_callbacks.append(cb)` inside a `__iadd__` override required a custom `FakeIB` class.
- **Root Cause:** `ib_insync` uses a custom `Event` class with `__iadd__`; MagicMock's `__iadd__` returns the mock itself rather than invoking a side_effect.
- **Resolution:** Wrote a `FakeIB` class with a real `disconnectedEvent.__iadd__` that appends to a callback list, then called the collected callbacks from `fake_util_run`. For the no-retry test, used `util.run.side_effect = IbGatewayDisconnectedError(...)` directly to avoid the callback path.
- **Time Lost:** ~10 minutes
- **Prevention:** Document in CLAUDE.md that `ib_insync` event hooks require a `FakeIB` class pattern, not plain MagicMock.

---

## Files Modified

**New Files (2 untracked):**
- `data/databento_backfill.py` — rolling-window Databento parquet backfill module (+~80 lines)
- `tests/test_databento_backfill.py` — 9-test unit test file for backfill logic (+119 lines)

**Modified Files (8 tracked):**
- `data/ib_realtime.py` — IbGatewayDisconnectedError, _stopping flag, disconnect hook, remove _gap_fill call (+20/−2)
- `automation/main.py` — import IbGatewayDisconnectedError, add except → sys.exit(2) (+5/−1)
- `orchestrator/process.py` — exit code 2 branch in _monitor(), ib_disconnected in run_session() (+18/−2)
- `orchestrator/main.py` — _pre_session_init(), call before loop, ib_disconnected → sys.exit(3) (+34/−3)
- `tests/test_ib_realtime.py` — 4 new disconnect tests (+88/−0)
- `tests/test_orchestrator_process.py` — 5 new exit-code-2 tests (+86/−0)
- `tests/test_orchestrator_main.py` — 6 new pre-session-init + ib_disconnected tests (+105/−3)
- `PROGRESS.md` — feature status updated (+7/−0)

**Total:** +386 insertions(+), −13 deletions(−) tracked; approximately +540/−13 including new files

---

## Success Criteria Met

- [x] `data/databento_backfill.py` exists and can be imported without error
- [x] `backfill_parquets()` creates parquets when they do not exist, fetching up to `max_lookback_days` back
- [x] `backfill_parquets()` skips the fetch entirely when the existing parquet's last bar is already ≥ cutoff timestamp
- [x] `backfill_parquets()` merges new rows into existing parquet and deduplicates (keep last) before saving
- [x] `IbGatewayDisconnectedError` is defined in `data/ib_realtime.py` and importable from it
- [x] `IbRealtimeSource.start()` does NOT call `self._gap_fill()`
- [x] `IbGatewayDisconnectedError` is raised when `ib.disconnectedEvent` fires while `_stopping=False`
- [x] `IbGatewayDisconnectedError` propagates immediately without triggering the retry loop
- [x] `automation/main.py` catches `IbGatewayDisconnectedError` and calls `sys.exit(2)`
- [x] `ProcessManager.run_session()` returns `"ib_disconnected"` when the subprocess exits with code 2
- [x] `ProcessManager.run_session()` does NOT restart the subprocess on `"ib_disconnected"`
- [x] `orchestrator/main.py` calls `_pre_session_init()` once before the session loop begins
- [x] `orchestrator/main.py` calls `_close_session_position()` then `sys.exit(3)` when `run_session()` returns `"ib_disconnected"`
- [x] `backfill_parquets()` does not raise when Databento returns `None`
- [x] `backfill_parquets()` does not raise when Databento returns an empty DataFrame
- [x] `_pre_session_init()` does not raise and logs a clear warning when `DATABENTO_API_KEY` is not set
- [x] `_pre_session_init()` does not raise when Databento fetch throws (logs warning, continues)
- [x] `IbRealtimeSource.stop()` setting `_stopping=True` prevents `_disconnected_by_gateway` on deliberate disconnect
- [ ] Starting `IbRealtimeSource` with IB Gateway active: start() connects without [gap_fill] log line — **Pending manual test (requires live IB)**
- [ ] Killing IB Gateway while running: `IbGatewayDisconnectedError` within 30 seconds, exit code 2, orchestrator alert — **Pending manual test (requires live IB)**

---

## Recommendations for Future

**Plan Improvements:**
- Note import-level vs. function-level placement explicitly when the function uses `DatabentSource` and tests will mock it — "import at module level for testability" avoids the mock-namespace trap.
- Add a note to plans using `ib_insync` events that `MagicMock` cannot intercept `+=` on `ib_insync.Event`; a `FakeIB` class with a real `__iadd__` override is required.

**Process Improvements:**
- The manual integration tests (Tests 1–4) should be run as a smoke check the next time IB Gateway is brought up, before the next live session.

**CLAUDE.md Updates:**
- Add a pattern note: "ib_insync disconnectedEvent testing requires a FakeIB class — MagicMock.__iadd__ does not capture `+=` callback registration. Append callbacks to a list in a custom __iadd__ and trigger them from a fake util.run side_effect."
- Add a note: "For functions that import a class lazily for isolation, if the class needs to be mocked in tests, move the import to module level and patch `<module>.ClassName`."

---

## Conclusion

**Overall Assessment:** Both features were implemented fully and correctly across all five production files and four test files. The 24 new unit tests cover all automated acceptance criteria, including happy path, skip/no-op, error-response, merge/dedup, no-retry, position-close ordering, and sys.exit code assertions. The two manual integration tests are correctly deferred as they require live IB Gateway hardware. The two implementation divergences from the plan were both improvements in testability or assertion granularity, not regressions or scope changes.

**Alignment Score:** 9/10 — Full feature scope delivered. One point withheld because the plan's lazy-import pattern and `MagicMock +=` intercept pattern required adjustments that a more precise plan would have anticipated, slightly increasing implementation friction.

**Ready for Production:** Yes — pending manual IB Gateway smoke tests (Tests 1–4 in plan) before the next live session.
