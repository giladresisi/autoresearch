# Code Review: SMT Solution F — Draw on Liquidity

**Plan:** `.agents/plans/smt-solution-f-draw-on-liquidity.md`  
**Date:** 2026-04-21  
**Reviewer:** Claude Sonnet 4.6 (ai-dev-env:code-review)

---

## Stats

- Files Modified: 6
- Files Added: 0
- Files Deleted: 0
- New lines: +610
- Deleted lines: -24

---

## Pre-existing Failures (not introduced by this changeset)

The following 3 tests in `tests/test_smt_structural_fixes.py` fail both with and without the current changes (verified via `git stash`):

- `test_s2_1_expanded_ref_levels_detects_prev_day_sweep` — pre-existing
- `test_s3_1_htf_visible_signal_passes_filter` — pre-existing
- `test_s3_3_htf_confirmed_timeframes_in_signal` — pre-existing

All 149 remaining tests pass with the changeset applied.

---

## Issues Found

### Issue 1 — MEDIUM

```
severity: medium
file: strategy_smt.py
line: 1407–1430
issue: Trail block sets tp_breached=True but the secondary block below (line 1433) then re-evaluates the same flag and can fire exit_secondary on the same bar where trail fired
detail: When TRAIL_AFTER_TP_PTS > 0 AND secondary_target IS None, the trail block runs (condition on line 1407 passes). The trail block returns "hold" at line 1430 when TP is first crossed. That is correct — no issue there.

However, the question was about the case where trail IS active AND secondary exists. By design (line 1407), trail is skipped when secondary_target is not None. This is correct and the gate is tight: the guard is `position.get("secondary_target") is None`, which evaluates correctly for both None and a missing key (get returns None for missing key). No logical error here.

The actual risk is the OTHER combination: TRAIL_AFTER_TP_PTS > 0 AND secondary_target IS None. In this case the trail block sets tp_breached=True at line 1423 and returns "hold" at line 1430, so the secondary block (lines 1432-1445) is NEVER reached on the same bar. Correct.

The subtle hazard: on the NEXT bar, tp_breached is already True and secondary is None. The trail block (line 1408: `if position.get("tp_breached")`) runs the trailing logic and updates stop_price. Then lines 1432-1445 check secondary (None) and skip. Then exit_tp line 1543 checks `TRAIL_AFTER_TP_PTS == 0 and position.get("secondary_target") is None` — with trail active this evaluates False (TRAIL_AFTER_TP_PTS != 0), so exit_tp is correctly suppressed. The trailing stop exit is achieved through exit_stop firing when stop catches up to price. This is the intended design.

No bug found, but the interaction is non-obvious. A comment clarifying the control flow would reduce maintenance risk.
suggestion: Add a brief comment above the trail block explaining: "Note: when secondary_target is set, trail is disabled; tp_breached is still set by the block at line 1432 for secondary target tracking."
```

### Issue 2 — HIGH

```
severity: high
file: backtest_smt.py
line: 714
issue: session_high guard uses _run_ses_high initialised to 0.0 — for a SHORT signal this means the draw is incorrectly excluded when session high is legitimately above entry but the running extreme has not yet been set
detail: _run_ses_high is initialised to 0.0 at line 439. At bar 0 of a session, _run_ses_high == 0.0. The draw-on-liquidity dict for long includes:
    "session_high": _run_ses_high if _run_ses_high > _ep + 1 else None
For a long signal, the session_high draw is suppressed when _run_ses_high == 0.0 (correct — no meaningful high established yet). However, for a SHORT signal the analogous dict (line 721) uses:
    "session_low": _run_ses_low if _run_ses_low < _ep - 1 else None
_run_ses_low is initialised to float("inf") at line 440 and correctly reflects "no bars seen yet" — this side is fine.

The risk is on the LONG side at very early bars: _run_ses_high starts at 0.0, not at a meaningful "no data" sentinel. If a long signal fires at bar 8+ (after the `bar_idx < 3` guard), _run_ses_high will have been populated with real session high values (updated at top of loop starting bar_idx=1, line 455). But at bar_idx == 3 (the first allowed bar), _run_ses_high holds bars 0-2's high — which is typically valid. So in practice the 0.0 init is safe because the draws dict for LONG is only evaluated on confirmation bars that arrive after bar 3, by which time _run_ses_high is non-zero.

The structural risk remains: if a very early confirmation bar (bar 3 exactly) fires while the session high so far is below entry+1 (e.g. tight ranging session), _run_ses_high is correctly excluded. This is semantically correct (session high not a valid draw) but the 0.0 init could mislead a future reader. If the `bar_idx < 3` guard is ever removed or relaxed, the 0.0 init becomes a latent bug that silently suppresses a draw entry.
suggestion: Change the initialisation on line 439 from `_run_ses_high = 0.0` to `_run_ses_high = float("nan")` and add a guard `if not _math.isnan(_run_ses_high) and _run_ses_high > _ep + 1` in the draws dict construction. This mirrors how _ses_mes_h, _ses_mnq_h etc. are initialised (all use nan, lines 434-437). The REENTRY_ELIGIBLE block at lines 838-839 uses the same draws dict and has the same latent issue.
```

