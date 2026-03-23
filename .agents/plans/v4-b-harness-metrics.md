# Feature: V4-B Harness Metric Improvements and Position Management Refinements

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

V4-B fixes what the optimization loop measures and tracks (R2, R4, R11) and applies three position-management refinements derived from multisector-mar23 trade analysis (R13, R14, R15). It is the second prerequisite for Phase 1 sector worktrees — V4-A must already be complete before running this plan.

Seven changes are included:
- **R2** – `min_test_pnl` fold trade-count guard: exclude folds with < 3 test trades when computing the minimum (prevents a sparse, noise-dominated fold from locking the optimizer)
- **R4** – Add `train_avg_pnl_per_trade` column to `results.tsv` schema in `program.md` (print lines already exist in code; only the TSV header / agent instructions need updating)
- **R11** – Track win/loss dollar ratio: compute `avg_win_loss_ratio` in `run_backtest()`, emit `win_loss_ratio:` in `print_results()`, and add a `discard-fragile` guard in `program.md`
- **R13** – Tighten trailing-stop distance 1.5× → 1.2× ATR in `manage_position()` (mutable zone)
- **R14** – Partial profit taking at +1.0R: when `price_10am >= entry + 1.0 × ATR` for the first time, close 50% of the position at current price, halve `position['shares']`, and record a partial trade exit row. Implemented in `run_backtest()` (immutable zone); `manage_position()` signature unchanged.
- **R15** – Early stall exit: when days_held ≤ 5 calendar days and price_10am < entry + 0.5 × ATR, set stop at price_10am to force exit (mutable zone)
- **R16** – Restructure walk-forward folds for longer test windows: change `WALK_FORWARD_WINDOWS = 3 → 7` and `FOLD_TEST_DAYS = 20 → 40` in train.py mutable-zone constants; update program.md to document 7 × 40-day as the production default. No GOLDEN_HASH update required (mutable zone only).
- **GOLDEN_HASH** – Single GOLDEN_HASH update covering R2, R11, and R14 (all immutable zone changes)

R7 (sector concentration guard) is marked **optional, low priority** in the PRD and is **not implemented** in this plan due to the sector-lookup complexity it requires.

## User Story

As a developer running the autoresearch optimization loop,
I want fold-quality guards and per-trade quality metrics in the harness output,
So that the optimizer converges on genuinely better strategies rather than strategies
that look good only in sparse or lucky test folds.

## Problem Statement

The `multisector-mar23` run revealed three harness measurement gaps: (1) a single fold with zero trades could dominate `min_test_pnl` and lock the optimizer at −$0.00; (2) `avg_pnl_per_trade` and win/loss ratio were not tracked, hiding strategies with high trade counts but tiny individual wins; (3) the trailing stop was too loose (1.5×) and there were no profit-taking or early-stall-exit mechanics, causing gradual erosion of open winners.

## Solution Statement

R2, R11, and R14 extend the immutable harness (`run_backtest()`, `print_results()`, `__main__`) — requiring one GOLDEN_HASH update. R13 and R15 are one- to three-line changes in `manage_position()` (mutable zone). R16 changes two mutable-zone constants (`WALK_FORWARD_WINDOWS`, `FOLD_TEST_DAYS`) and updates `program.md` with production defaults and fold schedule — no GOLDEN_HASH impact. `program.md` is updated with new metric columns, discard conditions, fold-count interpretation, and R16 configuration guidance.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (both zones), `program.md`,
  `tests/test_backtester.py`, `tests/test_optimization.py`
**Dependencies**: V4-A must be complete (R9 fallback-stop rejection, R10 time-based exit
  already active; R13/R15 extend `manage_position()`; R14 extends `run_backtest()`)
**Breaking Changes**: None — all existing tests continue to pass. GOLDEN_HASH update required.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` lines 36, 41 — `WALK_FORWARD_WINDOWS`, `FOLD_TEST_DAYS` constants — R16 target
- `train.py` (lines 320–348) — `manage_position()` — R13, R15 target (R14 is in run_backtest)
- `train.py` (lines 556–638) — `run_backtest()` Sharpe/stats block — R11 target
- `train.py` (lines 642–654) — `print_results()` — R11 emit target
- `train.py` (lines 758–823) — `__main__` walk-forward loop and `min_test_pnl` block — R2 target
- `tests/test_optimization.py` (lines 111–128) — `GOLDEN_HASH` test — must be updated
- `tests/test_optimization.py` (lines 390–401) — `_exec_main_block()` helper — reuse for R2 tests
- `tests/test_optimization.py` (lines 403–435) — `test_main_runs_walk_forward_windows_folds` — pattern for mock run_backtest dicts (include all required keys)
- `tests/test_backtester.py` (lines 1–46) — fixtures `make_position_df`, `make_minimal_df`, `make_signal_df_for_backtest` — reuse for manage_position tests
- `program.md` (lines 249–286) — results.tsv schema and Logging section — R4, R11 target
- `program.md` (lines 292–313) — experiment loop keep/discard rules — R11 discard-fragile target

### New Files to Create

- `tests/test_v4_b.py` — New unit tests for R2, R11

### Patterns to Follow

- **Test fixture naming**: `make_<purpose>_df`
- **Test naming**: `test_<behaviour>_<condition>`
- **Mock run_backtest dicts**: must include all keys returned by the real function to avoid KeyErrors in `__main__`. After V4-B, the full key set is:
  `sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl, ticker_pnl, backtest_start, backtest_end, trade_records, max_drawdown, calmar, pnl_consistency, pnl_min, regime_stats, avg_win_loss_ratio`
- **`_exec_main_block()`**: import from `test_optimization.py` or replicate inline. Overlays a dict onto the train module namespace and executes the `if __name__ == "__main__":` block.
- **GOLDEN_HASH recompute command** (run after all immutable zone changes):
  ```
  python -c "import hashlib; s=open('train.py', encoding='utf-8').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
  ```
  Update the string in `tests/test_optimization.py` at the line `GOLDEN_HASH = "..."`.

---

## KEY IMPLEMENTATION DETAILS

### R11 — Win/Loss Dollar Ratio in `run_backtest()` (immutable zone)

**Location**: After the `pnl_min` block (line ~610), before the `regime_stats` block.

```python
# R11: Win/loss dollar ratio — mean winner P&L divided by |mean loser P&L|.
# Returns 0.0 when there are no winners or no losers (avoids division by zero).
_winning_pnls = [p for p in trades if p > 0]
_losing_pnls  = [p for p in trades if p < 0]
if _winning_pnls and _losing_pnls:
    avg_win_loss_ratio = round(
        float(np.mean(_winning_pnls) / abs(np.mean(_losing_pnls))), 3
    )
