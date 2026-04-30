# SMT Performance Optimization Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the top CPU bottlenecks across strategy_smt.py, backtest_smt.py, and hypothesis_smt.py without changing any trading logic or backtest outputs.

**Architecture:** Pure performance refactoring — same trade results, faster execution. Three change families: (1) replace per-bar slice.max/min with incremental running extremes passed as `_cached` to `detect_smt_divergence`; (2) replace `iterrows()` and `.iloc`-in-loops with numpy `.values` array access; (3) remove unnecessary DataFrame `.copy()` calls. Existing tests are the regression suite — no new tests needed.

**Tech Stack:** Python 3.10+, pandas, numpy (already in project)

---

## Files Modified

| File | Changes |
|------|---------|
| `strategy_smt.py` | `detect_smt_divergence` add `_cached`, `detect_fvg` vectorize, `screen_session` running extremes |
| `backtest_smt.py` | Main loop `iterrows()` → `_BarRow`, `.copy()` removal, running session extremes, overnight sweep running max/min |
| `hypothesis_smt.py` | `_assign_case` iterrows, `_compute_rule3` iterrows, `_compute_rule4` iterrows |

---

### Task 1: Baseline — establish test pass/fail before any changes

**Files:** none

- [ ] **Step 1: Run the relevant test files and record results**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_hypothesis_smt.py tests/test_smt_position_arch.py -v 2>&1 | tail -30
```

Expected: document which tests pass, which fail. These are the regression baseline.

---

### Task 2: Fix `detect_fvg` — pre-extract .values, eliminate redundant range guard

**Files:**
- Modify: `strategy_smt.py:508-541`

The loop currently calls 4× `.iloc[]` per bar. Pre-extracting `.values` arrays makes indexing O(1) with no attribute lookup overhead. The guard `if i + 2 >= bar_idx: continue` always fires on the first iteration (when `i = bar_idx-2`, `i+2 = bar_idx`), so start the range at `bar_idx-3` instead.

- [ ] **Step 1: Replace detect_fvg body**

Replace lines 508-541 in `strategy_smt.py` with:

```python
def detect_fvg(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
    lookback: int = 20,
) -> dict | None:
    """Find the most recent Fair Value Gap in the given direction before bar_idx.

    Bullish FVG (long): bar3.Low > bar1.High — gap on the way up.
      Zone = [bar1.High, bar3.Low]; price retraces down into this zone for Layer B.
    Bearish FVG (short): bar3.High < bar1.Low — gap on the way down.
      Zone = [bar3.High, bar1.Low]; price retraces up into this zone for Layer B.

    Returns {"fvg_high": float, "fvg_low": float, "fvg_bar": int} or None.
    Always None when FVG_ENABLED is False.
    """
    if not FVG_ENABLED:
        return None
    # bar3 = i+2 must be strictly < bar_idx → i <= bar_idx-3
    if bar_idx < 3:
        return None
    start = max(0, bar_idx - lookback)
    if start > bar_idx - 3:
        return None
    # Pre-extract arrays once — avoids 4× .iloc overhead per iteration
    highs = bars["High"].values
    lows  = bars["Low"].values
    for i in range(bar_idx - 3, start - 1, -1):
        bar1_h = highs[i]
        bar1_l = lows[i]
        bar3_h = highs[i + 2]
        bar3_l = lows[i + 2]
        if direction == "long":
            if bar3_l > bar1_h and (bar3_l - bar1_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar3_l, "fvg_low": bar1_h, "fvg_bar": i}
        else:  # short
            if bar3_h < bar1_l and (bar1_l - bar3_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar1_l, "fvg_low": bar3_h, "fvg_bar": i}
    return None
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -v 2>&1 | tail -20
```

Expected: same pass/fail as baseline.

- [ ] **Step 3: Commit**

```bash
git add strategy_smt.py
git commit -m "perf: vectorize detect_fvg with .values arrays, fix range to avoid redundant guard iteration"
```

---

### Task 3: Fix `detect_smt_divergence` — add optional `_cached` extremes parameter

**Files:**
- Modify: `strategy_smt.py:318-401`

The function recomputes session high/low/close-extremes with `slice.max()` / `slice.min()` on every call. These are O(n) where n grows to ~400 bars by end of session. Add optional `_cached` parameter: when provided, use those values directly; when absent, fall back to original slice computation (backward compatible — all existing callers without `_cached` continue working unchanged).

- [ ] **Step 1: Replace detect_smt_divergence with _cached-aware version**

Replace the entire `detect_smt_divergence` function in `strategy_smt.py` (lines 318-401):

```python
def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
    _min_bars: int = 0,
    _cached: "dict | None" = None,
) -> tuple[str, float, float, str, float] | None:
    """Check for SMT divergence at bar_idx.

    Returns tuple (direction, sweep_pts, miss_pts, smt_type, smt_defended_level) or None.
    - direction: "short" if MES makes new session high but MNQ does not;
                 "long"  if MES makes new session low  but MNQ does not.
    - sweep_pts: how far MES exceeded the session extreme (always >= 0)
    - miss_pts:  how far MNQ failed to match MES (always >= 0)
    - smt_type: "wick" for high/low-based divergence; "body" for close-based (hidden SMT)
    - smt_defended_level: MNQ session extreme MNQ failed to match
    Returns None if no divergence, bar-count guard fires, or sweep/miss filters reject.

    Args:
        mes_bars: OHLCV DataFrame for MES, index = ET datetime (any bar interval)
        mnq_bars: OHLCV DataFrame for MNQ, same index alignment
        bar_idx: current bar position in the session slice
        session_start_idx: first bar index of current session
        _min_bars: Skip bars where bar_idx - session_start_idx < _min_bars.
            Default 0 disables the guard — callers should apply their own
            time-based threshold (e.g. screen_session uses MIN_BARS_BEFORE_SIGNAL
            as a wall-clock timedelta, which is interval-agnostic).
        _cached: optional dict with pre-computed session extremes for this bar:
            keys "mes_h", "mes_l", "mnq_h", "mnq_l" (wick-based),
            and "mes_ch", "mes_cl", "mnq_ch", "mnq_cl" (close-based for HIDDEN_SMT).
            When provided, skips the O(n) slice.max/min computation.
            Values should be nan (not -inf/+inf) when the session is empty (bar 0).
    """
    if bar_idx - session_start_idx < _min_bars:
        return None

    # Session extremes: use pre-computed values if available, else compute from slice
    if _cached is not None:
        mes_session_high = _cached["mes_h"]
        mes_session_low  = _cached["mes_l"]
        mnq_session_high = _cached["mnq_h"]
        mnq_session_low  = _cached["mnq_l"]
    else:
        session_slice = slice(session_start_idx, bar_idx)
        mes_session_high = mes_bars["High"].iloc[session_slice].max()
        mes_session_low  = mes_bars["Low"].iloc[session_slice].min()
        mnq_session_high = mnq_bars["High"].iloc[session_slice].max()
        mnq_session_low  = mnq_bars["Low"].iloc[session_slice].min()

    cur_mes = mes_bars.iloc[bar_idx]
    cur_mnq = mnq_bars.iloc[bar_idx]

    # Bearish SMT: MES sweeps session high (liquidity grab) but MNQ fails to confirm
    if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
        smt_sweep = cur_mes["High"] - mes_session_high
        mnq_miss   = mnq_session_high - cur_mnq["High"]
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("short", smt_sweep, mnq_miss, "wick", mnq_session_high)
    # Bullish SMT: MES sweeps session low but MNQ fails to confirm
    if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
        smt_sweep = mes_session_low - cur_mes["Low"]
        mnq_miss   = cur_mnq["Low"] - mnq_session_low
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("long", smt_sweep, mnq_miss, "wick", mnq_session_low)

    # Hidden SMT: body/close-based divergence (fires only when wick SMT did not).
    # MES close makes new session extreme but MNQ close does not confirm.
    if HIDDEN_SMT_ENABLED:
        if _cached is not None:
            mes_close_session_high = _cached["mes_ch"]
            mnq_close_session_high = _cached["mnq_ch"]
            mes_close_session_low  = _cached["mes_cl"]
            mnq_close_session_low  = _cached["mnq_cl"]
        else:
            session_slice = slice(session_start_idx, bar_idx)
            mes_close_session_high = mes_bars["Close"].iloc[session_slice].max()
            mnq_close_session_high = mnq_bars["Close"].iloc[session_slice].max()
            mes_close_session_low  = mes_bars["Close"].iloc[session_slice].min()
            mnq_close_session_low  = mnq_bars["Close"].iloc[session_slice].min()
        if cur_mes["Close"] > mes_close_session_high and cur_mnq["Close"] <= mnq_close_session_high:
            smt_sweep = cur_mes["Close"] - mes_close_session_high
            mnq_miss   = mnq_close_session_high - cur_mnq["Close"]
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "body", mnq_close_session_high)
        if cur_mes["Close"] < mes_close_session_low and cur_mnq["Close"] >= mnq_close_session_low:
            smt_sweep = mes_close_session_low - cur_mes["Close"]
            mnq_miss   = cur_mnq["Close"] - mnq_close_session_low
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("long", smt_sweep, mnq_miss, "body", mnq_close_session_low)
    return None
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -v 2>&1 | tail -20
```

Expected: same pass/fail as baseline (no callers pass `_cached` yet, so no behavior change).

- [ ] **Step 3: Commit**

```bash
git add strategy_smt.py
git commit -m "perf: add optional _cached extremes to detect_smt_divergence, skip O(n) slice.max/min when provided"
```

---

### Task 4: Fix `screen_session` — maintain running session extremes

**Files:**
- Modify: `strategy_smt.py:672-796`

`screen_session` calls `detect_smt_divergence` once per bar in a loop, which would compute session extremes from scratch each time (without `_cached`). Add pre-extracted numpy arrays and maintain running extremes updated at the start of each iteration for the previous bar. This eliminates 2M+ aggregation ops per backtest run.

The "update at start for previous bar" pattern is critical: if the update were at the end, any `continue` statement in the loop would skip the update, corrupting the running extremes.

- [ ] **Step 1: Replace screen_session loop preamble and add running extremes**

After the existing `mes_reset = mes_bars.reset_index(drop=True)` and `mnq_reset = mnq_bars.reset_index(drop=True)` lines (around line 694-695), insert:

```python
    import math as _math
    _mes_h_arr = mes_reset["High"].values
    _mes_l_arr = mes_reset["Low"].values
    _mnq_h_arr = mnq_reset["High"].values
    _mnq_l_arr = mnq_reset["Low"].values
    _mes_c_arr = mes_reset["Close"].values
    _mnq_c_arr = mnq_reset["Close"].values
    _ses_mes_h = _ses_mes_l = float("nan")
    _ses_mnq_h = _ses_mnq_l = float("nan")
    _ses_mes_ch = _ses_mes_cl = float("nan")
    _ses_mnq_ch = _ses_mnq_cl = float("nan")
