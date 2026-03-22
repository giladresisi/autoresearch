# Feature: V3-C Portfolio Robustness Controls (R8, R9-price-only)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Add two optional robustness controls to `run_backtest()`, each gated by a mutable-section constant so the LLM agent can tune them during optimization runs:

- **R8**: Position cap (hard limit on simultaneous positions) + correlation penalty (discounts the reported PnL for concentrated, correlated portfolios)
- **R9** (price only): Robustness perturbation — re-runs the backtest with ±0.5% entry prices and ±0.3 ATR stop offsets to surface strategies that are fragile to small fill deviations. Reports `pnl_min`; annotates `trades.tsv`; introduces `discard-fragile` status.

Both controls default to off (constants = 0) so existing behavior is fully preserved.

## User Story

As the LLM optimization agent,
I want optional position-cap and robustness-perturbation controls in the harness,
So that I can avoid overfitting to concentrated macro bets and detect stop-placement fragility before committing to a strategy.

## Problem Statement

Two structural weaknesses remain after V3-A/B:
1. The screener may simultaneously hold 10+ correlated longs — a leveraged sector bet, not diversified alpha.
2. A strategy can look profitable in nominal backtests yet collapse with tiny fill deviations (e.g., ±0.5% on entry price), indicating fragile stop placement rather than genuine edge.

## Solution Statement

- **R8**: Gate new entries in `run_backtest()` when `len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS`. Optionally subtract a correlation penalty from the reported PnL metric.
- **R9**: When `ROBUSTNESS_SEEDS > 0`, `run_backtest()` reruns N-1 additional times with perturbed entry prices / stop levels and exposes `pnl_min` in the output. `trades.tsv` gets an annotation header. Agent logs `discard-fragile` when nominal PnL > 0 but `pnl_min < 0`.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (mutable constants + immutable harness), `program.md`, `tests/test_optimization.py`
**Dependencies**: V3-A complete (sizing fix), V3-B complete (walk-forward framework) — both are already done
**Breaking Changes**: No — all new constants default to off; existing behavior preserved. `GOLDEN_HASH` must be updated.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (L1–37) — Mutable constants block; add new R8/R9 constants here
- `train.py` (L284–292) — DO NOT EDIT boundary marker comment; harness starts here
- `train.py` (L294–466) — `run_backtest()` full implementation; all R8/R9 harness changes go here
- `train.py` (L363–381) — Screening loop where R8 position cap guard inserts (step 3 block)
- `train.py` (L469–480) — `print_results()` — add `pnl_min:` line here
- `train.py` (L525–533) — `_write_trades_tsv()` — add annotation param and comment header
- `train.py` (L536–589) — `__main__` block — update `_write_trades_tsv` call with annotation
- `tests/test_optimization.py` (L115–128) — `GOLDEN_HASH` constant; must be recomputed after all immutable changes
- `program.md` (L179–230) — Logging results and experiment loop sections to update
- `prd.md` (L948–977) — V3-C specification including contradiction resolutions C3 and C4

### New Files to Create

None — all changes are in existing files.

### Patterns to Follow

**Constant naming**: SCREAMING_SNAKE_CASE in mutable block (see `WALK_FORWARD_WINDOWS`, `RISK_PER_TRADE`, `SILENT_END`)
**Immutable zone**: Additive only — new parameters with backwards-compatible defaults; no removal of existing fields from return dict
**Test naming**: `test_{feature}_{condition}()` (see `test_max_drawdown_is_non_negative`, `test_calmar_zero_when_no_drawdown`)

---

## PARALLEL EXECUTION STRATEGY

