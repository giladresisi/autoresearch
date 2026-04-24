# Feature: EQH/EQL Detection Extending Secondary-Target Candidate Pool (Gap 1)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

**⚠️ MECHANICS LOCK**: Prior campaign rejected all mechanics changes (M0/M1/M3/Option A/Gap 14) and all filter changes (Gap 3/Gap 13). Do NOT modify: `SHORT_STOP_RATIO`, `LONG_STOP_RATIO`, `MIN_STOP_POINTS`, `PARTIAL_STOP_BUFFER_PTS`, `PARTIAL_EXIT_ENABLED`, `STRUCTURAL_STOP_MODE`, `BREAKEVEN_TRIGGER_PCT`, `TRAIL_AFTER_TP_PTS`, `MIN_BARS_BEFORE_SIGNAL`. This feature is PURELY ADDITIVE — it adds new DOL candidates to the existing `secondary_target` mechanism without changing any of the validated baseline trade-management behavior.

---

## Feature Description

Add Equal Highs (EQH) and Equal Lows (EQL) detection to the SMT strategy, feeding them as additional candidates into the existing `_build_draws_and_select` function so the current `secondary_target` mechanism can pick them when they represent the nearest qualifying liquidity. EQH/EQL levels are the highest-priority institutional liquidity targets per ICT theory — where two or more swing highs/lows align at the same price, retail stops cluster 2-3× more densely than at single-swing levels.

The detection runs once per session using prior-day + overnight MNQ bars, computing swing points via a fractal window (N=3 bars on each side) and clustering them by price tolerance. Stale levels (those already closed through) are filtered out. Active levels flow into `SessionContext.eqh_levels` / `eql_levels` and are injected as named candidates into the draws dict used by `_build_draws_and_select`.

The existing `secondary_target` machinery picks the nearest candidate meeting RR/distance criteria — so adding EQH/EQL as candidates gives it a higher-quality menu without changing any selection logic, stop logic, or TP logic.

## User Story

As a **strategy optimizer**
I want to **feed institutional-quality EQH/EQL liquidity levels into the secondary-target DOL pool**
So that **the existing secondary-target mechanism can pick higher-concentration stop clusters when available, improving per-trade capture without changing validated mechanics**.

## Problem Statement

The current `_build_draws_and_select` function considers six candidate DOL levels for secondary target: `fvg_top`/`fvg_bottom`, `tdo`, `midnight_open`, `session_high`/`session_low`, `overnight_high`/`overnight_low`, and `pdh`/`pdl`. All six are single-price reference levels — a swing extreme from one observation. ICT theory identifies EQH/EQL (two or more swing points at the same price within tolerance) as the highest-density retail-stop cluster, with 2-3× the liquidity concentration of a single swing. These are the primary institutional targets for stop runs, yet they are never detected or considered by the current strategy.

## Solution Statement

Implement a pure detection function `detect_eqh_eql(bars, bar_idx, lookback, swing_bars, tolerance, min_touches)` that:
1. Identifies fractal swing highs/lows in the lookback window
2. Clusters swing points within `EQH_TOLERANCE_PTS` of each other
3. Returns clusters with ≥ `EQH_MIN_TOUCHES` as active levels
4. Filters out stale levels (price closed through since last touch)

Compute EQH/EQL once per session using prior-day + overnight bars, store them on `SessionContext` via two new slots (`eqh_levels`, `eql_levels`). Modify `_build_draws_and_select` to add the nearest active EQH (for long trades) or EQL (for short trades) as named candidates. `select_draw_on_liquidity` already handles arbitrary named candidates — no change needed there.

Integrate wiring in both `backtest_smt.run_backtest` (per-day session context construction) and `signal_smt.py` (realtime per-session init). Add unit tests for the detection function with adversarial staleness cases.

## Feature Metadata

