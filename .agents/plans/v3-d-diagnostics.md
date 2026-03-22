# Feature: V3-D Diagnostics and Advanced (R6, R10, R11)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils,
types, and models. Import from correct files.

---

## Feature Description

Three diagnostic and robustness improvements to the harness, each additive and gated:

- **R10**: Bootstrap confidence interval on final P&L — when `WRITE_FINAL_OUTPUTS = True`,
  resample the closed trade P&Ls from the final test run to produce 95% CI bounds, printed as
  `bootstrap_pnl_p05:` and `bootstrap_pnl_p95:` lines.

- **R11 (harness-level)**: Per-regime trade attribution — classify each trading day as `'bull'`
  or `'bear'` using a cross-sectional SMA50 majority vote; tag each trade at entry time; surface
  per-regime breakdown in `run_backtest()` return dict and in `trades.tsv` (new `regime` column).

- **R6**: Ticker holdout — hold back a deterministic subset of tickers (controlled by
  `TICKER_HOLDOUT_FRAC`, default 0.0 = off) from all walk-forward training folds; run an
  additional `run_backtest()` on the held-out tickers only and print `ticker_holdout_pnl:` as a
  generalization check.

All three are additive. `TICKER_HOLDOUT_FRAC` defaults to `0.0` (no holdout). Bootstrap CI only
prints when `WRITE_FINAL_OUTPUTS = True`. `regime` column is always populated in `trades.tsv`
but defaults to `'unknown'` for backward compatibility. One `GOLDEN_HASH` update covers all.

## User Story

As the LLM optimization agent,
I want regime-tagged trades, ticker holdout evaluation, and bootstrap CI on the final test P&L,
So that I can distinguish genuine multi-regime alpha from ticker-specific or regime-specific fits.

## Problem Statement

After V3-A/B/C, three diagnostic gaps remain:
1. **No regime context on trades** — the agent cannot tell whether wins came from bull or bear
   conditions, making regime-conditional strategy tuning blind.
2. **All tickers always in-sample** — the agent can inadvertently tune rules that memorize
   specific tickers in the universe, inflating apparent walk-forward performance.
3. **No uncertainty estimate on final PnL** — a single point estimate on a 14-day test window
   (typically < 15 trades) may be noise; a bootstrap interval flags low-confidence results.

## Solution Statement

- **R11**: Add `detect_regime(ticker_dfs, today)` to the immutable zone. Call it at each new
  entry inside `run_backtest()`. Store `'regime'` in each position dict and trade record.
  Return `regime_stats` dict from `run_backtest()`. Add `regime` column to `_write_trades_tsv()`.

- **R6**: Add `TICKER_HOLDOUT_FRAC = 0.0` to the mutable constants block. In `__main__`,
  split `ticker_dfs` into `_train_ticker_dfs` and `_holdout_ticker_dfs` using deterministic
  sorted-order tail split. Walk-forward folds use `_train_ticker_dfs` only. After `min_test_pnl`,
  run an additional backtest on `_holdout_ticker_dfs` if non-empty.

- **R10**: Add `_bootstrap_ci(pnls, n_boot=2000, ci=0.95)` helper to the immutable zone.
  Update `_write_final_outputs()` to accept an optional `trade_records` parameter and call
  `_bootstrap_ci` when trade records are available. Update `__main__` to pass
  `_silent_stats["trade_records"]` to `_write_final_outputs()`.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (mutable constants + immutable harness),
