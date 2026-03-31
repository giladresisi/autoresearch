# Feature: Data Layer Abstraction (Multi-Source / Multi-Interval)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Add a `data/` package that abstracts stock data fetching behind a `DataSource` protocol. Supports multiple sources (yfinance, IB-Gateway via ib_insync) and multiple fetch intervals ("1h", "1m", etc.). The cache is restructured to store data per interval per ticker at `{CACHE_DIR}/{interval}/{TICKER}.parquet`. prepare.py becomes the single source of truth for ticker universe and date range (written to a manifest), so train.py no longer owns those constants.

## User Story

As a trader/researcher running the auto-co-trader backtester,
I want to switch data sources and fetch intervals without modifying the strategy or harness,
So that I can compare yfinance vs IB-Gateway data quality and use higher-resolution intervals (e.g. 1m) for more accurate price_1030am computation, while keeping train.py's daily-bar strategy logic completely unchanged.

## Problem Statement

- Data fetching (yfinance, `download_ticker()`) is hardcoded in prepare.py with no abstraction layer
- Cache files are flat `{TICKER}.parquet` with no encoding of fetch interval; multiple intervals would collide
- `BACKTEST_START` and `BACKTEST_END` are duplicated in both prepare.py and train.py — they can drift out of sync
- No path to add IB-Gateway as a data source without rewriting prepare.py

## Solution Statement

Create `data/sources.py` with a `DataSource` protocol and two implementations (`YFinanceSource`, `IBGatewaySource`). Refactor prepare.py to use this abstraction and write data into `{CACHE_DIR}/{interval}/` subdirectories plus a `manifest.json` describing the run configuration. Update train.py loaders to read from the interval subdirectory and derive `BACKTEST_START`/`BACKTEST_END` from the manifest.

## Feature Metadata

**Feature Type**: Refactor + New Capability
**Complexity**: Medium
**Primary Systems Affected**: `prepare.py`, `train.py`, new `data/` package
**Dependencies**: `ib_insync>=0.9` (new), existing `yfinance`, `pandas`, `pyarrow`
**Breaking Changes**: Yes — cache directory layout changes. Old flat `{TICKER}.parquet` files must be deleted; re-run `prepare.py` to populate the new `{interval}/` subdirectory structure.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `prepare.py` (lines 1–289) — Full file; `download_ticker()` (109–121) moves into `YFinanceSource.fetch()`; `process_ticker()` (258–274) updated for new cache path and DataSource; `write_trend_summary()` (198–255) updated for interval subdirectory
- `train.py` (lines 1–108) — Constants and loader functions; `BACKTEST_START`/`BACKTEST_END` (23–24) replaced with manifest load; `load_ticker_data()` (89–94) and `load_all_ticker_data()` (97–107) updated for `{CACHE_DIR}/{interval}/` path
- `test_ib_connection.py` (lines 1–46) — IB connection pattern to follow: `IB()`, `ib.connect('127.0.0.1', 4002, clientId=1)`, `reqHistoricalData(..., barSizeSetting='1 hour', whatToShow='TRADES', useRTH=False, formatDate=1)`
- `tests/test_prepare.py` (lines 134–163) — Cache path tests that will need updating for interval subdirectory

### New Files to Create

- `data/__init__.py` — Empty package marker
- `data/sources.py` — `DataSource` protocol, `YFinanceSource`, `IBGatewaySource`
- `tests/test_data_sources.py` — Unit tests for both sources (YFinance with mocks, IB with mocked ib_insync)

### Files to Modify

- `prepare.py` — Use DataSource abstraction; cache path → `{CACHE_DIR}/{interval}/{TICKER}.parquet`; write `manifest.json`
- `train.py` — Replace hardcoded constants with manifest loader; update cache loaders
- `pyproject.toml` — Add `ib_insync>=0.9` dependency
- `tests/test_prepare.py` — Fix cache-path expectations for interval subdir; add manifest tests

### Patterns to Follow

