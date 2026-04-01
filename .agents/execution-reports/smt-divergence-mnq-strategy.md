# Execution Report: SMT Divergence MNQ Strategy

**Date:** 2026-03-31
**Plan:** `.agents/plans/smt-divergence-mnq-strategy.md`
**Executor:** subagent-driven-development (parallel wave execution)
**Outcome:** ⚠️ Partial

---

## Executive Summary

The SMT divergence strategy for MNQ futures was largely implemented: the data layer extension (`ContFuture` support), `prepare_futures.py`, all 5 strategy functions in `train_smt.py`, the backtest harness, conftest bootstrap, and `program_smt.md` were all delivered. However, `tests/test_smt_backtest.py` was left as an import-only stub — none of the 10 planned integration tests were written. The execution summary overstated test results, claiming "36 tests all pass" when the integration test file has 0 test functions and only 24 unit tests were actually collected.

**Key Metrics:**
- **Tasks Completed:** 7/8 (88%) — Task 4.1 incomplete (stub file, no tests)
- **Tests Added:** 26 (24 unit in test_smt_strategy.py + 2 data source in test_data_sources.py; 0 integration)
- **Test Pass Rate:** 24/24 unit tests pass (100%); 0/10 integration tests exist
- **Files Modified:** 3 (data/sources.py, tests/conftest.py, tests/test_data_sources.py)
- **Files Created:** 5 (prepare_futures.py, train_smt.py, tests/test_smt_strategy.py, tests/test_smt_backtest.py stub, program_smt.md)
- **Lines Changed:** ~471 modified + ~1150 created
- **Alignment Score:** 7/10

---

## Implementation Summary

### Wave 1 — Data Foundation
- `data/sources.py`: `IBGatewaySource.fetch()` extended with `contract_type: str = "stock"` param; `ContFuture` imported from `ib_insync`; branch logic selects `ContFuture("ticker", "CME", "USD")` with `useRTH=False` for futures vs `Stock("ticker", "SMART", "USD")` with `useRTH=True` for stocks.
- `prepare_futures.py` (89 lines): Downloads MNQ/MES 1m bars; `ThreadPoolExecutor(max_workers=2)`, `process_ticker()`, `write_manifest()` writing `futures_manifest.json`. Mirrors `prepare.py` config pattern exactly.

### Wave 2 — Strategy Core
- `train_smt.py` editable section: Constants block (SESSION_START/END, WALK_FORWARD_WINDOWS=6, FOLD_TEST_DAYS=60, RISK_PER_TRADE=50, MNQ_PNL_PER_POINT=2.0); all 5 strategy functions fully implemented.
- `tests/test_smt_strategy.py` (462 lines): 24 unit tests covering all 5 functions with synthetic 1m DataFrames, no IB connection.

### Wave 3 — Harness
- `train_smt.py` harness section: `_load_futures_manifest()`, `load_futures_data()`, `run_backtest()` (full intraday bar-by-bar loop), `_compute_metrics()` (Sharpe, max drawdown, Calmar, win rate, avg R:R, exit type breakdown), walk-forward fold loop, `__main__` print block with `fold{i}_train_*` keys.

### Wave 4 — Integration
- `tests/conftest.py`: Futures manifest bootstrap added to `pytest_configure()` — creates `futures_manifest.json` stub if absent.
- `program_smt.md`: Optimization agent instructions mirroring `program.md` for SMT strategy.
- `tests/test_smt_backtest.py`: **INCOMPLETE** — file contains only module docstring and imports (13 lines). No test functions were written.

---

## Divergences from Plan

### Divergence #1: Integration test file is a stub

**Classification:** ❌ BAD

**Planned:** 10 integration tests in `tests/test_smt_backtest.py` covering: empty data, long TP hit, short stop hit, session force-exit, end-of-backtest exit, PnL long/short correctness, one-trade-per-day, fold loop smoke, metrics shape.
**Actual:** File contains only module docstring and 7 import lines. Zero test functions collected.
**Reason:** Executor apparently created the file skeleton but did not fill in any test bodies. The execution summary incorrectly claimed "10 integration tests pass."
**Root Cause:** Likely a wave 4 agent task that was started but not completed, with the summary generated before verification.
**Impact:** Negative — `run_backtest()` has zero automated test coverage. The harness could have regressions introduced during optimization with no safety net.
**Justified:** No

### Divergence #2: Execution summary inaccurate test count

**Classification:** ❌ BAD