**Feature Type**: New Capability (information-additive)
**Complexity**: Medium
**Primary Systems Affected**: `strategy_smt.py` (detection, SessionContext, draw selection), `backtest_smt.py` (wiring), `signal_smt.py` (wiring)
**Dependencies**: None — pure Python on existing numpy/pandas infrastructure
**Breaking Changes**: No — all changes purely additive. Existing code paths preserved when `EQH_ENABLED=False`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py` (lines 482–517) — `SessionContext` class; add `eqh_levels` and `eql_levels` slots here
- `strategy_smt.py` (lines 1191–1219) — `select_draw_on_liquidity` function; no changes needed (handles arbitrary named candidates already)
- `strategy_smt.py` (lines 1355–1380) — `_build_draws_and_select` function; inject EQH/EQL candidates here
- `strategy_smt.py` (lines 847–885) — `detect_fvg` function; pattern reference for detection function signature and optional-gate pattern (returns None when disabled)
- `strategy_smt.py` (lines 829–844) — `compute_pdh_pdl` function; pattern reference for per-session computation function
- `strategy_smt.py` (lines 988–1033) — `_compute_ref_levels` function; pattern reference for how to compute a levels dict once per session from slices
- `strategy_smt.py` (lines 1080–1188) — `screen_session` wrapper; pattern for how SessionContext is constructed in tests/wrappers
- `backtest_smt.py` (lines 438–537) — Per-day session context construction; EQH/EQL computation wiring goes around line 488 (prev-day slices already computed), SessionContext update at line 526
- `backtest_smt.py` (lines 13–48) — Imports from strategy_smt; add `detect_eqh_eql` and constants
- `signal_smt.py` (lines 22–32) — Imports from strategy_smt
- `signal_smt.py` (lines 480–513) — Per-day session context construction at line 506
- `tests/test_smt_backtest.py` (lines 1–40) — Test file header/import patterns and docstring explaining monkeypatch targets
- `tests/test_smt_structural_fixes.py` — Pattern for tests that monkeypatch strategy constants
- `insights.md` sections "1. Draw on Liquidity — Extend `secondary_target`" and the "Implementation Approach (Gap 1)" — detailed design already published

### New Files to Create

- `tests/test_eqh_eql_detection.py` — Unit tests for `detect_eqh_eql` (pure-function tests; no monkeypatching needed)

### Patterns to Follow

**Naming conventions (from strategy_smt.py constants)**:
- Module-level constants: `ALL_CAPS_SNAKE` (e.g. `EQH_SWING_BARS`)
- Private helpers: `_leading_underscore_snake` (e.g. `_find_swing_points`)
- Public functions: `snake_case` (e.g. `detect_eqh_eql`)

**Error handling**: Detection functions return `None` or empty list when disabled/unavailable (see `detect_fvg` pattern `if not FVG_ENABLED: return None` at line 863). Never raise on missing data — caller checks return.

**Optional-gate pattern**: Add a master enable flag `EQH_ENABLED: bool = True` at top of constants block. When False, detection returns empty list and no candidates flow into draws dict. This lets future optimizer sweeps or rollback turn the feature off with one flag.

**Constants comment pattern**: Each new constant gets 2–4 comment lines above explaining: what it controls, valid range, optimizer search space (if applicable). Match the style of `MIN_TDO_DISTANCE_PTS` at line 85–89.

**SessionContext update pattern**: Add new slots, update `__init__` signature with keyword-only defaults (`eqh_levels: "list | None" = None`), default to `[]` in body (see how `ref_lvls` at line 505–516 handles this).

**Wiring pattern for backtest_smt.py**: Compute per-session via helper, guard with `if EQH_ENABLED` block, pass to `SessionContext(...)` constructor. Mirror the `EXPANDED_REFERENCE_LEVELS` block at lines 511–522 for structure.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation — Pure detection + data structure (Parallel)    │
├────────────────────────────────────────────────────────────────────┤
│ Task 1.1: detect_eqh_eql function + constants                      │
│ Task 1.2: SessionContext slots (eqh_levels, eql_levels)            │
│ Task 1.3: Unit tests for detect_eqh_eql                            │
└────────────────────────────────────────────────────────────────────┘
                                   ↓
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Integration — Wire detection into context (Parallel)       │
├────────────────────────────────────────────────────────────────────┤
│ Task 2.1: backtest_smt.py session wiring                           │
│ Task 2.2: signal_smt.py session wiring                             │
│ Task 2.3: _build_draws_and_select candidate injection              │
└────────────────────────────────────────────────────────────────────┘
                                   ↓
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation — End-to-end tests (Sequential)                 │
├────────────────────────────────────────────────────────────────────┤
│ Task 3.1: Integration test: synthetic bars → EQH picked            │
│ Task 3.2: Regression test: full backtest runs, trades.tsv diff     │
└────────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2, 1.3 — Task 1.3 consumes Task 1.1's signature but a mock/stub fn can satisfy the test skeleton until Task 1.1 completes (see Interface Contract 1 below).
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2, 2.3 — all consume the Wave 1 API.
**Wave 3 — Sequential**: Task 3.1 depends on all Wave 2 wiring; Task 3.2 depends on Task 3.1.

### Interface Contracts

**Contract 1** — `detect_eqh_eql` signature (Task 1.1 provides → Tasks 1.3, 2.1, 2.2, 2.3 consume):

```python
def detect_eqh_eql(
    bars: pd.DataFrame,           # OHLCV with DatetimeIndex
    bar_idx: int,                 # reference index (typically len(bars); detection scoped to [start..bar_idx-N-1])
    lookback: int = 100,          # bars to scan back from bar_idx
    swing_bars: int = 3,          # fractal window size on each side
    tolerance: float = 3.0,       # clustering tolerance in MNQ points
    min_touches: int = 2,         # minimum swing points per cluster
) -> "tuple[list[dict], list[dict]]":
    """Return (eqh_levels, eql_levels), each a list of {"price": float, "touches": int, "last_bar": int}.

    Returns ([], []) when EQH_ENABLED is False or insufficient data.
    Stale levels (any bar's close in (last_bar..bar_idx-1] passed through the level) are filtered out.
    Results sorted by touches desc, then last_bar desc (most recent tie-break).
    """
```

**Contract 2** — `SessionContext` API (Task 1.2 provides → Tasks 2.1, 2.2, 2.3 consume):

```python
class SessionContext:
    __slots__ = (..., "eqh_levels", "eql_levels")
    def __init__(self, ..., eqh_levels: "list | None" = None, eql_levels: "list | None" = None):
        self.eqh_levels = eqh_levels or []
        self.eql_levels = eql_levels or []
```

**Contract 3** — `_build_draws_and_select` candidate injection (Task 2.3 modifies):

When `direction == "long"`, add `draws["eqh"] = nearest_eqh_price_above_entry_or_None`.
When `direction == "short"`, add `draws["eql"] = nearest_eql_price_below_entry_or_None`.
No change to `select_draw_on_liquidity` — it already handles arbitrary named candidates.

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_eqh_eql_detection.py -v` (all unit tests pass)
**After Wave 2**: `uv run python -c "from strategy_smt import detect_eqh_eql, SessionContext; print('imports OK')"` (imports clean, no circular-import surprises)
**After Wave 3**: `uv run pytest tests/test_smt_backtest.py tests/test_eqh_eql_detection.py -v` (no regressions)

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — Pure Detection Logic

No external services; core logic + constants + data structure only.

### Phase 2: Integration — Wire into Session Lifecycle

Compute per session, pass to `SessionContext`, inject into draws dict.

### Phase 3: Integration Tests + Regression Validation