```

Then replace the `for bar_idx in range(n_bars):` block. At the TOP of the loop body, before any `if` / `continue`, add the running-extremes update and cache construction:

```python
    for bar_idx in range(n_bars):
        # Update running extremes with previous bar so _cached reflects bars [0..bar_idx-1]
        if bar_idx > 0:
            _p = bar_idx - 1
            _v = float(_mes_h_arr[_p])
            _ses_mes_h = _v if _math.isnan(_ses_mes_h) else max(_ses_mes_h, _v)
            _v = float(_mes_l_arr[_p])
            _ses_mes_l = _v if _math.isnan(_ses_mes_l) else min(_ses_mes_l, _v)
            _v = float(_mnq_h_arr[_p])
            _ses_mnq_h = _v if _math.isnan(_ses_mnq_h) else max(_ses_mnq_h, _v)
            _v = float(_mnq_l_arr[_p])
            _ses_mnq_l = _v if _math.isnan(_ses_mnq_l) else min(_ses_mnq_l, _v)
            _v = float(_mes_c_arr[_p])
            _ses_mes_ch = _v if _math.isnan(_ses_mes_ch) else max(_ses_mes_ch, _v)
            _ses_mes_cl = _v if _math.isnan(_ses_mes_cl) else min(_ses_mes_cl, _v)
            _v = float(_mnq_c_arr[_p])
            _ses_mnq_ch = _v if _math.isnan(_ses_mnq_ch) else max(_ses_mnq_ch, _v)
            _ses_mnq_cl = _v if _math.isnan(_ses_mnq_cl) else min(_ses_mnq_cl, _v)

        _smt_cache = {
            "mes_h": _ses_mes_h, "mes_l": _ses_mes_l,
            "mnq_h": _ses_mnq_h, "mnq_l": _ses_mnq_l,
            "mes_ch": _ses_mes_ch, "mes_cl": _ses_mes_cl,
            "mnq_ch": _ses_mnq_ch, "mnq_cl": _ses_mnq_cl,
        }

        if mnq_bars.index[bar_idx] < min_signal_ts:
            continue

        _smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0, _cached=_smt_cache)
        # ... rest of loop body unchanged ...
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_signal_smt.py -v 2>&1 | tail -20
```

Expected: same pass/fail as baseline.

- [ ] **Step 3: Commit**

```bash
git add strategy_smt.py
git commit -m "perf: screen_session maintains running session extremes, passes _cached to detect_smt_divergence"
```

---

### Task 5: Fix backtest_smt.py — _BarRow, remove .copy(), running extremes, overnight sweep

**Files:**
- Modify: `backtest_smt.py:193-718`

Four changes bundled together since they all touch the main session loop:
1. Remove `.copy()` from session slices (lines 248-249)
2. Add `_BarRow` class that supports `bar["X"]` and `bar.name` access (replaces Series from `iterrows()`)
3. Replace `iterrows()` with pre-extracted numpy arrays + `_BarRow`
4. Maintain running session extremes, pass `_cached` to `detect_smt_divergence`
5. Replace `mnq_reset["High"].iloc[:bar_idx].max()` overnight sweep with running max/min

Note: `backtest_smt.py` has a "Frozen — do not modify" docstring aimed at autoresearch agents. The user has explicitly requested these performance fixes, so these changes are authorized.

- [ ] **Step 1: Add `_BarRow` class and `import math` at module level**

After the imports block (around line 8-9), add:

```python
import math as _math


