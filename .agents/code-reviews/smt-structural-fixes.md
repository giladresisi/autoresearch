# Code Review: SMT Structural Fixes

**Date**: 2026-04-21
**Branch**: master (unstaged changes)
**Reviewer**: Claude Code (ai-dev-env:code-review skill)

## Stats

- Files Modified: 5 (`strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`, `tests/test_smt_backtest.py`, `tests/test_smt_strategy.py`)
- Files Added: 1 (`tests/test_smt_structural_fixes.py`)
- New lines: ~523
- Deleted lines: ~8

## Test Run Results

- 110 tests passed
- 2 tests failed: `test_regression_no_reentry_matches_legacy_behavior`, `test_run_backtest_max_reentry_count_limits_trades`
- Both failures are **pre-existing** (confirmed via `git stash` baseline run — both fail on the unmodified codebase)

---

## Issues Found

---

```
severity: high
file: strategy_smt.py
line: 148-152
issue: PESSIMISTIC_FILLS TP exits are optimistic, not pessimistic — name and docstring are misleading
detail: When PESSIMISTIC_FILLS=True and a TP fires, a long fills at bar["High"] and a short fills
  at bar["Low"]. If the bar continued past the TP level (e.g. long TP=20050, bar High=20100),
  the recorded exit_price (20100) is BETTER than the exact TP fill (20050). This OVERSTATES P&L
  for TP exits, making PESSIMISTIC_FILLS=True appear more profitable than PESSIMISTIC_FILLS=False,
  which is counterintuitive for a flag labeled "pessimistic". The stop side is correctly pessimistic
  (worse fills). The optimizer comparing True vs False will see True as having higher P&L due to
  this TP-side effect.
  The implementation matches the plan spec (plan line 25, 74-75), so this is not a spec deviation,
  but the constant name, comment in strategy_smt.py ("Pessimistic fill simulation"), and trade
  record comment all misrepresent the TP-side behavior.
suggestion: Either (a) rename the constant to BAR_EXTREME_FILLS or REALISTIC_FILLS and update the
  docstring to clearly state "TP fills at bar extreme (may be better than exact TP)", or (b) change
  the TP-side logic so long TP fills at exact take_profit and short TP fills at exact take_profit,
  reserving bar extreme for the stop side only. Option (b) makes the flag semantically correct.
```

---

```
severity: medium
file: strategy_smt.py
line: 759-772
issue: _quarterly_session_windows() is defined but never called — dead code
detail: The EXPANDED_REFERENCE_LEVELS spec (plan acceptance criteria, constant docstring) says
  "check sweeps against quarterly sessions, calendar day, and current calendar week H/L."
  _quarterly_session_windows() was added to support quarterly session window lookups, but
  it is never invoked anywhere in strategy_smt.py, backtest_smt.py, or signal_smt.py.
  The actual EXPANDED_REFERENCE_LEVELS implementation only checks prev-day and prev-session
  extremes. Quarterly-session and calendar-week reference levels are both missing.
  Additionally, both callers of _compute_ref_levels() pass week_mnq=None and week_mes=None,
  so week-level extremes are also never computed or checked.
suggestion: Either (a) remove _quarterly_session_windows() if quarterly sessions are deferred to a
  later plan, and update the constant docstring to say "prev-day and prev-session" rather than
  "quarterly sessions, calendar day, and current calendar week H/L"; or (b) implement the
  quarterly session and calendar week slice lookups in run_backtest() and screen_session() and
  pass them into _compute_ref_levels().
```

---

```
severity: medium
file: tests/test_smt_structural_fixes.py
line: 531-552
issue: test_s2_2 has a vacuous assertion — does not verify EXPANDED_REFERENCE_LEVELS=False suppresses prev-day signals
detail: The test is designed as the negative counterpart to test_s2_1: when
  EXPANDED_REFERENCE_LEVELS=False, prev-day sweep signals should NOT fire.
  But the assertion is: "if signal is not None: assert signal.get('smt_sweep_pts') > 0"
  — a conditional check that passes regardless of whether signal is None or not None.
  If a bug caused EXPANDED_REFERENCE_LEVELS=False to still produce a prev-day signal,
  this test would pass silently (the conditional branch would evaluate the always-true
  smt_sweep_pts > 0 check). The test never actually asserts signal IS None.
suggestion: Replace the conditional with an unconditional assertion:
  assert signal is None, "Expected no signal when EXPANDED_REFERENCE_LEVELS=False"
  The fixture _make_prev_day_divergence_bars() is designed so there is no session-level
  divergence (bar 6 MES high = base+22 < session high base+30), so signal must be None
  when EXPANDED_REFERENCE_LEVELS=False and only prev-day data is available.
```

---

```
severity: medium
file: tests/test_smt_structural_fixes.py
line: 265-287
issue: test_f2c_2 comment is misleading — the binding threshold is PTS-based not ratio-based
detail: The test is supposed to verify that move > 50% of entry-to-TP distance transitions to IDLE.
  The comment says "50% = 4999" and "move = 5001 > threshold 4999 → no reentry". But
  REENTRY_MAX_MOVE_PTS is set to 999.0 (the default from _ratio_threshold_patch extra_pts=999.0),
  so move_threshold = min(999, 4999) = 999, not 4999.
  The test works because 4999 > 999 (both exceed the actual threshold), but it does NOT exercise
  the ratio-based path as claimed. The ratio threshold is never the binding constraint here.
  A reader trusting the comment would misunderstand which threshold fired.
suggestion: To truly test the ratio-based threshold, set REENTRY_MAX_MOVE_PTS to a large value
  (e.g., 999999) so ratio_threshold = 0.5 * entry_to_tp is the binding constraint. Update
  comments to reflect the actual threshold value used.
```

