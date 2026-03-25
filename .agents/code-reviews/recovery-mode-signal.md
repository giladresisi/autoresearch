# Code Review: Recovery Mode Signal Path

**Date**: 2026-03-25
**Plan**: `.agents/plans/recovery-mode-signal.md`

## Stats

- Files Modified: 6 (train.py, screener.py, screener_prepare.py, tests/test_screener.py, tests/test_screener_script.py, tests/test_e2e.py)
- Files Added: 0
- Files Deleted: 0
- New lines: +350
- Deleted lines: -18

## Test Results

All 42 tests in `tests/test_screener.py` and `tests/test_screener_script.py` pass.

---

## Issues Found

---

```
severity: high
file: screener.py
line: 73-84
issue: _rejection_reason() never enters the recovery path, so recovery-passing tickers get misdiagnosed as "sma_misaligned"
detail: _rejection_reason() mirrors screen_day() for diagnostic purposes. At line 73, it checks the old bull-only SMA condition
        (price_1030am <= sma50 OR ... sma50 <= sma100). A recovery ticker by definition fails this bull check, so the
        function enters the if-branch at line 73. Inside that branch, it only checks for "death_cross" and then returns
        "sma_misaligned". It never checks whether the recovery path conditions are met. When screen_day() returns a signal
        via the recovery path, any call to _rejection_reason() on the same ticker (which should be unreachable in normal flow,
        but is reachable during debugging, unit tests, or audit runs) returns "sma_misaligned" instead of "unknown".
        This means the diagnostic rejection counter in run_screener() would over-count "sma_misaligned" if there's ever
        a code path that calls _rejection_reason() on a passing ticker. Verified by running:
          _rejection_reason(recovery_df, today, 173.0) → 'sma_misaligned'  (wrong; should be 'unknown')
suggestion: In _rejection_reason(), after the existing sma_misaligned/death_cross block, add a recovery_path check
        parallel to what screen_day() does. If the recovery conditions are met, continue down the filter chain rather
        than returning "sma_misaligned". Alternatively, add a guard at the call site in run_screener() to only call
        _rejection_reason() when signal is None — which is already the case, but makes the correctness requirement explicit.
        The safest targeted fix is to exit the SMA block early (without returning) when the recovery path conditions are met,
        then continue to check the remaining filters at the correct RSI range (40-65).
```

---

```
severity: high
file: screener.py
line: 108
issue: _rejection_reason() uses hardcoded bull-mode RSI range (50-75) instead of path-aware range
detail: After screen_day() was updated to use rsi_lo, rsi_hi = (40, 65) if recovery else (50, 75),
        _rejection_reason() was not updated. It still uses the hardcoded check:
          if not (50 <= rsi <= 75): return "rsi_out_of_range"
        A recovery-path ticker with RSI 42 would correctly pass screen_day() but _rejection_reason()
        would return "rsi_out_of_range" for it (misdiagnosis). The diagnostic counts shown at the end
        of run_screener() output would be wrong for recovery-mode tickers with RSI 40-49.
        Since _rejection_reason() is only called on tickers where signal is None, this does not affect
        trading decisions — but it corrupts the rejection histogram, which is used to tune the screener.
suggestion: Apply the same path-aware RSI logic to _rejection_reason(). This requires the function to
        determine signal_path (bull vs recovery) before the RSI check. A minimal fix: compute sma200,
        determine recovery_path, and use rsi_lo, rsi_hi = (40, 65) if recovery_path else (50, 75).
```

---

```
severity: medium
file: screener.py
line: 257
issue: Inline comment has incorrect description of "bull" and "recovery" signal paths
detail: The comment reads:
          # signal_path distinguishes bull (price>SMA200) from recovery (price near SMA200)
        This is factually wrong. Bull mode requires price > SMA50 (and price > SMA100, SMA20 > SMA50 > SMA100).
        Recovery mode is price <= SMA50 but price > SMA20 (with SMA50 > SMA200 as guard). Neither path is
        defined by price vs SMA200. This comment will mislead anyone reading the code or using it as a
        maintenance reference.
suggestion: Change to:
          # signal_path distinguishes bull (price > SMA50 full stack) from recovery (price below SMA50, within uptrend)
```

