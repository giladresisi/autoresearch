# Code Review: SMT Direction Control

**Date:** 2026-04-01
**Plan:** `.agents/plans/smt-direction-control.md`
**Reviewer:** Claude Sonnet 4.6

---

## Stats

- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: +398
- Deleted lines: -20

---

## Test Suite

**360 passed, 2 skipped, 0 failed.** All pre-existing tests pass. The 2 skipped tests are pre-existing (confirmed in execution report baseline).

---

## Issues Found

---

```
severity: medium
file: tests/test_smt_strategy.py
line: 653–663
issue: test_min_stop_points_zero_disables_guard uses `assert True` — assertion never fails
detail: The test body is `assert True`, which passes unconditionally regardless of whether the
        signal was produced or filtered. The stated intent ("signal should come through") is not
        verified. If a future change causes screen_session to return None even with
        MIN_STOP_POINTS=0.0, this test will still pass and hide the regression.
        (Runtime-confirmed: the signal DOES come through with the current code — the test is just
        not asserting it.)
suggestion: Replace `assert True  # no exception = pass` with `assert result is not None`, since
            the test data and monkeypatches guarantee a signal when the guard is disabled.
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 668–697
issue: Conditional assertions in stop-ratio tests silently pass if result is None
detail: Both test_long_stop_ratio_applied (line 679) and test_short_stop_ratio_applied (line 695)
        gate the actual stop-price assertion on `if result is not None and result["direction"] == ...`.
        If screen_session returns None for any reason, the ratio assertion never executes and the
        test reports as passed. This could conceal a regression in the stop ratio computation.
        Runtime analysis shows both tests DO produce signals under the autouse patch_min_bars
        fixture (MIN_BARS_BEFORE_SIGNAL=2), so the assertions currently execute. The guard is
        not currently hiding anything, but it reduces future regression detection reliability.
suggestion: Add `assert result is not None, "Expected signal with LONG_STOP_RATIO=0.3"` (and
            similarly for short) before the conditional, so a missing signal is an explicit failure
            rather than a silent skip.
```

---

```
severity: low
file: train_smt.py
line: 98 / 262–293
issue: PRINT_DIRECTION_BREAKDOWN constant is defined but referenced nowhere in production code
detail: The constant `PRINT_DIRECTION_BREAKDOWN = True` is defined in the STRATEGY TUNING section
        and documented as "caller's responsibility to check before calling". However, no caller
        in this file checks it — print_direction_breakdown() is not called anywhere in train_smt.py,
        and the frozen __main__ block (which cannot be modified) does not call it either.
        The constant has no runtime effect in the current codebase. Autoresearch agents reading
        fold output will not see direction breakdown unless they explicitly call the function.
        This is documented in the plan as intentional design, but the constant's value cannot
        influence any behavior without an external caller.
suggestion: Either (a) accept the design as-is (the constant is a signal to external callers),
            or (b) add a note in the constant's comment that it requires an external script to
            import train_smt and call print_direction_breakdown() when the constant is True.
            No code change required if the design is intentional.
```

---

```
severity: low
file: train_smt.py
line: 279–293
issue: print_direction_breakdown prints zero-count blocks for directions with no trades
detail: When trades exist for one direction only, the function still prints the block for the
        other direction with `n=0`, `win_rate=0.0`, `avg_pnl=0.0`, and no exit breakdown lines.
        Example: with only long trades, output includes `short_trades: 0`, `short_win_rate: 0.0`,
        `short_avg_pnl: 0.0`. This may confuse autoresearch agents that parse win_rate=0.0 as
        a meaningful signal rather than "no trades in this direction".
        The plan docstring says "prints nothing if trade_records is absent or empty" — which is
        true at the top level but not per-direction.
suggestion: Add `if n == 0: continue` after `n = len(subset)` to skip directions with no trades,
            or update the docstring to explicitly state that zero-count direction blocks are printed.
```

---

## Positive Findings

**Direction filter placement is correct.** The `continue` after the direction filter stays inside
the `for bar_idx` scan loop, so a filtered signal at bar N does not prevent discovery of a valid
signal at bar N+1. This is the right design for multi-bar scanning.

**TDO validity gate handles the zero-distance edge case correctly.** When `TDO_VALIDITY_CHECK=True`,
the gate (`tdo <= entry_price` for long, `tdo >= entry_price` for short) blocks the case where
entry equals TDO exactly. When `TDO_VALIDITY_CHECK=False`, the `MIN_STOP_POINTS` guard catches the
resulting zero-distance stop. No divide-by-zero risk.

**Guard ordering is correct.** Direction filter → TDO validity → stop computation → MIN_STOP_POINTS
is the optimal sequence: cheapest filters first, expensive computation deferred.

**_bars_for_minutes is interval-agnostic and correct.** At 1m: 5 minutes = 5 bars. At 5m: 5 minutes
= 1 bar. The `round()` is appropriate for non-integer ratios and `max(1, ...)` prevents a zero
threshold.

**All 14 new tests verify distinct code paths** and use appropriate monkeypatching to isolate each
guard. The autouse `patch_min_bars` fixture (MIN_BARS_BEFORE_SIGNAL=2) is correctly applied to all
new tests, ensuring the divergence at bar 4 is reachable by the scan loop.

**No security issues, SQL injection, or hardcoded secrets** were found. No production print() calls
were added (print_direction_breakdown is an explicit utility function, not a production logging path).