class _BarRow:
    """Lightweight bar data holder — replaces pd.Series from iterrows() in the session loop.

    Supports bar["Open"], bar["High"], bar["Low"], bar["Close"] and bar.name (timestamp).
    Reduces per-bar overhead from full Series construction to a simple object allocation.
    """
    __slots__ = ("Open", "High", "Low", "Close", "name")

    def __init__(self, o: float, h: float, l: float, c: float, ts) -> None:
        self.Open = o
        self.High = h
        self.Low  = l
        self.Close = c
        self.name  = ts

    def __getitem__(self, key: str) -> float:
        return getattr(self, key)
```

- [ ] **Step 2: Remove `.copy()` from session slices**

Lines 248-249 currently:
```python
        mnq_session = mnq_df[session_mask].copy()
        mes_session = mes_df[session_mask].copy()
```

Change to:
```python
        mnq_session = mnq_df[session_mask]
        mes_session = mes_df[session_mask]
```

Rationale: boolean-indexed DataFrames are already new objects; `.copy()` only prevents SettingWithCopyWarning on in-place mutation, which never occurs here.

- [ ] **Step 3: Replace iterrows() with numpy arrays + _BarRow in the session loop**

The loop currently starts at line 304:
```python
        for bar_idx, (ts, bar) in enumerate(mnq_session.iterrows()):
