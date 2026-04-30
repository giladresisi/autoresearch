# Code Review: SMT Strategy Refactor (train_smt.py split)

**Date:** 2026-04-18
**Branch:** master (pre-commit)

## Stats

- Files Modified: 7 (signal_smt.py, diagnose_bar_resolution.py, program_smt.md, PROGRESS.md, tests/test_signal_smt.py, tests/test_smt_backtest.py, tests/test_smt_strategy.py)
- Files Added: 2 (strategy_smt.py, backtest_smt.py)
- Files Deleted: 1 (train_smt.py)
- New lines: ~309
- Deleted lines: ~1456

## Test Results

All 112 tests pass: `pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_signal_smt.py` — 112 passed, 1 warning.

---

## Issues Found

---

```
severity: medium
file: backtest_smt.py
line: 17
issue: BREAKEVEN_TRIGGER_PCT and TRAIL_AFTER_TP_PTS imported but never used in backtest_smt
detail: Line 17 imports BREAKEVEN_TRIGGER_PCT and TRAIL_AFTER_TP_PTS from strategy_smt into
  backtest_smt's namespace. Neither constant is read by any function in backtest_smt — they
  are only read by manage_position() which lives in strategy_smt and reads them from strategy_smt's
  own namespace. The unused import creates a misleading alias in backtest_smt: an optimizer
  agent scanning backtest_smt for tunable constants will see these two names and may incorrectly
  conclude that patching backtest_smt.BREAKEVEN_TRIGGER_PCT affects manage_position. It does not
  — manage_position uses the strategy_smt binding.
suggestion: Remove BREAKEVEN_TRIGGER_PCT and TRAIL_AFTER_TP_PTS from the import on line 17.
  If their presence in backtest_smt's namespace is intentional for autoresearch inspection
  purposes, add a comment explaining why.
```

---

```
severity: low
file: strategy_smt.py
line: 136-145
issue: _mnq_bars and _mes_bars globals are set by set_bar_data() but never read
detail: set_bar_data() populates module-level globals _mnq_bars and _mes_bars (lines 137-138,
  141-145). No function in strategy_smt.py reads these globals — a full text search
  confirms they only appear in their declaration and in set_bar_data. The globals and
  set_bar_data are exported and called by signal_smt (on_mnq_1m_bar, on_mes_1m_bar) and
  by run_backtest, but the values are never consumed. This is dead state — stored but unused.
  Not a runtime bug, but confusing and implies planned-but-unimplemented functionality.
suggestion: Either document that _mnq_bars/_mes_bars are reserved for a future strategy
  function (add a TODO comment), or remove them if no such function is planned. If set_bar_data
  is only called for its side-effects on future code, a comment in set_bar_data's docstring
  should say so explicitly.
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 1
issue: Module docstring still references the deleted train_smt.py
detail: The file docstring reads "Unit tests for SMT strategy functions in train_smt.py."
  train_smt.py has been deleted and split into strategy_smt.py and backtest_smt.py.
  This is a stale reference that will mislead future maintainers.
suggestion: Update to "Unit tests for SMT strategy functions in strategy_smt.py."
```

---

## Architecture Observations (no action required)

**Namespace isolation is correct for the tests.** The dual-module patching pattern is implemented correctly:

- Constants consumed by `manage_position` (`BREAKEVEN_TRIGGER_PCT`, `TRAIL_AFTER_TP_PTS`, `MIN_STOP_POINTS`, `TDO_VALIDITY_CHECK`, etc.) are patched on `strategy_smt` — this is correct because `manage_position` reads them from `strategy_smt`'s own namespace.
- Constants consumed by `run_backtest` (`TRADE_DIRECTION`, `SIGNAL_BLACKOUT_*`, `ALLOWED_WEEKDAYS`, `REENTRY_MAX_MOVE_PTS`, `MAX_HOLD_BARS`, `MAX_REENTRY_COUNT`, `MIN_PRIOR_TRADE_BARS_HELD`, `SESSION_START`, `SESSION_END`) are patched on `backtest_smt` — this is correct because `run_backtest` reads them from the `from strategy_smt import X` bindings it holds in backtest_smt's namespace.

**`from strategy_smt import X` binding semantics are handled correctly.** After the split, `backtest_smt` has its own module-level binding for each imported constant. Patching `strategy_smt.X` after import does NOT affect `backtest_smt.X` — and all tests patch the right module for the right function. No test patches the wrong side.

**`signal_smt.py` test patching is correct.** The switch from `train_smt.*` to `signal_smt.*` patch targets is correct: `signal_smt` imports `screen_session`, `manage_position`, `compute_tdo` with `from strategy_smt import ...`, so patching `signal_smt.screen_session` (the local binding) is the right target for tests that want to intercept calls made from within `signal_smt`.

**`diagnose_bar_resolution.py` alias change is correct.** It only uses `load_futures_data`, `run_backtest`, `BACKTEST_START`, and `BACKTEST_END` — all present in `backtest_smt` (load_futures_data is re-exported via import from strategy_smt).

**`set_bar_data` call ordering in bar callbacks is safe.** Both `on_mnq_1m_bar` and `on_mes_1m_bar` update their respective DataFrame before calling `set_bar_data`. Because `main()` calls `_load_parquets()` and `_gap_fill_1m()` before connecting to IB, neither `_mnq_1m_df` nor `_mes_1m_df` can be None when callbacks fire.

**`exit_market` branch in `_process_managing` is correctly placed.** It handles a previously-unhandled return value from `manage_position` without breaking the existing exit_tp / exit_stop / exit_session_end paths.
