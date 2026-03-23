# Feature: V4-A Strategy Quality and Loop Control

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

V4-A fixes the sources of systematic loss that no amount of screener iteration can address
in the current optimization harness, and improves the agent loop instructions so the next
30 iterations are spent productively.

Post-run analysis of `multisector-mar23` (30 iterations, 85 tickers, 9 sectors, 19 months)
revealed that 25% of trades used "fallback" stops (no structural support below price) with a
79% vs 25% win-rate differential vs pivot-stop trades; 6 of 10 losses were earnings-proximity
entries; and long-holding, low-PnL positions (88–98 days earning <$13) wasted capital slots.
None of these can be improved by threshold tuning — they require structural screener/position
management fixes.

Seven changes are included:
- **R9** – Reject fallback-stop entries (one line in `screen_day()`)
- **R8** – Earnings-proximity filter: skip entries within 14 days of next earnings (requires
  a new `next_earnings_date` column in parquet files via `prepare.py` changes)
- **R10** – Time-based capital-efficiency exit: if position held >30 business days and
  unrealised P&L < 30% of RISK_PER_TRADE, set stop at current price to force exit
- **R1** – Widen FOLD_TEST_DAYS default to 40 in `program.md` (20 was too short for the
  30–98 day hold duration observed)
- **R3** – Auto-calibrate consistency floor in `program.md`: replace `−RISK_PER_TRADE × 2`
  with `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10`
- **R5** – Deadlock detection pivot rule in `program.md`
- **R6** – Explicit guidance in `program.md` to test position management early (iterations 6–10)

## User Story

As a developer running the autoresearch optimization loop,
I want the backtester to reject structurally weak entries and exit stalled positions,
So that each optimization iteration reflects genuine alpha rather than accidental wins or
capital misallocation.

## Problem Statement

The `multisector-mar23` run wasted ~17 iterations on a deadlocked `min_test_pnl` and accepted
loses from earnings events and fallback-stop entries that no screener tweak could fix.
FOLD_TEST_DAYS=20 was too short, the consistency floor formula was too tight for large universes,
and position management was never tested until the final iterations.

## Solution Statement

Three structural code changes (R8, R9, R10) are applied to `screen_day()` and
`manage_position()` in the mutable section of `train.py`, and one data change to `prepare.py`.
Four `program.md` instruction changes (R1, R3, R5, R6) tune the loop control rules. No
immutable zone is touched; GOLDEN_HASH update is NOT required.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (mutable zone), `prepare.py`, `program.md`,
  `tests/test_screener.py`, `tests/test_backtester.py`
**Dependencies**: yfinance (`earnings_dates` property — already installed)
**Breaking Changes**: Yes — R9 changes `screen_day()` behaviour: existing
  `make_signal_df` fixture produces a fallback-stop signal that will now return None.
  Affected tests must be updated to use a pivot-stop fixture.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 224–312) — `screen_day()` function to modify for R8 and R9
- `train.py` (lines 314–335) — `manage_position()` function to modify for R10
- `prepare.py` (lines 65–77) — `download_ticker()` — earnings_dates fetched alongside this
- `prepare.py` (lines 80–116) — `resample_to_daily()` — next_earnings_date column added here
- `prepare.py` (lines 191–206) — `process_ticker()` — orchestrates download + resample + save
- `tests/test_screener.py` (lines 47–87) — `make_signal_df` fixture (uses fallback stop —
  WILL BREAK after R9; must be updated to use pivot stop)
- `tests/test_backtester.py` (lines 49–71) — `make_signal_df_for_backtest` (same issue)
- `program.md` (lines 44–68) — FOLD_TEST_DAYS setup section (R1 target)
- `program.md` (lines 296–302) — keep/discard condition (R3 target)
- `program.md` (lines 285–316) — experiment loop body (R5, R6 targets)

### New Files to Create

- `tests/test_v4_a.py` — New unit tests for R8, R9, R10 changes

### Patterns to Follow

**Test fixture naming**: `make_<purpose>_df` (e.g. `make_pivot_signal_df`)
**Test naming**: `test_<behaviour>_<condition>` (e.g. `test_screen_day_rejects_fallback_stop`)
**program.md rule references**: inline in the relevant section, no separate heading needed