```

Replace the block from the `reset_index` pre-computation (lines 300-302) through the start of the `for` loop with:

```python
        # Pre-compute reset-index views once per session to avoid repeated resets in the loop.
        mes_reset = mes_session.reset_index(drop=True)
        mnq_reset = mnq_session.reset_index(drop=True)

        # Pre-extract numpy arrays for fast per-bar access (avoids iterrows() Series overhead)
        _mnq_opens  = mnq_session["Open"].values
        _mnq_highs  = mnq_session["High"].values
        _mnq_lows   = mnq_session["Low"].values
        _mnq_closes = mnq_session["Close"].values
        _mes_highs  = mes_session["High"].values
        _mes_lows   = mes_session["Low"].values
        _mes_closes = mes_session["Close"].values
        _mnq_idx    = mnq_session.index

        # Running session extremes — updated at start of each bar for bars [0..bar_idx-1]
        # Initialized to nan so bar 0 comparisons behave identically to empty-slice .max()/.min()
        _ses_mes_h = _ses_mes_l = float("nan")
        _ses_mnq_h = _ses_mnq_l = float("nan")
        _ses_mes_ch = _ses_mes_cl = float("nan")
        _ses_mnq_ch = _ses_mnq_cl = float("nan")
        # Running high/low for overnight sweep gate (replaces slice-based recomputation)
        _run_ses_high = 0.0           # matches: mnq_reset["High"].iloc[:0].max() == 0 fallback
        _run_ses_low  = float("inf")  # matches: mnq_reset["Low"].iloc[:0].min() == inf fallback

        for bar_idx in range(len(mnq_session)):
            ts = _mnq_idx[bar_idx]

            # Bring running extremes up-to-date with the previous bar.
            # Done at the TOP of each iteration so continue-statements cannot skip the update.
            if bar_idx > 0:
                _p = bar_idx - 1
                _v = float(_mes_highs[_p])
                _ses_mes_h  = _v if _math.isnan(_ses_mes_h)  else max(_ses_mes_h,  _v)
                _v = float(_mes_lows[_p])
                _ses_mes_l  = _v if _math.isnan(_ses_mes_l)  else min(_ses_mes_l,  _v)
                _v = float(_mnq_highs[_p])
                _ses_mnq_h  = _v if _math.isnan(_ses_mnq_h)  else max(_ses_mnq_h,  _v)
                _run_ses_high = max(_run_ses_high, _v)
                _v = float(_mnq_lows[_p])
                _ses_mnq_l  = _v if _math.isnan(_ses_mnq_l)  else min(_ses_mnq_l,  _v)
                _run_ses_low  = min(_run_ses_low,  _v)
                _v = float(_mes_closes[_p])
                _ses_mes_ch = _v if _math.isnan(_ses_mes_ch) else max(_ses_mes_ch, _v)
                _ses_mes_cl = _v if _math.isnan(_ses_mes_cl) else min(_ses_mes_cl, _v)
                _v = float(_mnq_closes[_p])
                _ses_mnq_ch = _v if _math.isnan(_ses_mnq_ch) else max(_ses_mnq_ch, _v)
                _ses_mnq_cl = _v if _math.isnan(_ses_mnq_cl) else min(_ses_mnq_cl, _v)

            _smt_cache = {
                "mes_h": _ses_mes_h,  "mes_l": _ses_mes_l,
                "mnq_h": _ses_mnq_h,  "mnq_l": _ses_mnq_l,
                "mes_ch": _ses_mes_ch, "mes_cl": _ses_mes_cl,
                "mnq_ch": _ses_mnq_ch, "mnq_cl": _ses_mnq_cl,
            }

            bar = _BarRow(
                float(_mnq_opens[bar_idx]),
                float(_mnq_highs[bar_idx]),
                float(_mnq_lows[bar_idx]),
                float(_mnq_closes[bar_idx]),
                ts,
            )
