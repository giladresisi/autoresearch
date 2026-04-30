# Code Review: limit order entry at anchor_close price

**Date:** 2026-04-22
**Branch:** master
**Focus:** State machine transitions, fill logic, bar_seconds detection, expiry calculation, test coverage quality

## Stats

- Files Modified: 4 (backtest_smt.py, strategy_smt.py, tests/test_smt_backtest.py, tests/test_smt_strategy.py)
- Files Added: 0
- Files Deleted: 0
- New lines: ~684 (277 bk + 40 strat + 241 test_bk + 126 test_strat)
- Deleted lines: ~65

---

## Issues Found

---

### Issue 1

```
severity: high
file: tests/test_smt_backtest.py
line: 1036 (_setup_limit_patches) and 1150 (test_bar_seconds_detected_1s)
issue: test_bar_seconds_detected_1s fails with a test-ordering dependency due to missing bk.MIN_BARS_BEFORE_SIGNAL patch
detail: When pytest runs tests from test_smt_strategy.py before test_smt_backtest.py::test_bar_seconds_detected_1s (T16), the autouse fixture `patch_min_bars` in test_smt_strategy.py patches `strategy_smt.MIN_BARS_BEFORE_SIGNAL = 2`. If backtest_smt is imported for the first time WHILE that fixture is active (i.e., during the first test function in test_smt_strategy.py that calls `import backtest_smt as _bk`), Python's `from strategy_smt import MIN_BARS_BEFORE_SIGNAL` reads the value 2 at import time and binds `backtest_smt.MIN_BARS_BEFORE_SIGNAL = 2`. After the test, monkeypatch restores `strategy_smt.MIN_BARS_BEFORE_SIGNAL = 0`, but `backtest_smt.MIN_BARS_BEFORE_SIGNAL` remains 2 for the rest of the pytest session. When T16 runs its 11 one-second bars (09:00:00–09:00:10), run_backtest computes `min_signal_ts = 09:00:00 + 2 minutes = 09:02:00`, which is after all bars. No signals are generated, producing 0 trade records and failing both assertions. Confirmed by debug output: `[DBG] day=2025-01-02 n_bars=11 MIN_BARS=2 min_signal_ts=2025-01-02 09:02:00-05:00`.
suggestion: Add `monkeypatch.setattr(bk, "MIN_BARS_BEFORE_SIGNAL", 0)` to _setup_limit_patches (between lines 1065–1066) so that all limit-entry integration tests (T11–T19) explicitly set bk.MIN_BARS_BEFORE_SIGNAL to 0. Alternatively, extend the T16 1-second session to start at 09:00:00 and end at 09:05:00 (300+ bars) so that bars 7 and 8 fall after the 2-minute threshold. The first approach is more robust.
```

---

### Issue 2

```
severity: medium
file: backtest_smt.py
line: 1057 (WAITING_FOR_LIMIT_FILL state handler)
issue: _limit_bars_elapsed is incremented before the fill check, making same-session fill count off-by-one semantics ambiguous
detail: At the top of the WAITING_FOR_LIMIT_FILL block, `_limit_bars_elapsed += 1` runs unconditionally. Then the fill check is evaluated. This means: on the very first forward-bar the counter is already 1 when `pending_limit_signal["limit_fill_bars"] = _limit_bars_elapsed` is assigned. T11 asserts `limit_fill_bars == 1` for a fill on bar N+1 (the first forward bar), which matches this behavior. However, the counter starts at 0 when WAITING is entered (line 548) and increments to 1 at the top of the next bar. If the fill happens on bar N+1 (the bar immediately after the confirmation bar), `limit_fill_bars = 1`, which is correct by the test's definition. But the expiry check `_limit_bars_elapsed >= _limit_max_bars` (line 1110) fires after the increment, meaning a 1-bar window (`max_limit_bars=1`) expires on bar N+1 if no fill — the same bar where a fill is also possible. The fill check runs first (line 1071) before the expiry check (line 1110), so a fill on bar N+1 with max_limit_bars=1 would succeed. This is correct, but the behavior is subtle. No bug exists; this is an observation.
suggestion: Add a comment at line 1057 explaining the increment-then-check ordering: "Increment first so limit_fill_bars=1 for fills on the immediate next bar (N+1). Fill check precedes expiry check so max_limit_bars=1 allows exactly one forward bar."
```

---

### Issue 3

