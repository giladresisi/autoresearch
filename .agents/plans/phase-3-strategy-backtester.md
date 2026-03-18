# Feature: Phase 3 — Strategy + Backtester (`train.py`)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Complete the `train.py` stock strategy backtester by implementing: (1) a working `manage_position()` function that raises the stop to breakeven once price clears entry + 1×ATR14, (2) a chronological backtester loop that simulates stop-hit detection, entries, position management, and daily mark-to-market across the entire backtest window, (3) Sharpe ratio computation from daily portfolio value changes, and (4) a fixed-format output block parseable by the optimization agent.

The screener (`screen_day()`) and all indicator helpers are already in `train.py` from the screener feature. This phase wires them into a complete, runnable backtester.

## User Story

As a developer running the optimization loop,
I want to execute `uv run train.py` and get a `sharpe:` line in the output,
So that the LLM agent has a single numeric metric to optimize against across experiments.

## Problem Statement

`train.py` currently has a fully-implemented `screen_day()` but only a stub `manage_position()` and no backtester loop. Running it as a script only screens a single ticker for one day — it produces no Sharpe output and cannot serve as the optimization target.

## Solution Statement

Add `BACKTEST_START`/`BACKTEST_END` constants, a `load_all_ticker_data()` function, a `run_backtest()` function containing the chronological loop, a `print_results()` helper for the fixed output format, and update `__main__` to run the full backtest. Write ~15 new tests in `tests/test_backtester.py` covering manage_position, the loop, and the output format.

## Feature Metadata

**Feature Type**: New Capability (completing Phase 3 of MVP)
**Complexity**: Medium
**Primary Systems Affected**: `train.py`, `tests/test_backtester.py`
**Dependencies**: `prepare.py` cache must be populated for real runs; tests use synthetic DataFrames
**Breaking Changes**: No — existing `screen_day()` API and indicator helpers unchanged

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 1–249) — Full current state: CACHE_DIR, load_ticker_data, indicator helpers, screen_day, manage_position stub, __main__; understand the existing code before adding
- `tests/test_screener.py` (lines 1–50) — Fixture patterns (`make_passing_df`, `make_signal_df`), import conventions, date construction; mirror in new test file
- `prepare.py` — BACKTEST_START/BACKTEST_END constants defined here; `train.py` should define its own copies for standalone operation
- `prd.md` (lines 206–261) — Features 3–5: manage_position interface, backtester loop specification, Sharpe formula, output block format
- `example-screener.py` (lines 96–179) — Original indicator implementations for reference (already ported to train.py with lowercase columns)

### New Files to Create

- `tests/test_backtester.py` — 15 tests covering manage_position, run_backtest, and output format

### Relevant Documentation — READ BEFORE IMPLEMENTING

- PRD Features 3–5 (`prd.md` lines 206–261) — exact loop semantics, interface signatures, output format

### Patterns to Follow

**Naming Conventions**: lowercase column names (`high`, `low`, `close`, `open`, `volume`, `price_10am`); `date` as index name; `pd.Index(dates, name='date')`
**Error Handling**: return `None` for missing data; `sys.exit(1)` on fatal errors; no `print()` in library functions
**Test fixtures**: synthetic DataFrames using `pd.Index([date(...),...], name='date')`; `make_signal_df()` pattern from test_screener.py returns a df where screen_day returns non-None
**No look-ahead**: always slice `df.loc[:today]` before passing to screener
**Production code is silent**: no print() inside `run_backtest()`, `manage_position()`, or helper functions

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: Core Functions (can be done in one sitting)        │
├────────────────────────────────────────────────────────────┤
│ Task 1.1: Implement manage_position() in train.py          │
│ Task 1.2: Add constants + load_all_ticker_data()           │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Backtester + Output (Depends on Wave 1)            │
├────────────────────────────────────────────────────────────┤
│ Task 2.1: Implement run_backtest() + print_results()       │
│ Task 2.2: Update __main__ block                            │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (Depends on Wave 2)                          │
├────────────────────────────────────────────────────────────┤
│ Task 3.1: Write tests/test_backtester.py (15 tests)        │
└────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

