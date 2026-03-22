# Code Review: V3-C Portfolio Robustness Controls (R8, R9)

**Date:** 2026-03-22
**Reviewer:** Claude Code (automated pre-commit review)
**Branch:** master (unstaged working-tree changes)
**Plan:** `.agents/plans/v3-c-portfolio-robustness.md`

---

## Stats

- Files Modified: 4 (`train.py`, `tests/test_optimization.py`, `program.md`, `PROGRESS.md`)
- Files Added: 0
- Files Deleted: 0
- New lines: +334
- Deleted lines: -12
- Test result: 23 passed, 1 skipped (pre-existing)

---

## Issues Found

---

```
severity: medium
file: train.py
line: 470
issue: Correlation penalty formula inverts its effect when total_pnl is negative
detail: The formula is:
          total_pnl = total_pnl - CORRELATION_PENALTY_WEIGHT * avg_corr * total_pnl
        which factors to:
          total_pnl * (1 - weight * avg_corr)
        When total_pnl < 0 and avg_corr > 0, the subtracted term is also negative,
        so penalized total_pnl ends up LESS negative than the unpenalized value.
        A losing, correlated portfolio gets its loss reduced by the penalty — the
        opposite of the intended "concentration discount". Confirmed empirically:
        a two-ticker, perfectly-correlated, losing run with weight=0.5 produced
        unpenalized=-275.52 but penalized=-137.76.
suggestion: Gate the penalty on positive total_pnl, or use absolute magnitude:
              if CORRELATION_PENALTY_WEIGHT > 0.0 and total_pnl > 0 and len(ticker_pnl) >= 2:
                  total_pnl = round(total_pnl - CORRELATION_PENALTY_WEIGHT * _avg_corr * total_pnl, 2)
            The existing comment "penalty factor for correlated portfolios" implies
            the intent is to discount profitable concentrated runs, not to soften losses.
            The test at line 700 (test_correlation_penalty_reduces_pnl_when_positive)
            already guards against the positive-pnl case but there is no test for the
            negative-pnl inversion.
```

---

```
severity: low
file: train.py
line: 502
issue: R9 recursion guard uses float equality (== 0.0) on user-supplied bias values
detail: The guard `_price_bias == 0.0 and _stop_atr_bias == 0.0` works correctly
        here because the perturbation vectors (±0.005, ±0.3) are never zero,
        so perturbed recursive calls will always have a non-zero bias and will not
        trigger another perturbation loop. However, the guard is semantically fragile:
        if a future caller passes _price_bias=0.0001 (very small, but non-zero), the
        guard fires but the perturbation vectors already include ±0.005, so no vector
        equals the tiny bias, and the recursion is still correctly blocked. The real
        risk is the inverse: if someone passes _price_bias=0.005 (equal to one of the
        vectors), the guard blocks the loop, which is correct. In practice the function
        signature uses leading underscores to signal internal-only use, so the exposure
        is low — but a comment explaining the guard's contract would prevent accidental
        misuse.
suggestion: Add a brief inline comment, e.g.:
              # Guard: only run from the nominal call to avoid recursion.
              # Perturbed recursive calls always have non-zero biases so this
              # condition is False for them.
            No code change required — the logic is correct.
```

---

```
severity: low
file: train.py
line: 642
issue: __main__ references _fold_train_stats after the loop without guard — NameError if WALK_FORWARD_WINDOWS=0
detail: Line 642:
          _last_fold_pnl_min = _fold_train_stats.get("pnl_min", ...)
        _fold_train_stats is assigned inside the for loop (line 627). If an agent
        were to set WALK_FORWARD_WINDOWS=0, the loop body never executes and
        _fold_train_stats is undefined, raising NameError at line 642.
        Currently WALK_FORWARD_WINDOWS defaults to 3 and the comment says
        "Written by the agent at setup time", so this is an edge case that
        would not arise in normal operation. But the guard at line 637
        (`if fold_test_pnls else 0.0`) already accounts for the empty-loop
        case for min_test_pnl — the same defensiveness is absent for _fold_train_stats.
suggestion: Initialise _fold_train_stats before the loop:
              _fold_train_stats = {}
            The .get() call on line 642 already handles missing keys gracefully,
            so this is the only additional change needed.
```

---

```
severity: low
file: tests/test_optimization.py
line: 411-418
issue: Mock return dicts in test_main_runs_walk_forward_windows_folds and test_main_outputs_min_test_pnl_line omit 'pnl_min'
detail: Both mocks return a stats dict without the 'pnl_min' key. When the __main__
        block hits line 642:
          _fold_train_stats.get("pnl_min", _fold_train_stats["total_pnl"])
        the .get() fallback silently masks the missing key. The tests pass because
        the fallback path works, but they do not exercise the code path where pnl_min
        is actually present. If a future change removes the .get() fallback in favour
        of direct key access, these tests would start failing without anyone having
        noticed the gap.
suggestion: Add 'pnl_min': 0.0 to the mock return dicts in both tests to close the
            gap and ensure they stay aligned with the actual run_backtest() contract.
```

---

## Summary

No critical or high-severity issues. The implementation is logically correct for all
currently tested cases and all 23 tests pass.

The one substantive issue (medium) is the correlation penalty sign inversion for
negative-PnL runs: a losing concentrated portfolio gets its loss softened rather
than amplified. This is the opposite of the feature's stated intent. It does not
affect the optimization agent in practice when it is seeking to maximise profitable
runs (penalty correctly reduces positive PnL), but it is a latent correctness bug
if the agent uses the penalty metric to evaluate losing configurations.

The remaining three issues are low-severity: two are documentation/guard improvements
and one is test coverage completeness.
