# Phase 2: Data Layer (`prepare.py`)

**⚠️ EXECUTION RULES — READ FIRST:**
- Make ALL code changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

Rewrite `prepare.py` from the old LLM/NLP/GPU data preparation script to a stock OHLCV downloader
that feeds the backtester in `train.py`. The new script downloads historical hourly data from
yfinance for a user-configured ticker list, resamples it to daily OHLCV + `price_10am`, and caches
one Parquet file per ticker in `~/.cache/autoresearch/stock_data/`.

This is a **full file replacement**. The old `prepare.py` imports `torch`, `rustbpe`, `tiktoken`,
`requests` — all removed from `pyproject.toml` in Phase 1. Those references will break on import;
the file must be replaced before any use of the project.

**Source of truth for all behaviour**: `prd.md` — Feature 1 spec (lines 146–171) and Phase 2 tasks
(lines 462–479), and the yfinance API Specification section (lines 379–416).

## User Story

As a developer using the stock strategy backtester,
I want to run `uv run prepare.py` once to download and cache OHLCV data for my ticker list,
So that the LLM agent can run `train.py` repeatedly without re-downloading data.

## Problem Statement

The current `prepare.py` is a 200-line LLM data pipeline. It imports `torch`, `rustbpe`, and
`requests`. Phase 1 already removed those packages from `pyproject.toml`. Any import of the
current `prepare.py` will fail with `ModuleNotFoundError`. The file must be fully replaced
before Phase 3 (backtester loop) or Phase 5 (end-to-end test) can proceed.

## Solution Statement

Replace `prepare.py` with a new file structured as:

1. **User configuration block** — `TICKERS`, `BACKTEST_START`, `BACKTEST_END` (user fills before running)
2. **Derived constants** — `HISTORY_START` (2 years before `BACKTEST_START`), `CACHE_DIR`
3. **`download_ticker(ticker)`** — fetch hourly data via yfinance for `[HISTORY_START, BACKTEST_END]`
4. **`resample_to_daily(df_hourly)`** — hourly → daily OHLCV + `price_10am`; lowercase column names
5. **`validate_ticker_data(ticker, df_daily, backtest_start)`** — print warnings for insufficient history or missing 10am bars
6. **`process_ticker(ticker)`** — idempotent wrapper: skip if Parquet exists, else download → resample → validate → save
7. **Main block** — iterate `TICKERS`, call `process_ticker`, print summary

**Data contract to `train.py`**: Parquet index is `date` (Python `date` objects, named `'date'`).
Columns are `open`, `high`, `low`, `close`, `volume`, `price_10am` — all lowercase `float64`.

## Feature Metadata

**Feature Type**: Replacement (greenfield rewrite of `prepare.py`)
**Complexity**: Medium
**Primary Systems Affected**: `prepare.py` (full rewrite), `tests/test_prepare.py` (new)
**Dependencies**: `yfinance` (already in `pyproject.toml`), `pandas>=2.x`, `pyarrow>=21.x`
**Breaking Changes**: Yes — the old LLM data pipeline is deleted. Expected and documented in PRD.

---

## CONTEXT REFERENCES

### Relevant Files — READ BEFORE IMPLEMENTING

- `prd.md` lines 146–171 — Feature 1 (Data Download): schema, constants, idempotency rules
- `prd.md` lines 379–416 — yfinance API spec: exact call signature, 10am bar extraction, timezone
- `prd.md` lines 350–367 — Configuration Management: exact layout of the USER CONFIGURATION block
- `train.py` lines 1–20 — `CACHE_DIR` path and `load_ticker_data` that must read the produced Parquet

### Data Contract (what `train.py` expects)

```
Index: date  (Python date objects, index name = "date")
Columns: open, high, low, close, volume, price_10am   (all float64, lowercase)
```

`load_ticker_data(ticker)` in `train.py` calls `pd.read_parquet(path)` with no further transforms.
The Parquet written by `prepare.py` must be readable as-is.

### yfinance Call (from PRD API Specification)

```python
import yfinance as yf
ticker_obj = yf.Ticker(ticker)
df = ticker_obj.history(
    start=HISTORY_START,
    end=BACKTEST_END,
    interval="1h",
    auto_adjust=True,
    prepost=False,
)
# df.index is DatetimeIndex in UTC
```