else:
    avg_win_loss_ratio = 0.0
```

Add `"avg_win_loss_ratio": avg_win_loss_ratio` to the `return` dict.

**Location in `print_results()`**: After the `pnl_min` line (currently last), add:

```python
print(f"{prefix}win_loss_ratio:      {stats.get('avg_win_loss_ratio', 0.0):.3f}")
```

### R2 — Fold Trade-Count Guard in `__main__` (immutable zone)

**Location**: The current `min_test_pnl` computation block in `__main__` (lines ~797–799):

```python
min_test_pnl = min(fold_test_pnls) if fold_test_pnls else 0.0
print("---")
print(f"min_test_pnl:            {min_test_pnl:.2f}")
```

`fold_test_pnls` currently accumulates only `total_pnl` floats. Change to accumulate
`(total_pnl, total_trades)` tuples:

```python
# In the fold loop body, replace:
#   fold_test_pnls.append(_fold_test_stats["total_pnl"])
# with:
fold_test_pnls.append((_fold_test_stats["total_pnl"], _fold_test_stats["total_trades"]))
```

Then replace the `min_test_pnl` computation:

```python
# R2: Exclude folds with < 3 test trades — sparse folds are noise-dominated.
# If all folds are excluded, fall back to the raw minimum of all folds.
_qualified = [(p, t) for p, t in fold_test_pnls if t >= 3]
if _qualified:
    min_test_pnl = min(p for p, t in _qualified)
    _n_included  = len(_qualified)
else:
    min_test_pnl = min(p for p, t in fold_test_pnls) if fold_test_pnls else 0.0
    _n_included  = len(fold_test_pnls)
print("---")
print(f"min_test_pnl:            {min_test_pnl:.2f}")
print(f"min_test_pnl_folds_included: {_n_included}")
```

**Note on tuple unpacking elsewhere**: The `fold_test_pnls` list is only used to compute
`min_test_pnl`. There are no other references to it in `__main__`. Confirm by searching
`train.py` for `fold_test_pnls` before making the change.

### R13 — Tighten Trailing Stop 1.5× → 1.2× (mutable zone, `manage_position()`)

**Location**: Line ~347 in `manage_position()`:

```python
# Before:
trail_stop = round(recent_high - 1.5 * atr, 2) if recent_high >= entry_price + 2.0 * atr else current_stop

# After:
trail_stop = round(recent_high - 1.2 * atr, 2) if recent_high >= entry_price + 2.0 * atr else current_stop
```

One character change. The trailing activation threshold (2.0×) is unchanged.

### R14 — Partial Close at +1.0R in `run_backtest()` (immutable zone)

**Location**: Inside the position-management loop in `run_backtest()`, immediately before
the `manage_position()` call.

**At position entry** (where the position dict is created), store two extra fields:

```python
position['atr14']        = float(df_ticker['atr14'].iloc[entry_idx])  # ATR at entry
position['partial_taken'] = False
```

**In the daily loop** (for each day after entry, before calling manage_position), add:

```python
# R14: Partial close at +1.0R — close 50% of position on first up-move of +1 ATR.
if not position.get('partial_taken', False):
    _atr_entry = position.get('atr14', 0.0)
    if _atr_entry > 0 and price_10am >= position['entry_price'] + _atr_entry:
        _close_shares = position['shares'] * 0.5
        _partial_pnl  = round((_close_shares) * (price_10am - position['entry_price']), 2)
        trade_records.append({
            'ticker':     position['ticker'],
            'entry_date': position['entry_date'],
            'exit_date':  today,
            'pnl':        _partial_pnl,
            'exit_type':  'partial',
        })
        position['shares']        *= 0.5
        position['partial_taken']  = True
```

**Interaction note**: After the partial close, the remaining shares continue to be managed
by `manage_position()` normally. R13 (trail at 1.2×) and R15 (early stall) remain in the
mutable zone and are unaffected. The `manage_position()` signature is unchanged.

### R15 — Early Stall Exit (mutable zone, `manage_position()`)

**Location**: In `manage_position()`, after the R10 block and before breakeven/trail logic.
R10 uses `_bdays_held` (business days) and checks > 30 days. R15 uses calendar days and
checks ≤ 5 days:

```python
# R15: Early stall exit — if price stalls in first 5 calendar days, force exit next day.
# Targets the 6–14 day loss cluster from multisector-mar23 (SCHW, UNH, RTX, ORCL all
# stalled at day 5 without momentum). Winners (TSLA, MSTR, GOOGL) clear 0.5 ATR by day 3.
_cal_days_held = (_today_date - position['entry_date']).days
if _cal_days_held <= 5 and price_10am < entry_price + 0.5 * atr:
    return max(current_stop, price_10am)  # force exit; never lower existing stop
```

Place this block immediately after the R10 block (after line ~340 in current train.py).

**Note**: `_today_date` is already computed by R10 (line `_today_date = df.index[-1]`). No
additional date computation needed.

### R16 — Walk-Forward Fold Restructure (mutable zone constants)

**Location**: `train.py` lines 36 and 41 (above the DO NOT EDIT boundary).

```python
# Before:
WALK_FORWARD_WINDOWS = 3
FOLD_TEST_DAYS       = 20

