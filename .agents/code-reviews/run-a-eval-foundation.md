# Code Review: run-a-eval-foundation

**Date**: 2026-03-25
**Plan**: `.agents/plans/run-a-eval-foundation.md`
**Reviewer**: Claude Sonnet 4.6

---

## Stats

- Files Modified: 7 (train.py, program.md, tests/test_screener.py, tests/test_backtester.py, tests/test_v4_b.py, tests/test_program_md.py, tests/test_v4_a.py)
- Files Added: 0
- Files Deleted: 0
- New lines: ~173 (excluding trades.tsv and PROGRESS.md which were not in scope)
- Deleted lines: ~49

## Test Results

All 126 targeted tests pass (`test_backtester.py`, `test_screener.py`, `test_v4_a.py`, `test_v4_b.py`, `test_program_md.py`).

**Pre-existing failures** (on HEAD before this changeset):
- `test_manage_position_raises_to_breakeven` — used `price=103.0` but BE threshold is `entry + 1.0×ATR`, so at `atr=2`, the threshold is `102.0`; `price=103.0` triggered the trail stop at `103 - 1.0×2 = 101.0`, not `100.0`. This changeset fixes it by using `price=102.5`, which is below the trail activation (`recent_high < 103`). The fix is correct.
- `test_manage_position_never_lowers_existing_stop` — same root cause, same fix. Correct.

---

## Issues Found

### MEDIUM

```
severity: medium
file: train.py
line: 386-390
issue: manage_position() docstring contradicts the actual implementation
detail: The docstring says "Breakeven once price_1030am >= entry + 1.5 ATR" and
        "Trail by 1.2 ATR below recent high once 2.0 ATR in profit." The actual code
        (unchanged by this PR, pre-existing in HEAD) uses 1.0× ATR for breakeven,
        1.5× ATR for trail activation, and 1.0× ATR for trail distance. The updated
        test_manage_position_trail_uses_1_0_atr docstring (test_backtester.py:515-519)
        and test comments correctly describe the actual coefficients, but the function
        docstring in train.py remains stale and misleading. Agents reading the docstring
        will receive wrong information when calibrating iteration 2-4 experiments.
suggestion: Update the manage_position docstring to read:
        "Breakeven once price_1030am >= entry + 1.0 ATR.
         Trail 1.0 ATR below recent high once 1.5 ATR in profit.
         Never lower the stop."
        This is a one-line-per-coefficient change and does not touch the DO NOT EDIT
        boundary.
```

### MEDIUM

```
severity: medium
file: program.md
line: 335 (column definition list)
issue: mean_test_pnl column is numbered 14 but its position in the header is 2
detail: The TSV header order is: commit, min_test_pnl, mean_test_pnl, train_pnl,
        test_pnl, ... status, description. The numbered column definitions list mean_test_pnl
        as item 14 (appended after description at item 13). An agent generating or
        parsing a results row from the column definitions will place mean_test_pnl in
        the wrong column position — column 14 instead of column 3. The example rows
        show it in the correct position (column 3), but the numbered list is inconsistent.
suggestion: Insert mean_test_pnl as item 2 in the column definitions list, renumbering
        items 2-13 as 3-14:
        "2. `mean_test_pnl`: arithmetic mean of all fold test P&Ls ..."
        then "3. `train_pnl`: most recent fold's ..."
        This aligns the definition list with the TSV header order.
```

### LOW

```
severity: low
file: program.md
line: 597-598 (Run A Agenda, Iter 9 description)
issue: Iter 9 SMA slope instruction partially duplicates existing slope_floor logic
detail: screen_day() already has a slope_floor check: if sma20 < sma20_5d_ago * slope_floor
        (0.995 for bull path). Iter 9 proposes requiring sma20 > sma20_5d_ago * 1.002.
        This is a tighter absolute constraint, not a structural change. The agenda text
        calls it "a stricter form of the existing slope_floor check" which is accurate.
        However, the instruction says "SMA20 today" — given the SMA20 is computed from
        hist (close_hist = hist['close']), this is SMA20 as of yesterday's close, not
        today's. The wording is technically imprecise but will be understood correctly
        by an agent reading the surrounding code context.
        No look-ahead risk. Operational concern only.
suggestion: Clarify: "require sma20 (as computed from hist, i.e., yesterday's data)
        > sma20_5d_ago × 1.002" to avoid ambiguity about which day's SMA20 is meant.
```