**Planned:** Execution summary should reflect actual verified results.
**Actual:** Summary stated "36 tests all pass" and "Level 3 (integration): 10 passed." Actual: 24 unit tests pass; integration test file is empty.
**Reason:** The reporting agent did not run `pytest --collect-only` before writing the summary.
**Root Cause:** Trust placed in agent self-report without verification against collected test IDs.
**Impact:** Misleading — creates false confidence in backtest harness coverage.
**Justified:** No

### Divergence #3: `_test_smt_backtest_part1.txt` artifact left in tests/

**Classification:** ⚠️ ENVIRONMENTAL

**Planned:** No scratch files in tests/.
**Actual:** `tests/_test_smt_backtest_part1.txt` (1207 bytes) left as an untracked file — appears to be an intermediate agent artifact from a partial attempt at writing test_smt_backtest.py.
**Reason:** An agent wave wrote a partial draft to a .txt file and did not clean it up.
**Root Cause:** Wave coordination gap — agent wrote to wrong filename and did not delete.
**Impact:** Minor — untracked file, not indexed, doesn't affect test runs. Should be deleted.
**Justified:** No — needs cleanup.

---

## Test Results

**Tests Added:**
- `tests/test_smt_strategy.py` — 24 unit tests for detect_smt_divergence, find_entry_bar, compute_tdo, screen_session, manage_position
- `tests/test_data_sources.py` — 2 ContFuture tests (test_ibgateway_contfuture_uses_correct_contract, test_ibgateway_stock_contract_unchanged)
- `tests/test_smt_backtest.py` — 0 tests (stub only, planned 10)

**Test Execution:**
```
tests/test_smt_strategy.py: 24 passed in 0.19s
tests/test_smt_backtest.py: 0 collected (stub)
Full suite: 336 passed, 8 skipped, 3 pre-existing failures (unchanged)
```

**Pass Rate:** 24/24 unit tests (100%); 0/10 integration tests (file is a stub)

---

## What was tested