# After:
WALK_FORWARD_WINDOWS = 7
FOLD_TEST_DAYS       = 40
```

**Rationale**: The 19-month date range (Sep 2024 → Mar 2026, ~378 business days) supports
7 × 40-day test windows = 280 test days. Fold 1 training data starts at Sep 2024, giving
~98 business days of training context — sufficient for the optimizer. A 40-day test window
captures the strategy's typical hold duration (avg 30–90 days) rather than force-closing
mid-hold as the prior 20-day window did. The old default of `WALK_FORWARD_WINDOWS = 3`
was a development convenience; this change sets the production default explicitly.

**Impact on tests**: All existing tests that reference `train.WALK_FORWARD_WINDOWS` do so
dynamically (`train.WALK_FORWARD_WINDOWS * 2 + 1`, `range(train.WALK_FORWARD_WINDOWS)`, etc.)
and adapt automatically. No test hardcodes the value 3. The R2 tests in `test_v4_b.py` must
also use `train.WALK_FORWARD_WINDOWS` (or a local constant) dynamically — do NOT hardcode 3.

**FOLD_TEST_DAYS note**: The V4-A test `test_program_md_fold_test_days_default_is_40` already
passes because program.md documents 40 as the recommended value. This change makes the
code constant match the documented recommendation.

### program.md — R4, R11, R2, R16 Updates

**R4** — In the Logging section, `results.tsv` header row, add `train_avg_pnl_per_trade`
after `win_rate`:

Old header:
```
commit	min_test_pnl	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	train_calmar	train_pnl_consistency	status	description
```

New header (add `train_avg_pnl_per_trade` after `win_rate`):
```
commit	min_test_pnl	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	train_avg_pnl_per_trade	train_win_loss_ratio	train_calmar	train_pnl_consistency	status	description
```

Also add `train_avg_pnl_per_trade` and `train_win_loss_ratio` to the column definitions list.
Update the example TSV row to include values for both new columns.

**R11** — In the keep/discard rules (step 8), add a third `discard-fragile` condition:

> If `avg_win_loss_ratio < 0.5` **and** `train_pnl_consistency < floor`: log status
> `discard-fragile` and revert. A win/loss ratio below 0.5 means losers are on average more
> than 2× the size of winners — the strategy depends on a high win rate that is unlikely
> to persist out-of-sample.

Add to the output format section the new grep command:
```bash
grep "^fold3_train_win_loss_ratio:" run.log
```

**R2** — In the output format section, after the `min_test_pnl:` line, add:
```
min_test_pnl_folds_included: N
```
Add interpretation note: "N = number of folds with ≥ 3 test trades included in the minimum.
If N < WALK_FORWARD_WINDOWS, at least one fold was sparse and excluded. If N = 0 (all folds
sparse), the raw minimum is used and N equals WALK_FORWARD_WINDOWS."

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 1: Parallel foundations (different files, no interdependency)│
├──────────────────────────────────────────────────────────────────┤
│ Task 1.1: train.py manage_pos.   │ Task 1.2: program.md updates  │
│  (R13, R15 in manage_pos.)       │  (R4, R11, R2, R16 docs)      │
│ Task 1.3: train.py constants     │                               │
│  (R16: WALK_FORWARD_WINDOWS=7,   │                               │
│   FOLD_TEST_DAYS=40)             │                               │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 2: Immutable zone changes (same section of train.py)         │
├──────────────────────────────────────────────────────────────────┤
│ Task 2.1: run_backtest() — R11 win/loss computation + dict key    │
│ Task 2.2: print_results() — R11 emit win_loss_ratio line          │
│ Task 2.3: __main__ — R2 fold trade-count guard + folds_included   │
│ Task 2.4: run_backtest() — R14 partial close at +1.0R             │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 3: GOLDEN_HASH update (depends on Wave 2 completion)         │
├──────────────────────────────────────────────────────────────────┤
│ Task 3.1: Recompute hash and update test_optimization.py          │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 4: Tests (parallel; both depend on Waves 1–3)                │
├──────────────────────────────────────────────────────────────────┤
│ Task 4.1: New tests/test_v4_b.py   │ Task 4.2: Update             │
│   (R2 fold exclusion, R11 ratio)   │   tests/test_backtester.py   │
│                                    │   (R13 trail, R15 stall,     │
│                                    │    R14 run_backtest integ.)  │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 5: Full test suite (depends on Wave 4)                       │
├──────────────────────────────────────────────────────────────────┤
│ Task 5.1: uv run pytest tests/ -x  — verify all pass             │
└──────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Parallel**: Tasks 1.1, 1.2, and 1.3 modify different parts of different files; all three can be dispatched simultaneously.
**Wave 2 — Sequential**: Tasks 2.1, 2.2, 2.3, 2.4 all modify the immutable zone of `train.py`; run in order.
**Wave 3 — Sequential**: GOLDEN_HASH depends on Wave 2 being complete.
**Wave 4 — Parallel**: Tasks 4.1 and 4.2 modify different test files; both can be dispatched simultaneously.
**Wave 5 — Sequential**: Final test run depends on Wave 4.

### Interface Contracts

**Wave 1 → Wave 4**: `manage_position()` accepts `position` dict with at minimum:
  `{entry_price, stop_price, shares, entry_date, ticker}`. Returns `float` (updated stop).

**Wave 2 → Wave 3**: `run_backtest()` return dict gains `avg_win_loss_ratio: float`.
  `print_results()` emits a `{prefix}win_loss_ratio: X.XXX` line.
  `__main__` emits `min_test_pnl_folds_included: N` after `min_test_pnl:`.

**Wave 2 → Wave 4**: Mock run_backtest dicts in test_v4_b.py must include `avg_win_loss_ratio`
  alongside all existing keys. R14 integration test (Task 4.2) requires `atr14` column in
  the ticker DataFrame and `partial_taken`/`atr14` fields in the position dict.

---

## IMPLEMENTATION PLAN

### Wave 1 (Parallel)

---

#### Task 1.1: UPDATE train.py manage_position() — R13, R15

**WAVE**: 1
**AGENT_ROLE**: Mutable-zone editor
**PURPOSE**: Apply two position-management refinements above the DO NOT EDIT boundary.
**DEPENDS_ON**: Nothing (mutable zone is independent of immutable zone changes)

**Note**: R14 (partial close) is implemented in `run_backtest()` (immutable zone) in Task 2.4.
`manage_position()` signature and return type are unchanged.

**Steps**:

1. Read `train.py` lines 320–350 (`manage_position()`) to confirm current code structure.

2. **R13** — Change trailing stop multiplier from 1.5 to 1.2.
   Find: `round(recent_high - 1.5 * atr, 2)`
   Replace with: `round(recent_high - 1.2 * atr, 2)`

3. **R15** — Add early stall guard after the R10 block and before the breakeven block.
   Find the R10 block ending with `return max(current_stop, price_10am)`.
   Immediately after it, insert:
   ```python
   # R15: Early stall exit — force exit if price stalls in first 5 calendar days
   _cal_days_held = (_today_date - position['entry_date']).days
   if _cal_days_held <= 5 and price_10am < entry_price + 0.5 * atr:
       return max(current_stop, price_10am)  # force exit; never lower existing stop
   ```

4. **Validate**: `python -c "import train; print('manage_position ok')"` — no errors.

---

#### Task 1.2: UPDATE program.md — R4, R11, R2, R16 instructions

**WAVE**: 1
**AGENT_ROLE**: Documentation editor
**PURPOSE**: Update agent instructions for new metrics, fold-quality interpretation, and R16 fold defaults.
**DEPENDS_ON**: Nothing

**Steps**:

1. **R4 — results.tsv header**: Find the `Header row (tab-separated):` block in the Logging
   section. Replace the header with the new 13-column version adding `train_avg_pnl_per_trade`
   and `train_win_loss_ratio` after `win_rate`:
   ```
   commit	min_test_pnl	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	train_avg_pnl_per_trade	train_win_loss_ratio	train_calmar	train_pnl_consistency	status	description
   ```
   Update the column definitions list: add entries for columns 8 (`train_avg_pnl_per_trade`)
   and 9 (`train_win_loss_ratio`), and renumber `train_calmar` to 10, `train_pnl_consistency`
   to 11, `status` to 12, `description` to 13.
   Update the example TSV row to include placeholder values for both new columns (e.g., `18.45` and `1.250`).

2. **R11 — discard-fragile guard**: Find the keep/discard step 8 conditions. After the
   existing `discard-inconsistent` condition, add:
   > If `train_win_loss_ratio < 0.5` **and** `train_pnl_consistency < floor`: log status
   > `discard-fragile` and revert (`git reset --hard HEAD~1`). A ratio below 0.5 means average
   > losers are more than 2× the size of average winners — the strategy is loss-amplifying and
   > unlikely to hold out-of-sample. Grep: `grep "^fold${WALK_FORWARD_WINDOWS}_train_win_loss_ratio:" run.log`

3. **R11 — grep command**: In the Output format section, after the existing grep commands block,
   add:
   ```bash
   grep "^fold3_train_win_loss_ratio:" run.log
   ```
   (alongside the other `fold3_train_*` greps; note to replace `fold3` with `fold${WALK_FORWARD_WINDOWS}`)

4. **R2 — output format**: In the Output format section, after the `min_test_pnl:` example line,
   add the new `min_test_pnl_folds_included: N` line to the output block example.
   Add a note below:
   > `min_test_pnl_folds_included: N` — number of folds with ≥ 3 test trades used to compute
   > the minimum. If N < WALK_FORWARD_WINDOWS, at least one fold was sparse (< 3 trades) and
   > excluded. If N = 0 (all folds sparse), the raw minimum is used. Treat the metric as less
   > reliable when N ≤ 1.

5. **R16 — production defaults and fold schedule**: In the harness constants section of
   program.md, update `FOLD_TEST_DAYS` default from 20 to **40** and `WALK_FORWARD_WINDOWS`
   from 9 to **7**. Replace or update the `WALK_FORWARD_WINDOWS` recommended-values block
   to make `7` the stated default (not just an option). Add the fold schedule table:
   ```
   | Fold | Test window (approx)        | Fold 1 train |
   |------|-----------------------------|--------------|
   | 1    | Apr – Jun 2025              | Sep 2024 – Mar 2025 |
   | 2    | Jun – Aug 2025              | Sep 2024 – May 2025 |
   | 3    | Aug – Oct 2025              | Sep 2024 – Jul 2025 |
   | 4    | Oct – Dec 2025              | Sep 2024 – Sep 2025 |
   | 5    | Dec 2025 – Jan 2026         | Sep 2024 – Nov 2025 |
   | 6    | Jan – Feb 2026              | Sep 2024 – Jan 2026 |
   | 7    | Feb – Mar 2026              | Sep 2024 – Feb 2026 |
   ```
   Update the "For `WALK_FORWARD_WINDOWS = 3`, the output is:" example block to say 7
   and update the grep example (`fold3_train_*`) to use `fold7_train_*` (or `fold${WALK_FORWARD_WINDOWS}_train_*`).

6. **Validate**:
   ```python
   python -c "
   t=open('program.md', encoding='utf-8').read()
   assert 'train_avg_pnl_per_trade' in t
   assert 'train_win_loss_ratio' in t
   assert 'min_test_pnl_folds_included' in t
   assert 'WALK_FORWARD_WINDOWS' in t
   print('program.md OK')
   "
   ```

---

#### Task 1.3: UPDATE train.py constants — R16 fold restructure

**WAVE**: 1
**AGENT_ROLE**: Constants editor
**PURPOSE**: Set production defaults for walk-forward fold configuration.
**DEPENDS_ON**: Nothing

**Steps**:

1. Read `train.py` lines 34–44 to confirm current constant values:
   - `WALK_FORWARD_WINDOWS = 3`
   - `FOLD_TEST_DAYS = 20`

2. Change `WALK_FORWARD_WINDOWS` from 3 to 7.

3. Change `FOLD_TEST_DAYS` from 20 to 40.

4. **Validate**: `python -c "import train; assert train.WALK_FORWARD_WINDOWS == 7; assert train.FOLD_TEST_DAYS == 40; print('R16 constants ok')"`

**Note**: No GOLDEN_HASH update required — these constants are in the mutable zone (above
`# ── DO NOT EDIT BELOW THIS LINE`). All existing tests reference `train.WALK_FORWARD_WINDOWS`
dynamically and will adapt automatically (no hardcoded fold count of 3 in the test suite).

