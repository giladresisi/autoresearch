# Code Review: V5-B Volume Criterion Redesign

**Date**: 2026-03-25
**Scope**: Volume criterion redesign (pre-market look-ahead fix) + dedicated test fixture infrastructure

## Stats

- Files Modified: 5 (train.py, strategy_selector.py, tests/test_e2e.py, tests/test_optimization.py, trades.tsv)
- Files Added: 3 (tests/conftest.py, tests/test_fold_auto_detect.py, .agents/plans/dedicated-test-parquet-fixture.md)
- Files Deleted: 0
- New lines: ~191
- Deleted lines: ~84

---

## Summary of Changes

This changeset does two distinct things:

1. **Volume criterion bug fix (train.py `screen_day`)**: Removes `today_vol / vm30 >= 2.5` — which used today's intraday volume, unavailable pre-market — and replaces it with two prior-data-only rules: 5-day avg volume >= MA30 (`vol_trend_ratio >= 1.0`) and yesterday's volume >= 0.8× MA30 (`prev_vol_ratio >= 0.8`).

2. **Dedicated test fixture infrastructure**: Adds `tests/conftest.py` with a session-scoped `test_parquet_fixtures` fixture that downloads AAPL/MSFT/NVDA via yfinance and caches to disk, eliminating the dependency on a pre-populated 389-ticker cache for integration tests. Also adds `_compute_fold_params()` to handle short backtest windows.

---

## What Was Done Well

- The core bug fix (removing `today_vol` from a pre-market screener) is correct and clearly motivated. The comment "today_vol excluded — 0 pre-market" is accurate and helpful.
- `prev_vol_ratio` uses `hist['volume'].iloc[-1]` (yesterday) while `vol_trend_ratio` uses `hist['volume'].iloc[-5:].mean()` — both correctly operate on `hist = df.iloc[:-1]`, so look-ahead is preserved.
- The `strategy_selector.py` fix for `None` `train_pnl` is minimal and correct. The `pnl_str` guard handles exactly the case where a strategy has no training data.
- `conftest.py` is clean: session scope for expensive downloads, persistent disk cache, graceful skip on network failure, tmpdir cleanup in a finally-equivalent `yield` teardown.
- The GOLDEN_HASH update protocol (updating hash + skipping the commit guard) is followed correctly.
- Return dict in `screen_day` now returns both `prev_vol_ratio` and `vol_trend_ratio`, preserving diagnostic visibility — the old `vol_ratio` key is cleanly replaced.

---

## Issues Found

---

```
severity: high
file: tests/test_optimization.py
line: 655
issue: fold_prefixes built from WALK_FORWARD_WINDOWS (7) but subprocess runs with _effective_n_folds (1 for short window)
detail: test_live_train_py_subprocess_outputs_pnl_min runs train.py via subprocess with AUTORESEARCH_CACHE_DIR pointing
        to the 3-ticker small fixture. Because the fixture's backtest window (2024-09-01 to TRAIN_END ~2026-02-20) is
        >> 130 bdays, _effective_n_folds stays at WALK_FORWARD_WINDOWS=7 in practice on the current fixture, so this
        test passes today. However the assertion is latently fragile: if BACKTEST_START or TRAIN_END is adjusted so
        that the fixture window becomes short, _compute_fold_params will emit only fold1_* lines while fold_prefixes
        still expects fold1 through fold7, causing 12 assertion failures (6 folds × 2 splits). The test does not
        call _compute_fold_params to determine the expected fold count — it hardcodes the constant.
suggestion: Replace `range(train.WALK_FORWARD_WINDOWS)` at line 655 with a call to `train._compute_fold_params(
        train.BACKTEST_START, train.TRAIN_END, train.WALK_FORWARD_WINDOWS, train.FOLD_TEST_DAYS)[0]`
        to derive the expected fold count consistently with the actual subprocess.
```

---

```
severity: medium
file: tests/test_optimization.py
line: 586-587
issue: test_live_walk_forward_min_test_pnl_is_finite iterates using WALK_FORWARD_WINDOWS and hardcoded step of 10, ignoring _compute_fold_params
detail: This test manually reimplements the fold loop using `range(train.WALK_FORWARD_WINDOWS)` and a hardcoded
        `_BDay_live(10)` step. If BACKTEST_START is set short enough that _effective_n_folds drops to 1 with a
        different step size, this test's fold boundaries diverge from what the real harness computes, making
        the "same fold boundaries" assertion meaningless. Currently non-failing due to the fixture window being long,
        but structurally inconsistent with the adaptive fold logic introduced in this PR.
suggestion: Use train._compute_fold_params(...) and train.FOLD_TEST_DAYS (or its effective variant) to drive
        the loop, rather than raw WALK_FORWARD_WINDOWS and a magic 10.
```

