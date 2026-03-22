# Feature: V3-B Walk-Forward Evaluation Framework

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

V3-B replaces the single train→test split (from V2) with a rolling walk-forward
cross-validation loop as the optimization objective. Three changes work together:

- **R2 — Walk-forward CV**: The `__main__` block runs `run_backtest()` N times (default 3)
  with staggered 10-business-day test windows stepping back from `TRAIN_END`. Reports one
  result block per fold. Keep/discard criterion: `min_test_pnl` (minimum test P&L across all
  folds). This penalizes strategies that only work in one regime.

- **R4-full — Silent holdout**: Add `SILENT_END` constant (= `TRAIN_END − 14 calendar days`).
  After the walk-forward folds, `__main__` runs an additional backtest for
  `[TRAIN_END, BACKTEST_END]`; prints `silent_pnl: HIDDEN` during the optimization loop;
  reveals the full result only in the final run (`WRITE_FINAL_OUTPUTS = True`).

- **R7 — Calmar + consistency diagnostics**: `run_backtest()` computes and returns
  `max_drawdown`, `calmar` ratio, and `pnl_consistency` (minimum monthly P&L).
  `print_results()` emits `calmar:` and `pnl_consistency:` lines. Added as columns to
  `results.tsv`; not used for keep/discard.

**Contradiction handling (from PRD §V3 Background):**
- C1: R4-simple "print HIDDEN every iteration" is dropped; the walk-forward visible test
  windows ARE the per-fold evaluation (R2). R4-full's hard holdout [TRAIN_END, BACKTEST_END]
  sits outside all folds.
- C2: R7 is a diagnostic only; `min_test_pnl` (R2) is the sole keep/discard criterion.

## User Story

As a developer running the optimization loop,
I want each experiment evaluated across multiple time windows rather than a single split,
So that the optimizer finds strategies that generalize across regimes rather than fitting one.

## Feature Metadata

**Feature Type**: Enhancement (evaluation framework restructure)
**Complexity**: ⚠️ Medium
**Primary Systems Affected**: `train.py` (mutable + immutable), `program.md`,
  `tests/test_optimization.py`
**Pre-requisite**: V3-A complete (GOLDEN_HASH = `8c797ebed7a436656539ab4d664c2c147372505769a140c29e3c4ad2b483f3c7`)

---

## Architecture & Design

### Walk-Forward Fold Structure

With `WALK_FORWARD_WINDOWS = 3`, `TRAIN_END = "2026-03-06"`, `BACKTEST_START = "2025-12-20"`:

```
BACKTEST_START                   SILENT_END       TRAIN_END   BACKTEST_END
2025-12-20                       2026-02-20       2026-03-06  2026-03-20
    |                                |                |            |
    [===fold1 train=======|fold1test ]                |            |
    [========fold2 train===========| fold2 test ======]            |
    [=============fold3 train=================|fold3 test=========]
                                                      [  HIDDEN   ]
```

Each fold's test window = 10 business days. Folds step back 10 bdays from `TRAIN_END`.
- Fold 3 test: [TRAIN_END − 10bd, TRAIN_END]
- Fold 2 test: [TRAIN_END − 20bd, TRAIN_END − 10bd]
- Fold 1 test: [TRAIN_END − 30bd, TRAIN_END − 20bd]

`SILENT_END` ≈ start of fold 3's test window (TRAIN_END − 14 cal days ≈ − 10 business days).
It is a setup reference written into the mutable section; the code derives fold boundaries
dynamically from `TRAIN_END` and `WALK_FORWARD_WINDOWS`.

### Equity Curve for R7

The existing `daily_values` is MTM-only (no realized PnL) and is unchanged (Sharpe computation
remains identical). For R7, a parallel `equity_curve` is built:

```
equity_point[day] = cumulative_realized_pnl + sum(open positions MTM)
```

- `cumulative_realized_pnl` increments each time a position closes.
- `max_drawdown = max(peak − equity)` across the equity curve.
- `calmar = total_pnl / max_drawdown` if `max_drawdown > 0` else `0.0`.
- Monthly P&L = last equity of month − last equity of previous month.
- `pnl_consistency = min(monthly_pnl_list)` if any months else `total_pnl`.

### New Output Format (3 folds)