**Naming Conventions**: snake_case functions, SCREAMING_SNAKE_CASE constants, lowercase DataFrame columns
**Error Handling**: Return `None` / empty DataFrame on failure; print warning to stdout; no exceptions propagated to caller (matches existing `process_ticker` style)
**Env var overrides**: `os.environ.get("VAR_NAME", default)` pattern (matches `AUTORESEARCH_CACHE_DIR`, `PREPARE_WORKERS`)
**Test fixtures**: `tmp_path` pytest fixture for temp directories; `mock.patch` for external calls (see `test_process_ticker_skips_existing_file`)

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundations (Fully Parallel)                        │
├─────────────────────────────────────────────────────────────┤
│ Task 1.1: CREATE data/sources.py (skeleton + YFinance)      │
│ Agent: data-layer-specialist                                │
├─────────────────────────────────────────────────────────────┤
│ Task 1.2: UPDATE pyproject.toml (add ib_insync)             │
│ Agent: config-specialist                                    │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: IBGatewaySource (After Wave 1)                      │
├─────────────────────────────────────────────────────────────┤
│ Task 2.1: ADD IBGatewaySource to data/sources.py            │
│ Agent: data-layer-specialist                                │
│ Deps: 1.1, 1.2                                              │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: Core refactors (Parallel after Wave 2)              │
├─────────────────────────────────────────────────────────────┤
│ Task 3.1: REFACTOR prepare.py           │ Task 3.2: CREATE  │
│ Agent: backend-specialist               │ tests/test_data_  │
│ Deps: 2.1                               │ sources.py        │
│                                         │ Agent: test-spec  │
│                                         │ Deps: 2.1         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 4: train.py + test_prepare.py (Parallel after Wave 3)  │
├─────────────────────────────────────────────────────────────┤
│ Task 4.1: UPDATE train.py               │ Task 4.2: UPDATE  │
│ Agent: backend-specialist               │ tests/test_       │
│ Deps: 3.1                               │ prepare.py        │
│                                         │ Agent: test-spec  │
│                                         │ Deps: 3.1         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 5: Validation (Sequential)                             │
├─────────────────────────────────────────────────────────────┤
│ Task 5.1: Run full test suite; confirm all pass             │
│ Deps: 4.1, 4.2                                              │
└─────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 — no dependencies between them
**Wave 2 — Sequential**: Task 2.1 — needs data/sources.py skeleton from 1.1 and ib_insync available from 1.2
**Wave 3 — Parallel after Wave 2**: Tasks 3.1 and 3.2 share the IBGatewaySource interface contract but do not modify each other's files
**Wave 4 — Parallel after Wave 3**: Tasks 4.1 and 4.2 share the manifest schema contract but modify different files
**Wave 5 — Sequential**: Task 5.1 — needs all code and tests to be in place

### Interface Contracts

**Contract Wave1→Wave2**: `DataSource` protocol defined in `data/sources.py` with signature:
```python
class DataSource(Protocol):
    def fetch(self, ticker: str, start: str, end: str, interval: str) -> pd.DataFrame | None: ...
```
`YFinanceSource.fetch()` returns a tz-aware DatetimeIndex DataFrame with columns `Open, High, Low, Close, Volume` (uppercase, matching yfinance output before `resample_to_daily()` lowercases them).

**Contract Wave2→Wave3 (sources → prepare.py)**: `IBGatewaySource` and `YFinanceSource` both expose `.fetch(ticker, start, end, interval)` returning the same DataFrame schema. `prepare.py` calls `source.fetch(...)` then passes result to existing `resample_to_daily()`.

**Contract Wave3→Wave4 (manifest schema)**:
```json
{
  "tickers": ["AAPL", ...],
  "backtest_start": "2024-09-01",
  "backtest_end": "2026-03-20",
  "fetch_interval": "1h",
  "source": "yfinance"
}
```
Written to `{CACHE_DIR}/manifest.json` by the `__main__` block in prepare.py.

**Contract Wave3→Wave4 (cache path)**: `{CACHE_DIR}/{interval}/{TICKER}.parquet` — both `prepare.py` write and `train.py` read use this layout.

### Synchronization Checkpoints

**After Wave 2**: `python -c "from data.sources import YFinanceSource, IBGatewaySource, DataSource; print('imports ok')"` must succeed
**After Wave 3**: `uv run pytest tests/test_data_sources.py -v` must pass; `python -c "import prepare; print('prepare imports ok')"` must succeed
**After Wave 4**: `uv run pytest tests/test_prepare.py tests/test_data_sources.py -v` must pass; `python -c "import train; print('manifest loaded:', train.BACKTEST_START)"` must succeed (requires manifest.json to exist)
**After Wave 5**: `uv run pytest -m "not integration" -v` — full suite green

---

## IMPLEMENTATION PLAN

### Phase 1: data/ package skeleton + dependency

Create the `data/` package with the `DataSource` protocol and `YFinanceSource` implementation, and register `ib_insync` in pyproject.toml.

### Phase 2: IBGatewaySource

Implement the real IB-Gateway connector using `ib_insync`. Key design decisions:
- Interval normalization: yfinance-style strings → IB barSizeSetting and durationStr
- Pagination for high-frequency intervals (1m: ≤29-day chunks; 1h: ≤180-day chunks)
- `useRTH=True` for stocks (regular trading hours only, matching yfinance `prepost=False`)
- `formatDate=2` returns datetime objects directly
- Earnings dates always come from yfinance regardless of data source (IB has no calendar endpoint)

### Phase 3: prepare.py refactor + tests

Refactor prepare.py to:
1. Select `DataSource` from `PREPARE_SOURCE` env var (yfinance/ib)
2. Use `PREPARE_INTERVAL` env var (default "1h")
3. Write to `{CACHE_DIR}/{interval}/{TICKER}.parquet`
4. Write `{CACHE_DIR}/manifest.json` after all tickers processed
5. Update `write_trend_summary()` to accept and use `interval` parameter