### 10am Bar Extraction (from PRD)

```python
df.index = df.index.tz_convert("America/New_York")
df_10am = df[df.index.time == pd.Timestamp("10:00").time()]
# price_10am for each date = df_10am.loc[date, "Open"]
```

### Configuration Block Layout (from PRD lines 350–367)

```python
# ── USER CONFIGURATION ──────────────────────────────────────────────────────
TICKERS = []  # TODO: fill in ticker symbols, e.g. ["AAPL", "MSFT", "NVDA"]

BACKTEST_START = "2026-01-01"  # first day of the backtest window (inclusive)
BACKTEST_END   = "2026-03-01"  # last day of the backtest window (exclusive)
# ────────────────────────────────────────────────────────────────────────────

# Derived (do not modify)
HISTORY_START = (pd.Timestamp(BACKTEST_START) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
```

---

## PARALLEL EXECUTION STRATEGY

This feature has a single primary implementor with a sequential dependency chain. The implementation
is not large enough to benefit from splitting across multiple agents (≤ 150 lines of production code).

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: Implementation                                     │
├────────────────────────────────────────────────────────────┤
│ Task 1.1: WRITE prepare.py (full rewrite)                  │
│ Agent: implementer                                         │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests                                              │
├────────────────────────────────────────────────────────────┤
│ Task 2.1: WRITE tests/test_prepare.py (14 unit tests)      │
│ Agent: test-writer                                         │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation                                         │
├────────────────────────────────────────────────────────────┤
│ Task 3.1: RUN tests + integration smoke test               │
│ Agent: validator                                           │
└────────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Wave 1 → Wave 2**: `prepare.py` must export `resample_to_daily`, `validate_ticker_data`,
`process_ticker`, `CACHE_DIR`, `HISTORY_START`, `BACKTEST_START`, `BACKTEST_END` as importable names.

**Wave 2 → Wave 3**: `tests/test_prepare.py` imports from `prepare` using these names.

---

## STEP-BY-STEP TASKS

### WAVE 1: Implementation

#### Task 1.1: WRITE `prepare.py` (full rewrite)

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Importable `prepare.py` with all helpers

**Implementation structure** (write in this exact order):

**Section 1 — Docstring + imports**:
```python
"""
prepare.py — One-time stock data download and cache for the autoresearch backtester.

Usage:
    uv run prepare.py

Edit TICKERS, BACKTEST_START, and BACKTEST_END below before running.
Data is cached in ~/.cache/autoresearch/stock_data/{TICKER}.parquet.
Running again skips tickers whose file already exists (idempotent).
"""
import os
import sys
import warnings
from datetime import datetime

import pandas as pd
import yfinance as yf
```

**Section 2 — User configuration block** (exact layout from PRD):
```python
# ── USER CONFIGURATION ──────────────────────────────────────────────────────
TICKERS = []  # TODO: fill in ticker symbols, e.g. ["AAPL", "MSFT", "NVDA"]

BACKTEST_START = "2026-01-01"  # first day of the backtest window (inclusive)
BACKTEST_END   = "2026-03-01"  # last day of the backtest window (exclusive)
# ────────────────────────────────────────────────────────────────────────────

# Derived (do not modify)
HISTORY_START = (pd.Timestamp(BACKTEST_START) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
```

**Section 3 — `download_ticker(ticker: str) -> pd.DataFrame`**:
- Call `yf.Ticker(ticker).history(start=HISTORY_START, end=BACKTEST_END, interval="1h", auto_adjust=True, prepost=False)`
- Suppress yfinance console warnings using `warnings.filterwarnings` or catch within the function
- Return the raw DataFrame (empty DataFrame = invalid ticker; caller handles this)
- No print statements inside this function (silent production code per CLAUDE.md)

**Section 4 — `resample_to_daily(df_hourly: pd.DataFrame) -> pd.DataFrame`**:

```
Step 4a: tz_convert to America/New_York
    - df = df_hourly.copy()
    - If index is UTC-naive, localize first; otherwise tz_convert directly
    - df.index = df.index.tz_convert("America/New_York")

Step 4b: Extract price_10am
    - import datetime  (or use pd.Timestamp("10:00").time())
    - mask = df.index.time == datetime.time(10, 0)
    - df_10am = df[mask][["Open"]].copy()
    - df_10am.index = pd.Index([ts.date() for ts in df_10am.index], name="date")
    - price_10am_series = df_10am["Open"].rename("price_10am")

Step 4c: Resample to calendar-day OHLCV
    - Set index to just the date part before resampling is NOT needed — resample on DatetimeIndex
    - daily = df.resample("D").agg({"Open": "first", "High": "max", "Low": "min",
                                    "Close": "last", "Volume": "sum"})
    - Drop non-trading days (all NaN rows): daily = daily.dropna(subset=["Open"])
    - Convert index to date objects: daily.index = pd.Index([ts.date() for ts in daily.index], name="date")

Step 4d: Join price_10am and lowercase
    - daily = daily.join(price_10am_series, how="left")
    - daily.columns = [c.lower() for c in daily.columns]  # → open, high, low, close, volume, price_10am
    - daily.index.name = "date"

Return daily (pd.DataFrame)
```

**Section 5 — `validate_ticker_data(ticker: str, df: pd.DataFrame, backtest_start: str) -> None`**:
- If `len(df) < 200`: `print(f"WARNING: {ticker} has only {len(df)} rows — insufficient indicator history (need ≥ 200)")`
- Filter rows in the backtest window: `backtest_mask = df.index >= pd.Timestamp(backtest_start).date()`
- Count NaN `price_10am` in backtest rows; if any: `print(f"WARNING: {ticker} has {n} backtest days with missing price_10am")`
- No `return` value (side-effects only — prints to stdout)

**Section 6 — `process_ticker(ticker: str) -> bool`**:
```python
def process_ticker(ticker: str) -> bool:
    """Download, resample, validate, and cache one ticker. Returns True on success."""
    path = os.path.join(CACHE_DIR, f"{ticker}.parquet")
    if os.path.exists(path):
        print(f"  {ticker}: already cached, skipping")
        return True
    df_hourly = download_ticker(ticker)
    if df_hourly.empty:
        print(f"  {ticker}: no data returned — skipping")
        return False
    df_daily = resample_to_daily(df_hourly)
    validate_ticker_data(ticker, df_daily, BACKTEST_START)
    df_daily.to_parquet(path)
    print(f"  {ticker}: saved {len(df_daily)} days → {path}")
    return True
```

**Section 7 — Main block**:
```python
if __name__ == "__main__":
    if not TICKERS:
        print("ERROR: TICKERS list is empty. Edit prepare.py and add ticker symbols before running.")
        sys.exit(1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"Downloading {len(TICKERS)} tickers → {CACHE_DIR}")
    print(f"Date range: {HISTORY_START} → {BACKTEST_END} (1h bars, resampled to daily)")
    ok = 0
    for ticker in TICKERS:
        if process_ticker(ticker):
            ok += 1
    print(f"\nDone: {ok}/{len(TICKERS)} tickers cached successfully.")
```

**Important implementation notes**:
- No `print` inside `download_ticker` or `resample_to_daily` (only in `process_ticker` and main)
- `validate_ticker_data` prints warnings — this is intentional and expected
- The empty-TICKERS check must use `sys.exit(1)` to fail visibly
- Do NOT use `pd.Timestamp(...).date()` for the index; use `ts.date()` in a list comprehension to avoid dtype issues

**VALIDATE**:
```bash
uv run python -c "
from prepare import (resample_to_daily, validate_ticker_data, process_ticker,
                     CACHE_DIR, HISTORY_START, BACKTEST_START, BACKTEST_END, TICKERS)
print('PASS: all names importable from prepare')
print(f'  HISTORY_START={HISTORY_START}, CACHE_DIR={CACHE_DIR}')
"
```

---

### WAVE 2: Tests

#### Task 2.1: WRITE `tests/test_prepare.py` (14 unit tests)

- **WAVE**: 2
- **AGENT_ROLE**: test-writer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Passing pytest suite for `prepare.py`

**Implementation**:

Create `tests/test_prepare.py`. Use `unittest.mock.patch` (stdlib — no extra dependencies).