```

- [ ] **Step 4: Update detect_smt_divergence call in IDLE state to pass _smt_cache**

Find the `detect_smt_divergence` call in the IDLE state (around line 582):
```python
                _smt = detect_smt_divergence(
                    mes_reset,
                    mnq_reset,
                    bar_idx,
                    0,
                )
```

Replace with:
```python
                _smt = detect_smt_divergence(
                    mes_reset,
                    mnq_reset,
                    bar_idx,
                    0,
                    _cached=_smt_cache,
                )
```

- [ ] **Step 5: Replace overnight sweep iloc-based max/min with running extremes**

Find lines 644-650 in the IDLE state:
```python
                    if direction == "short" and oh is not None:
                        pre_bar_high = mnq_reset["High"].iloc[:bar_idx].max() if bar_idx > 0 else 0
                        if pre_bar_high <= oh:
                            continue
                    if direction == "long" and ol is not None:
                        pre_bar_low = mnq_reset["Low"].iloc[:bar_idx].min() if bar_idx > 0 else float("inf")
                        if pre_bar_low >= ol:
                            continue
```

Replace with:
```python
                    if direction == "short" and oh is not None:
                        if _run_ses_high <= oh:
                            continue
                    if direction == "long" and ol is not None:
                        if _run_ses_low >= ol:
                            continue