---

## Review of Specific Focus Areas

### 1. Dollar volume filter placement — CORRECT

The filter is at `train.py:324-328`, after the `prev_vol_ratio` check and after `hist = df.iloc[:-1]` is assigned (line 265). It uses `hist['close'] * hist['volume']` — both arrays from `hist`, which excludes today's row entirely. `iloc[-60:]` on `hist` takes 60 prior trading days (today excluded). The minimum row guard `len(df) < 102` ensures `hist` always has ≥ 101 rows, so `iloc[-60:]` always returns 60 rows. No look-ahead. Placement is correct.

### 2. MIN_DOLLAR_VOLUME = $150M — appropriate starting value

At 381 tickers: $150M/day targets approximately the top 150 most-liquid tickers. The comment documents $100M ≈ top 200 and $200M ≈ top 100, giving the agent clear calibration anchors. The Run A Agenda then tests $100M, $200M, and $250M in iterations 5-7, with $175M as an interpolation step if needed. The floor is appropriate as a starting experiment value; it is neither too restrictive (total_trades check catches over-filtering) nor too loose (it meaningfully reduces the universe).

### 3. Test fixture volume changes — CORRECT and semantically sound

`make_signal_df` and `make_signal_df_for_backtest`: changed from 1M to 2M base volume. With close prices around $100, `avg_dol_vol = 100 × 2M = $200M ≥ $150M`. The signal day volume (3M) is on the last row which is excluded from `hist` in screen_day, so it does not pollute the dollar-vol calculation. The comment explains this explicitly.

`make_passing_df` (not changed): retains 1M volume but close prices are in the $175-196 range (linspace 100→200 over 250 bars), giving `avg_dol_vol ≈ $185M ≥ $150M`. It passes the filter naturally. Tests using `make_passing_df` with `screen_day` are testing failure conditions that return None before reaching the dollar volume check (SMA alignment, price breakout, etc.), so there is no correctness risk from the 1M volume in that fixture.

`test_manage_position_trail_uses_1_0_atr` (in test_backtester.py): retains 1M volume in its df — this is correct because that test calls `manage_position()` directly, not `screen_day()`, so the dollar volume filter is never invoked.

### 4. program.md additions

**Session Override block**: Logically self-contained. The Active=NO default ensures backward compatibility. The template fields are properly masked (no real values). No issues.

**Position management priority update**: Correct — moves the priority window from "iterations 6-10" to "iterations 2-4", consistent with the Run A Agenda's sequencing.

**discard-thin floor**: The 110-trade floor is well-specified with a clear computation rationale ("27% of baseline trades"). No issues.

**Structural vs threshold guidance**: Accurate summary of prior run results. The RSI/SMA characterization is consistent with the cited data.

**mean_test_pnl column**: Column order issue noted above (medium severity). The awk extraction command is correct for the log format. The "do not use as keep/discard criterion" constraint is explicit. The discard-manual-review flag is well-defined with required description format.

**Run A Agenda**: Iterations 2-4 correctly re-validate parameters that were tuned under 7×40 fold config. The agenda is sequential and self-contained. The gate assessment note ("record fold6_train_total_trades") provides continuity into Run B. No issues.

---

## Summary

Two medium issues require attention before executing Run A:
1. The `manage_position()` docstring is stale — it describes the old 1.5/1.2/2.0 ATR coefficients, not the current 1.0/1.0/1.5 coefficients. An agent reading the docstring before calibrating iterations 2-4 will start from wrong baseline assumptions.
2. The `mean_test_pnl` column definition is numbered out of order in program.md — it appears as item 14 when it is the 3rd column in the TSV header.

Both can be fixed with small targeted edits. The dollar volume filter itself is technically sound.
