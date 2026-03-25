# Plan: train.py Performance Optimization

**Type**: Enhancement (pure performance — no behavioral changes)
**Complexity**: ⚠️ Medium
**Status**: Planned

---

## Overview

Optimize CPU-hot paths in `train.py` without changing any backtest outputs or strategy logic.
Five targeted changes, divided into two categories:

**Category A — Editable section (above `# DO NOT EDIT BELOW THIS LINE`, no hash update needed):**
1. Vectorize `find_pivot_lows()` — replace Python/iloc loop with numpy `sliding_window_view`
2. Vectorize `zone_touch_count()` — replace Python/iloc loop with numpy boolean ops
3. Vectorize `nearest_resistance_atr()` — same pattern as `find_pivot_lows`
4. Tail-slice in `manage_position()` — pass `df.iloc[-30:]` instead of full df to `calc_atr14()`

**Category B — Locked section (below the DO NOT EDIT marker, requires GOLDEN_HASH update):**
5. Optimize `detect_regime()` — replace O(N) boolean filter + full rolling with O(log N) loc-slice
   + O(50) tail-slice mean

**Category C — Documentation:**
6. Update `program.md` experiment loop to instruct agents to apply the
   `python-performance-optimization` skill after each code edit

---

## Execution agent rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Background: why these changes are safe

### Category A correctness

- **`find_pivot_lows()`**: original condition `l <= df['low'].iloc[i+k] for k in range(-bars,bars+1) if k!=0`
  is equivalent to `low[i] == min(low[i-bars : i+bars+1])`. numpy's `sliding_window_view`
  extracts these windows in one call; `.min(axis=1)` gives per-window minimums; `center <= window_min`
  selects the same pivots. Mathematically identical.

- **`zone_touch_count()`**: replaces `iloc`-based generator sum with numpy boolean array `.sum()`.
  Identical arithmetic; only Python loop overhead eliminated.

- **`nearest_resistance_atr()`**: same pattern as `find_pivot_lows()` — pivot highs (local maxima
  above entry_price). `sliding_window_view` + `.max(axis=1)` selects the same candidates.

- **`manage_position()`**: `calc_atr14(df.iloc[-30:]).iloc[-1]` is identical to
  `calc_atr14(df).iloc[-1]`. ATR's rolling(14) mean at the last bar depends only on the last 14 bars
  of true range; each true range bar depends only on that bar's high/low and the previous bar's close.
  30 rows → 29 usable TR values → 16 valid rolling(14) values. The last value is the same as if the
  full history were passed. `screen_day()` already uses this identical pattern (`calc_atr14(hist.iloc[-30:])`).

### Category B correctness

- **`detect_regime()` filter**: `df[df.index <= today]` creates an O(N) boolean mask.
  `df.loc[:today]` on a sorted `DatetimeIndex` uses pandas binary search — O(log N). Result is
  identical: all rows with index ≤ today.

- **`detect_regime()` SMA**: `hist['close'].rolling(50).mean().iloc[-1]` equals
  `hist['close'].iloc[-50:].mean()` by definition of a simple moving average. The file already
  documents this equivalence in `screen_day()`:
  ```
  # Fast SMA checks using direct tail-slicing (equivalent to rolling().mean().iloc[-1]
  # but only computes the last window instead of the full series — much faster).
  ```

---

## Implementation tasks

All changes are in a single wave (they are independent and non-overlapping).

### Task 1 — Vectorize `find_pivot_lows()` (train.py L138–145)

Replace:
```python
def find_pivot_lows(df, bars=4):
    # A pivot low is the lowest bar in a symmetric window of `bars` on each side
    pivots = []
    for i in range(bars, len(df) - bars):
        l = float(df['low'].iloc[i])
        if all(l <= float(df['low'].iloc[i+k]) for k in range(-bars, bars+1) if k != 0):
            pivots.append((i, l))
    return pivots
```

With:
```python
def find_pivot_lows(df, bars=4):
    # A pivot low is the lowest bar in a symmetric window of `bars` on each side.
    # Vectorized: sliding_window_view extracts all (2*bars+1)-wide windows in one call;
    # a center bar is a pivot low iff its value equals the window minimum.
    lows = df['low'].values
    n = len(lows)
    if n < 2 * bars + 1:
        return []
    windows = np.lib.stride_tricks.sliding_window_view(lows, 2 * bars + 1)
    window_mins = windows.min(axis=1)
    center_lows = lows[bars:n - bars]
    is_pivot = center_lows <= window_mins
    indices = np.where(is_pivot)[0] + bars
    return [(int(i), float(lows[i])) for i in indices]
```

No import needed — `np` is already imported at module level.

---

### Task 2 — Vectorize `zone_touch_count()` (train.py L148–154)

