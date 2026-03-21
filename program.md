# autoresearch

Autonomous stock strategy optimizer: iterates on screener and position management logic in `train.py` to maximize `train_total_pnl` on the training window of a historical backtest.

---

## Parameters

The following parameters can be specified in the user's query. If not specified, defaults apply.

| Parameter | Default | Description |
|-----------|---------|-------------|
| **Timeframe** | Past 3 months before today | The backtest window: `BACKTEST_START` = first day of the 3-month window, `BACKTEST_END` = today. |
| **Tickers** | As specified in `prepare.py` (`TICKERS` list) | Stock symbols to backtest. |
| **Iterations** | 30 | Number of experiment iterations to run before stopping. |

---

## Setup (once per session)

0. **Verify you are in an optimization worktree**: Run `git branch --show-current`. The branch
   must match `autoresearch/*`. If it shows `master` or any other branch, **stop immediately**
   and tell the user:
   > You are on the `master` branch. Please use the `prepare-optimization` skill first to create
   > a dedicated worktree, then open a new Claude Code session inside it (`cd ../<tag> && claude`).

1. **Parse parameters from the user's query**: Identify any user-specified values for timeframe, tickers, and iterations. For each parameter, note whether it is user-defined or using the default.

2. **Compute dates**:
   - If the user specified a timeframe, parse it into `BACKTEST_START` and `BACKTEST_END` dates (`YYYY-MM-DD`).
   - If using the default, set `BACKTEST_END` = today's date, `BACKTEST_START` = 3 months prior (e.g. today `2026-03-20` → start `2025-12-20`).

3. **Update `prepare.py` USER CONFIGURATION**: Edit the `TICKERS`, `BACKTEST_START`, and `BACKTEST_END` values in the `# ── USER CONFIGURATION ──` block of `prepare.py`. These three variables are the only lines you may edit in `prepare.py`.

   Example edit for tickers `AAPL, NVDA` and window `2025-12-20` to `2026-03-20`:
   ```python
   TICKERS = ["AAPL", "NVDA"]
   BACKTEST_START = "2025-12-20"
   BACKTEST_END   = "2026-03-20"
   ```

4. **Update `train.py` constants**: Set `BACKTEST_START` and `BACKTEST_END` at the top of `train.py` to match the values you just wrote into `prepare.py`.

