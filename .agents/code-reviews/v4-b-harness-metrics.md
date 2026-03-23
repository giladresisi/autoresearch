# Code Review: V4-B Harness Metric Improvements

**Date**: 2026-03-24
**Branch**: master (uncommitted changes)
**Plan**: `.agents/plans/v4-b-harness-metrics.md`

## Stats

- Files Modified: 6 (train.py, tests/test_backtester.py, tests/test_optimization.py, tests/test_program_md.py, program.md, trades.tsv)
- Files Added: 1 (tests/test_v4_b.py)
- Files Deleted: 0
- New lines: ~394
- Deleted lines: ~48

## Test Results

All 119 tests pass in 255s (0:04:15). No regressions introduced.

---

## Issues Found

### Issue 1

```
severity: medium
file: train.py
line: 351
issue: stale comment says "trail 1.5 ATR" after coefficient was changed to 1.2×
detail: The inline comment on the trail_stop line still reads
    "# Trailing stop: trail 1.5 ATR below recent high once 2.0 ATR in profit (earlier activation)"
    The docstring at line 323 was correctly updated to "Trail by 1.2 ATR", but the
    in-body comment was not. This will mislead anyone reading the code about the
    actual coefficient in effect.
suggestion: Change the comment on line 351 to:
    "# Trailing stop: trail 1.2 ATR below recent high once 2.0 ATR in profit"
```

### Issue 2

```
severity: medium
file: train.py
lines: 497-499
issue: R14 partial-close PnL is computed against pos["entry_price"] which already includes the 0.03 slippage added at entry — the PnL is correct — but the comment says "+1.0R" where R = RISK_PER_TRADE, while the actual trigger uses atr14 as the move threshold, not R (risk dollars per trade). The trigger fires when price_10am >= entry_price + atr14, which is a +1 ATR move, not a +1R move in the risk-dollar sense.
detail: "R" in position-sizing jargon means one unit of initial risk (entry_price − stop_price),
    which equals RISK_PER_TRADE / shares. The partial fires at +1 ATR, not +1R. With a typical
    stop placed 1.5–2.5 ATR below entry, +1 ATR is only 40–67% of a full R, so the comment
    "+1.0R" overstates the move required and will cause confusion when tuning the partial-take
    profit level.
suggestion: Change the comment on line 494 to:
    "# R14: Partial close at +1 ATR — close 50% of position on first up-move of +1 ATR."
    This is consistent with the plan spec wording "price_10am >= entry + 1.0 × ATR".
```

### Issue 3

```
severity: medium
file: train.py
lines: 503-509
issue: The partial trade record is missing the required "days_held", "stop_type", "regime", "entry_price", and "exit_price" fields that the test at test_backtester.py:460-464 asserts are present in every trade record.
detail: The full schema required by test_run_backtest_trade_records_schema is:
    {"ticker", "entry_date", "exit_date", "days_held", "stop_type", "entry_price", "exit_price", "pnl"}
    The partial close record only includes "ticker", "entry_date", "exit_date", "pnl", "exit_type".
    The schema test currently passes because it uses a mock for manage_position and the partial close
    never fires in that test (atr14 is not provided in the fake signal so atr14 defaults to 0.0,
    which causes the `_atr_entry > 0` guard to skip the partial block). In real runs however,
    the partial record will silently land in trade_records with missing fields, which will cause
    KeyError if any consumer (e.g. _write_trades_tsv) iterates over all records and accesses
    a field from the standard schema.
    _write_trades_tsv uses fieldnames = ["ticker","entry_date","exit_date","days_held",
    "stop_type","regime","entry_price","exit_price","pnl"] and writes with restval="" —
    so the missing fields will silently appear as empty strings in trades.tsv rather than raising,
    but the data will be incomplete and misleading.
suggestion: Extend the partial record to include all standard fields:
    trade_records.append({
        "ticker":       ticker,
        "entry_date":   str(pos["entry_date"]),
        "exit_date":    str(today),
        "days_held":    (today - pos["entry_date"]).days,
        "stop_type":    pos.get("stop_type", "partial"),
        "regime":       pos.get("regime", "unknown"),
        "entry_price":  round(pos["entry_price"], 4),
        "exit_price":   round(price_10am, 4),
        "pnl":          _partial_pnl,
        "exit_type":    "partial",
    })
```

### Issue 4