This plan operates on a single Python file. Tasks within Wave 1 are logically independent (manage_position vs. constants/loader) but since they're in the same file, write them sequentially. Wave 2 depends on Wave 1. Wave 3 depends on Wave 2.

**Wave 1 — Sequential (same file)**: Tasks 1.1, 1.2
**Wave 2 — Sequential after Wave 1**: Tasks 2.1, 2.2
**Wave 3 — Sequential after Wave 2**: Task 3.1

### Interface Contracts

**Contract 1**: `run_backtest(ticker_dfs: dict[str, pd.DataFrame]) -> dict` — accepts preloaded data dict, returns stats dict with keys: `sharpe`, `total_trades`, `win_rate`, `avg_pnl_per_trade`, `total_pnl`; testable without filesystem

**Contract 2**: `manage_position(position: dict, df: pd.DataFrame) -> float` — already declared; this plan provides the real implementation

**Contract 3**: Output block always starts with `---` on its own line, followed by `sharpe: <value>` (no leading spaces), enabling `grep "^sharpe:" run.log`

---

## IMPLEMENTATION PLAN

### Phase 1: manage_position() + Constants + Loader

#### Task 1.1: Implement manage_position()

Replace the stub at `train.py:232-234` with the real implementation.

**Logic:**
- Extract `current_stop = position['stop_price']` and `entry_price = position['entry_price']`
- Compute ATR14 from `df` using existing `calc_atr14(df)`; get last value
- Get `price_10am = float(df['price_10am'].iloc[-1])`
- If ATR is NaN or 0, return `current_stop` unchanged
- If `price_10am >= entry_price + atr`: raise stop to `max(current_stop, entry_price)` (never lower)
- Otherwise return `current_stop`

**Implementation:**
```python
def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    Raise stop to breakeven (entry_price) once price_10am >= entry_price + 1 × ATR14.
    Never lower the stop. Returns updated stop_price (>= position['stop_price']).
    """
    current_stop = position['stop_price']
    entry_price  = position['entry_price']
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr == 0:
        return current_stop
    price_10am = float(df['price_10am'].iloc[-1])
    if price_10am >= entry_price + atr:
        return max(current_stop, entry_price)
    return current_stop
```

#### Task 1.2: Add BACKTEST_START, BACKTEST_END, load_all_ticker_data()

Add after the `CACHE_DIR` constant (line 12) and before the indicator section:

```python
# Backtest window — matches prepare.py; edit here to change the simulation period
BACKTEST_START = "2026-01-01"
BACKTEST_END   = "2026-03-01"
```

Add a new function after `load_ticker_data()`:

```python
def load_all_ticker_data() -> dict[str, pd.DataFrame]:
    """Loads all *.parquet files from CACHE_DIR. Returns {} if directory is empty or missing."""
    if not os.path.isdir(CACHE_DIR):
        return {}
    result = {}
    for fname in os.listdir(CACHE_DIR):
        if fname.endswith(".parquet"):
            ticker = fname[:-len(".parquet")]
            path = os.path.join(CACHE_DIR, fname)
            result[ticker] = pd.read_parquet(path)
    return result
```

### Phase 2: run_backtest() + print_results() + __main__

#### Task 2.1: Implement run_backtest()

Add a new top-level function. Place it after `manage_position()` and before the `if __name__ == "__main__"` block.

**Loop semantics (per PRD Feature 4):**

On each trading day T (in chronological order):
1. **Check stops**: for each open position, if `Low[T-1] <= stop_price` → close at stop price. Accumulate P&L. Remove from portfolio.
2. **Screen**: for each ticker NOT in portfolio, run `screen_day(df.loc[:T], T)`. If signal: enter at `price_10am[T] + 0.03`, shares = 500.0 / entry_price.
3. **Manage**: for each open position (including new ones), call `manage_position(pos, df.loc[:T])`. Apply new stop if higher.
4. **Mark-to-market**: `portfolio_value = Σ(price_10am[T] × shares)` for all open positions. Append to `daily_values`.

**End of backtest**: close all open positions at their last available `price_10am`.

**Sharpe**: `np.diff(daily_values)` → mean/std × √252. If std == 0 or fewer than 2 daily values, sharpe = 0.0.

**Trade tracking**: each closed position records `pnl = (exit_price - entry_price) × shares`. A trade is a win if pnl > 0.