Replace:
```python
def zone_touch_count(df, level, lookback=90, band_pct=0.015):
    window = df.iloc[-lookback:]
    lo, hi = level * (1 - band_pct), level * (1 + band_pct)
    return int(sum(
        1 for i in range(len(window))
        if float(window['low'].iloc[i]) <= hi and float(window['high'].iloc[i]) >= lo
    ))
```

With:
```python
def zone_touch_count(df, level, lookback=90, band_pct=0.015):
    window = df.iloc[-lookback:]
    lo, hi = level * (1 - band_pct), level * (1 + band_pct)
    return int(((window['low'].values <= hi) & (window['high'].values >= lo)).sum())
```

---

### Task 3 — Vectorize `nearest_resistance_atr()` (train.py L206–219)

Replace:
```python
def nearest_resistance_atr(df, entry_price, atr, lookback=90):
    # Returns distance to nearest overhead pivot high in ATR units, or None if none exists above entry
    window = df.iloc[-lookback:].copy().reset_index(drop=True)
    bars, pivot_highs = 4, []
    for i in range(bars, len(window) - bars):
        h = float(window['high'].iloc[i])
        if h > entry_price and all(
            h >= float(window['high'].iloc[i+k])
            for k in range(-bars, bars+1) if k != 0
        ):
            pivot_highs.append(h)
    if not pivot_highs:
        return None
    return (min(pivot_highs) - entry_price) / atr
```

With:
```python
def nearest_resistance_atr(df, entry_price, atr, lookback=90):
    # Returns distance to nearest overhead pivot high in ATR units, or None if none exists above entry.
    # Vectorized: a pivot high is a bar that equals the window maximum and exceeds entry_price.
    window = df.iloc[-lookback:]
    bars = 4
    highs = window['high'].values
    n = len(highs)
    if n < 2 * bars + 1:
        return None
    windows = np.lib.stride_tricks.sliding_window_view(highs, 2 * bars + 1)
    window_maxes = windows.max(axis=1)
    center_highs = highs[bars:n - bars]
    is_pivot_high = (center_highs >= window_maxes) & (center_highs > entry_price)
    pivot_highs = center_highs[is_pivot_high]
    if len(pivot_highs) == 0:
        return None
    return (float(pivot_highs.min()) - entry_price) / atr
```

Note: `.copy().reset_index(drop=True)` removed — the vectorized path operates on `.values`
(numpy array) so the pandas index is irrelevant. This also avoids an unnecessary copy.

---

### Task 4 — Tail-slice in `manage_position()` (train.py L350)

Replace:
```python
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
```

With:
```python
    atr_series   = calc_atr14(df.iloc[-30:])
    atr          = float(atr_series.iloc[-1])
```

Rationale: `calc_atr14` uses `rolling(14).mean()` on true range. The last value depends only on
the last 14 TR bars; TR at each bar depends only on that bar's OHLC and the previous bar's close.
30 rows → 29 valid TR values → 16 valid rolling(14) values. Result is identical to computing on
the full df. `screen_day()` already uses this identical pattern.

---

### Task 5 — Optimize `detect_regime()` (train.py, below DO NOT EDIT boundary)

This function lives below the `# ── DO NOT EDIT BELOW THIS LINE` marker (line 422).
Changes here require updating `GOLDEN_HASH` in `tests/test_optimization.py` in the same step.

Replace (lines 433–437 of current train.py):
```python
    for ticker, df in ticker_dfs.items():
        hist = df[df.index <= today]
        if len(hist) < 51:
            continue
        sma50 = float(hist['close'].rolling(50).mean().iloc[-1])
        price = float(hist['price_1030am'].iloc[-1])
```

With:
```python
    for ticker, df in ticker_dfs.items():
        hist = df.loc[:today]
        if len(hist) < 51:
            continue
        sma50 = float(hist['close'].iloc[-50:].mean())
        price = float(hist['price_1030am'].iloc[-1])
```

Two changes:
- `df[df.index <= today]` → `df.loc[:today]`: O(N) boolean mask → O(log N) binary search on sorted DatetimeIndex
- `rolling(50).mean().iloc[-1]` → `iloc[-50:].mean()`: recomputes entire rolling series → computes last 50 values only

After making this change, recompute GOLDEN_HASH:
```python
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# \u2500\u2500 DO NOT EDIT BELOW THIS LINE'
print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())
"
```

Then update `GOLDEN_HASH = "..."` on line 118 of `tests/test_optimization.py` with the new hash.

---

### Task 6 — Add screener rejection diagnostics to `screener.py`

Add a separate diagnostic loop in `screener.py` that tallies how many tickers were
eliminated by each rule, without changing `screen_day()`'s contract.