`tests/test_optimization.py`, `program.md`
**Dependencies**: V3-A, V3-B, V3-C complete — all already done
**Breaking Changes**: No — all new constants default to off; existing behavior preserved.
`GOLDEN_HASH` must be updated after all immutable-zone changes are made.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` L1–44 — Mutable constants block; add `TICKER_HOLDOUT_FRAC` here
- `train.py` L291–299 — DO NOT EDIT boundary; all changes below this line update GOLDEN_HASH
- `train.py` L326–529 — `run_backtest()` full implementation; R11 `detect_regime` call and
  `regime_stats` accumulation go here
- `train.py` L395–417 — Screening loop (step 3) where new entries are created; add regime call
  here, store `regime` in position dict
- `train.py` L360–387 — Stop-close section; include `regime` in trade_records here
- `train.py` L432–450 — End-of-backtest close section; include `regime` in trade_records here
- `train.py` L515–529 — `run_backtest()` return dict; add `regime_stats` key here
- `train.py` L532–544 — `print_results()`; no changes needed
- `train.py` L547–586 — `_write_final_outputs()`; add `trade_records=None` param + R10
  bootstrap CI call; add `_bootstrap_ci()` helper before this function
- `train.py` L589–600 — `_write_trades_tsv()`; add `regime` to fieldnames list
- `train.py` L603–659 — `__main__` block; R6 ticker split + holdout backtest after
  `min_test_pnl`; pass `trade_records` to `_write_final_outputs()`
- `tests/test_optimization.py` L106–128 — `GOLDEN_HASH` constant; must be recomputed after
  all immutable changes
- `tests/test_optimization.py` L536–640 — Live tests that run real backtests; verify they still
  pass after adding `regime_stats` key to the return dict
- `program.md` — Update output format docs and trades.tsv schema

### New Files to Create

None — all changes are in existing files.

### Patterns to Follow

**Constant naming**: SCREAMING_SNAKE_CASE in mutable block
(see `WALK_FORWARD_WINDOWS`, `RISK_PER_TRADE`, `TICKER_HOLDOUT_FRAC`)
**Immutable zone**: Additive only — new optional parameters with backwards-compatible defaults;
no removal of existing fields from return dict
**Test naming**: `test_{feature}_{condition}()` (see `test_max_drawdown_is_non_negative`,
`test_calmar_zero_when_no_drawdown`, `test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl`)
**No random seeding beyond bootstrap**: Use `np.random.default_rng(42)` for deterministic bootstrap

---

## PARALLEL EXECUTION STRATEGY

This feature is primarily sequential: mutable constants must exist before harness code references
them, harness changes must be complete before GOLDEN_HASH can be recomputed, and tests are best
written after the harness is stable. Wave 3 (tests + hash + docs) has two parallelisable sub-tasks.

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Mutable constant (R6)                                │
│ Task 1.1: Add TICKER_HOLDOUT_FRAC constant                   │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Immutable-zone harness changes (Sequential)          │
│ Task 2.1: Add detect_regime() helper + R11 in run_backtest() │
│ Task 2.2: Add _bootstrap_ci() helper + R10 in               │
│           _write_final_outputs()                             │
│ Task 2.3: Add regime field to _write_trades_tsv()            │
│ Task 2.4: R6 ticker split + holdout backtest in __main__     │
│ Task 2.5: Pass trade_records to _write_final_outputs()       │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests + GOLDEN_HASH + program.md (Partly parallel)   │
│ Task 3.1: Write unit tests for R11 detect_regime             │
│ Task 3.2: Write unit tests for R10 bootstrap CI              │
│ Task 3.3: Write unit tests for R6 ticker holdout             │
│ Task 3.4: Recompute and update GOLDEN_HASH                   │
│ Task 3.5: Update program.md                                  │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 4: Full test suite validation                           │
│ Task 4.1: Run uv run pytest tests/test_optimization.py -v   │
└──────────────────────────────────────────────────────────────┘
```

---

## Implementation Tasks

### Wave 1 — Mutable Constant

**Task 1.1: Add TICKER_HOLDOUT_FRAC to mutable constants block**

In `train.py` at approximately L43 (after the `ROBUSTNESS_SEEDS` constant, before the blank line
separating it from `load_ticker_data`), add:

```python
# R6: Ticker holdout — fraction of tickers withheld from all training folds
# Set to 0.0 to disable; 0.2 = hold out the last-sorted 20% of the universe.
# Holdout evaluation uses BACKTEST_START..TRAIN_END (same window as training folds).
TICKER_HOLDOUT_FRAC = 0.0
```

---

### Wave 2 — Immutable-Zone Changes

All changes below are in the immutable zone (below the DO NOT EDIT boundary at L291). Any
modification here, no matter how small, requires a GOLDEN_HASH update in Wave 3.

---

**Task 2.1: Add detect_regime() helper and R11 trade attribution in run_backtest()**

**Step 2.1a — Add `detect_regime()` immediately before `_compute_avg_correlation()`** (currently
at L301):

