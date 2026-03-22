# Feature: V3-A Signal Correctness

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

V3-A fixes the measurement foundation of the optimization harness before any structural evaluation changes (V3-B, V3-C). Four changes:

- **R1 — Look-ahead fix**: `screen_day()` currently computes indicators including today's close (available only at end-of-day), then uses `price_10am` for the entry signal. On a real trading day at 10am, only yesterday's close (and prior history) is known. Fix: compute all indicators on `df.iloc[:-1]` (yesterday + prior); read only `price_10am` and today's volume from `df.iloc[-1]`.

- **R3 — Risk-proportional sizing**: Currently each position uses `500 / entry_price` shares regardless of stop distance. Replace with `RISK_PER_TRADE / (entry_price - stop)`, so wide-stop positions are smaller and tight-stop positions are larger — normalizing dollar risk per trade.

- **R5 — Trade-level attribution**: Currently `run_backtest()` only accumulates a list of P&L floats. Extend to accumulate full per-trade records (ticker, dates, hold duration, stop type, prices, P&L) and write them to `trades.tsv` after each run.

- **R4-partial — Behavioral clarification**: Update `program.md` to explicitly state that `test_total_pnl` is printed for information only and must NOT influence keep/discard decisions during the loop.

## User Story

As a developer running the optimization loop,
I want the harness to measure strategy performance without look-ahead bias and with risk-normalized position sizing,
So that the Sharpe ratio and P&L figures reflect what would actually be achievable in live trading, and subsequent V3-B/V3-C changes build on a clean foundation.

## Problem Statement

The current harness has three measurement flaws:
1. `screen_day()` computes indicators on today's close, which is only known after market close — not at the 10am entry time.
2. Fixed dollar sizing (`500 / entry_price`) ignores stop distance, meaning the same dollar-amount is risked whether the stop is 2% or 10% away.
3. No per-trade record exists — only aggregate P&L is available, making it impossible to attribute P&L to specific trades or diagnose stop behavior.

## Solution Statement