---

## KEY IMPLEMENTATION DETAILS

### R9 — Reject Fallback-Stop Entries (screen_day, 1 line)

Current code (train.py ~line 287–290):
```python
stop = find_stop_price(hist, price_10am, atr)
if stop is None:
    stop = round(price_10am - 2.0 * atr, 2)
    stop_type = 'fallback'
```

After R9 (change the fallback branch):
```python
stop = find_stop_price(hist, price_10am, atr)
if stop is None:
    return None  # R9: reject entries with no structural support
stop_type = 'pivot'
```

The `stop_type` field in the return dict always equals `'pivot'` after this change, but
leave it in the return dict for backward compatibility with `trades.tsv` schema.

**Impact on existing tests**: `make_signal_df` and `make_signal_df_for_backtest` both
produce DataFrames where `find_stop_price()` returns None (no pivot structure). Both
fixtures must be updated to use a pivot-stop design, OR new pivot-signal fixtures must be
created and the affected tests updated to use them. **Preferred approach**: create a new
`make_pivot_signal_df()` fixture and update the two tests that assert `result is not None`.
Keep `make_signal_df` as-is — it now becomes the test fixture for "fallback stop → None".

### R8 — Earnings-Proximity Filter

**prepare.py change** — In `resample_to_daily()` (or as a separate helper called from
`process_ticker()`), after building the daily DataFrame:

```python
def _add_earnings_dates(df_daily: pd.DataFrame, ticker_obj) -> pd.DataFrame:
    """Add next_earnings_date column: for each trading day, the next upcoming earnings."""
    try:
        edf = ticker_obj.earnings_dates
        if edf is None or len(edf) == 0:
            df_daily['next_earnings_date'] = pd.NaT
            return df_daily
        # earnings_dates index is tz-aware; convert to plain dates
        edates = sorted(set(d.date() for d in edf.index))
    except Exception:
        df_daily['next_earnings_date'] = pd.NaT
        return df_daily

    result = []
    for day in df_daily.index:
        future = [d for d in edates if d > day]
        result.append(future[0] if future else None)
    df_daily = df_daily.copy()
    df_daily['next_earnings_date'] = pd.array(result, dtype='object')
    return df_daily
```

Call this from `process_ticker()` before `df_daily.to_parquet(path)`. Pass the same
`ticker_obj` that was used for `download_ticker`.

**train.py screen_day() change** — Add guard before all other checks (after the NaN/zero
guard on line ~256):

```python
# R8: skip entries within 14 days of next earnings announcement
if 'next_earnings_date' in df.columns:
    ned = df['next_earnings_date'].iloc[-1]
    if pd.notna(ned):
        days_to_earnings = (ned - today).days
        if 0 <= days_to_earnings <= 14:
            return None
```

Note: `today` is a `date` object, `ned` is also a `date` object from parquet. The subtraction
`ned - today` returns a `timedelta`, `.days` is an integer.

**Important**: Existing parquet files do NOT have the `next_earnings_date` column. The
`'next_earnings_date' in df.columns` guard handles backward compatibility gracefully —
the filter simply doesn't fire for old files. New optimization sessions must re-run
`prepare.py` (delete old parquets first, since `process_ticker` skips existing files).
Add a note to the plan output.

### R10 — Time-Based Capital-Efficiency Exit (manage_position)

Add at the beginning of `manage_position()`, after computing `price_10am`:

```python
# R10: time-based capital-efficiency exit
# If held >30 business days and unrealised gain < 0.3×RISK_PER_TRADE, force exit
today_date = df.index[-1]
entry_date = position['entry_date']
bdays_held = int(np.busday_count(entry_date, today_date))
unrealised_pnl = (price_10am - entry_price) * position['shares']
if bdays_held > 30 and unrealised_pnl < 0.3 * RISK_PER_TRADE:
    return max(current_stop, price_10am)  # set stop at current price; never lower
```

