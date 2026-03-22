# Execution Report: V3-D Diagnostics and Advanced (R6, R10, R11)

**Date:** 2026-03-22
**Plan:** `.agents/plans/v3-d-diagnostics.md`
**Executor:** Sequential (single agent, 4-wave plan)
**Outcome:** Success

---

## Executive Summary

All three V3-D diagnostic features were implemented fully: regime detection and trade attribution (R11), bootstrap CI on final P&L (R10), and ticker holdout for generalization checks (R6). All 15 unit tests pass (39 total vs 24 baseline), the GOLDEN_HASH was updated, and the smoke test confirms `train.py` runs cleanly with a `regime` column in `trades.tsv`.

**Key Metrics:**
- **Tasks Completed:** 9/9 (100%)
- **Tests Added:** 15
- **Test Pass Rate:** 39/39 (100%) — 1 pre-existing skip unchanged
- **Files Modified:** 4 (`train.py`, `tests/test_optimization.py`, `program.md`, `PROGRESS.md`)
- **Lines Changed:** +475 / -9
- **Execution Time:** ~1 session
- **Alignment Score:** 10/10

---

## Implementation Summary

### Wave 1 — Mutable Constant
`TICKER_HOLDOUT_FRAC = 0.0` added to the mutable constants block in `train.py` after `ROBUSTNESS_SEEDS`, matching the naming convention and defaulting to off.

### Wave 2 — Immutable-Zone Harness Changes
Five tasks completed in the immutable zone:

- **R11 `detect_regime()`** added immediately before `_compute_avg_correlation()`. Uses cross-sectional SMA50 majority vote across all tickers; requires ≥51 rows per ticker and ≥2 valid tickers to return `'bull'`/`'bear'`; falls back to `'unknown'`.
- **R11 trade attribution**: `_entry_regime = detect_regime(ticker_dfs, today)` called at each new entry in `run_backtest()`; `"regime"` stored in `portfolio[ticker]` dict and propagated to both stop-close and end-of-backtest trade record appends. `regime_stats` dict accumulated from `trade_records` and added to the return dict (including the early-exit guard path).
- **R10 `_bootstrap_ci()`** added before `_write_final_outputs()`. Resamples closed trade P&Ls with 2000 bootstrap draws using `np.random.default_rng(42)` for determinism. Returns `(0.0, 0.0)` for < 2 trades. `_write_final_outputs()` signature extended with optional `trade_records: list | None = None`; bootstrap lines printed when `trade_records` is provided.
- **R11 TSV**: `"regime"` added to `fieldnames` in `_write_trades_tsv()` as the 6th column (after `stop_type`); `restval=""` ensures backward compatibility for any record missing the key.
- **R6 ticker split** added in `__main__` immediately after `load_all_ticker_data()`. Deterministic tail split using `sorted(ticker_dfs.keys())`. Walk-forward folds use `_train_ticker_dfs`; silent holdout continues using full `ticker_dfs` (time-based, not ticker-based). Holdout backtest runs after `min_test_pnl` when `_holdout_ticker_dfs` is non-empty; `trade_records` passed to `_write_final_outputs()`.

### Wave 3 — Tests, GOLDEN_HASH, program.md
- 15 unit tests written covering all 15 planned test cases from the TEST COVERAGE SUMMARY table.
- GOLDEN_HASH updated from `8f2174487376cd0ac3e40a2dc8628ec374cc3753dbfb566cec2c6a16d5857bad` to `83da6a88893d587eb96edd0360caee1f3d58c6c3e4c9857ae42d4c79c9cf5133`.
- `program.md` updated with `regime` column in trades.tsv schema table, `ticker_holdout_pnl:` / `ticker_holdout_trades:` output note, and bootstrap CI note in the final run section.

### Wave 4 — Validation
Full test suite: 39 passed, 1 skipped. `train.py` smoke test: PASS. `trades.tsv` regime column: PASS.

---

## Divergences from Plan

No divergences. All plan tasks were implemented as specified with no deviations in behavior, structure, or naming.

---

## Test Results

**Tests Added:** 15 (all in `tests/test_optimization.py`)
**Test Execution:** `uv run pytest tests/test_optimization.py -v`
**Pass Rate:** 39/39 (100%) — 1 pre-existing skip (git state check)

---

## What was tested

