# Code Review: SMT Bar-by-Bar State Machine Refactor (Phase 2)

**Date**: 2026-04-16
**Reviewer**: Claude Code (ai-dev-env:code-review)
**Branch**: master (unstaged changes)

## Stats

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: +850 (approx)
- Deleted lines: -442 (approx)

## Test Results

- 68 tests in test_smt_strategy.py + test_smt_backtest.py: **68 passed**
- Full suite (417 passed, 1 failed, 10 skipped): the 1 failure (`test_process_managing_exit_tp` in `test_signal_smt.py`) is **pre-existing** — confirmed by stash test.

---

## Issues Found

---

```
severity: medium
file: train_smt.py
line: 794-807 (WAITING_FOR_ENTRY block) and 809-822 (REENTRY_ELIGIBLE block)
issue: Blackout filter not applied to confirmation bar timestamps in WAITING_FOR_ENTRY and REENTRY_ELIGIBLE states
detail: The blackout filter (SIGNAL_BLACKOUT_START/SIGNAL_BLACKOUT_END) is only checked in the IDLE
        state block (line 831-833), where it gates divergence *detection*. Once state transitions to
        WAITING_FOR_ENTRY or REENTRY_ELIGIBLE, no blackout check is applied before calling
        is_confirmation_bar(). This means: if a divergence fires at 10:59 (one minute before the
        11:00 blackout), the confirmation bar at 11:05 is inside the blackout window yet a trade
        is taken.

        By contrast, screen_session() explicitly checks entry_time in the blackout window and uses
        `break` to abandon the entry (line 437-439). This creates a behavioral discrepancy between
        the live-trading shim and the backtest engine for setups that straddle the blackout boundary.

        With the current default SIGNAL_BLACKOUT_START="11:00", this is a live scenario. The
        backtest will show more trades than screen_session would have taken, making the backtest
        results not reproducible by live trading.
suggestion: In WAITING_FOR_ENTRY and REENTRY_ELIGIBLE, add a blackout check on `ts` before calling
            _build_signal_from_bar. Example in WAITING_FOR_ENTRY block:
                if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                    t = ts.strftime("%H:%M")
                    if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                        state = "IDLE"   # or: continue to abandon this pending divergence
                        pending_direction = None
                        anchor_close = None
                        continue
            Whether to silently drop the pending setup (transition to IDLE) or just skip the bar
            (stay WAITING_FOR_ENTRY) is a strategy design decision — but the behavior must be made
            explicit and consistent with screen_session.
```

---

```
severity: low
file: train_smt.py
line: 506-512
issue: manage_position() docstring references deprecated constants BREAKEVEN_TRIGGER_PTS and TRAIL_AFTER_BREAKEVEN_PTS
detail: The docstring still reads:
            "If BREAKEVEN_TRIGGER_PTS > 0 and price has moved that many points in our favor..."
            "If TRAIL_AFTER_BREAKEVEN_PTS > 0, stop trails behind best_price instead."
        Both constants are now frozen at 0.0 below the DO NOT EDIT boundary and are no longer used
        by manage_position(). The implementation correctly uses BREAKEVEN_TRIGGER_PCT, but the
        docstring describes the old behavior, which could mislead future readers or agents about
        what the function actually does.
suggestion: Replace the Breakeven/trailing stop section of the docstring with:
                "Breakeven (progress-based):
                    If BREAKEVEN_TRIGGER_PCT > 0 and price has traveled that fraction of
                    |entry - TDO| in the favorable direction, stop_price is moved to entry_price
                    (breakeven). Stop only ever tightens, never widens."
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 910-935
issue: test_no_reentry_when_move_exceeds_threshold does not verify its stated behavior
detail: The test is titled "Stop-out after move > REENTRY_MAX_MOVE_PTS -> no second trade." but the
        test body's own comment (lines 927-934) acknowledges the synthetic data does NOT produce a
        move exceeding the threshold — the short stop-out bar closes at base (doji), giving
        move = (base-2) - base = -2, which is LESS than the 5.0 threshold. The assertion is
        therefore just `assert "total_trades" in stats` — a smoke test that does not exercise the
        intended code path. The REENTRY_MAX_MOVE_PTS >= threshold blocking path has no meaningful
        test coverage.
suggestion: Build synthetic bars where the stop-out bar has a CLOSE significantly BELOW entry for a
            short (favorable progress then reversal), so move = entry - exit_close > REENTRY_MAX_MOVE_PTS.
            Then assert stats["total_trades"] == 1 (no re-entry). The _make_reentry_session_bars()
            helper would need a variant where closes[9] is well below entry (e.g., base - 50).
```

---

## Pre-existing Failures

```
file: tests/test_signal_smt.py
test: test_process_managing_exit_tp
status: FAILED before this changeset (confirmed via git stash)
note: AssertionError on MANAGING state string check — unrelated to Phase 2 changes.
```

---

## Items Verified Correct

- **State machine transitions**: IDLE -> WAITING_FOR_ENTRY -> IN_TRADE -> (exit) -> IDLE/REENTRY_ELIGIBLE transitions are correct. No double-close possible (session_close + in-loop close).
- **Day boundary resets**: WAITING_FOR_ENTRY and REENTRY_ELIGIBLE both reset to IDLE at the start of each new trading day (line 734-737). Positions in IN_TRADE correctly carry across days.
- **entry_bar_count stale state**: only incremented inside IN_TRADE; cannot be stale across days since non-IN_TRADE state cannot carry.
- **BREAKEVEN_TRIGGER_PCT logic**: progress = (entry - Low) / tdo_dist for shorts; tightens stop only (min(stop, entry)); sets breakeven_active flag. Correct for all tested cases including same-bar breakeven + stop.
- **REENTRY_ELIGIBLE anchor reset**: anchor_close is overwritten with the stop-out bar's Close — intentional fresh anchor for confirmation. Timing is correct (state transitions after current bar, next iteration starts on next bar).
- **Re-entry with breakeven_active**: bypasses move check and goes directly to REENTRY_ELIGIBLE. Correct per spec.
- **find_anchor_close()**: backward scan from bar_idx inclusive; returns float of first qualifying bar's close; returns None on no match. Correct.
- **is_confirmation_bar()**: single-bar check; short = bearish (close < open) AND high > anchor_close; long = bullish (close > open) AND low < anchor_close. Correct.
- **_build_signal_from_bar()**: applies TDO_VALIDITY_CHECK, MIN_TDO_DISTANCE_PTS, MIN_STOP_POINTS guards in correct order. divergence_bar/entry_bar set to -1 (schema compat). Correct.
- **screen_session() retention**: correctly kept as compatibility shim for signal_smt.py (signal_smt.py confirmed present). Deviates from plan Task 2.3 but is intentional and justified.
- **detect_smt_divergence arg order**: (mes_reset, mnq_reset, ...) consistent across run_backtest IDLE block and screen_session.
- **Subprocess in _write_results_tsv**: uses a fixed argument list `["git", "log", "--format=%h", "-1"]` — no injection risk.
- **No eval/exec/dynamic imports**: confirmed absent.
- **session_close priority**: only overrides "hold" result; stop/tp exits take priority. MAX_HOLD_BARS exit_time also takes priority over session_close (same PnL calculation, functionally equivalent).
- **BREAKEVEN_TRIGGER_PTS / TRAIL_AFTER_BREAKEVEN_PTS**: correctly frozen below DO NOT EDIT boundary at 0.0 with deprecation comment.
- **New constants**: REENTRY_MAX_MOVE_PTS, BREAKEVEN_TRIGGER_PCT, MAX_HOLD_BARS all in editable section with correct defaults and optimizer search space comments.