### Issue 3 — MEDIUM

```
severity: medium
file: backtest_smt.py
line: 730–731 (and mirrored at line 848–849 in REENTRY_ELIGIBLE)
issue: Draws dict for LONG uses _run_ses_high but overnight_high is inserted raw without directional guard
detail: The draws dict for LONG (line 710-717) correctly guards tdo, midnight_open, and session_high against being below entry price. However, "overnight_high" is inserted as-is from _day_overnight.get("overnight_high") without any check that it is above entry_price. If overnight_high <= entry_price (price ran up through the overnight high before entry), this draw would pass a negative dist value to select_draw_on_liquidity. Inside select_draw_on_liquidity (strategy_smt.py:1269), dist = (price - entry_price) for "long", and the `if dist >= min_dist` check requires dist >= 0 (since min_dist >= 0). So a negative dist is naturally filtered out. The bug does not produce incorrect results but the intent was to exclude these draws early in the dict construction (as the other draws are), and the inconsistency could confuse future maintainers.

Similarly for SHORT, "overnight_low" is inserted without a check that it is below entry_price (line 727). The same safety valve in select_draw_on_liquidity catches it.

Also: "pdh" (line 717 for LONG) and "pdl" (line 726 for SHORT) have no directional guard either. For LONG, PDH above entry is valid; below entry it is silently filtered by select_draw_on_liquidity. Correct but inconsistent with the explicit guards on the other keys.
suggestion: For consistency and defence-in-depth, add directional guards to overnight and PDH/PDL draws:
    "overnight_high": _day_overnight.get("overnight_high") if _day_overnight.get("overnight_high") and _day_overnight.get("overnight_high") > _ep else None,
    "pdh":            _day_pdh if _day_pdh and _day_pdh > _ep else None,
Mirror for SHORT. This matches the guard style already applied to tdo, midnight_open, and session_high/low.
```

### Issue 4 — MEDIUM

```
severity: medium
file: strategy_smt.py
line: 1433–1437
issue: tp_breached set on same bar as secondary target hit — exit_secondary can fire on the same bar TP is first crossed
detail: The tp_breached setter block (lines 1432-1437) executes unconditionally after the trail block. The secondary check block (lines 1439-1445) then executes. If a bar's High crosses both tp AND sec on the same candle (e.g. a large momentum bar that clears both levels), tp_breached is set to True in the setter block, then immediately the secondary check fires exit_secondary — all on the same bar.

This means: for a LONG position, if a single bar goes High = 130 with take_profit = 110 and secondary_target = 120, the position exits at secondary_target (120) without any partial exit opportunity at the primary. The fill price is 120 (correct by plan), but the position skips any partial-exit at 110. This is consistent with the plan spec ("Secondary target fires after primary has been breached") but may not match ICT intent where the primary is treated as a partial-exit level.

More importantly: there is a test for this (test_exit_secondary_fires_after_primary, line 818) that exercises this same-bar case correctly. The test uses tp_breached=True pre-set (starting already breached), so it does not test the same-bar breach+exit case.

The specific concern from the review prompt: "can tp_breached be set prematurely?" — Answer: No, it cannot be set prematurely because the setter is guarded by `if not position.get("tp_breached")` (line 1433), preventing double-setting. It can only be set when tp is actually crossed. The same-bar semantics described above are by-design and correct per the spec.
suggestion: Add a test case that exercises the same-bar TP+secondary cross scenario to make the intentional same-bar semantics explicit and prevent accidental regression.
```

