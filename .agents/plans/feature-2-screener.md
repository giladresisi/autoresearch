# Feature: Feature 2 — Screener (`screen_day` in `train.py`)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Implement `screen_day(df, today)` in `train.py`, along with all supporting indicator and pivot-logic
helpers. This function applies 11 trading rules to a ticker's daily history to produce an entry signal
or `None`. It is the first of three components in the new `train.py` (screener → position manager →
backtester). The current `train.py` is an LLM/GPU pretraining script that is fully replaced by this plan.

**Source of truth for rule logic**: `example-screener.py` (v2) in the repo. Port the indicator helpers
and screening logic with one systematic adaptation: **all column names are lowercase** (`open`, `high`,
`low`, `close`, `volume`, `price_10am`) rather than the capitalized yfinance names in the screener script.

## User Story

As the LLM optimization agent running the backtester loop,
I want `screen_day(df, today)` to return a valid entry signal dict with `{'stop': float}` or `None`,
So that the backtester can identify which tickers to enter positions in on each day.

## Problem Statement

`train.py` contains the old nanochat pretraining loop (torch/GPU). The screener in `example-screener.py`
cannot be reused as-is — it fetches live data, uses capitalized column names, and writes results to JSON.

## Solution Statement

Replace `train.py` with a new file containing:
1. `CACHE_DIR` constant and `load_ticker_data(ticker)` helper
2. All indicator/pivot helpers ported from `example-screener.py` (lowercase columns)
3. `screen_day(df, today)` implementing all 11 rules in order
4. Stubs (`# TODO: Phase 3`) for `manage_position()` and the backtester loop
5. `if __name__ == "__main__"` demo block

Plus `tests/__init__.py` and `tests/test_screener.py` with a pytest suite.

## Feature Metadata

**Feature Type**: New Capability (greenfield replacement of `train.py`)
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (full rewrite), `tests/test_screener.py` (new)
**Dependencies**: `pandas>=2.3.3`, `numpy>=2.2.6` (both already in `pyproject.toml`)
**Breaking Changes**: Yes — old LLM training code deleted. Expected and documented in PRD. Phase 1
already made its GPU imports non-functional.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `example-screener.py` (lines 89–180) — Indicator helper implementations to port
- `example-screener.py` (lines 182–330) — Rule ordering, rule logic, and return dict structure
- `prd.md` (lines 174–202) — Authoritative interface spec: `screen_day` signature, rule table,
  `{'stop': float}` return contract

### Column Name Adaptation (ALL references must be translated)

| `example-screener.py` | `train.py` (this plan) |
|-----------------------|------------------------|
| `df['High']`          | `df['high']`           |
| `df['Low']`           | `df['low']`            |
| `df['Close']`         | `df['close']`          |
| `df['Open']`          | `df['open']`           |
| `df['Volume']`        | `df['volume']`         |
| `close_now` (current price for comparisons) | `price_10am[-1]` |

**CCI and ATR14** still use `high`, `low`, `close` (OHLC). Only the "current price" comparisons
(SMA, pullback pct, stop distance, ATR buffer) use `price_10am`.

### Data Contract (Parquet schema from `prepare.py`)

Index: `date`. Columns: `open`, `high`, `low`, `close`, `volume`, `price_10am` — all lowercase floats.

### New Files to Create

- `train.py` — Full replacement
- `tests/__init__.py` — Empty (required for pytest discovery)
- `tests/test_screener.py` — pytest suite

### Patterns to Follow

- `CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")`
- No `print` statements in helper functions or `screen_day` (production code is silent per CLAUDE.md)
- `manage_position` stub must match PRD interface exactly and return `position['stop_price']`

---

## RULE IMPLEMENTATION REFERENCE (apply in this order)

