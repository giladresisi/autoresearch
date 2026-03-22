# Code Review: V3-B Walk-Forward Evaluation Framework

**Date**: 2026-03-22
**Plan**: `.agents/plans/v3-b-walk-forward.md`
**Reviewer**: Claude Sonnet 4.6

## Stats

- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: ~419 (train.py +99, test_optimization.py +247, program.md +74 net)
- Deleted lines: ~51 (program.md -50, test_optimization.py -1)

## Test Results

All 13 tests collected; 12 passed, 1 skipped (pre-existing skip: `test_most_recent_train_commit_modified_only_editable_section` skips when train.py has no commits on current branch — expected on a fresh branch).

GOLDEN_HASH in `test_harness_below_do_not_edit_is_unchanged` correctly updated to `796595717636d21cc47c897589e08fa8cf3c8d9c34cc8dedfc80957c7f64fe3d` and verified to match the actual harness content.

---

## Issues Found

```
severity: medium
file: train.py
line: 391
issue: equity_curve uses absolute position MTM (price×shares) not unrealized P&L ((price−entry)×shares)
detail: The equity curve point is computed as `cumulative_realized + portfolio_value` where
  `portfolio_value = price_10am * shares`. This is the absolute dollar value of open positions,
  not their P&L relative to entry. As a result, `pnl_consistency` is computed from monthly deltas
  starting from `prev_eq = 0.0`. In the first calendar month of the backtest, the delta is
  `equity[month_end] - 0 = cumulative_realized + price*shares`, which includes the full position
  cost, not just the gain/loss. For example: entry at price=108.57, shares=2.5 → first-month
  'P&L' = 250 (absolute MTM) instead of the true -21.4 (loss on falling prices). This causes
  `pnl_consistency` to silently over-report the worst month, making the metric unreliable as
  a diagnostic for regime-specific performance.

  Confirmed empirically: a strategy entering at 108.57 and exiting at ~100 reports
  `total_pnl=-21.47` but `pnl_consistency=0.0` (the inflated Dec monthly PnL masks the loss).

  Note: `max_drawdown` is NOT affected by this bug. The peak-minus-equity delta correctly
  cancels the absolute MTM offset because both peak and current equity use the same basis.

  Note: `min_test_pnl` (the optimizer's keep/discard criterion) is also NOT affected —
  it reads `total_pnl` from `run_backtest()` directly, not `pnl_consistency`. The core
  optimization loop remains correct.

suggestion: Replace `portfolio_value` in the equity curve with unrealized P&L:
  ```python
  open_pnl = sum(
      (float(ticker_dfs[t].loc[today, "price_10am"]) - pos["entry_price"]) * pos["shares"]
      for t, pos in portfolio.items()
      if today in ticker_dfs[t].index
  )
  equity_curve.append((today, cumulative_realized + open_pnl))
  ```
  Keep `daily_values.append(portfolio_value)` unchanged (used only for Sharpe computation).
```

```
severity: low
file: tests/test_optimization.py
line: 369-371
issue: test_pnl_consistency_equals_min_monthly_pnl uses a weak assertion that does not verify the min computation
detail: The test name promises it checks that pnl_consistency equals the minimum monthly P&L,
  but the only assertion is `pnl_consistency <= total_pnl`. This passes even when
  pnl_consistency is incorrect (e.g. 0.0 when the true min monthly P&L is -21.4).
  The test does not catch the equity-curve absolute-MTM bug described above.
suggestion: Add an assertion that checks the approximate expected value. Given the test
  setup (Jan: rising prices with entries, Feb: flat prices), the min monthly P&L should
  be <= 0 (Feb is flat), so at minimum add:
  ```python
  assert stats["pnl_consistency"] <= 0.0, (
      "Feb has flat prices and no new entries; its monthly P&L should be <= 0"
  )
  ```
  Or compute the expected value from the synthetic data and assert equality within a tolerance.
```

```
severity: low
file: train.py
line: 435
issue: calmar uses signed total_pnl, producing negative calmar for losing strategies
detail: `calmar = total_pnl / max_drawdown` where `total_pnl` is signed. The conventional
  Calmar ratio uses annualized return (always positive) divided by max drawdown, yielding
  a non-negative value. With this implementation, a strategy that loses exactly as much
  as its max drawdown gets calmar = -1.0. Since `pnl_consistency` is diagnostic-only per
  the plan and `min_test_pnl` drives keep/discard, there is no functional impact on the
  optimizer. However, the non-standard sign convention may mislead interpretation.
suggestion: If calmar is intended as a pure diagnostic, document the non-standard convention
  in a comment: `# non-standard: signed (total_pnl can be negative)`. If conventional
  calmar is desired, use `abs(total_pnl) / max_drawdown` or cap at 0.
```

```
severity: low
file: train.py
line: 394-411
issue: end-of-backtest position closes are not reflected in equity_curve, causing potential max_drawdown underreporting
detail: After the main loop, open positions are closed at `last_price = price_10am.iloc[-1]`.
  These final realized P&Ls update `cumulative_realized` but `equity_curve` is only appended
  inside the main loop (line 391), so the equity curve's last point represents the pre-close
  MTM state. If a position's last-day close results in a large realized loss (e.g. stop triggers
  on the last day), the equity_curve doesn't capture that drop and max_drawdown is understated.
  In practice this scenario is unusual (stops are checked against previous-day low, line 336),
  but the end-of-backtest close uses `last_price` which could differ significantly from the stop.
suggestion: After the end-of-backtest loop, append the post-close equity to the curve:
  ```python
  if trading_days:
      equity_curve.append((trading_days[-1], cumulative_realized))  # all positions now closed
  ```
  This ensures max_drawdown sees the final realized state.
```

---

## No Issues Found In

- Walk-forward fold boundary computation (lines 548-569): fold windows are non-overlapping, contiguous, and correctly step 10 business days back from TRAIN_END. Verified numerically.
- Silent holdout separation: the holdout window [TRAIN_END, BACKTEST_END] is strictly after all fold test windows. Correctly hidden unless `WRITE_FINAL_OUTPUTS = True`.
- `GOLDEN_HASH` update: correctly recomputed and matches current harness content.
- `_exec_main_block` test helper: exec-in-namespace approach correctly inherits the train module's functions (including the patched versions via `extra_ns`). The `sys.exit` guard is not reached because `load_all_ticker_data` is patched to return non-empty data.
- File write side effects in tests: `_write_trades_tsv` is patched to a no-op in all `__main__` tests. `_write_final_outputs` is not called (gated on `WRITE_FINAL_OUTPUTS = False`).
- `test_main_runs_walk_forward_windows_folds`: correctly asserts `WALK_FORWARD_WINDOWS * 2 + 1` total `run_backtest` calls (N train + N test + 1 holdout).
- Fold train data non-leakage: each fold's train window ends strictly before its test window starts (`[s, e)` semantics in `run_backtest`).
- `WALK_FORWARD_WINDOWS = 3` constant is safe: fold1 train_end = 2026-01-23, well after BACKTEST_START = 2025-12-20. No empty training windows.
- `pnl_consistency` fallback `else total_pnl` is unreachable after the `< 2 trading days` early-exit guard — dead code, not a bug.
- Security: no secrets, hardcoded URLs, SQL, or external I/O introduced.