```

- [ ] **Step 6: Replace mnq_reset.iloc[bar_idx] accesses in IDLE state with pre-extracted arrays**

Find the displacement bar extreme computation (around line 617-621):
```python
                _displacement_bar_extreme = None
                if _smt_type == "displacement":
                    if direction == "long":
                        _displacement_bar_extreme = float(mnq_reset.iloc[bar_idx]["Low"])
                    else:
                        _displacement_bar_extreme = float(mnq_reset.iloc[bar_idx]["High"])
```

Replace with:
```python
                _displacement_bar_extreme = None
                if _smt_type == "displacement":
                    if direction == "long":
                        _displacement_bar_extreme = float(_mnq_lows[bar_idx])
                    else:
                        _displacement_bar_extreme = float(_mnq_highs[bar_idx])
```

Find the divergence bar data capture (around line 661-668):
```python
                _div_bar = mnq_reset.iloc[bar_idx]
                pending_direction     = direction
                anchor_close          = ac
                pending_smt_sweep     = _smt_sweep
                pending_smt_miss      = _smt_miss
                divergence_bar_idx    = bar_idx
                _pending_div_bar_high = float(_div_bar["High"])
                _pending_div_bar_low  = float(_div_bar["Low"])
```

Replace with:
```python
                pending_direction     = direction
                anchor_close          = ac
                pending_smt_sweep     = _smt_sweep
                pending_smt_miss      = _smt_miss
                divergence_bar_idx    = bar_idx
                _pending_div_bar_high = float(_mnq_highs[bar_idx])
                _pending_div_bar_low  = float(_mnq_lows[bar_idx])
```

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_smt_position_arch.py tests/test_smt_signal_quality.py -v 2>&1 | tail -30
```

Expected: same pass/fail as baseline.

- [ ] **Step 8: Commit**

```bash
git add backtest_smt.py
git commit -m "perf: replace iterrows() with _BarRow+numpy arrays, running session extremes, remove .copy(), vectorize overnight sweep"
```

---

### Task 6: Fix `hypothesis_smt.py` — replace iterrows() in three functions

**Files:**
- Modify: `hypothesis_smt.py:142-179` (`_assign_case`)
- Modify: `hypothesis_smt.py:229-295` (`_compute_rule3`)
- Modify: `hypothesis_smt.py:298-390` (`_compute_rule4`)

These functions use `iterrows()` which returns full Series objects per row. Replace with `.values` array access (for pure numeric/scalar access) or `.itertuples()` (when timestamps are needed).

- [ ] **Step 1: Fix _assign_case — replace iterrows() with .values**

Find lines 154-161 in `_assign_case`:
```python
    near_far_extreme = False
    if rng > 0:
        for _, row in overnight.iterrows():
            if row["High"] > pd_midpoint and (pdh - row["High"]) <= 0.15 * rng:
                near_far_extreme = True
                break
            if row["Low"] < pd_midpoint and (row["Low"] - pdl) <= 0.15 * rng:
                near_far_extreme = True
                break
```

Replace with:
```python
    near_far_extreme = False
    if rng > 0:
        _oh = overnight["High"].values
        _ol = overnight["Low"].values
        _thresh = 0.15 * rng
        for _h, _l in zip(_oh, _ol):
            if _h > pd_midpoint and (pdh - _h) <= _thresh:
                near_far_extreme = True
                break
            if _l < pd_midpoint and (_l - pdl) <= _thresh:
                near_far_extreme = True
                break
```

- [ ] **Step 2: Fix _compute_rule3 — replace list(iterrows()) with .itertuples()**

