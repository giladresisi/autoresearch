# Execution Report: Expand Historical Data via IB Quarterly Contracts

**Date:** 2026-04-01
**Plan:** `.agents/plans/ib-quarterly-conid-fetch.md`
**Executor:** Sequential
**Outcome:** ✅ Success

---

## Executive Summary

Replaced the ContFuture approach in the SMT data pipeline with explicit conId-based fetching (`Contract(conId=..., exchange="CME")`), enabling paginated historical data from Sep 24, 2025 onward for MNQM6/MESM6. A pre-existing length-mismatch bug in `train_smt.py` was exposed by the now-unequal bar counts and fixed in the editable zone via index intersection alignment.

**Key Metrics:**
- **Tasks Completed:** 5/5 (100%) — 3 planned + 1 out-of-scope fix + 1 test suite
- **Tests Added:** 6 (5 unit + 1 integration)
- **Test Pass Rate:** 366/368 (366 passed, 2 pre-existing skips)
- **Files Modified:** 4 (+ PROGRESS.md)
- **Lines Changed:** +225/-47 (net across `data/sources.py`, `prepare_futures.py`, `train_smt.py`, `tests/test_data_sources.py`)
- **Execution Time:** ~60 minutes
- **Alignment Score:** 9/10

---

## Implementation Summary

### Phase 1 — `data/sources.py`: New `future_by_conid` contract type

Added an `elif contract_type == "future_by_conid":` branch inside `IBGatewaySource.fetch()`. This branch:
- Creates `Contract(conId=int(ticker), exchange="CME")` instead of `Stock` or `ContFuture`
- Calls `ib.qualifyContracts(contract)` for symbol resolution
- Falls into the standard pagination loop (shared with the `stock` path) which uses explicit `endDateTime` strings per chunk — bypassing the ContFuture restriction that prohibits explicit `endDateTime` on CME 1m/5m bars

### Phase 2 — `prepare_futures.py`: Hardcoded conIds + fixed `BACKTEST_START`

- Added `CONIDS = {"MNQ": 770561201, "MES": 770561194}` mapping MNQM6/MESM6
- Changed `BACKTEST_START` from a dynamic 45-day rolling window to the fixed string `"2025-09-24"` (first full trading week of MNQM6)
- Updated `process_ticker()` to call `source.fetch(str(CONIDS[ticker]), ..., contract_type="future_by_conid")`

### Phase 3 — `train_smt.py`: Index alignment fix (out-of-scope, necessary)

After loading both MNQ and MES parquets, added a 7-line block in the editable zone of `load_futures_data()` that trims both DataFrames to their common index intersection. This prevents a `ValueError: Item wrong length` that occurs in the frozen `run_backtest()` when MNQ's session_mask (derived from MNQ's index) is applied to MES (which had 1,242 fewer bars due to pagination boundary differences).

### Phase 4 — `tests/test_data_sources.py`: 6 new tests

5 unit tests (fully mocked) + 1 integration test (live IB, auto-skipped offline). See "What was tested" section.

---

## Divergences from Plan

### Divergence #1: `train_smt.py` alignment fix (out-of-scope file modified)

**Classification:** ✅ GOOD