| Order | ID    | Rule                            | Key expression                                                           |
|-------|-------|---------------------------------|--------------------------------------------------------------------------|
| 1     | R1    | ≥ 150 rows                     | `len(df) < 150 → return None`                                            |
| 2     | 1     | price_10am > SMA150             | SMA150 on `close`; `price_10am[-1] > sma150[-1]`                        |
| 3     | 2     | 3 consecutive up-close days    | `close[-1] > close[-2] > close[-3] > close[-4]`                         |
| 4     | 3     | Vol ≥ 0.85× MA30 (last 2 days) | MA30 on `volume`; both `vol[-1]` and `vol[-2]` ≥ 0.85× MA30             |
| 5     | 4     | CCI(20) < −50, rising 2d       | `c0 < -50` and `c0 > c1 > c2`                                           |
| 6     | 5     | Pullback ≥ 8% from local + ATH | `local_high = high[-8:-1].max()`; both pct_local ≥ 0.08, pct_ath ≥ 0.08 using `price_10am[-1]` |
| 7     | R4    | Upper wick < body               | `body = \|close[-1] - open[-1]\|`; `wick = high[-1] - max(close[-1], open[-1])` |
| 8     | R3    | Not stalling at ceiling         | `is_stalling_at_ceiling(df)` — last 3 highs tight, all closes below them |
| 9     | R2+R6 | Pivot-low stop valid            | `find_stop_price(df, price_10am[-1], atr[-1])` returns non-None         |
| 10    | 1.5×  | ATR buffer to stop              | enforced inside `find_stop_price`; stop rejected if buffer < 1.5× ATR   |
| 11    | R5    | Resistance ≥ 2× ATR above      | `nearest_resistance_atr()` ≥ 2.0 or None (None = no resistance = OK)   |

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                              │
├─────────────────────────────┬──────────────────────────────┤
│ Task 1.1: CREATE indicator  │ Task 1.2: CREATE test        │
│ helpers in train.py         │ fixtures + skeleton in       │
│ Agent: implementer          │ tests/test_screener.py       │
│                             │ Agent: test-writer           │
└─────────────────────────────┴──────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Core (After Wave 1)                                │
├────────────────────────────────────────────────────────────┤
│ Task 2.1: CREATE screen_day() in train.py                  │
│ Agent: implementer                                         │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests + Integration (Parallel)                     │
├─────────────────────────────┬──────────────────────────────┤
│ Task 3.1: FILL test suite   │ Task 3.2: VALIDATE with      │
│ (all 11 rules)              │ real parquet data (if avail) │
│ Agent: test-writer          │ Agent: validator             │
└─────────────────────────────┴──────────────────────────────┘
```

### Interface Contracts

**Wave 1 → Wave 2**: `train.py` must export `calc_cci`, `calc_atr14`, `find_pivot_lows`,
`zone_touch_count`, `find_stop_price`, `is_stalling_at_ceiling`, `nearest_resistance_atr`.

**Wave 1.2 → Wave 3.1**: `make_passing_df(n)` and `make_pivot_df(n)` fixtures must be importable
from `tests.test_screener`.

**Wave 2 → Wave 3.1**: `screen_day` importable from `train`.

### Synchronization Checkpoints

**After Wave 1**:
```bash
uv run python -c "from train import calc_cci, calc_atr14, find_stop_price; print('PASS')"
```
**After Wave 2**:
```bash
uv run python -c "from train import screen_day; print('PASS')"
```
**After Wave 3**:
```bash
uv run python -m pytest tests/test_screener.py -v
```

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation (Parallel)

#### Task 1.1: CREATE indicator helper functions in `train.py`

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Importable helper functions
- **IMPLEMENT**:

  Replace all content of `train.py` with a new file structured as follows:

  **Section 1 — Docstring + imports**:
  ```python
  """
  train.py — Stock strategy screener, position manager, and backtester.
  Rewrite screener criteria, position manager logic, and entry/exit rules to optimize Sharpe ratio.
  Do NOT modify: CACHE_DIR, load_ticker_data(), Sharpe computation, or the output block format.
  """
  import os, sys
  from datetime import date
  import numpy as np
  import pandas as pd
  ```

  **Section 2 — Configuration + data loading**:
  - `CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")`
  - `load_ticker_data(ticker: str) -> pd.DataFrame | None` — reads `CACHE_DIR/{ticker}.parquet`

  **Section 3 — Indicator helpers** (port from `example-screener.py` lines 91–179, adapting column names):
  - `calc_cci(df, p=20)` — CCI with `df['high']`, `df['low']`, `df['close']`; use `raw=True` in `.apply`
  - `calc_atr14(df)` — ATR14 with `df['high']`, `df['low']`, `df['close']`
  - `find_pivot_lows(df, bars=4)` — same logic, uses `df['low']`
  - `zone_touch_count(df, level, lookback=90, band_pct=0.015)` — uses `df['low']`, `df['high']`
  - `find_stop_price(df, entry_price, atr)` — uses `df['low']`, `df['high']`; returns `round(stop, 2)` or `None`
  - `is_stalling_at_ceiling(df, band_pct=0.03)` — uses `df['high']`, `df['close']`
  - `nearest_resistance_atr(df, entry_price, atr, lookback=90)` — uses `df['high']`

  **Section 4 — Screener, position manager, backtester stubs**:
  ```python
  def screen_day(df, today):
      raise NotImplementedError("screen_day: implemented in Task 2.1")

  def manage_position(position: dict, df: pd.DataFrame) -> float:
      """Returns updated stop_price (>= position['stop_price']). TODO: Phase 3."""
      return position['stop_price']

  # TODO: Phase 3 — chronological backtester loop + Sharpe output

  if __name__ == "__main__":
      ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
      df = load_ticker_data(ticker)
      if df is None:
          print(f"No cached data for {ticker}. Run prepare.py first.")
          sys.exit(1)
      today_val = df.index[-1]
      if hasattr(today_val, 'date'):
          today_val = today_val.date()
      print(screen_day(df, today_val))
  ```