---

```
severity: medium
file: tests/test_optimization.py
line: 421-453
issue: test_main_runs_walk_forward_windows_folds expects WALK_FORWARD_WINDOWS * 2 + 1 calls but _compute_fold_params may reduce to 1 fold
detail: The test uses _exec_main_block with a minimal_df (200 bdays ending 2026-02-27). The __main__ block now
        calls _compute_fold_params(BACKTEST_START, TRAIN_END, ...). With BACKTEST_START="2024-09-01" and
        TRAIN_END ~2026-02-20, bdate_range gives ~365 bdays >> 130, so n_folds stays 7 and the test still passes.
        But the test comment still says "WALK_FORWARD_WINDOWS * 2 + 1" and does not acknowledge that
        _compute_fold_params exists — so a future change to BACKTEST_START or the threshold could cause a silent
        divergence between the expected count and actual.
suggestion: Add a comment noting that _compute_fold_params is called inside __main__ and will override the
        fold count when total_bdays < 130. Optionally assert that _compute_fold_params returns unchanged values
        for the test fixture's date range, to make the assumption explicit.
```

---

```
severity: low
file: tests/conftest.py
line: 4
issue: Module docstring says backtest window is "2024-09-01..2024-11-01" but TEST_BACKTEST_END is "2025-11-01"
detail: The docstring at line 4 reads "backtest window (2024-09-01..2024-11-01)" which is inconsistent with
        TEST_BACKTEST_END = "2025-11-01" at line 38. The actual window is 15 months, not 2 months. The comment
        at line 38 correctly states "~15 months of backtest data". The docstring was likely not updated when
        the end date was extended.
suggestion: Update line 4 docstring to "backtest window (2024-09-01..2025-11-01)" to match the constant.
```

---

```
severity: low
file: tests/test_e2e.py
line: 186-193
issue: test_agent_loop_two_iterations_multi_ticker docstring still references old vol_ratio semantics
detail: The docstring at line 188 says "Iteration 1 — relax volume threshold (vol_ratio 2.5 → 2.0)" but the
        implementation at line 235 now uses vol_trend_ratio. The docstring was not updated with the code change.
suggestion: Update the docstring to read "Iteration 1 — relax volume trend threshold (vol_trend_ratio 1.0 → 0.7)".
```

---

```
severity: low
file: tests/test_e2e.py
line: 333
issue: Stale error message references vol_ratio rather than vol_trend_ratio
detail: The assertion failure message at line 333 reads:
        "Relaxed screener (vol_ratio 0.8, RSI 80, res_atr 0.1) produced 0 trades..."
        The threshold name "vol_ratio 0.8" is a holdover from the old criterion; the new screener
        uses vol_trend_ratio and prev_vol_ratio. While this does not affect test correctness,
        it is misleading when diagnosing failures.
suggestion: Update the message to "Relaxed screener (vol_trend_ratio 0.7, RSI 80, res_atr 0.1) produced 0 trades..."
```

---

## Pre-existing Failures

Per PROGRESS.md: `test_most_recent_train_commit_modified_only_editable_section` has a pre-existing failure status from commit ecbc2d2. This commit's changes correctly handle this via the GOLDEN_HASH update path (the test now skips with a documented explanation when harness + GOLDEN_HASH are updated together). This is not a new regression.

---

## Plan Alignment

The implementation matches the plan in `.agents/plans/dedicated-test-parquet-fixture.md`. All six deliverables from the plan are present: `conftest.py`, `_compute_fold_params()`, `__main__` fold loop update, `test_e2e.py` fixture wiring, `test_fold_auto_detect.py`, and GOLDEN_HASH update. The volume criterion redesign (train.py `screen_day`) is correctly scoped to the mutable section above the DO NOT EDIT line and does not touch the harness.

One plan deviation to note: the plan specifies `history: 2023-09-01` for the fixture's history start, but the implementation uses `TEST_HISTORY_START = "2024-04-01"` with a comment explaining the yfinance 730-day limit for 1h data. This is a justified deviation — the plan pre-dated the discovery of the yfinance API constraint.