This feature is primarily sequential because each change builds on the previous:
- Mutable constants must exist before harness code references them
- Harness changes must be complete before GOLDEN_HASH can be recomputed
- Tests can be written incrementally as functions are updated

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Mutable constants + harness core (Sequential)        │
│ Task 1.1: Add mutable constants (R8+R9)                      │
│ Task 1.2: Update run_backtest() (R8 cap + R9 perturbation)   │
│ Task 1.3: Update print_results() + _write_trades_tsv()       │
│ Task 1.4: Update __main__ block                              │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests + GOLDEN_HASH + program.md (After Wave 1)      │
│ Task 2.1: Write new unit tests for R8                        │
│ Task 2.2: Write new unit tests for R9                        │
│ Task 2.3: Recompute and update GOLDEN_HASH                   │
│ Task 2.4: Update program.md                                  │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Full test suite validation                           │
│ Task 3.1: Run pytest and confirm all tests pass              │
└──────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Sequential**: All train.py changes in dependency order (constants → run_backtest → print_results → __main__)
**Wave 2 — Parallel**: Tasks 2.1, 2.2, 2.3, 2.4 can run concurrently after Wave 1
**Wave 3 — Sequential**: Full pytest run after all changes

---

## IMPLEMENTATION PLAN

### Phase 1: Mutable Constants

**Task 1.1**: Add three new constants to `train.py` mutable section (above the DO NOT EDIT line), after the existing `RISK_PER_TRADE` constant:

```python
# R8: Position concentration controls
MAX_SIMULTANEOUS_POSITIONS = 5     # cap on open positions at any time (set to large int to disable)
CORRELATION_PENALTY_WEIGHT = 0.0   # penalty factor for correlated portfolios (0 = off)

# R9: Robustness perturbation (price/stop jitter)
ROBUSTNESS_SEEDS = 0               # 0 = off; 5 = recommended (runs 4 perturbed seeds + nominal)
```

These go after `RISK_PER_TRADE = 50.0` and before `def load_ticker_data`.

### Phase 2: run_backtest() Changes

**Task 1.2**: Update `run_backtest()` in the immutable zone with the following changes. Read the full function first before editing.

#### 2a: Add perturbation parameters to signature

Change the signature from:
```python
def run_backtest(ticker_dfs: dict, start: str | None = None, end: str | None = None) -> dict:
```
to:
```python
def run_backtest(ticker_dfs: dict, start: str | None = None, end: str | None = None,
                 _price_bias: float = 0.0, _stop_atr_bias: float = 0.0) -> dict:
```
These private parameters are used by the internal perturbation loop; callers always use defaults.

#### 2b: R8 — Position cap guard in screening loop

In the screening loop (step 3, around L363), BEFORE the `if ticker in portfolio: continue` check, add:
```python
        # 3. Screen for new entries
        for ticker, df in ticker_dfs.items():
            if ticker in portfolio:
                continue
            if len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS:   # R8: position cap
                continue
```
This caps simultaneous open positions at `MAX_SIMULTANEOUS_POSITIONS`.

#### 2c: R9 — Apply perturbation biases at entry

In the entry creation (step 3, after `if signal is None: continue`), update the entry_price and stop computation to apply biases:

```python
            entry_price = (signal["entry_price"] + 0.03) * (1 + _price_bias)
            stop_raw    = signal["stop"] + _stop_atr_bias * signal.get("atr14", 0.0)
            risk = entry_price - stop_raw
            shares = RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price
            portfolio[ticker] = {
                "entry_price": entry_price,
                "entry_date": today,
                "shares": shares,
                "stop_price": stop_raw,
                "stop_type": signal.get("stop_type", "unknown"),
                "ticker": ticker,
            }
```
When `_price_bias == 0.0` and `_stop_atr_bias == 0.0` (the default), this is identical to the current behavior.

#### 2d: R8 — Correlation penalty

After computing `total_pnl` (around the current line that does `total_pnl = float(sum(trades))`), add the correlation penalty block:

```python
    # R8: Correlation penalty — discounts PnL for concentrated correlated portfolios
    if CORRELATION_PENALTY_WEIGHT > 0.0 and len(ticker_pnl) >= 2:
        _traded_tickers = list(ticker_pnl.keys())
        _avg_corr = _compute_avg_correlation(ticker_dfs, _traded_tickers, s, e)
        total_pnl = round(total_pnl - CORRELATION_PENALTY_WEIGHT * _avg_corr * total_pnl, 2)
```

Add the helper `_compute_avg_correlation()` to the immutable zone, just before `run_backtest()`:

```python
def _compute_avg_correlation(ticker_dfs: dict, tickers: list, start, end) -> float:
    """
    Mean pairwise Pearson correlation of daily price_10am returns for the given tickers
    over [start, end). Returns 0.0 if fewer than 2 tickers have data.
    """
    series = []
    for t in tickers:
        if t not in ticker_dfs:
            continue
        df = ticker_dfs[t]
        sub = df[(df.index >= start) & (df.index < end)]["price_10am"].dropna()
        if len(sub) >= 2:
            series.append(sub.pct_change().dropna())
    if len(series) < 2:
        return 0.0
    # Align on common index
    aligned = pd.concat(series, axis=1).dropna()
    if aligned.shape[0] < 2 or aligned.shape[1] < 2:
        return 0.0
    corr_matrix = aligned.corr().values
    n = corr_matrix.shape[0]
    off_diag = [(corr_matrix[i, j]) for i in range(n) for j in range(i + 1, n)]
    return float(np.mean(off_diag)) if off_diag else 0.0
```

#### 2e: R9 — Perturbation loop and pnl_min

At the end of `run_backtest()`, just before the `return {` statement, add the perturbation loop:

```python
    # R9: Robustness perturbation — run additional seeds with jittered entries/stops
    # Only run from the nominal call (both biases == 0) to avoid infinite recursion.
    _PERTURBATION_VECTORS = [
        (-0.005, -0.3), (-0.005, +0.3),
        (+0.005, -0.3), (+0.005, +0.3),
    ]
    pnl_min = total_pnl
    if ROBUSTNESS_SEEDS > 0 and _price_bias == 0.0 and _stop_atr_bias == 0.0:
        all_pnls = [total_pnl]
        n_extra = min(ROBUSTNESS_SEEDS - 1, len(_PERTURBATION_VECTORS))
        for _pb, _sb in _PERTURBATION_VECTORS[:n_extra]:
            _perturbed = run_backtest(ticker_dfs, start=start, end=end,
                                      _price_bias=_pb, _stop_atr_bias=_sb)
            all_pnls.append(_perturbed["total_pnl"])
        pnl_min = round(min(all_pnls), 2)
```

Add `"pnl_min": pnl_min` to the return dict.

### Phase 3: print_results() and _write_trades_tsv()

**Task 1.3a**: In `print_results()`, add a `pnl_min:` line after `pnl_consistency:`:

```python
    print(f"{prefix}pnl_min:             {stats.get('pnl_min', stats['total_pnl']):.2f}")
```

**Task 1.3b**: Update `_write_trades_tsv()` signature and body to accept an optional annotation:

```python
def _write_trades_tsv(trade_records: list, annotation: str | None = None) -> None:
    """Write per-trade records to trades.tsv (tab-separated). Overwrites each run.
    If annotation is provided, writes it as a comment line before the header row."""
    import csv
    fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
                  "stop_type", "entry_price", "exit_price", "pnl"]
    with open("trades.tsv", "w", newline="", encoding="utf-8") as f:
        if annotation:
            f.write(f"# {annotation}\n")
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        w.writeheader()
        w.writerows(trade_records)
```

### Phase 4: __main__ Update

**Task 1.4**: In the `__main__` block, update the `_write_trades_tsv` call to pass the annotation when R9 is active:

After computing `last_fold_train_records` and its corresponding stats, capture the `pnl_min` for the annotation:

```python
    # Write trades.tsv from the most recent training fold
    _last_fold_pnl_min = _fold_train_stats.get("pnl_min", _fold_train_stats["total_pnl"])
    _annotation = f"pnl_min: ${_last_fold_pnl_min:.2f}" if ROBUSTNESS_SEEDS > 0 else None
    _write_trades_tsv(last_fold_train_records, annotation=_annotation)
```

Replace the current `_write_trades_tsv(last_fold_train_records)` call with the above.

---

## STEP-BY-STEP TASKS

### WAVE 1: Harness Implementation

#### Task 1.1: ADD mutable constants to train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [1.2, 1.3, 1.4]
- **PROVIDES**: `MAX_SIMULTANEOUS_POSITIONS`, `CORRELATION_PENALTY_WEIGHT`, `ROBUSTNESS_SEEDS` constants readable by harness
- **IMPLEMENT**: Insert three new constants after `RISK_PER_TRADE = 50.0` (around L36), before `def load_ticker_data`. See Phase 1 above for exact values.
- **PATTERN**: `train.py:27–37` (existing mutable constants block)
- **VALIDATE**: `python -c "import train; print(train.MAX_SIMULTANEOUS_POSITIONS, train.CORRELATION_PENALTY_WEIGHT, train.ROBUSTNESS_SEEDS)"`