**Imports and helper factory**:
```python
"""tests/test_prepare.py — Unit tests for prepare.py data layer."""
import io
import os
import sys
import datetime
import unittest.mock as mock
import pytest
import numpy as np
import pandas as pd

from prepare import resample_to_daily, validate_ticker_data, process_ticker, CACHE_DIR, BACKTEST_START


def _make_hourly_df(n_days: int = 10) -> pd.DataFrame:
    """
    Synthetic hourly DataFrame mimicking yfinance output.
    Creates n_days of trading data, 7 hourly bars per day (9:30–15:30 ET),
    with a guaranteed 10:00 AM bar each day.
    UTC-aware DatetimeIndex.
    """
    rows = []
    base_date = datetime.date(2025, 1, 2)  # Thursday, a trading day
    day = 0
    trading_days_made = 0
    while trading_days_made < n_days:
        # Skip weekends
        d = base_date + datetime.timedelta(days=day)
        if d.weekday() >= 5:
            day += 1
            continue
        # 7 bars: 9:30, 10:00, 10:30, 11:00, 11:30, 12:00, 15:00 (Eastern)
        for hour, minute in [(9,30),(10,0),(10,30),(11,0),(11,30),(12,0),(15,0)]:
            et = datetime.datetime(d.year, d.month, d.day, hour, minute,
                                   tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
            utc = et.astimezone(datetime.timezone.utc)
            price = 100.0 + trading_days_made + (hour - 9) * 0.1
            rows.append({
                "datetime": utc,
                "Open": price,
                "High": price + 0.5,
                "Low": price - 0.5,
                "Close": price + 0.1,
                "Volume": 100_000.0,
            })
        trading_days_made += 1
        day += 1
    df = pd.DataFrame(rows).set_index("datetime")
    df.index = pd.DatetimeIndex(df.index)
    return df
```

**Resampling tests** (5 tests):

| Test | What to assert |
|------|----------------|
| `test_resample_produces_expected_columns` | Output has exactly `{open, high, low, close, volume, price_10am}` |
| `test_price_10am_is_open_of_10am_bar` | For each date, `price_10am` equals the `Open` of the 10:00 AM bar in the hourly input |
| `test_open_is_first_bar_of_day` | `open` column equals the `Open` of the 9:30 AM bar for each day |
| `test_high_is_max_of_day` | `high` = `max(High)` across all hourly bars for each day |
| `test_non_trading_days_excluded` | Output has no rows for weekend dates (no NaN rows present) |

**Schema tests** (3 tests):

| Test | What to assert |
|------|----------------|
| `test_index_is_date_objects` | All index values are `datetime.date` instances (not `pd.Timestamp`) |
| `test_index_name_is_date` | `df.index.name == "date"` |
| `test_all_columns_lowercase` | `all(c == c.lower() for c in df.columns)` |

**Caching / idempotency tests** (3 tests):

| Test | How to test | What to assert |
|------|-------------|----------------|
| `test_process_ticker_skips_existing_file` | Create empty file at expected path; mock `download_ticker`; call `process_ticker` | `download_ticker` is never called |
| `test_process_ticker_skips_empty_download` | Mock `download_ticker` to return empty DataFrame; mock `resample_to_daily` | `resample_to_daily` is never called; return False |
| `test_process_ticker_saves_parquet` | Mock `download_ticker` to return `_make_hourly_df(5)`; use `tmp_path`; patch `CACHE_DIR` | Parquet file written at expected path; `pd.read_parquet` loads it without error |

Implementation note for `test_process_ticker_saves_parquet`:
```python
def test_process_ticker_saves_parquet(tmp_path):
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.download_ticker", return_value=_make_hourly_df(5)):
        result = process_ticker("TEST")
    assert result is True
    path = tmp_path / "TEST.parquet"
    assert path.exists()
    loaded = pd.read_parquet(path)
    assert "price_10am" in loaded.columns
```

**Validation warning tests** (3 tests):

| Test | Setup | What to assert |
|------|-------|----------------|
| `test_warn_if_fewer_than_200_rows` | DataFrame with 150 rows | `capsys.readouterr().out` contains "WARNING" and "150" |
| `test_no_warn_if_200_rows` | DataFrame with 200 rows | stdout contains no "WARNING" about history |
| `test_warn_if_missing_10am_on_backtest_dates` | daily df with NaN `price_10am` on a date ≥ `BACKTEST_START` | stdout contains "WARNING" and "missing price_10am" |