- `detect_smt_divergence` returns "short" when MES makes a new session high but MNQ does not.
- `detect_smt_divergence` returns "long" when MES makes a new session low but MNQ does not.
- `detect_smt_divergence` returns None when both instruments confirm the same new extreme.
- `detect_smt_divergence` is suppressed when fewer than MIN_BARS_BEFORE_SIGNAL bars have elapsed since session start.
- `find_entry_bar` returns the index of the first bearish confirmation candle whose high wicks past the prior bull body close for short setups.
- `find_entry_bar` returns the index of the first bullish confirmation candle whose low wicks past the prior bear body close for long setups.
- `find_entry_bar` returns None when no valid confirmation candle exists before session_end_idx.
- `find_entry_bar` returns None when a directional candle is present but the wick does not pierce the opposite body.
- `compute_tdo` returns the open of the 9:30 AM bar when it exists.
- `compute_tdo` falls back to the first available bar's open when no 9:30 AM bar is present.
- `compute_tdo` returns None when no bars for the requested date exist.
- Stop placement for short positions equals entry + 0.45 × distance to TDO.
- Stop placement for long positions equals entry − 0.45 × distance to TDO.
- Take-profit equals TDO in signal dicts produced by screen_session.
- R:R ratio equals distance_to_TP / distance_to_stop ≈ 1/0.45 ≈ 2.22.
- `screen_session` produces a signal dict with direction="short", entry_price, stop_price, take_profit for a bearish SMT divergence with confirmation.
- `screen_session` produces a signal dict with direction="long" for a bullish SMT setup.
- `screen_session` returns None when MES and MNQ move in lockstep with no divergence.
- `manage_position` returns "exit_tp" for a long when bar high >= take_profit.
- `manage_position` returns "exit_stop" for a long when bar low <= stop_price.
- `manage_position` returns "exit_tp" for a short when bar low <= take_profit.
- `manage_position` returns "exit_stop" for a short when bar high >= stop_price.
- `manage_position` returns "hold" when neither TP nor stop is touched.
- `IBGatewaySource.fetch` instantiates ContFuture("MNQ", "CME", "USD") when contract_type="contfuture" and does not call Stock.
- `IBGatewaySource.fetch` continues to use Stock when contract_type defaults to "stock".

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "from data.sources import IBGatewaySource; import prepare_futures; import train_smt; print('ok')"` | ✅ | All imports clean |
| 2 | `pytest tests/test_smt_strategy.py tests/test_data_sources.py -v` | ✅ | 24 unit + 2 data source pass (26 total) |
| 3 | `pytest tests/test_smt_backtest.py -v` | ❌ | 0 tests collected — file is a stub |
| 4 | `pytest tests/ -x` | ✅ | 336 passed, 3 pre-existing failures unchanged |

---

## Challenges & Resolutions

**Challenge 1:** Integration test file was reported as complete but is actually empty
- **Issue:** `tests/test_smt_backtest.py` contains only imports — no test functions.
- **Root Cause:** Wave 4 task 4.1 was either not executed or the agent failed partway through without surfacing the failure. An intermediate artifact (`_test_smt_backtest_part1.txt`) was left in tests/, suggesting an aborted attempt.
- **Resolution:** NOT yet resolved — 10 integration tests still need to be written.
- **Time Lost:** Discovered at report stage only.
- **Prevention:** Execution checkpoint after Wave 4 should include `pytest --collect-only tests/test_smt_backtest.py | grep "test session"` to confirm count before declaring done.

---

## Files Modified

**Modified (3 files):**
- `data/sources.py` — ContFuture support added to IBGatewaySource.fetch() (+194/-0)
- `tests/conftest.py` — futures_manifest.json bootstrap in pytest_configure() (+33/-0)
- `tests/test_data_sources.py` — 2 ContFuture tests added (+244/-0)

**Created (5 files):**
- `prepare_futures.py` — MNQ/MES 1m bar downloader (89 lines)
- `train_smt.py` — SMT strategy + harness (615 lines)
- `tests/test_smt_strategy.py` — 24 unit tests (462 lines)
- `tests/test_smt_backtest.py` — STUB ONLY (13 lines, 0 tests)
- `program_smt.md` — optimization agent instructions

**Artifact to delete:**
- `tests/_test_smt_backtest_part1.txt` — scratch file, 1207 bytes, should be removed

---

## Success Criteria Met

- [x] `IBGatewaySource.fetch()` accepts `contract_type="contfuture"` and uses `ContFuture("MNQ"/"MES", "CME", "USD")` with `useRTH=False`
- [x] `prepare_futures.py` imports without error and contains all config constants
- [x] `train_smt.py` imports without error; `# DO NOT EDIT BELOW THIS LINE` boundary present
- [x] All 5 strategy functions implemented: `detect_smt_divergence`, `find_entry_bar`, `compute_tdo`, `screen_session`, `manage_position`
- [x] All 24 unit tests in `test_smt_strategy.py` pass
- [ ] All 10 integration tests in `test_smt_backtest.py` pass — **MISSING: file is a stub**
- [x] 2 new data source tests pass
- [x] Full test suite passes with 0 new failures
- [x] GOLDEN_HASH in `test_optimization.py` unchanged
- [x] `program_smt.md` exists and mirrors `program.md` structure
- [x] `conftest.py` bootstraps `futures_manifest.json` alongside equity manifest

---

## Recommendations for Future

**Plan Improvements:**
- Add explicit per-task completion verification step: after each wave, run `pytest --collect-only <test_file> | grep "<N> tests"` and assert the count before proceeding.
- Wave 4 task 4.1 should have a blocking checkpoint: "confirm 10 tests collected from test_smt_backtest.py" before declaring wave complete.

**Process Improvements:**
- Execution summaries must be generated from actual `pytest --collect-only` output, not from agent self-report.
- Scratch/draft files (e.g., `_test_smt_backtest_part1.txt`) must be deleted before the wave is marked complete.

**CLAUDE.md Updates:**
- Add to "Testing" section: "Before marking any task complete, confirm test count with `pytest --collect-only <file> -q` and verify it matches the plan's expected count."

---

## Conclusion

**Overall Assessment:** The core deliverable — a functional SMT divergence strategy for MNQ futures — is fully implemented and covered by 24 passing unit tests. The data layer extension (`ContFuture`), `prepare_futures.py`, and the backtest harness are all in good shape. The gap is Task 4.1: the integration test file is an empty stub, leaving `run_backtest()` with zero automated test coverage. This is a material gap — the harness is the most complex component and the one most likely to drift during optimization runs. The `_test_smt_backtest_part1.txt` scratch artifact also needs cleanup. The 10 integration tests from the plan must be written before this feature is considered complete.

**Alignment Score:** 7/10 — strong implementation of strategy logic and harness; fails on integration test delivery and accurate reporting.
**Ready for Production:** No — integration tests must be written first. Manual IB data download test also pending.