#### Task 1.2: UPDATE run_backtest() in train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [1.3, 1.4, 2.1, 2.2, 2.3]
- **PROVIDES**: Updated `run_backtest()` with R8 cap, R8 correlation penalty, R9 perturbation loop, `pnl_min` in return dict
- **IMPLEMENT**: Apply changes 2a through 2e in sequence (see Phase 2 above). Add `_compute_avg_correlation()` helper before `run_backtest()`. Ensure `pnl_min` is in the return dict at all code paths (including early-exit path for < 2 trading days).
- **PATTERN**: `train.py:294–466` (existing run_backtest implementation)
- **VALIDATE**: `python -c "import train; r = train.run_backtest({}); print('pnl_min' in r, r['pnl_min'])"`

#### Task 1.3: UPDATE print_results() and _write_trades_tsv() in train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [2.1, 2.2, 2.3]
- **PROVIDES**: `print_results()` emits `pnl_min:` line; `_write_trades_tsv()` accepts optional annotation
- **IMPLEMENT**: See Phase 3 above (Tasks 1.3a and 1.3b).
- **PATTERN**: `train.py:469–480` (print_results), `train.py:525–533` (_write_trades_tsv)
- **VALIDATE**: `python -c "import train; train.print_results({'sharpe':0,'total_trades':0,'win_rate':0,'avg_pnl_per_trade':0,'total_pnl':10,'backtest_start':'2026-01-01','backtest_end':'2026-03-01','calmar':0,'pnl_consistency':0,'pnl_min':8})"`  — must print `pnl_min: 8.00`

#### Task 1.4: UPDATE __main__ block in train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.3]
- **BLOCKS**: [2.1, 2.2, 2.3]
- **PROVIDES**: `__main__` calls `_write_trades_tsv` with annotation, passes R9 `pnl_min` annotation when active
- **IMPLEMENT**: See Phase 4 above. Replace the bare `_write_trades_tsv(last_fold_train_records)` call.
- **PATTERN**: `train.py:536–589` (existing __main__ block)
- **VALIDATE**: Syntax check: `python -c "import ast; ast.parse(open('train.py').read()); print('OK')"`

**Wave 1 Checkpoint**: `python -c "import train; r = train.run_backtest({}); print(all(k in r for k in ['pnl_min', 'max_drawdown', 'calmar', 'pnl_consistency']))"`

---

### WAVE 2: Tests, GOLDEN_HASH, program.md

#### Task 2.1: ADD R8 unit tests to test_optimization.py

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [3.1]
- **IMPLEMENT**: Add the following tests after the existing V3-B tests:

**Test: test_position_cap_limits_simultaneous_positions**
```
Patch MAX_SIMULTANEOUS_POSITIONS = 2.
Use _make_rising_dataset() with 3 tickers (SYNTH1, SYNTH2, SYNTH3 — all same data, same screener fires).
Patch screen_day to always return a signal with stop = price - 10.
Run run_backtest().
Assert: at no point in the portfolio dict did len(portfolio) exceed 2.
Approach: use mock.patch.object(train, 'MAX_SIMULTANEOUS_POSITIONS', 2).
Track max positions via a counting wrapper.
```

**Test: test_position_cap_does_not_fire_when_unlimited**
```
Patch MAX_SIMULTANEOUS_POSITIONS = 1000 (effectively unlimited).
Use a 3-ticker dataset.
Verify that at least 1 trade occurs (screen_day always returns signal).
This guards against the cap accidentally firing at default limit.
```

**Test: test_correlation_penalty_reduces_pnl_when_positive**
```
Patch CORRELATION_PENALTY_WEIGHT = 0.5.
Use _make_rising_dataset() with 2 tickers of identical price data (perfectly correlated).
screen_day always returns a signal; manage_position no-op.
Run run_backtest() and compare total_pnl to a run with CORRELATION_PENALTY_WEIGHT = 0.0.
Assert: penalized total_pnl < unpenalized total_pnl (when both are positive).
```