For validation tests, build a minimal daily DataFrame (not hourly — pass directly to `validate_ticker_data`):
```python
def _make_daily_df(n_rows: int, backtest_start: str = BACKTEST_START,
                   nan_10am: bool = False) -> pd.DataFrame:
    start = pd.Timestamp(backtest_start).date()
    dates = [start + datetime.timedelta(days=i) for i in range(n_rows)]
    price_10am = [np.nan if nan_10am and i < 3 else 100.0 + i for i in range(n_rows)]
    return pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1_000_000.0, "price_10am": price_10am,
    }, index=pd.Index(dates, name="date"))
```

**Integration test** (1 test — live network, `@pytest.mark.integration`):

| Test | What to assert |
|------|----------------|
| `test_download_ticker_returns_expected_schema` | Live `download_ticker("AAPL")` + `resample_to_daily` produces correct columns, date index, and non-empty result; skips (not fails) if yfinance returns empty |

```python
@pytest.mark.integration
def test_download_ticker_returns_expected_schema():
    """Live yfinance call — requires internet. Verifies schema contract end-to-end."""
    from prepare import download_ticker, resample_to_daily
    df_hourly = download_ticker("AAPL")
    if df_hourly.empty:
        pytest.skip("yfinance returned empty — network unavailable")
    df_daily = resample_to_daily(df_hourly)
    assert set(df_daily.columns) == {"open", "high", "low", "close", "volume", "price_10am"}
    assert all(isinstance(d, datetime.date) for d in df_daily.index)
    assert df_daily.index.name == "date"
    assert len(df_daily) > 0
```

**Main block test** (1 test — subprocess):

| Test | What to assert |
|------|----------------|
| `test_main_exits_1_when_tickers_empty` | `subprocess.run(["uv", "run", "python", "prepare.py"])` returns exit code 1 and stdout contains "TICKERS" |

```python
def test_main_exits_1_when_tickers_empty():
    """Main block exits 1 with clear message when TICKERS list is empty."""
    import subprocess
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    result = subprocess.run(
        ["uv", "run", "python", "prepare.py"],
        capture_output=True,
        cwd=project_root,
    )
    assert result.returncode == 1
    assert b"TICKERS" in result.stdout
```

Note: this test requires `TICKERS = []` (the default) in `prepare.py`. The executor must NOT populate TICKERS before running the test suite.

**VALIDATE**:
```bash
uv run python -m pytest tests/test_prepare.py -v --tb=short
# Target: 16 tests pass, 0 failures (including @pytest.mark.integration)
```

---

### WAVE 3: Validation

#### Task 3.1: RUN tests + integration smoke test

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: Confirmed all tests pass; confirmed import works; integration test attempted

**Step 3a — Level 1: imports**:
```bash
uv run python -c "
from prepare import (resample_to_daily, validate_ticker_data, process_ticker,
                     CACHE_DIR, HISTORY_START, BACKTEST_START, BACKTEST_END, TICKERS,
                     download_ticker)
print('PASS: all names importable from prepare')
print(f'  TICKERS={TICKERS!r}')
print(f'  HISTORY_START={HISTORY_START}')
print(f'  CACHE_DIR={CACHE_DIR}')
"
```

**Step 3b — Level 2: unit tests**:
```bash
uv run python -m pytest tests/test_prepare.py -v --tb=short
```
All 14 tests must pass.

**Step 3c — Level 3: existing screener tests still pass**:
```bash
uv run python -m pytest tests/test_screener.py -v --tb=short
```
Must remain 20/20 (no regressions from changes to shared infrastructure).

**Step 3d — Level 4: run full test suite including integration**:
```bash
uv run python -m pytest tests/test_prepare.py -v --tb=short
# test_download_ticker_returns_expected_schema will SKIP if network unavailable, not FAIL
```

---

## TESTING STRATEGY

All 16 tests are automated via pytest. 14 use `unittest.mock` (no network required). 1 makes a live
yfinance call marked `@pytest.mark.integration` (skips gracefully if network unavailable). 1 uses
`subprocess.run` to test the main block guard.

### Test Enumeration (required per CLAUDE.md)

