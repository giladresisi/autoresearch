# Code Review: SMT Quality Parameters

**Date:** 2026-04-17
**Reviewer:** Claude Sonnet 4.6
**Plan:** `.agents/plans/smt-quality-params.md`

---

## Stats

- Files Modified: 4 (train_smt.py, program_smt.md, tests/test_smt_strategy.py, tests/test_smt_backtest.py)
- Files Added: 0
- Files Deleted: 0
- New lines: +435
- Deleted lines: -104

---

## Test Results

All 79 tests pass (79 pre-existing + 13 new).

---

## Issues Found

---

```
severity: medium
file: train_smt.py
line: 732-736
issue: bars_since_divergence is always -1 in all run_backtest trade records
detail: _build_signal_from_bar() initialises "entry_bar": -1 in the returned dict. In
  screen_session() this is overwritten on line 501 (signal["entry_bar"] = conf_idx). In
  run_backtest() there is no equivalent assignment — position["entry_bar"] stays -1 for
  every trade. The _build_trade_record() computation therefore always evaluates the
  "else -1" branch, making bars_since_divergence useless as a diagnostic field.
suggestion: After the two position-building blocks in run_backtest (WAITING_FOR_ENTRY
  state, line ~928, and REENTRY_ELIGIBLE state, line ~963), add:
    position["entry_bar"] = bar_idx
  This mirrors what screen_session() already does on line 501.
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 344-358
issue: test_run_backtest_max_reentry_count_limits_trades produces zero trades and the
  assertion passes vacuously — the cap is never actually exercised
detail: The test sets MAX_REENTRY_COUNT=1 but does not disable TDO_VALIDITY_CHECK
  (default True). The synthetic bars produce a short entry at close=19998 against
  TDO=20000; the validity check rejects this (tdo >= entry_price for a short). With
  zero trades, trades_by_day is empty and all(v <= 1 for v in {}.values()) is True by
  definition. The test gives false confidence that the cap is wired up.
suggestion: Add monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False) (and
  MIN_STOP_POINTS=0.0, MIN_TDO_DISTANCE_PTS=0.0 for completeness), then add a positive
  assertion such as assert len(stats["trade_records"]) >= 1 before the per-day cap check.
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 361-371
issue: test_run_backtest_max_reentry_disabled_allows_multiple asserts >= 0 on total_trades —
  this always passes regardless of whether the code executes correctly
detail: Same TDO_VALIDITY_CHECK issue as above: 0 trades are produced. The assertion
  stats["total_trades"] >= 0 is unconditionally true and tests nothing.
suggestion: Disable TDO_VALIDITY_CHECK, then either assert total_trades >= 1 or
  compare against a baseline where MAX_REENTRY_COUNT is restrictive.
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 376-391
issue: test_run_backtest_trade_record_contains_new_fields — the for loop over trade_records
  never executes because 0 trades are produced, so the six key assertions are never run
detail: TDO_VALIDITY_CHECK is not disabled; same short-rejection issue applies. All six
  assert statements are inside a for loop that iterates over an empty list.
suggestion: Add monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False) and
  monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0). Then assert
  len(stats["trade_records"]) >= 1 before the loop to fail explicitly when no trades are
  produced.
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 394-409
issue: test_run_backtest_min_prior_bars_held_infrastructure — both backtests produce
  zero trades, so 0 <= 0 always passes; the gate is never exercised
detail: TDO_VALIDITY_CHECK not disabled. stats_base["total_trades"] == 0 and
  stats_blocked["total_trades"] == 0 in all executions. The assertion
  stats_blocked["total_trades"] <= stats_base["total_trades"] (0 <= 0) tells us nothing.
suggestion: Disable TDO_VALIDITY_CHECK and MIN_STOP_POINTS. Assert
  stats_base["total_trades"] >= 1 before the comparison to ensure the baseline has trades
  to block.
```

---

## No Issues Found

- All 5 new constants have correct disabled defaults (999.0/999/0/0.0/0.0).
- MIN_CONFIRM_BODY_RATIO correctly absent from the module.
- detect_smt_divergence() return-type change is clean: tuple on match, None on all filter
  paths. Both callers (screen_session, run_backtest IDLE) are correctly updated.
- sweep/miss sign is always >= 0 for both directions (mathematically guaranteed by the
  divergence conditions).
- MIN_SMT_SWEEP_PTS=0 and MIN_SMT_MISS_PTS=0 correctly disable their guards via
  `if constant > 0` (not `if constant`), so zero is the clean disabled value.
- MAX_TDO_DISTANCE_PTS uses strictly-greater-than (`> MAX`), meaning distance equal to the
  ceiling passes (consistent with MIN_TDO_DISTANCE_PTS which uses strictly-less-than).
- reentry_count increments before assignment, so first trade correctly gets sequence=1.
- Day-boundary reset: divergence_bar_idx/pending_smt_sweep/pending_smt_miss are not reset
  at day-end (line 1011-1015), but they are reset at the start of each new day
  (line 837-841) so no stale-state bug results.
- _build_trade_record() correctly uses position.get() with safe defaults for all 6 new
  fields, so records from code paths that do not set them remain valid.
- No security issues, SQL injection vectors, or exposed secrets.
- No print statements added to production paths.