**Test: test_correlation_penalty_zero_when_weight_is_zero**
```
Run run_backtest() with CORRELATION_PENALTY_WEIGHT = 0.0 (default) on rising data.
Run the same with CORRELATION_PENALTY_WEIGHT = 0.5.
Capture total_pnl of both runs.
Assert: weight=0 run has higher total_pnl than weight=0.5 run (assuming profitable run).
```

- **VALIDATE**: `uv run pytest tests/test_optimization.py::test_position_cap_limits_simultaneous_positions tests/test_optimization.py::test_position_cap_does_not_fire_when_unlimited tests/test_optimization.py::test_correlation_penalty_reduces_pnl_when_positive tests/test_optimization.py::test_correlation_penalty_zero_when_weight_is_zero -v`

#### Task 2.2: ADD R9 unit tests to test_optimization.py

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2, 1.3, 1.4]
- **BLOCKS**: [3.1]
- **IMPLEMENT**: Add the following tests:

**Test: test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl**
```
Run run_backtest() with ROBUSTNESS_SEEDS = 0 (default).
Assert: stats["pnl_min"] == stats["total_pnl"]
This verifies the default no-op behavior.
```

**Test: test_robustness_seeds_nonzero_returns_pnl_min**
```
Patch ROBUSTNESS_SEEDS = 3.
Use _make_rising_dataset() with screen_day always-enter and manage_position no-op.
Run run_backtest().
Assert: "pnl_min" in stats and isinstance(stats["pnl_min"], float).
Assert: stats["pnl_min"] <= stats["total_pnl"]  (worst seed <= nominal).
```

**Test: test_print_results_includes_pnl_min_line (capsys)**
```
Construct a stats dict with "pnl_min": 42.00.
Call train.print_results(stats).
Assert: "pnl_min:" in captured.out and "42.00" in captured.out.
```

**Test: test_write_trades_tsv_annotation_header (tmp_path)**
```
Call _write_trades_tsv([], annotation="pnl_min: $25.50").
Read trades.tsv.
Assert first line == "# pnl_min: $25.50".
Assert second line starts with "ticker\t" (the TSV header row).
```

**Test: test_write_trades_tsv_no_annotation_when_none (tmp_path)**
```
Call _write_trades_tsv([], annotation=None).
Read trades.tsv first line.
Assert it starts with "ticker" (no comment line).
```

- **VALIDATE**: `uv run pytest tests/test_optimization.py::test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl tests/test_optimization.py::test_robustness_seeds_nonzero_returns_pnl_min tests/test_optimization.py::test_print_results_includes_pnl_min_line tests/test_optimization.py::test_write_trades_tsv_annotation_header tests/test_optimization.py::test_write_trades_tsv_no_annotation_when_none -v`

#### Task 2.3: RECOMPUTE and UPDATE GOLDEN_HASH in test_optimization.py

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.2, 1.3, 1.4]
- **BLOCKS**: [3.1]
- **IMPLEMENT**: After all immutable-zone changes are done, run the recompute command and update the constant:

```bash
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# \u2500\u2500 DO NOT EDIT BELOW THIS LINE'
below = s.partition(m)[2]
print(hashlib.sha256(below.encode('utf-8')).hexdigest())
"
```

Update `GOLDEN_HASH` in `tests/test_optimization.py` line ~118 with the new hash value.

- **VALIDATE**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` — must pass

#### Task 2.4: UPDATE program.md

- **WAVE**: 2
- **AGENT_ROLE**: documentation-specialist
- **DEPENDS_ON**: [1.3, 1.4]
- **BLOCKS**: []
- **IMPLEMENT**:

**2.4a**: In the "Logging results" section, add `discard-fragile` as a fourth valid status value. Update the column definitions for `status`:

```
10. `status`: `keep`, `discard`, `crash`, or `discard-fragile`
    - `discard-fragile`: nominal `min_test_pnl > 0` but at least one fold's `pnl_min < 0` (strategy
      collapses with small fill deviations); revert just like `discard`.