```python
def run_backtest(ticker_dfs: dict) -> dict:
    """
    Run chronological backtest over BACKTEST_START..BACKTEST_END.
    ticker_dfs: {ticker: full history DataFrame with date index}
    Returns stats dict: sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl.
    """
    start = date.fromisoformat(BACKTEST_START)
    end   = date.fromisoformat(BACKTEST_END)

    # Collect all trading days that fall in [start, end)
    all_days: set = set()
    for df in ticker_dfs.values():
        for d in df.index:
            if start <= d < end:
                all_days.add(d)
    trading_days = sorted(all_days)

    if len(trading_days) < 2:
        return {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
                "avg_pnl_per_trade": 0.0, "total_pnl": 0.0}

    portfolio: dict = {}   # ticker -> position dict
    trades: list = []      # list of pnl floats per closed trade
    daily_values: list = []

    for i, today in enumerate(trading_days):
        prev_day = trading_days[i - 1] if i > 0 else None

        # 1. Check stops using previous day's low
        if prev_day is not None:
            to_close = []
            for ticker, pos in portfolio.items():
                df = ticker_dfs[ticker]
                if prev_day in df.index:
                    prev_low = float(df.loc[prev_day, "low"])
                    if prev_low <= pos["stop_price"]:
                        pnl = (pos["stop_price"] - pos["entry_price"]) * pos["shares"]
                        trades.append(pnl)
                        to_close.append(ticker)
            for t in to_close:
                del portfolio[t]

        # 2. Screen for new entries
        for ticker, df in ticker_dfs.items():
            if ticker in portfolio:
                continue
            hist = df.loc[:today]
            signal = screen_day(hist, today)
            if signal is None:
                continue
            entry_price = signal["entry_price"] + 0.03
            shares = 500.0 / entry_price
            portfolio[ticker] = {
                "entry_price": entry_price,
                "entry_date": today,
                "shares": shares,
                "stop_price": signal["stop"],
                "ticker": ticker,
            }

        # 3. Manage positions (including new entries)
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            hist = df.loc[:today]
            new_stop = manage_position(pos, hist)
            pos["stop_price"] = max(new_stop, pos["stop_price"])

        # 4. Mark-to-market
        portfolio_value = 0.0
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            if today in df.index:
                portfolio_value += float(df.loc[today, "price_10am"]) * pos["shares"]
        daily_values.append(portfolio_value)

    # End of backtest: close all remaining positions
    for ticker, pos in portfolio.items():
        df = ticker_dfs[ticker]
        last_price = float(df["price_10am"].iloc[-1])
        pnl = (last_price - pos["entry_price"]) * pos["shares"]
        trades.append(pnl)

    # Sharpe computation (PRD Feature 5)
    arr = np.array(daily_values, dtype=float)
    changes = np.diff(arr)
    if len(changes) == 0 or changes.std() == 0:
        sharpe = 0.0
    else:
        sharpe = float((changes.mean() / changes.std()) * np.sqrt(252))

    total_trades = len(trades)
    total_pnl    = float(sum(trades))
    wins         = sum(1 for p in trades if p > 0)
    win_rate     = (wins / total_trades) if total_trades > 0 else 0.0
    avg_pnl      = (total_pnl / total_trades) if total_trades > 0 else 0.0

    return {
        "sharpe":            round(sharpe, 6),
        "total_trades":      total_trades,
        "win_rate":          round(win_rate, 3),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "total_pnl":         round(total_pnl, 2),
    }
```

#### Task 2.2: Add print_results() and update __main__

Add `print_results()` right after `run_backtest()`:

```python
def print_results(stats: dict) -> None:
    """Print the fixed-format summary block. Agent parses this with grep."""
    print("---")
    print(f"sharpe:              {stats['sharpe']:.6f}")
    print(f"total_trades:        {stats['total_trades']}")
    print(f"win_rate:            {stats['win_rate']:.3f}")
    print(f"avg_pnl_per_trade:   {stats['avg_pnl_per_trade']:.2f}")
    print(f"total_pnl:           {stats['total_pnl']:.2f}")
    print(f"backtest_start:      {BACKTEST_START}")
    print(f"backtest_end:        {BACKTEST_END}")
```