- `detect_regime()` returns `'bull'` when all tickers have `price_10am` above their 50-day SMA.
- `detect_regime()` returns `'bear'` when the majority of tickers have `price_10am` below their 50-day SMA.
- `detect_regime()` returns `'unknown'` when fewer than 2 tickers have at least 51 rows of history.
- All trade records returned by `run_backtest()` contain a `'regime'` key with a value in `{'bull', 'bear', 'unknown'}`.
- `run_backtest()` return dict contains `'regime_stats'` as a dict where each entry has `trades`, `wins`, and `pnl` sub-keys, and the sum of `trades` across all regime entries equals `total_trades`.
- `_bootstrap_ci()` returns `(p_low, p_high)` with `p_low <= p_high`, both finite, for a 5-element P&L list.
- `_bootstrap_ci()` returns `(0.0, 0.0)` for both an empty list and a single-element list.
- `_bootstrap_ci()` is deterministic: two calls with the same input return identical results.
- `_write_final_outputs()` prints `bootstrap_pnl_p05:` and `bootstrap_pnl_p95:` lines when `trade_records` is provided.
- `_write_final_outputs()` does not print bootstrap lines when called with `trade_records=None`.
- With `TICKER_HOLDOUT_FRAC=0.0`, the holdout set is empty and all tickers remain in training.
- With 5 sorted tickers and `TICKER_HOLDOUT_FRAC=0.4`, the last 2 tickers (`D`, `E`) go to holdout and the first 3 (`A`, `B`, `C`) remain in training.
- The ticker holdout split is deterministic: two calls with the same input produce the same holdout set.
- When `TICKER_HOLDOUT_FRAC=0.5` and two tickers are loaded, `__main__` output contains a `ticker_holdout_pnl:` line.
- When `TICKER_HOLDOUT_FRAC=0.0`, `__main__` output does not contain a `ticker_holdout_pnl:` line.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `uv run python train.py > /dev/null 2>&1 && echo OK` | PASS | Smoke test with real cached data |
| 2 | `head -3 trades.tsv` | PASS | `regime` column present in header and rows |
| 3 | `uv run pytest tests/test_optimization.py -v` | PASS | 39 passed, 1 skipped |
| 4 | `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` | PASS | GOLDEN_HASH matches |

---

## Challenges & Resolutions

No significant challenges were encountered. The implementation followed the plan precisely. Patterns established in V3-A/B/C (such as `restval=""` in DictWriter, the `_exec_main_block()` helper for testing `__main__`, and the early-exit guard path needing all return dict keys) were directly applicable and required no rediscovery.

---

## Files Modified

**Core implementation (2 files):**
- `train.py` — `TICKER_HOLDOUT_FRAC` constant, `detect_regime()`, `_bootstrap_ci()`, R11 in `run_backtest()`, R10 in `_write_final_outputs()`, regime in `_write_trades_tsv()`, R6 split + holdout backtest in `__main__` (+102 / -3)

**Tests (1 file):**
- `tests/test_optimization.py` — 15 new unit tests for R11/R10/R6, GOLDEN_HASH updated (+328 / -3)

**Documentation (2 files):**
- `program.md` — trades.tsv schema table, ticker_holdout_pnl output note, bootstrap CI note (+41 / -1)
- `PROGRESS.md` — V3-D status updated to Complete (+7 / -2)

**Total:** 475 insertions(+), 9 deletions(-)

---

## Success Criteria Met

- [x] AC-1: `detect_regime()` callable with correct signature; returns `'bull'`/`'bear'`/`'unknown'`
- [x] AC-2: All trade_records have `'regime'` key; `regime_stats` in return dict; TSV has `regime` column
- [x] AC-3: `_bootstrap_ci()` edge cases handled; deterministic; bootstrap lines printed when `trade_records` provided
- [x] AC-4: `TICKER_HOLDOUT_FRAC=0.0` leaves behavior unchanged; split is deterministic; holdout line appears when non-empty
- [x] AC-5: GOLDEN_HASH updated and test passes
- [x] AC-6: No regressions; all 24 previously passing tests still pass

---

## Recommendations for Future

**Plan Improvements:**
- The plan correctly identified the early-exit guard path (L345–349) as needing `"regime_stats": {}`; this level of specificity saved time and avoided a subtle regression.
- Future plans could similarly enumerate all early-exit return sites for any new return dict key.

**Process Improvements:**
- The `_compute_holdout_split()` test helper mirrors `__main__` logic inline rather than importing it directly, which is the right call given `__main__` is not a function. This pattern should be documented as the standard approach for testing module-level `__main__` split logic.

**CLAUDE.md Updates:**
- None required; existing patterns (DictWriter restval, `_exec_main_block` for `__main__` testing, immutable zone hash discipline) were sufficient and worked as documented.

---

## Conclusion

**Overall Assessment:** V3-D was a clean, additive implementation with zero regressions and complete planned test coverage. All three features (R11 regime attribution, R10 bootstrap CI, R6 ticker holdout) are fully functional, gated off by default, and well-tested. The plan's level of specificity — line-number references, exact function signatures, and an explicit test coverage table — made execution straightforward.

**Alignment Score:** 10/10 — implemented exactly as planned with no deviations.
**Ready for Production:** Yes — all tests pass, smoke test clean, GOLDEN_HASH updated, behavior fully backward compatible.