Note: `np.busday_count` accepts Python `date` objects directly and returns a numpy int.
Using `max(current_stop, price_10am)` ensures we never lower an existing stop
(e.g. if trailing stop is already above current price).

`RISK_PER_TRADE` is a module-level constant in `train.py`, accessible from `manage_position()`.

### R1, R3, R5, R6 — program.md Changes

**R1** — In the "Walk-forward fold constants" section (step 4b), change the default for
`FOLD_TEST_DAYS` from `20` to `40`. Update the example: "40 ≈ 2 calendar months". Add note:
"Reduce `WALK_FORWARD_WINDOWS` to 7 if the total date range cannot fit 9 folds of 40 days."

**R3** — In the experiment loop step 8, change the consistency floor from:
  `−RISK_PER_TRADE × 2` (e.g. ≥ −$100 when RISK_PER_TRADE = 50.0)
to:
  `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10` (e.g. ≥ −$2500 when
   RISK_PER_TRADE=50, MAX_SIMULTANEOUS_POSITIONS=5)
Update the `discard-inconsistent` status column definition in the Logging section to
match the new formula.

**R5** — In "The experiment loop" section, after the zero-trade plateau rule, add:
> **Deadlock detection pivot**: If `min_test_pnl` has not changed for 4 consecutive
> kept iterations (same value), pivot to optimizing `mean_test_pnl` (average across all
> walk-forward folds) for the next 3 iterations before returning to `min_test_pnl`.
> `mean_test_pnl = (fold1_test_pnl + ... + foldN_test_pnl) / N` — compute from run.log.

**R6** — In "The experiment loop" → "What you CAN do" or as a new "Loop strategy" note,
add:
> **Test position management early**: In iterations 6–10, explicitly test trailing-stop
> distance, breakeven trigger, and stop distance changes before exhausting screener ideas.
> Position management changes are high-leverage and often overlooked until the end.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────┐
│ WAVE 1: Parallel foundations (no interdependencies) │
├────────────────────────────────────────────────────┤
│ Task 1.1: prepare.py (R8 data)  │ Task 1.2: program.md (R1,R3,R5,R6) │
└────────────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────────────┐
│ WAVE 2: train.py code changes (same file — sequential) │
├────────────────────────────────────────────────────┤
│ Task 2.1: R9 reject fallback stop in screen_day()  │
│ Task 2.2: R8 earnings guard in screen_day()        │
│ Task 2.3: R10 time-based exit in manage_position() │
└────────────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────────────┐
│ WAVE 3: Tests                                       │
├────────────────────────────────────────────────────┤
│ Task 3.1: Update existing fixtures + create test_v4_a.py │
│ Task 3.2: Run full test suite, verify GOLDEN_HASH   │
└────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Parallel**: Tasks 1.1 and 1.2 are fully independent
**Wave 2 — Sequential**: All three tasks modify `train.py`; must run in order
**Wave 3 — Sequential**: Tests depend on Wave 2 completion

### Interface Contracts

**Wave 1 → Wave 2**: `prepare.py` adds `next_earnings_date` column (date object, nullable).
`screen_day()` reads `df['next_earnings_date'].iloc[-1]` with a column-existence guard.

### Synchronization Checkpoints

**After Wave 2**: `python -c "import train"` — must not raise
**After Wave 3**: `uv run pytest tests/ -x` — must pass with existing count + new V4-A tests

---

## IMPLEMENTATION PLAN

### Phase 1: prepare.py — Earnings Date Column (R8 Data)

#### Task 1.1: UPDATE prepare.py — add next_earnings_date column

**Purpose**: Add per-row `next_earnings_date` to each ticker's parquet for use by R8 filter.
**Dependencies**: None
**Steps**:
1. Read `prepare.py` to confirm `download_ticker` and `process_ticker` signatures.
2. Add helper function `_add_earnings_dates(df_daily, ticker_obj)` — see implementation
   details above. Place it after `validate_ticker_data()` (line ~131).