Synthetic bar tests for the integration path; full backtest smoke test to verify trades.tsv remains consistent in shape and non-empty.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: ADD `detect_eqh_eql` + constants to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: core-logic-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: Tasks 1.3, 2.1, 2.2, 2.3
- **PROVIDES**: `detect_eqh_eql(bars, bar_idx, lookback, swing_bars, tolerance, min_touches) -> (list, list)` public function + 5 new constants
- **IMPLEMENT**:
  1. Add to the END of the strategy-tuning constants block (immediately AFTER `CONFIRMATION_WINDOW_BARS: int = 1` — around line 364 — and BEFORE the `# ── Module-level bar data ──` separator at line 367). **Important: do NOT interleave these near existing constants (e.g., near FVG_* around line 219).** Placing them at the tail of the block prevents merge conflicts with a concurrent parameter-optimization branch that may be tuning nearby existing constants.
     ```python
     # ── Equal Highs / Equal Lows (Gap 1) ──────────────────────────────────────
     # Master enable flag — when False, detect_eqh_eql returns ([], []) and no candidates
     # flow into draws dict. Keep True by default after initial validation.
     # Optimizer search space: [True, False]
     EQH_ENABLED: bool = True
     # Number of bars on each side for a fractal swing-point qualification.
     # A bar at index i is a swing high iff its High beats all N bars on each side.
     # Optimizer search space: [2, 3, 4]
     EQH_SWING_BARS: int = 3
     # Clustering tolerance in MNQ points. Two swing points within this distance
     # are grouped into one EQH/EQL level at their mean price.
     # Optimizer search space: [2.0, 3.0, 5.0, 8.0]
     EQH_TOLERANCE_PTS: float = 3.0
     # Minimum swing-point count to qualify as a valid EQH/EQL cluster.
     # Single swing points (count=1) are plain PDH/PDL-style levels, not EQH/EQL.
     # Optimizer search space: [2, 3]
     EQH_MIN_TOUCHES: int = 2
     # Lookback window in bars for the detection scan. ~100 bars = ~1.5 hrs of 1m data
     # (overnight + prior-session window). Larger values find more candidates but
     # include older, potentially-less-relevant levels.
     # Optimizer search space: [50, 100, 200, 400]
     EQH_LOOKBACK_BARS: int = 100
     ```
  2. Implement a private helper `_find_swing_points(highs, lows, start, end, swing_bars)`:
     - Returns two lists: `swing_highs = [(idx, price)]` and `swing_lows = [(idx, price)]`
     - For each `i in [start + swing_bars, end - swing_bars - 1]` (avoids lookahead by requiring forward window to stop at `end - swing_bars - 1`):
       - Swing high iff `highs[i] > highs[i-j]` for all `j in 1..swing_bars` AND `highs[i] > highs[i+j]` for all `j in 1..swing_bars`
       - Swing low iff `lows[i] < lows[i-j]` for all `j in 1..swing_bars` AND `lows[i] < lows[i+j]` for all `j in 1..swing_bars`
     - Pure function — no pandas accessors inside inner loop; pre-extract `.values`
  3. Implement a private helper `_cluster_swing_points(points, tolerance, min_touches)`:
     - Input: list of `(idx, price)` tuples
     - Output: list of `{"price": cluster_mean, "touches": n, "last_bar": max_idx}` dicts
     - Greedy clustering: sort points by price; walk through, accumulate into a cluster while next point is within `tolerance` of cluster's running mean; emit cluster when a point falls outside; require `touches >= min_touches` to emit
     - Sort output by `touches` desc, then `last_bar` desc
  4. Implement a private helper `_filter_stale_levels(levels, closes, bar_idx, direction)`:
     - For each level, scan `closes[level["last_bar"] + 1 : bar_idx]`:
       - For EQH: if any close > level["price"], the level is stale (closed through on the upside)
       - For EQL: if any close < level["price"], the level is stale (closed through on the downside)
     - Return only non-stale levels
  5. Implement public `detect_eqh_eql(bars, bar_idx, lookback=100, swing_bars=3, tolerance=3.0, min_touches=2)`:
     - If not EQH_ENABLED: return ([], [])
     - If `bar_idx < 2*swing_bars + min_touches` or `len(bars) == 0`: return ([], [])
     - Pre-extract `highs = bars["High"].values`, `lows = bars["Low"].values`, `closes = bars["Close"].values`
     - Set `scan_start = max(0, bar_idx - lookback)`, `scan_end = bar_idx`
     - Call `_find_swing_points(highs, lows, scan_start, scan_end, swing_bars)` to get swing_highs, swing_lows
     - Call `_cluster_swing_points(swing_highs, tolerance, min_touches)` for EQH clusters
     - Call `_cluster_swing_points(swing_lows, tolerance, min_touches)` for EQL clusters
     - Call `_filter_stale_levels(eqh_clusters, closes, bar_idx, "eqh")` and same for EQL
     - Return (active_eqh, active_eql)
- **VALIDATE**:
  - `uv run python -c "import strategy_smt as s; print(s.detect_eqh_eql.__doc__)"` — docstring visible
  - `uv run python -c "import strategy_smt as s; import pandas as pd; df = pd.DataFrame({'Open':[0]*50,'High':[100]*50,'Low':[99]*50,'Close':[99.5]*50}); print(s.detect_eqh_eql(df, 50))"` — returns `([], [])` on flat data
- **IF_FAILS**: Verify `EQH_ENABLED` is True at module level. Verify helpers use numpy arrays (not pandas Series) in hot loops.

#### Task 1.2: ADD `eqh_levels` and `eql_levels` slots to `SessionContext` in `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: data-structure-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: Tasks 2.1, 2.2, 2.3
- **PROVIDES**: `SessionContext.eqh_levels: list[dict]`, `SessionContext.eql_levels: list[dict]`
- **IMPLEMENT**:
  1. In `SessionContext.__slots__` (line 488), append `"eqh_levels"` and `"eql_levels"` to the tuple:
     ```python
     __slots__ = (
         "day", "tdo", "midnight_open", "overnight",
         "pdh", "pdl", "hyp_ctx", "hyp_dir",
         "bar_seconds", "ref_lvls",
         "eqh_levels", "eql_levels",   # Gap 1
     )
     ```
  2. In `SessionContext.__init__` signature (line 494), add two new keyword-only parameters with `None` defaults, positioned after `ref_lvls`:
     ```python
     eqh_levels: "list | None" = None,
     eql_levels: "list | None" = None,
     ```
  3. In the `__init__` body (line 507–516), assign with `or []` fallback:
     ```python
     self.eqh_levels = eqh_levels or []
     self.eql_levels = eql_levels or []
     ```
  4. In `screen_session` (line 1121–1132), update the `SessionContext` construction to include `eqh_levels=[]` and `eql_levels=[]` (for now — the wrapper is not the primary entry point for EQH; real wiring is in backtest/signal).
- **VALIDATE**:
  - `uv run python -c "from strategy_smt import SessionContext; import datetime; ctx = SessionContext(day=datetime.date(2025,1,1), tdo=100.0); print(ctx.eqh_levels, ctx.eql_levels)"` — prints `[] []`
- **IF_FAILS**: Verify `__slots__` tuple uses a trailing comma if multi-line; verify defaults are `None` not `[]` (mutable default warning).