- **VALIDATE**:
  ```bash
  uv run python -c "
  from train import (calc_cci, calc_atr14, find_pivot_lows, zone_touch_count,
                     find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr)
  print('PASS: all helpers importable')
  "
  ```

---

#### Task 1.2: CREATE `tests/__init__.py` and fixtures in `tests/test_screener.py`

- **WAVE**: 1
- **AGENT_ROLE**: test-writer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Importable test fixtures
- **IMPLEMENT**:

  Create `tests/__init__.py` (empty).

  Create `tests/test_screener.py` with imports and two fixture functions only (no test functions yet):

  ```python
  """tests/test_screener.py — Pytest suite for screen_day() and helpers."""
  import pytest
  import numpy as np
  import pandas as pd
  from datetime import date, timedelta

  def make_passing_df(n: int = 250) -> pd.DataFrame:
      """Synthetic DataFrame that satisfies all screener pre-conditions (may not pass stop logic)."""
      base = date(2024, 1, 2)
      dates = [base + timedelta(days=i) for i in range(n)]
      close = np.linspace(100.0, 200.0, n)
      # Last 3 closes strictly rising, last value pulled back 12% from peak
      close[-10:] = close[-10] * np.array([1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.91, 0.92, 0.93, 0.94])
      close[-3] = close[-4] * 1.01
      close[-2] = close[-3] * 1.01
      close[-1] = close[-2] * 1.01
      return pd.DataFrame({
          'open':       close * 0.995,
          'high':       close * 1.005,
          'low':        close * 0.990,
          'close':      close,
          'volume':     np.full(n, 1_000_000.0),
          'price_10am': close * 0.998,
      }, index=pd.Index(dates, name='date'))

  def make_pivot_df(n: int = 250) -> pd.DataFrame:
      """Like make_passing_df but with a clear pivot low ~35 bars from end for stop detection."""
      df = make_passing_df(n).copy()
      pivot_idx = n - 35
      pivot_price = float(df['close'].iloc[-1]) * 0.85
      df.iloc[pivot_idx, df.columns.get_loc('low')] = pivot_price
      touch_idx = pivot_idx - 10
      if touch_idx >= 0:
          df.iloc[touch_idx, df.columns.get_loc('low')] = pivot_price * 0.99
          df.iloc[touch_idx, df.columns.get_loc('high')] = pivot_price * 1.01
      return df
  ```

- **VALIDATE**:
  ```bash
  uv run python -c "from tests.test_screener import make_passing_df, make_pivot_df; print('PASS: fixtures OK')"
  uv run python -m pytest tests/test_screener.py --collect-only -q 2>&1 | head -5
  # Expect: "no tests ran" (fixtures only) with no import errors
  ```

**Wave 1 Checkpoint**:
```bash
uv run python -c "
from train import calc_cci, calc_atr14, find_stop_price
from tests.test_screener import make_passing_df
print('PASS: Wave 1 complete')
"
```