Write `tests/test_data_sources.py` with mocked sources.

### Phase 4: train.py update + test_prepare.py update

Update train.py to load `BACKTEST_START`/`BACKTEST_END` from manifest and read from interval subdirectory. Update existing test_prepare.py cache-path assertions for new layout and add manifest tests.

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Foundation

#### Task 1.1: CREATE data/\_\_init\_\_.py + data/sources.py (skeleton + YFinanceSource)

- **WAVE**: 1
- **AGENT_ROLE**: data-layer-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `DataSource` protocol + `YFinanceSource` with `.fetch()` returning tz-aware DataFrame with uppercase OHLCV columns

**IMPLEMENT**:

1. Create `data/__init__.py` — empty file (package marker only)

2. Create `data/sources.py` with:

```python
"""data/sources.py — DataSource protocol and concrete implementations."""
from __future__ import annotations
import datetime
import warnings
from typing import Protocol
import pandas as pd
import yfinance as yf


class DataSource(Protocol):
    """Fetch raw OHLCV bars for a ticker over a date range at a given interval.

    Returns a DataFrame with a tz-aware DatetimeIndex and columns:
        Open, High, Low, Close, Volume  (uppercase, float64)
    Returns None on failure (network error, no data, unknown ticker).
    """
    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str,
    ) -> pd.DataFrame | None: ...


class YFinanceSource:
    """Fetch OHLCV from Yahoo Finance via yfinance."""

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1h",
    ) -> pd.DataFrame | None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                prepost=False,
            )
        if df.empty:
            return None
        # Normalize column names to uppercase OHLCV (yfinance may vary)
        df = df.rename(columns={c: c.capitalize() for c in df.columns})
        return df[["Open", "High", "Low", "Close", "Volume"]]
```

- **VALIDATE**: `python -c "from data.sources import DataSource, YFinanceSource; s = YFinanceSource(); print('ok')"`

#### Task 1.2: UPDATE pyproject.toml — add ib_insync dependency

- **WAVE**: 1
- **AGENT_ROLE**: config-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `ib_insync` importable in the environment

**IMPLEMENT**:

In `pyproject.toml`, add `"ib_insync>=0.9"` to the `dependencies` list:

```toml
dependencies = [
    "ib-insync>=0.9",
    "matplotlib>=3.10.8",
    "numpy>=2.2.6",
    "pandas>=2.3.3",
    "pyarrow>=21.0.0",
    "yfinance",
]
```

- **VALIDATE**: `uv pip install ib-insync && python -c "import ib_insync; print(ib_insync.__version__)"`

**Wave 1 Checkpoint**: `python -c "from data.sources import YFinanceSource; import ib_insync; print('wave1 ok')"`

---

### WAVE 2: IBGatewaySource

#### Task 2.1: ADD IBGatewaySource to data/sources.py

- **WAVE**: 2
- **AGENT_ROLE**: data-layer-specialist
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [3.1, 3.2]
- **PROVIDES**: `IBGatewaySource` class with `.fetch()` matching `DataSource` protocol; interval mapping table; pagination logic

**IMPLEMENT**:

Append `IBGatewaySource` to `data/sources.py`:

```python
from ib_insync import IB, Stock, util


# Maps yfinance-style interval strings → IB barSizeSetting
_IB_BAR_SIZE: dict[str, str] = {
    "1m":  "1 min",
    "2m":  "2 mins",
    "5m":  "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "2h":  "2 hours",
    "4h":  "4 hours",
    "1d":  "1 day",
}

# Max calendar days to request per IB call at each interval
# (conservative — IB limits vary by subscription; these are safe defaults)
_IB_CHUNK_DAYS: dict[str, int] = {
    "1m":  29,
    "2m":  29,
    "5m":  60,
    "15m": 60,
    "30m": 180,
    "1h":  180,
    "2h":  365,
    "4h":  365,
    "1d":  3650,
}


class IBGatewaySource:
    """Fetch OHLCV from Interactive Brokers via ib_insync.

    Requires IB Gateway (or TWS) running at host:port with API enabled.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1h",
    ) -> pd.DataFrame | None:
        if interval not in _IB_BAR_SIZE:
            print(f"  IBGatewaySource: unsupported interval '{interval}'")
            return None

        bar_size = _IB_BAR_SIZE[interval]
        chunk_days = _IB_CHUNK_DAYS[interval]

        ib = IB()
        try:
            ib.connect(self.host, self.port, clientId=self.client_id)
            contract = Stock(ticker, "SMART", "USD")
            ib.qualifyContracts(contract)

            start_dt = pd.Timestamp(start)
            end_dt   = pd.Timestamp(end)
            all_bars: list = []

            # Paginate backwards from end_dt to start_dt in chunk_days windows
            chunk_end = end_dt
            while chunk_end > start_dt:
                chunk_start = max(start_dt, chunk_end - pd.Timedelta(days=chunk_days))
                duration_days = (chunk_end - chunk_start).days
                duration_str = f"{duration_days} D"

                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                    durationStr=duration_str,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=2,  # returns datetime objects
                )
                if bars:
                    all_bars.extend(bars)
                chunk_end = chunk_start

            if not all_bars:
                return None

            df = util.df(all_bars)
            df = df.rename(columns={
                "date":   "datetime",
                "open":   "Open",
                "high":   "High",
                "low":    "Low",
                "close":  "Close",
                "volume": "Volume",
            })
            df = df.set_index("datetime")
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("America/New_York")
            else:
                df.index = df.index.tz_convert("America/New_York")
            df = df.sort_index()
            # Deduplicate bars that appear in multiple pagination windows
            df = df[~df.index.duplicated(keep="last")]
            # Trim to requested window
            df = df[(df.index >= start_dt.tz_localize("America/New_York")) &
                    (df.index <  end_dt.tz_localize("America/New_York"))]
            return df[["Open", "High", "Low", "Close", "Volume"]]

        except Exception as e:
            print(f"  IBGatewaySource: error fetching {ticker}: {e}")
            return None
        finally:
            if ib.isConnected():
                ib.disconnect()
```

