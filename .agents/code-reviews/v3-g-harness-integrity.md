# Code Review: V3-G Harness Integrity

**Date:** 2026-03-23
**Plan:** `.agents/plans/v3-g-harness-integrity.md`
**Reviewer:** automated code-review skill

---

## Stats

- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: +143 (train.py: +14, program.md: +43, tests/test_optimization.py: +115, GOLDEN_HASH line: +1/-1)
- Deleted lines: -4 (train.py: -3 comment chars, test_optimization.py: -1 GOLDEN_HASH value)

---

## Test Results

- `python -m pytest tests/test_optimization.py -k "v3g" -v` → **10 passed**
- `python -m pytest tests/test_optimization.py -k "harness" -v` → **1 passed** (GOLDEN_HASH regression)
- `python -m pytest tests/test_optimization.py -x -q` → **49 passed, 1 skipped** (no regressions; pre-existing skip is unchanged)

---

## Issues Found

### Issue 1

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\train.py
line: 69-72
issue: TICKER_HOLDOUT_FRAC is placed inside the STRATEGY TUNING block, contradicting program.md's SESSION SETUP scope instruction
detail: The plan's "SESSION SETUP constants in order" list (plan lines 173-174) explicitly includes
  TICKER_HOLDOUT_FRAC as a SESSION SETUP constant, alongside RISK_PER_TRADE, SILENT_END, etc.
  However in the implemented train.py, TICKER_HOLDOUT_FRAC lives after the
  "# ══ STRATEGY TUNING ══" header (line 59), not before it.
  Meanwhile program.md Edit A (line 93-94) instructs the agent that SESSION SETUP constants
  must not be changed during experiments — but if TICKER_HOLDOUT_FRAC is in the STRATEGY TUNING
  block an agent will treat it as a valid optimization target.
  program.md Edit B also calls it a "recommended default" to "Set at session setup",
  which directly conflicts with it living inside the STRATEGY TUNING zone.
suggestion: Move TICKER_HOLDOUT_FRAC (lines 69-72) and its comment block to before the
  "# ══ STRATEGY TUNING ══" header, placing it with the other SESSION SETUP constants
  (after SILENT_END, before the STRATEGY TUNING header). Update the test
  test_v3g_max_simultaneous_positions_in_strategy_tuning to confirm TICKER_HOLDOUT_FRAC
  is in the SESSION SETUP zone as well (or add a new test mirroring test_v3g_risk_per_trade_in_session_setup).
```

### Issue 2

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\program.md
line: 157-158 (the "Goal" section)
issue: The "Goal" section still states the keep rule as a single condition, contradicting the updated step 8
detail: Lines 157-158 read: "Keep any change that increases min_test_pnl; discard any change that does not."
  This is a direct summary of the old single-condition keep rule.
  Step 8 (lines 296-300) now correctly enforces dual conditions (min_test_pnl AND train_pnl_consistency),
  but the "Goal" section summary was not updated and contradicts it.
  A reading agent that only skims the "Goal" section will miss the consistency floor entirely.
suggestion: Update lines 157-158 to reflect both conditions, e.g.:
  "Keep any change that increases min_test_pnl AND passes the pnl_consistency floor
  (train_pnl_consistency ≥ −RISK_PER_TRADE × 2); discard any change that does not meet both criteria."
```

### Issue 3

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\program.md
line: 309-315
issue: Zero-trade plateau rule does not clarify whether the 10-consecutive-iteration reset applies only when every iteration produces zero trades, vs. when 10 post-threshold-relaxation retries produce zero trades
detail: The 3-iteration rule says "relax the threshold and try a different modification."
  The 10-iteration rule says "10 consecutive iterations all produce zero trades → log plateau."
  These two rules interact: after relaxing at iteration 3, are the counters reset, or does the
  10-iteration counter continue accumulating? An agent could reasonably interpret this either way,
  leading to premature or delayed plateau detection.
suggestion: Add a clarifying sentence after the 10-iteration rule, e.g.:
  "The 10-iteration counter resets to 0 whenever a non-zero-trade iteration occurs."
  This makes the plateau definition unambiguous.
```

### Issue 4

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\tests\test_optimization.py
line: 1298-1311 (test_v3g_risk_per_trade_comment_warns_inflation)
issue: Search window for the "inflate" warning is 3 lines (rpt_idx-2 to rpt_idx) but the warning is on the comment line directly above RISK_PER_TRADE, which is only 1 line above; the window is fine but the test would silently pass even if "inflate" were placed as far as 2 lines above RISK_PER_TRADE with an unrelated intermediate line
detail: This is a robustness concern rather than a correctness bug: the window is generous enough
  that future edits to the comment block could introduce an unrelated intermediate line and the
  test would still pass even if "inflate" were no longer adjacent to RISK_PER_TRADE.
  Not a blocking issue since the current implementation is correct, but the test's intent is
  "adjacent warning" and the assertion reflects "within 3 lines."
suggestion: Narrow the window to lines[max(0, rpt_idx-1):rpt_idx+1] (1 line above) to tighten
  the assertion to the intended proximity, since the comment is always immediately above the
  assignment in the implemented train.py.
```

---

## Summary

The implementation is functionally correct and all 50 tests pass. The two most notable issues are:

1. **TICKER_HOLDOUT_FRAC placement** (medium): it ended up in the STRATEGY TUNING block but the plan and program.md both treat it as a SESSION SETUP constant. This creates a genuine behavioral gap — an optimization agent will treat holdout fraction as a tunable parameter rather than a fixed session setting.

2. **"Goal" section inconsistency** (low): the one-line goal summary still describes the old single-condition keep rule, which an agent that skims the file will see before reaching the corrected step 8.

Issues 3 and 4 are documentation clarity and test robustness concerns with no correctness impact.