---

### WAVE 2: Core Implementation (After Wave 1)

#### Task 2.1: CREATE `screen_day()` in `train.py`

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Working `screen_day` implementing all 11 rules
- **USES_FROM_WAVE_1**: All 7 helper functions from Task 1.1
- **IMPLEMENT**:

  Replace the `NotImplementedError` stub in `train.py` with the full implementation following the
  RULE IMPLEMENTATION REFERENCE table above. Key implementation notes:

  1. First line: `df = df.loc[:today]` (ensure slice, even if caller already sliced)
  2. Guard: `if len(df) < 150: return None`
  3. Compute indicators once upfront: `df = df.copy()`, add `_sma150`, `_vm30`, `_cci`, `_atr14`
     as temporary columns; extract scalar values from `.iloc[-1]`
  4. Guard NaN/zero: `if pd.isna(sma150) or pd.isna(vm30) or pd.isna(atr) or vm30 == 0 or atr == 0: return None`
  5. Apply rules 1–11 in order per the table; each failed rule `return None` immediately (fail-fast)
  6. Return dict: `{'stop': stop, 'entry_price': price_10am, 'atr14': round(atr, 4), 'sma150': round(sma150, 4), 'cci': round(c0, 2), 'pct_local': round(pct_local, 4), 'pct_ath': round(pct_ath, 4)}`

  **Rule 2** (3 up days): compare indices `[-4], [-3], [-2], [-1]` — requires 4 `close` values.
  Guard: `if len(df) < 4: return None` (already covered by R1 ≥ 150).

  **Rule 5** (pullback): `local_high = float(df['high'].iloc[-8:-1].max())` — 7-day window before today.
  `ath = float(df['high'].max())`. Both `pct_local` and `pct_ath` computed as `(level - price_10am) / level`.

  **R5** (resistance): `nearest_resistance_atr(df, price_10am, atr)` returns `None` or distance in ATR.
  `None` means no overhead resistance — treat as passing (do NOT return None).

- **VALIDATE**:
  ```bash
  uv run python -c "
  from train import screen_day
  import inspect
  src = inspect.getsource(screen_day)
  assert 'NotImplementedError' not in src, 'stub not replaced'
  print('PASS: screen_day is implemented')
  "
  ```

**Wave 2 Checkpoint**:
```bash
uv run python -c "from train import screen_day; print('PASS:', screen_day)"
```

---

### WAVE 3: Tests + Integration (Parallel)

#### Task 3.1: FILL `tests/test_screener.py` with complete test suite

- **WAVE**: 3
- **AGENT_ROLE**: test-writer
- **DEPENDS_ON**: [1.2, 2.1]
- **BLOCKS**: []
- **PROVIDES**: Passing pytest suite
- **USES_FROM_WAVE_1**: Fixtures from Task 1.2
- **USES_FROM_WAVE_2**: `screen_day` from Task 2.1
- **IMPLEMENT**:

  Append the following import block and test functions to `tests/test_screener.py`:

  ```python
  from train import (
      screen_day, calc_cci, calc_atr14, find_pivot_lows, zone_touch_count,
      find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr,
  )
  ```

  Write pytest functions for each of the following test cases. Each function must import only from
  `train` and `tests.test_screener`, use only the fixtures, and have a clear assertion.

  **Indicator unit tests** (8 tests):
  | Test function | What to assert |
  |---|---|
  | `test_calc_cci_returns_series` | `isinstance(result, pd.Series)`, `len == len(df)` |
  | `test_calc_atr14_nan_first_13_bars` | `pd.isna(atr.iloc[13])` and `not pd.isna(atr.iloc[14])` |
  | `test_find_pivot_lows_detects_explicit_pivot` | Insert clear low at row 20; assert 20 in pivot indices |
  | `test_zone_touch_count_zero_for_far_level` | `zone_touch_count(df, 1_000_000.0) == 0` |
  | `test_zone_touch_count_nonneg` | `zone_touch_count(df, df['close'].mean()) >= 0` |
  | `test_stalling_false_for_trending` | `not is_stalling_at_ceiling(make_passing_df(10))` |
  | `test_stalling_true_when_constructed` | Set last 3 highs tight + all closes below; assert True |
  | `test_resistance_none_if_no_overhead` | price above ATH → `nearest_resistance_atr` returns None |

  **`screen_day` rule tests** (9 tests):
  | Test function | How to trigger failure |
  |---|---|
  | `test_r1_fail_149_rows` | `df.iloc[:149]` → must return None |
  | `test_r1_pass_150_rows` | `df.iloc[:150]` → no exception raised |
  | `test_rule1_fail_below_sma` | Set `price_10am[-1] = 1.0` → None |
  | `test_rule2_fail_not_3_up_days` | Set `close[-2] = close[-3] * 0.99` → None |
  | `test_rule3_fail_low_volume` | Set `volume[-1] = 1.0` → None |
  | `test_r4_fail_large_upper_wick` | Set `high[-1] = close[-1] * 1.10` → None |
  | `test_screen_day_no_exception_on_pivot_df` | `make_pivot_df(250)` + last date → no exception |
  | `test_return_dict_has_stop_key` | If not None: `'stop' in result` and `isinstance(result['stop'], float)` |
  | `test_stop_always_below_entry` | If not None: `result['stop'] < result['entry_price']` |

  **Edge case tests** (2 tests):
  | Test function | What to assert |
  |---|---|
  | `test_missing_price_10am_raises` | Drop `price_10am` column → `pytest.raises(KeyError)` |
  | `test_returns_none_or_dict_only` | Result is `None` or `dict` — no other type |