| # | Test function | Category | Happy/Error/Auth |
|---|---------------|----------|------------------|
| 1 | `test_resample_produces_expected_columns` | Resampling | Happy path |
| 2 | `test_price_10am_is_open_of_10am_bar` | Resampling | Happy path |
| 3 | `test_open_is_first_bar_of_day` | Resampling | Happy path |
| 4 | `test_high_is_max_of_day` | Resampling | Happy path |
| 5 | `test_non_trading_days_excluded` | Resampling | Edge case |
| 6 | `test_index_is_date_objects` | Schema | Happy path |
| 7 | `test_index_name_is_date` | Schema | Happy path |
| 8 | `test_all_columns_lowercase` | Schema | Happy path |
| 9 | `test_process_ticker_skips_existing_file` | Idempotency | Primary error path |
| 10 | `test_process_ticker_skips_empty_download` | Idempotency | Primary error path |
| 11 | `test_process_ticker_saves_parquet` | Caching | Happy path |
| 12 | `test_warn_if_fewer_than_200_rows` | Validation | Error path |
| 13 | `test_no_warn_if_200_rows` | Validation | Happy path |
| 14 | `test_warn_if_missing_10am_on_backtest_dates` | Validation | Error path |
| 15 | `test_download_ticker_returns_expected_schema` | Integration (live network) | Happy path |
| 16 | `test_main_exits_1_when_tickers_empty` | Main block | Primary error path |

### Coverage Analysis

**New code paths in `prepare.py`**:
- `download_ticker` — mocked in tests 9, 10, 11; live schema verified in test 15
- `resample_to_daily` (10am extraction) — ✅ test 2
- `resample_to_daily` (OHLCV aggregation) — ✅ tests 3, 4
- `resample_to_daily` (weekend exclusion) — ✅ test 5
- `resample_to_daily` (column lowercase, index naming) — ✅ tests 6, 7, 8
- `validate_ticker_data` (< 200 rows branch) — ✅ test 12
- `validate_ticker_data` (≥ 200 rows, no warn) — ✅ test 13
- `validate_ticker_data` (missing price_10am branch) — ✅ test 14
- `process_ticker` (skip existing) — ✅ test 9
- `process_ticker` (empty download) — ✅ test 10
- `process_ticker` (happy path: download → resample → save) — ✅ test 11
- Main block (TICKERS empty guard) — ✅ test 16 (subprocess.run, checks exit code + stdout)
- Main block (os.makedirs, full loop) — ⚠️ loop body covered by test 11 (process_ticker happy path); makedirs side-effect not independently tested (low risk)

**Existing code re-validated**:
- `tests/test_screener.py` (20 tests) — re-run in Step 3c to confirm no regressions

### Test Automation Summary

| Category | Count | % | Tool |
|----------|-------|---|------|
| ✅ Automated (pytest + mock) | 14 | 87.5% | pytest, unittest.mock |
| ✅ Automated (pytest + live network, skippable) | 1 | 6.25% | pytest, @pytest.mark.integration |
| ✅ Automated (pytest + subprocess) | 1 | 6.25% | pytest, subprocess.run |
| ⚠️ Manual | 0 | 0% | — |
| **Total** | 16 | 100% | |

---

## VALIDATION COMMANDS

### Level 1: Imports

```bash
uv run python -c "
from prepare import (resample_to_daily, validate_ticker_data, process_ticker,
                     download_ticker, CACHE_DIR, HISTORY_START, BACKTEST_START,
                     BACKTEST_END, TICKERS)
print('PASS: all names importable from prepare')
"
```

### Level 2: Unit Tests

```bash
uv run python -m pytest tests/test_prepare.py -v --tb=short
# Expected: 16 passed (15 always; test 15 skips if network unavailable)
```

### Level 3: No Regressions

```bash
uv run python -m pytest tests/test_screener.py -v --tb=short
# Expected: 20 passed (same as before this plan)
```

### Level 4: Full Suite (includes integration + subprocess tests)