- **VALIDATE**: `python -c "from data.sources import IBGatewaySource; print('IBGatewaySource import ok')"`

**Wave 2 Checkpoint**: `python -c "from data.sources import YFinanceSource, IBGatewaySource, DataSource; print('wave2 ok')"`

---

### WAVE 3: prepare.py refactor + tests/test_data_sources.py

#### Task 3.1: REFACTOR prepare.py — DataSource abstraction, interval subdir, manifest

- **WAVE**: 3
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [4.1, 4.2]
- **PROVIDES**: new cache layout at `{CACHE_DIR}/{interval}/{TICKER}.parquet`; `manifest.json` written by `__main__`

**IMPLEMENT** the following changes to `prepare.py`:

1. **Add imports** at top:
```python
import json
from pathlib import Path
from data.sources import DataSource, YFinanceSource, IBGatewaySource
```

2. **Add new env-var constants** (after `MAX_WORKERS` line):
```python
# Data source selection: "yfinance" (default) or "ib"
PREPARE_SOURCE = os.environ.get("PREPARE_SOURCE", "yfinance")
# Fetch interval passed to DataSource.fetch(): "1h" (default), "1m", "1d", etc.
PREPARE_INTERVAL = os.environ.get("PREPARE_INTERVAL", "1h")
```

3. **Replace `download_ticker()`** — the function is deleted; its logic now lives in `YFinanceSource.fetch()`. Any remaining internal callers within prepare.py use `source.fetch(ticker, HISTORY_START, BACKTEST_END, PREPARE_INTERVAL)` instead.

4. **Update `process_ticker()`** — change signature to `process_ticker(ticker: str, source: DataSource, interval: str) -> bool`:
   - Cache path: `os.path.join(CACHE_DIR, interval, f"{ticker}.parquet")`
   - `os.makedirs(os.path.join(CACHE_DIR, interval), exist_ok=True)` before writing
   - Replace `download_ticker(ticker)` call with `source.fetch(ticker, HISTORY_START, BACKTEST_END, interval)`
   - For earnings dates: always use `yf.Ticker(ticker)` regardless of source (unchanged)

5. **Update `write_trend_summary()`** — add `interval: str` parameter:
   - Change internal path from `os.path.join(cache_dir, f"{ticker}.parquet")` to `os.path.join(cache_dir, interval, f"{ticker}.parquet")`
   - Update all callers

6. **Add `write_manifest()` function**:
```python
def write_manifest(
    tickers: list[str],
    backtest_start: str,
    backtest_end: str,
    fetch_interval: str,
    source_name: str,
    cache_dir: str,
) -> None:
    """Write run configuration to {cache_dir}/manifest.json."""
    manifest = {
        "tickers": tickers,
        "backtest_start": backtest_start,
        "backtest_end": backtest_end,
        "fetch_interval": fetch_interval,
        "source": source_name,
    }
    path = os.path.join(cache_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"manifest.json written -> {path}")
```

7. **Update `__main__` block**:
   - Instantiate source based on `PREPARE_SOURCE`:
     ```python
     if PREPARE_SOURCE == "ib":
         source = IBGatewaySource()
     else:
         source = YFinanceSource()
     ```
   - Pass `source` and `PREPARE_INTERVAL` to each `process_ticker()` call via `executor.map`
   - After results, call `write_manifest(TICKERS, BACKTEST_START, BACKTEST_END, PREPARE_INTERVAL, PREPARE_SOURCE, CACHE_DIR)`
   - Call `write_trend_summary(TICKERS, BACKTEST_START, BACKTEST_END, CACHE_DIR, PREPARE_INTERVAL)` (add `interval` arg)