---

```
severity: medium
file: tests/test_smt_structural_fixes.py
line: 137-193
issue: F1 tests (signal_smt reentry guard) test guard logic inline rather than through _process_scanning
detail: All four F1 tests (test_f1_1 through test_f1_4) manipulate module-level state directly
  (_current_session_date, _current_divergence_level, _divergence_reentry_count) and simulate
  the guard logic inline rather than calling _process_scanning() with a mocked signal.
  This means the tests verify that the guard expression produces the right boolean result, but
  NOT that _process_scanning() actually executes the return statement at line 515 when the count
  is at the limit. If the guard were accidentally removed or placed after the position-opening
  code, all four tests would still pass.
suggestion: Add an integration test that patches screen_session() to return a signal with a known
  smt_defended_level, calls _process_scanning() multiple times (same level, same bar_ts), and
  asserts that _state remains "SCANNING" (position not opened) after the count reaches
  MAX_REENTRY_COUNT. This would catch regressions where the guard is bypassed.
```

---

```
severity: low
file: signal_smt.py
line: 508-514
issue: MAX_REENTRY_COUNT semantics differ between signal_smt and backtest_smt
detail: In backtest_smt.py, reentry_count is incremented when a position is ENTERED (line 661),
  so MAX_REENTRY_COUNT=1 allows exactly 1 entry + 0 reentries per session.
  In signal_smt.py, _divergence_reentry_count is incremented every time a signal at the SAME
  defended level arrives (line 512), regardless of whether the position was actually entered.
  With MAX_REENTRY_COUNT=1: backtest allows initial entry + blocks first reentry;
  signal_smt allows first signal to fire + blocks second signal at same level (regardless of
  whether position opened). These are not equivalent if signals fire that _process_scanning
  itself filters out for other reasons (startup guard, re-detection guard).
suggestion: Add a comment at line 512 clarifying that this count tracks "signal detections at this
  defended level" not "entries taken", and document the deliberate semantic difference from the
  backtest counter if this is intentional. If the intent is to match the backtest semantics,
  move the increment to after the position is confirmed open (after line 528).
```

---

```
severity: low
file: strategy_smt.py
line: 639-649
issue: MIN_DISPLACEMENT_BODY_PTS check in detect_displacement() is dead code when set below MIN_DISPLACEMENT_PTS
detail: detect_displacement() first checks body < MIN_DISPLACEMENT_PTS and returns False (line 643).
  The new Plan-4 check at line 645 only executes if body >= MIN_DISPLACEMENT_PTS. If
  MIN_DISPLACEMENT_BODY_PTS is configured with a value less than or equal to MIN_DISPLACEMENT_PTS,
  line 645 can never cause a rejection (the earlier check already covers it). This is not a bug —
  the intended use is MIN_DISPLACEMENT_BODY_PTS > MIN_DISPLACEMENT_PTS for a stricter filter —
  but there is no guard or comment preventing misconfiguration.
suggestion: Add a comment: "NOTE: only effective when MIN_DISPLACEMENT_BODY_PTS > MIN_DISPLACEMENT_PTS.
  Values at or below MIN_DISPLACEMENT_PTS are shadowed by the earlier check."
  Optionally add an assertion or log warning at module load if MIN_DISPLACEMENT_BODY_PTS > 0
  and MIN_DISPLACEMENT_BODY_PTS <= MIN_DISPLACEMENT_PTS.
```

---

```
severity: low
file: tests/test_smt_structural_fixes.py
line: 592-610
issue: test_s3_1 is a smoke test only — does not assert HTF-visible signal actually passes the filter
detail: test_s3_1 is titled "Signal visible on at least one HTF passes when HTF_VISIBILITY_REQUIRED=True"
  but the test only asserts that screen_session() does not raise an exception. There is no assertion
  that a signal is actually returned. With 30 bars at 5-min frequency, a 15m period boundary IS
  crossed by bar 3, so a prior-period extreme can be populated. However the test passes even if
  the HTF filter blocks all signals (returns None), making it impossible to distinguish a correct
  "pass" from a correct "block" or a buggy "always-block".
  Similarly test_s3_3 uses a conditional check "if signal is not None" which makes the
  htf_confirmed_timeframes assertion vacuous if no signal fires.
suggestion: Extend the n=90 bar fixture so a prior-period boundary is definitely crossed before
  the divergence bar and assert signal is not None. Alternatively add a separate fixture with
  controlled timestamps where exactly one 15m period has closed before the divergence bar.
```

---

## Pre-existing Test Failures (not introduced by this changeset)

- `tests/test_smt_backtest.py::test_regression_no_reentry_matches_legacy_behavior` — asserts `total_trades == 1` but gets 2. Pre-existing on the unpatched codebase.
- `tests/test_smt_backtest.py::test_run_backtest_max_reentry_count_limits_trades` — asserts trades per day <= 1 with MAX_REENTRY_COUNT=1. Pre-existing on the unpatched codebase.

Both failures pre-date this changeset and are not caused by the new code.