#### Task 1.3: CREATE `tests/test_eqh_eql_detection.py` — unit tests for detect_eqh_eql

- **WAVE**: 1 (parallel-startable with Task 1.1 using a mock signature; blocks until Task 1.1 lands for final run)
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: Task 1.1 (for final pytest run); signature known from Contract 1
- **BLOCKS**: Task 3.1
- **PROVIDES**: Comprehensive unit test suite covering the detection contract
- **IMPLEMENT**: Create `tests/test_eqh_eql_detection.py` with these test cases:

  **Basic detection tests:**
  - `test_disabled_flag_returns_empty`: monkeypatch `EQH_ENABLED=False`, expect `([], [])`
  - `test_insufficient_bars_returns_empty`: pass df with 5 rows, expect `([], [])`
  - `test_flat_data_returns_empty`: 50 identical OHLC bars, expect `([], [])`
  - `test_single_swing_not_enough`: build bars with ONE clear swing high (no cluster), expect 0 EQH

  **Clustering tests:**
  - `test_two_exact_equal_highs_cluster`: build bars with two swing highs at identical price (e.g., 20100 at i=10 and i=30), expect 1 EQH with touches=2
  - `test_two_equal_highs_within_tolerance`: highs at 20100 and 20102 with tolerance=3, expect 1 EQH cluster with mean price ~20101
  - `test_two_equal_highs_outside_tolerance`: highs at 20100 and 20110 with tolerance=3, expect 2 separate clusters (both below min_touches=2, so 0 active EQH)
  - `test_three_equal_highs_one_cluster`: three highs within tolerance; expect 1 cluster with touches=3
  - `test_min_touches_filter`: two highs within tolerance, but `min_touches=3` — expect 0 active levels
  - `test_eql_symmetric`: mirror of "two equal lows" case; verify EQL detection works identically

  **Staleness tests (critical — staleness bugs cause regressions):**
  - `test_stale_level_filtered`: build EQH at price 20100, then a bar whose Close is 20150 (closes through EQH upward), expect 0 active EQH
  - `test_wick_through_level_still_active`: build EQH at 20100, add a bar with High 20105 but Close 20099 (wicked through but closed below), expect EQH still active
  - `test_eql_stale_below`: EQL at 20000, then a bar with Close 19950 (closes through downward), expect 0 active EQL
  - `test_wick_through_eql_still_active`: EQL at 20000, bar with Low 19995 but Close 20001, expect EQL still active
  - `test_stale_scan_window`: EQH at bar 5, bar 8 closes through, bar 9+ doesn't — verify staleness scans FROM last_bar+1 TO bar_idx (not just the last few bars)

  **Lookahead tests (critical — lookahead bugs cause backtest overfit):**
  - `test_no_lookahead_in_swing_detection`: pass `bar_idx=20`; build bars such that bar 25 would be a swing high. Verify it's NOT detected (bar_idx=20 means scan ends at 20-swing_bars-1 = 16)
  - `test_swing_at_bar_end_filtered`: bars with a swing high exactly at bar_idx-1 (which has no forward window) — must not qualify

  **Ranking tests:**
  - `test_sort_by_touches_desc`: two EQH clusters, one with 3 touches, one with 2 — verify `eqh[0]["touches"] == 3`
  - `test_tiebreak_by_recency`: two EQH clusters with identical touches=2, different last_bar values — verify the one with later last_bar comes first

  **Use the following helper to build test DataFrames** (add near the top of the test file):
  ```python
  def _bars(patterns):
      """patterns: list of (open, high, low, close) tuples. Returns DataFrame with DatetimeIndex."""
      ts_start = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
      idx = pd.date_range(start=ts_start, periods=len(patterns), freq="1min")
      return pd.DataFrame(
          patterns, columns=["Open","High","Low","Close"], index=idx,
      ).assign(Volume=100.0)
  ```

- **VALIDATE**: `uv run pytest tests/test_eqh_eql_detection.py -v` — all tests pass
- **IF_FAILS**: Inspect specific failing tests; staleness and lookahead bugs are the most likely culprits.

---

### WAVE 2: Integration

#### Task 2.1: UPDATE `backtest_smt.py` — wire EQH/EQL into per-day SessionContext