Replace the entire `if __name__ == "__main__":` block with:

```python
if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)
    stats = run_backtest(ticker_dfs)
    print_results(stats)
```

---

## STEP-BY-STEP TASKS

### WAVE 1: Core Functions

#### Task 1.1: UPDATE manage_position() in train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Real manage_position() raising stop to breakeven at entry + 1× ATR14
- **IMPLEMENT**: Replace stub at train.py lines 232–234 with the implementation in Phase 1 above
- **VALIDATE**: `uv run python -c "import train; print('OK')"`

#### Task 1.2: ADD BACKTEST_START, BACKTEST_END, load_all_ticker_data() in train.py

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Backtest window constants; filesystem loader for all cached tickers
- **IMPLEMENT**: Add constants after CACHE_DIR; add load_all_ticker_data() after load_ticker_data()
- **VALIDATE**: `uv run python -c "from train import BACKTEST_START, BACKTEST_END, load_all_ticker_data; print(BACKTEST_START, BACKTEST_END)"`

**Wave 1 Checkpoint**: `uv run python -c "import train; print('Wave 1 OK')"`

---

### WAVE 2: Backtester + Output

#### Task 2.1: ADD run_backtest() + print_results() in train.py

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [2.2, 3.1]
- **PROVIDES**: Runnable backtester returning stats dict; formatted output function
- **IMPLEMENT**: Add run_backtest() and print_results() after manage_position(), before __main__; see implementation in Phase 2 above
- **VALIDATE**: `uv run python -c "from train import run_backtest; print(run_backtest({}))"`
  Expected: `{'sharpe': 0.0, 'total_trades': 0, 'win_rate': 0.0, 'avg_pnl_per_trade': 0.0, 'total_pnl': 0.0}`

#### Task 2.2: UPDATE __main__ block in train.py

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Runnable script that loads cache and prints Sharpe output
- **IMPLEMENT**: Replace existing __main__ block with the new version from Phase 2 above
- **VALIDATE**: Syntax check: `uv run python -c "import py_compile, train"` (no error expected)

**Wave 2 Checkpoint**: `uv run python -c "import train"` — verify imports cleanly

---

### WAVE 3: Tests

#### Task 3.1: CREATE tests/test_backtester.py

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2]
- **PROVIDES**: 15 automated tests covering manage_position, run_backtest, output format
- **IMPLEMENT**: See TESTING STRATEGY below for all test cases
- **VALIDATE**: `uv run pytest tests/test_backtester.py -v` — all 15 tests pass

**Final Checkpoint**:
```bash
uv run pytest tests/ -v
# Expect: all existing tests (20 screener + 16 prepare) + 15 new backtester = 51 total passing
```

---

## TESTING STRATEGY

**All tests are automated with pytest. No manual tests required.**

### tests/test_backtester.py — 15 tests

**Imports needed:**
```python
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import patch
from train import (
    manage_position, run_backtest, print_results, calc_atr14,
    BACKTEST_START, BACKTEST_END,
)
```

**Fixture 1 — `make_position_df`: minimal df for manage_position tests**
```python
def make_position_df(n=30, price=100.0, atr_spread=2.0):
    """Returns df where price_10am is constant at `price` and ATR14 ≈ atr_spread."""
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    highs  = np.full(n, price + atr_spread)
    lows   = np.full(n, price - atr_spread)
    closes = np.full(n, price)
    return pd.DataFrame({
        'open': closes, 'high': highs, 'low': lows, 'close': closes,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': np.full(n, price),
    }, index=pd.Index(dates, name='date'))
```

**Fixture 2 — `make_minimal_df`: bare-bones df for backtester loop tests (used with mocked screen_day)**

Loop-mechanics tests (stop-hit, re-entry, end-of-backtest close, Sharpe math) mock `screen_day`
via `unittest.mock.patch` and use this minimal fixture. Do NOT attempt to build real-data-satisfying
fixtures for these tests — screen_day's correctness is already covered by test_screener.py.