3. In `process_ticker()`, after `df_daily = resample_to_daily(df_hourly)`, add:
   `df_daily = _add_earnings_dates(df_daily, yf.Ticker(ticker))`
   — or pass the already-fetched `ticker_obj` if you refactor `process_ticker` to cache it.
   Simplest approach: create a new `yf.Ticker(ticker)` call inside `_add_earnings_dates`.
   Alternatively, inline the earnings date fetch inside `process_ticker` after the hourly
   download, passing `ticker_obj` to `_add_earnings_dates`.
4. Verify column is written: `python -c "import pandas as pd; df=pd.read_parquet('~/.cache/autoresearch/stock_data/AAPL.parquet'); print('next_earnings_date' in df.columns)"`
   (this verifies structure only if AAPL.parquet was written after this change)

**Validation**: `python -c "import prepare"` — no errors

---

### Phase 1: program.md — Loop Control Improvements (R1, R3, R5, R6)

#### Task 1.2: UPDATE program.md — FOLD_TEST_DAYS and loop rules

**Purpose**: Improve optimization loop behaviour for future sessions.
**Dependencies**: None
**Steps**:
1. **R1**: Find the line "Default `20` (≈1 calendar month..." in step 4b.
   Change `20` to `40` and update the description: "Default `40` (≈2 calendar months,
   ~80–200 trades on 85 tickers; better coverage for the 30–98 day hold durations
   observed in the multisector-mar23 run). Set to `10` for legacy V3-B/D behavior only."
   Add the note: "Reduce `WALK_FORWARD_WINDOWS` to 7 if the total date range cannot
   accommodate 9 folds of 40 days each."

2. **R3**: Find step 8 keep/discard condition 2:
   `train_pnl_consistency` ≥ `−RISK_PER_TRADE × 2`
   Replace with:
   `train_pnl_consistency` ≥ `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10`
   Update the parenthetical example: "(e.g. ≥ −$2500 when RISK_PER_TRADE=50,
   MAX_SIMULTANEOUS_POSITIONS=5 — scales with universe size and position cap)"
   Also update the `discard-inconsistent` status description in the Logging section to
   use the new formula.

3. **R5**: Find the "Zero-trade plateau rule" paragraph. After the plateau rule, add a new
   paragraph:
   > **Deadlock detection pivot**: If `min_test_pnl` has not changed for 4 consecutive
   > kept iterations (same value appearing 4+ times in the `keep` rows of `results.tsv`),
   > switch objective: optimize `mean_test_pnl = mean(fold1_test_pnl…foldN_test_pnl)` for
   > the next 3 iterations, then revert to `min_test_pnl`. Use this pivot to unlock
   > improvement in folds that are not the minimum. Grep: `awk -F'\t' '$10=="keep"' results.tsv | tail -5`

4. **R6**: In "What you CAN do" or immediately before "AUTONOMOUS UNTIL DONE", add:
   > **Position management priority**: Explicitly test trailing-stop distance, breakeven
   > trigger level, and stop-distance changes in iterations 6–10, before exhausting
   > screener ideas. These changes are high-leverage and were systematically found last in
   > the multisector-mar23 run (iterations 27–28 of 30).

**Validation**: `python -c "import re; t=open('program.md').read(); assert '40' in t and 'MAX_SIMULTANEOUS_POSITIONS' in t and 'Deadlock detection' in t and 'iterations 6' in t; print('OK')"`

---

### Phase 2: train.py Mutable Zone Changes

Read `train.py` fully before making any changes to understand the current structure.

#### Task 2.1: UPDATE train.py screen_day() — R9 reject fallback stop

**Purpose**: Remove the fallback stop path so only pivot-backed entries are taken.
**Dependencies**: Wave 1 complete (for context only; this change is independent of R8)
**Steps**:
1. Find the fallback stop block (~line 287–291):
   ```python
   if stop is None:
       stop = round(price_10am - 2.0 * atr, 2)
       stop_type = 'fallback'
   else:
       stop_type = 'pivot'
   ```
2. Replace with:
   ```python
   if stop is None:
       return None  # R9: reject entries with no structural pivot support
   stop_type = 'pivot'
   ```
3. The `stop_type` in the return dict always reads `'pivot'` now. Keep the field for
   `trades.tsv` backward compatibility.

**Validation**: `python -c "import train; print('R9 ok')"` — no errors