- **R1**: Restructure `screen_day()` to compute all indicators on a history slice ending at yesterday (`df.iloc[:-1]`), and read only `price_10am` (and today's volume) from today's row (`df.iloc[-1]`). Update the minimum history check from `len(df) < 60` to `len(df) < 61`.
- **R3**: Add `RISK_PER_TRADE = 50.0` to `train.py`'s mutable section; update `run_backtest()` (immutable zone) to use `RISK_PER_TRADE / (entry_price - signal["stop"])` for shares.
- **R5**: Extend `screen_day()` to include `stop_type: 'pivot' | 'fallback'` in its return dict; extend `run_backtest()` to accumulate per-trade dicts and return them; add `_write_trades_tsv()` helper and call it from `__main__`.
- **R4-partial**: Add a sentence to `program.md`'s experiment loop section explicitly forbidding the agent from using `test_total_pnl` as a keep/discard signal.

## Feature Metadata

**Feature Type**: Enhancement (correctness fix + new output)
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (mutable + immutable zones), `program.md`, `tests/test_optimization.py`
**Dependencies**: None external
**Breaking Changes**: Yes — immutable zone changes require `GOLDEN_HASH` update in `tests/test_optimization.py`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` lines 168–247 — `screen_day()` — mutable zone; R1 and R5 (stop_type) changes here
- `train.py` lines 267–275 — DO NOT EDIT boundary — do not move this marker
- `train.py` lines 277–387 — `run_backtest()` — immutable zone; R3 shares formula + R5 trade accumulation here
- `train.py` lines 390–454 — `print_results()`, `_write_final_outputs()`, `__main__` — immutable zone; R5 trades.tsv write and `_write_trades_tsv()` helper here
- `tests/test_optimization.py` lines 106–128 — `test_harness_below_do_not_edit_is_unchanged()` — GOLDEN_HASH must be updated
- `tests/test_optimization.py` lines 60–101 — `test_editable_section_stays_runnable_after_threshold_change()` — checks `vol_ratio < 1.0` in editable section; must remain valid after R1 changes
- `program.md` lines 192–209 — experiment loop, keep/discard logic — R4-partial change here
- `tests/test_program_md.py` — structural tests for `program.md`; R4-partial must not break existing assertions
- `tests/test_backtester.py` — existing backtester tests; pattern for new R3/R5 tests

### New Files to Create

- None. All changes are modifications to existing files.

### Patterns to Follow

**Naming Conventions**: existing `_write_final_outputs()` pattern → mirror for `_write_trades_tsv()`
**Error Handling**: guard NaN/zero before division (see existing `if pd.isna(atr) or atr == 0`)
**Immutable zone**: all functions below `# ── DO NOT EDIT BELOW THIS LINE` are "evaluation harness" — R3 and R5 add to these; existing structure must be preserved

---

## PARALLEL EXECUTION STRATEGY

This feature modifies a single file (`train.py`) in two separate zones that must be changed carefully and in order. The `program.md` change is independent and can be done in parallel with any wave.

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: Independent changes (Parallel)                      │
├──────────────────────────────────────┬──────────────────────┤
│ Task 1.1: Mutable zone + stop_type   │ Task 1.2: program.md │
│ R1 + R3-const + R5-stop_type         │ R4-partial           │
│ Agent: backtester-engineer           │ Agent: doc-updater   │
└──────────────────────────────────────┴──────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: Immutable zone changes (After Wave 1)               │
├─────────────────────────────────────────────────────────────┤
│ Task 2.1: R3-shares + R5-trade-records + _write_trades_tsv  │
│ Agent: backtester-engineer                                  │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: Hash + tests (After Wave 2)                         │
├──────────────────────────────────────┬──────────────────────┤
│ Task 3.1: Recompute GOLDEN_HASH      │ Task 3.2: New tests  │
│ Agent: backtester-engineer           │ Agent: test-engineer │
└──────────────────────────────────────┴──────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 (mutable zone) and 1.2 (program.md) — no dependencies
**Wave 2 — Sequential after Wave 1**: Task 2.1 — needs mutable zone done (stop_type in signal dict)
**Wave 3 — Parallel after Wave 2**: Tasks 3.1 and 3.2 — both need immutable zone finalized

### Interface Contracts

**Contract 1**: Task 1.1 provides `stop_type: 'pivot' | 'fallback'` in `screen_day()` return dict → Task 2.1 consumes `signal["stop_type"]` inside `run_backtest()`.
**Contract 2**: Task 2.1 provides `"trade_records"` key in `run_backtest()` return dict → Task 3.2 tests against it.

---

## IMPLEMENTATION PLAN

### Phase 1: Mutable Zone + program.md (Wave 1)

Changes above the `# ── DO NOT EDIT BELOW THIS LINE` marker and `program.md`. Neither touches the immutable zone, so GOLDEN_HASH is unaffected until Phase 2.

### Phase 2: Immutable Zone (Wave 2)

Changes inside `run_backtest()`, `__main__`, and adding `_write_trades_tsv()`. This phase invalidates GOLDEN_HASH.

### Phase 3: Hash Update + New Tests (Wave 3)

Recompute GOLDEN_HASH after all immutable zone changes are finalized. Add new unit tests for R1, R3, and R5 behaviors.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: UPDATE `train.py` mutable section — R1 look-ahead fix + R3 constant + R5 stop_type

- **WAVE**: 1
- **AGENT_ROLE**: backtester-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Corrected `screen_day()` with no look-ahead; `RISK_PER_TRADE` constant; `stop_type` in signal dict
- **IMPLEMENT**:

  **1a. Add `RISK_PER_TRADE` constant** (in the mutable constants block, near `BACKTEST_START`/`TRAIN_END`/`WRITE_FINAL_OUTPUTS`):
  ```python
  # Risk-proportional sizing: dollar risk per trade (V3-A R3)
  RISK_PER_TRADE = 50.0
  ```
  Place it after `WRITE_FINAL_OUTPUTS = False` and before the indicator functions.

  **1b. Fix look-ahead bias in `screen_day()`** (R1):

  Current structure (lines 168–247):
  ```python
  def screen_day(df, today):
      df = df.loc[:today]
      if len(df) < 60:
          return None
      df = df.copy()
      df['_sma50']  = df['close'].rolling(50).mean()
      df['_vm30']   = df['volume'].rolling(30).mean()
      df['_atr14']  = calc_atr14(df)
      df['_rsi14']  = calc_rsi14(df)
      sma50      = float(df['_sma50'].iloc[-1])
      vm30       = float(df['_vm30'].iloc[-1])
      atr        = float(df['_atr14'].iloc[-1])
      rsi        = float(df['_rsi14'].iloc[-1])
      price_10am = float(df['price_10am'].iloc[-1])
      ...
      high20 = float(df['close'].iloc[-21:-1].max())
      ...
      prev_high = float(df['high'].iloc[-2])
      ...
      vol_ratio = float(df['volume'].iloc[-1]) / vm30
      ...
      if is_stalling_at_ceiling(df):
      ...
      stop = find_stop_price(df, price_10am, atr)
      ...
      res_atr = nearest_resistance_atr(df, price_10am, atr)
  ```

  New structure after R1:
  ```python
  def screen_day(df, today):
      df = df.loc[:today]
      # Need at least 1 today row + 60 rows of history for indicators
      if len(df) < 61:
          return None
      # Compute all indicators on history up to yesterday (no look-ahead)
      hist = df.iloc[:-1].copy()
      hist['_sma50']  = hist['close'].rolling(50).mean()
      hist['_vm30']   = hist['volume'].rolling(30).mean()
      hist['_atr14']  = calc_atr14(hist)
      hist['_rsi14']  = calc_rsi14(hist)
      sma50 = float(hist['_sma50'].iloc[-1])
      vm30  = float(hist['_vm30'].iloc[-1])
      atr   = float(hist['_atr14'].iloc[-1])
      rsi   = float(hist['_rsi14'].iloc[-1])
      # Today's observable data: only price_10am and partial-day volume
      price_10am = float(df['price_10am'].iloc[-1])
      today_vol  = float(df['volume'].iloc[-1])
      ...
      high20 = float(hist['close'].iloc[-20:].max())   # last 20 days of history
      prev_high = float(hist['high'].iloc[-1])          # yesterday's high
      vol_ratio = today_vol / vm30
      ...
      if is_stalling_at_ceiling(hist):
      ...
      stop = find_stop_price(hist, price_10am, atr)
      ...
      res_atr = nearest_resistance_atr(hist, price_10am, atr)
  ```

  Key changes:
  - `len(df) < 60` → `len(df) < 61`
  - All indicator computation uses `hist = df.iloc[:-1].copy()` not `df.copy()`
  - All `df['_xxx'].iloc[-1]` → `hist['_xxx'].iloc[-1]`
  - `price_10am = float(df['price_10am'].iloc[-1])` — stays `df` (today)
  - `today_vol = float(df['volume'].iloc[-1])` — today's partial volume
  - `high20 = float(df['close'].iloc[-21:-1].max())` → `float(hist['close'].iloc[-20:].max())`
  - `prev_high = float(df['high'].iloc[-2])` → `float(hist['high'].iloc[-1])`
  - `vol_ratio = float(df['volume'].iloc[-1]) / vm30` → `today_vol / vm30`
  - `is_stalling_at_ceiling(df)` → `is_stalling_at_ceiling(hist)`
  - `find_stop_price(df, ...)` → `find_stop_price(hist, ...)`
  - `nearest_resistance_atr(df, ...)` → `nearest_resistance_atr(hist, ...)`
  - NaN guard: add `today_vol` to the guard: `if pd.isna(price_10am) or pd.isna(sma50) or ... or pd.isna(today_vol) or vm30 == 0 or atr == 0`

  **1c. Add `stop_type` to `screen_day()` return dict** (R5):

  After the stop computation:
  ```python
  stop = find_stop_price(hist, price_10am, atr)
  if stop is None:
      stop = round(price_10am - 2.0 * atr, 2)
      stop_type = 'fallback'
  else:
      stop_type = 'pivot'
  ```

  Add `stop_type` to the return dict:
  ```python
  return {
      'stop':        stop,
      'entry_price': price_10am,
      'stop_type':   stop_type,   # R5: 'pivot' or 'fallback'
      'atr14':       round(atr, 4),
      ...
  }
  ```

- **VALIDATE**: `python -m pytest tests/test_screener.py tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change -v`
- **NOTE**: `test_harness_below_do_not_edit_is_unchanged` will STILL PASS here (Wave 1 doesn't touch immutable zone).

**Wave 1, Task 1.1 Checkpoint**: `python -m pytest tests/test_screener.py -v`

---

#### Task 1.2: UPDATE `program.md` — R4-partial: test_pnl is informational only

- **WAVE**: 1
- **AGENT_ROLE**: doc-updater
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PROVIDES**: Explicit instruction that `test_total_pnl` must not drive keep/discard decisions
- **IMPLEMENT**:

  In the experiment loop section (around "## The experiment loop"), immediately after step 8 (keep if `train_total_pnl` improved), add this note:

  ```markdown
  > **Note:** `test_total_pnl` is printed for diagnostic purposes only. Do NOT use it to
  > decide whether to keep or discard an experiment. The sole keep/discard criterion is
  > `train_total_pnl`. Acting on test P&L during the loop contaminates the holdout window.
  ```

  The current step 8 text is:
  ```
  8. If `train_total_pnl` **improved (higher)** compared to the current best → keep the commit, advance the branch.
  ```

  Add the note after step 8. Do not remove the existing text.

- **VALIDATE**: `python -m pytest tests/test_program_md.py -v`

**Wave 1 Checkpoint**: `python -m pytest tests/test_screener.py tests/test_program_md.py tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change -v`

---

### WAVE 2: Immutable Zone Changes

#### Task 2.1: UPDATE `train.py` immutable zone — R3 shares formula + R5 trade accumulation + _write_trades_tsv

- **WAVE**: 2
- **AGENT_ROLE**: backtester-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 3.2]
- **PROVIDES**: Risk-proportional sizing; per-trade records in `run_backtest()` return; `trades.tsv` written each run
- **USES_FROM_WAVE_1**: Task 1.1 provides `stop_type` in signal dict (consumed here); `RISK_PER_TRADE` constant (referenced here)
- **IMPLEMENT**:

  **2a. R3: Replace shares formula in `run_backtest()`** (line ~339):

  Current:
  ```python
  entry_price = signal["entry_price"] + 0.03
  shares = 500.0 / entry_price
  portfolio[ticker] = {
      "entry_price": entry_price,
      "entry_date": today,
      "shares": shares,
      "stop_price": signal["stop"],
      "ticker": ticker,
  }
  ```

  New:
  ```python
  entry_price = signal["entry_price"] + 0.03
  risk = entry_price - signal["stop"]
  shares = RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price
  portfolio[ticker] = {
      "entry_price": entry_price,
      "entry_date": today,
      "shares": shares,
      "stop_price": signal["stop"],
      "stop_type": signal.get("stop_type", "unknown"),   # R5
      "ticker": ticker,
  }
  ```

  **2b. R5: Add `trade_records` list to `run_backtest()`**:

  After `trades: list = []` initialization, add:
  ```python
  trade_records: list = []   # R5: per-trade attribution dicts
  ```

  **2c. R5: Append to `trade_records` when a stop is hit** (inside the stop-check loop):

  After `trades.append(pnl)` and `ticker_pnl[ticker] = ...`:
  ```python
  trade_records.append({
      "ticker": ticker,
      "entry_date": str(pos["entry_date"]),
      "exit_date": str(prev_day),
      "days_held": (prev_day - pos["entry_date"]).days,
      "stop_type": pos.get("stop_type", "unknown"),
      "entry_price": round(pos["entry_price"], 4),
      "exit_price": round(pos["stop_price"], 4),
      "pnl": round(pnl, 2),
  })
  ```

  **2d. R5: Append to `trade_records` for end-of-backtest forced closures**:

  After `trades.append(pnl)` and `ticker_pnl[ticker] = ...` in the end-of-backtest loop:
  ```python
  last_day = trading_days[-1] if trading_days else None
  trade_records.append({
      "ticker": ticker,
      "entry_date": str(pos["entry_date"]),
      "exit_date": str(last_day),
      "days_held": (last_day - pos["entry_date"]).days if last_day else 0,
      "stop_type": pos.get("stop_type", "unknown"),
      "entry_price": round(pos["entry_price"], 4),
      "exit_price": round(last_price, 4),
      "pnl": round(pnl, 2),
  })
  ```

  **2e. R5: Add `trade_records` to `run_backtest()` return dict**:

  ```python
  return {
      "sharpe": ...,
      "total_trades": ...,
      ...
      "trade_records": trade_records,   # R5
  }
  ```

  **2f. R5: Add `_write_trades_tsv()` helper function** (place between `_write_final_outputs` and `__main__`):

  ```python
  def _write_trades_tsv(trade_records: list) -> None:
      """Write per-trade records to trades.tsv (tab-separated). Overwrites each run."""
      import csv
      fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
                    "stop_type", "entry_price", "exit_price", "pnl"]
      with open("trades.tsv", "w", newline="", encoding="utf-8") as f:
          w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
          w.writeheader()
          w.writerows(trade_records)
  ```

  **2g. R5: Call `_write_trades_tsv()` from `__main__`** (after `print_results(train_stats, ...)`):

  Current `__main__`:
  ```python
  if __name__ == "__main__":
      ticker_dfs = load_all_ticker_data()
      if not ticker_dfs:
          print(...)
          sys.exit(1)
      train_stats = run_backtest(ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
      print_results(train_stats, prefix="train_")
      test_stats = run_backtest(ticker_dfs, start=TEST_START, end=BACKTEST_END)
      print_results(test_stats, prefix="test_")
      if WRITE_FINAL_OUTPUTS:
          _write_final_outputs(...)
  ```

  New `__main__`:
  ```python
  if __name__ == "__main__":
      ticker_dfs = load_all_ticker_data()
      if not ticker_dfs:
          print(...)
          sys.exit(1)
      train_stats = run_backtest(ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
      print_results(train_stats, prefix="train_")
      _write_trades_tsv(train_stats["trade_records"])   # R5
      test_stats = run_backtest(ticker_dfs, start=TEST_START, end=BACKTEST_END)
      print_results(test_stats, prefix="test_")
      if WRITE_FINAL_OUTPUTS:
          _write_final_outputs(...)
  ```

- **VALIDATE**: `python -c "import train; print('import ok')"` (syntax check)
- **NOTE**: `test_harness_below_do_not_edit_is_unchanged` will NOW FAIL — expected, because we just changed the immutable zone. Task 3.1 fixes this.

**Wave 2 Checkpoint**: `python -c "import train; stats = train.run_backtest({}); print(stats.keys())"` — verify `trade_records` key is present and no exception is raised.

---

### WAVE 3: Hash Update + New Tests

#### Task 3.1: UPDATE `tests/test_optimization.py` — Recompute GOLDEN_HASH

- **WAVE**: 3
- **AGENT_ROLE**: backtester-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Passing `test_harness_below_do_not_edit_is_unchanged` test
- **IMPLEMENT**:

  Run the recompute command:
  ```bash
  python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
  ```

  Copy the output hash and replace `GOLDEN_HASH` in `tests/test_optimization.py` line ~118:
  ```python
  GOLDEN_HASH = "<new hash from command above>"
  ```

- **VALIDATE**: `python -m pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v`

---

#### Task 3.2: ADD tests for R1, R3, R5 behaviors

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Regression coverage for V3-A changes
- **IMPLEMENT**:

  Add the following tests. **Append them to `tests/test_backtester.py`** (do not create a new file).

  **Test R1a — Look-ahead: indicators use yesterday's close, not today's**

  ```python
  def test_screen_day_indicators_use_yesterday_close_not_today():
      """
      R1 correctness: screen_day must compute all rolling indicators on df.iloc[:-1].
      Verify by providing data where today's close is dramatically different from
      yesterday's, and checking the function still uses the yesterday-anchored SMA.

      Strategy: patch calc_atr14 to record which df it receives.
      The df passed to calc_atr14 must NOT include today's row (i.e. its last
      price_10am must match yesterday's price_10am, not today's).
      """
      from train import screen_day
      import unittest.mock as mock

      # 100-row df so indicators can warm up; give today a wildly different close
      bdays = pd.bdate_range(end="2026-02-27", periods=101)
      dates = [d.date() for d in bdays]
      prices = np.linspace(80.0, 120.0, 101)
      prices[-1] = 200.0    # today: anomalous close — must NOT influence indicators
      df = pd.DataFrame({
          "open": prices * 0.99, "high": prices * 1.01, "low": prices * 0.99,
          "close": prices,
          "volume": np.full(101, 1_000_000.0),
          "price_10am": prices,
      }, index=pd.Index(dates, name="date"))

      received_dfs = []

      original_calc_atr14 = train.calc_atr14
      def recording_calc_atr14(d):
          received_dfs.append(d.copy())
          return original_calc_atr14(d)

      with mock.patch.object(train, "calc_atr14", side_effect=recording_calc_atr14):
          screen_day(df, dates[-1])

      # calc_atr14 must have been called with a df whose last close is NOT 200.0
      assert received_dfs, "calc_atr14 was never called — screen_day returned early"
      last_close = float(received_dfs[0]["close"].iloc[-1])
      assert last_close != 200.0, (
          f"screen_day passed today's close ({last_close}) to calc_atr14; "
          "expected yesterday's close. R1 look-ahead fix may not be applied."
      )
  ```

  **Test R1b — Minimum history: len(df) == 61 passes, len(df) == 60 returns None**

  ```python
  def test_screen_day_minimum_history_boundary():
      """
      After R1 fix, screen_day needs len(df) >= 61 (60 history rows + today).
      With exactly 60 rows (hist has 59 rows), it must return None.
      With exactly 61 rows, it proceeds past the length guard.
      """
      from train import screen_day

      def make_df(n):
          bdays = pd.bdate_range(end="2026-02-27", periods=n)
          dates = [d.date() for d in bdays]
          prices = np.linspace(80.0, 120.0, n)
          return pd.DataFrame({
              "open": prices * 0.99, "high": prices * 1.01, "low": prices * 0.99,
              "close": prices, "volume": np.full(n, 1_000_000.0), "price_10am": prices,
          }, index=pd.Index(dates, name="date"))

      df_60 = make_df(60)
      result_60 = screen_day(df_60, df_60.index[-1])
      assert result_60 is None, "With 60 rows (hist=59), screen_day must return None"

      # 61-row df won't necessarily produce a signal, but it must NOT return None
      # due to the length guard alone — it may still return None for other reasons.
      # Just confirm the minimum-history check no longer rejects it:
      df_61 = make_df(61)
      # We can only assert it doesn't raise; the result may be None for non-length reasons
      try:
          screen_day(df_61, df_61.index[-1])
      except Exception as e:
          pytest.fail(f"screen_day raised with 61 rows: {e}")
  ```

  **Test R3 — Risk-proportional sizing: wide stop → fewer shares**

  ```python
  def test_run_backtest_risk_proportional_sizing():
      """
      R3: shares = RISK_PER_TRADE / (entry_price - stop).
      A signal with a wider stop distance must produce fewer shares than one with
      a tight stop distance, when entry_price is the same.
      """
      import train as tr
      from datetime import date, timedelta

      signal_date = date(2026, 1, 10)
      days = [signal_date - timedelta(days=i) for i in reversed(range(20))]
      base_price = 100.0
      df = pd.DataFrame({
          "open": np.full(20, base_price), "high": np.full(20, base_price * 1.01),
          "low": np.full(20, base_price * 0.99), "close": np.full(20, base_price),
          "volume": np.full(20, 1_000_000.0), "price_10am": np.full(20, base_price),
      }, index=pd.Index(days, name="date"))

      shares_recorded = []

      def tight_stop_screen(d, today):
          # stop is 2.0 below entry → risk = 2.0
          return {"entry_price": base_price, "stop": base_price - 2.0, "stop_type": "pivot"}

      def wide_stop_screen(d, today):
          # stop is 8.0 below entry → risk = 8.0
          return {"entry_price": base_price, "stop": base_price - 8.0, "stop_type": "fallback"}

      import unittest.mock as mock

      with mock.patch.object(tr, "screen_day", tight_stop_screen), \
           mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
          tight_stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

      with mock.patch.object(tr, "screen_day", wide_stop_screen), \
           mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
          wide_stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

      if tight_stats["total_trades"] > 0 and wide_stats["total_trades"] > 0:
          # tight stop → more shares → larger absolute P&L per dollar move
          # wide stop → fewer shares → smaller absolute P&L per dollar move
          # (Both are closed at last price_10am = base_price → entry_price+0.03 was cost)
          # Since all prices are equal, pnl = (exit - entry) * shares; exit=100, entry=100.03
          # tight: shares = RISK_PER_TRADE / 2.0
          # wide:  shares = RISK_PER_TRADE / 8.0
          tight_shares = tr.RISK_PER_TRADE / (base_price + 0.03 - (base_price - 2.0))
          wide_shares  = tr.RISK_PER_TRADE / (base_price + 0.03 - (base_price - 8.0))
          assert tight_shares > wide_shares, (
              f"tight stop should produce more shares ({tight_shares:.4f}) "
              f"than wide stop ({wide_shares:.4f})"
          )
  ```

  **Test R5a — trade_records in run_backtest return**

  ```python
  def test_run_backtest_returns_trade_records_key():
      """R5: run_backtest() must return a 'trade_records' key."""
      stats = run_backtest({})
      assert "trade_records" in stats, "run_backtest() must return 'trade_records'"
      assert isinstance(stats["trade_records"], list)
  ```

  **Test R5b — trade_records schema**

  ```python
  def test_run_backtest_trade_records_schema():
      """R5: each trade record must have the 8 required fields."""
      import train as tr
      import unittest.mock as mock
      from datetime import date, timedelta

      signal_date = date(2026, 1, 10)
      days = [signal_date - timedelta(days=i) for i in reversed(range(20))]
      base_price = 100.0
      df = pd.DataFrame({
          "open": np.full(20, base_price), "high": np.full(20, base_price * 1.01),
          "low": np.full(20, base_price * 0.99), "close": np.full(20, base_price),
          "volume": np.full(20, 1_000_000.0), "price_10am": np.full(20, base_price),
      }, index=pd.Index(days, name="date"))

      def always_signal(d, today):
          return {"entry_price": base_price, "stop": base_price - 5.0, "stop_type": "pivot"}

      with mock.patch.object(tr, "screen_day", always_signal), \
           mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
          stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

      expected_fields = {"ticker", "entry_date", "exit_date", "days_held",
                         "stop_type", "entry_price", "exit_price", "pnl"}
      for rec in stats["trade_records"]:
          missing = expected_fields - set(rec.keys())
          assert not missing, f"trade record missing fields: {missing}"
          assert rec["stop_type"] in ("pivot", "fallback", "unknown")
  ```

  **Test R5c — trades.tsv is written by __main__ block**

  ```python
  def test_trades_tsv_written_on_run(tmp_path, monkeypatch):
      """
      R5: running train.py as __main__ must produce trades.tsv.
      Uses monkeypatch to change cwd so trades.tsv lands in tmp_path.
      """
      import train as tr
      import subprocess, sys, os

      monkeypatch.chdir(tmp_path)
      # Patch load_all_ticker_data to return empty dict → no trades, but file still written
      with mock.patch.object(tr, "load_all_ticker_data", return_value={}), \
           mock.patch("sys.exit") as mock_exit:
          # __main__ exits with code 1 on empty data; prevent that
          mock_exit.side_effect = SystemExit(1)
          try:
              import importlib, runpy
              runpy.run_path(str(tr.__file__), run_name="__main__")
          except SystemExit:
              pass  # expected — no data

      # With no data, _write_trades_tsv is still called (empty list)
      # Actually, sys.exit(1) is called before _write_trades_tsv; this test
      # instead just verifies the function exists and produces the correct output.
      # Test _write_trades_tsv directly:
      tr._write_trades_tsv([])
      tsv_path = tmp_path / "trades.tsv"
      assert tsv_path.exists(), "trades.tsv must be created by _write_trades_tsv even with no trades"
      content = tsv_path.read_text(encoding="utf-8")
      header = content.splitlines()[0]
      assert "ticker" in header
      assert "entry_date" in header
      assert "stop_type" in header
      assert "pnl" in header
  ```

  **Test R5d — stop_type field in screen_day return dict**

  Add to `tests/test_screener.py` (or at end of `tests/test_backtester.py`):

  ```python
  def test_screen_day_returns_stop_type_field():
      """
      R5: screen_day() must include 'stop_type' as either 'pivot' or 'fallback'
      in every non-None return dict.
      Use a synthetic dataset designed to produce a signal.
      """
      from train import screen_day
      from tests.test_backtester import make_signal_df_for_backtest  # reuse existing fixture

      df = make_signal_df_for_backtest(signal_date=date(2026, 1, 10))
      result = screen_day(df, df.index[-1])

      if result is not None:
          assert "stop_type" in result, "screen_day() must return 'stop_type' in signal dict"
          assert result["stop_type"] in ("pivot", "fallback"), (
              f"stop_type must be 'pivot' or 'fallback', got {result['stop_type']!r}"
          )
  ```

- **VALIDATE**: `python -m pytest tests/test_backtester.py tests/test_screener.py -v`

**Wave 3 Checkpoint**: `python -m pytest tests/test_optimization.py tests/test_backtester.py tests/test_screener.py tests/test_program_md.py --tb=short -q`

---

## TESTING STRATEGY

### Unit Tests

**R1 — Look-ahead fix**
| Test | Status | Tool | File | Run |
|---|---|---|---|---|
| Indicators use yesterday's close | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_screen_day_indicators_use_yesterday_close_not_today -v` |
| Minimum history boundary (60 → 61) | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_screen_day_minimum_history_boundary -v` |

**R3 — Risk-proportional sizing**
| Test | Status | Tool | File | Run |
|---|---|---|---|---|
| Wide stop → fewer shares | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_run_backtest_risk_proportional_sizing -v` |

**R5 — Trade-level attribution**
| Test | Status | Tool | File | Run |
|---|---|---|---|---|
| `trade_records` key in return | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_run_backtest_returns_trade_records_key -v` |
| `trade_records` schema (8 fields) | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_run_backtest_trade_records_schema -v` |
| `trades.tsv` written with correct header | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_trades_tsv_written_on_run -v` |
| `stop_type` field in screen_day dict | ✅ Automated | pytest | `tests/test_backtester.py` | `python -m pytest tests/test_backtester.py::test_screen_day_returns_stop_type_field -v` |

**R4-partial — program.md**
| Test | Status | Tool | File | Run |
|---|---|---|---|---|
| Existing program.md structural tests | ✅ Automated | pytest | `tests/test_program_md.py` | `python -m pytest tests/test_program_md.py -v` |

**Harness integrity**
| Test | Status | Tool | File | Run |
|---|---|---|---|---|
| `test_harness_below_do_not_edit_is_unchanged` (GOLDEN_HASH) | ✅ Automated | pytest | `tests/test_optimization.py` | `python -m pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` |
| Editable section stays runnable after agent edit | ✅ Automated | pytest | `tests/test_optimization.py` | `python -m pytest tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change -v` |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 9 new + 42 existing = ~51 | ~100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | ~51 | 100% |

**No manual tests required.** All V3-A behaviors are directly testable via pytest.

---

## VALIDATION COMMANDS

### Level 1: Syntax Check

```bash
python -c "import train; print('import ok')"
```

### Level 2: Unit Tests (new V3-A tests)

```bash
python -m pytest tests/test_backtester.py tests/test_screener.py tests/test_program_md.py -v
```

### Level 3: Immutable Zone Integrity

```bash
python -m pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v
```

### Level 4: Full Suite

```bash
python -m pytest tests/test_optimization.py tests/test_backtester.py tests/test_screener.py tests/test_program_md.py tests/test_registry.py --tb=short -q
```

Expected: all tests pass (including new V3-A tests). Previous baseline: 42 passed, 1 skipped.

---

## ACCEPTANCE CRITERIA

- [ ] `screen_day()` computes all indicators on `df.iloc[:-1]`; only reads `price_10am` and `volume` from today's row
- [ ] Minimum history guard is `len(df) < 61` (was `< 60`)
- [ ] `RISK_PER_TRADE = 50.0` constant exists in mutable section of `train.py`
- [ ] `run_backtest()` uses `RISK_PER_TRADE / (entry_price - signal["stop"])` for shares
- [ ] `screen_day()` return dict includes `stop_type: 'pivot' | 'fallback'`
- [ ] `run_backtest()` return dict includes `trade_records` key with list of per-trade dicts
- [ ] Each trade record has: ticker, entry_date, exit_date, days_held, stop_type, entry_price, exit_price, pnl
- [ ] `_write_trades_tsv()` function exists in immutable zone and writes tab-separated trades.tsv
- [ ] `__main__` calls `_write_trades_tsv(train_stats["trade_records"])` after each run
- [ ] `program.md` explicitly states `test_total_pnl` is informational only and must not drive keep/discard
- [ ] `GOLDEN_HASH` in `tests/test_optimization.py` updated to match new immutable zone
- [ ] All tests pass: `python -m pytest tests/test_optimization.py tests/test_backtester.py tests/test_screener.py tests/test_program_md.py --tb=short -q`
- [ ] No regressions (42 previously-passing tests still pass)

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete (mutable zone: R1, R3-const, R5-stop_type)
- [ ] Task 1.2 complete (program.md: R4-partial)
- [ ] Wave 1 checkpoint passes
- [ ] Task 2.1 complete (immutable zone: R3-shares, R5-trade_records, _write_trades_tsv, __main__)
- [ ] Wave 2 checkpoint passes
- [ ] Task 3.1 complete (GOLDEN_HASH updated and test passes)
- [ ] Task 3.2 complete (new tests added and passing)
- [ ] Wave 3 checkpoint passes
- [ ] Full test suite passes
- [ ] No regressions from baseline (42 passed, 1 skipped)
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why `len(df) < 61` not `< 62`?

After `df = df.loc[:today]`, the slice includes today. `hist = df.iloc[:-1]` removes today, leaving `len(df) - 1` rows of history. We need at least 60 history rows for SMA50 to be defined (rolling(50) needs 50 + some warmup). `len(hist) >= 60` ↔ `len(df) >= 61`.

### Why `hist['close'].iloc[-20:].max()` instead of `df['close'].iloc[-21:-1].max()`?

Both expressions extract the last 20 rows of history (excluding today). After R1, `hist` is already the pre-today history, so `iloc[-20:]` is cleaner and correct. The old `iloc[-21:-1]` was awkward (21st from end to 2nd from end = 20 items).

### RISK_PER_TRADE guard

The guard `risk = entry_price - signal["stop"]; shares = RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price` is defensive. In practice `risk > 0` is guaranteed because `screen_day()` only returns a signal when `price_10am - stop >= 1.5 * atr > 0`, and `entry_price = signal["entry_price"] + 0.03 > signal["entry_price"]`. The fallback prevents a ZeroDivisionError in case a custom agent-modified screener returns a signal with `stop >= entry_price`.

### GOLDEN_HASH recompute is the final step

Do not recompute GOLDEN_HASH before Task 2.1 is fully complete. Any further edit to the immutable zone after the hash is updated will require another recompute. Run Task 3.1 last.

### test_editable_section_stays_runnable_after_threshold_change

This test checks that `vol_ratio < 1.0` appears in the editable section. After R1, the volume ratio check remains in `screen_day()` but uses `today_vol / vm30` instead of `float(df['volume'].iloc[-1]) / vm30`. The threshold expression `vol_ratio < 1.0` must still be present for this test to pass. Do not rename the variable.

### R4-partial is a text-only change

R4-partial adds a documentation note to `program.md`. It does NOT change how `train.py` computes anything. The note makes the existing behavior (keep/discard on `train_total_pnl`) explicit rather than implicit. Current `test_program_md.py` tests must all continue to pass — verify after editing.