```

Update the example table to include a `discard-fragile` row:
```
e5f6g7h	8.00	300.00	30.00	2.100000	12	0.583	3.0000	50.00	discard-fragile	fragile stops — pnl_min -12.00
```

**2.4b**: In the experiment loop section (step 8 and related), add after the keep/discard instruction:

```
If `ROBUSTNESS_SEEDS > 0`: also check whether any fold's `pnl_min:` line is negative while
`min_test_pnl > 0`. If so, log status `discard-fragile` and revert (`git reset --hard HEAD~1`).
To diagnose which trades are fragile, read `trades.tsv` — the first line is a comment
`# pnl_min: $X.XX` showing the worst-case train-fold P&L under perturbation.
```

**2.4c**: In the Output format section, add `pnl_min:` to each fold block and update the grep commands:

In the fold output example, after `pnl_consistency:` lines add:
```
fold1_train_pnl_min:         95.00
...
fold1_test_pnl_min:          18.00
```

Add grep commands:
```bash
grep "^fold3_train_pnl_min:" run.log
grep "^fold3_test_pnl_min:" run.log
```

**2.4d**: In the "What you CAN do" section, add:
```
- Tune `MAX_SIMULTANEOUS_POSITIONS`, `CORRELATION_PENALTY_WEIGHT`, and `ROBUSTNESS_SEEDS`
  in the mutable constants block to control position concentration and stop fragility.
```

- **VALIDATE**: Manual read — confirm `discard-fragile` appears in status definitions, loop instructions reference `trades.tsv` annotation, and grep commands include pnl_min lines.

**Wave 2 Checkpoint**: `uv run pytest tests/test_optimization.py -v --tb=short`

---

### WAVE 3: Full Validation

#### Task 3.1: RUN full test suite and verify

- **WAVE**: 3
- **AGENT_ROLE**: qa-engineer
- **DEPENDS_ON**: [2.1, 2.2, 2.3, 2.4]
- **IMPLEMENT**: Run the full test suite and confirm all tests pass (or skip for known pre-existing reasons).
- **VALIDATE**:
  ```bash
  uv run pytest tests/test_optimization.py -v --tb=short 2>&1 | tail -30
  ```
  Expected: all tests pass. The 1 pre-existing git-state skip is acceptable.

**Final Checkpoint**: All tests pass, including:
- `test_harness_below_do_not_edit_is_unchanged` (GOLDEN_HASH updated)
- `test_position_cap_limits_simultaneous_positions`
- `test_correlation_penalty_reduces_pnl_when_positive`
- `test_robustness_seeds_nonzero_returns_pnl_min`
- `test_write_trades_tsv_annotation_header`
- All pre-existing V3-A and V3-B tests

---

## TESTING STRATEGY

### Unit Tests (all automated with pytest)

| Test | Feature | Status | File | Run Command |
|---|---|---|---|---|
| test_position_cap_limits_simultaneous_positions | R8 cap | ✅ | test_optimization.py | pytest ...::test_position_cap_limits... |
| test_position_cap_does_not_fire_when_unlimited | R8 cap (regression) | ✅ | test_optimization.py | pytest ...::test_position_cap_does_not_fire... |
| test_correlation_penalty_reduces_pnl_when_positive | R8 penalty | ✅ | test_optimization.py | pytest ...::test_correlation_penalty_reduces... |
| test_correlation_penalty_zero_when_weight_is_zero | R8 penalty (regression) | ✅ | test_optimization.py | pytest ...::test_correlation_penalty_zero... |
| test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl | R9 off | ✅ | test_optimization.py | pytest ...::test_robustness_seeds_zero... |
| test_robustness_seeds_nonzero_returns_pnl_min | R9 active | ✅ | test_optimization.py | pytest ...::test_robustness_seeds_nonzero... |
| test_print_results_includes_pnl_min_line | R9 output | ✅ | test_optimization.py | pytest ...::test_print_results_includes_pnl_min... |
| test_write_trades_tsv_annotation_header | R9 trades.tsv | ✅ | test_optimization.py | pytest ...::test_write_trades_tsv_annotation... |
| test_write_trades_tsv_no_annotation_when_none | R9 regression | ✅ | test_optimization.py | pytest ...::test_write_trades_tsv_no_annotation... |

All tests use `mock.patch.object(train, ...)` to isolate from module-level constants.
Tests that write files use `tmp_path` or `os.chdir` fixtures as needed; since trades.tsv is written to CWD, use `os.chdir(tmp_path)` in the fixture.

### Edge Cases

- **R8 with 0 positions ever opened**: ✅ cap guard only checks `len(portfolio)`, which starts at 0 — no effect
- **R9 with ROBUSTNESS_SEEDS = 1**: ✅ n_extra = min(0, 4) = 0 perturbed seeds; pnl_min = total_pnl (nominal only)
- **R9 with empty ticker_dfs**: ✅ early-exit path in run_backtest returns pnl_min = 0.0
- **CORRELATION_PENALTY_WEIGHT > 0 with only 1 traded ticker**: ✅ `_compute_avg_correlation` returns 0.0, no penalty
- **pnl_min key in all early-exit paths**: ✅ early exit dict at L312–316 must include `pnl_min: 0.0`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 9 new | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total new** | 9 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax & Import

```bash
python -c "import ast; ast.parse(open('train.py', encoding='utf-8').read()); print('syntax OK')"
python -c "import train; print('import OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_optimization.py -v --tb=short
```

### Level 3: Integration — verify new constants accessible

```bash
python -c "import train; print('R8:', train.MAX_SIMULTANEOUS_POSITIONS, train.CORRELATION_PENALTY_WEIGHT); print('R9:', train.ROBUSTNESS_SEEDS)"
```

### Level 4: Integration — run_backtest with new features

```bash
# R8: position cap returns pnl_min key
python -c "import train; r = train.run_backtest({}); print('pnl_min key present:', 'pnl_min' in r); print('default pnl_min:', r['pnl_min'])"

