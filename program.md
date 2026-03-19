# autoresearch

Autonomous stock strategy optimizer: iterates on screener and position management logic in `train.py` to maximize the `sharpe` ratio of a historical backtest.

---

## Setup (once per session)

1. **Agree on a run tag**: Propose a tag based on today's date (e.g. `mar18`). The branch `autoresearch/<tag>` must not already exist — check with `git branch -a | grep autoresearch/`.
2. **Create the branch**: `git checkout -b autoresearch/<tag>` from current master.
3. **Read the in-scope files**: `README.md`, `prepare.py` (read-only), `train.py` (the file you modify).
4. **Verify data exists**: Check that `~/.cache/autoresearch/stock_data/` contains `.parquet` files. Run `ls ~/.cache/autoresearch/stock_data/`. If the directory is empty or missing, tell the human to run `uv run prepare.py` and wait for confirmation before continuing.
5. **Initialize results.tsv**: Create the file with just the header row below. The baseline `sharpe` will be recorded after the first run.
6. **Confirm and go**: Confirm setup looks good, then begin the experiment loop.

---

## Experimentation rules

### What you CAN do

- Modify `screen_day()` in `train.py` — screener criteria, thresholds, indicator parameters, entry/exit rules, and any indicator helper functions it calls.
- Modify `manage_position()` in `train.py` — stop management logic, breakeven trigger level, trailing stop behavior.
- Add new indicator helper functions that `screen_day()` or `manage_position()` call.

### What you CANNOT do

- Modify anything below the `# DO NOT EDIT BELOW THIS LINE` comment in `train.py` — `run_backtest()`, `print_results()`, data loading functions, and the `__main__` block are the evaluation harness. Fixed. Must not be touched.
- Modify `print_results()` or the output format block — the agent parses this output; changing it breaks the loop.
- Modify `load_ticker_data()` or `load_all_ticker_data()` — fixed data loading infrastructure.
- Modify `CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END` constants — the backtest window is fixed per the PRD.
- Modify `prepare.py` — read-only data pipeline.
- Modify the Sharpe computation formula inside `run_backtest()`.
- Install new packages or add dependencies beyond what's in `pyproject.toml`.

### Goal

Maximize `sharpe` — **higher Sharpe is better**. Keep any change that increases Sharpe; discard any change that does not.

### Simplicity criterion

All else being equal, simpler is better. A small `sharpe` improvement that adds ugly complexity is not worth it. A simplification that maintains equal or better `sharpe` is always a win. Prefer interpretable threshold changes over convoluted logic.

### First run

Always run the strategy as-is (unmodified) as the very first run to establish the baseline Sharpe before making any changes.

---

## Output format

When the script completes successfully it prints a fixed-format block:

```
---
sharpe:              1.234567
total_trades:        12
win_rate:            0.583
avg_pnl_per_trade:   18.45
total_pnl:           221.40
backtest_start:      2026-01-01
backtest_end:        2026-03-01
```

Extract the key metrics from `run.log`:

```bash
grep "^sharpe:" run.log
grep "^total_trades:" run.log
```

Exit code 0 on success. Exit code 1 if the cache is empty (no parquet files found).

---

## Logging results

Log every run to `results.tsv` (tab-separated — NOT comma-separated; commas break in descriptions).

Header row (5 columns, tab-separated):

```
commit	sharpe	total_trades	status	description
```

Column definitions:
1. `commit`: git commit hash (short, 7 chars) from `git rev-parse --short HEAD`
2. `sharpe`: Sharpe ratio achieved (e.g. `1.234567`) — use `0.000000` for crashes
3. `total_trades`: number of trades completed in the backtest — use `0` for crashes
4. `status`: `keep`, `discard`, or `crash`
5. `description`: short text description of what this experiment tried

Example:

```
commit	sharpe	total_trades	status	description
a1b2c3d	0.000000	0	keep	baseline (no trades, strict screener)
b2c3d4e	1.234567	12	keep	relaxed CCI threshold to -30
c3d4e5f	0.872000	9	discard	removed volume filter
d4e5f6g	0.000000	0	crash	divide-by-zero in custom indicator
```

**Do NOT commit `results.tsv`** — it is intentionally untracked.

---

## The experiment loop

**LOOP FOREVER:**

1. Check git state: verify current branch and commit hash (`git log --oneline -1`).
2. Modify `train.py` with an experimental idea. Edit the file directly. **Only edit code above the `# DO NOT EDIT BELOW THIS LINE` comment** — everything below it is the evaluation harness and must not be touched.
3. `git commit` the change to `train.py` only; leave `results.tsv` untracked.
4. Run: `uv run train.py > run.log 2>&1` (redirect everything — do NOT use tee).
5. Extract results: `grep "^sharpe:" run.log` and `grep "^total_trades:" run.log`.
6. If grep output is empty, the run crashed. Run `tail -n 50 run.log` to read the Python traceback and attempt a fix. If you cannot fix it within a few attempts, give up: log status `crash`, run `git reset --hard HEAD~1`, and move on.
7. Record the result in `results.tsv`.
8. If Sharpe **improved (higher)** compared to the current best → keep the commit, advance the branch.
9. If Sharpe is equal or worse → `git reset --hard HEAD~1` (revert to previous commit).

### NEVER STOP

Once the loop has begun, do NOT pause to ask the user if you should continue. The user may be asleep. **You are autonomous.** Loop until manually stopped.

If you run out of ideas: try relaxing individual screener criteria one at a time, combining near-misses, varying the stop management trigger level, or adjusting position entry/exit rules.

### Crash handling

- If a run crashes and the fix is trivial (typo, missing import): fix and re-run.
- If the underlying idea is broken and cannot be quickly repaired: log `crash` and `git reset --hard HEAD~1` to move on.

### No-trades scenario

A Sharpe of `0.0` with `total_trades: 0` means the screener found zero signals across the entire backtest window. This is common on first run with the strict default screener. Try relaxing one threshold at a time (e.g. the CCI threshold, volume ratio, or momentum requirement) to generate at least some trades before optimizing Sharpe. **No signals = no information** — you cannot optimize what you cannot measure.
