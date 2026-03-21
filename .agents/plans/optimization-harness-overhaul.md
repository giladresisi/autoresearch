# Feature: Optimization Harness Overhaul (Enhancements 1–5)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Upgrades the autoresearch optimization loop with five improvements identified after multi-sector runs revealed structural flaws in the current harness:

1. **E1 — Train/test split**: Reserve the final 2 weeks of each backtest window as a held-out test set. The optimization loop operates on the training window only; test results are tracked but never used for keep/discard decisions.
2. **E2 — Optimize for train P&L**: Replace the Sharpe-based keep/discard criterion with `train_total_pnl`. The Sharpe formula is flawed on a sparse portfolio — idle portfolios have low variance and mechanically high Sharpe regardless of P&L.
3. **E3 — Final test outputs**: After the final optimization iteration, generate `final_test_data.csv` (per-ticker OHLCV + indicators for the test window) and print a per-ticker P&L breakdown table.
4. **E4 — Sector trend summary**: `prepare.py` writes `data_trend.md` after data download with a one-paragraph summary of the sector's price behavior — bullish/bearish/mixed, top gainers/losers, median return.
5. **E5 — Extended results.tsv**: Add `train_pnl`, `test_pnl`, `win_rate` columns to the per-iteration results log so P&L trends are visible across experiments.

## User Story

As a strategy researcher using autoresearch
I want a properly separated train/test evaluation harness that optimizes for real P&L
So that strategies I optimize don't overfit to the training window and I can assess out-of-sample behavior.

## Problem Statement

The current harness has four confirmed failure modes documented in `prd.md` (Enhancements 2026-03-20):
- **Overfitting**: Optimizing on the full backtest window allows strategies to memorize noise. Energy strategy: Sharpe 5.79 in-sample, -0.01 OOS.
- **Degenerate Sharpe**: `mean(diff(portfolio_values)) / std(...)` rewards inactivity. Reducing stop-management events smooths the series, lowers variance, raises Sharpe — while worsening P&L. Financials run: Sharpe 6.58, total P&L -$60.
- **Blind results.tsv**: Only `sharpe` and `total_trades` tracked; P&L gaps between Sharpe values were invisible across iterations.
- **No market context before the loop**: Agent has no summary of the sector's character when starting optimization.

## Solution Statement

Modify `train.py` (both sections) to support optional `start`/`end` window parameters and a `prefix` for output keys. Change the optimization criterion in `program.md` from `sharpe` to `train_total_pnl`. Add `write_trend_summary()` to `prepare.py`. All changes are additive and backwards-compatible with existing test signatures.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `train.py`, `program.md`, `prepare.py`, `tests/test_optimization.py`, `tests/test_program_md.py`
**Dependencies**: None (no new packages required)
**Breaking Changes**: Yes — `print_results()` output keys now carry a prefix (`train_sharpe:` not `sharpe:`); `results.tsv` schema changes. Both are internal to the optimization loop and not consumed by any external system.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 1–17) — Mutable section: BACKTEST_START/END constants pattern; add TRAIN_END, TEST_START, WRITE_FINAL_OUTPUTS here
- `train.py` (lines 258–391) — Full immutable section: `run_backtest()`, `print_results()`, `__main__` — all three are modified in this feature
- `program.md` (lines 1–183) — Full file; setup, loop, results.tsv schema, and grep commands all change
- `prepare.py` (lines 89–130) — `process_ticker()` and `__main__`: new `write_trend_summary()` is called after the download loop
- `tests/test_optimization.py` (lines 60–128) — Two tests need updating: pre-existing broken assertion (line 72) and GOLDEN_HASH (line 118)
- `tests/test_program_md.py` (lines 34–46) — Three assertions check old schema/grep patterns and must be updated
- `prd.md` (lines 618–730) — Source of truth for all enhancement specifications

### New Files Created

- `data_trend.md` — runtime artifact written by `prepare.py`; should be gitignored

### Patterns to Follow