#### Task 2.2: UPDATE train.py screen_day() — R8 earnings guard

**Purpose**: Reject entries within 14 calendar days of next earnings announcement.
**Dependencies**: Task 2.1 (same file)
**Steps**:
1. In `screen_day()`, find the NaN/zero guard block (~line 256):
   ```python
   if pd.isna(price_10am) or pd.isna(sma20) or ...
   ```
2. **After** this guard, add the earnings check:
   ```python
   # R8: skip entries within 14 calendar days of next earnings announcement
   if 'next_earnings_date' in df.columns:
       ned = df['next_earnings_date'].iloc[-1]
       if pd.notna(ned):
           days_to_earnings = (ned - today).days
           if 0 <= days_to_earnings <= 14:
               return None
   ```
   Note: `today` is the second parameter of `screen_day(df, today)`, already in scope.
   `ned` is a Python `date` object (stored as object dtype in parquet). The guard
   `'next_earnings_date' in df.columns` ensures backward compatibility with old parquets.

**Validation**: `python -c "import train; print('R8 ok')"` — no errors

#### Task 2.3: UPDATE train.py manage_position() — R10 time-based exit

**Purpose**: Force exit on positions held >30 business days with negligible unrealised gain.
**Dependencies**: Task 2.2 (same file)
**Steps**:
1. In `manage_position()`, find the line where `price_10am` is computed (~line 326):
   ```python
   price_10am = float(df['price_10am'].iloc[-1])
   ```
2. **After** this line, before the breakeven trigger, add:
   ```python
   # R10: time-based capital-efficiency exit
   # If held >30 business days and unrealised P&L < 30% of RISK_PER_TRADE, force exit
   _today_date = df.index[-1]
   _bdays_held = int(np.busday_count(position['entry_date'], _today_date))
   _unrealised_pnl = (price_10am - entry_price) * position['shares']
   if _bdays_held > 30 and _unrealised_pnl < 0.3 * RISK_PER_TRADE:
       return max(current_stop, price_10am)  # force exit; never lower existing stop
   ```
   Note: `np` is already imported at the top of `train.py`. `RISK_PER_TRADE` is a
   module-level constant, visible from inside `manage_position()`.

**Validation**: `python -c "import train; print('R10 ok')"` — no errors

---

### Phase 3: Tests

#### Task 3.1: Update existing fixtures + create tests/test_v4_a.py

**Purpose**: Fix existing tests broken by R9 and add coverage for all V4-A changes.
**Dependencies**: Wave 2 complete
**Steps**:

**Step A — Fix tests/test_screener.py (R9 fixture breakage)**:
The existing `make_signal_df` fixture produces a DataFrames where `find_stop_price()` returns
None (no pivot structure), so after R9 it will return None rather than a signal. Two tests
rely on it returning a non-None result:
- `test_return_dict_has_stop_key`
- `test_stop_always_below_entry`

Fix approach:
1. Keep `make_signal_df` unchanged — it is now a valid fixture for "signal_df that would have
   used fallback stop → returns None after R9". Do NOT rename it; it is used by other tests
   that assert None.
2. Add a new fixture `make_pivot_signal_df()` in `test_screener.py` that produces a DataFrame
   with a clear pivot-low structure so `find_stop_price()` succeeds. Base it on `make_signal_df`
   but add a pivot dip ~35 bars from the end with a prior touch ~10 bars before that.
   The pivot must be at least 1.5×ATR below `price_10am=115.0` to satisfy the buffer.
   Pattern to mirror: `make_pivot_df(n)` in `test_screener.py` (lines 34–44) already creates
   this structure — combine with `make_signal_df`'s momentum/volume properties.
3. Update `test_return_dict_has_stop_key` and `test_stop_always_below_entry` to use
   `make_pivot_signal_df` instead of `make_signal_df`.

**Step B — Fix tests/test_backtester.py (R9 fixture breakage)**:
`make_signal_df_for_backtest` has the same fallback-stop design as `make_signal_df`.
Check which tests in `test_backtester.py` call `screen_day()` directly or indirectly via
`run_backtest()` with this fixture, and either:
- Add a pivot structure to `make_signal_df_for_backtest`, OR
- Mock `screen_day` in tests that use `make_signal_df_for_backtest` (already mocked in
  some backtester tests)