```python
def detect_regime(ticker_dfs: dict, today) -> str:
    """
    Classify today's market regime as 'bull' or 'bear' using cross-sectional SMA50 majority vote.
    A ticker votes 'bull' if today's price_10am > its 50-day SMA (computed on history up to and
    including today). Returns 'bull' if bull_count >= bear_count, else 'bear'.
    Returns 'unknown' if fewer than 2 tickers have valid data for today.
    """
    bull_count = 0
    bear_count = 0
    for ticker, df in ticker_dfs.items():
        hist = df[df.index <= today]
        if len(hist) < 51:
            continue
        sma50 = float(hist['close'].rolling(50).mean().iloc[-1])
        price = float(hist['price_10am'].iloc[-1])
        if pd.isna(sma50) or pd.isna(price):
            continue
        if price > sma50:
            bull_count += 1
        else:
            bear_count += 1
    if bull_count + bear_count < 2:
        return 'unknown'
    return 'bull' if bull_count >= bear_count else 'bear'
```

**Step 2.1b — Update `run_backtest()` body for R11:**

In the entry opening block inside the screening loop (currently around L397–417), after computing
`portfolio[ticker] = {...}`, add the regime call and store it:

Replace the block starting at `portfolio[ticker] = {` with the following (adding `"regime"` key):

```python
_entry_regime = detect_regime(ticker_dfs, today)
portfolio[ticker] = {
    "entry_price": entry_price,
    "entry_date": today,
    "shares": shares,
    "stop_price": stop_raw,
    "stop_type": signal.get("stop_type", "unknown"),   # R5
    "ticker": ticker,
    "regime": _entry_regime,                            # R11
}
```

In the stop-close section (currently L375–384), add `"regime"` to the trade_records append:

```python
trade_records.append({
    "ticker":       ticker,
    "entry_date":   str(pos["entry_date"]),
    "exit_date":    str(prev_day),
    "days_held":    (prev_day - pos["entry_date"]).days,
    "stop_type":    pos.get("stop_type", "unknown"),
    "regime":       pos.get("regime", "unknown"),       # R11
    "entry_price":  round(pos["entry_price"], 4),
    "exit_price":   round(pos["stop_price"], 4),
    "pnl":          round(pnl, 2),
})
```

In the end-of-backtest close section (currently L440–450), add `"regime"` similarly:

```python
trade_records.append({
    "ticker":       ticker,
    "entry_date":   str(pos["entry_date"]),
    "exit_date":    str(last_day),
    "days_held":    (last_day - pos["entry_date"]).days if last_day else 0,
    "stop_type":    pos.get("stop_type", "unknown"),
    "regime":       pos.get("regime", "unknown"),       # R11
    "entry_price":  round(pos["entry_price"], 4),
    "exit_price":   round(last_price, 4),
    "pnl":          round(pnl, 2),
})
```

After the pnl_min block and just before the `return` statement, compute `regime_stats`:

```python
# R11: Per-regime trade attribution
regime_stats: dict = {}
for _rec in trade_records:
    _r = _rec.get("regime", "unknown")
    if _r not in regime_stats:
        regime_stats[_r] = {"trades": 0, "wins": 0, "pnl": 0.0}
    regime_stats[_r]["trades"] += 1
    if _rec["pnl"] > 0:
        regime_stats[_r]["wins"] += 1
    regime_stats[_r]["pnl"] = round(regime_stats[_r]["pnl"] + _rec["pnl"], 2)
```

In the `return` dict, add `"regime_stats": regime_stats` alongside the other keys.

Also update the early-exit return dict (at L345–349, the `len(trading_days) < 2` guard) to
include `"regime_stats": {}` so callers always see the key.

---

**Task 2.2: Add `_bootstrap_ci()` and update `_write_final_outputs()`**

**Step 2.2a — Add `_bootstrap_ci()` immediately before `_write_final_outputs()`** (currently
at L547):