**Function signatures**: Use `str | None` type hints with `None` defaults (matching `pd.DataFrame | None` pattern in `load_ticker_data`)
**Stats dict**: Always include all keys even in early-return paths (see implementation notes for full key list)
**print format**: Use existing `f"{prefix}{key}:              {value}"` spacing style

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                               │
├──────────────────────────────┬──────────────────────────────┤
│ Task 1.1: train.py mutable   │ Task 1.2: prepare.py E4      │
│ section — add constants      │ write_trend_summary()        │
│ Agent: harness-engineer      │ Agent: data-engineer         │
└──────────────────────────────┴──────────────────────────────┘
               ↓ (1.1 completes)
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: Core Harness (Sequential after 1.1)                 │
├─────────────────────────────────────────────────────────────┤
│ Task 2.1: train.py immutable section                        │
│ run_backtest(start,end) + print_results(prefix) +           │
│ __main__ double-run + ticker_pnl + _write_final_outputs     │
└─────────────────────────────────────────────────────────────┘
               ↓ (2.1 completes)
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: Integration (Parallel after 2.1)                    │
├──────────────────────────────┬──────────────────────────────┤
│ Task 3.1: test_optimization  │ Task 3.2: program.md update  │
│ GOLDEN_HASH + fix pre-       │ setup, loop, schema, grep,   │
│ existing broken assertion    │ final test run step          │
│ Agent: test-engineer         │ Agent: docs-engineer         │
└──────────────────────────────┴──────────────────────────────┘
               ↓ (3.2 completes)
┌─────────────────────────────────────────────────────────────┐
│ WAVE 4: Test Alignment (Sequential after 3.2)               │
├─────────────────────────────────────────────────────────────┤
│ Task 4.1: test_program_md.py — fix schema + grep tests      │
└─────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 (different files, zero interaction)
**Wave 2 — Sequential**: Task 2.1 (depends on constants from 1.1)
**Wave 3 — Parallel**: Tasks 3.1, 3.2 (different files; 3.1 needs 2.1 harness bytes for hash; 3.2 needs 2.1 output format for grep commands)
**Wave 4 — Sequential**: Task 4.1 (depends on 3.2 program.md content)

**Interface Contract**: Task 2.1 → Task 3.2: the prefixed output block format determines grep commands documented in program.md. Task 2.1 → Task 3.1: new harness bytes determine GOLDEN_HASH value.

**Parallelizable tasks**: 4 of 6 (~67%)

---

## IMPLEMENTATION PLAN

### Phase 1: train.py Mutable Section + prepare.py

#### Task 1.1 Details — Mutable constants

Add three constants immediately after `BACKTEST_END` in `train.py`:

```python
# Train/test split — last 14 calendar days of the backtest window are held out as test set.
# Written by the agent at setup time. Do NOT modify during the experiment loop.
TRAIN_END   = "2026-03-06"   # BACKTEST_END − 14 calendar days
TEST_START  = "2026-03-06"   # same date as TRAIN_END (test window starts here)

# Final output flag — set to True only for the special post-loop final test run.
# Agent sets True, runs train.py, then immediately restores to False.
WRITE_FINAL_OUTPUTS = False
```

#### Task 1.2 Details — write_trend_summary

Add function between `validate_ticker_data()` and `process_ticker()` in `prepare.py`:

```python
def write_trend_summary(tickers: list, backtest_start: str, backtest_end: str, cache_dir: str) -> None:
    """Compute sector price behaviour for the backtest window and write data_trend.md."""
    records = []
    for ticker in tickers:
        path = os.path.join(cache_dir, f"{ticker}.parquet")
        if not os.path.exists(path):
            continue
        df = pd.read_parquet(path)
        start_dt = pd.Timestamp(backtest_start).date()
        end_dt   = pd.Timestamp(backtest_end).date()
        sub = df[(df.index >= start_dt) & (df.index < end_dt)]
        if len(sub) < 2:
            continue
        first_close = float(sub["close"].iloc[0])
        last_close  = float(sub["close"].iloc[-1])
        if first_close == 0:
            continue
        ret = (last_close - first_close) / first_close
        records.append((ticker, ret))

    if not records:
        with open("data_trend.md", "w", encoding="utf-8") as f:
            f.write("# Sector Trend Summary\n\nNo data available.\n")
        return

    records.sort(key=lambda x: x[1], reverse=True)
    returns = [r for _, r in records]
    median_ret = float(sorted(returns)[len(returns) // 2])
    n_up   = sum(1 for r in returns if r > 0)
    n_down = len(returns) - n_up
    top3   = records[:3]
    bot3   = records[-3:][::-1]

    if median_ret > 0.03:
        character = f"Broadly bullish: {n_up}/{len(records)} tickers rose, median {median_ret:+.1%}."
    elif median_ret < -0.03:
        character = f"Broadly bearish: {n_down}/{len(records)} tickers fell, median {median_ret:+.1%}."
    else:
        character = f"Mixed/flat: {n_up}/{len(records)} tickers rose, median {median_ret:+.1%}."

    lines = [
        "# Sector Trend Summary",
        "",
        f"**Window**: {backtest_start} → {backtest_end} | **Tickers**: {len(records)}",
        f"**Median return**: {median_ret:+.1%} | **Up**: {n_up} | **Down**: {n_down}",
        "",
        f"**Top gainers**: " + ", ".join(f"{t} ({r:+.1%})" for t, r in top3),
        f"**Bottom losers**: " + ", ".join(f"{t} ({r:+.1%})" for t, r in bot3),
        "",
        f"**Sector character**: {character}",
    ]
    with open("data_trend.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("data_trend.md written")
```

Call from `prepare.py` `__main__` after the final `print(f"\nDone: ...")` line:

```python
    write_trend_summary(TICKERS, BACKTEST_START, BACKTEST_END, CACHE_DIR)
```

### Phase 2: train.py Immutable Section

**⚠️ GOLDEN_HASH must be recomputed immediately after this phase (see Task 3.1).**

#### Task 2.1 Details

**2.1a — run_backtest signature + body changes:**

```python
def run_backtest(ticker_dfs: dict, start: str | None = None, end: str | None = None) -> dict:
    """
    Run chronological backtest over start..end (defaults to BACKTEST_START..BACKTEST_END).
    Returns stats dict: sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl,
                        ticker_pnl, backtest_start, backtest_end.
    """
    s = date.fromisoformat(start or BACKTEST_START)
    e = date.fromisoformat(end or BACKTEST_END)
```

Replace all uses of `start` (the old `date.fromisoformat(BACKTEST_START)`) and `end` with `s`/`e`.

**Early-return path** — update to include all keys:
```python
    if len(trading_days) < 2:
        return {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
                "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
                "ticker_pnl": {}, "backtest_start": start or BACKTEST_START,
                "backtest_end": end or BACKTEST_END}
```

**Add ticker_pnl accumulation** — declare before the loop:
```python
    ticker_pnl: dict[str, float] = {}
```

When stop is hit (close position):
```python
                        pnl = (pos["stop_price"] - pos["entry_price"]) * pos["shares"]
                        trades.append(pnl)
                        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
```

When end-of-backtest close:
```python
        pnl = (last_price - pos["entry_price"]) * pos["shares"]
        trades.append(pnl)
        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
```

**Normal return path** — add new keys:
```python
    return {
        "sharpe":            round(sharpe, 6),
        "total_trades":      total_trades,
        "win_rate":          round(win_rate, 3),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "total_pnl":         round(total_pnl, 2),
        "ticker_pnl":        ticker_pnl,
        "backtest_start":    start or BACKTEST_START,
        "backtest_end":      end or BACKTEST_END,
    }
```

**2.1b — print_results signature:**