```python
def make_minimal_df(trading_days: list, base_price: float = 100.0) -> pd.DataFrame:
    """Bare-minimum df covering specific dates. Used when screen_day is mocked."""
    n = len(trading_days)
    return pd.DataFrame({
        'open':       np.full(n, base_price),
        'high':       np.full(n, base_price * 1.01),
        'low':        np.full(n, base_price * 0.99),
        'close':      np.full(n, base_price),
        'volume':     np.full(n, 1_000_000.0),
        'price_10am': np.full(n, base_price),
    }, index=pd.Index(trading_days, name='date'))
```

**Fixture 3 — `make_signal_df_for_backtest`: real screen_day fires on a known backtest date**

Used only for the one integration test (test 12) that verifies screen_day → run_backtest
end-to-end. Identical price construction to `make_signal_df` in test_screener.py, but dates
are anchored so `index[-1]` lands on `signal_date` inside the backtest window.

```python
def make_signal_df_for_backtest(signal_date: date = date(2026, 1, 10)) -> pd.DataFrame:
    """250-row df where screen_day(df, signal_date) returns a non-None dict.
    All simulation logic lives here in the test file — train.py is unmodified."""
    n = 250
    dates = [signal_date - timedelta(days=(n - 1 - i)) for i in range(n)]

    close = np.linspace(60.0, 120.0, n, dtype=float)
    for i, idx in enumerate(range(230, 242)):
        close[idx] = 116.0 + (180.0 - 116.0) * (i / 11)
    close[242:246] = [170.0, 150.0, 130.0, 110.0]
    close[246], close[247], close[248], close[249] = 100.0, 101.0, 102.0, 103.0

    high  = close * 1.005
    low   = close * 0.995
    open_ = close * 0.998
    open_[249] = 99.0
    price_10am = close.copy()
    price_10am[249] = 110.0

    df = pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': price_10am,
    }, index=pd.Index(dates, name='date'))

    # Pivot low + prior touch required by R2+R6
    df.iloc[210, df.columns.get_loc('low')] = 90.0
    df.iloc[195, df.columns.get_loc('low')] = 89.1
    df.iloc[195, df.columns.get_loc('high')] = 90.9
    return df
```

**Test cases:**

#### manage_position tests (5 tests)

1. **test_manage_position_no_raise_below_threshold**
   - Setup: `df = make_position_df(price=100.0, atr_spread=2.0)` → ATR14 ≈ 2.0; price_10am=100 < entry+ATR=102
   - Call: `manage_position({'entry_price': 100.0, 'stop_price': 90.0, 'shares': 5.0, 'ticker': 'X', 'entry_date': date(2025,1,2)}, df)`
   - Assert: result == 90.0 (stop unchanged)

2. **test_manage_position_raises_to_breakeven**
   - Setup: `df = make_position_df(price=103.0, atr_spread=2.0)` → price_10am=103 >= entry(100)+ATR(2)=102
   - Call: `manage_position({'entry_price': 100.0, 'stop_price': 90.0, ...}, df)`
   - Assert: result == 100.0 (stop raised to entry_price)

3. **test_manage_position_never_lowers_existing_stop**
   - Setup: position with stop_price=100 (already at breakeven); `make_position_df(price=103.0, atr_spread=2.0)`
   - Assert: result == 100.0 (max(new_stop=100, current=100) — never goes below current)

4. **test_manage_position_nan_atr**
   - Setup: `make_position_df(n=5)` → ATR14 will be NaN (needs 14 rows); position with stop=90
   - Assert: result == 90.0 (NaN ATR → no change)

5. **test_manage_position_zero_atr**
   - Setup: df with identical high/low/close on all rows (TR=0, ATR=0); position with stop=90
   - Assert: result == 90.0 (zero ATR → no change)

#### run_backtest tests (8 tests)

Tests 6–8 use no mocking (trivially no signals). Tests 9–12 use `patch('train.screen_day')` to
inject controlled signals. Test 13 is the one integration test using `make_signal_df_for_backtest`.

6. **test_run_backtest_empty_dict_returns_zero_sharpe**
   - `stats = run_backtest({})`
   - Assert: stats['sharpe'] == 0.0, stats['total_trades'] == 0

7. **test_run_backtest_no_backtest_days_returns_zero_sharpe**
   - Build `make_minimal_df([date(2020, 1, 2), date(2020, 1, 3)])` — all dates before BACKTEST_START
   - Assert: `run_backtest({'X': df})['sharpe'] == 0.0`

