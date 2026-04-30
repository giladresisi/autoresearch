# Code Review: uncap-hold-time

**Plan**: `.agents/plans/uncap_hold_time.md`
**Reviewer**: ai-dev-env:code-review
**Date**: 2026-04-22

---

## Stats

- Files Modified: 4 (strategy_smt.py, backtest_smt.py, tests/test_smt_strategy.py, tests/test_smt_backtest.py)
- Files Added: 0
- Files Deleted: 0
- New lines: +514 (code files only, excluding plan/docs)
- Deleted lines: -56

---

## Test Results

164 passed, 1 failed (pre-existing — see below).
All 15 new tests (T1–T13 unit, T14/T26, T15/T27 integration) pass.

---

## Pre-existing Failures

**`test_reentry_after_stop`** — pre-existing test data bug, NOT introduced by this changeset.

Root cause: The test's `_make_reentry_session_bars()` places the stop-out trigger at `mnq_highs[9] = base+40`. The entry is taken on bar 9 (entry_bar=9). `manage_position` is first called on bar 10 onwards, where `mnq_high = base+5 = 20005`, which never exceeds `stop = 20033.65`. The position runs to session close rather than stop-out, so `result == "session_close"` (not `"exit_stop"`), and re-entry eligibility is never set.

This failure existed before this changeset: the prior HEAD had `NameError: PARTIAL_EXIT_LEVEL_RATIO` in `run_backtest`, which masked this bug by crashing the test entirely. This changeset added the missing import, unmasking the pre-existing test data flaw. The changeset itself did not cause or worsen it.

---

## Issues Found

---

```
severity: medium
file: strategy_smt.py
line: 1487-1493
issue: TDO-crossing bar that simultaneously breaches the original stop returns "hold" instead of "exit_stop"
detail: When a bar first crosses TDO (tp_breached was False), the trail block sets tp_breached=True,
        updates best_after_tp, and returns "hold" immediately — before the stop check at line 1606 runs.
        If the same bar's low (long) or high (short) also penetrates the original stop_price, the stop
        is silently missed. Example: long entry=100, stop=97, TDO=110; bar high=115, low=94.
        manage_position returns "hold" even though low=94 < stop=97.
        With TRAIL_AFTER_TP_PTS=1.0 (old default), this was moot because the crossing bar tightened the
        stop to TDO+1 (~111), so any subsequent bar below 111 would exit quickly. With the new
        TRAIL_AFTER_TP_PTS=50.0 and deferred stop placement, the original loose stop (97) remains
        active, and the 1-bar delay on an intrabar crash scenario becomes meaningful.
suggestion: Before the "return hold" on the first crossing, check the stop exit condition:
            if direction == "long" and current_bar["Low"] <= position["stop_price"]:
                return "exit_stop"
            if direction == "short" and current_bar["High"] >= position["stop_price"]:
                return "exit_stop"
            position["tp_breached"] = True
            ...
            return "hold"
```

---

```
severity: low
file: backtest_smt.py
line: 633
issue: TRAIL_AFTER_TP_PTS == 0 guard is a fragile float-equality comparison
detail: The condition `if TRAIL_AFTER_TP_PTS == 0:` compares a float to an integer 0.
        While safe for the clean sentinel values used (0.0, 50.0, etc.), a floating-point
        optimizer value like 0.0001 would silently enable trail mode for partial exits while
        being functionally disabled in strategy logic (where the > 0 check fires). This
        creates a gap: trail block in manage_position uses `TRAIL_AFTER_TP_PTS > 0`,
        partial block in backtest uses `TRAIL_AFTER_TP_PTS == 0`. They are logically
        complementary but syntactically inconsistent — the two guards should use the same idiom.
suggestion: Change line 633 to `if TRAIL_AFTER_TP_PTS <= 0:` to match the `> 0` pattern
            used in manage_position (line 1466) and eliminate the float-equality fragility.
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 1537 (new section)
issue: _make_full_position duplicates _make_position without extending it, creating two diverging helpers
detail: _make_position (line 338) is a 4-field minimal helper. _make_full_position is a comprehensive
        25-field helper for the same purpose. The new tests correctly require the extra fields, so
        _make_full_position is justified. However, _make_position is now stale for any test that
        exercises code paths touching secondary_target, layer_b_entered, contracts, etc. Future test
        authors must choose between two helpers without clear guidance on which to use.
suggestion: Add a docstring to _make_full_position noting it is the preferred helper for
            manage_position tests requiring trail/partial/layer-B fields, and that _make_position
            is retained for legacy tests only. No code change required — documentation only.
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 1572 (T27 assertion)
issue: T27 (test_trail_mode_partial_slides_stop_no_contract_reduction) does not assert that the stop slid
detail: The test verifies that no partial_exit trade record is appended (correct) and that the
        eventual exit is exit_stop or session_close (correct). But it does not assert that
        position["stop_price"] advanced from the initial stop to near the partial level as a
        result of the stop-slide. If PARTIAL_STOP_BUFFER_PTS were accidentally removed from the
        stop-slide formula, the test would still pass.
suggestion: After run_backtest, check that the exit_price of the surviving trade is above
            the initial stop (confirming the stop slid from 94 to near the partial level ~20018).
            Example: assert trades[0]["exit_price"] > <initial_stop>, "stop-slide must have fired"
```

---

## Logic Correctness Assessment

**Never-widen rule**: Correct. Long uses `max(current_stop, new_stop)` (stop only moves up, tightening a long). Short uses `min(current_stop, new_stop)` (stop only moves down, tightening a short). The comments say "never widen" and the operators implement exactly that.

**Activation threshold**: Correct. For longs: `best - tp >= activation_dist` where `activation_dist = TRAIL_ACTIVATION_R * initial_stop_pts`. For shorts: `tp - best >= activation_dist`. At `TRAIL_ACTIVATION_R=0.0`, `activation_dist=0.0` and the condition is always true (immediate activation, back-compat). Edge case when `initial_stop_pts` is absent from position dict: `position.get("initial_stop_pts", 0.0)` defaults to 0.0, meaning immediate activation — the safe fallback documented in the plan.

**Deferred stop on crossing bar**: Correct as designed. The crossing bar sets `tp_breached=True` and `best_after_tp` without touching `stop_price`, then returns "hold". Subsequent bars enter the `tp_breached=True` branch where activation is checked. The issue flagged above (medium) is an edge case where the crossing bar's body simultaneously crosses the original stop — this 1-bar delayed exit was also present in the old code (which also returned "hold" on the crossing bar) but has wider impact now because the trail stop is no longer tightened on that bar.

**Partial block restructure**: Correct. Stop-slide runs unconditionally (line 624–631), then `if TRAIL_AFTER_TP_PTS == 0:` gates contract reduction. The `continue` at line 670 is outside the inner `if` block, so it always fires — the loop correctly stays IN_TRADE regardless of trail mode.

**`initial_stop_pts` computation**: `round(abs(entry_price - round(stop_price, 4)), 4)` — stop_price is independently rounded in the dict, so this is effectively `round(abs(entry - stop), 4)`. Correct. No double-rounding issue.

**`TRAIL_AFTER_TP_PTS` binding in backtest_smt.py**: The new import `from strategy_smt import TRAIL_AFTER_TP_PTS` binds the name at import time. Integration tests correctly patch both `_strat.TRAIL_AFTER_TP_PTS` and `train_smt.TRAIL_AFTER_TP_PTS`. Production optimizer must likewise update both modules' bindings if it modifies the constant after import.