```
severity: low
file: train.py
line: 344
issue: R15 cal_days_held type inconsistency: df.index contains date objects, but position['entry_date'] is also a date — the subtraction yields a timedelta, and .days is correct. However, on the very first trading day after entry (i.e. when today == entry_date), cal_days_held is 0, not 1, so the condition fires on the same day as entry if price is stagnant. This is unlikely to be the intended semantics — typically "day 1" means the first full day of holding.
detail: If entry occurs on day D and today == D (same day), _cal_days_held = 0 <= 5.
    If price_10am < entry + 0.5 * atr, the exit fires immediately — on entry day itself —
    before the position has had any time to develop. The R10 block above uses > 30 business days
    (strictly greater than), so it would be internally consistent to use < 5 (strictly less than)
    for R15 or document that day-0 exits are intentional.
suggestion: If same-day exit is unintentional, change the condition to:
    if 0 < _cal_days_held <= 5 and price_10am < entry_price + 0.5 * atr:
    Or add a comment clarifying that a cal_days_held == 0 exit (same day as entry) is acceptable.
```

### Issue 5

```
severity: low
file: tests/test_v4_b.py
lines: 172-179
issue: The fake_run_backtest in test_main_min_test_pnl_folds_included_in_output uses call_idx parity (even=train, odd=test) to distinguish train vs test calls. With WALK_FORWARD_WINDOWS=7, the loop makes 7 pairs = 14 calls (indices 0–13). The fold number is computed as idx // 2, which maps indices 1,3,5,7,9,11,13 to folds 0,1,2,3,4,5,6. total_pnl for test fold is 100.0 + fold, giving [100, 101, 102, 103, 104, 105, 106]. All have total_trades=5 >= 3, so all qualify. The test then asserts n_included == WALK_FORWARD_WINDOWS (7). This is correct and will pass. However, if WALK_FORWARD_WINDOWS is changed again, the fold numbering remains correct because call_idx is not reset between tests — but since each test creates its own call_idx=[0], this is safe.
detail: No actual bug; noting this for clarity. The test is correctly written for WALK_FORWARD_WINDOWS=7.
suggestion: No change required.
```

### Issue 6

```
severity: low
file: tests/test_v4_b.py
line: 324-328
issue: test_program_md_walk_forward_windows_default_is_7 has an overly permissive assertion: it checks that both 'WALK_FORWARD_WINDOWS' and '7' appear anywhere in program.md, which will pass even if the document says "WALK_FORWARD_WINDOWS was formerly 7". The intended assertion is that the document specifies the current default value of 7.
detail: The condition is:
    'WALK_FORWARD_WINDOWS = 7' in text or 'WALK_FORWARD_WINDOWS=7' in text or (
        'WALK_FORWARD_WINDOWS' in text and '7' in text
    )
    The third branch ('WALK_FORWARD_WINDOWS' in text and '7' in text) will always be true if the
    first two branches don't match, since any document mentioning the constant and any number
    containing '7' would pass. However in practice program.md does contain `WALK_FORWARD_WINDOWS = 7`
    explicitly, so the first branch fires and the test is effectively tight enough.
suggestion: Remove the fallback third branch to make the assertion strictly require
    'WALK_FORWARD_WINDOWS = 7' or 'WALK_FORWARD_WINDOWS=7' in the document.
```

---

## Summary

All 119 tests pass. The implementation correctly delivers R2 (fold trade-count guard), R11 (win/loss ratio), R13 (1.2× trail coefficient), R14 (partial close at +1 ATR), R15 (early stall exit), and R16 (7×40 fold defaults).

**Three issues require attention before this is considered production-quality:**

1. **Issue 1** (stale comment, line 351) — minor but will mislead maintainers about the active trail coefficient.
2. **Issue 2** (comment "+1.0R" vs actual "+1 ATR", line 494) — minor but semantically wrong terminology.
3. **Issue 3** (partial trade record missing standard schema fields, lines 503-509) — medium severity: the schema test does not catch this because the mock signal omits `atr14`, but in real runs the partial record lands in `trade_records` with missing `days_held`, `stop_type`, `regime`, `entry_price`, `exit_price`. `_write_trades_tsv` uses `restval=""` so it silently produces empty columns rather than raising — a quiet data-quality defect.

Issues 4–6 are low priority observations requiring either a clarifying comment or a test tightening, with no functional impact on correctness.