8. **test_run_backtest_no_signals_returns_zero_sharpe**
   - Build `make_minimal_df` with 3 dates inside the backtest window
   - screen_day will return None (df has < 150 rows — fails R1 immediately, no mock needed)
   - Assert: stats['sharpe'] == 0.0, stats['total_trades'] == 0

9. **test_run_backtest_no_reentry_same_ticker**
   - Setup: `make_minimal_df([d0, d1, d2, d3])` with d0–d3 inside backtest window
   - `patch('train.screen_day')` → returns a fixed signal dict on d0 AND d2 for the same ticker
   - Assert: stats['total_trades'] <= 1 (ticker is in portfolio on d2, so screener call is skipped)

10. **test_run_backtest_stop_hit_closes_position**
    - Setup: `make_minimal_df([d0, d1, d2])` with d0–d2 inside backtest window
    - Set `df.loc[d1, 'low'] = 89.0` (below stop=90)
    - `patch('train.screen_day')` → returns `{'stop': 90.0, 'entry_price': 100.0, ...}` on d0, None otherwise
    - Assert: stats['total_trades'] == 1 (position entered on d0, stopped out when d2 checks low[d1])

11. **test_run_backtest_end_of_backtest_closes_all_positions**
    - Setup: `make_minimal_df([d0, d1, d2])` with d0–d2 inside backtest window, low always above stop
    - `patch('train.screen_day')` → returns signal on d0 only
    - Assert: stats['total_trades'] == 1 (position closed at end), stats['total_pnl'] is a finite float

12. **test_run_backtest_sharpe_is_finite_when_trades_occur**
    - Setup: same as test 11 (mock signal, position held through end)
    - Assert: `math.isfinite(stats['sharpe'])` and `not math.isnan(stats['sharpe'])`

13. **test_run_backtest_integration_real_screener_fires** *(integration — uses real screen_day)*
    - Setup: `df = make_signal_df_for_backtest(signal_date=date(2026, 1, 10))`
    - Call: `run_backtest({'FAKE': df})` — no mock; real screen_day fires on Jan 10
    - Assert: stats['total_trades'] >= 1 (at least one entry was made)
    - Mark with `@pytest.mark.integration` (consistent with existing test suite convention)

#### print_results / output format tests (2 tests via capsys)

14. **test_output_format_sharpe_line_parseable**
    ```python
    def test_output_format_sharpe_line_parseable(capsys):
        print_results({'sharpe': 1.23456, 'total_trades': 5, 'win_rate': 0.6,
                       'avg_pnl_per_trade': 20.0, 'total_pnl': 100.0})
        out = capsys.readouterr().out
        sharpe_lines = [l for l in out.splitlines() if l.startswith('sharpe:')]
        assert len(sharpe_lines) == 1
        assert abs(float(sharpe_lines[0].split(':')[1].strip()) - 1.23456) < 1e-4
    ```

15. **test_output_format_all_seven_fields_present**
    ```python
    def test_output_format_all_seven_fields_present(capsys):
        print_results({'sharpe': 0.5, 'total_trades': 2, 'win_rate': 0.5,
                       'avg_pnl_per_trade': 10.0, 'total_pnl': 20.0})
        out = capsys.readouterr().out
        for field in ['sharpe:', 'total_trades:', 'win_rate:', 'avg_pnl_per_trade:',
                      'total_pnl:', 'backtest_start:', 'backtest_end:']:
            assert field in out, f"Missing field: {field}"
    ```

### Edge Cases

- **No cached data** — `run_backtest({})` → sharpe=0.0, 0 trades ✅ test 6
- **No backtest-window days** — sharpe=0.0 ✅ test 7
- **No screener signals** — sharpe=0.0, 0 trades ✅ test 8
- **Single trading day** — len(trading_days) < 2 → sharpe=0.0 ✅ covered by test 7
- **All positions stopped out day 1** — all closed; daily_values all 0; sharpe=0.0 ✅ test 13
- **Sharpe infinity (std=0, mean≠0)** — guarded by std==0 check returning 0.0 ✅ test 13
- **NaN ATR in manage_position** — stop unchanged ✅ test 4

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 15 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 15 | 100% |