- **VALIDATE**:
  ```bash
  uv run python -m pytest tests/test_screener.py -v --tb=short
  # Target: all 19 tests pass (8 indicator + 9 rule + 2 edge)
  ```

---

#### Task 3.2: VALIDATE with real parquet data (if Phase 2 complete)

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: Confirmed `screen_day` works on real data without error
- **IMPLEMENT**:
  ```bash
  uv run python -c "
  import os, sys
  cache = os.path.join(os.path.expanduser('~'), '.cache', 'autoresearch', 'stock_data')
  files = [f for f in os.listdir(cache) if f.endswith('.parquet')] if os.path.exists(cache) else []
  if not files:
      print('SKIP: no parquet cache — run prepare.py first')
      sys.exit(0)
  from train import screen_day, load_ticker_data
  df = load_ticker_data(files[0].replace('.parquet',''))
  for d in df.index[-10:]:
      d_val = d.date() if hasattr(d, 'date') else d
      result = screen_day(df.loc[:d], d_val)
  print(f'PASS: screen_day ran on {files[0]} for 10 days — last result: {result}')
  "
  ```
  If `SKIP`: acceptable. Phase 2 must complete before end-to-end validation is meaningful.

---

## TESTING STRATEGY

All tests are automated via pytest — no browser, hardware, or CAPTCHA required.

### Test Automation Summary

| Category                      | Count | % |
|-------------------------------|-------|---|
| ✅ Indicator unit tests (pytest) | 8   | 42% |
| ✅ Rule pass/fail tests (pytest) | 9   | 47% |
| ✅ Edge case tests (pytest)      | 2   | 11% |
| ⚠️ Manual                        | 0   | 0% |
| **Total**                        | 19  | 100% |

---

## VALIDATION COMMANDS

### Level 1: Imports

```bash
uv run python -c "
from train import (calc_cci, calc_atr14, find_pivot_lows, zone_touch_count,
                   find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr,
                   screen_day, manage_position, load_ticker_data, CACHE_DIR)
print('PASS: all names importable from train')
"
```

### Level 2: Unit Tests

```bash
uv run python -m pytest tests/test_screener.py -v --tb=short
```

### Level 3: Integration with Real Data

```bash
# See Task 3.2 command above (SKIP if parquet cache not populated)
```

### Level 4: Demo Run