- **VALIDATE**: `python -c "import prepare; print('prepare imports ok')"`

#### Task 3.2: CREATE tests/test_data_sources.py

- **WAVE**: 3
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Unit tests for `DataSource` protocol, `YFinanceSource`, `IBGatewaySource`

**IMPLEMENT** `tests/test_data_sources.py`:

```python
"""tests/test_data_sources.py — Unit tests for data/sources.py."""
import datetime
import unittest.mock as mock
import pytest
import pandas as pd
import numpy as np
from data.sources import YFinanceSource, IBGatewaySource, DataSource


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_tz_aware_ohlcv(n_bars: int = 10) -> pd.DataFrame:
    """Synthetic hourly DataFrame matching yfinance output format."""
    base = pd.Timestamp("2025-01-02 14:30:00", tz="UTC")  # 9:30 ET
    idx = pd.date_range(start=base, periods=n_bars, freq="1h")
    return pd.DataFrame({
        "Open":   np.linspace(100, 110, n_bars),
        "High":   np.linspace(101, 111, n_bars),
        "Low":    np.linspace(99, 109, n_bars),
        "Close":  np.linspace(100.5, 110.5, n_bars),
        "Volume": [1_000_000.0] * n_bars,
    }, index=idx)
```

Tests to implement (one function each):

**YFinanceSource tests (5):**
1. `test_yfinance_source_fetch_calls_yfinance_ticker` — mock `yf.Ticker`; verify `history()` called with correct `start`, `end`, `interval`
2. `test_yfinance_source_fetch_returns_dataframe_with_ohlcv_columns` — mock `yf.Ticker` returning `_make_tz_aware_ohlcv()`; verify columns are exactly `{"Open", "High", "Low", "Close", "Volume"}`
3. `test_yfinance_source_fetch_returns_none_on_empty` — mock `yf.Ticker` returning empty DataFrame; verify `fetch()` returns `None`
4. `test_yfinance_source_fetch_returns_tz_aware_index` — verify returned DataFrame index has timezone set
5. `test_yfinance_source_conforms_to_protocol` — `assert isinstance(YFinanceSource(), DataSource)` (structural check)

**IBGatewaySource tests (6):**
6. `test_ibgateway_source_interval_mapping_1h` — verify `_IB_BAR_SIZE["1h"] == "1 hour"`
7. `test_ibgateway_source_interval_mapping_1m` — verify `_IB_BAR_SIZE["1m"] == "1 min"`
8. `test_ibgateway_source_fetch_returns_none_on_unsupported_interval` — call `fetch("AAPL", ..., interval="invalid")`; expect `None`
9. `test_ibgateway_source_fetch_calls_reqHistoricalData` — mock `ib_insync.IB`; verify `reqHistoricalData` called with correct `barSizeSetting="1 hour"` for interval `"1h"`
10. `test_ibgateway_source_fetch_returns_ohlcv_dataframe` — mock `ib_insync.IB` and `util.df` returning synthetic bars; verify result columns and non-empty
11. `test_ibgateway_source_fetch_returns_none_on_exception` — mock `ib_insync.IB.connect` raising `ConnectionRefusedError`; verify `fetch()` returns `None` (no exception propagated)

**Integration test (1, live IB required):**
12. `test_ibgateway_source_fetch_live` — `@pytest.mark.integration`; real IB-Gateway connection; verify AAPL 1h bars return non-empty DataFrame with expected schema

- **VALIDATE**: `uv run pytest tests/test_data_sources.py -v -m "not integration"`

**Wave 3 Checkpoint**: `uv run pytest tests/test_data_sources.py -v -m "not integration"` — all tests green

---

### WAVE 4: train.py update + test_prepare.py fixes

#### Task 4.1: UPDATE train.py — manifest loader + interval-aware cache loaders

- **WAVE**: 4
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: [5.1]
- **PROVIDES**: `BACKTEST_START`/`BACKTEST_END` loaded from manifest; loaders read from `{CACHE_DIR}/{interval}/`

**IMPLEMENT** the following changes to `train.py`:

1. **Add imports** (top of file, after existing imports):
```python
import json
from pathlib import Path
```

2. **Add manifest loader** (after `CACHE_DIR` assignment, before `# ══ SESSION SETUP` block):
```python
def _load_manifest() -> dict:
    """Load run config written by prepare.py. Raises FileNotFoundError if missing."""
    path = Path(CACHE_DIR) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No manifest.json found at {path}. Run prepare.py first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)

_manifest = _load_manifest()
_FETCH_INTERVAL: str = _manifest.get("fetch_interval", "1h")
```

3. **Replace hardcoded constants** in the `# ══ SESSION SETUP` section:

Replace:
```python
BACKTEST_START = "2024-09-01"
BACKTEST_END   = "2026-03-20"
```
With:
```python
# Loaded from manifest.json written by prepare.py — do not edit here.
BACKTEST_START: str = _manifest["backtest_start"]
BACKTEST_END:   str = _manifest["backtest_end"]
```

Keep `TRAIN_END`, `TEST_START`, `SILENT_END`, and all other SESSION SETUP constants unchanged.

4. **Update `load_ticker_data()`**:
```python
def load_ticker_data(ticker: str) -> pd.DataFrame | None:
    """Reads CACHE_DIR/{interval}/{ticker}.parquet; returns None if file does not exist."""
    path = os.path.join(CACHE_DIR, _FETCH_INTERVAL, f"{ticker}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)
```

5. **Update `load_all_ticker_data()`**:
```python
def load_all_ticker_data() -> dict[str, pd.DataFrame]:
    """Loads all *.parquet from CACHE_DIR/{interval}/. Returns {} if missing."""
    interval_dir = os.path.join(CACHE_DIR, _FETCH_INTERVAL)
    if not os.path.isdir(interval_dir):
        return {}
    result = {}
    for fname in os.listdir(interval_dir):
        if fname.endswith(".parquet"):
            ticker = fname[:-len(".parquet")]
            result[ticker] = pd.read_parquet(os.path.join(interval_dir, fname))
    return result
```

- **VALIDATE**: `python -c "import train; print('BACKTEST_START:', train.BACKTEST_START)"` (requires manifest.json to exist in CACHE_DIR)

#### Task 4.2: UPDATE tests/test_prepare.py — fix cache paths, add manifest tests

- **WAVE**: 4
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: [5.1]

**IMPLEMENT** the following changes to `tests/test_prepare.py`:

1. **Update import** — add `write_manifest` and `PREPARE_INTERVAL` to the import line:
```python
from prepare import (
    resample_to_daily, validate_ticker_data, process_ticker,
    write_trend_summary, write_manifest,
    CACHE_DIR, BACKTEST_START, PREPARE_INTERVAL,
)
```

2. **Fix `test_process_ticker_skips_existing_file`** — update the pre-existing file path to use interval subdirectory:
```python
def test_process_ticker_skips_existing_file(tmp_path):
    interval_dir = tmp_path / PREPARE_INTERVAL
    interval_dir.mkdir()
    path = interval_dir / "SKIP.parquet"
    path.write_bytes(b"")
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.download_ticker") as mock_dl:
        result = process_ticker("SKIP", YFinanceSource(), PREPARE_INTERVAL)
    mock_dl.assert_not_called()
    assert result is True
```
Note: `process_ticker` no longer uses the old `download_ticker` function directly — adjust mock to patch `YFinanceSource.fetch` instead.

3. **Fix `test_process_ticker_skips_empty_download`** — update to use new `process_ticker(ticker, source, interval)` signature with a mocked source.

4. **Fix `test_process_ticker_saves_parquet`** — verify file is written at `{tmp_path}/{PREPARE_INTERVAL}/TEST.parquet` (not `{tmp_path}/TEST.parquet`).

5. **Fix `_make_parquet` helper** (used by `write_trend_summary` tests) — update path to write into `{tmp_path}/{interval}/` subdirectory; update all callers of `_make_parquet` to pass interval.

6. **Fix `write_trend_summary` tests** — update calls to pass `interval` parameter: `write_trend_summary([...], start, end, cache_dir, interval)`.

7. **Add manifest tests (3 new)**:

```python
def test_write_manifest_creates_file(tmp_path):
    write_manifest(["AAPL", "MSFT"], "2024-09-01", "2026-03-20", "1h", "yfinance", str(tmp_path))
    assert (tmp_path / "manifest.json").exists()


def test_write_manifest_content(tmp_path):
    write_manifest(["AAPL"], "2024-09-01", "2026-03-20", "1h", "yfinance", str(tmp_path))
    import json
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert data["backtest_start"] == "2024-09-01"
    assert data["backtest_end"] == "2026-03-20"
    assert data["fetch_interval"] == "1h"
    assert data["source"] == "yfinance"
    assert "AAPL" in data["tickers"]


def test_write_manifest_interval_field(tmp_path):
    """fetch_interval in manifest reflects PREPARE_INTERVAL passed in."""
    write_manifest(["AAPL"], "2024-09-01", "2026-03-20", "1m", "ib", str(tmp_path))
    import json
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert data["fetch_interval"] == "1m"
    assert data["source"] == "ib"
```

8. **Update integration test** `test_download_ticker_returns_expected_schema` — update to use `YFinanceSource().fetch("AAPL", ...)` instead of the removed `download_ticker()` function.

- **VALIDATE**: `uv run pytest tests/test_prepare.py -v -m "not integration"`

**Wave 4 Checkpoint**: `uv run pytest tests/test_prepare.py tests/test_data_sources.py -v -m "not integration"`