**Execution agent**: CREATE `tests/test_backtester.py` as part of Task 3.1. RUN after Wave 3.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Import

```bash
uv run python -c "import train; print('import OK')"
uv run python -c "from train import manage_position, run_backtest, print_results, load_all_ticker_data; print('exports OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_backtester.py -v
# Expected: 15/15 passing
```

### Level 3: Regression (existing test suites)

```bash
uv run pytest tests/test_screener.py -v
# Expected: 20/20 passing (unchanged)
uv run pytest tests/test_prepare.py -v
# Expected: 14/14 non-integration + 1 integration + 1 subprocess passing (unchanged)
```

### Level 4: Full Suite + Smoke Test

```bash
uv run pytest tests/ -v
# Expected: ~51 total passing (20 screener + 16 prepare + 15 backtester)

# Smoke test (requires prepare.py cache to be populated):
# uv run train.py 2>&1 | grep "^sharpe:"
# If no cache: expected to print error to stderr and exit 1 (verified by test 6 logic)
```

---

## ACCEPTANCE CRITERIA

- [ ] `manage_position()` raises stop to entry_price when price_10am >= entry_price + ATR14; never lowers stop
- [ ] `run_backtest({})` returns sharpe=0.0, total_trades=0 (empty input handled gracefully)
- [ ] Backtester uses **previous day's low** for stop-hit detection on day T
- [ ] A ticker already in the portfolio is skipped by the screener (no double-entry)
- [ ] Open positions at end of backtest are closed at their last available price_10am
- [ ] `daily_portfolio_values` uses mark-to-market (Σ price_10am × shares for open positions)
- [ ] Sharpe = 0.0 when std of daily changes is 0
- [ ] Output block starts with `---` and all 7 fields present; `sharpe:` line has no leading spaces
- [ ] `grep "^sharpe:" run.log` captures exactly one line with a parseable float
- [ ] All 15 new tests in tests/test_backtester.py pass
- [ ] All 20 existing screener tests still pass (no regressions)
- [ ] All 16 existing prepare tests still pass (no regressions)
- [ ] No print() calls inside manage_position() or run_backtest()
- [ ] Exit code 0 on normal completion; exit code 1 when cache is empty

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: manage_position() implemented and validated
- [ ] Task 1.2: BACKTEST_START, BACKTEST_END, load_all_ticker_data() added
- [ ] Task 2.1: run_backtest() and print_results() implemented; run_backtest({}) returns zero stats
- [ ] Task 2.2: __main__ block updated to call load_all_ticker_data() + run_backtest() + print_results()
- [ ] Task 3.1: tests/test_backtester.py created with 15 tests
- [ ] Level 1–4 validation commands all pass
- [ ] All acceptance criteria checked
- [ ] No debug prints added during implementation
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Key Design Decisions

**Stop-hit timing**: On loop day T, check Low[T-1] for stops. This means a stop hit on the last bar of day T-1 is detected at the start of day T. This creates a 1-day lag but avoids look-ahead (you don't know today's low until EOD).

**Mark-to-market uses price_10am**: The portfolio value on day T = Σ(price_10am[T] × shares). This is the same price used for entries, keeping the accounting consistent. Positions that were stopped out today do not contribute.

**End-of-backtest settlement**: Remaining open positions are closed at `df['price_10am'].iloc[-1]` (the last row in the ticker's DataFrame, which may extend past BACKTEST_END). This mirrors realistic behavior where you exit at the next available price.

**Ticker universe**: Determined by what's in CACHE_DIR. `train.py` never hardcodes tickers — it loads whatever parquet files exist. This allows the user to control the universe by running `prepare.py` with their preferred TICKERS list.

**run_backtest() accepts a dict**: This makes the function directly testable without hitting the filesystem. `load_all_ticker_data()` is the only filesystem-touching function, and it can be bypassed in tests.

**No position size limit**: Each ticker can have at most one position at a time ($500), but the number of concurrent tickers is unbounded. Portfolio-level risk limits are out of scope per PRD.

**Slippage applied only at entry**: Entry price = `price_10am + 0.03`. Stop-outs and end-of-backtest exits use the raw price with no slippage, consistent with the PRD.