```bash
uv run python -m pytest tests/test_prepare.py -v --tb=short
# test_download_ticker_returns_expected_schema: PASS or SKIP (never FAIL) depending on network
# test_main_exits_1_when_tickers_empty: always runs via subprocess
```

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] `prepare.py` is fully replaced — no `import torch`, `import rustbpe`, `import tiktoken`, or `import requests` remains
- [ ] User configuration block is at the top, exactly matching the PRD layout: `TICKERS = []`, `BACKTEST_START`, `BACKTEST_END`, and derived `HISTORY_START`, `CACHE_DIR`
- [ ] `download_ticker(ticker)` calls `yf.Ticker(ticker).history(interval="1h", auto_adjust=True, prepost=False)` for the correct date range
- [ ] `resample_to_daily(df_hourly)` produces a DataFrame with index of Python `date` objects named `"date"` and columns `open, high, low, close, volume, price_10am` (all lowercase `float64`)
- [ ] `price_10am` column contains the `Open` price of the 10:00 AM ET bar for each trading day
- [ ] `resample_to_daily` excludes non-trading days (no NaN-only rows in output)
- [ ] `process_ticker(ticker)` is idempotent: skips the download if `{CACHE_DIR}/{ticker}.parquet` already exists
- [ ] `process_ticker` returns `False` and prints a skip message when yfinance returns an empty DataFrame
- [ ] `validate_ticker_data` prints a `WARNING` to stdout when `len(df) < 200`
- [ ] `validate_ticker_data` prints a `WARNING` to stdout when any row in the backtest window has `NaN` `price_10am`
- [ ] The main block raises `sys.exit(1)` with a clear error message when `TICKERS` is empty

### Data Contract Compliance

- [ ] Parquet files written by `prepare.py` are loadable by `pd.read_parquet` with the correct schema (verified by `test_process_ticker_saves_parquet`)
- [ ] The Parquet schema matches what `load_ticker_data` in `train.py` expects (index `date`, columns lowercase)

### Quality

- [ ] No `print` statements inside `download_ticker` or `resample_to_daily` (production code silent per CLAUDE.md)
- [ ] `validate_ticker_data` prints are warnings — acceptable as per PRD spec

### Validation

- [ ] `uv run python -c "from prepare import resample_to_daily, validate_ticker_data, process_ticker, download_ticker, CACHE_DIR, HISTORY_START, BACKTEST_START, BACKTEST_END, TICKERS; print('OK')"` exits 0
- [ ] `uv run python -m pytest tests/test_prepare.py -v` passes 14/14
- [ ] `uv run python -m pytest tests/test_screener.py -v` still passes 20/20 (no regressions)

### Out of Scope

- Running `prepare.py` end-to-end against live yfinance API — Phase 5 (End-to-End Test)
- `train.py` backtester loop — Phase 3
- `program.md` rewrite — Phase 4
- Parallel ticker downloads — out of scope per PRD

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: `prepare.py` rewritten — all 6 sections implemented, imports correct
- [ ] Task 2.1: `tests/test_prepare.py` created with 16 tests (14 mock + 1 integration + 1 subprocess)
- [ ] Task 3.1 Step 3a: Level 1 imports pass
- [ ] Task 3.1 Step 3b: 15/16 tests pass; test 15 PASS or SKIP (never FAIL)
- [ ] Task 3.1 Step 3c: 20/20 screener tests still pass (no regressions)
- [ ] Task 3.1 Step 3d: Full suite run confirmed
- [ ] All acceptance criteria checked
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why `date` objects (not `pd.Timestamp`) for the index

`train.py`'s `screen_day(df, today)` function receives `today` as a Python `date` object and calls
`df.loc[:today]` to slice. If the Parquet index contains `pd.Timestamp` objects (datetime), the
comparison with a `date` object may fail or behave unexpectedly depending on the pandas version.
Using `date` objects in both the index and the slice argument eliminates this ambiguity.

### `resample("D")` includes non-trading days

Daily resampling with `resample("D")` fills all calendar days (including weekends and holidays)
with NaN. The `dropna(subset=["Open"])` step removes these empty rows. After the drop, only
actual trading days (where yfinance returned data) remain.

### `price_10am` may be NaN on some dates

Some trading days do not have a 10:00 AM bar in the yfinance hourly data (e.g., half-days, early
closes, or data gaps). These produce `NaN` in `price_10am` after the left join. `validate_ticker_data`
warns about these gaps; the backtester in `train.py` must handle NaN `price_10am` gracefully.

### yfinance returns capitalized column names

`yf.Ticker.history()` returns columns `Open`, `High`, `Low`, `Close`, `Volume` (capitalized).
The `resample_to_daily` function is responsible for lowercasing these into the canonical schema.
No other function in `prepare.py` should hardcode the capitalized form — always go through
`resample_to_daily` before accessing columns.

### Empty TICKERS guard is mandatory

If `TICKERS = []` and the main block runs silently, the user would see no output and no data.
The `sys.exit(1)` guard ensures a visible error when the user forgets to configure tickers.