After the `_check_staleness(parquet_paths)` call and before the main `armed`/`gap_skipped`
loop, add a `rejection_counts` dict and a second pass that evaluates each rule independently:

```python
# Diagnostic counters — tallied in the main loop below via _rejection_reason()
_RULES = [
    "too_few_rows", "no_price", "earnings_soon",
    "sma_misaligned", "sma20_declining",
    "vol_trend_low", "prev_vol_dead",
    "no_breakout", "no_breakout_cont",
    "rsi_out_of_range", "ceiling_stall",
    "no_pivot_stop", "resistance_too_close",
]
rejection_counts = {r: 0 for r in _RULES}
```

In the per-ticker loop, replace the `except Exception: continue` with a helper that
identifies the *first* rule that fired and increments its counter. After the main loop,
print the rejection breakdown only when `--diagnose` is passed as a CLI arg (or always
when no signals fire), so normal runs stay clean.

Print format:
```
=== REJECTION BREAKDOWN (1034 tickers) ===
  sma_misaligned    : 712  (68.9%)
  no_breakout       : 189  (18.3%)
  sma20_declining   :  67   (6.5%)
  ...
```

**Why Option 2 (separate diagnostic loop) over modifying `screen_day()`:**
`screen_day()` is part of the locked strategy contract — its return type (`dict | None`)
is tested and relied on by the backtester. A rejection-label return would require changing
callers. A parallel evaluation in `screener.py` keeps the contract intact.

---

### Task 8 — Update `program.md` experiment loop (step 2)

In `program.md`, locate the experiment loop step 2:

```
2. Modify `train.py` with an experimental idea. Edit the file directly. **Only edit code above the `# DO NOT EDIT BELOW THIS LINE` comment** — everything below it is the evaluation harness and must not be touched.
```

Replace with:

```
2. Modify `train.py` with an experimental idea. Edit the file directly. **Only edit code above the `# DO NOT EDIT BELOW THIS LINE` comment** — everything below it is the evaluation harness and must not be touched.

   After making any code edit, apply the `python-performance-optimization` skill to the modified function(s) to verify the new code is fully optimized for performance. Use `/python-performance-optimization` and pass the function name and file. Reject any implementation that introduces Python loops iterating over pandas rows (use numpy vectorization instead).
```

---

## Verification

After all tasks are complete, run the full test suite:

```bash
uv run pytest
```

Expected: all previously-passing tests continue to pass. The only test that exercises the
locked section is `test_harness_below_do_not_edit_is_unchanged` — it will pass once
GOLDEN_HASH is updated to match the new detect_regime code.

Key tests that exercise the changed code paths:
- `tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged` — verifies new GOLDEN_HASH
- `tests/test_optimization.py::test_optimization_feasible_on_synthetic_data` — exercises full backtest loop including detect_regime, screen_day (which calls find_pivot_lows, zone_touch_count, nearest_resistance_atr), and manage_position
- `tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change` — exercises editable section

If any test fails after the changes, diagnose the root cause before attempting a fix.

---

## Post-implementation PROGRESS.md update

Add a new entry to PROGRESS.md:

```markdown
## Feature: train.py Performance Optimization

**Status**: ✅ Complete
**Plan File**: .agents/plans/train-py-performance-optimization.md

### Core Changes
- `train.py` `find_pivot_lows()` — vectorized with numpy sliding_window_view (eliminates Python/iloc loop)
- `train.py` `zone_touch_count()` — vectorized with numpy boolean array ops
- `train.py` `nearest_resistance_atr()` — vectorized with numpy sliding_window_view
- `train.py` `manage_position()` — tail-sliced calc_atr14 input (df.iloc[-30:] instead of full df)
- `train.py` `detect_regime()` — df.loc[:today] (binary search) + tail-slice mean (iloc[-50:])
- `tests/test_optimization.py` GOLDEN_HASH — updated to match new detect_regime code
- `program.md` — experiment loop step 2 updated with python-performance-optimization skill instruction
```

---

## Expected performance impact

| Change | Frequency | Estimated speedup |
|---|---|---|
| `find_pivot_lows` vectorized | Per ticker × per day × 16 backtests | ~15–25× on 90-row window |
| `zone_touch_count` vectorized | Per pivot candidate × per day | ~10–20× on 90-row window |
| `nearest_resistance_atr` vectorized | Per ticker × per day | ~15–25× on 90-row window |
| `manage_position` tail-slice | Per open position × per day × 16 backtests | ~5–15× (avoids O(N) on 500+ rows) |
| `detect_regime` O(log N) filter + tail-slice | 85 tickers × per day × 16 backtests | ~10–30× per call |

Overall estimated speedup: **2–4× reduction in total `uv run train.py` wall time**.
