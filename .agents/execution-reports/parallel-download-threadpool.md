# Execution Report: Parallel Download Thread Pool

**Date:** 2026-03-25
**Plan:** `.agents/plans/parallel-download-threadpool.md`
**Executor:** Sequential (2-wave)
**Outcome:** ✅ Success

---

## Executive Summary

Replaced the sequential per-ticker `for` loops in both `screener_prepare.py` and `prepare.py` with `ThreadPoolExecutor`-based parallel execution, targeting ~10× throughput improvement for I/O-bound yfinance HTTP calls. All 9 planned tests were implemented and pass; the full suite of 282 tests passes with the single pre-existing failure (`test_selector.py::test_select_strategy_real_claude_code`) unchanged.

**Key Metrics:**
- **Tasks Completed:** 4/4 (100%)
- **Tests Added:** 9
- **Test Pass Rate:** 38/38 (100%) in targeted files; 282/283 full suite (pre-existing failure excluded from scope)
- **Files Modified:** 5 (screener_prepare.py, prepare.py, tests/test_screener_prepare.py, tests/test_prepare.py, PROGRESS.md)
- **Lines Changed:** +219/-26
- **Execution Time:** ~15 minutes
- **Alignment Score:** 10/10

---

## Implementation Summary

### Wave 1 — Implementation

**Task 1.1 — `screener_prepare.py`:**
- Added `import threading` and `from concurrent.futures import ThreadPoolExecutor, as_completed` to imports.
- Added `MAX_WORKERS = int(os.environ.get("SCREENER_PREPARE_WORKERS", "10"))` constant after `HISTORY_DAYS`.
- Extracted `_process_one(ticker, history_start) -> tuple` as a module-level helper. The function calls `is_ticker_current()` to short-circuit, then delegates to `download_and_cache()`, reads back the parquet to verify, and returns `(ticker, status_string)` — never raises.
- Replaced the 17-line sequential `for` loop in `__main__` with a `ThreadPoolExecutor(max_workers=MAX_WORKERS)` + `as_completed` pattern; progress counter `done` is incremented only in the main thread so no lock is needed.

**Task 1.2 — `prepare.py`:**
- Added `from concurrent.futures import ThreadPoolExecutor` to imports.
- Added `MAX_WORKERS = int(os.environ.get("PREPARE_WORKERS", "10"))` constant after `CACHE_DIR`.
- Replaced the 4-line sequential `for ticker in TICKERS` loop in `__main__` with `executor.map(process_ticker, TICKERS)` + `ok = sum(results)`. `write_trend_summary` call is unchanged and still fires after all downloads complete.

### Wave 2 — Tests

**Task 2.1 — `tests/test_screener_prepare.py`:** Appended 5 new tests (86 new lines).

**Task 2.2 — `tests/test_prepare.py`:** Appended 4 new tests (78 new lines).

---

## Divergences from Plan

No divergences. All steps were implemented exactly as specified in the plan, including the `as_completed` vs `executor.map` choice rationale (UX progress printing vs simplicity), module-level helper placement, and the minor side-note that `import threading` was added even though the implementation does not call any `threading.*` symbols directly (plan step 1 explicitly listed it).

---

## Test Results

**Tests Added:**
- `test_process_one_returns_skip_for_current_ticker` — _process_one SKIP path
- `test_process_one_returns_cached_after_download` — _process_one success path
- `test_process_one_returns_fail_on_empty_download` — _process_one FAIL path
- `test_parallel_all_tickers_processed` — 3 tickers / 2 workers, completeness check
- `test_max_workers_reads_from_env` — SCREENER_PREPARE_WORKERS env var
- `test_parallel_loop_processes_all_tickers` — all tickers called exactly once
- `test_parallel_loop_counts_failures` — False returns counted correctly
- `test_prepare_max_workers_reads_from_env` — PREPARE_WORKERS env var
- `test_process_ticker_parallel_no_contention` — two threads, separate parquet paths

**Test Execution:**
```
Level 1: python -c "import screener_prepare, prepare"  →  ✅ PASS
Level 2: pytest tests/test_screener_prepare.py tests/test_prepare.py -v  →  ✅ 38/38 PASS
Level 3: pytest tests/ -q  →  282 passed, 1 failed (pre-existing)
```

**Pass Rate:** 38/38 new+existing in targeted files (100%); 282/283 full suite (pre-existing failure unrelated to this feature).

---

## What was tested