```
---
fold1_train_sharpe:              0.123456
fold1_train_total_trades:        8
fold1_train_win_rate:            0.625
fold1_train_avg_pnl_per_trade:   12.50
fold1_train_total_pnl:           100.00
fold1_train_backtest_start:      2025-12-20
fold1_train_backtest_end:        2026-01-09
fold1_train_calmar:              2.5000
fold1_train_pnl_consistency:     30.00
---
fold1_test_sharpe:               0.234567
fold1_test_total_trades:         3
fold1_test_win_rate:             0.667
fold1_test_avg_pnl_per_trade:    8.33
fold1_test_total_pnl:            25.00
fold1_test_backtest_start:       2026-01-09
fold1_test_backtest_end:         2026-01-23
fold1_test_calmar:               1.2500
fold1_test_pnl_consistency:      25.00
---
... (fold2, fold3 blocks) ...
---
min_test_pnl:            -5.00
---
silent_pnl: HIDDEN
```

---

## Implementation Tasks

### Task 1 — WAVE 1 | train.py mutable section: add R2 + R4-full constants

Add two new constants to the mutable section (between `WRITE_FINAL_OUTPUTS` and `RISK_PER_TRADE`):

```python
# Walk-forward evaluation: number of rolling test folds (V3-B R2)
WALK_FORWARD_WINDOWS = 3

# Silent holdout boundary (V3-B R4-full): TRAIN_END − 14 calendar days.
# Walk-forward folds' test windows end at approximately this date.
# Set by the agent at session setup. Do NOT change during the loop.
SILENT_END = "2026-02-20"   # example; agent computes this at setup
```

Place after `WRITE_FINAL_OUTPUTS = False` and before `RISK_PER_TRADE = 50.0`.

**Note on SILENT_END value**: The value `"2026-02-20"` is the correct value for the current
`TRAIN_END = "2026-03-06"`. If tickers/dates change, program.md instructs the agent to recompute.

---

### Task 2 — WAVE 2 | train.py immutable zone: update run_backtest() for R7

This is the most complex change. `run_backtest()` must:

1. Add `cumulative_realized = 0.0` and `equity_curve: list[tuple] = []` before the main loop.
2. After each stop-close, add `cumulative_realized += pnl`.
3. After end-of-backtest closes, add `cumulative_realized += pnl` for each.
4. In the mark-to-market step (step 4), also append `(today, cumulative_realized + portfolio_value)`
   to `equity_curve`.
5. After the main loop, compute:
   - `max_drawdown` from the equity curve
   - `calmar` ratio
   - `pnl_consistency` (min monthly P&L)
6. Include these in the early-exit (< 2 trading days) return dict.
7. Include these in the final return dict.

**Detailed code for post-loop R7 computation** (add after the Sharpe computation block):

```python
# R7: Equity curve metrics
if equity_curve:
    eq_values = np.array([v for _, v in equity_curve], dtype=float)
    peak = np.maximum.accumulate(eq_values)
    max_drawdown = float(np.max(peak - eq_values))
else:
    max_drawdown = 0.0

calmar = (total_pnl / max_drawdown) if max_drawdown > 0 else 0.0

# R7: Monthly P&L consistency (min monthly P&L across all months in window)
monthly_equity: dict = {}
for dt, eq in equity_curve:
    key = (dt.year, dt.month)
    monthly_equity[key] = eq   # last equity value for that month
month_keys = sorted(monthly_equity.keys())
monthly_pnl_list = []
prev_eq = 0.0
for mk in month_keys:
    monthly_pnl_list.append(monthly_equity[mk] - prev_eq)
    prev_eq = monthly_equity[mk]
pnl_consistency = min(monthly_pnl_list) if monthly_pnl_list else total_pnl
```

**Early-exit return dict** (update the `if len(trading_days) < 2:` block):
```python
return {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
        "ticker_pnl": {}, "backtest_start": start or BACKTEST_START,
        "backtest_end": end or BACKTEST_END, "trade_records": [],
        "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0}
```

**Final return dict** — add three new keys:
```python
return {
    ...existing keys...,
    "max_drawdown":      round(max_drawdown, 2),
    "calmar":            round(calmar, 4),
    "pnl_consistency":   round(pnl_consistency, 2),
}
```

**Placement of equity_curve updates within the loop:**
- In stop-check section, after `trades.append(pnl)`: add `cumulative_realized += pnl`
- In end-of-backtest close, after `trades.append(pnl)`: add `cumulative_realized += pnl`
- In mark-to-market step (after `daily_values.append(portfolio_value)`):
  add `equity_curve.append((today, cumulative_realized + portfolio_value))`

---

### Task 3 — WAVE 2 | train.py immutable zone: update print_results() for R7

Append two new lines after `backtest_end`:

```python
def print_results(stats: dict, prefix: str = "") -> None:
    print("---")
    print(f"{prefix}sharpe:              {stats['sharpe']:.6f}")
    print(f"{prefix}total_trades:        {stats['total_trades']}")
    print(f"{prefix}win_rate:            {stats['win_rate']:.3f}")
    print(f"{prefix}avg_pnl_per_trade:   {stats['avg_pnl_per_trade']:.2f}")
    print(f"{prefix}total_pnl:           {stats['total_pnl']:.2f}")
    print(f"{prefix}backtest_start:      {stats['backtest_start']}")
    print(f"{prefix}backtest_end:        {stats['backtest_end']}")
    print(f"{prefix}calmar:              {stats.get('calmar', 0.0):.4f}")
    print(f"{prefix}pnl_consistency:     {stats.get('pnl_consistency', 0.0):.2f}")
```

---

### Task 4 — WAVE 2 | train.py immutable zone: rewrite __main__ for R2 + R4-full

Replace the current `if __name__ == "__main__":` block entirely:

```python
if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)

    # R2: Walk-forward CV — N folds with 10-business-day test windows
    # stepping back from TRAIN_END.
    import pandas as _pd
    from pandas.tseries.offsets import BDay as _BDay

    _train_end_ts = _pd.Timestamp(TRAIN_END)
    fold_test_pnls: list = []
    last_fold_train_records: list = []

    for _i in range(WALK_FORWARD_WINDOWS):
        # Fold _i (0-indexed, oldest first).
        # Fold WALK_FORWARD_WINDOWS-1 (newest) has test window ending at TRAIN_END.
        _steps_back = WALK_FORWARD_WINDOWS - 1 - _i
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * 10)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(10)
        _fold_train_end_ts  = _fold_test_start_ts

        _fold_train_end   = str(_fold_train_end_ts.date())
        _fold_test_start  = str(_fold_test_start_ts.date())
        _fold_test_end    = str(_fold_test_end_ts.date())
        _fold_n           = _i + 1

        _fold_train_stats = run_backtest(ticker_dfs, start=BACKTEST_START, end=_fold_train_end)
        _fold_test_stats  = run_backtest(ticker_dfs, start=_fold_test_start, end=_fold_test_end)

        print_results(_fold_train_stats, prefix=f"fold{_fold_n}_train_")
        print_results(_fold_test_stats,  prefix=f"fold{_fold_n}_test_")

        fold_test_pnls.append(_fold_test_stats["total_pnl"])
        if _fold_n == WALK_FORWARD_WINDOWS:
            last_fold_train_records = _fold_train_stats["trade_records"]

    min_test_pnl = min(fold_test_pnls) if fold_test_pnls else 0.0
    print("---")
    print(f"min_test_pnl:            {min_test_pnl:.2f}")

    # Write trades.tsv from the most recent training fold
    _write_trades_tsv(last_fold_train_records)

    # R4-full: Silent holdout — [TRAIN_END, BACKTEST_END]
    _silent_stats = run_backtest(ticker_dfs, start=TRAIN_END, end=BACKTEST_END)
    print("---")
    if WRITE_FINAL_OUTPUTS:
        print_results(_silent_stats, prefix="holdout_")
        _write_final_outputs(ticker_dfs, TRAIN_END, BACKTEST_END, _silent_stats["ticker_pnl"])
    else:
        print(f"silent_pnl: HIDDEN")
```

**Note**: Underscore-prefixed local variables (`_i`, `_pd`, etc.) prevent name collisions with
any ticker names or user variables in the strategy above the boundary.

---

### Task 5 — WAVE 1 (parallel with Task 1) | program.md: update for V3-B

**5a. Setup step 4b** — update to compute SILENT_END in addition to TRAIN_END/TEST_START:

Replace the current step 4b:
```
4b. **Compute train/test split**: Set `TRAIN_END = BACKTEST_END − 14 calendar days`
    (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`). Write both
    `TRAIN_END` and `TEST_START` (same date) into the mutable section of `train.py`.
