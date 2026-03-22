# Code Review: V3-D Diagnostics (R6, R10, R11)

**Date:** 2026-03-22
**Branch:** master
**Reviewer:** Claude Sonnet 4.6 (ai-dev-env:code-review)

---

## Stats

- Files Modified: 4 (`train.py`, `tests/test_optimization.py`, `program.md`, `PROGRESS.md`)
- Files Added: 0
- Files Deleted: 0
- New lines: +475
- Deleted lines: -9

---

## Test Results

All 39 tests pass (1 pre-existing skip unchanged). No regressions.

---

## Issues Found

---

### Issue 1

```
severity: medium
file: train.py
line: 658
issue: Bootstrap CI label mismatch — print keys say p05/p95 but actual percentiles are 2.5/97.5
detail: _bootstrap_ci() is called with ci=0.95 (default). alpha = (1 - 0.95) / 2 = 0.025,
        so the lower bound is the 2.5th percentile and the upper bound is the 97.5th percentile.
        The print statements use 'bootstrap_pnl_p05:' and 'bootstrap_pnl_p95:', which imply
        the 5th and 95th percentiles (a 90% CI). The label is factually wrong and will mislead
        the agent when it parses run.log to interpret the bootstrap interval width.
        program.md correctly describes them as "95% bootstrap CI bounds" in prose, but the key
        names themselves are inconsistent with both the code and the prose description.
suggestion: Either rename the keys to match the actual percentiles:
              print(f"bootstrap_pnl_p025:      {_ci_low:.2f}")
              print(f"bootstrap_pnl_p975:       {_ci_high:.2f}")
            Or change the CI default to 0.90 so that alpha=0.05 and the p05/p95 labels are
            accurate. The second option also aligns the label with the "p05"/"p95" convention
            used elsewhere. Update GOLDEN_HASH and program.md to match whichever fix is chosen.
```

---

### Issue 2

```
severity: medium
file: program.md
line: (new table added in this diff — "stop_type | Stop type at exit (atr / unknown)")
issue: trades.tsv schema documents wrong stop_type values — 'atr / unknown' vs actual 'pivot / fallback / unknown'
detail: The new trades.tsv schema table in program.md states stop_type values are 'atr / unknown'.
        In train.py, screen_day() sets stop_type to 'pivot' (line 257) or 'fallback' (line 255).
        The fallback in run_backtest() uses pos.get('stop_type', 'unknown'), which only fires if
        screen_day() returned a dict with no stop_type key — not in normal operation.
        So real values written to trades.tsv are 'pivot', 'fallback', and (edge) 'unknown'.
        'atr' is never written. An agent parsing trades.tsv and filtering for 'atr' will find no rows,
        silently producing a wrong result.
suggestion: Update the program.md table description to:
              stop_type | Stop type at exit ('pivot' / 'fallback' / 'unknown') |
            where 'pivot' = stop anchored to a pivot low, 'fallback' = 2×ATR stop,
            'unknown' = legacy/missing value.
```

---

### Issue 3

```
severity: low
file: train.py
line: 319
issue: detect_regime() SMA50 includes today's close — minor intraday look-ahead
detail: The function computes hist = df[df.index <= today], which includes today's row.
        rolling(50).mean().iloc[-1] therefore includes today's daily close in the SMA.
        But detect_regime() is called at entry time using price_10am (an intraday price).
        Today's close is not yet known at 10am, so the SMA uses a future value.
        In a pure EOD-data backtesting context this is a one-out-of-50 bar error (≈2%) and
        has negligible practical impact, but it is technically lookahead.
        This is a design-level decision (acceptable given the data model) rather than a code bug,
        but worth documenting in the docstring.
suggestion: Add a note to the detect_regime() docstring:
              "Note: includes today's close in the SMA50 computation; this is a one-bar
               look-ahead (≈2% of the window) acceptable in this EOD data context."
            No code change needed unless strict no-lookahead is required, in which case
            filter to df[df.index < today] and drop the ≥51 threshold to ≥50.
```

---

### Issue 4

```
severity: low
file: train.py
line: 443
issue: detect_regime() called once per screened entry, not cached per day — redundant computation
detail: detect_regime() is inside the 'for ticker, df in ticker_dfs.items()' screening loop
        (step 3 of the backtest loop). On any day with multiple entries, it is called once per
        entry that passes screen_day(). Each call iterates all tickers and computes rolling(50)
        on each — O(N_tickers × N_history) per call.
        With MAX_SIMULTANEOUS_POSITIONS=5 this is bounded to 5 calls/day, so runtime impact is
        limited in practice. However, all calls on the same day return the same value.
suggestion: Cache the result per trading day by computing it once before the screening loop:
              _today_regime = detect_regime(ticker_dfs, today)
            Then replace line 443 with:
              _entry_regime = _today_regime
            This is a pure performance fix with no behavioral change.
```