# R9: verify pnl_min == total_pnl when seeds=0
python -c "import train; r = train.run_backtest({}); assert r['pnl_min'] == r['total_pnl'], f'mismatch: {r}'; print('R9 off: pnl_min == total_pnl OK')"
```

### Level 5: GOLDEN_HASH verification

```bash
uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `MAX_SIMULTANEOUS_POSITIONS = 5`, `CORRELATION_PENALTY_WEIGHT = 0.0`, and `ROBUSTNESS_SEEDS = 0` appear in `train.py`'s mutable section (above the DO NOT EDIT line)
- [ ] `run_backtest()` skips a new entry when `len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS`, so no more than `MAX_SIMULTANEOUS_POSITIONS` positions are ever open simultaneously
- [ ] `run_backtest()` returns `"pnl_min"` in the stats dict on all code paths, including the early-exit path for < 2 trading days
- [ ] When `ROBUSTNESS_SEEDS == 0` (default), `stats["pnl_min"] == stats["total_pnl"]` — no overhead, no change to existing behavior
- [ ] When `ROBUSTNESS_SEEDS > 0`, `stats["pnl_min"] <= stats["total_pnl"]` (worst perturbed seed ≤ nominal)
- [ ] When `CORRELATION_PENALTY_WEIGHT > 0.0` and ≥ 2 tickers traded, `total_pnl` in the return dict is reduced by `CORRELATION_PENALTY_WEIGHT × avg_pairwise_correlation × total_pnl`
- [ ] When `CORRELATION_PENALTY_WEIGHT == 0.0` (default), `total_pnl` is identical to the unpenalized value
- [ ] `print_results()` emits a `{prefix}pnl_min:` line parseable by `grep "^pnl_min:"` (or `grep "^fold._test_pnl_min:"`)
- [ ] `_write_trades_tsv(records, annotation="pnl_min: $X.XX")` writes `# pnl_min: $X.XX` as the first line of `trades.tsv` before the TSV header row
- [ ] `_write_trades_tsv(records, annotation=None)` (default) writes no comment line — first line is the TSV header
- [ ] `__main__` passes a `pnl_min` annotation string to `_write_trades_tsv` when `ROBUSTNESS_SEEDS > 0`; passes `None` when `ROBUSTNESS_SEEDS == 0`