```
severity: medium
file: backtest_smt.py
line: 1094 (WAITING_FOR_LIMIT_FILL fill path)
issue: DISPLACEMENT_STOP_MODE override not applied when limit order fills
detail: In the normal WAITING_FOR_ENTRY fill path (line 1039), when DISPLACEMENT_STOP_MODE is active and smt_type=='displacement', the stop_price is overridden from `_pending_displacement_bar_extreme`. In the WAITING_FOR_LIMIT_FILL fill path (line 1079–1108), no such override is applied. However, this is by design: the displacement stop IS applied at the time the signal is converted to a pending_limit_signal (lines 539–545 and 691–697). The stop_price is frozen into pending_limit_signal when the WAITING state is entered, not when it fills. This means the stop does not update if a new extreme forms during the waiting window. If price extends significantly during the wait, the stop could be stale.
suggestion: Document this design decision with a comment at line 1094: "Stop_price was frozen into pending_limit_signal when WAITING was entered (including DISPLACEMENT_STOP_MODE override). No re-application here — the stop is intentionally fixed at limit-order creation time."
```

---

### Issue 4

```
severity: low
file: tests/test_smt_strategy.py
line: 33
issue: autouse fixture `patch_min_bars` is scoped to test_smt_strategy.py but silently affects backtest_smt's module-level state when backtest_smt is first imported during a test in that file
detail: The `patch_min_bars` autouse fixture uses `monkeypatch.setattr(train_smt, "MIN_BARS_BEFORE_SIGNAL", 2)`, which only patches `strategy_smt`. However, since `backtest_smt` is imported inside test helper functions (not at module level), the first call to `import backtest_smt` may occur while the fixture is active, causing `backtest_smt.MIN_BARS_BEFORE_SIGNAL` to be bound to 2 at import time. This is the root cause of Issue 1. The autouse fixture is not the bug itself, but its interaction with lazy imports is fragile.
suggestion: Move the `import backtest_smt` statements from inside test functions to the top of test_smt_strategy.py as a module-level import, or add `import backtest_smt` to the autouse fixture before patching, so that backtest_smt is always imported before any patching occurs. This ensures the import always sees the clean (0) value of MIN_BARS_BEFORE_SIGNAL.
```

---

### Issue 5

```
severity: low
file: backtest_smt.py
line: 443
issue: bar_seconds detection uses only the first two bars of the session, which is brittle for sessions with irregular bar spacing at the open
detail: `bar_seconds = (mnq_session.index[1] - mnq_session.index[0]).total_seconds()` uses only the first two bars. If the session has an irregular gap at the open (e.g., a missed or late first bar), bar_seconds will be wrong for the entire session, computing the wrong max_limit_bars. This affects the expiry window.
suggestion: Use the median or mode of adjacent-bar deltas across the session: `pd.Series(mnq_session.index).diff().dt.total_seconds().dropna().mode()[0]`. At minimum, fall back to the mode of all bar deltas if the first two bars show an outlier gap (e.g., > 3x the expected interval).
```

---

### Issue 6

```
severity: low
file: tests/test_smt_backtest.py
line: 1099 (T13 test)
issue: T13 asserts missed_move_pts == 14.0 but the comment says "missed_move ≈ 14"
detail: T13 sets `bar9_low=base - 12` and entry_price = base + 2 (anchor_close with zero buffer). Expected missed_move = (base+2) - (base-12) = 14.0. The assertion uses `pytest.approx(14.0, rel=1e-3)`. The comment says "≈ 14" but the calculation is exact. Minor inconsistency — not a functional bug.
suggestion: Update the comment to "missed_move_pts = entry_price - low = (base+2) - (base-12) = 14.0 exactly".
```

---

## Non-Issues (Confirmed Correct)

- **Fill logic direction**: Short fills when `High >= entry_price` (correct for sell-short limit). Long fills when `Low <= entry_price` (correct for buy limit). Verified.
- **missed_move_pts direction**: Short: `entry_price - Low` (favorable downward move). Long: `High - entry_price` (favorable upward move). Both correct.
- **Limit entry buffer direction**: Short: `anchor_close - buffer` (pull-back entry below anchor). Long: `anchor_close + buffer` (pull-back entry above anchor). Symmetric and consistent with strategy intent.
- **session-end expiry handler**: Properly resets state to IDLE and clears pending_limit_signal (line 1267–1276).
- **T11–T17, T18, T19 test assertions**: All logically correct given the synthetic session structure.
- **_build_limit_expired_record**: contracts=0, pnl=0.0, entry_bar=-1 for unfilled limit. Correct.

---

## Summary

1 introduced test failure (high severity): `test_bar_seconds_detected_1s` (T16) fails when run after any test from `test_smt_strategy.py` that triggers the first import of `backtest_smt`. Fix: add `monkeypatch.setattr(bk, "MIN_BARS_BEFORE_SIGNAL", 0)` to `_setup_limit_patches`.

The production logic (state machine, fill conditions, expiry, session-end handling, bar_seconds detection) is correct. Test T16's fragility is the only blocking issue; all other findings are documentation or minor robustness suggestions.