---

### Wave 2 (Sequential within wave)

Read `train.py` fully before making changes to understand the current immutable-zone layout.
**All four tasks in Wave 2 modify the immutable zone of `train.py`.**
Complete them in order 2.1 → 2.2 → 2.3 before proceeding to Wave 3.

---

#### Task 2.1: UPDATE train.py run_backtest() — R11 win/loss ratio computation

**WAVE**: 2
**AGENT_ROLE**: Immutable-zone editor
**PURPOSE**: Compute and return `avg_win_loss_ratio` from closed trades.
**DEPENDS_ON**: Wave 1 (for context; code is independent)

**Steps**:

1. Find the `# R9: Robustness perturbation` block (lines ~594–609) and the `regime_stats`
   block (lines ~612–622) in `run_backtest()`.

2. Between `pnl_min` computation and `regime_stats`, insert the R11 computation:
   ```python
   # R11: Win/loss dollar ratio
   _winning_pnls = [p for p in trades if p > 0]
   _losing_pnls  = [p for p in trades if p < 0]
   if _winning_pnls and _losing_pnls:
       avg_win_loss_ratio = round(
           float(np.mean(_winning_pnls) / abs(np.mean(_losing_pnls))), 3
       )
   else:
       avg_win_loss_ratio = 0.0
   ```