Safest approach: Add pivot dip to `make_signal_df_for_backtest` following the same pattern
as Step A. This keeps backtester tests integration-style (real screen_day call).

**Step C — Create tests/test_v4_a.py with ≥15 new tests**:

```python
"""tests/test_v4_a.py — Unit tests for V4-A: R8 earnings filter, R9 fallback reject, R10 time exit."""
```

Tests to include:

**R9 tests (screen_day fallback reject)**:
1. `test_screen_day_rejects_fallback_stop_entry`
   — Use `make_signal_df` (no pivot, fallback stop). Assert result is None.
   (Replaces what was previously a non-None assertion — now validates R9.)

2. `test_screen_day_accepts_pivot_stop_entry`
   — Use `make_pivot_signal_df`. Assert result is not None and `result['stop_type'] == 'pivot'`.

3. `test_screen_day_stop_type_always_pivot_when_signal`
   — Use `make_pivot_signal_df`. Assert `result['stop_type'] == 'pivot'`.

**R8 tests (earnings filter)**:
4. `test_screen_day_rejects_earnings_within_14_days`
   — Use `make_pivot_signal_df`, add `next_earnings_date` column = today + 7 days.
   Assert result is None.

5. `test_screen_day_rejects_earnings_exactly_14_days`
   — `next_earnings_date` = today + 14 days. Assert result is None.

6. `test_screen_day_passes_earnings_15_days_away`
   — `next_earnings_date` = today + 15 days. Assert result is not None.

7. `test_screen_day_passes_earnings_nat`
   — `next_earnings_date` column present but value is pd.NaT. Assert result is not None.

8. `test_screen_day_passes_no_earnings_column`
   — `next_earnings_date` column absent entirely. Assert result is not None.

9. `test_screen_day_rejects_earnings_day_itself`
   — `next_earnings_date` = today. Assert result is None (days_to_earnings == 0).

**R10 tests (manage_position time-based exit)**:
10. `test_manage_position_forces_exit_stalled_position`
    — Position held >30 business days, unrealised_pnl < 0.3 × RISK_PER_TRADE.
    Assert returned stop == price_10am (force exit at current price).
    Implementation: make_position_df(n=50, price=100.0), entry_date=df.index[0],
    entry_price=100.0, shares=RISK_PER_TRADE/(100.0-95.0)=10.0,
    unrealised_pnl = (100.0-100.0)*10 = $0 < $15. Assert result == 100.0.

11. `test_manage_position_no_force_exit_when_short_hold`
    — Position held ≤30 business days, same PnL condition. Assert result ≠ price_10am
    (normal stop management applies). Use make_position_df(n=20, price=100.0),
    entry_date=df.index[0], entry_date close to df.index[-1].

12. `test_manage_position_no_force_exit_when_profitable`
    — Position held >30 business days, unrealised_pnl >= 0.3 × RISK_PER_TRADE.
    Use price_10am = entry_price + 2×RISK_PER_TRADE/shares (well above threshold).
    Assert result ≠ price_10am.

13. `test_manage_position_force_exit_never_lowers_stop`
    — Position held >30 business days, unrealised_pnl < threshold, BUT existing stop
    is already above price_10am. Assert returned stop == existing stop (no lowering).
    E.g. existing stop = 105.0, price_10am = 100.0 → return max(105.0, 100.0) = 105.0.

14. `test_manage_position_force_exit_respects_current_price`
    — Basic: force exit returns stop == price_10am when price_10am > current_stop.

**program.md tests**:
15. `test_program_md_fold_test_days_default_is_40`
    — Read program.md, assert '40' appears in the fold_test_days guidance section.

16. `test_program_md_consistency_floor_uses_max_simultaneous_positions`
    — Read program.md, assert 'MAX_SIMULTANEOUS_POSITIONS' in consistency floor description.

17. `test_program_md_deadlock_detection_rule_present`
    — Read program.md, assert 'Deadlock detection' in text.