```python
def print_results(stats: dict, prefix: str = "") -> None:
    """Print the fixed-format summary block. Agent parses this with grep."""
    print("---")
    print(f"{prefix}sharpe:              {stats['sharpe']:.6f}")
    print(f"{prefix}total_trades:        {stats['total_trades']}")
    print(f"{prefix}win_rate:            {stats['win_rate']:.3f}")
    print(f"{prefix}avg_pnl_per_trade:   {stats['avg_pnl_per_trade']:.2f}")
    print(f"{prefix}total_pnl:           {stats['total_pnl']:.2f}")
    print(f"{prefix}backtest_start:      {stats['backtest_start']}")
    print(f"{prefix}backtest_end:        {stats['backtrack_end']}")
```

**⚠️ Typo guard**: use `stats['backtest_end']` (not `backtrack_end` — that is a typo in this document; use the correct key).

**2.1c — _write_final_outputs helper** (add before `__main__`):

```python
def _write_final_outputs(ticker_dfs: dict, test_start: str, test_end: str,
                         ticker_pnl: dict) -> None:
    """Write final_test_data.csv and print per-ticker P&L table for the test window."""
    import csv
    s = date.fromisoformat(test_start)
    e = date.fromisoformat(test_end)
    rows = []
    for ticker, df in ticker_dfs.items():
        sub = df[(df.index >= s) & (df.index < e)].copy()
        if sub.empty:
            continue
        sub = sub.copy()
        sub["ticker"] = ticker
        sub["sma50"]  = sub["close"].rolling(50).mean()
        sub["rsi14"]  = calc_rsi14(sub)
        sub["atr14"]  = calc_atr14(sub)
        sub["vm30"]   = sub["volume"].rolling(30).mean()
        for idx_date, row in sub.iterrows():
            rows.append({
                "ticker": ticker,
                "date": str(idx_date),
                **{c: round(float(row[c]), 4) if not pd.isna(row[c]) else ""
                   for c in ["open", "high", "low", "close", "volume",
                              "price_10am", "sma50", "rsi14", "atr14", "vm30"]},
            })
    if rows:
        fieldnames = list(rows[0].keys())
        with open("final_test_data.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"final_test_data.csv written ({len(rows)} rows)")
    if ticker_pnl:
        sorted_pnl = sorted(ticker_pnl.items(), key=lambda x: x[1], reverse=True)
        print("\nPer-ticker P&L (test window):")
        print(f"  {'Ticker':<10} {'P&L':>10}")
        print("  " + "-" * 22)
        for t, p in sorted_pnl:
            print(f"  {t:<10} {p:>10.2f}")
```

**2.1d — __main__ block** (replace entirely):

```python
if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)
    train_stats = run_backtest(ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
    print_results(train_stats, prefix="train_")
    test_stats = run_backtest(ticker_dfs, start=TEST_START, end=BACKTEST_END)
    print_results(test_stats, prefix="test_")
    if WRITE_FINAL_OUTPUTS:
        _write_final_outputs(ticker_dfs, TEST_START, BACKTEST_END, test_stats["ticker_pnl"])
```

### Phase 3: Tests + program.md (Parallel)

#### Task 3.1 Details — test_optimization.py

**Fix 1 — Pre-existing broken assertion (line ~72):**

```python
# Replace:
assert "c0 < -50" in above, (
    "Expected CCI threshold 'c0 < -50' in the editable section of train.py. "
    "Update this test if the threshold expression changes."
)
modified_source = above.replace("c0 < -50", "c0 < -30", 1) + marker + below

# With:
assert "vol_ratio < 1.0" in above, (
    "Expected volume ratio threshold 'vol_ratio < 1.0' in the editable section of train.py. "
    "Update this test if the threshold expression changes."
)
modified_source = above.replace("vol_ratio < 1.0", "vol_ratio < 1.2", 1) + marker + below
```

The call to `mod.run_backtest({})` remains valid — default args still work.

**Fix 2 — GOLDEN_HASH (line ~118):**

After completing Task 2.1, run this command from the project root to get the new hash:

```bash
python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
```

Update `GOLDEN_HASH` in `tests/test_optimization.py` with the output.

#### Task 3.2 Details — program.md

Make the following surgical edits to `program.md`. Read the file first, then apply each change:

**3.2a — Add step 6b to Setup section** (between step 6 and step 7):

```
6b. **Compute train/test split**: Set `TRAIN_END = BACKTEST_END − 14 calendar days`
    (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`). Write both
    `TRAIN_END` and `TEST_START` (same date) into the mutable section of `train.py`.
```

**3.2b — Replace results.tsv header** (in "Initialize results.tsv" step and "Logging results" section):

Old schema:
```
commit	sharpe	total_trades	status	description
```

New schema:
```
commit	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	status	description
```

Update the example row accordingly:
```
a1b2c3d	0.00	0.00	0.000000	0	0.000	keep	baseline (no trades, strict screener)
```

**3.2c — Replace output format block** (replace the single block with the double-block format):

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

**3.2d — Update grep commands** (Extract results step):

Old:
```bash
grep "^sharpe:" run.log
grep "^total_trades:" run.log
```

New:
```bash
grep "^train_total_pnl:" run.log
grep "^train_total_trades:" run.log
grep "^train_win_rate:" run.log
grep "^test_total_pnl:" run.log
```

**3.2e — Replace keep/discard criterion** (steps 8 and 9 of the loop):

Old steps 8–9:
```
8. If Sharpe **improved (higher)** compared to the current best → keep the commit, advance the branch.
9. If Sharpe is equal or worse → `git reset --hard HEAD~1` (revert to previous commit).
```

New steps 8–9:
```
8. If `train_total_pnl` **improved (higher)** compared to the current best → keep the commit, advance the branch.
9. If `train_total_pnl` is equal or lower → `git reset --hard HEAD~1` (revert to previous commit).
```

**3.2f — Replace Goal section**:

Old:
```
Maximize `sharpe` — **higher Sharpe is better**. Keep any change that increases Sharpe; discard any change that does not.
```

New:
```
Maximize `train_total_pnl` — **higher total P&L on the training window is better**.
Keep any change that increases `train_total_pnl`; discard any change that does not.

`train_sharpe` is still computed and printed — use it as a risk-quality diagnostic,
but it does not drive the keep/discard decision.
```

**3.2g — Add to "What you CANNOT do" section**:

Add this bullet:
```
- Modify `TRAIN_END` or `TEST_START` after setup. These are set once at session start and must not be changed during the experiment loop.
```

**3.2h — Add Final Test Run section** after the "The experiment loop" section:

```markdown
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
```

### Phase 4: test_program_md.py

#### Task 4.1 Details

Three test assertions reference old patterns. Update each:

**test_results_tsv_header (line ~36)**:
```python
# Old:
assert "commit\tsharpe\ttotal_trades\tstatus\tdescription" in c
# New:
assert "commit\ttrain_pnl\ttest_pnl\ttrain_sharpe\ttotal_trades\twin_rate\tstatus\tdescription" in c
```

**test_grep_sharpe_command (line ~41)**:
```python
# Old:
assert 'grep "^sharpe:" run.log' in c
# New:
assert 'grep "^train_total_pnl:" run.log' in c
```

**test_grep_total_trades_command (line ~46)**:
```python
# Old:
assert 'grep "^total_trades:" run.log' in c
# New:
assert 'grep "^train_total_trades:" run.log' in c
```

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Foundation (Parallel)

#### Task 1.1: UPDATE train.py mutable section

- **WAVE**: 1
- **AGENT_ROLE**: harness-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [Task 2.1]
- **PROVIDES**: TRAIN_END, TEST_START, WRITE_FINAL_OUTPUTS constants available at module level
- **IMPLEMENT**: Add three constants after `BACKTEST_END` in `train.py` (above the `# DO NOT EDIT` line) as specified in Phase 1, Task 1.1. Use `TRAIN_END = "2026-03-06"`, `TEST_START = "2026-03-06"`.
- **VALIDATE**: `python -c "import train; print(train.TRAIN_END, train.TEST_START, train.WRITE_FINAL_OUTPUTS)"`

