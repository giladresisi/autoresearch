# Code Review: SMT Hypothesis Alignment Plan 3

## Stats

- Files Modified: 4 (backtest_smt.py, strategy_smt.py, hypothesis_smt.py, tests/test_hypothesis_smt.py)
- Files Added: 3 (analyze_hypothesis.py, tests/test_hypothesis_analysis.py, plan2_experiment_runner.py)
- Files Deleted: 0
- New lines: ~450
- Deleted lines: ~20

---

## Issues

```
severity: medium
file: backtest_smt.py
line: ~310 (WAITING_FOR_ENTRY signal construction)
issue: fvg_detected reports False when FVG was found but suppressed by FVG_LAYER_B_REQUIRES_HYPOTHESIS gate
detail: _pending_fvg is set to None in IDLE state when FVG_LAYER_B_REQUIRES_HYPOTHESIS=True and score is below threshold.
        Later, signal["fvg_detected"] = _pending_fvg_zone is not None reflects the post-suppression value.
        When the flag is True, a trade that had a real FVG zone present will record fvg_detected=False,
        making it impossible to distinguish "no FVG existed" from "FVG existed but was gated."
        Plan 3 acceptance criterion states fvg_detected should be "independent of whether Layer B entered."
suggestion: Capture the raw FVG result before the gate check into a separate variable (e.g. _raw_fvg_zone),
            then set _pending_fvg = None to suppress Layer B, but set signal["fvg_detected"] = _raw_fvg_zone is not None.
```

```
severity: low
file: backtest_smt.py
line: ~10
issue: compute_hypothesis_direction is imported but no longer called in run_backtest()
detail: After the refactor to compute_hypothesis_context, the old function import remains.
        It is only used in tests that import it directly from hypothesis_smt — not from backtest_smt.
        Dead imports add confusion about which functions the module depends on.
suggestion: Remove the compute_hypothesis_direction import from backtest_smt.py. Tests importing it
            directly from hypothesis_smt are unaffected.
```

```
severity: low
file: analyze_hypothesis.py
line: 138
issue: consistent_count variable is dead code — incremented in loop but never read
detail: consistent_count is initialized to 0 at line 138 and incremented inside the fold loop at line 147.
        However, the verdict at line 193 uses a separate consistent_folds = sum(...) recomputation
        rather than reading consistent_count. The loop variable is pure dead code.
suggestion: Remove the consistent_count = 0 initialization (line 138) and the consistent_count += 1
            increment (line 147). The verdict logic is correct; only the dead accumulator needs removal.
```

```
severity: low
file: analyze_hypothesis.py
line: 82-83
issue: Degenerate fold boundaries when all entry_date values are identical
detail: compute_fold_stats calls dates.quantile(quantiles) which returns identical boundary strings
        when all trades occur on the same date (e.g. single-day test data). This produces 6 zero-width
        folds where mask = (date >= X) & (date < X) is always False, so all folds have 0 trades.
        Not a runtime error but silently produces a misleading "no data in folds" analysis.
suggestion: Add a guard after quantile computation: if len(set(fold_boundaries)) < 2, return [] with
            an optional warning. Low priority — production data always spans multiple dates.
```

---

## Pre-existing Failures

- `test_ib_connection.py` in project root requires a live IB Gateway connection. Not part of the `tests/` directory, not introduced by Plan 3.

---

## Summary

No critical or high-severity issues. The medium-severity `fvg_detected` semantic mismatch (issue 1) is the most actionable finding — at default settings (`FVG_LAYER_B_REQUIRES_HYPOTHESIS=False`) it is a non-issue, but when that flag is enabled the diagnostic field loses its intended meaning. The remaining issues are cleanup items.