### Issue 5 — LOW

```
severity: low
file: backtest_smt.py
line: 705–733 (WAITING_FOR_ENTRY) and 817–851 (REENTRY_ELIGIBLE)
issue: Draws dict construction is duplicated verbatim between WAITING_FOR_ENTRY and REENTRY_ELIGIBLE blocks
detail: The 12-line draws dict construction and select_draw_on_liquidity call appear identically in both the WAITING_FOR_ENTRY block (lines 705-731) and the REENTRY_ELIGIBLE block (lines 817-851). Any future change to draws logic requires updating both copies. The same duplication applies to the position dict construction and hypothesis context fields.
suggestion: Extract the common draws dict construction into a helper function or at minimum add a comment noting the duplication so it is not silently diverged. Example: `_draws = _build_draws_dict(pending_direction, _ep, _day_pdh, _day_pdl, ...)`.
```

### Issue 6 — LOW

```
severity: low
file: signal_smt.py
line: 510
issue: session_high computed as float(mnq_session["High"].max()) — this includes the bar where the signal fired, not just prior bars
detail: In backtest_smt.py, _run_ses_high is updated at the TOP of each bar loop with the previous bar's values (bars [0..bar_idx-1]). This means the current bar's high is excluded from the session_high draw level — correct because the current bar triggered the signal. In signal_smt._process_scanning (line 510), session_high is computed as the full max of mnq_session["High"], which includes the current (most recent) bar that triggered the signal. This means the session_high draw for LONG includes the current bar's high as a potential target level, where backtest excludes it. This creates a backtest/live divergence for the session_high draw. The practical impact is small (session_high is rarely the selected primary draw) but is a semantic inconsistency.
suggestion: Change line 510 to exclude the last bar: `_ses_high = float(mnq_session["High"].iloc[:-1].max()) if len(mnq_session) > 1 else 0.0` to match the backtest's prior-bar-only semantics.
```

### Issue 7 — LOW

```
severity: low
file: tests/test_smt_backtest.py
line: 759
issue: test_signal_with_pdh_draw_placed asserts tp field incorrectly — checks stop_price instead of take_profit
detail: The test at line 759 reads:
    tp = stats["trade_records"][0]["stop_price"]  # check position opened
    assert tp is not None
The comment says "check position opened" but the variable is named `tp` and reads stop_price rather than take_profit. This test does not actually verify that take_profit == PDH as the test name asserts. It only confirms stop_price is not None, which will always be True for any opened trade.
suggestion: Replace lines 759-760 with:
    trade = stats["trade_records"][0]
    assert trade["take_profit"] is not None  # position opened
    # Take_profit should be the PDL (for short signal with base-40 = 19960)
```

---

## Summary

**149/149 tests pass** (3 pre-existing failures excluded).

The primary bug risk is **Issue 2** (high): the `_run_ses_high = 0.0` initialisation is structurally inconsistent with the `float("nan")` pattern used for all other running extremes, and would silently suppress the session_high draw if the `bar_idx < 3` guard is ever relaxed. All other issues are medium/low and either have safety valves in `select_draw_on_liquidity` or are test quality issues.

**Issues 1, 3, 4** cover the three focus areas from the review request:
1. Trail active + secondary exists: correctly handled — trail block is gated by `secondary_target is None` (line 1407)
2. Draws dict None guards: mostly correct with safety valve in selector; overnight/PDH guards are inconsistently absent but functionally safe
3. tp_breached premature set: not possible — guarded by `if not position.get("tp_breached")`; same-bar breach+exit is by-design correct