#### Task 1.2: UPDATE prepare.py — write_trend_summary (E4)

- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PROVIDES**: `write_trend_summary()` function; `data_trend.md` written after `uv run prepare.py`
- **IMPLEMENT**: Add `write_trend_summary()` between `validate_ticker_data()` and `process_ticker()`. Call it from `__main__` after the done-count print. Full implementation in Phase 1, Task 1.2.
- **VALIDATE**: `python -c "import prepare; print('write_trend_summary' in dir(prepare))"`

**Wave 1 Checkpoint**: `uv run pytest tests/test_prepare.py -x -q`

---

### WAVE 2: Core Harness (Sequential after 1.1)

#### Task 2.1: UPDATE train.py immutable section

- **WAVE**: 2
- **AGENT_ROLE**: harness-engineer
- **DEPENDS_ON**: [Task 1.1]
- **BLOCKS**: [Task 3.1, Task 3.2]
- **PROVIDES**: Updated run_backtest(start, end), print_results(prefix), _write_final_outputs helper, updated __main__ double-run
- **IMPLEMENT**: Make all four changes in Phase 2: (a) run_backtest signature + ticker_pnl accumulation + return dict keys, (b) print_results prefix param, (c) _write_final_outputs helper, (d) __main__ double-run with WRITE_FINAL_OUTPUTS gate. Keep Sharpe formula, stop logic, and mark-to-market unchanged.
- **VALIDATE**: `uv run train.py > run.log 2>&1 && grep "^train_total_pnl:" run.log && grep "^test_total_pnl:" run.log`

**Wave 2 Checkpoint**: `uv run pytest tests/test_backtester.py -x -q`
(Existing tests pass — `run_backtest({})` and `run_backtest({'X': df})` still work with default args.)

---

### WAVE 3: Integration (Parallel after 2.1)

#### Task 3.1: UPDATE tests/test_optimization.py

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [Task 2.1]
- **BLOCKS**: []
- **PROVIDES**: Passing test_harness_below_do_not_edit_is_unchanged; fixed pre-existing broken test
- **IMPLEMENT**:
  1. Recompute GOLDEN_HASH using the command in Phase 3, Task 3.1 Fix 2
  2. Update `GOLDEN_HASH` constant with the new value
  3. Fix broken assertion: replace `"c0 < -50"` check and replacement with `"vol_ratio < 1.0"` → `"vol_ratio < 1.2"` (Phase 3, Task 3.1 Fix 1)
- **VALIDATE**: `uv run pytest tests/test_optimization.py -x -q`

#### Task 3.2: UPDATE program.md

- **WAVE**: 3
- **AGENT_ROLE**: docs-engineer
- **DEPENDS_ON**: [Task 2.1]
- **BLOCKS**: [Task 4.1]
- **PROVIDES**: Updated agent instructions: train/test split setup, new keep/discard criterion, new schema, double output block format, final test run step
- **IMPLEMENT**: Read program.md, then apply the 8 surgical edits described in Phase 3, Task 3.2 (3.2a through 3.2h).
- **VALIDATE**: `uv run pytest tests/test_program_md.py -x -q` (3 tests will fail until Task 4.1 — that is expected)

**Wave 3 Checkpoint**: `uv run pytest tests/test_optimization.py tests/test_backtester.py -x -q`

---

### WAVE 4: Test Alignment (Sequential after 3.2)

#### Task 4.1: UPDATE tests/test_program_md.py

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [Task 3.2]
- **BLOCKS**: []
- **PROVIDES**: All test_program_md tests passing against updated program.md
- **IMPLEMENT**: Apply three assertion fixes as specified in Phase 4, Task 4.1.
- **VALIDATE**: `uv run pytest tests/test_program_md.py -x -q`