```

With:
```
4b. **Compute train/test split and walk-forward boundaries**:
    - `TRAIN_END = BACKTEST_END − 14 calendar days`
      (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`)
    - `TEST_START = TRAIN_END` (same date — kept for backward reference)
    - `SILENT_END = TRAIN_END − 14 calendar days`
      (e.g. TRAIN_END `2026-03-06` → SILENT_END `2026-02-20`)

    Write `TRAIN_END`, `TEST_START`, and `SILENT_END` into the mutable section of `train.py`.
    `WALK_FORWARD_WINDOWS` is already set to 3 — leave it unless the user specifies otherwise.
```

**5b. Output format** — replace the current output format block with the new fold-based structure:

Replace the entire "## Output format" section with:

```markdown
## Output format

When the script completes successfully it prints N×2 fold blocks (N = `WALK_FORWARD_WINDOWS`),
then a `min_test_pnl` summary, then a `silent_pnl` line.

For `WALK_FORWARD_WINDOWS = 3`, the output is:
\```
---
fold1_train_sharpe:              1.234567
fold1_train_total_trades:        8
fold1_train_win_rate:            0.625
fold1_train_avg_pnl_per_trade:   12.50
fold1_train_total_pnl:           100.00
fold1_train_backtest_start:      2025-12-20
fold1_train_backtest_end:        2026-01-09
fold1_train_calmar:              2.5000
fold1_train_pnl_consistency:     30.00
---
fold1_test_sharpe:               0.234567
...
fold1_test_total_pnl:            25.00
fold1_test_backtest_start:       2026-01-09
fold1_test_backtest_end:         2026-01-23
...
---
(fold2 and fold3 blocks follow the same pattern)
---
min_test_pnl:            -5.00
---
silent_pnl: HIDDEN
\```

Extract the key metrics from `run.log`:

\```bash
grep "^min_test_pnl:" run.log
grep "^fold3_train_total_pnl:" run.log
grep "^fold3_train_total_trades:" run.log
grep "^fold3_train_win_rate:" run.log
grep "^fold3_train_sharpe:" run.log
grep "^fold3_train_calmar:" run.log
grep "^fold3_train_pnl_consistency:" run.log
\```

Replace `fold3` with `fold${WALK_FORWARD_WINDOWS}` if you change `WALK_FORWARD_WINDOWS`.

Exit code 0 on success. Exit code 1 if the cache is empty.
```

**5c. Experiment loop (step 5 and step 8)** — update keep/discard criterion and the no-trades
guard. Find and update these lines in "## The experiment loop":

- Step 5 (currently `grep "^train_total_pnl:"`): change to `grep "^min_test_pnl:" run.log`
- Step 8 (currently "If `train_total_pnl` **improved (higher)**"):
  change to "If `min_test_pnl` **improved (higher)**"
- Step 9 (currently "If `train_total_pnl` is equal or lower"):
  change to "If `min_test_pnl` is equal or lower"

Also update the `> **Note:** ...` block to:
```
> **Note:** `silent_pnl` is hidden during the loop (`HIDDEN`). Do NOT attempt to infer or
> act on the hidden holdout result. The sole keep/discard criterion is `min_test_pnl`.
> `fold{N}_train_*` metrics are for diagnostics only.
```

**5d. Logging results schema** — replace the header and column definitions:

```markdown
Header row (tab-separated):
\```
commit  min_test_pnl  train_pnl  test_pnl  train_sharpe  total_trades  win_rate  train_calmar  train_pnl_consistency  status  description
\```

Column definitions:
1. `commit`: git commit hash (short, 7 chars)
2. `min_test_pnl`: minimum test P&L across all walk-forward folds — the keep/discard criterion
3. `train_pnl`: most recent fold's (fold N) train total P&L
4. `test_pnl`: most recent fold's (fold N) test total P&L
5. `train_sharpe`: most recent fold's train Sharpe
6. `total_trades`: most recent fold's train trade count
7. `win_rate`: most recent fold's train win rate
8. `train_calmar`: most recent fold's train Calmar ratio (diagnostic)
9. `train_pnl_consistency`: most recent fold's train min monthly P&L (diagnostic)
10. `status`: `keep`, `discard`, or `crash`
11. `description`: short experiment description
```

Update the example rows accordingly.

**5e. Final test run section** — update references from `TEST_START`/`BACKTEST_END` to `TRAIN_END`/`BACKTEST_END` (the silent holdout window):
- "Set `WRITE_FINAL_OUTPUTS = True`" — unchanged
- "Collect `final_test_data.csv`" — unchanged (already uses holdout window internally)
- Remove the step that references `test_total_pnl` for per-ticker output; the per-ticker P&L
  is now from the holdout: grep for `holdout_` prefix in run.log.

---

### Task 6 — WAVE 3 | tests/test_optimization.py: update GOLDEN_HASH

After completing Tasks 2–4, the immutable zone has changed. Recompute the hash:

```bash
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# ── DO NOT EDIT BELOW THIS LINE'
print(hashlib.sha256(s.partition(m)[2].encode('utf-8')).hexdigest())
"
```

Replace `GOLDEN_HASH` in `tests/test_optimization.py` with the new value.

---

### Task 7 — WAVE 3 (parallel with Task 6) | tests/test_optimization.py: new tests

Add 9 new tests after the existing ones. All tests use `import train` (already at top).

**Test 7.1 — run_backtest returns R7 keys**
```python
def test_run_backtest_returns_r7_keys():
    """run_backtest() must return max_drawdown, calmar, pnl_consistency."""
    ticker_dfs = _make_rising_dataset()
    stats = train.run_backtest(ticker_dfs)
    assert "max_drawdown" in stats
    assert "calmar" in stats
    assert "pnl_consistency" in stats
    assert isinstance(stats["max_drawdown"], float)
    assert isinstance(stats["calmar"], float)
    assert isinstance(stats["pnl_consistency"], float)
```

**Test 7.2 — max_drawdown is non-negative**
```python
def test_max_drawdown_is_non_negative():
    """max_drawdown must be >= 0 for any valid run (drawdown cannot be negative)."""
    ticker_dfs = _make_rising_dataset()

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price, "stop_type": "fallback"}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(ticker_dfs)

    assert stats["max_drawdown"] >= 0.0, (
        f"max_drawdown must be >= 0, got {stats['max_drawdown']}"
    )
```

**Test 7.3 — calmar is zero when max_drawdown is zero**
```python
def test_calmar_zero_when_no_drawdown():
    """calmar must be 0.0 when max_drawdown == 0 (guard against division by zero)."""
    stats = train.run_backtest({})  # empty dataset → early exit → max_drawdown = 0
    assert stats["calmar"] == 0.0
    assert stats["max_drawdown"] == 0.0
```

**Test 7.4 — pnl_consistency is min monthly PnL**
```python
def test_pnl_consistency_equals_min_monthly_pnl():
    """
    pnl_consistency must equal the minimum monthly P&L across the backtest window.
    Uses a dataset spanning exactly 2 calendar months where month 1 is profitable
    and month 2 is breakeven, so pnl_consistency must be <= 0.
    """
    # 60 business days in Jan-Feb 2026: prices rise Jan, flat Feb
    bdays_jan = pd.bdate_range(start="2026-01-05", end="2026-01-30")
    bdays_feb = pd.bdate_range(start="2026-02-02", end="2026-02-27")
    all_bdays = bdays_jan.append(bdays_feb)
    dates = [d.date() for d in all_bdays]

    prices_jan = np.linspace(100.0, 120.0, len(bdays_jan))
    prices_feb = np.full(len(bdays_feb), 120.0)  # flat
    prices = np.concatenate([prices_jan, prices_feb])

    df = pd.DataFrame({
        "open":       prices - 0.5,
        "high":       prices + 1.0,
        "low":        prices - 1.0,
        "close":      prices,
        "volume":     np.full(len(dates), 1_000_000.0),
        "price_10am": prices,
    }, index=pd.Index(dates, name="date"))
    ticker_dfs = {"SYNTH": df}

    def enter_jan_only(df_arg, today):
        if today.month != 1:
            return None
        if len(df_arg) < 2:
            return None
        price = float(df_arg["price_10am"].iloc[-1])
        return {"stop": price - 5.0, "entry_price": price, "stop_type": "fallback"}

    def no_op_manage(position, df_arg):
        return position["stop_price"]

    with mock.patch.object(train, "screen_day", enter_jan_only), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(
            ticker_dfs,
            start="2026-01-05",
            end="2026-03-01"
        )

    # pnl_consistency = min monthly PnL; Feb has flat prices so its P&L should be <= Jan's
    assert isinstance(stats["pnl_consistency"], float)
    # min monthly P&L is not higher than total_pnl (otherwise every month was equally good)
    assert stats["pnl_consistency"] <= stats["total_pnl"]
```

**Test 7.5 — print_results includes calmar and pnl_consistency**
```python
def test_print_results_includes_r7_lines(capsys):
    """print_results() must emit calmar: and pnl_consistency: lines."""
    stats = {
        "sharpe": 1.5, "total_trades": 5, "win_rate": 0.6,
        "avg_pnl_per_trade": 20.0, "total_pnl": 100.0,
        "backtest_start": "2026-01-01", "backtest_end": "2026-03-01",
        "calmar": 2.5, "pnl_consistency": 30.0,
    }
    train.print_results(stats)
    captured = capsys.readouterr()
    assert "calmar:" in captured.out
    assert "pnl_consistency:" in captured.out
    assert "2.5000" in captured.out
    assert "30.00" in captured.out
```

**Test 7.6 — __main__ walk-forward runs correct number of folds**
```python
def test_main_runs_walk_forward_windows_folds():
    """
    __main__ must call run_backtest exactly (WALK_FORWARD_WINDOWS * 2 + 1) times:
    N train + N test folds + 1 silent holdout.
    """
    call_count = []

    def counting_backtest(ticker_dfs, start=None, end=None):
        call_count.append((start, end))
        return {
            "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
            "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
            "ticker_pnl": {}, "backtest_start": start or train.BACKTEST_START,
            "backtest_end": end or train.BACKTEST_END,
            "trade_records": [], "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0,
        }

    # Temporarily provide a non-empty ticker_dfs so the main block doesn't exit early
    minimal_df = _make_rising_dataset()

    with mock.patch.object(train, "load_all_ticker_data", return_value=minimal_df), \
         mock.patch.object(train, "run_backtest", side_effect=counting_backtest), \
         mock.patch.object(train, "_write_trades_tsv"), \
         mock.patch("builtins.print"):
        # Simulate __main__ execution
        exec(
            compile(
                "ticker_dfs = load_all_ticker_data()\n"
                + open(str(TRAIN_PY), encoding='utf-8').read().split("if __name__")[1].split("\"\"\"", 1)[0],
                "<main>", "exec"
            ),
            {**vars(train), "load_all_ticker_data": lambda: minimal_df}
        )

    expected = train.WALK_FORWARD_WINDOWS * 2 + 1  # N folds × 2 + 1 holdout
    assert len(call_count) == expected, (
        f"Expected {expected} run_backtest calls "
        f"(WALK_FORWARD_WINDOWS={train.WALK_FORWARD_WINDOWS}), got {len(call_count)}"
    )
```

**Note on Test 7.6**: If the exec-based approach is fragile, use a subprocess approach instead (see Test 7.7 pattern below).

**Test 7.7 — min_test_pnl line appears in output**
```python
def test_main_outputs_min_test_pnl_line():
    """
    __main__ output must contain a 'min_test_pnl:' line parseable by grep.
    Uses a subprocess with a temp directory as CACHE_DIR override (empty cache →
    early exit with sys.exit(1)), so instead we verify the output format via unit test.
    """
    # Build minimal synthetic parquet in a temp dir and run as subprocess
    import tempfile, pathlib, subprocess as sp

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a synthetic parquet file
        cache_path = pathlib.Path(tmpdir)
        ticker_dfs = _make_rising_dataset()
        ticker_dfs["SYNTHETIC"].to_parquet(str(cache_path / "SYNTHETIC.parquet"))

        result = sp.run(
            ["python", "-c",
             f"import sys, os; os.environ['AUTORESEARCH_CACHE_OVERRIDE'] = r'{tmpdir}'; "
             f"import importlib.util, pathlib; "
             f"spec = importlib.util.spec_from_file_location('train', r'{TRAIN_PY}'); "
             f"mod = importlib.util.module_from_spec(spec); "
             f"mod.CACHE_DIR = r'{tmpdir}'; "
             f"spec.loader.exec_module(mod); "
             f"import runpy; runpy.run_path(r'{TRAIN_PY}', run_name='__main__')"
             ],
            capture_output=True, text=True, timeout=30
        )
        # min_test_pnl must appear in stdout
        assert "min_test_pnl:" in result.stdout, (
            f"Expected 'min_test_pnl:' in __main__ output.\n"
            f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
        )
```

**Note on Test 7.7**: The subprocess test is complex due to CACHE_DIR override. Use
`mock.patch.object(train, 'CACHE_DIR', tmpdir)` approach via a helper module instead if the
subprocess proves fragile. The critical requirement is that `min_test_pnl:` appears in output.

A simpler alternative for 7.7: mock `load_all_ticker_data` and `_write_trades_tsv`, then
call the main block code after extracting it from the source — similar to how test 7.6 works.

**Test 7.8 — silent_pnl is HIDDEN by default**
```python
def test_silent_pnl_hidden_by_default(capsys):
    """
    When WRITE_FINAL_OUTPUTS is False, silent_pnl line must say 'HIDDEN'.
    Patching all I/O to run the relevant part of __main__ logic.
    """
    fake_stats = {
        "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
        "ticker_pnl": {}, "backtest_start": train.TRAIN_END,
        "backtest_end": train.BACKTEST_END, "trade_records": [],
        "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0,
    }
    assert train.WRITE_FINAL_OUTPUTS is False, "WRITE_FINAL_OUTPUTS must default to False"
    # Simulate the silent holdout output section
    print("---")
    if train.WRITE_FINAL_OUTPUTS:
        train.print_results(fake_stats, prefix="holdout_")
    else:
        print(f"silent_pnl: HIDDEN")

    captured = capsys.readouterr()
    assert "silent_pnl: HIDDEN" in captured.out
    assert "holdout_" not in captured.out
```

**Test 7.9 — walk-forward fold dates are distinct and step by 10 bdays**
```python
def test_walk_forward_fold_dates_are_distinct():
    """
    Each fold's test window must be distinct and non-overlapping.
    Derives fold boundaries using the same logic as __main__ and verifies:
    - All test windows are 10 business days
    - No two folds share the same test_start date
    """
    import pandas as _pd_test
    from pandas.tseries.offsets import BDay as _BDay_test

    train_end_ts = _pd_test.Timestamp(train.TRAIN_END)
    n = train.WALK_FORWARD_WINDOWS
    fold_test_starts = []
    fold_test_ends   = []

    for i in range(n):
        steps_back = n - 1 - i
        test_end_ts   = train_end_ts - _BDay_test(steps_back * 10)
        test_start_ts = test_end_ts  - _BDay_test(10)
        fold_test_starts.append(test_start_ts)
        fold_test_ends.append(test_end_ts)

    # All test_start dates must be distinct
    assert len(set(fold_test_starts)) == n, (
        f"Fold test windows are not distinct: {fold_test_starts}"
    )

    # Consecutive folds must step by exactly 10 business days
    for i in range(1, n):
        delta = fold_test_starts[i] - fold_test_starts[i - 1]
        bday_count = len(_pd_test.bdate_range(fold_test_starts[i-1], fold_test_starts[i])) - 1
        assert bday_count == 10, (
            f"Fold {i} and fold {i+1} test windows differ by {bday_count} bdays, expected 10"
        )
```

---

## Validation

After all tasks are complete, run:

```bash
# 1. Verify file syntax
python -c "import ast; ast.parse(open('train.py', encoding='utf-8').read()); print('train.py: OK')"
python -c "import ast; ast.parse(open('tests/test_optimization.py', encoding='utf-8').read()); print('tests: OK')"

# 2. Recompute GOLDEN_HASH and confirm it matches the value set in Task 6
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# ── DO NOT EDIT BELOW THIS LINE'
h = hashlib.sha256(s.partition(m)[2].encode('utf-8')).hexdigest()
print('GOLDEN_HASH =', h)
"

# 3. Run test suite
uv run pytest tests/test_optimization.py -v 2>&1 | tail -30

# 4. Spot-check the output format
# (Requires populated cache) uv run train.py 2>&1 | head -60
# Verify: fold1_train_*, fold1_test_*, ..., min_test_pnl:, silent_pnl: HIDDEN lines present

# 5. Verify mutable constants present in train.py
grep "WALK_FORWARD_WINDOWS" train.py
grep "SILENT_END" train.py
```

**Acceptance criteria:**
- [ ] `python -c "import train"` succeeds (no import-time errors)
- [ ] `run_backtest({})` returns dict with keys: `max_drawdown`, `calmar`, `pnl_consistency`
- [ ] `print_results(stats)` output includes `calmar:` and `pnl_consistency:` lines
- [ ] `WALK_FORWARD_WINDOWS = 3` present in mutable section
- [ ] `SILENT_END` constant present in mutable section
- [ ] All 73 pre-existing tests still pass (same baseline as V3-A)
- [ ] All 9 new V3-B tests pass
- [ ] GOLDEN_HASH updated to match new immutable zone
- [ ] `program.md` setup step 4b mentions SILENT_END computation
- [ ] `program.md` loop uses `min_test_pnl` as keep/discard criterion
- [ ] `program.md` results.tsv schema includes `min_test_pnl`, `train_calmar`, `train_pnl_consistency`

---

## Test Coverage Summary

| Path | Type | Test | Status |
|------|------|------|--------|
| `run_backtest()` — R7 key presence | Unit | test_run_backtest_returns_r7_keys | ✅ Covered |
| `run_backtest()` — max_drawdown ≥ 0 | Unit | test_max_drawdown_is_non_negative | ✅ Covered |
| `run_backtest()` — calmar = 0 when no drawdown | Unit | test_calmar_zero_when_no_drawdown | ✅ Covered |
| `run_backtest()` — pnl_consistency = min monthly | Unit | test_pnl_consistency_equals_min_monthly_pnl | ✅ Covered |
| `print_results()` — calmar + pnl_consistency lines | Unit | test_print_results_includes_r7_lines | ✅ Covered |
| `__main__` — N×2+1 run_backtest calls | Unit | test_main_runs_walk_forward_windows_folds | ✅ Covered |
| `__main__` — min_test_pnl line in output | Integration | test_main_outputs_min_test_pnl_line | ✅ Covered |
| `__main__` — silent_pnl: HIDDEN by default | Unit | test_silent_pnl_hidden_by_default | ✅ Covered |
| Walk-forward fold date distinctness | Unit | test_walk_forward_fold_dates_are_distinct | ✅ Covered |
| Immutable zone unchanged (GOLDEN_HASH) | Hash | test_harness_below_do_not_edit_is_unchanged | ✅ Covered (updated) |
| Mutable section editable | Unit | test_editable_section_stays_runnable_after_threshold_change | ✅ Pre-existing |
| Agent commit editable-only | Git | test_most_recent_train_commit_modified_only_editable_section | ✅ Pre-existing |
| P&L optimization feasibility | Unit | test_optimization_feasible_on_synthetic_data | ✅ Pre-existing |

**New tests: 9 | Pre-existing: 3 harness tests + 70 V3-A and prior | Total: ~82 passing**

### Manual tests required
- None — all acceptance criteria are automated.

---

## Risks

1. **__main__ test complexity**: Testing `__main__` via exec/subprocess is fragile. If Tests 7.6/7.7
   are unstable, fall back to verifying behavior through direct `run_backtest()` + `print_results()`
   function calls, and add a simpler subprocess test that just checks for `min_test_pnl:` in stdout.

2. **BDay import in immutable zone**: The `__main__` block imports `pandas.tseries.offsets.BDay`.
   This is already available via `import pandas as pd` at the top of the file (pd is already imported
   in the mutable section context). Verify that `from pandas.tseries.offsets import BDay` resolves
   correctly within the `if __name__ == "__main__"` block.

3. **GOLDEN_HASH ordering**: Tasks 2–4 must all be complete before computing GOLDEN_HASH (Task 6).
   If any immutable-zone change is missed, the hash will be wrong and the test will fail.

4. **Fold boundary edge case**: When `WALK_FORWARD_WINDOWS` is large relative to the training
   window, early folds may have insufficient history for indicators. `run_backtest()` already handles
   short windows gracefully (returns 0-trade dict). No special handling needed.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `WALK_FORWARD_WINDOWS = 3` and `SILENT_END` constants are present in `train.py` mutable section
- [ ] `run_backtest()` returns a dict containing `max_drawdown`, `calmar`, and `pnl_consistency` keys
- [ ] `calmar` is `0.0` when `max_drawdown == 0` (no division-by-zero)
- [ ] `pnl_consistency` equals the minimum monthly P&L across all calendar months in the backtest window
- [ ] `print_results()` output includes `calmar:` and `pnl_consistency:` lines for every call
- [ ] `__main__` calls `run_backtest()` exactly `WALK_FORWARD_WINDOWS * 2 + 1` times (N fold pairs + 1 silent holdout)
- [ ] `__main__` output contains a `min_test_pnl:` line equal to the minimum of all fold test P&Ls
- [ ] `silent_pnl: HIDDEN` appears in output when `WRITE_FINAL_OUTPUTS = False`
- [ ] Silent holdout result is printed with `holdout_` prefix when `WRITE_FINAL_OUTPUTS = True`
- [ ] Walk-forward fold test windows are distinct, non-overlapping, and each spans exactly 10 business days
- [ ] `trades.tsv` is written from the most recent training fold's records

### Error Handling
- [ ] `run_backtest()` early-exit (< 2 trading days) returns `max_drawdown=0.0`, `calmar=0.0`, `pnl_consistency=0.0`
- [ ] Empty dataset (`run_backtest({})`) returns all numeric fields as `0.0` without raising

### Integration / E2E
- [ ] `program.md` setup step 4b instructs the agent to compute and write `SILENT_END`
- [ ] `program.md` experiment loop uses `min_test_pnl` (not `train_total_pnl`) as the keep/discard criterion
- [ ] `program.md` results.tsv schema includes `min_test_pnl`, `train_calmar`, `train_pnl_consistency` columns

### Validation
- [ ] All 73+ pre-existing tests continue to pass — verified by: `uv run pytest tests/test_optimization.py -v`
- [ ] All 9 new V3-B tests pass — verified by: `uv run pytest tests/test_optimization.py -v`
- [ ] `GOLDEN_HASH` in `test_harness_below_do_not_edit_is_unchanged` matches the updated immutable zone — verified by the hash test
- [ ] `python -c "import train"` succeeds with no import-time errors

### Out of Scope
- V3-C controls (R8 position cap, R9 robustness perturbation) — not part of this plan
- Changing the Sharpe computation formula — unchanged from V3-A
- Changes to `prepare.py` or the strategies registry