Find lines 259-261 in `_compute_rule3`:
```python
        bars_list = list(day_bars.iterrows())
        for i in range(1, len(bars_list)):
            _, prev_bar = bars_list[i - 1]
            _, curr_bar = bars_list[i]
```

Replace with (using pre-extracted numpy arrays for the two columns accessed):
```python
        _d_highs = day_bars["High"].values
        _d_lows  = day_bars["Low"].values
        for i in range(1, len(_d_highs)):
            prev_low  = float(_d_lows[i - 1])
            curr_high = float(_d_highs[i])
            curr_low  = float(_d_lows[i])
```

Then update the references inside that loop body. The original code used:
- `float(curr_bar["Low"])` → `curr_low`
- `wick_level < float(prev_bar["Low"])` → `curr_low < prev_low` (for bullish trend)
- `float(curr_bar["High"])` → `curr_high`
- `wick_level > float(prev_bar["High"])` → `curr_high > float(_d_highs[i-1])` (for bearish trend)

The full replacement for the loop body:
```python
        _d_highs = day_bars["High"].values
        _d_lows  = day_bars["Low"].values
        for i in range(1, len(_d_highs)):
            if trend_direction == "bullish":
                wick_level = float(_d_lows[i])
                is_wick = wick_level < float(_d_lows[i - 1])
                direction_label = "bearish"
            else:
                wick_level = float(_d_highs[i])
                is_wick = wick_level > float(_d_highs[i - 1])
                direction_label = "bullish"

            if not is_wick:
                continue

            # Check if wick touches TWO or Asia H/L within EVIDENCE_TOUCH_PTS
            touched = None
            if two is not None and abs(wick_level - two) <= EVIDENCE_TOUCH_PTS:
                touched = "two"
            elif asia_high is not None and abs(wick_level - asia_high) <= EVIDENCE_TOUCH_PTS:
                touched = "asia_high"
            elif asia_low is not None and abs(wick_level - asia_low) <= EVIDENCE_TOUCH_PTS:
                touched = "asia_low"

            if touched:
                result.append({
                    "date":           str(d),
                    "direction":      direction_label,
                    "touched_level":  touched,
                    "confirms_trend": True,
                })
```

- [ ] **Step 3: Fix _compute_rule4 — replace iterrows() with .itertuples()**

Find lines 355-364:
```python
    if not overnight_bars.empty:
        for ts, row in overnight_bars.iterrows():
            close = float(row["Close"])
            for level_name, level_val in extremes.items():
                if level_val is None:
                    continue
                if abs(close - level_val) <= 10.0:
                    if last_extreme_ts is None or ts > last_extreme_ts:
                        last_extreme_visited = level_name
                        last_extreme_ts = ts
```

Replace with:
```python
    if not overnight_bars.empty:
        for row in overnight_bars.itertuples():
            close = float(row.Close)
            ts = row.Index
            for level_name, level_val in extremes.items():
                if level_val is None:
                    continue
                if abs(close - level_val) <= 10.0:
                    if last_extreme_ts is None or ts > last_extreme_ts:
                        last_extreme_visited = level_name
                        last_extreme_ts = ts
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_hypothesis_smt.py tests/test_smt_backtest.py -v 2>&1 | tail -20
```

Expected: same pass/fail as baseline.

- [ ] **Step 5: Commit**

```bash
git add hypothesis_smt.py
git commit -m "perf: replace iterrows() with .values/.itertuples() in _assign_case, _compute_rule3, _compute_rule4"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_hypothesis_smt.py tests/test_smt_position_arch.py tests/test_smt_signal_quality.py tests/test_signal_smt.py -v 2>&1 | tail -40
```

Expected: same pass/fail counts as Task 1 baseline.

- [ ] **Step 2: Verify backtest produces identical results**

If a reference run output exists (e.g. in `.agents/experiment-log.md`), run the backtest and compare key metrics:

```bash
uv run python backtest_smt.py 2>&1 | grep -E "mean_test_pnl|min_test_pnl|total_test_trades|holdout_total_pnl"
```

Numbers must match prior run within floating-point tolerance. Any divergence indicates a logic change.
