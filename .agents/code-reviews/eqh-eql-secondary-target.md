# Code Review: EQH/EQL Secondary Target (Gap 1)

**Plan**: `.agents/plans/eqh-eql-secondary-target.md`

## Stats

- Files Modified: 4 (`strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`, `tests/test_smt_backtest.py`)
- Files Added: 1 (`tests/test_eqh_eql_detection.py`)
- Files Deleted: 0
- New lines: ~303 (production + tests)
- Tests: 23/23 passing (dedicated file), both regression smoke tests pass

## Focus-Area Verdicts

1. **Lookahead correctness** — Verified clean. `_find_swing_points` bounds `i_end = end - swing_bars - 1`; the test `test_no_lookahead_in_swing_detection` exercises this explicitly and passes. In `backtest_smt.py` L462–464 and `signal_smt.py` L514–516, the window is `[session_start - 2d, session_start)` — current-session bars are strictly excluded.
2. **Staleness window** — Correct. `range(last_bar+1, bar_idx)` with `bar_idx = len(_eqh_bars)` scans every post-swing bar including the final pre-session bar. No off-by-one.
3. **Numpy in inner loops** — Verified; `bars["High"].values` extracted once, passed as numpy arrays to both swing detection and staleness filtering.

---

## Findings

```
severity: medium
file: strategy_smt.py
line: 947-960
issue: Running-mean clustering allows cluster span to exceed `tolerance`
detail: Each point is compared to the CURRENT running mean, not to the cluster's min/max. Demonstrated empirically: input [100, 102, 104, 106] with tolerance=3 produces a single cluster of 3 points with span 6 (double the tolerance). As points drift upward, the mean drifts with them and admits progressively-farther points. For EQH where "equal" semantically means "all within N points of each other", this lets clusters span 2*tolerance or more in pathological cases. The ranking test at test_sort_by_touches_desc passes only because prices are exactly equal.
suggestion: Either (a) check `abs(price - cluster_min) <= tolerance AND abs(price - cluster_max) <= tolerance` instead of comparing to the mean, or (b) document this as intentional behavior in the docstring and note the effective max-span is ~2*tolerance. Option (a) is safer for a "levels must be close" heuristic.
```

```
severity: low
file: strategy_smt.py
line: 1022-1023
issue: `detect_eqh_eql` silently reads module-level `EQH_ENABLED` but not other constants
detail: The function signature exposes `lookback`, `swing_bars`, `tolerance`, `min_touches` as parameters (good — testable), but `EQH_ENABLED` is consulted as a global. This creates two configuration surfaces: callers must remember that monkeypatching `EQH_ENABLED` short-circuits even when explicit params are passed. Tests work around this; not a bug, but inconsistent.
suggestion: Either move the `EQH_ENABLED` check to the call sites (backtest_smt, signal_smt) so `detect_eqh_eql` is a pure function of its args, OR accept an `enabled: bool = EQH_ENABLED` parameter. Minor — current behavior is documented.
```

```
severity: low
file: strategy_smt.py
line: 1026
issue: `bar_idx < 2 * swing_bars + min_touches` early-return uses `min_touches` as bar count
detail: The guard mixes units — `2 * swing_bars` is a bar-count floor (need swing windows on both sides of at least one swing), but adding `min_touches` is semantically off: you need at minimum `2 * swing_bars + 1` bars to form one swing, and `min_touches` swings (spaced by at least `swing_bars+1` each) to cluster. The current guard is more permissive than the theoretical minimum but stricter than `2 * swing_bars + 1`. Practically harmless (the `_find_swing_points` loop simply returns empty on insufficient data), but the arithmetic is not what the naming suggests.
suggestion: Simplify to `if bar_idx < 2 * swing_bars + 1: return [], []` and let downstream min_touches filter handle cluster-count validation. Pure cosmetic.
```

```
severity: nit
file: strategy_smt.py
line: 1619-1621
issue: `_build_draws_and_select` takes only "nearest" EQH/EQL — doesn't leverage multiple levels
detail: The function takes potentially many EQH levels from detection but only picks the nearest-above (for longs). A farther EQH with many more touches (stronger level) cannot surface. `select_draw_on_liquidity` already does distance-based ranking, so feeding ALL above-entry EQH levels (named `eqh_1`, `eqh_2`, ...) would let the existing ranker choose. Current behavior is correct per the plan's spec but under-uses the detected data.
suggestion: Optional enhancement for a future iteration — feed all above-entry EQH levels as separate draws. Out of scope for this PR per plan text.
```

```
severity: nit
file: strategy_smt.py
line: 1631 / 1649
issue: Minor: sort key `lambda l: -l["price"]` for EQL uses unary minus; `reverse=True` is more idiomatic
detail: `sorted(eql_levels, key=lambda l: -l["price"])` works but `sorted(eql_levels, key=lambda l: l["price"], reverse=True)` is the Pythonic idiom.
suggestion: `sorted(eql_levels, key=lambda l: l["price"], reverse=True)` — purely cosmetic.
```

```
severity: nit
file: backtest_smt.py
line: 461 / signal_smt.py:513
issue: 2-day window is documented as "prior RTH + overnight" but isn't time-zone safe across DST
detail: `_session_start_ts - pd.Timedelta(days=2)` is fine because `_session_start_ts` is tz-aware (America/New_York). `pd.Timedelta` subtracts absolute duration, not calendar days, so DST transitions shift the window boundary by 1 hour. Practically negligible for a 2-day lookback with 100-bar cap, but worth noting. No fix needed.
suggestion: None. Informational.
```

```
severity: nit
file: tests/test_smt_backtest.py
line: end of file
issue: File does not end with a newline
detail: Git reports "\ No newline at end of file" on the appended block. Unix convention prefers a trailing newline.
suggestion: Append a single `\n`.
```

---

## Positive Notes

- Numpy arrays pre-extracted in `detect_eqh_eql` — matches project's existing hot-path pattern (L498–507 in `backtest_smt.py`).
- Excellent test coverage: 23 unit tests, dedicated lookahead test (`test_no_lookahead_in_swing_detection`), dedicated staleness-window test (`test_stale_scan_window`), wick-vs-close semantics tested in both directions.
- Master flag `EQH_ENABLED` gives clean rollback path.
- Keyword-only arguments added to `_build_draws_and_select` with `None` defaults preserve backward compatibility for existing callers (only 1 call site in production).
- Slot additions to `SessionContext` preserve `__slots__` invariant.
- Type hints present on all new functions.
- Both backtest and signal construct the EQH window identically — reduces drift risk between paths.

---

## Pre-existing Failures

None observed in the targeted test run. Regression smoke tests pass.

---

## Overall Verdict

**APPROVE_WITH_NITS**

One medium-severity item (clustering drift past tolerance) is worth addressing before optimizer sweeps start, because clusters with a 6-point range being called "equal highs" at tolerance=3 could inflate touch counts and produce misleading optimizer signal. Recommend a 2-line fix to use min/max bounds instead of running mean, then proceed to merge. All other findings are low/nit.