**Final Checkpoint**: `uv run pytest tests/ -x -q`

---

## TESTING STRATEGY

This feature changes Python scripts and markdown — no frontend, no external APIs, no new packages.

### Existing Tests (must remain passing — backwards-compatible changes)

| Test | File | Reason passes |
|---|---|---|
| test_run_backtest_empty_dict_returns_zero_sharpe | test_backtester.py | `run_backtest({})` still works; default args |
| test_run_backtest_no_backtest_days_returns_zero_sharpe | test_backtester.py | Default window unchanged |
| test_run_backtest_no_signals_returns_zero_sharpe | test_backtester.py | Unaffected |
| test_run_backtest_no_reentry_same_ticker | test_backtester.py | ticker_pnl is additive; logic unchanged |
| test_run_backtest_stop_hit_closes_position | test_backtester.py | ticker_pnl accumulation is additive |
| All manage_position tests | test_backtester.py | Untouched |
| test_file_exists, test_title, etc. | test_program_md.py | Structural checks still satisfied |
| test_higher_sharpe_is_better | test_program_md.py | "higher" still present (train_total_pnl higher is better) |

### Tests Requiring Updates (tracked in tasks)

| Test | File | Status | Fix in |
|---|---|---|---|
| test_editable_section_stays_runnable_after_threshold_change | test_optimization.py | ⚠️ PRE-EXISTING FAILURE → ✅ | Task 3.1 |
| test_harness_below_do_not_edit_is_unchanged | test_optimization.py | ⚠️ → ✅ | Task 3.1 |
| test_results_tsv_header | test_program_md.py | ⚠️ → ✅ | Task 4.1 |
| test_grep_sharpe_command | test_program_md.py | ⚠️ → ✅ | Task 4.1 |
| test_grep_total_trades_command | test_program_md.py | ⚠️ → ✅ | Task 4.1 |

### Edge Cases

- **Empty ticker_dfs**: early-return path includes all keys (`ticker_pnl: {}`, `backtest_start`, `backtest_end`) — ✅ covered by existing empty-dict test
- **TRAIN_END == TEST_START**: adjacent windows, no overlap — ✅ no special guard needed
- **WRITE_FINAL_OUTPUTS = False (default)**: `_write_final_outputs` never called — ✅ guarded by `if WRITE_FINAL_OUTPUTS:`
- **print_results with prefix=""**: produces identical output to current format — ✅ prefix is empty string by default
- **write_trend_summary with missing parquet files**: skips missing tickers gracefully — ✅ guarded by `os.path.exists`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Existing passing (pytest) | 22+ | ~82% |
| ⚠️ Requiring updates (tracked) | 5 | ~18% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 27+ | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax Check

```bash
python -c "import train; print('train.py OK')"
python -c "import prepare; print('prepare.py OK')"
```

### Level 2: Unit Tests (by wave)

```bash
# After Wave 1:
uv run pytest tests/test_prepare.py -x -q

# After Wave 2:
uv run pytest tests/test_backtester.py -x -q

# After Wave 3:
uv run pytest tests/test_optimization.py -x -q

# After Wave 4:
uv run pytest tests/test_program_md.py -x -q
```

### Level 3: Integration

```bash
uv run train.py > run.log 2>&1
grep "^train_total_pnl:" run.log
grep "^test_total_pnl:" run.log
grep "^train_sharpe:" run.log
grep "^test_sharpe:" run.log
```

### Level 4: Full Suite