- `_process_one` returns `("AAPL", "SKIP (current)")` when the ticker's parquet file is already dated yesterday or later.
- `_process_one` returns `("AAPL", "cached (N rows)")` after a mocked yfinance call returns a valid hourly DataFrame.
- `_process_one` returns `("AAPL", "FAIL")` when yfinance returns an empty DataFrame (no file written or updated).
- A `ThreadPoolExecutor` with 2 workers dispatched over 3 tickers processes all 3 with no ticker silently dropped; result keys equal the full input set.
- `screener_prepare.MAX_WORKERS` reads the integer value of `SCREENER_PREPARE_WORKERS` environment variable when the module is reloaded.
- `executor.map(process_ticker, tickers)` calls a mocked `process_ticker` exactly once per ticker and collects all True return values.
- A single failing ticker (`FAIL_ME`) in a 3-ticker list produces `ok == 2` when `False` returns are summed after `executor.map`.
- `prepare.MAX_WORKERS` reads the integer value of `PREPARE_WORKERS` environment variable when the module is reloaded.
- Two tickers processed in parallel threads each write their own `.parquet` file to `tmp_path` with no contention or corruption, both returning `True`.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import screener_prepare, prepare"` | ✅ | Clean import, no syntax errors |
| 2 | `pytest tests/test_screener_prepare.py tests/test_prepare.py -v` | ✅ | 38/38 pass |
| 3 | `pytest tests/ -q` | ✅ | 282 passed, 1 pre-existing failure excluded from scope |

---

## Challenges & Resolutions

No challenges encountered. Both worker functions (`download_and_cache`, `process_ticker`) were already thread-safe prior to this change (each writes to its own ticker-namespaced path; `os.makedirs` uses `exist_ok=True`). The extraction of `_process_one` was mechanical and the test fixtures from the existing suite (`screener_cache_tmpdir`, `_make_hourly_df`) composed cleanly with the new tests.

---

## Files Modified

**Implementation (2 files):**
- `screener_prepare.py` — added imports, `MAX_WORKERS`, `_process_one` helper, replaced sequential loop (+63/-26)
- `prepare.py` — added import, `MAX_WORKERS`, replaced sequential loop (+11/-4 net in diff, +3 lines net in __main__)

**Tests (2 files):**
- `tests/test_screener_prepare.py` — appended 5 parallel tests (+86)
- `tests/test_prepare.py` — appended 4 parallel tests (+78)

**Docs (1 file):**
- `PROGRESS.md` — feature status + report reference (+7)

**Total:** 219 insertions(+), 26 deletions(-)

---

## Success Criteria Met

- [x] `screener_prepare.py` has `MAX_WORKERS` constant from `SCREENER_PREPARE_WORKERS` env var, default `10`
- [x] `screener_prepare.py` main loop uses `ThreadPoolExecutor(max_workers=MAX_WORKERS)` with `as_completed`
- [x] `_process_one(ticker, history_start) -> tuple` exists at module level, returns `(ticker, status_string)`
- [x] `_process_one` returns `"SKIP (current)"` for an already-current ticker
- [x] `_process_one` returns `"cached (N rows)"` on successful download
- [x] `_process_one` returns `"FAIL"` when yfinance returns empty data
- [x] `prepare.py` has `MAX_WORKERS` constant from `PREPARE_WORKERS` env var, default `10`
- [x] `prepare.py` main loop uses `ThreadPoolExecutor(max_workers=MAX_WORKERS)` with `executor.map`
- [x] `write_trend_summary` is still called after all parallel downloads complete
- [x] A failed ticker download does not abort other in-flight downloads
- [x] Final `cached/skipped/failed` counts correctly reflect outcomes across all threads
- [x] Both scripts import cleanly
- [x] All 9 new tests pass
- [x] Full test suite has no new failures

---

## Recommendations for Future

**Plan Improvements:**
- The plan correctly called out that `import threading` was needed even without direct `threading.*` usage — this level of import specificity in plans avoids unnecessary back-and-forth during execution.
- The thread-safety audit table in the plan was a high-value addition; include this pattern in future parallelisation plans to pre-empt reviewer questions.

**Process Improvements:**
- The `as_completed` vs `executor.map` choice was clearly justified in the plan notes section. Continue using a Notes section for design rationale that doesn't fit elsewhere.

**CLAUDE.md Updates:**
- None required. The existing "Production code is silent" rule was correctly applied: `_process_one` returns status strings rather than printing, keeping all output in the `__main__` block.

---

## Conclusion

**Overall Assessment:** A clean, low-risk refactor executed exactly to spec. The parallelisation is transparent to all callers — identical observable behaviour, no API changes, no new dependencies (stdlib `concurrent.futures`). The `_process_one` extraction produces a well-tested, importable unit that also makes the `__main__` block simpler. Expected runtime improvement is 8–12× for both scripts under normal network conditions.

**Alignment Score:** 10/10 — zero divergences from plan; all acceptance criteria met; all planned tests implemented and passing.

**Ready for Production:** Yes — syntax clean, full suite green (minus pre-existing unrelated failure), thread-safety pre-verified by design.