```bash
uv run python train.py AAPL   # prints signal dict or "No cached data for AAPL"
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `train.py` is fully replaced — no `import torch`, `import kernels`, or any GPU/NLP reference remains
- [ ] All 7 indicator helpers are present in `train.py` and importable: `calc_cci`, `calc_atr14`, `find_pivot_lows`, `zone_touch_count`, `find_stop_price`, `is_stalling_at_ceiling`, `nearest_resistance_atr`
- [ ] `screen_day(df, today)` implements all 11 rules in the exact order specified in the Rule Implementation Reference table
- [ ] `screen_day` uses lowercase column names (`open`, `high`, `low`, `close`, `volume`, `price_10am`) — no capitalized yfinance column names
- [ ] `screen_day` uses `price_10am[-1]` (not `close[-1]`) as the current price for SMA, pullback, and ATR buffer comparisons
- [ ] `CACHE_DIR` constant is defined as `~/.cache/autoresearch/stock_data`
- [ ] `load_ticker_data(ticker)` reads `CACHE_DIR/{ticker}.parquet` and returns `None` if file does not exist
- [ ] `manage_position` stub is present with the correct PRD interface signature and returns `position['stop_price']` unchanged

### Error Handling & Edge Cases
- [ ] `screen_day` returns `None` (not raises) for DataFrames with < 150 rows
- [ ] `screen_day` returns `None` (not raises) when any required indicator is NaN or when volume MA30 is zero
- [ ] `screen_day` returns `None` (not raises) when ATR is zero
- [ ] `screen_day` raises `KeyError` when `price_10am` column is missing (schema contract for callers)
- [ ] When `nearest_resistance_atr` returns `None` (no overhead pivot), R5 is treated as passing — `screen_day` does NOT return `None`

### Return Contract
- [ ] `screen_day` returns only `None` or `dict` — no other type
- [ ] When a `dict` is returned, it always contains `'stop'` as a `float`
- [ ] When a `dict` is returned, `stop < entry_price` always holds

### Integration / E2E
- [ ] `uv run python train.py AAPL` runs without error (prints signal dict or "No cached data" message)
- [ ] `screen_day` runs without exception on all trailing 10 days of any available Parquet file (if Phase 2 is complete)

### Validation
- [ ] `uv run python -c "from train import calc_cci, calc_atr14, find_pivot_lows, zone_touch_count, find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr, screen_day, manage_position, load_ticker_data, CACHE_DIR; print('OK')"` exits 0
- [ ] `uv run python -m pytest tests/test_screener.py -v` passes with 0 failures and 0 errors across all 19 tests

### Non-Functional
- [ ] No `print` statements inside any helper function or `screen_day` (production code is silent per CLAUDE.md)
- [ ] `calc_cci` uses `raw=True` in `.rolling().apply()` for performance

### Out of Scope
- `manage_position` full implementation — Phase 3
- Chronological backtester loop — Phase 3
- Sharpe ratio computation and output block — Phase 3/5
- `prepare.py` rewrite — separate plan
- Pinning yfinance or other dependency versions — Phase 1 concern

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: Indicator helpers in `train.py`, all importable
- [ ] Task 1.2: `tests/__init__.py` + fixtures in `tests/test_screener.py`
- [ ] Task 2.1: `screen_day()` fully implemented (no `NotImplementedError`)
- [ ] Task 3.1: 19 test functions written and passing
- [ ] Task 3.2: Real-data smoke test passes (or SKIP)
- [ ] All 4 validation levels run
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why `price_10am` instead of `close` for entry-price comparisons

The backtester enters at `price_10am + $0.03`. The screener uses `price_10am` as "current price" so
screener conditions reflect actual entry conditions with no look-ahead from close to 10am.

### `find_stop_price` already enforces 1.5× ATR

The 1.5× check inside `find_stop_price` (on candidate pivot prices) and on the returned stop value
are both required. The outer check in `screen_day` is a safety net.

### `raw=True` in `calc_cci`

The `rolling().apply()` lambda must use `raw=True` to receive a numpy array. Without it, pandas
passes a Series which is ~10× slower — meaningful over 100 tickers × years of daily data.

### R5 `None` means no overhead resistance — treat as passing

If `nearest_resistance_atr` returns `None` (no pivot high above entry price), the stock has no
historical resistance overhead. This is a bullish signal — skip the R5 check entirely (do not reject).
Only reject when `res_atr is not None and res_atr < 2.0`.

### Column lowercase consistency

All column accesses in `train.py` must use lowercase keys. `prepare.py` is responsible for writing
lowercase columns to Parquet. If a future version of `prepare.py` deviates, `load_ticker_data` is the
right place to add `.rename(columns=str.lower)` — not inside the screener functions.