3. In the `return` dict, add `"avg_win_loss_ratio": avg_win_loss_ratio` after `"pnl_min"`.

4. Also update the early-exit guard return dict (when `len(trading_days) < 2`, around lines
   ~434–439) to include `"avg_win_loss_ratio": 0.0`.

5. **Validate**: `python -c "import train; s=train.run_backtest({}); assert 'avg_win_loss_ratio' in s; print('R11 run_backtest ok')"` — no errors.

---

#### Task 2.2: UPDATE train.py print_results() — R11 emit line

**WAVE**: 2
**AGENT_ROLE**: Immutable-zone editor
**PURPOSE**: Emit `win_loss_ratio:` in the fixed-format output block.
**DEPENDS_ON**: Task 2.1 (same zone of same file)

**Steps**:

1. Find `print_results()` (lines ~642–654). Locate the last `print(...)` line:
   ```python
   print(f"{prefix}pnl_min:             {stats.get('pnl_min', stats['total_pnl']):.2f}")
   ```

2. After it, add:
   ```python
   print(f"{prefix}win_loss_ratio:      {stats.get('avg_win_loss_ratio', 0.0):.3f}")
   ```

3. **Validate**: `python -c "import io, sys, train; import contextlib; buf=io.StringIO(); [sys.stdout.__class__.write(sys.stdout, l) for l in []]; print('print_results ok')"` — simpler: just confirm syntax compiles: `python -c "import train; print('print_results ok')"`.

---

#### Task 2.3: UPDATE train.py __main__ — R2 fold trade-count guard

**WAVE**: 2
**AGENT_ROLE**: Immutable-zone editor
**PURPOSE**: Exclude sparse test folds from `min_test_pnl`; print `min_test_pnl_folds_included`.
**DEPENDS_ON**: Task 2.2 (same zone of same file)

**Steps**:

1. In `__main__`, find `fold_test_pnls: list = []` (line ~764). It stays as `list`.

2. Find the fold loop body (around line ~793):
   ```python
   fold_test_pnls.append(_fold_test_stats["total_pnl"])
   ```
   Change to:
   ```python
   fold_test_pnls.append((_fold_test_stats["total_pnl"], _fold_test_stats["total_trades"]))
   ```

3. Find the `min_test_pnl` computation block (lines ~797–799):
   ```python
   min_test_pnl = min(fold_test_pnls) if fold_test_pnls else 0.0
   print("---")
   print(f"min_test_pnl:            {min_test_pnl:.2f}")
   ```
   Replace with:
   ```python
   # R2: exclude folds with < 3 test trades (noise-dominated); fall back to raw min if all excluded
   _qualified   = [(p, t) for p, t in fold_test_pnls if t >= 3]
   if _qualified:
       min_test_pnl = min(p for p, t in _qualified)
       _n_included  = len(_qualified)
   else:
       min_test_pnl = min(p for p, t in fold_test_pnls) if fold_test_pnls else 0.0
       _n_included  = len(fold_test_pnls)
   print("---")
   print(f"min_test_pnl:            {min_test_pnl:.2f}")
   print(f"min_test_pnl_folds_included: {_n_included}")
   ```

4. **Validate**: `python -c "import train; print('__main__ ok')"` — no errors.

---

#### Task 2.4: UPDATE train.py run_backtest() — R14 partial close at +1.0R

**WAVE**: 2
**AGENT_ROLE**: Immutable-zone editor
**PURPOSE**: Close 50% of a position when price first reaches +1.0R; record partial trade row.
**DEPENDS_ON**: Task 2.3 (same zone of same file)

**Steps**:

1. Read `train.py` `run_backtest()` to identify: (a) where positions are created/entered
   (position dict construction), and (b) the daily management loop where manage_position
   is called.

2. **At position entry** — in the block where the position dict is first created, add two fields:
   ```python
   position['atr14']         = float(df_ticker['atr14'].iloc[entry_idx])
   position['partial_taken'] = False
   ```
   `entry_idx` is the index into `df_ticker` corresponding to the entry date. If `df_ticker`
   uses a DatetimeIndex, use `.loc[entry_date, 'atr14']` instead of `iloc[entry_idx]`.

3. **In the daily loop** — immediately before the `manage_position()` call for each day, insert
   the partial-close check:
   ```python
   # R14: Partial close at +1.0R — first time price_10am reaches entry + 1 ATR.
   if not position.get('partial_taken', False):
       _atr_entry = position.get('atr14', 0.0)
       if _atr_entry > 0 and price_10am >= position['entry_price'] + _atr_entry:
           _close_shares = position['shares'] * 0.5
           _partial_pnl  = round(_close_shares * (price_10am - position['entry_price']), 2)
           trade_records.append({
               'ticker':     position['ticker'],
               'entry_date': position['entry_date'],
               'exit_date':  today,
               'pnl':        _partial_pnl,
               'exit_type':  'partial',
           })
           position['shares']        *= 0.5
           position['partial_taken']  = True
   ```
   The name of the local variables (`price_10am`, `today`, `trade_records`) must match what
   `run_backtest()` already uses in the management loop — read the code first to confirm.

4. **Validate**: `python -c "import train; print('run_backtest R14 ok')"` — no errors.

---

### Wave 3 (Sequential, depends on Wave 2)

---

#### Task 3.1: UPDATE tests/test_optimization.py — GOLDEN_HASH

**WAVE**: 3
**AGENT_ROLE**: Hash updater
**PURPOSE**: Sync the immutable-zone hash to reflect R2, R11, and R14 changes.
**DEPENDS_ON**: Tasks 2.1, 2.2, 2.3, 2.4 (all immutable zone changes must be complete)

**Steps**:

1. Run the hash recompute command:
   ```bash
   python -c "import hashlib; s=open('train.py', encoding='utf-8').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
   ```

2. In `tests/test_optimization.py`, find the line:
   ```python
   GOLDEN_HASH = "912907497f6da52e3f4907a43a0f176a4b71784194f9ebfab5faae133fd20ea9"
   ```
   Replace the hash string with the new value printed by the command above.