```python
def _bootstrap_ci(pnls: list, n_boot: int = 2000, ci: float = 0.95) -> tuple:
    """
    Bootstrap CI on total P&L by resampling closed trade P&Ls with replacement.
    Uses a fixed seed (42) for deterministic output.
    Returns (p_low, p_high) percentile bounds. Returns (0.0, 0.0) if fewer than 2 trades.
    """
    if len(pnls) < 2:
        return (0.0, 0.0)
    rng = np.random.default_rng(42)
    pnl_arr = np.array(pnls, dtype=float)
    boot_totals = np.array([
        rng.choice(pnl_arr, size=len(pnl_arr), replace=True).sum()
        for _ in range(n_boot)
    ])
    alpha = (1.0 - ci) / 2.0
    return (float(np.percentile(boot_totals, 100.0 * alpha)),
            float(np.percentile(boot_totals, 100.0 * (1.0 - alpha))))
```

**Step 2.2b — Update `_write_final_outputs()` signature and body:**

Change signature from:
```python
def _write_final_outputs(ticker_dfs: dict, test_start: str, test_end: str,
                         ticker_pnl: dict) -> None:
```
to:
```python
def _write_final_outputs(ticker_dfs: dict, test_start: str, test_end: str,
                         ticker_pnl: dict, trade_records: list | None = None) -> None:
```

At the end of `_write_final_outputs()`, after the per-ticker P&L table block, add:

```python
# R10: Bootstrap CI on final test P&L
if trade_records:
    _pnls = [r["pnl"] for r in trade_records]
    _ci_low, _ci_high = _bootstrap_ci(_pnls)
    print(f"bootstrap_pnl_p05:       {_ci_low:.2f}")
    print(f"bootstrap_pnl_p95:       {_ci_high:.2f}")
```

---

**Task 2.3: Add `regime` column to `_write_trades_tsv()`**

In `_write_trades_tsv()` (currently at L589–600), update `fieldnames` to include `"regime"`:

Change:
```python
fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
              "stop_type", "entry_price", "exit_price", "pnl"]
```
to:
```python
fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
              "stop_type", "regime", "entry_price", "exit_price", "pnl"]
```

The `csv.DictWriter` will write `""` for any trade record that is missing the `regime` key
(which should not happen after Task 2.1, but is safe anyway because `DictWriter` does not raise
on missing keys by default — add `extrasaction='ignore'` if needed; prefer `restval=''`):

```python
w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", restval="")
```

---

**Task 2.4: R6 ticker split and holdout backtest in `__main__`**

In `__main__` (currently L603), immediately after `ticker_dfs = load_all_ticker_data()` and the
empty-cache guard (L604–607), add the R6 split logic:

```python
# R6: Ticker holdout — deterministic tail split of sorted ticker list
_all_tickers_sorted = sorted(ticker_dfs.keys())
_n_holdout = round(TICKER_HOLDOUT_FRAC * len(_all_tickers_sorted)) if TICKER_HOLDOUT_FRAC > 0 else 0
_holdout_set = set(_all_tickers_sorted[-_n_holdout:]) if _n_holdout > 0 else set()
_train_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t not in _holdout_set}
_holdout_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t in _holdout_set}
if not _train_ticker_dfs:
    _train_ticker_dfs = ticker_dfs  # safety: if holdout fraction is too large, fall back
```

Replace all uses of `ticker_dfs` inside the walk-forward loop (the `for _i in range(WALK_FORWARD_WINDOWS)` block) with `_train_ticker_dfs`:

- `_fold_train_stats = run_backtest(_train_ticker_dfs, ...)` (was `ticker_dfs`)
- `_fold_test_stats  = run_backtest(_train_ticker_dfs, ...)` (was `ticker_dfs`)

Note: the silent holdout (`_silent_stats`) continues to use `ticker_dfs` (full universe), as it
is a time-based holdout, not a ticker holdout.

After the `min_test_pnl` print block and before `_write_trades_tsv()`, add the holdout backtest:

```python
# R6: Evaluate on held-out tickers (generalization check)
if _holdout_ticker_dfs:
    _holdout_stats = run_backtest(_holdout_ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
    print("---")
    print(f"ticker_holdout_pnl:      {_holdout_stats['total_pnl']:.2f}")
    print(f"ticker_holdout_trades:   {_holdout_stats['total_trades']}")
```

---

**Task 2.5: Pass trade_records to _write_final_outputs()**

In the silent holdout block near the end of `__main__` (currently L652–658), update the
`_write_final_outputs()` call to pass `trade_records`:

```python
if WRITE_FINAL_OUTPUTS:
    print_results(_silent_stats, prefix="holdout_")
    _write_final_outputs(ticker_dfs, TRAIN_END, BACKTEST_END,
                         _silent_stats["ticker_pnl"],
                         _silent_stats["trade_records"])   # R10: pass trade_records
else:
    print(f"silent_pnl: HIDDEN")
```

---

### Wave 3 — Tests, GOLDEN_HASH, program.md

**Task 3.1: Write unit tests for R11 `detect_regime()`**

Add in `tests/test_optimization.py` (after existing V3-C tests, around line 900+):

```
test_detect_regime_bull_when_majority_above_sma50
    Setup: 3 tickers, all with price_10am > sma50.
    Assert: detect_regime(..., today) == 'bull'

test_detect_regime_bear_when_majority_below_sma50
    Setup: 3 tickers, 2 have price_10am < sma50.
    Assert: detect_regime(..., today) == 'bear'

test_detect_regime_unknown_when_insufficient_history
    Setup: 2 tickers with only 10 rows each (< 51 needed for SMA50).
    Assert: detect_regime(..., today) == 'unknown'

test_trade_records_include_regime_field
    Setup: synthetic ticker_dfs with >51 rows, run_backtest() with enough data to trigger
    one entry and one exit.
    Assert: all trade_records dicts have 'regime' key with value in {'bull', 'bear', 'unknown'}

test_regime_stats_in_run_backtest_return
    Setup: same synthetic data as above.
    Assert: return dict contains 'regime_stats' key; value is dict; each entry has 'trades',
    'wins', 'pnl' sub-keys; total trades across regimes == total_trades.
```

**Task 3.2: Write unit tests for R10 `_bootstrap_ci()`**

```
test_bootstrap_ci_returns_valid_bounds
    Input: [10.0, 20.0, -5.0, 15.0, 8.0] (5 trade P&Ls)
    Assert: p_low < p_high, both finite floats.

test_bootstrap_ci_fewer_than_two_trades
    Input: [] (no trades) and [5.0] (single trade)
    Assert: both return (0.0, 0.0)

test_bootstrap_ci_is_deterministic
    Input: same pnl list called twice.
    Assert: both calls return identical (p_low, p_high)

test_write_final_outputs_includes_bootstrap_lines(tmp_path, capsys)
    Setup: mock ticker_dfs, trade_records with 5 entries.
    Call _write_final_outputs with trade_records list.
    Assert: captured stdout includes 'bootstrap_pnl_p05:' and 'bootstrap_pnl_p95:' lines.

test_write_final_outputs_no_bootstrap_when_no_trade_records(tmp_path, capsys)
    Setup: call _write_final_outputs with trade_records=None.
    Assert: captured stdout does NOT include 'bootstrap_pnl_p05:'.
```

**Task 3.3: Write unit tests for R6 ticker holdout**

```
test_ticker_holdout_zero_uses_all_tickers
    Verify: with TICKER_HOLDOUT_FRAC=0.0, _n_holdout=0, _holdout_set is empty,
    _train_ticker_dfs == ticker_dfs.
    Use a monkeypatch or directly test the split logic inline.

test_ticker_holdout_fraction_splits_correctly
    Input: 5 sorted tickers ['A','B','C','D','E'], TICKER_HOLDOUT_FRAC=0.4
    Expected: _n_holdout=2, _holdout_set={'D','E'}, _train has 3 tickers.

test_ticker_holdout_deterministic
    Input: same 5 tickers, same fraction, called twice.
    Assert: holdout set is identical both times (no randomness).

test_main_outputs_ticker_holdout_pnl(monkeypatch, capsys)
    Monkeypatch TICKER_HOLDOUT_FRAC=0.5 and a synthetic ticker_dfs.
    Run __main__ block via _exec_main_block().
    Assert: stdout contains 'ticker_holdout_pnl:' line.

test_main_no_ticker_holdout_output_when_frac_zero(monkeypatch, capsys)
    Monkeypatch TICKER_HOLDOUT_FRAC=0.0 and synthetic ticker_dfs.
    Run __main__ block.
    Assert: stdout does NOT contain 'ticker_holdout_pnl:'.
```

**Task 3.4: Recompute and update GOLDEN_HASH**

After all immutable-zone changes are complete, recompute the hash with:

```bash
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# ── DO NOT EDIT BELOW THIS LINE'
print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())
"
```

Update `GOLDEN_HASH` in `tests/test_optimization.py` L118 with the new value.

**Task 3.5: Update program.md**

Make the following targeted updates to `program.md`:

1. **trades.tsv schema section** — add `regime` column after `stop_type`:
   ```
   | regime   | 'bull' / 'bear' / 'unknown' — market regime at trade entry |
   ```

2. **Output format / grep commands** — after the `ticker_holdout_pnl:` output is introduced,
   add a note that when `TICKER_HOLDOUT_FRAC > 0` the following lines appear after
   `min_test_pnl:`:
   ```
   ---
   ticker_holdout_pnl:      $X.XX
   ticker_holdout_trades:   N
   ```
   Grep command for agents: `grep "^ticker_holdout_pnl:" run.log`

3. **Keep/discard decision** — note that `ticker_holdout_pnl` is **informational only**;
   keep/discard continues to use `min_test_pnl`. If holdout PnL diverges sharply from
   walk-forward PnL (e.g., large positive `min_test_pnl` + negative `ticker_holdout_pnl`),
   flag it in the description column as a potential ticker-overfitting signal.

4. **Final run (WRITE_FINAL_OUTPUTS=True)** — note that the final output now also prints:
   ```
   bootstrap_pnl_p05:       $X.XX
   bootstrap_pnl_p95:       $X.XX
   ```
   These are 95% bootstrap CI bounds on the total holdout P&L. A narrow interval (< $50 spread)
   with < 10 trades indicates unreliable signal; interpret the point estimate cautiously.

---

### Wave 4 — Validation

**Task 4.1: Run the full test suite**

```bash
uv run pytest tests/test_optimization.py -v 2>&1 | tee test_run.log
grep -E "PASSED|FAILED|ERROR|warnings" test_run.log | tail -30
```

All tests must pass. Pre-existing failures (if any) must be confirmed pre-existing by checking
git blame — no regressions introduced by V3-D changes.

Spot-check the live tests that run actual backtests:
- `test_live_run_backtest_r7_metrics_are_finite` — should now also have `regime_stats` in return
- `test_live_walk_forward_min_test_pnl_is_finite` — unchanged
- `test_live_train_py_subprocess_outputs_pnl_min` — unchanged (subprocess run)

---

## ACCEPTANCE CRITERIA

### AC-1: R11 detect_regime correctness
- `detect_regime()` callable from the immutable zone with `(ticker_dfs: dict, today)` signature
- Returns `'bull'` when ≥ 50% of tickers with valid data have `price_10am > SMA50`
- Returns `'bear'` otherwise
- Returns `'unknown'` when < 2 tickers have valid data for the given date

### AC-2: R11 trade attribution
- All trade_records returned by `run_backtest()` include a `"regime"` key
- `run_backtest()` return dict includes `"regime_stats"` key
- `regime_stats` contains entries for each regime observed; each entry has `trades`, `wins`, `pnl`
- Sum of `regime_stats[r]["trades"]` across all `r` == `total_trades`
- `trades.tsv` contains a `regime` column (6th column after `stop_type`)

### AC-3: R10 bootstrap CI
- `_bootstrap_ci([])` and `_bootstrap_ci([x])` both return `(0.0, 0.0)`
- `_bootstrap_ci(pnls)` returns `(p_low, p_high)` with `p_low <= p_high`, both finite
- Two calls with identical input return identical output (deterministic via seed 42)
- When `WRITE_FINAL_OUTPUTS = True`, the final run output includes `bootstrap_pnl_p05:` and
  `bootstrap_pnl_p95:` lines
- When `WRITE_FINAL_OUTPUTS = False`, these lines do not appear

### AC-4: R6 ticker holdout
- `TICKER_HOLDOUT_FRAC = 0.0` (default) leaves all behavior unchanged; no `ticker_holdout_pnl:`
  line in output
- `TICKER_HOLDOUT_FRAC = 0.2` holds out the last-sorted 20% of tickers (rounded to nearest int)
- The holdout split is deterministic: same tickers → same holdout set on every run
- Walk-forward folds (and the trades.tsv) use only the training tickers when holdout is enabled
- `ticker_holdout_pnl:` and `ticker_holdout_trades:` lines appear after `min_test_pnl:` when
  holdout is non-empty