---

### WAVE 5: Full validation

#### Task 5.1: RUN full test suite and verify all pass

- **WAVE**: 5
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [4.1, 4.2]
- **PROVIDES**: Confirmed green test suite

**IMPLEMENT** (validation only — no code changes):

1. Run `uv run pytest -m "not integration" -v 2>&1 | tail -20` — verify 0 failures, 0 errors
2. Confirm `data/sources.py` and `data/__init__.py` exist
3. Confirm `{CACHE_DIR}/manifest.json` schema is correct (manual check or write a quick script)
4. Confirm `tests/test_data_sources.py` and updated `tests/test_prepare.py` exist and pass

**Final Checkpoint**: `uv run pytest -m "not integration" -v` — full suite green with new tests included

---

## TESTING STRATEGY

| What you're testing | Tool |
|---|---|
| DataSource protocol conformance | pytest |
| YFinanceSource fetch logic | pytest (mock yf.Ticker) |
| IBGatewaySource fetch logic | pytest (mock ib_insync.IB) |
| IBGatewaySource interval mapping | pytest (direct dict access) |
| prepare.py cache path layout | pytest (tmp_path) |
| prepare.py manifest writing | pytest (tmp_path) |
| prepare.py process_ticker with source | pytest (mock source) |
| train.py manifest loading | pytest (tmp_path + patch CACHE_DIR) |
| train.py loader interval subdir | pytest (tmp_path + patch CACHE_DIR) |
| Full IB-Gateway live connection | pytest integration mark |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_data_sources.py`, `tests/test_prepare.py` | **Run**: `uv run pytest tests/test_data_sources.py tests/test_prepare.py -v -m "not integration"`

### Integration Tests

**Status**: ✅ Automated (marked `@pytest.mark.integration`) | **Tool**: pytest | **Location**: `tests/test_data_sources.py::test_ibgateway_source_fetch_live`, `tests/test_prepare.py::test_download_ticker_returns_expected_schema` | **Run**: `uv run pytest -m integration -v`

### Edge Cases

- **No manifest.json**: train.py raises `FileNotFoundError` with clear message — ✅ `uv run pytest tests/test_train_manifest.py` (add inline test)
- **Empty IBGatewaySource.fetch() result**: `process_ticker` returns `False` without crashing — ✅ `test_process_ticker_skips_empty_download` updated
- **Unsupported interval in IBGatewaySource**: returns `None` — ✅ `test_ibgateway_source_fetch_returns_none_on_unsupported_interval`
- **IB connection refused**: returns `None`, no exception propagated — ✅ `test_ibgateway_source_fetch_returns_none_on_exception`
- **Duplicate bars from pagination**: deduplicated by `df.index.duplicated(keep="last")` — ✅ covered in `test_ibgateway_source_fetch_returns_ohlcv_dataframe` (mock multiple paginated windows)

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest, mock) | 16 | 89% |
| ✅ Integration (live IB/Yahoo) | 2 | 11% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 18 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import checks

```bash
python -c "from data.sources import DataSource, YFinanceSource, IBGatewaySource; print('imports ok')"
python -c "import prepare; print('prepare ok')"
# Requires manifest.json in CACHE_DIR — create a minimal one first for CI:
# echo '{"tickers":[],"backtest_start":"2024-09-01","backtest_end":"2026-03-20","fetch_interval":"1h","source":"yfinance"}' > ~/.cache/autoresearch/stock_data/manifest.json
python -c "import train; print('BACKTEST_START:', train.BACKTEST_START)"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_data_sources.py -v -m "not integration"
uv run pytest tests/test_prepare.py -v -m "not integration"
```

### Level 3: Full Suite (non-integration)

```bash
uv run pytest -m "not integration" -v
```

### Level 4: Integration / Manual

```bash
# With IB-Gateway running on 127.0.0.1:4002:
uv run pytest -m integration -v

# Smoke test prepare.py with yfinance for 1 ticker:
PREPARE_WORKERS=1 python -c "
import prepare, os
prepare.TICKERS = ['AAPL']
import importlib; importlib.reload(prepare)
"
# (or edit TICKERS to ['AAPL'] and run: uv run prepare.py)