---

```
severity: medium
file: tests/test_screener.py
line: 449-459
issue: test_screen_day_recovery_rsi_range_40_65 is a vacuous test — the RSI gate is never actually exercised
detail: The test forces RSI up by setting df.iloc[-60:-1, 'close'] = linspace(200, 300, 59).
        This drives SMA20 to ~283, but price_1030am=173.0 < SMA20=283, so screen_day() rejects on the
        recovery condition "price > SMA20" check (Rule 1), not the RSI gate. screen_day() returns None.
        The test's assertion block is:
          if result is not None:
              assert result.get("signal_path") != "recovery" or result.get("rsi14", 100) <= 65
        Since result is None, this `if` block is never entered. The test always passes unconditionally
        without exercising the RSI ceiling at all. Verified by running the fixture and observing
        screen_day returning None due to sma20 gate, not rsi gate.
suggestion: Redesign the fixture to produce high RSI while keeping price_1030am > sma20 and <= sma50.
        One approach: apply the price increase only to bars in the RSI-14 window (last 14-15 bars of
        history) rather than the full 60 bars, so sma20 stays low enough. Then assert result is None
        (unconditionally) with an assertion message confirming it's the RSI gate. To verify it's the RSI
        gate rather than another gate, use _rejection_reason() in a separate assertion.
```

---

```
severity: low
file: tests/test_screener_script.py
line: 248-249
issue: Stale comment references wrong breakout_price value and wrong fixture price path
detail: The comment says:
          # Inject breakout price — must be > 20-day high of all 260 hist bars (~169.5) and <= SMA50 (~174.7)
          # With prev_close ~168.6, a breakout_price of 171 gives gap_pct ≈ +1.4%, above -10% threshold
        "all 260 hist bars" is misleading: high20 is computed from the last 20 history bars only (not
        all 260). The actual breakout_price=171.0 is different from the unit tests (173.0), with no
        explanation of why. The comment says SMA50 ~174.7 but it is actually ~174.47 — close enough,
        but the comment was copied from the original plan draft which used a different price path.
        This is a cosmetic accuracy issue, not a bug.
suggestion: Update comment to say "20-day high of the last 20 history bars" and optionally note that
        171.0 was chosen to be between high20 (169.50) and SMA50 (174.47), ensuring it is a valid
        recovery breakout price.
```

---

```
severity: low
file: screener_prepare.py
line: 28-29
issue: Multi-line constant comment uses inconsistent indentation (leading spaces vs aligned)
detail: The continuation line of the comment uses a leading space to align under the first line,
        but the style is not consistent with other multi-line comments in the file which use
        a standard trailing-comment format. Minor style issue.
suggestion: Use a standard inline comment or a separate comment line:
          HISTORY_DAYS = 300  # calendar days; ~210 trading days; 300 needed for SMA200 (200 td × 365/252 ≈ 290)
```

---

## Summary

The trading logic in `train.py` (`screen_day()`) is correct and well-structured. The two-path (bull/recovery) design is clean, the SMA200 guard is properly nil-safe, and the RSI and slope_floor adjustments for recovery mode are consistent with the plan spec.

The two **high** severity issues are both in `_rejection_reason()` in `screener.py`. They do not affect trading correctness (signals fired, stops set, entries armed) because `_rejection_reason()` is only called after `screen_day()` returns `None`. However, they make the rejection diagnostics printed at the end of every screener run systematically wrong for recovery-path tickers — `sma_misaligned` will be over-counted and `rsi_out_of_range` may be mis-attributed. Since these counts are used to tune the screener and understand where candidates are being rejected, the corruption is meaningful in practice.

The **medium** severity test issue (`test_screen_day_recovery_rsi_range_40_65`) means the recovery RSI ceiling (65) has no effective automated test. A future change that accidentally widens the recovery RSI range to 75 would not be caught.

No security issues, no SQL injection surface, no hardcoded credentials.