18. `test_program_md_position_management_iteration_guidance_present`
    — Read program.md, assert 'iterations 6' in text (or equivalent marker).

**Validation**: `uv run pytest tests/test_v4_a.py -v` — all pass

#### Task 3.2: Run full test suite

**Purpose**: Confirm no regressions and GOLDEN_HASH still passes.
**Dependencies**: Task 3.1
**Steps**:
1. `uv run pytest tests/ -v`
2. Expected: all previously passing tests still pass + new V4-A tests pass.
   The GOLDEN_HASH test MUST pass (no immutable zone was touched).
3. If any previously-passing test fails (not one of the two `make_signal_df` tests
   which are intentionally being updated), investigate and fix before completing.

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_v4_a.py`,
`tests/test_screener.py`, `tests/test_backtester.py` | **Run**: `uv run pytest tests/ -v`

### Edge Cases

- **Earnings date exactly 14 days away**: ✅ `test_screen_day_rejects_earnings_exactly_14_days`
- **Earnings date = today**: ✅ `test_screen_day_rejects_earnings_day_itself`
- **next_earnings_date column absent (old parquet)**: ✅ `test_screen_day_passes_no_earnings_column`
- **Force exit stop never lowers**: ✅ `test_manage_position_force_exit_never_lowers_stop`
- **Short-hold position not force-exited**: ✅ `test_manage_position_no_force_exit_when_short_hold`
- **R9 old fallback signal now rejected**: ✅ `test_screen_day_rejects_fallback_stop_entry`
- **program.md text assertions**: ✅ tests 15–18 in test_v4_a.py

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit tests — new (test_v4_a.py) | 18 | 75% |
| ✅ Updated fixtures (test_screener.py + test_backtester.py) | ~4 | 17% |
| ✅ Regression: full suite (GOLDEN_HASH + existing) | 49+ | — |
| ⚠️ Manual | 0 | 0% |
| **Total new** | ~22 | 100% |

**Note on live parquet test**: `prepare.py`'s `_add_earnings_dates` is tested structurally
only (column presence/dtype). A live yfinance test is not included — the earnings_dates API
is a network call and yfinance behavior varies. The exception handler in `_add_earnings_dates`
ensures graceful fallback to NaT if the API returns unexpected data.

---

## VALIDATION COMMANDS

### Level 1: Import Checks

```bash
python -c "import train; print('train ok')"
python -c "import prepare; print('prepare ok')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_v4_a.py -v
```

### Level 3: Full Suite + GOLDEN_HASH

```bash
uv run pytest tests/ -v
```

Expected: all 49+ pre-V4-A tests pass + 18+ new V4-A tests pass.
GOLDEN_HASH test must pass (immutable zone untouched).

### Level 4: Smoke Check

```bash
# Verify screen_day rejects fallback-stop signal:
uv run python -c "
from tests.test_screener import make_signal_df
from train import screen_day
from datetime import date
df = make_signal_df(250)
today = df.index[-1]
result = screen_day(df, today)
assert result is None, f'Expected None (R9), got {result}'
print('R9 smoke: OK')
"
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `screen_day()` returns `None` when `find_stop_price()` returns `None` (R9: no fallback stop allowed)
- [ ] `screen_day()` returns a signal dict with `stop_type == 'pivot'` when a valid pivot stop is found
- [ ] `screen_day()` returns `None` when `next_earnings_date` is 0–14 calendar days from today (R8)
- [ ] `screen_day()` returns a signal when `next_earnings_date` is exactly 15+ days away
- [ ] `screen_day()` returns a signal when `next_earnings_date` is `NaT` or the column is absent (backward compat)
- [ ] `manage_position()` returns `max(current_stop, price_10am)` when held >30 business days AND unrealised P&L < `0.3 × RISK_PER_TRADE` (R10)
- [ ] `manage_position()` does not force exit when held ≤30 business days, regardless of P&L
- [ ] `manage_position()` does not force exit when held >30 business days but P&L ≥ threshold
- [ ] `prepare.py` writes a `next_earnings_date` column (nullable date objects) to each ticker's parquet
- [ ] `prepare.py` handles `earnings_dates` API failures gracefully (writes `NaT` column, does not crash)