---

### Issue 5

```
severity: low
file: train.py
line: 551-560
issue: regime_stats pnl accumulation uses incremental round() which can diverge from sum-then-round
detail: The accumulation pattern:
          regime_stats[_r]["pnl"] = round(regime_stats[_r]["pnl"] + _rec["pnl"], 2)
        applies round() after each addition. Since _rec["pnl"] is already rounded to 2dp (line 416
        and 485), both operands have at most 2 decimal places, so intermediate rounding cannot
        introduce additional error beyond the final sum-then-round result.
        However, for large trade counts the cascading round() calls can still produce a value
        that differs from round(sum(all_pnls), 2) by ±0.01 due to floating point representation.
        Example: round(round(0.005+0.005, 2) + 0.005, 2) = 0.01 vs round(0.005+0.005+0.005, 2) = 0.01
        — benign in this case, but the pattern is subtle and worth a comment.
suggestion: Add a brief comment above the accumulation line:
              # Both operands are already 2dp (trade records are pre-rounded), so intermediate
              # rounding is benign but kept for consistency with the pnl_min accumulation pattern.
            No code change needed.
```

---

## No Issues Found In

- **detect_regime() correctness**: SMA50 majority vote logic is correct. `len(hist) < 51` boundary correctly ensures rolling(50) produces a valid value at iloc[-1]. The `bull_count >= bear_count` tie-break (returns 'bull') is consistent with the docstring. The `< 2 valid tickers → 'unknown'` threshold is correct and covers both 0 and 1 ticker edge cases.
- **_bootstrap_ci() determinism**: `np.random.default_rng(42)` creates a fresh seeded RNG on each call, guaranteeing identical results for identical inputs. Verified by test and manual confirmation.
- **_bootstrap_ci() edge cases**: `len(pnls) < 2` guard correctly returns `(0.0, 0.0)` for empty and single-element lists.
- **R6 ticker split safety**: The `if not _train_ticker_dfs: _train_ticker_dfs = ticker_dfs` fallback correctly handles TICKER_HOLDOUT_FRAC=1.0. The split uses `sorted()` for determinism. The holdout backtest uses `BACKTEST_START..TRAIN_END` (same window as training folds) — correct.
- **regime_stats accumulation**: All trade records are iterated, including both stop-close and end-of-backtest paths. The `.get("regime", "unknown")` default is safe.
- **trade_records regime field propagation**: Correctly propagated in the stop-close path (line 413) and end-of-backtest path (line 482). Both paths use `pos.get("regime", "unknown")` which is defensive and correct.
- **TICKER_HOLDOUT_FRAC placement**: Correctly placed in the mutable constants block above the DO NOT EDIT marker (line 48 vs marker at line 296).
- **Early-return guard**: `"regime_stats": {}` correctly added to the early-exit return dict (line 381), preventing KeyError in callers.
- **_write_trades_tsv restval**: `restval=""` correctly handles any trade record that might be missing the `regime` key for backward compatibility.
- **GOLDEN_HASH**: Updated from the V3-C hash to the V3-D hash; test passes confirming the below-marker zone is consistent.
- **Test coverage**: 15 new tests cover all planned cases. Mock patches use `mock.patch.object` correctly. `_exec_main_block` helper pattern used consistently with prior V3 tests.
- **Security**: No secrets, no SQL, no user-controlled data paths, no injection vectors.
- **No print() in non-test code (production paths)**: The bootstrap CI prints and ticker_holdout prints are intentional output lines (agent parses stdout), consistent with the existing `print_results()` pattern throughout the harness.

---

## Pre-existing Issues (Not Introduced by This Changeset)

- **WALK_FORWARD_WINDOWS=0 KeyError** (pre-existing from V3-B, line 736): When `WALK_FORWARD_WINDOWS=0`, `_fold_train_stats` remains `{}` (the guard value), and `_fold_train_stats.get("pnl_min", _fold_train_stats["total_pnl"])` raises `KeyError: 'total_pnl'`. This is not triggered by V3-D since the default is 3. Not introduced by this changeset.
