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

4b. **Compute train/test split and walk-forward boundaries**:
    - `TRAIN_END = BACKTEST_END − 14 calendar days`
      (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`)
    - `TEST_START = TRAIN_END` (same date — kept for backward reference)
    - `SILENT_END = TRAIN_END − 14 calendar days`
      (e.g. TRAIN_END `2026-03-06` → SILENT_END `2026-02-20`)

    Write `TRAIN_END`, `TEST_START`, and `SILENT_END` into the mutable section of `train.py`.
    `WALK_FORWARD_WINDOWS` is already set to 3 — leave it unless the user specifies otherwise.

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

Maximize `min_test_pnl` — **higher minimum test P&L across all walk-forward folds is better**.
Keep any change that increases `min_test_pnl`; discard any change that does not.

`fold{N}_train_*` metrics are still computed and printed — use them as diagnostics,
but they do not drive the keep/discard decision.

### Simplicity criterion

All else being equal, simpler is better. A small `sharpe` improvement that adds ugly complexity is not worth it. A simplification that maintains equal or better `sharpe` is always a win. Prefer interpretable threshold changes over convoluted logic.

### First run

Always run the strategy as-is (unmodified) as the very first run to establish the baseline Sharpe before making any changes.

---

## Output format

When the script completes successfully it prints N×2 fold blocks (N = `WALK_FORWARD_WINDOWS`),
then a `min_test_pnl` summary, then a `silent_pnl` line.

For `WALK_FORWARD_WINDOWS = 3`, the output is:

```
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
```

Extract the key metrics from `run.log`:

```bash
grep "^min_test_pnl:" run.log
grep "^fold3_train_total_pnl:" run.log
grep "^fold3_train_total_trades:" run.log
grep "^fold3_train_win_rate:" run.log
grep "^fold3_train_sharpe:" run.log
grep "^fold3_train_calmar:" run.log
grep "^fold3_train_pnl_consistency:" run.log
```

Replace `fold3` with `fold${WALK_FORWARD_WINDOWS}` if you change `WALK_FORWARD_WINDOWS`.

Exit code 0 on success. Exit code 1 if the cache is empty (no parquet files found).

---

## Logging results

Log every run to `results.tsv` (tab-separated — NOT comma-separated; commas break in descriptions).

Header row (tab-separated):

```
commit	min_test_pnl	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	train_calmar	train_pnl_consistency	status	description
```

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

Example:

```
commit	min_test_pnl	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	train_calmar	train_pnl_consistency	status	description
a1b2c3d	0.00	0.00	0.00	0.000000	0	0.000	0.0000	0.00	keep	baseline (no trades, strict screener)
b2c3d4e	15.60	221.40	25.00	1.234567	12	0.583	2.5000	30.00	keep	relaxed volume ratio to 1.2
c3d4e5f	-5.00	180.00	8.00	0.872000	9	0.444	1.2000	15.00	discard	removed volume filter
d4e5f6g	0.00	0.00	0.00	0.000000	0	0.000	0.0000	0.00	crash	divide-by-zero in custom indicator
```

**Do NOT commit `results.tsv`** — it is intentionally untracked.

---

## The experiment loop

**LOOP for the configured number of iterations (default: 30):**

1. Check git state: verify current branch and commit hash (`git log --oneline -1`).
2. Modify `train.py` with an experimental idea. Edit the file directly. **Only edit code above the `# DO NOT EDIT BELOW THIS LINE` comment** — everything below it is the evaluation harness and must not be touched.
3. `git commit` the change to `train.py` only; leave `results.tsv` untracked.
4. Run: `uv run train.py > run.log 2>&1` (redirect everything — do NOT use tee).
5. Extract results: `grep "^min_test_pnl:" run.log` and `grep "^fold${WALK_FORWARD_WINDOWS}_train_total_trades:" run.log`.
6. If grep output is empty, the run crashed. Run `tail -n 50 run.log` to read the Python traceback and attempt a fix. If you cannot fix it within a few attempts, give up: log status `crash`, run `git reset --hard HEAD~1`, and move on.
7. Record the result in `results.tsv`.
8. If `min_test_pnl` **improved (higher)** compared to the current best → keep the commit, advance the branch.

> **Note:** `silent_pnl` is hidden during the loop (`HIDDEN`). Do NOT attempt to infer or
> act on the hidden holdout result. The sole keep/discard criterion is `min_test_pnl`.
> `fold{N}_train_*` metrics are for diagnostics only.

9. If `min_test_pnl` is equal or lower → `git reset --hard HEAD~1` (revert to previous commit).
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
4. Collect `final_test_data.csv` — per-ticker daily OHLCV + indicators for the holdout window
5. Read the per-ticker P&L table printed at the end of `run.log` (prefixed `holdout_`)
6. Append per-ticker P&L to `results.tsv` — add an empty line after the last experiment row,
   then a header row, then one row per ticker (tab-separated):

   ```bash
   printf "\n" >> results.tsv
   printf "ticker\tholdout_pnl\n" >> results.tsv
   uv run python -c "
   import re, sys
   lines = open('run.log').readlines()
   i = next(i for i, l in enumerate(lines) if 'Per-ticker P&L' in l)
   for l in lines[i+3:]:
       m = re.match(r'\s+(\S+)\s+([-\d.]+)', l.rstrip())
       if m:
           print(m.group(1) + '\t' + m.group(2))
   " >> results.tsv
   ```

**Design constraint**: `WRITE_FINAL_OUTPUTS` must be restored to `False` after the final
run to prevent all subsequent runs from writing CSV files. Never commit it as `True`.

---

## Harness reflection (write harness_upgrade.md)

After the final test run, perform a deep, max-effort reflection on this run in its entirety —
the harness design, the parameters chosen, and what the outcomes reveal about both.
**Think as hard as you can.** This reflection will be harvested by the `fetch-strategies`
skill and merged with reflections from other worktrees to drive improvements to the system.

### What to reflect on

**1. Statistical validity of the test window**
- How many test-window trades were there? (< 10 = noisy signal)
- Were test-window positions mostly force-closed at the boundary rather than exiting via stop?
  (If so: the 14-day test window is too short for this strategy's typical holding period)
- Is the gap between `train_total_pnl` and `test_total_pnl` large? Estimate what fraction
  of that gap is likely overfitting vs. genuine regime difference.

**2. Overfitting indicators**
- Did `train_total_pnl` improve monotonically across iterations while `test_pnl` stagnated
  or declined? That is a strong overfitting signal.
- Did the final screener accumulate many conditions? Each added rule is a potential
  overfit to training-window noise.
- Did the strategy converge toward very narrow parameter ranges (e.g. RSI 58–62)?
  Narrow ranges are fragile out-of-sample.

**3. Training window market regime**
- Was the training period trending, volatile, or range-bound? How does that affect
  the validity of the learned parameters for future markets?
- Were the chosen tickers correlated with each other? (Many semis/financials moving
  together inflates apparent diversification.)

**4. Parameter choices**
- **Tickers**: Were some tickers responsible for almost all the P&L? Would a broader
  or more diversified basket produce more robust results?
- **Timeframe**: Was the backtest window long enough to capture multiple market
  regimes, or did it all fall in one trend / one event?
- **Iterations**: Did improvement plateau early (e.g. after iteration 10)?
  Was 30 iterations too many (overfit) or too few (unexplored space)?

**5. Harness structural issues**
- Is the 14-day test window appropriate for this strategy's typical trade duration?
  (If avg hold is > 5 days and test window is 10 trading days, most test trades
  are force-closed — consider recommending a longer test window.)
- Is `train_total_pnl` the right objective? (It scales with trade count — a strategy
  that generates many small trades can outscore a strategy with fewer but higher-quality
  trades. Consider whether `train_avg_pnl_per_trade` or `train_win_rate` should be
  a co-objective or guard rail.)

### Output format

Write your conclusions to `harness_upgrade.md` in this worktree using exactly this structure:

```markdown
# Harness Update Recommendations

**Worktree:** <current branch name>
**Run date:** <today's date YYYY-MM-DD>
**Parameters:** tickers=<list>, timeframe=<BACKTEST_START>→<BACKTEST_END>, iterations=<N>
**Results:** train_pnl=<X>, test_pnl=<Y>, train_trades=<N>, test_trades=<M>

---

## Recommendations

### R1: <short title>
**Category:** `harness-split` | `harness-objective` | `harness-structure` | `params-tickers` | `params-timeframe` | `params-iterations`
**Priority:** `high` | `medium` | `low`
**Rationale:** <1–3 sentences: what you observed that motivates this recommendation>
**Suggested change:** <concrete, actionable change to program.md, train.py constants, or parameter selection guidance>

### R2: ...
```

**Rules:**
- Write between 3 and 8 recommendations. Don't pad with obvious/generic advice.
- Every recommendation must be grounded in a specific observation from this run's data
  (reference actual numbers: trade counts, PnL values, iteration curve, etc.).
- Be critical of the harness and parameters even when the run looked "good" —
  a good train result with a weak test result is a problem, not a success.
- Do NOT commit `harness_upgrade.md` — it is intentionally untracked (like `results.tsv`).