```bash
uv run pytest tests/ -x -q
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `train.py` mutable section contains `TRAIN_END`, `TEST_START`, and `WRITE_FINAL_OUTPUTS` constants
- [ ] `uv run train.py` produces two prefixed output blocks: all keys prefixed with `train_` followed by all keys prefixed with `test_`
- [ ] `grep "^train_total_pnl:" run.log` returns a value after any run
- [ ] `grep "^test_total_pnl:" run.log` returns a value after any run
- [ ] Setting `WRITE_FINAL_OUTPUTS = True` and running `uv run train.py` creates `final_test_data.csv` with one row per (ticker, date) in the test window
- [ ] Per-ticker P&L table is printed to stdout when `WRITE_FINAL_OUTPUTS = True`, sorted by P&L descending
- [ ] `uv run prepare.py` writes `data_trend.md` to the working directory after downloading data
- [ ] `data_trend.md` contains: ticker count, median return, up/down count, top 3 gainers, bottom 3 losers, sector character phrase

### Error Handling
- [ ] `run_backtest({})` (empty dict) returns a valid stats dict with all keys including `ticker_pnl: {}` — no exception raised
- [ ] `write_trend_summary` skips tickers whose parquet file is missing without raising
- [ ] `WRITE_FINAL_OUTPUTS = True` with no trades in test window prints an empty per-ticker table gracefully (no exception)

### Integration / E2E
- [ ] `program.md` results.tsv schema line reads `commit\ttrain_pnl\ttest_pnl\ttrain_sharpe\ttotal_trades\twin_rate\tstatus\tdescription`
- [ ] `program.md` keep/discard decision references `train_total_pnl` (not `sharpe`)
- [ ] `program.md` contains a Final Test Run section describing how to use `WRITE_FINAL_OUTPUTS`
- [ ] `program.md` setup section includes the step to compute and write `TRAIN_END`/`TEST_START`

### Validation
- [ ] `uv run pytest tests/test_backtester.py -x -q` passes with no changes to existing tests
- [ ] `uv run pytest tests/test_optimization.py -x -q` passes after GOLDEN_HASH update
- [ ] `uv run pytest tests/test_program_md.py -x -q` passes after test updates
- [ ] Full suite green — verified by: `uv run pytest tests/ -x -q`

### Out of Scope
- E6 (strategy registry, LLM selector) — separate plan
- Changes to `screen_day()` or `manage_position()` strategy logic
- Changes to the Sharpe formula in `run_backtest()`
- Committing or pushing any changes

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Wave 1 checkpoint passed (`tests/test_prepare.py`)
- [ ] Wave 2 checkpoint passed (`tests/test_backtester.py`)
- [ ] Wave 3 checkpoint passed (`tests/test_optimization.py` + `tests/test_backtester.py`)
- [ ] Final checkpoint passed (`uv run pytest tests/ -x -q`)
- [ ] GOLDEN_HASH recomputed and updated
- [ ] Pre-existing broken test (`c0 < -50`) fixed
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### GOLDEN_HASH Recompute

After completing Task 2.1, run this exact command from the project root:

```bash
python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
```

Copy the output and replace the value of `GOLDEN_HASH` in `tests/test_optimization.py`.

### Pre-existing Test Failure

`test_editable_section_stays_runnable_after_threshold_change` has been broken since the strategy was evolved from a CCI-based pullback screener to a momentum breakout (commit `68ac7ae`). The assertion `"c0 < -50" in above` was never updated. This plan fixes it as Task 3.1.

### Backwards Compatibility

All existing test calls `run_backtest({})` and `run_backtest({'X': df})` continue to work — `start=None, end=None` defaults to module globals `BACKTEST_START`/`BACKTEST_END`. The return dict adds two new keys (`ticker_pnl`, `backtest_start`, `backtest_end`) but does not remove or rename existing ones.

### TRAIN_END / TEST_START Dates

This plan uses `"2026-03-06"` (BACKTEST_END `"2026-03-20"` minus 14 calendar days). When the next session uses a different BACKTEST_END, the agent recomputes this at setup time per the new step 6b in program.md.

### data_trend.md Gitignore

`data_trend.md` is a runtime artifact (like `run.log`). Add it to `.gitignore` so it is not committed. Check whether `.gitignore` already excludes it; if not, add the entry.

### Target Branch

This plan is implemented on a new branch `enhancements/mar20` off `master`. Do not implement on an `autoresearch/` worktree branch.