3. **Validate**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -x` — must pass.

---

### Wave 4 (Parallel, depends on Waves 1–3)

---

#### Task 4.1: CREATE tests/test_v4_b.py — R2 fold exclusion + R11 win/loss ratio

**WAVE**: 4
**AGENT_ROLE**: Test author (harness metrics)
**PURPOSE**: Verify R2 fold exclusion logic, R11 win/loss ratio tracking, and R16 constants.
**DEPENDS_ON**: Tasks 1.3, 2.1, 2.2, 2.3, 3.1

Create `tests/test_v4_b.py` with the following tests.

**File structure**: Standard pytest file. Copy `_exec_main_block()` from `test_optimization.py`
(lines 390–400) verbatim. Define a `_fake_stats(total_pnl, total_trades, avg_win_loss_ratio)`
helper that returns a full run_backtest-compatible dict including all keys:
`sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl, ticker_pnl, backtest_start,
backtest_end, trade_records, max_drawdown, calmar, pnl_consistency, pnl_min, regime_stats,
avg_win_loss_ratio`.

**R11 tests** (7 tests):
- `test_run_backtest_returns_avg_win_loss_ratio_key` — `run_backtest({})` → key present
- `test_run_backtest_win_loss_ratio_zero_no_trades` — no trades → `avg_win_loss_ratio == 0.0`
- `test_run_backtest_win_loss_ratio_positive_with_mixed_trades` — patch `screen_day` to
  produce a winning entry then a losing entry; assert `stats['avg_win_loss_ratio'] > 0`
- `test_run_backtest_win_loss_ratio_zero_when_no_losers` — all winning trades → `0.0` sentinel
- `test_print_results_emits_win_loss_ratio_line` — `print_results({'avg_win_loss_ratio':1.5,...})`
  → output contains `win_loss_ratio:`
- `test_print_results_win_loss_ratio_parseable` — value after `win_loss_ratio:` parses as float
- `test_print_results_win_loss_ratio_missing_key_uses_default` — dict without key → no raise,
  output contains `0.000`

**R2 tests** (4 tests, use `_exec_main_block`):
- `test_main_min_test_pnl_folds_included_in_output` — standard mock (all folds ≥ 3 trades)
  → `min_test_pnl_folds_included:` line appears in output
- `test_main_fold_exclusion_skips_sparse_fold` — mock N=`train.WALK_FORWARD_WINDOWS` folds;
  fold 1 test: pnl=−100, trades=1 (excluded); folds 2…N: pnl=50+i, trades=5.
  Assert `min_test_pnl` = 51 (fold 2 pnl, not −100) and `min_test_pnl_folds_included: N-1`.
- `test_main_fold_exclusion_fallback_all_sparse` — all N folds < 3 trades → fallback to raw min
  (−100); assert `min_test_pnl_folds_included: N` (all included via fallback)
- `test_main_fold_exclusion_included_count_equals_qualifying_folds` — N-1 of N folds qualify;
  assert `min_test_pnl_folds_included: N-1` in output

**Important**: All `_exec_main_block` R2 tests must use `train.WALK_FORWARD_WINDOWS`
dynamically (not hardcode 3 or 7). Build mock fold data as `[...] * train.WALK_FORWARD_WINDOWS`.

For `_exec_main_block` R2 tests: mock `run_backtest` using a call counter to distinguish
train calls (even index) from test calls (odd index) and return different stats per fold.
Use `_write_trades_tsv=lambda records, annotation=None: None` to suppress file I/O.

**R16 tests** (3 tests):
- `test_train_walk_forward_windows_default_is_7` — `import train; assert train.WALK_FORWARD_WINDOWS == 7`
- `test_train_fold_test_days_default_is_40` — `import train; assert train.FOLD_TEST_DAYS == 40`
- `test_program_md_walk_forward_windows_default_is_7` — `open('program.md').read()` contains `'7'`
  alongside `WALK_FORWARD_WINDOWS` in the constants/defaults section. (Mirror of `test_program_md_fold_test_days_default_is_40` pattern from test_v4_a.py.)

---

#### Task 4.2: UPDATE tests/test_backtester.py — R13 trail, R15 stall; ADD R14 integration test

**WAVE**: 4
**AGENT_ROLE**: Test author (manage_position + run_backtest)
**PURPOSE**: Verify R13 trail tightening and R15 early stall in manage_position; verify R14
partial close in run_backtest via integration test.
**DEPENDS_ON**: Tasks 1.1, 2.4

**Required fixture**: `make_position_df(n, price, atr_spread)` already in file (lines ~23–33).
ATR ≈ 2 × atr_spread per bar (TR = high−low). `make_position_df(price=X, atr_spread=2.0)` → ATR14 ≈ 4.0.

**New tests to append** (6 tests after existing manage_position tests):

1. `test_manage_position_trail_uses_1_2_atr_not_1_5` — Build a 30-row df where first 29
   rows have `price_10am=110` and last row has `price_10am=104` (backed off so R14 doesn't
   fire). `atr_spread=2.0` → ATR≈4. `entry=100`, `stop=90`, `entry_date=dates[0]`.
   `recent_high = 110 ≥ entry+2.0×ATR=108` → trail activates. Assert result ≈ `110 − 1.2×4 = 103.2`
   (not old 104.0). R15 does not fire (cal_days=29 > 5).

2. `test_run_backtest_partial_close_fires_at_1r` — Integration test using `run_backtest()`.
   Patch `screen_day` to return a signal on day 0 and `None` thereafter. Set up a ticker df
   where `price_10am` jumps to `entry + ATR14` on day 5. Assert:
   - `len(stats['trade_records']) >= 2` (at least the partial row + final exit row)
   - At least one record has `exit_type == 'partial'`

3. `test_run_backtest_partial_close_fires_only_once` — Same setup, price stays above
   `entry + ATR14` for multiple days. Assert exactly one record with `exit_type == 'partial'`.

4. `test_manage_position_early_stall_exit_within_5_days` — `make_position_df(n=5,
   price=101.0, atr_spread=2.0)`. entry=100, stop=90, `entry_date=df.index[0]`.
   cal_days=4 ≤ 5 and 101 < entry+0.5×4=102 → R15 fires. Assert `result == 101.0`.

5. `test_manage_position_no_early_stall_after_5_days` — `make_position_df(n=10, price=101.0,
   atr_spread=2.0)`. cal_days=9 > 5 → R15 does NOT fire. Assert `result == 90.0`.

6. `test_manage_position_early_stall_not_fired_when_price_strong` — `make_position_df(n=5,
   price=103.0, atr_spread=2.0)`. ATR≈4 → threshold=102. 103 ≥ 102 → R15 does NOT fire.
   Assert `result == 90.0`.

---

### Wave 5 (Sequential, depends on Wave 4)

---

#### Task 5.1: RUN FULL TEST SUITE

**WAVE**: 5
**AGENT_ROLE**: Validator
**PURPOSE**: Verify all existing tests pass and new tests pass.
**DEPENDS_ON**: Tasks 4.1, 4.2

**Steps**:

1. Run: `uv run pytest tests/ -x --tb=short`
2. Expected result: All pre-existing tests pass + new V4-B tests pass.
   - Pre-existing baseline after V4-A: 64 passed.
   - New V4-B tests: ~18 new tests (7 R11 + 4 R2 + 3 R16 from test_v4_b.py + 6 in test_backtester.py for R13/R14/R15).
   - GOLDEN_HASH test must pass (test_harness_below_do_not_edit_is_unchanged).
3. If the GOLDEN_HASH test fails: confirm Task 3.1 was run after all immutable zone changes.
4. If a manage_position test fails: verify ATR computation. `make_position_df(atr_spread=2.0)`
   produces TR = (price+2)−(price−2) = 4.0 per bar, ATR14 ≈ 4.0.

---

## TEST COVERAGE ANALYSIS

### New code paths introduced

| Path | Test | Status |
|------|------|--------|
| R11: `_winning_pnls` / `_losing_pnls` computation | `test_run_backtest_win_loss_ratio_positive_with_mixed_trades` | ✅ Covered |
| R11: no trades → ratio=0.0 | `test_run_backtest_win_loss_ratio_zero_no_trades` | ✅ Covered |
| R11: no losers → ratio=0.0 | `test_run_backtest_win_loss_ratio_zero_when_no_losers` | ✅ Covered |
| R11: `print_results` emits line | `test_print_results_emits_win_loss_ratio_line` | ✅ Covered |
| R11: missing key uses default | `test_print_results_win_loss_ratio_missing_key_uses_default` | ✅ Covered |
| R2: fold excluded (< 3 trades) | `test_main_fold_exclusion_skips_sparse_fold` | ✅ Covered |
| R2: all folds sparse → fallback | `test_main_fold_exclusion_fallback_all_sparse` | ✅ Covered |
| R2: folds_included line in output | `test_main_min_test_pnl_folds_included_in_output` | ✅ Covered |
| R13: trail = 1.2× ATR | `test_manage_position_trail_uses_1_2_atr_not_1_5` | ✅ Covered |
| R14: partial close fires at +1.0R (first time) | `test_run_backtest_partial_close_fires_at_1r` | ✅ Covered |
| R14: partial close fires only once (`partial_taken` flag) | `test_run_backtest_partial_close_fires_only_once` | ✅ Covered |
| R14: `position['shares']` halved after partial close | `test_run_backtest_partial_close_fires_at_1r` | ✅ Covered |
| R14: `exit_type='partial'` row in trade_records | `test_run_backtest_partial_close_fires_at_1r` | ✅ Covered |
| R15: stall guard fires ≤5 days | `test_manage_position_early_stall_exit_within_5_days` | ✅ Covered |
| R15: no fire after 5 days | `test_manage_position_no_early_stall_after_5_days` | ✅ Covered |
| R15: no fire when price is strong | `test_manage_position_early_stall_not_fired_when_price_strong` | ✅ Covered |
| R16: `WALK_FORWARD_WINDOWS == 7` in code | `test_train_walk_forward_windows_default_is_7` (test_v4_b.py) | ✅ Covered |
| R16: `FOLD_TEST_DAYS == 40` in code | `test_train_fold_test_days_default_is_40` (test_v4_b.py) | ✅ Covered |
| R16: program.md documents 7 as production default | `test_program_md_walk_forward_windows_default_is_7` (test_v4_b.py) | ✅ Covered |
| GOLDEN_HASH reflects R2+R11+R14 | `test_harness_below_do_not_edit_is_unchanged` | ✅ Covered (existing test, hash updated) |

### Existing tests at risk of breakage

| Risk | Resolution |
|------|------------|
| `test_output_format_all_seven_fields_present` checks for `calmar:` and `pnl_consistency:` but not the new `win_loss_ratio:` — no conflict | No change needed |
| `test_main_outputs_min_test_pnl_line` uses a fake_backtest without `avg_win_loss_ratio` — `print_results` uses `.get()` so no KeyError | No change needed |
| Mock run_backtest dicts in test_optimization.py do not include `avg_win_loss_ratio` key — used in `__main__` via `print_results` which uses `.get()` | No change needed |
| `fold_test_pnls.append(...)` change: `__main__` tests in test_optimization.py that check `min_test_pnl` output use mocks returning `total_trades=0` (< 3) — fall-back path returns raw min, so expected value unchanged | No change needed |

### Test Automation Summary

- **Automated**: 18 new tests (100%) — `pytest` via `uv run pytest tests/ -x`
- **Manual**: 0 — all behaviors testable with in-memory fixtures
- **Gaps remaining**: None

---

## VALIDATION CRITERIA

After all tasks complete, the following must hold:

1. `python -c "import train"` — no import errors
2. `python -c "import train; s=train.run_backtest({}); assert 'avg_win_loss_ratio' in s"` — key present
3. `python -c "import io, sys, train; import contextlib; buf=io.StringIO(); \
   contextlib.redirect_stdout(buf); train.print_results({'sharpe':0.0,'total_trades':0,'win_rate':0.0,'avg_pnl_per_trade':0.0,'total_pnl':0.0,'backtest_start':'x','backtest_end':'y','avg_win_loss_ratio':1.5}); \
   assert 'win_loss_ratio:' in buf.getvalue()"` — line emitted
4. `min_test_pnl_folds_included:` appears in `__main__` output when run with mocked data
5. `manage_position()` returns 1.2× trail (not 1.5×) when trailing activates
6. `run_backtest()` trade_records contains a row with `exit_type='partial'` when price reaches entry+1×ATR (R14)
7. `manage_position()` returns `price_10am` when cal_days ≤ 5 and price < entry + 0.5 × ATR (R15)
8. `python -c "import train; assert train.WALK_FORWARD_WINDOWS == 7 and train.FOLD_TEST_DAYS == 40"` — R16 constants set
9. `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged` — passes
10. `uv run pytest tests/ -x` — full suite passes; no new regressions

---

## NOTES AND EDGE CASES

**R14 + R13 interaction**: R14 closes 50% at +1.0R in `run_backtest()`. The remaining 50%
of the position continues to be managed by `manage_position()`, where R13 (trail at 1.2×,
activates at +2.0R) may later fire. These are complementary: R14 locks in early gains, R13
manages the remaining half.

**R14 + R15 ordering**: R15 is in `manage_position()` (mutable zone) and runs on every bar.
R14 is in `run_backtest()` and runs in the position loop before calling `manage_position()`.
Both operate independently. If R14 fires on day 5, `manage_position()` is still called for
the remaining half position — R15 (cal_days ≤ 5) may also fire that same day, exiting the
remainder via stop. This is expected and safe (both conditions are checked independently).

**R10 + R15 ordering**: R10 checks bdays > 30 (long-hold low-PnL). R15 checks cal_days ≤ 5
(early stall). These are mutually exclusive by definition (≤5 vs >30 days), so ordering
doesn't matter.

**R2 tuple change**: `fold_test_pnls` changes from `list[float]` to `list[tuple[float, int]]`.
If any downstream code in `__main__` (after the min_test_pnl block) iterates over
`fold_test_pnls` expecting floats, it will break. Confirm no such code exists by searching
`train.py` for `fold_test_pnls` before Task 2.3.

**R16 test speed**: Changing `WALK_FORWARD_WINDOWS` from 3 to 7 means `__main__` tests call
the mock backtester 15 times (7×2+1) instead of 7. Since all calls are mocked, wall-clock
impact is negligible. If any test unexpectedly slows, check for un-mocked `run_backtest` calls.

**R16 + R2 interaction**: With 7 folds (vs 3), the R2 fold-exclusion guard is more meaningful —
at least 7 opportunities for a sparse fold to distort `min_test_pnl`. The fallback path (all
sparse) becomes less likely at 7 folds because sparse folds tend to cluster in specific periods.

**R11 no-losers sentinel**: When all trades are winners, `avg_win_loss_ratio = 0.0` (not
infinity). This is a sentinel meaning "undefined ratio" rather than a real measurement.
The `discard-fragile` guard (ratio < 0.5) will not trigger for this case, which is correct —
an all-winning strategy should not be flagged as fragile.

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] **R2**: `min_test_pnl` excludes test folds with fewer than 3 trades when computing the minimum across walk-forward folds.
- [ ] **R2**: When all folds have fewer than 3 trades, `min_test_pnl` falls back to the raw minimum of all folds (no crash, no silent wrong value).
- [ ] **R2**: `__main__` prints a `min_test_pnl_folds_included: N` line immediately after `min_test_pnl:` in every run.
- [ ] **R4**: `program.md` results.tsv header includes `train_avg_pnl_per_trade` and `train_win_loss_ratio` columns with definitions and an updated example row.
- [ ] **R11**: `run_backtest()` return dict contains an `avg_win_loss_ratio` key (float).
- [ ] **R11**: `avg_win_loss_ratio` equals `mean(winning_pnls) / |mean(losing_pnls)|` when both groups are non-empty; equals `0.0` otherwise.
- [ ] **R11**: `print_results()` emits a `win_loss_ratio:` line formatted to 3 decimal places.
- [ ] **R11**: `program.md` includes a `discard-fragile` rule: revert if `train_win_loss_ratio < 0.5` and `train_pnl_consistency < floor`.
- [ ] **R13**: `manage_position()` trailing stop distance is `1.2 × ATR` (not `1.5 × ATR`) when the trailing activation threshold is met.
- [ ] **R14 (full)**: `run_backtest()` closes 50% of a position at `price_10am` when `price_10am >= entry_price + 1.0 × ATR` for the first time; halves `position['shares']`; records a partial trade exit row in `trade_records`; sets `position['partial_taken'] = True` to prevent re-firing.
- [ ] **R15**: `manage_position()` forces exit (returns `max(current_stop, price_10am)`) when `cal_days_held ≤ 5` and `price_10am < entry_price + 0.5 × ATR`.
- [ ] **R16**: `train.WALK_FORWARD_WINDOWS == 7` in code (was 3).
- [ ] **R16**: `train.FOLD_TEST_DAYS == 40` in code (was 20).
- [ ] **R16**: `program.md` documents 7 as the production default for `WALK_FORWARD_WINDOWS` with the 7-fold schedule table.

### Error Handling

- [ ] `print_results()` does not raise `KeyError` when `avg_win_loss_ratio` is absent from the stats dict (uses `.get()` with default `0.0`).
- [ ] R14 partial close does not fire when `atr14 == 0` or `atr14` key is missing from the position dict.
- [ ] R2 fold guard does not crash when `fold_test_pnls` is empty.
- [ ] R16 constant change does not require a GOLDEN_HASH update (verified: both constants are above the `# ── DO NOT EDIT BELOW THIS LINE` boundary).

### Integration / E2E

- [ ] `uv run pytest tests/ -x` passes with zero failures after all changes (including GOLDEN_HASH update).
- [ ] `test_harness_below_do_not_edit_is_unchanged` passes with the updated GOLDEN_HASH covering R2, R11, and R14.
- [ ] No pre-existing V4-A tests regress (baseline: 64 passing after V4-A).

### Validation

- [ ] `python -c "import train"` — no import errors — verified by: `python -c "import train; print('ok')"`
- [ ] `avg_win_loss_ratio` key present in `run_backtest({})` return — verified by: `python -c "import train; s=train.run_backtest({}); assert 'avg_win_loss_ratio' in s"`
- [ ] Full test suite passes — verified by: `uv run pytest tests/ -x --tb=short`

### Out of Scope

- R7 (sector concentration guard) — not implemented; marked optional/low-priority in PRD
- Changes to `manage_position()` signature or return type
- Any changes to `results.tsv` column order for columns other than the two new ones (8 and 9)
- Partial close logic for short positions (only long positions exist in current strategy)