4b. **Compute train/test split**: Set `TRAIN_END = BACKTEST_END − 14 calendar days`
    (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`). Write both
    `TRAIN_END` and `TEST_START` (same date) into the mutable section of `train.py`.

5. **Download data**: Run `uv run prepare.py`. Wait for it to complete. If it fails, report the error to the user and stop. Data is cached in `~/.cache/autoresearch/stock_data/` as `.parquet` files — one per ticker.

6. **Read the in-scope files**: `README.md`, `train.py` (the file you modify in experiments).

6b. **Check for legacy Sharpe objective**: Grep `train.py` for the line:
    ```
    # LEGACY_OBJECTIVE: sharpe
    ```
    If found, the strategy's parameters were tuned under the old Sharpe-optimizing harness (pre-Enhancement 2).
    Before the experiment loop begins, update `train.py` above the boundary to remove or replace any
    logic that was explicitly written to boost Sharpe at the expense of P&L (e.g. overly tight screeners
    that reduce trade count to inflate Sharpe). Concretely:
    - Keep the core entry/exit structure and indicator logic intact.
    - Reset any threshold that was tightened purely to reduce trades and lift Sharpe (e.g. unusually
      narrow RSI bands, very high volume multiples) back to a more neutral starting value.
    - The first baseline run will then establish a clean P&L baseline for the new loop.
    If the line is **not** present, the strategy was already optimized for train P&L — proceed normally.

7. **Print the parameter trace** — output this block immediately after setup completes, before the first experiment run:

   ```
   ── Run parameters ────────────────────────────────
   Tickers:     AAPL, NVDA          [user-defined]
   Timeframe:   2025-12-20 → 2026-03-20  [user-defined]
   Iterations:  30                   [default]
   ──────────────────────────────────────────────────
   ```

   Label each line `[user-defined]` or `[default]` as appropriate.

8. **Initialize results.tsv**: Create the file with just the header row below. The baseline `sharpe` will be recorded after the first run.

9. **Confirm and go**: Confirm setup looks good, then begin the experiment loop.

---

## Experimentation rules

### What you CAN do

- Modify `screen_day()` in `train.py` — screener criteria, thresholds, indicator parameters, entry/exit rules, and any indicator helper functions it calls.
- Modify `manage_position()` in `train.py` — stop management logic, breakeven trigger level, trailing stop behavior.
- Add new indicator helper functions that `screen_day()` or `manage_position()` call.
- Edit the `TICKERS`, `BACKTEST_START`, and `BACKTEST_END` lines in the `# ── USER CONFIGURATION ──` block of `prepare.py` **during setup only** (step 3 above). No other lines in `prepare.py` may be changed.

### What you CANNOT do

- Modify anything below the `# DO NOT EDIT BELOW THIS LINE` comment in `train.py` — `run_backtest()`, `print_results()`, data loading functions, and the `__main__` block are the evaluation harness. Fixed. Must not be touched.
- Modify `print_results()` or the output format block — the agent parses this output; changing it breaks the loop.
- Modify `load_ticker_data()` or `load_all_ticker_data()` — fixed data loading infrastructure.
- Modify `CACHE_DIR` in `train.py` — fixed cache path.
- Modify `prepare.py` beyond the three USER CONFIGURATION variables (`TICKERS`, `BACKTEST_START`, `BACKTEST_END`).
- Modify the Sharpe computation formula inside `run_backtest()`.
- Install new packages or add dependencies beyond what's in `pyproject.toml`.
- Modify `TRAIN_END` or `TEST_START` after setup. These are set once at session start and must not be changed during the experiment loop.

### Goal

Maximize `train_total_pnl` — **higher total P&L on the training window is better**.
Keep any change that increases `train_total_pnl`; discard any change that does not.

`train_sharpe` is still computed and printed — use it as a risk-quality diagnostic,
but it does not drive the keep/discard decision.

### Simplicity criterion

All else being equal, simpler is better. A small `sharpe` improvement that adds ugly complexity is not worth it. A simplification that maintains equal or better `sharpe` is always a win. Prefer interpretable threshold changes over convoluted logic.

### First run

Always run the strategy as-is (unmodified) as the very first run to establish the baseline Sharpe before making any changes.

---

## Output format

When the script completes successfully it prints two fixed-format blocks — one for the training window, one for the test window:

```
---
train_sharpe:              1.234567
train_total_trades:        12
train_win_rate:            0.583
train_avg_pnl_per_trade:   18.45
train_total_pnl:           221.40
train_backtest_start:      2026-01-01
train_backtest_end:        2026-03-06
---
test_sharpe:               0.876543
test_total_trades:         3
test_win_rate:             0.333
test_avg_pnl_per_trade:    5.20
test_total_pnl:            15.60
test_backtest_start:       2026-03-06
test_backtest_end:         2026-03-20
```

Extract the key metrics from `run.log`:

```bash
grep "^train_total_pnl:" run.log
grep "^train_total_trades:" run.log
grep "^train_win_rate:" run.log
grep "^test_total_pnl:" run.log
```

Exit code 0 on success. Exit code 1 if the cache is empty (no parquet files found).

---

## Logging results

Log every run to `results.tsv` (tab-separated — NOT comma-separated; commas break in descriptions).

Header row (7 columns, tab-separated):

```
commit	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	status	description
```

Column definitions:
1. `commit`: git commit hash (short, 7 chars) from `git rev-parse --short HEAD`
2. `train_pnl`: total P&L on the training window — use `0.00` for crashes
3. `test_pnl`: total P&L on the test window — use `0.00` for crashes
4. `train_sharpe`: Sharpe ratio on the training window (e.g. `1.234567`) — use `0.000000` for crashes
5. `total_trades`: number of trades completed in the training backtest — use `0` for crashes
6. `win_rate`: fraction of winning trades — use `0.000` for crashes
7. `status`: `keep`, `discard`, or `crash`
8. `description`: short text description of what this experiment tried

Example:

```
commit	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	status	description
a1b2c3d	0.00	0.00	0.000000	0	0.000	keep	baseline (no trades, strict screener)
b2c3d4e	221.40	15.60	1.234567	12	0.583	keep	relaxed volume ratio to 1.2
c3d4e5f	180.00	8.00	0.872000	9	0.444	discard	removed volume filter
d4e5f6g	0.00	0.00	0.000000	0	0.000	crash	divide-by-zero in custom indicator
```

**Do NOT commit `results.tsv`** — it is intentionally untracked.

---

## The experiment loop

**LOOP for the configured number of iterations (default: 30):**

1. Check git state: verify current branch and commit hash (`git log --oneline -1`).
2. Modify `train.py` with an experimental idea. Edit the file directly. **Only edit code above the `# DO NOT EDIT BELOW THIS LINE` comment** — everything below it is the evaluation harness and must not be touched.
3. `git commit` the change to `train.py` only; leave `results.tsv` untracked.
4. Run: `uv run train.py > run.log 2>&1` (redirect everything — do NOT use tee).
5. Extract results: `grep "^train_total_pnl:" run.log` and `grep "^train_total_trades:" run.log`.
6. If grep output is empty, the run crashed. Run `tail -n 50 run.log` to read the Python traceback and attempt a fix. If you cannot fix it within a few attempts, give up: log status `crash`, run `git reset --hard HEAD~1`, and move on.
7. Record the result in `results.tsv`.
8. If `train_total_pnl` **improved (higher)** compared to the current best → keep the commit, advance the branch.
9. If `train_total_pnl` is equal or lower → `git reset --hard HEAD~1` (revert to previous commit).
10. When the configured number of iterations is reached, stop and report the best result to the user.

### AUTONOMOUS UNTIL DONE — NEVER STOP EARLY

Once the loop has begun, do NOT pause to ask the user if you should continue. The user may be asleep. **You are autonomous.** NEVER stop before reaching the configured iteration count. Loop until done, then stop and report.

If you run out of ideas before reaching the iteration limit: try relaxing individual screener criteria one at a time, combining near-misses, varying the stop management trigger level, or adjusting position entry/exit rules.

### Crash handling

- If a run crashes and the fix is trivial (typo, missing import): fix and re-run.
- If the underlying idea is broken and cannot be quickly repaired: log `crash` and `git reset --hard HEAD~1` to move on.

### No-trades scenario

A Sharpe of `0.0` with `total_trades: 0` means the screener found zero signals across the entire backtest window. This is common on first run with the strict default screener. Try relaxing one threshold at a time (e.g. the CCI threshold, volume ratio, or momentum requirement) to generate at least some trades before optimizing Sharpe. **No signals = no information** — you cannot optimize what you cannot measure.

---

## Final test run (after last iteration)

After the configured number of iterations is reached, trigger a special final test run
with full output mode enabled:

1. Edit `WRITE_FINAL_OUTPUTS = True` in the mutable section of `train.py`
2. Run: `uv run train.py > run.log 2>&1`
3. Edit `WRITE_FINAL_OUTPUTS = False` (restore immediately)
4. Collect `final_test_data.csv` — per-ticker daily OHLCV + indicators for the test window
5. Read the per-ticker P&L table printed at the end of `run.log`

**Design constraint**: `WRITE_FINAL_OUTPUTS` must be restored to `False` after the final
run to prevent all subsequent runs from writing CSV files. Never commit it as `True`.