- **WAVE**: 2
- **AGENT_ROLE**: integration-engineer
- **DEPENDS_ON**: Tasks 1.1, 1.2
- **BLOCKS**: Tasks 3.1, 3.2
- **PROVIDES**: Each per-day `SessionContext` in `run_backtest` carries populated `eqh_levels`/`eql_levels` from prior-day + overnight MNQ bars
- **IMPLEMENT**:
  1. Extend imports at `backtest_smt.py:13–48`:
     ```python
     # Add to the `from strategy_smt import (` block:
     detect_eqh_eql,
     EQH_ENABLED, EQH_SWING_BARS, EQH_TOLERANCE_PTS, EQH_MIN_TOUCHES, EQH_LOOKBACK_BARS,
     ```
  2. Between the `_day_pdh, _day_pdl = compute_pdh_pdl(...)` call (line 451) and the `_session_hyp_ctx = compute_hypothesis_context(...)` call (line 454), add a new EQH/EQL computation block:
     ```python
     # Gap 1: compute EQH/EQL from prior-day + overnight MNQ bars for secondary_target candidate pool
     _day_eqh: list = []
     _day_eql: list = []
     if EQH_ENABLED:
         # Use MNQ bars from prior trading day + today's overnight (up to session start).
         # This window matches the "institutional memory" that the session will react to.
         _session_start_ts = mnq_session.index[0]
         _eqh_window_start = _session_start_ts - pd.Timedelta(days=2)  # prior day + overnight
         _eqh_bars = mnq_df[
             (mnq_df.index >= _eqh_window_start) & (mnq_df.index < _session_start_ts)
         ]
         if not _eqh_bars.empty:
             _day_eqh, _day_eql = detect_eqh_eql(
                 _eqh_bars, len(_eqh_bars),
                 lookback=EQH_LOOKBACK_BARS,
                 swing_bars=EQH_SWING_BARS,
                 tolerance=EQH_TOLERANCE_PTS,
                 min_touches=EQH_MIN_TOUCHES,
             )
     ```
  3. Update the `SessionContext(...)` construction at line 526 to pass the new kwargs:
     ```python
     _session_ctx = SessionContext(
         day=day,
         tdo=day_tdo,
         midnight_open=_day_midnight_open,
         overnight=_day_overnight,
         pdh=_day_pdh, pdl=_day_pdl,
         hyp_ctx=_session_hyp_ctx, hyp_dir=_session_hyp_dir,
         bar_seconds=bar_seconds,
         ref_lvls=_bt_ref_lvls,
         eqh_levels=_day_eqh,    # Gap 1
         eql_levels=_day_eql,    # Gap 1
     )
     ```
- **VALIDATE**: `uv run python -c "import backtest_smt; print('imports OK')"` succeeds with no errors.
- **IF_FAILS**: Verify imports list is syntactically correct (commas, no stray characters).

#### Task 2.2: UPDATE `signal_smt.py` — wire EQH/EQL into realtime SessionContext

- **WAVE**: 2
- **AGENT_ROLE**: integration-engineer
- **DEPENDS_ON**: Tasks 1.1, 1.2
- **BLOCKS**: Task 3.2
- **PROVIDES**: Realtime `SessionContext` init carries populated `eqh_levels`/`eql_levels`
- **IMPLEMENT**:
  1. Extend imports at `signal_smt.py:22–32`:
     ```python
     # Add to the `from strategy_smt import (` block:
     detect_eqh_eql,
     EQH_ENABLED, EQH_SWING_BARS, EQH_TOLERANCE_PTS, EQH_MIN_TOUCHES, EQH_LOOKBACK_BARS,
     ```
  2. In the session init block (line 497–513), after `_hyp_ctx` is computed and before the `SessionContext(...)` construction at line 506, add:
     ```python
     # Gap 1: compute EQH/EQL from the loaded 1m history (pre-session + overnight slice)
     _eqh_levels: list = []
     _eql_levels: list = []
     if EQH_ENABLED and _mnq_1m_df is not None and not _mnq_1m_df.empty:
         _session_start_dt = pd.Timestamp(f"{today} {SESSION_START}", tz="America/New_York")
         _eqh_window_start = _session_start_dt - pd.Timedelta(days=2)
         _eqh_bars = _mnq_1m_df[
             (_mnq_1m_df.index >= _eqh_window_start) & (_mnq_1m_df.index < _session_start_dt)
         ]
         if not _eqh_bars.empty:
             _eqh_levels, _eql_levels = detect_eqh_eql(
                 _eqh_bars, len(_eqh_bars),
                 lookback=EQH_LOOKBACK_BARS,
                 swing_bars=EQH_SWING_BARS,
                 tolerance=EQH_TOLERANCE_PTS,
                 min_touches=EQH_MIN_TOUCHES,
             )
     ```
  3. Update the `SessionContext(...)` construction at line 506 to pass the new kwargs:
     ```python
     _session_ctx = strategy_smt.SessionContext(
         day=today, tdo=tdo,
         midnight_open=midnight_open_price,
         overnight=overnight_range or {},
         pdh=_day_pdh, pdl=_day_pdl,
         hyp_ctx=_hyp_ctx, hyp_dir=_hyp_dir,
         bar_seconds=1.0,
         eqh_levels=_eqh_levels,
         eql_levels=_eql_levels,
     )
     ```
- **VALIDATE**: `uv run python -c "import signal_smt; print('imports OK')"` — module-level import executes (side effect: reads .env; that's fine).
- **IF_FAILS**: Verify the `SESSION_START` constant is imported in signal_smt (yes at line 46 — `SIGNAL_SESSION_END` aliases). The pattern `pd.Timestamp(f"{today} {SESSION_START}", tz="America/New_York")` must reference signal_smt's SESSION_START (09:00), not strategy's.

#### Task 2.3: UPDATE `_build_draws_and_select` in `strategy_smt.py` — inject EQH/EQL candidates

- **WAVE**: 2
- **AGENT_ROLE**: core-logic-engineer
- **DEPENDS_ON**: Tasks 1.1, 1.2
- **BLOCKS**: Tasks 3.1, 3.2
- **PROVIDES**: EQH/EQL levels participate in `select_draw_on_liquidity` candidate selection for both primary and secondary target picking
- **IMPLEMENT**:
  1. Modify `_build_draws_and_select` at `strategy_smt.py:1355–1380`. Change the signature to accept `eqh_levels` and `eql_levels`:
     ```python
     def _build_draws_and_select(
         direction: str, ep: float, sp: float, fvg_zone: "dict | None",
         day_tdo: "float | None", midnight_open: "float | None",
         run_ses_high: float, run_ses_low: float,
         overnight: dict, pdh: "float | None", pdl: "float | None",
         eqh_levels: "list | None" = None,
         eql_levels: "list | None" = None,
     ) -> "tuple[str | None, float | None, str | None, float | None]":
     ```
  2. After the existing `draws` dict construction (inside the `if direction == "long"` branch ~line 1362–1370 and the `else` branch ~1372–1379), add:
     ```python
     # Long branch addition (after existing draws dict):
     if eqh_levels:
         # Nearest active EQH ABOVE entry price — EQH is only meaningful for long TPs
         nearest = next(
             (lvl for lvl in sorted(eqh_levels, key=lambda l: l["price"]) if lvl["price"] > ep),
             None,
         )
         draws["eqh"] = nearest["price"] if nearest else None

     # Short branch addition (after existing draws dict):
     if eql_levels:
         # Nearest active EQL BELOW entry price
         nearest = next(
             (lvl for lvl in sorted(eql_levels, key=lambda l: -l["price"]) if lvl["price"] < ep),
             None,
         )
         draws["eql"] = nearest["price"] if nearest else None
     ```
  3. Update the single caller inside `process_scan_bar` at `strategy_smt.py:~1724` (search for `_build_draws_and_select(`):
     ```python
     _tp_name, _day_tp, _sec_tp_name, _sec_tp = _build_draws_and_select(
         state.pending_direction, _ep, _sp, state.pending_fvg_zone,
         context.tdo, context.midnight_open, run_ses_high, run_ses_low,
         context.overnight, context.pdh, context.pdl,
         eqh_levels=context.eqh_levels, eql_levels=context.eql_levels,
     )
     ```
- **VALIDATE**:
  - `uv run python -c "from strategy_smt import _build_draws_and_select; print(_build_draws_and_select.__code__.co_varnames[:12])"` — includes `eqh_levels`, `eql_levels`
- **IF_FAILS**: Check the caller update — only ONE call site exists; the signature change propagates cleanly only if the caller is updated.

---

### WAVE 3: Validation

#### Task 3.1: CREATE integration test — synthetic bars, verify EQH is picked as secondary target

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: Tasks 2.1, 2.3
- **BLOCKS**: Task 3.2
- **PROVIDES**: End-to-end test exercising detection → SessionContext → `_build_draws_and_select`
- **IMPLEMENT**: Add to `tests/test_eqh_eql_detection.py` (or create `tests/test_eqh_eql_integration.py` — prefer the former to keep related tests together):

  ```python
  def test_build_draws_includes_eqh_candidate():
      """Verify _build_draws_and_select picks an EQH level when it is the nearest valid candidate above entry."""
      from strategy_smt import _build_draws_and_select

      # entry 100, stop 98 (stop_dist=2). EQH at 105 (5pt away, beats TDO=110).
      eqh_levels = [{"price": 105.0, "touches": 3, "last_bar": 50}]
      eql_levels = []

      tp_name, tp_price, sec_name, sec_price = _build_draws_and_select(
          direction="long", ep=100.0, sp=98.0, fvg_zone=None,
          day_tdo=110.0, midnight_open=None,
          run_ses_high=120.0, run_ses_low=95.0,
          overnight={"overnight_high": 115.0, "overnight_low": 95.0},
          pdh=125.0, pdl=90.0,
          eqh_levels=eqh_levels, eql_levels=eql_levels,
      )
      # EQH at 105 is nearest above 100; should be primary target
      assert tp_name == "eqh"
      assert tp_price == 105.0

  def test_build_draws_skips_eqh_when_below_entry_for_long():
      """EQH must be ABOVE entry price for longs to qualify as a target."""
      from strategy_smt import _build_draws_and_select
      eqh_levels = [{"price": 95.0, "touches": 3, "last_bar": 50}]
      tp_name, tp_price, _, _ = _build_draws_and_select(
          direction="long", ep=100.0, sp=98.0, fvg_zone=None,
          day_tdo=110.0, midnight_open=None,
          run_ses_high=120.0, run_ses_low=95.0,
          overnight={"overnight_high": 115.0, "overnight_low": 95.0},
          pdh=125.0, pdl=90.0,
          eqh_levels=eqh_levels, eql_levels=[],
      )
      # EQH at 95 is below entry 100 — not valid for long TP. TDO at 110 wins.
      assert tp_name == "tdo"

  def test_build_draws_short_picks_eql():
      """Mirror test for shorts picking EQL."""
      from strategy_smt import _build_draws_and_select
      eql_levels = [{"price": 95.0, "touches": 3, "last_bar": 50}]
      tp_name, tp_price, _, _ = _build_draws_and_select(
          direction="short", ep=100.0, sp=102.0, fvg_zone=None,
          day_tdo=90.0, midnight_open=None,
          run_ses_high=105.0, run_ses_low=80.0,
          overnight={"overnight_high": 105.0, "overnight_low": 85.0},
          pdh=110.0, pdl=75.0,
          eqh_levels=[], eql_levels=eql_levels,
      )
      assert tp_name == "eql"
      assert tp_price == 95.0

  def test_build_draws_empty_eqh_degrades_gracefully():
      """When eqh_levels/eql_levels are empty lists, behavior is identical to before Gap 1."""
      from strategy_smt import _build_draws_and_select
      tp_name, tp_price, _, _ = _build_draws_and_select(
          direction="long", ep=100.0, sp=98.0, fvg_zone=None,
          day_tdo=110.0, midnight_open=None,
          run_ses_high=120.0, run_ses_low=95.0,
          overnight={"overnight_high": 115.0, "overnight_low": 95.0},
          pdh=125.0, pdl=90.0,
          eqh_levels=[], eql_levels=[],
      )
      # TDO at 110 is the nearest candidate above 100 from the existing pool
      assert tp_name == "tdo"
  ```
- **VALIDATE**: `uv run pytest tests/test_eqh_eql_detection.py -v -k "build_draws"` — all pass
- **IF_FAILS**: Trace `select_draw_on_liquidity` to confirm it correctly ranks by distance; likely the default ranking hasn't changed.

#### Task 3.2: ADD regression smoke test for full backtest — trades.tsv shape unchanged, non-empty

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: Task 3.1 (integration contract validated); Task 2.1 (backtest wiring)
- **BLOCKS**: []
- **PROVIDES**: Confidence that enabling EQH/EQL does not break the existing backtest pipeline
- **IMPLEMENT**:
  1. Add a new test class or module-level test in `tests/test_smt_backtest.py`:
     ```python
     def test_eqh_enabled_backtest_runs_and_produces_trades(monkeypatch, tmp_path):
         """Smoke test: EQH_ENABLED=True must not break existing backtest; trades.tsv shape preserved."""
         import strategy_smt as _strat
         import backtest_smt as _bt

         # Run two quick backtests, only differing by EQH_ENABLED, on the same minimal synthetic data.
         # Assert: both produce a trades list of the same length/shape (proving EQH doesn't reject trades),
         # and the EQH-enabled version picks "eqh" as tp_name on at least one trade where EQH is the nearest candidate
         # (requires synthetic bars to have a detectable EQH pattern).

         # Use the existing _build_short_signal_bars helper if present, or build bars that:
         #  1. Produce a short SMT signal on the current day
         #  2. Have two identical-price swing highs in the prior day's window forming an EQH
         #  3. Allow one full trade to fill and exit

         # Assert imports don't error
         monkeypatch.setattr(_strat, "EQH_ENABLED", True)
         # Build fixture; run backtest; assert trades list is non-empty and has expected columns.
         # At minimum verify: no AttributeError on context.eqh_levels; trades.tsv has same column headers.
         pass  # Implementation detail — executor to write based on existing test_smt_backtest patterns
     ```
  2. Also add a negative test:
     ```python
     def test_eqh_disabled_preserves_baseline_behavior(monkeypatch):
         """When EQH_ENABLED=False, detect_eqh_eql returns ([], []) and baseline behavior is preserved."""
         import strategy_smt as _strat
         monkeypatch.setattr(_strat, "EQH_ENABLED", False)
         # Call detect_eqh_eql with any bars
         import pandas as pd
         ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
         idx = pd.date_range(start=ts, periods=50, freq="1min")
         df = pd.DataFrame({"Open":[20000]*50,"High":[20100]*50,"Low":[19900]*50,"Close":[20000]*50,"Volume":[100]*50}, index=idx)
         eqh, eql = _strat.detect_eqh_eql(df, 50)
         assert eqh == []
         assert eql == []
     ```
- **VALIDATE**:
  - `uv run pytest tests/test_smt_backtest.py -v -k "eqh"` — both tests pass
  - `uv run pytest tests/ -x --timeout=120` — no regressions across the whole test suite (full suite smoke)
- **IF_FAILS**: If existing backtest tests fail, verify `SessionContext` default-value behavior (unset `eqh_levels`/`eql_levels` must default to `[]`, NOT raise AttributeError).

---

## TESTING STRATEGY

### Test Framework

- **Framework**: `pytest` with `uv run` runner (matching repo convention)
- **Fixture pattern**: In-function DataFrame builders (see `_bars` helper in Task 1.3) — avoids cross-test state
- **Monkeypatch pattern**: Follow `tests/test_smt_backtest.py` lines 1–20 guidance — patch constants on BOTH `strategy_smt` and (where imported) `backtest_smt` to stay consistent

### Test Organization

| File | Purpose |
|------|---------|
| `tests/test_eqh_eql_detection.py` (new) | Unit tests for `detect_eqh_eql` and its helpers; integration tests for `_build_draws_and_select` with EQH/EQL candidates |
| `tests/test_smt_backtest.py` (extended) | Regression smoke test that full backtest runs with EQH enabled |

### Test Automation Summary

- **Total test cases**: 22
- **Automated**: 22 (100%) — all pure-function tests, no external services
- **Manual**: 0 — no human-only validation needed
- **Tool**: pytest + pandas DataFrame builders
- **Run command**: `uv run pytest tests/test_eqh_eql_detection.py tests/test_smt_backtest.py -v`

### Coverage Review Pass

| Code path | Test | Status |
|-----------|------|--------|
| `detect_eqh_eql` entry: `EQH_ENABLED=False` | `test_disabled_flag_returns_empty` | ✅ |
| `detect_eqh_eql` entry: insufficient bars | `test_insufficient_bars_returns_empty` | ✅ |
| `_find_swing_points`: no swings | `test_flat_data_returns_empty` | ✅ |
| `_find_swing_points`: single swing | `test_single_swing_not_enough` | ✅ |
| `_find_swing_points`: lookahead boundary | `test_no_lookahead_in_swing_detection`, `test_swing_at_bar_end_filtered` | ✅ |
| `_cluster_swing_points`: exact match | `test_two_exact_equal_highs_cluster` | ✅ |
| `_cluster_swing_points`: within tolerance | `test_two_equal_highs_within_tolerance` | ✅ |
| `_cluster_swing_points`: outside tolerance | `test_two_equal_highs_outside_tolerance` | ✅ |
| `_cluster_swing_points`: 3+ touches | `test_three_equal_highs_one_cluster` | ✅ |
| `_cluster_swing_points`: min_touches filter | `test_min_touches_filter` | ✅ |
| `_cluster_swing_points`: symmetry (EQL) | `test_eql_symmetric` | ✅ |
| `_filter_stale_levels`: EQH closed through | `test_stale_level_filtered` | ✅ |
| `_filter_stale_levels`: wick-only preserves | `test_wick_through_level_still_active` | ✅ |
| `_filter_stale_levels`: EQL closed through | `test_eql_stale_below` | ✅ |
| `_filter_stale_levels`: wick-only EQL | `test_wick_through_eql_still_active` | ✅ |
| `_filter_stale_levels`: scan window span | `test_stale_scan_window` | ✅ |
| Result sort: touches desc | `test_sort_by_touches_desc` | ✅ |
| Result sort: recency tiebreak | `test_tiebreak_by_recency` | ✅ |
| `SessionContext` new slots | implicit via Task 1.2 validate line | ✅ |
| `_build_draws_and_select`: long picks EQH | `test_build_draws_includes_eqh_candidate` | ✅ |
| `_build_draws_and_select`: reject EQH below long entry | `test_build_draws_skips_eqh_when_below_entry_for_long` | ✅ |
| `_build_draws_and_select`: short picks EQL | `test_build_draws_short_picks_eql` | ✅ |
| `_build_draws_and_select`: empty degrades | `test_build_draws_empty_eqh_degrades_gracefully` | ✅ |
| Full backtest smoke w/ EQH enabled | `test_eqh_enabled_backtest_runs_and_produces_trades` | ✅ |
| EQH disabled baseline | `test_eqh_disabled_preserves_baseline_behavior` | ✅ |

All code paths covered by automated tests.

### Script Deliverables Check

This feature introduces no new runnable scripts — it modifies existing Python modules only. No script-level runnability, encoding, or subprocess env concerns apply.

---

## RISKS

### Risk 1: Staleness logic bugs feed swept levels into DOL pool

**Impact**: HIGH. Feeding stale (already-swept) levels into `_build_draws_and_select` causes secondary_target to pick levels that price has already cleared. Price ignores these levels, runs past them, and frequently reverses at the NEXT real liquidity level — meaning our "target" never fires. Current mechanics would then close trades at session end or initial stop, both worse than correctly-placed secondary exits.

**Mitigation**:
1. Adversarial unit tests in Task 1.3 specifically cover wick-vs-close distinction (`test_wick_through_level_still_active`, `test_stale_level_filtered`)
2. `EQH_ENABLED` master flag allows immediate rollback if regression detected
3. Staleness check uses closes array — unambiguous semantics (a close > level for EQH = stale; anything else = active)

### Risk 2: Lookahead leaks in swing detection

**Impact**: HIGH. If `_find_swing_points` scans bars forward of `bar_idx`, backtest performance will be artificially inflated (the function "knows" future swing completions). Deployment to live trading would then underperform versus backtest.

**Mitigation**:
1. Scan window explicitly bounded to `[scan_start + swing_bars, bar_idx - swing_bars - 1]` — forward window (`i + swing_bars`) never reaches `bar_idx`
2. Dedicated test `test_no_lookahead_in_swing_detection` builds a pattern where a future bar would qualify if lookahead existed; asserts it does NOT appear in results
3. All hot-loop access uses pre-extracted numpy arrays; bar indices are explicit integers (no pandas iloc/tz gymnastics that could hide slicing bugs)

### Risk 3: Per-session computation cost slows backtest

**Impact**: MEDIUM. Backtest processes ~500 session-days across walk-forward folds. Running `detect_eqh_eql` on ~2 days of 1m bars per session = up to 3,000 bars × 500 sessions = 1.5M inner-loop iterations. If the Python loop is slow, backtest runtime grows noticeably.

**Mitigation**:
1. Inner loop uses numpy arrays, not pandas Series — orders-of-magnitude faster
2. Short-circuit on `EQH_ENABLED=False` returns immediately
3. Lookback capped by `EQH_LOOKBACK_BARS=100` — per-session detection is O(100) swing-window comparisons + small clustering pass, bounded well under 1ms per session

### Risk 4: Signature change to `_build_draws_and_select` breaks tests

**Impact**: LOW. The function has a single call site (inside `process_scan_bar`). Adding keyword-only params with None defaults is backward compatible for direct callers, but any test that calls it positionally must be updated.

**Mitigation**:
1. New parameters are keyword-only (`eqh_levels=None`, `eql_levels=None`) — positional callers unaffected
2. Default values produce empty behaviour — tests that don't care about EQH don't need changes
3. Task 2.3 explicitly updates the single caller; any other hits in test files will be caught by Task 3.2's full-suite smoke test

---

## INTEGRATION POINTS

- **Inbound dependencies**: `MNQ 1m parquet data` (prior-day + overnight slice required); `SessionContext` consumer (read-only)
- **Outbound dependencies**: None — EQH/EQL data is consumed only by `_build_draws_and_select` inside `process_scan_bar`
- **Configuration**: 5 new constants in `strategy_smt.py` (`EQH_ENABLED`, `EQH_SWING_BARS`, `EQH_TOLERANCE_PTS`, `EQH_MIN_TOUCHES`, `EQH_LOOKBACK_BARS`)
- **Optimizer hooks**: All 5 constants have documented search spaces for future optimizer sweeps

---

## ROLLBACK

**Single-flag rollback**: Set `EQH_ENABLED = False` at the top of `strategy_smt.py` constants. `detect_eqh_eql` returns `([], [])`; SessionContext slots stay `[]`; `_build_draws_and_select` ignores empty candidate lists and falls back to the existing 6-candidate pool. No other code changes needed.

**Full revert**: `git checkout strategy_smt.py backtest_smt.py signal_smt.py && git rm tests/test_eqh_eql_detection.py` (if committed). All changes are purely additive, so revert is clean.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `detect_eqh_eql(bars, bar_idx, ...)` returns a tuple `(list[dict], list[dict])` where each dict has keys `{"price": float, "touches": int, "last_bar": int}`
- [ ] Returns `([], [])` when `EQH_ENABLED=False` regardless of input bars
- [ ] Returns `([], [])` when input bars has length < `2 * swing_bars + min_touches`
- [ ] Detects a cluster of 2+ swing highs at the same price within `EQH_TOLERANCE_PTS` as a single EQH level
- [ ] Detects the symmetric EQL case identically
- [ ] Filters out levels where any bar's Close (between `last_bar+1` and `bar_idx`) has passed through the level (EQH: close above; EQL: close below)
- [ ] Does NOT filter levels where bars only wicked through but closed back on the correct side
- [ ] Does NOT use lookahead: swing point detection only scans bars in `[scan_start + swing_bars, bar_idx - swing_bars - 1]`
- [ ] Sorts returned levels by `touches` desc, then `last_bar` desc
- [ ] `SessionContext` has `eqh_levels` and `eql_levels` attributes (default `[]`)
- [ ] `_build_draws_and_select` adds `draws["eqh"]` (long branch) / `draws["eql"]` (short branch) when candidate levels exist on the correct side of entry
- [ ] TDO remains the primary TP when it is still the closest valid candidate

### Integration / E2E
- [ ] `backtest_smt.py` runs end-to-end with `EQH_ENABLED=True`, produces a non-empty `trades.tsv` with unchanged column schema
- [ ] `signal_smt.py` imports without errors and can construct a `SessionContext` with new `eqh_levels`/`eql_levels` kwargs
- [ ] Full pytest suite runs without regressions (no tests broken by signature/slot changes)

### Validation (commands)
- [ ] `uv run pytest tests/test_eqh_eql_detection.py -v` — all 22 unit tests pass
- [ ] `uv run pytest tests/test_smt_backtest.py -v -k "eqh"` — both new integration tests pass
- [ ] `uv run pytest tests/ -x --timeout=120` — full suite passes with no regressions
- [ ] `uv run python -c "from strategy_smt import detect_eqh_eql, SessionContext, EQH_ENABLED"` — clean import, no circular dependencies

### Non-Functional
- [ ] Rollback safety: setting `EQH_ENABLED=False` restores baseline behavior — no draws dict changes, no performance impact, no signal path divergence
- [ ] Detection cost is bounded: per-session call to `detect_eqh_eql` with default parameters completes in <10ms on typical bar data (numpy-array inner loops, no pandas iteration inside hot paths)

### Out of Scope
- Changes to TDO as primary TP (TDO remains primary; EQH only adds to the candidate pool)
- Changes to stop/partial/trail mechanics (all locked from prior rejected campaign)
- Changes to signal quality / divergence scoring (Gap 2 is a separate feature)
- Conviction-based position sizing (discussed but not part of this plan)
- "Trail after secondary hit" hybrid (separate future feature)
- Backtest re-optimization of EQH constants (this plan ships with one default set; sweeps are a later task)

---

## OPEN QUESTIONS

None at plan-write time. Any ambiguity during execution should be resolved by defaulting to the most conservative interpretation of the above specs (especially for staleness and lookahead logic).