### Error Handling
- [ ] `screen_day()` with `next_earnings_date` column absent (old parquet): filter silently skips, no `KeyError`
- [ ] `manage_position()` R10 guard never lowers existing stop: returns `max(current_stop, price_10am)`
- [ ] `_add_earnings_dates()` wraps `yf.Ticker().earnings_dates` in try/except; fills `NaT` on any exception

### program.md
- [ ] `FOLD_TEST_DAYS` default changed from `20` to `40` in step 4b setup instructions
- [ ] Consistency floor formula updated to `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10` in loop step 8 and in the `discard-inconsistent` status definition
- [ ] Deadlock detection pivot rule added (4 consecutive kept iterations with same `min_test_pnl` → pivot to `mean_test_pnl` for 3 iterations)
- [ ] Explicit position management priority guidance added (test trailing-stop / breakeven in iterations 6–10)

### Validation
- [ ] `python -c "import train; import prepare"` exits 0
- [ ] `uv run pytest tests/test_v4_a.py -v` passes with ≥15 new tests, all green
- [ ] `uv run pytest tests/ -v` passes with 0 regressions from pre-V4-A baseline
- [ ] GOLDEN_HASH test passes (no immutable zone was touched)
- [ ] `make_signal_df` fixture now causes `screen_day()` to return `None` (R9 demonstration)
- [ ] New `make_pivot_signal_df` fixture causes `screen_day()` to return a non-None signal

### Out of Scope
- Live yfinance network test for `earnings_dates` — API is flaky; unit tests use synthetic data only
- Automatic deletion/refresh of existing parquet files — user must delete and re-run `prepare.py`
- V4-B or V4-C changes — separate plans
- GOLDEN_HASH update — immutable zone is untouched in V4-A

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: prepare.py `_add_earnings_dates` added, `process_ticker` updated
- [ ] Task 1.2 complete: program.md R1/R3/R5/R6 edits applied and verified
- [ ] Task 2.1 complete: R9 one-line change in screen_day() applied
- [ ] Task 2.2 complete: R8 earnings guard added to screen_day()
- [ ] Task 2.3 complete: R10 business-day exit added to manage_position()
- [ ] Task 3.1 complete: test_v4_a.py created (≥15 tests); fixtures updated in test_screener.py and test_backtester.py
- [ ] Task 3.2 complete: full suite passes, GOLDEN_HASH passes
- [ ] `python -c "import train; import prepare"` — no errors
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Parquet Refresh Requirement

Existing parquet files in `~/.cache/autoresearch/stock_data/` do NOT have the
`next_earnings_date` column. The R8 earnings filter will silently no-op on old files
(the column-existence guard handles this gracefully). For new optimization sessions to
benefit from R8, the user must:
1. Delete existing parquet files (or move to backup)
2. Re-run `uv run prepare.py` to regenerate with `next_earnings_date`

This is documented in `prd.md` V4-A compatibility notes: "R8 requires a data refresh."

### R9 Trade Count Impact

R9 (reject fallback stops) will reduce the number of signals returned by `screen_day()`.
In the `multisector-mar23` dataset, ~25% of trades used fallback stops. Expect fold trade
counts to drop by approximately 25%. The next optimization session should use
`FOLD_TEST_DAYS=40` (R1) and `WALK_FORWARD_WINDOWS=7–9` to compensate.

### R10 Threshold Calibration

The threshold `0.3 × RISK_PER_TRADE` = $15 at default RISK_PER_TRADE=50. The PRD examples
show COF ($7) and AMZN ($13) being recycled while APP ($81) and PLTR ($40) are preserved.
The $15 threshold correctly separates these cases. It scales automatically when RISK_PER_TRADE
changes — no separate calibration needed.

### stop_type Field After R9

With R9 active, `stop_type` in the signal dict is always `'pivot'`. The field is kept in
the return dict for `trades.tsv` backward compatibility. A future cleanup could remove it,
but that would change the trades.tsv schema — out of scope for V4-A.
