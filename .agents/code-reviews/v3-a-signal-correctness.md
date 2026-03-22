# Code Review: V3-A Signal Correctness

**Stats:**

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: 274
- Deleted lines: 23

---

## Test Run

All 29 tests pass, 1 skipped (pre-existing skip for `test_most_recent_train_commit_modified_only_editable_section` when the branch has no experiment commits yet).

Golden hash verified: `8c797ebed7a436656539ab4d664c2c147372505769a140c29e3c4ad2b483f3c7` matches the current immutable zone content.

---

## Issues Found

```
severity: medium
file: tests/test_backtester.py
line: 377-417
issue: test_run_backtest_risk_proportional_sizing asserts mathematical identity, not behavioral correctness
detail: The R3 test computes tight_shares and wide_shares from the formula directly and asserts
        tight > wide — but this is a purely mathematical assertion. It does not verify that
        run_backtest() actually used RISK_PER_TRADE / risk when sizing the position. A regression
        where run_backtest reverts to 500/entry_price (the old formula) would not be caught
        because the test never inspects the actual shares used or the resulting P&L difference
        between sizing methods.
suggestion: Add a P&L comparison assertion: since tight_shares > wide_shares and both trades close
            at the same price in the same rising/falling direction, the tight-stop run should
            produce a larger absolute P&L. Assert abs(tight_stats['total_pnl']) != abs(wide_stats['total_pnl'])
            or assert a ratio between them matching RISK_PER_TRADE / tight_risk vs RISK_PER_TRADE / wide_risk.
            Alternatively, expose shares in the trade_records dict to make behavioral verification possible.
```

```
severity: low
file: tests/test_backtester.py
line: 143
issue: Comment says screen_day "needs 60 rows" but the guard was updated to 61
detail: Line 143 reads: '# df has only 3 rows → screen_day fails R1 (needs 60 rows) → no signals, no trades'
        After the R1 fix, the minimum history guard is len(df) < 61 (not 60). The test logic
        is correct (3 < 61 → returns None) but the comment is now stale. It refers to the
        pre-R1 threshold.
suggestion: Change the comment to: '# df has only 3 rows → screen_day fails length guard (needs 61 rows) → no signals, no trades'
```

---

## Correctness Verification

### R1 — Look-ahead fix

The `hist = df.iloc[:-1].copy()` restructure is correct. All indicator computations (`calc_atr14`, `calc_rsi14`, rolling SMA/volume), `is_stalling_at_ceiling`, `find_stop_price`, and `nearest_resistance_atr` all receive `hist`. Only `price_10am` and `today_vol` are read from `df.iloc[-1]`. The `len(df) < 61` guard is correct: 60 history rows + 1 today row = 61 minimum.

The `high20` change from `df.iloc[-21:-1].max()` to `hist.iloc[-20:].max()` is equivalent: both select the same 20 rows (the 20 rows prior to today).

The `prev_high` change from `df['high'].iloc[-2]` to `hist['high'].iloc[-1]` is equivalent: both refer to yesterday's high.

### R3 — Risk-proportional sizing

The formula `RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price` is correct. The `risk > 0` guard is logically unreachable in normal flow (screen_day enforces `price_10am - stop >= 1.5 * atr > 0`, and entry_price = price_10am + 0.03 > price_10am > stop), but provides a safe fallback. Division by zero is not possible.

`RISK_PER_TRADE` is correctly placed in the mutable zone so agents can tune it without touching the immutable harness.

### R5 — Trade-level attribution

`trade_records` is accumulated for both stop-hit closes and end-of-backtest closes. Exit prices are consistent with PnL calculations (stop-hit uses `pos["stop_price"]`; end-of-backtest uses `last_price = df["price_10am"].iloc[-1]`). The `_write_trades_tsv()` function mirrors the `_write_final_outputs()` pattern (relative path, same `csv` module usage). The `trade_records: []` key is present in the early-return path (line 307) so callers always get the key regardless of data.

### R4-partial — program.md

The note is placed between step 8 and step 9, which is the correct location. It clearly forbids using `test_total_pnl` as a keep/discard signal. The wording is unambiguous.

---

## Pre-existing Issues (Not Introduced by This Changeset)

- `test_run_backtest_no_reentry_same_ticker` and adjacent tests use a `fake_signal` dict with stale fields (`sma150`, `cci`, `pct_local`, `pct_ath`) that no longer match `screen_day`'s return schema. These fields are ignored by `run_backtest` so no functional impact, but the fixtures are misleading. This predates this changeset.