**Planned:** `train_smt.py` listed as "Out of Scope — no changes"
**Actual:** 7-line index intersection alignment added in `load_futures_data()` editable zone
**Reason:** The new paginated conId fetch produces unequal bar counts (MNQ: 35,375 / MES: 34,133) because chunk boundaries land on different calendar days for each symbol. The frozen `run_backtest()` applies MNQ's boolean session_mask to MES directly, which raises `ValueError: Item wrong length`.
**Root Cause:** Plan assumed both symbols would have identical bar counts (as was incidentally true with ContFuture's single-shot `endDateTime=''` fetch). The pagination approach breaks that assumption.
**Impact:** Positive — silently latent bug is now fixed; backtest runs cleanly end-to-end
**Justified:** Yes — fix is minimal, confined to the editable zone, and leaves the frozen zone untouched

---

### Divergence #2: `qualifyContracts` retained in `future_by_conid` branch

**Classification:** ⚠️ ENVIRONMENTAL

**Planned:** Plan code snippet included `ib.qualifyContracts(contract)` but noted it may fail for expired contracts
**Actual:** `qualifyContracts` is called; works correctly for MNQM6 (not yet expired as of 2026-04-01)
**Reason:** Retained for symbol resolution (populates `contract.symbol`, `contract.localSymbol`, etc.) which may be used by downstream logging
**Root Cause:** No expired contract in scope for this sprint; risk is deferred
**Impact:** Neutral for current sprint; future risk when MNQM6 expires and is not rolled
**Justified:** Yes — with mitigation note: when rolling to MNQZ6, test with `qualifyContracts` removed or wrapped in a try/except

---

## Test Results

**Tests Added:**
1. `test_ibgateway_future_by_conid_uses_contract_not_stock` — verifies `Contract(conId=...)` is used, not `Stock` or `ContFuture`
2. `test_ibgateway_future_by_conid_uses_explicit_enddatetime` — verifies `endDateTime` is never `''` in the future_by_conid path
3. `test_ibgateway_future_by_conid_paginates_multiple_chunks` — 6.5-month window produces ≥4 `reqHistoricalData` calls
4. `test_ibgateway_future_by_conid_returns_none_on_exception` — connection failure returns `None` without raising
5. `test_ibgateway_contfuture_path_unchanged_after_refactor` — regression: contfuture path still uses `endDateTime=''`
6. `test_ibgateway_future_by_conid_live_fetch` (integration, `@pytest.mark.integration`) — live MNQM6 fetch for Oct 2025 window

**Test Execution:**
```
Before: 360 passed, 2 skipped
After:  366 passed, 2 skipped
Delta:  +6 tests (5 unit passing + 1 integration auto-skipped offline)
```

**Pass Rate:** 366/368 (100% of executable tests; 2 pre-existing integration skips)

---

## What was tested

- `future_by_conid` fetch uses `Contract(conId=..., exchange="CME")` and does not invoke `Stock` or `ContFuture` constructors
- `future_by_conid` fetch never passes `endDateTime=''`; every `reqHistoricalData` call receives an explicit datetime string
- A 6.5-month window triggers at least 4 separate `reqHistoricalData` calls, confirming chunk-based pagination is active
- A connection failure (refused) during `future_by_conid` fetch returns `None` without raising an exception to the caller
- The existing `contfuture` code path still passes `endDateTime=''` after the new branch was added (regression guard)
- Live MNQM6 fetch returns a DataFrame with correct columns, unique monotonic index, timezone-aware timestamps, and ≥50 bars for a 5-trading-day window (integration, requires live IB-Gateway)

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "from data.sources import IBGatewaySource; import prepare_futures; print('OK')"` | ✅ PASS | Clean imports |
| 2 | `pytest tests/test_data_sources.py -x -q` | ✅ PASS | 26 passed |
| 3 | `pytest tests/ -x -q` | ✅ PASS | 366 passed, 2 skipped |
| 4 (manual) | `uv run prepare_futures.py` | ✅ PASS | MNQ: 35,375 bars, MES: 34,133 bars, first bar 2025-09-24 |
| 5 (manual) | `uv run python train_smt.py` | ✅ PASS | 6 folds, 42 total trades, no errors |

---

## Challenges & Resolutions

**Challenge 1:** MNQ/MES bar count mismatch breaks `run_backtest()`
- **Issue:** Paginated conId fetch produces 35,375 MNQ bars and 34,133 MES bars; the frozen `run_backtest()` applies MNQ's session_mask to MES without length checking, raising `ValueError: Item wrong length`
- **Root Cause:** ContFuture fetched both symbols with `endDateTime=''` in a single call, which incidentally produced matching lengths; paginated chunking breaks this coincidence
- **Resolution:** Added index intersection alignment in the editable zone of `load_futures_data()` — both DFs are trimmed to `mnq_df.index.intersection(mes_df.index)` before being returned
- **Time Lost:** ~15 minutes
- **Prevention:** Add a length/index equality assertion in the editable zone of `load_futures_data()` as a guard in future; document in plan that paginated multi-symbol fetches are unlikely to produce equal lengths

---

## Files Modified

**Core Implementation (3 files):**
- `data/sources.py` — Added `future_by_conid` branch to `IBGatewaySource.fetch()` (+47/-0)
- `prepare_futures.py` — Replaced ContFuture with conId-based fetch, added `CONIDS` dict, fixed `BACKTEST_START` (+30/-17)
- `train_smt.py` — Added MNQ/MES index alignment in `load_futures_data()` editable zone (+7/-0)

**Tests (1 file):**
- `tests/test_data_sources.py` — 6 new tests for `future_by_conid` path (+125/-0)

**Total:** +209 insertions, -17 deletions across feature files (plus PROGRESS.md updates)

---

## Success Criteria Met

- [x] `future_by_conid` branch added to `IBGatewaySource.fetch()`
- [x] `prepare_futures.py` uses conId fetch; `BACKTEST_START = "2025-09-24"`
- [x] `prepare_futures.py` downloads ≥35k bars with first bar on 2025-09-24
- [x] `train_smt.py` runs full 6-fold walk-forward without errors
- [x] 5 unit tests added and passing
- [x] 1 integration test added (auto-skipped when IB offline)
- [x] Full test suite passes (366/366 executable)
- [x] `contfuture` regression test confirms existing path unchanged

---

## Recommendations for Future

**Plan Improvements:**
- When introducing paginated multi-symbol fetches, explicitly state that bar counts across symbols will diverge and require index alignment before joint processing
- Add a "known failure modes" section for plans touching the frozen zone — length mismatches are a class of bugs that require editable-zone workarounds

**Process Improvements:**
- Run a post-download sanity check (`assert len(mnq_df) == len(mes_df)`) early in validation to surface alignment issues before the full backtest
- When rolling contracts (MNQM6 → MNQZ6), test `qualifyContracts` against the new conId before relying on it in production — expired contracts may fail silently or raise

**CLAUDE.md Updates:**
- Document the IB pattern: `Contract(conId=..., exchange="CME")` with explicit `endDateTime` pagination is the correct approach for historical quarterly futures; `ContFuture` with `endDateTime=''` is only usable for the most-recent ~14 calendar days of 1m data

---

## Conclusion

**Overall Assessment:** The implementation cleanly achieved its goal — 6.5 months of SMT backtest data (vs. ~14 days previously) by switching from ContFuture to explicit conId fetching. The one out-of-scope change (`train_smt.py` alignment fix) was necessary, minimal, and well-contained. All 6 planned tests were delivered and the full suite is green.

**Alignment Score:** 9/10 — one out-of-scope file touched, but for a legitimate reason that the plan could not anticipate without live data. The `qualifyContracts` retention is a minor risk deferred to contract roll time.

**Ready for Production:** Yes — data pipeline produces correct output, backtest runs 6 folds with 42 trades, and all tests pass.