- Keep/discard decision is NOT affected by `ticker_holdout_pnl`

### AC-5: GOLDEN_HASH updated
- `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged`
  passes after the hash is recomputed and updated

### AC-6: No regressions
- All previously passing tests continue to pass
- `test_live_run_backtest_r7_metrics_are_finite` passes (regime_stats key added, does not break)
- The output format lines for all existing metrics are unchanged

---

## TEST COVERAGE SUMMARY

| Code path | Test | Tool | Run command |
|---|---|---|---|
| `detect_regime()` bull majority | `test_detect_regime_bull_when_majority_above_sma50` | pytest | `uv run pytest tests/test_optimization.py::test_detect_regime_bull_when_majority_above_sma50` |
| `detect_regime()` bear majority | `test_detect_regime_bear_when_majority_below_sma50` | pytest | `uv run pytest ...::test_detect_regime_bear_when_majority_below_sma50` |
| `detect_regime()` insufficient history | `test_detect_regime_unknown_when_insufficient_history` | pytest | `uv run pytest ...::test_detect_regime_unknown_when_insufficient_history` |
| `regime` in trade_records | `test_trade_records_include_regime_field` | pytest | `uv run pytest ...::test_trade_records_include_regime_field` |
| `regime_stats` in return dict | `test_regime_stats_in_run_backtest_return` | pytest | `uv run pytest ...::test_regime_stats_in_run_backtest_return` |
| `_bootstrap_ci()` valid bounds | `test_bootstrap_ci_returns_valid_bounds` | pytest | `uv run pytest ...::test_bootstrap_ci_returns_valid_bounds` |
| `_bootstrap_ci()` < 2 trades | `test_bootstrap_ci_fewer_than_two_trades` | pytest | `uv run pytest ...::test_bootstrap_ci_fewer_than_two_trades` |
| `_bootstrap_ci()` determinism | `test_bootstrap_ci_is_deterministic` | pytest | `uv run pytest ...::test_bootstrap_ci_is_deterministic` |
| bootstrap lines in final output | `test_write_final_outputs_includes_bootstrap_lines` | pytest | `uv run pytest ...::test_write_final_outputs_includes_bootstrap_lines` |
| no bootstrap without trade_records | `test_write_final_outputs_no_bootstrap_when_no_trade_records` | pytest | `uv run pytest ...::test_write_final_outputs_no_bootstrap_when_no_trade_records` |
| holdout frac=0 no split | `test_ticker_holdout_zero_uses_all_tickers` | pytest | `uv run pytest ...::test_ticker_holdout_zero_uses_all_tickers` |
| holdout split math | `test_ticker_holdout_fraction_splits_correctly` | pytest | `uv run pytest ...::test_ticker_holdout_fraction_splits_correctly` |
| holdout deterministic | `test_ticker_holdout_deterministic` | pytest | `uv run pytest ...::test_ticker_holdout_deterministic` |
| holdout pnl line in output | `test_main_outputs_ticker_holdout_pnl` | pytest | `uv run pytest ...::test_main_outputs_ticker_holdout_pnl` |
| no holdout line when frac=0 | `test_main_no_ticker_holdout_output_when_frac_zero` | pytest | `uv run pytest ...::test_main_no_ticker_holdout_output_when_frac_zero` |
| GOLDEN_HASH updated | `test_harness_below_do_not_edit_is_unchanged` | pytest | `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged` |

**Automated: 16 tests (100%)** — None require manual verification.

---

## VALIDATION COMMANDS

```bash
# 1. Smoke-test train.py runs at all (requires cached data)
uv run python train.py > /dev/null 2>&1 && echo "train.py OK" || echo "train.py FAILED"

# 2. Verify regime column in trades.tsv
head -3 trades.tsv

# 3. Run full test suite
uv run pytest tests/test_optimization.py -v 2>&1 | tail -20

# 4. Verify GOLDEN_HASH matches
uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v

# 5. (Optional) Verify bootstrap output with WRITE_FINAL_OUTPUTS=True
# Temporarily set WRITE_FINAL_OUTPUTS = True in train.py, run:
#   uv run python train.py 2>&1 | grep "bootstrap"
# Then restore to False.
```