# Verify new cache layout:
ls ~/.cache/autoresearch/stock_data/1h/ | head -5
cat ~/.cache/autoresearch/stock_data/manifest.json
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `data/__init__.py` exists as an empty package marker
- [ ] `data/sources.py` defines `DataSource` as a structural protocol with signature `fetch(ticker, start, end, interval) -> pd.DataFrame | None`
- [ ] `YFinanceSource.fetch()` returns a DataFrame with columns `{Open, High, Low, Close, Volume}` and a tz-aware DatetimeIndex when yfinance returns data
- [ ] `YFinanceSource.fetch()` returns `None` (not raises) when yfinance returns an empty DataFrame
- [ ] `IBGatewaySource.fetch()` maps `"1h"` → `barSizeSetting="1 hour"` and `"1m"` → `barSizeSetting="1 min"` when calling `reqHistoricalData`
- [ ] `IBGatewaySource.fetch()` paginates 1m requests in ≤29-day chunks and 1h requests in ≤180-day chunks
- [ ] `prepare.py` writes cached daily parquets to `{CACHE_DIR}/{PREPARE_INTERVAL}/{TICKER}.parquet` (not flat root)
- [ ] `prepare.py` selects `YFinanceSource` when `PREPARE_SOURCE=yfinance`, `IBGatewaySource` when `PREPARE_SOURCE=ib`
- [ ] `prepare.py` writes `{CACHE_DIR}/manifest.json` containing `tickers`, `backtest_start`, `backtest_end`, `fetch_interval`, `source`
- [ ] `write_trend_summary()` accepts an `interval` parameter and reads from `{CACHE_DIR}/{interval}/{TICKER}.parquet`
- [ ] `train.py` assigns `BACKTEST_START` and `BACKTEST_END` from `manifest.json` values, not from hardcoded strings
- [ ] `train.py`'s `load_all_ticker_data()` reads from `{CACHE_DIR}/{_FETCH_INTERVAL}/` subdirectory
- [ ] No remaining code reads from the old flat `{CACHE_DIR}/{TICKER}.parquet` path

### Error Handling
- [ ] `IBGatewaySource.fetch()` returns `None` without propagating an exception when IB-Gateway is not reachable (`ConnectionRefusedError`)
- [ ] `IBGatewaySource.fetch()` returns `None` for an unsupported interval string (e.g. `"invalid"`)
- [ ] `process_ticker()` returns `False` when the source returns `None`, without raising
- [ ] `train._load_manifest()` raises `FileNotFoundError` with a message mentioning `manifest.json` when the file is absent

### Integration / E2E
- [ ] After running `uv run prepare.py`, the file `{CACHE_DIR}/1h/AAPL.parquet` exists and contains daily bars with columns `open, high, low, close, volume, price_1030am`
- [ ] After running `uv run prepare.py`, `{CACHE_DIR}/manifest.json` is valid JSON with all required keys
- [ ] `uv run train.py` completes without error after prepare.py has run and produces fold output lines parseable by existing grep patterns

### Validation
- [ ] `uv run pytest tests/test_data_sources.py -v -m "not integration"` — all 12 tests pass
- [ ] `uv run pytest tests/test_prepare.py -v -m "not integration"` — all tests pass including 3 new manifest tests
- [ ] `uv run pytest -m "not integration" -v` — full suite passes with no regressions vs. pre-change baseline

### Out of Scope
- `screener.py` and `screener_prepare.py` — not updated in this plan
- `TRAIN_END`, `TEST_START`, `SILENT_END` in train.py — remain hardcoded (experiment-specific, agent-set)
- Earnings dates — always use yfinance regardless of `PREPARE_SOURCE`
- Cache migration code — wipe old flat files and re-run prepare.py is the migration path

---

## COMPLETION CHECKLIST

- [ ] Wave 1 checkpoint passes
- [ ] Wave 2 checkpoint passes
- [ ] Wave 3 checkpoint passes
- [ ] Wave 4 checkpoint passes
- [ ] Wave 5 (full suite) passes
- [ ] `data/sources.py` implements both sources per spec
- [ ] `prepare.py` uses DataSource protocol, writes interval subdir, writes manifest
- [ ] `train.py` reads from manifest; loaders use interval subdir
- [ ] `tests/test_data_sources.py` created with 12 tests
- [ ] `tests/test_prepare.py` updated (cache path fixes + 3 manifest tests)
- [ ] No debug logs added during execution remain
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Earnings dates**: `_add_earnings_dates()` in prepare.py always uses `yf.Ticker()` regardless of `PREPARE_SOURCE`. IB-Gateway has no earnings calendar endpoint. This is intentional and unchanged.

**`download_ticker()` removal**: The function is fully replaced by `YFinanceSource.fetch()`. The public API of prepare.py shrinks by one function. Any external callers (e.g. screener_prepare.py) should be checked — but per scope decision, screener files are NOT updated in this plan.

**train.py `TRAIN_END`, `TEST_START`, `SILENT_END` stay hardcoded**: These are experiment-specific constants set by the agent loop. Only `BACKTEST_START` and `BACKTEST_END` move to the manifest.

**IB durationStr calculation**: We compute `(chunk_end - chunk_start).days` and pass as `"{n} D"`. IB accepts duration as calendar days (D), weeks (W), months (M), or years (Y). Using days for all intervals keeps the logic uniform and avoids edge cases.

**Cache migration**: Delete `~/.cache/autoresearch/stock_data/*.parquet` (flat files) and re-run `prepare.py`. No migration code needed.