### Error Handling & Edge Cases
- [ ] `_compute_avg_correlation()` returns `0.0` (no penalty) when fewer than 2 tickers were traded — no crash
- [ ] `ROBUSTNESS_SEEDS = 1` runs only the nominal seed: `pnl_min == total_pnl`, no extra backtests
- [ ] `ROBUSTNESS_SEEDS = 5` runs exactly 4 perturbed seeds + nominal (5 total); `ROBUSTNESS_SEEDS = 3` runs 2 + nominal
- [ ] `run_backtest({})` (empty ticker_dfs) returns `pnl_min: 0.0` without crashing

### Integration / E2E
- [ ] `uv run python train.py` completes without error with all three new constants at their defaults (behavior is identical to pre-V3-C)
- [ ] `python -c "import train; r = train.run_backtest({}); print('pnl_min' in r)"` prints `True`

### Validation
- [ ] `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` passes — GOLDEN_HASH updated
- [ ] All 9 new unit tests pass: `test_position_cap_limits_simultaneous_positions`, `test_position_cap_does_not_fire_when_unlimited`, `test_correlation_penalty_reduces_pnl_when_positive`, `test_correlation_penalty_zero_when_weight_is_zero`, `test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl`, `test_robustness_seeds_nonzero_returns_pnl_min`, `test_print_results_includes_pnl_min_line`, `test_write_trades_tsv_annotation_header`, `test_write_trades_tsv_no_annotation_when_none`
- [ ] `uv run pytest tests/test_optimization.py -v` — full suite passes (pre-existing tests unchanged, 1 git-state skip acceptable)
- [ ] `program.md` contains `discard-fragile` as a documented status value with agent instructions to inspect `trades.tsv` annotation

### Out of Scope
- R9 date-shift perturbation (C3 resolution: dropped; V3-B rolling windows cover this)
- Harness auto-detection of fragility (the `discard-fragile` status is an agent decision, not computed code)
- R6 (ticker holdout), R10 (bootstrap CI), R11 (regime-conditional parameters) — V3-D future work
- Changes to `prepare.py` or `strategies/`

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] Level 1–5 validation commands executed and passing
- [ ] All 9 new automated tests written and passing
- [ ] GOLDEN_HASH updated and test passes
- [ ] program.md updated with discard-fragile status, trades.tsv inspection, and pnl_min grep commands
- [ ] Full test suite passes (unit + integration)
- [ ] No linting/syntax errors
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Design decisions

**R8 correlation penalty formula**: Uses the literal PRD formula `penalized_pnl = total_pnl - CORRELATION_PENALTY_WEIGHT * avg_corr * total_pnl`, which scales PnL by `(1 - CORRELATION_PENALTY_WEIGHT * avg_corr)`. This works symmetrically for positive and negative PnL. Default weight = 0.0 means no change.

**R9 perturbation seeds**: Uses 4 fixed perturbation corners: (±0.5% price, ±0.3 ATR stop). With `ROBUSTNESS_SEEDS = 5`, runs all 4 + nominal = 5 total. With `ROBUSTNESS_SEEDS = 3`, runs first 2 + nominal = 3 total. Set to `ROBUSTNESS_SEEDS = 1` for just the nominal run (pnl_min = total_pnl, but key is present).

**Avoiding infinite recursion in R9**: Perturbed runs are gated by `_price_bias == 0.0 and _stop_atr_bias == 0.0`. Perturbed calls pass non-zero biases → they never trigger another round of perturbation.

**trades.tsv annotation**: Written as a `#`-prefixed comment line before the TSV header. This is non-standard for TSV but readable by humans and grep. The agent uses `head -1 trades.tsv` to read it.

**Early-exit path**: The early-exit return (< 2 trading days) at the top of `run_backtest()` must also include `"pnl_min": 0.0` to keep the return schema consistent.

**Performance**: Default `ROBUSTNESS_SEEDS = 0` adds zero overhead. With `ROBUSTNESS_SEEDS = 5` and `WALK_FORWARD_WINDOWS = 3`, each `train.py` run adds 4 extra backtests per fold call (24 extra backtests total per iteration). On a ~40-day window with 17 tickers, each backtest takes ~0.5s, so the overhead is ~12s per iteration — acceptable.
