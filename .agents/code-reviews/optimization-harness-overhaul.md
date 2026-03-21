# Code Review: optimization-harness-overhaul

**Branch**: autoresearch/mar20
**Files reviewed**: train.py, prepare.py, tests/test_optimization.py, tests/test_backtester.py, tests/test_program_md.py

## Stats

- Files Modified: 5 (+ program.md not in scope, also modified)
- Files Added: 0
- Files Deleted: 0
- New lines: ~221
- Deleted lines: ~53

---

## Pre-existing Failures

```
test: tests/test_backtester.py::test_run_backtest_integration_real_screener_fires
status: FAILED (pre-existing — confirmed via git stash + rerun on HEAD~)
root cause: The synthetic dataset in make_signal_df_for_backtest() was designed for a
            prior version of screen_day. The new screener (RSI 50-75 filter + breakout
            rules) no longer fires on that synthetic pattern, making the integration
            test a stale fixture that needs updating. The test failure predates this PR.
```

---

## Issues Found

```
severity: medium
file: train.py
line: 401-431 (_write_final_outputs)
issue: Indicators computed only on the 14-day test window, producing all-NaN columns in final_test_data.csv
detail: The function slices `sub = df[(df.index >= s) & (df.index < e)]` to the test
        window (~14 calendar days) BEFORE computing rolling indicators. sma50 requires
        50 bars and vm30 requires 30 bars — both will be 100% NaN for any test window
        shorter than their lookback. rsi14 and atr14 need 14 bars; in a 14-day window
        only the very last row will produce a value. The CSV is technically valid but
        all indicator columns will be empty strings for virtually every row, making the
        file useless for the stated purpose ("per-ticker daily OHLCV + indicators for
        the test window").
suggestion: Compute indicators on the full `df` first, then slice to the test window:
            df = df.copy()
            df['sma50'] = df['close'].rolling(50).mean()
            ...
            sub = df[(df.index >= s) & (df.index < e)]
            Then iterate over sub for the CSV rows.
```

```
severity: low
file: train.py
line: 267-269 (DO NOT EDIT comment)
issue: DO NOT EDIT comment does not list _write_final_outputs() as an off-limits function
detail: The comment enumerates "run_backtest(), print_results(), load_ticker_data(),
        load_all_ticker_data(), and the __main__ block" as the frozen harness. The new
        _write_final_outputs() function lives below the marker and is covered by the
        GOLDEN_HASH guard, but it is not mentioned in the comment. An agent following
        the comment literally might think it is modifying a listed function when
        it is editing _write_final_outputs, and not realise the hash will change.
suggestion: Add _write_final_outputs() to the enumeration in the comment at line 268.
```

```
severity: low
file: program.md
line: 3, 98, 102
issue: Goal stated as "maximize sharpe ratio" in description/prose but Goal section says "maximize train_total_pnl"
detail: The file header (line 3) reads "to maximize the sharpe ratio of a historical
        backtest." Line 98 says "A small sharpe improvement that adds ugly complexity
        is not worth it." Line 102 says "to establish the baseline Sharpe before making
        any changes." These are inconsistent with the Goal section (line 91) which
        explicitly redirects agents to maximize train_total_pnl instead of Sharpe.
        An agent reading the header first could be confused about the objective.
suggestion: Update the description on line 3 to "to maximize total P&L on the training
            window" and update line 102's phrasing from "baseline Sharpe" to "baseline
            P&L and Sharpe". Line 98 is a simplicity heuristic and can remain as-is or
            be updated to reference train_total_pnl.
```

```
severity: low
file: prepare.py
line: 128
issue: Non-standard median computation for even-length lists
detail: `median_ret = float(sorted(returns)[len(returns) // 2])` uses integer division
        to pick the upper-middle element for even-length lists rather than averaging the
        two middle elements (which is the standard median). For odd N this is correct;
        for even N (e.g. 16 tickers) it always picks the upper of the two middle values,
        which biases the reported median slightly high.
suggestion: Use `statistics.median(returns)` from the stdlib to get the correct median,
            or explicitly average the two middle elements for even-length inputs.
            This is low severity because the output is a human-readable summary file,
            not an input to the optimization loop.
```

---

## Positive Observations

- **GOLDEN_HASH correctly updated**: The hash in test_optimization.py matches the actual
  harness below the DO NOT EDIT marker. Verified via SHA-256 computation.

- **Train/test boundary logic is correct**: TRAIN_END == TEST_START == "2026-03-06".
  The backtest uses `s <= d < e`, so train gets [2025-12-20, 2026-03-06) and test gets
  [2026-03-06, 2026-03-20). No data leakage; the boundary day is in test only.

- **ticker_pnl accumulation is correct**: Both the stop-triggered close path (line 317)
  and the end-of-backtest close path (line 361) correctly accumulate into ticker_pnl
  using `.get(ticker, 0.0)` pattern. No double-counting.

- **WRITE_FINAL_OUTPUTS defaults False**: The flag is off by default and the program.md
  instructions correctly require restoring it to False immediately after the final run,
  with an explicit "Never commit it as True" warning.

- **Early-return on empty test window is safe**: If TEST_START == BACKTEST_END (edge case),
  run_backtest returns the zero-stats dict rather than crashing.

- **Test updates are consistent**: test_backtester.py correctly adds backtest_start/
  backtest_end keys to the dict literals; test_optimization.py correctly updates both
  the assertion expression and the golden hash.
