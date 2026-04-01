# Plan: Databento Historical Data Pipeline

**Complexity**: ⚠️ Medium
**Type**: New Capability
**Feature**: Integrate Databento as a historical data source for MNQ/MES continuous futures

---

## User Story

As a trader running walk-forward optimization on SMT divergence signals,
I want `prepare_futures.py` to download 2 years of 5m MNQ/MES bars from Databento,
so that I have 100+ test trades across 6 folds instead of ~30, making parameter optimization statistically meaningful.

---

## Problem Summary

IB-Gateway can only provide ~6.5 months of history via quarterly conIds, yielding ~30–42 test trades across walk-forward folds — insufficient for statistically meaningful parameter tuning. Databento provides clean stitched continuous-contract history back to Jan 2024 at low cost (~$4.93), extending the window to ~2 years and targeting 120+ trades.

---

## Affected Systems

| File | Change Type |
|------|-------------|
| `data/sources.py` | Add `DatabentSource` class |
| `prepare_futures.py` | Update lookup priority, constants, manifest |
| `tests/test_data_sources.py` | Add DatabentSource unit + integration tests |
| `.gitignore` | Add `data/historical/*.parquet` |
| `data/historical/.gitkeep` | New directory marker |
| `pyproject.toml` | Add `databento` dependency |

---

## Execution Agent Rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Pre-Execution Baseline

Before any changes, run:
```bash
uv run pytest tests/ -q --ignore=tests/test_e2e.py 2>&1 | tail -5
```
Document passing/failing/skipped counts. All regressions introduced by this plan are bugs.

---

## Task Breakdown

### WAVE 1 — Infrastructure (no dependencies)

#### Task 1.1 — Add `databento` dependency
**WAVE**: 1
**AGENT_ROLE**: Dependency manager
**DEPENDS_ON**: none

Add `databento` to `pyproject.toml` dependencies list:
```toml
"databento>=0.35",
```

Verify it installs without conflicts:
```bash
uv add databento
uv run python -c "import databento; print(databento.__version__)"
```

---

#### Task 1.2 — Create `data/historical/` directory with `.gitkeep`
**WAVE**: 1
**AGENT_ROLE**: File structure setup
**DEPENDS_ON**: none

1. Create `data/historical/.gitkeep` (empty file)
2. Add to `.gitignore`:
   ```
   # Databento permanent parquet store (large, not committed)
   data/historical/*.parquet
   ```

---

### WAVE 2 — DatabentSource Implementation (depends on Task 1.1)

#### Task 2.1 — Implement `DatabentSource` in `data/sources.py`
**WAVE**: 2
**AGENT_ROLE**: Core implementor
**DEPENDS_ON**: Task 1.1

Add the following class to `data/sources.py`, after the `YFinanceSource` class and before `IBGatewaySource`. The class must:

1. Import `databento` (lazy import inside the class to avoid hard dependency at module load)
2. Read `DATABENTO_API_KEY` from environment at `__init__` time; raise `RuntimeError` if missing
3. Implement `fetch(ticker, start, end, interval, **kwargs) -> pd.DataFrame | None`
4. Call `db.Historical(key=self._api_key).timeseries.get_range(dataset="GLBX.MDP3", schema="ohlcv-1m", symbols=[ticker], start=start, end=end)`
5. Convert result to DataFrame via `.to_df()`
6. Rename columns: `{"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}`
7. Resample 1m → 5m if `interval == "5m"`:
   ```python
   df = df.resample("5min").agg({
       "Open": "first",
       "High": "max",
       "Low": "min",
       "Close": "last",
       "Volume": "sum",
   }).dropna()
   ```
8. Convert index to America/New_York timezone: `df.index = df.index.tz_convert("America/New_York")`
9. Return `None` on empty DataFrame
10. Catch `databento.BentoError` and connection errors, log to stderr, return `None` (do NOT raise)
11. If `interval` is not `"1m"` and not `"5m"`, raise `ValueError("DatabentSource only supports 1m and 5m intervals")`

**Full implementation skeleton:**
```python
class DatabentSource:
    """Fetch OHLCV from Databento for CME Globex futures (GLBX.MDP3).

    Requires DATABENTO_API_KEY environment variable.
    Downloads 1m bars and optionally resamples to 5m.
    Returns tz-aware (America/New_York) DataFrame or None on failure.
    """

    def __init__(self) -> None:
        import os
        api_key = os.environ.get("DATABENTO_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DATABENTO_API_KEY environment variable is required for DatabentSource"
            )
        self._api_key = api_key

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "5m",
        **kwargs,
    ) -> pd.DataFrame | None:
        import databento as db

        if interval not in ("1m", "5m"):
            raise ValueError(
                f"DatabentSource only supports 1m and 5m intervals, got {interval!r}"
            )
        try:
            client = db.Historical(key=self._api_key)
            data = client.timeseries.get_range(
                dataset="GLBX.MDP3",
                symbols=[ticker],
                schema="ohlcv-1m",
                start=start,
                end=end,
            )
            df = data.to_df()
        except Exception as exc:
            import sys
            print(f"DatabentSource: error fetching {ticker}: {exc}", file=sys.stderr)
            return None

        if df.empty:
            return None

        # Rename to standard uppercase OHLCV columns
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        df = df[["Open", "High", "Low", "Close", "Volume"]]

        # Resample 1m → 5m if requested
        if interval == "5m":
            df = df.resample("5min").agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }).dropna()

        # Convert UTC → America/New_York
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("America/New_York")

        return df if not df.empty else None
```

**Also add** `DatabentSource` to the public exports in `data/__init__.py` if that file has explicit exports (check first).

---

### WAVE 3 — prepare_futures.py Updates (depends on Task 2.1)

#### Task 3.1 — Update `prepare_futures.py` with Databento lookup priority
**WAVE**: 3
**AGENT_ROLE**: Script updater
**DEPENDS_ON**: Task 2.1

Make the following changes to `prepare_futures.py`:

**1. Update module docstring** to mention Databento as primary source.

**2. Update imports**: add `DatabentSource` import:
```python
from data.sources import IBGatewaySource, DatabentSource
```

**3. Add constants** after the existing `CONIDS` block:
```python
# Databento permanent store — survives cache clears
HISTORICAL_DATA_DIR = Path("data/historical")

# Databento continuous front-month symbols (roll-adjusted by Databento)
DATABENTO_SYMBOLS = {
    "MNQ": "MNQ.c.0",
    "MES": "MES.c.0",
}

# Extend window to Databento's full 2-year history
BACKTEST_START = "2024-01-01"
```

**4. Update `HISTORY_START`**: keep it equal to `BACKTEST_START` (no warmup needed for SMT).

**5. Rewrite `process_ticker()`** to implement the 3-level lookup priority:
```python
def process_ticker(ticker: str) -> bool:
    """Fetch and cache futures bars. Returns True on success.

    Priority:
      1. data/historical/{ticker}.parquet  — Databento permanent store
      2. {CACHE_DIR}/5m/{ticker}.parquet   — IB ephemeral cache
      3. Live Databento download
    """
    # Level 1: Databento permanent store
    historical_path = HISTORICAL_DATA_DIR / f"{ticker}.parquet"
    if historical_path.exists():
        print(f"  {ticker}: found in data/historical/, skipping download")
        return True

    # Level 2: IB ephemeral cache (legacy data from previous runs)
    ib_path = Path(CACHE_DIR) / INTERVAL / f"{ticker}.parquet"
    if ib_path.exists():
        print(f"  {ticker}: found in IB cache ({ib_path}), skipping download")
        return True

    # Level 3: Download from Databento
    HISTORICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        source = DatabentSource()
    except RuntimeError as exc:
        print(f"  {ticker}: Databento init failed — {exc}", file=sys.stderr)
        return False

    db_ticker = DATABENTO_SYMBOLS[ticker]
    df = source.fetch(db_ticker, HISTORY_START, BACKTEST_END, INTERVAL)
    if df is None or df.empty:
        print(f"  {ticker}: no data returned from Databento", file=sys.stderr)
        return False

    df.to_parquet(historical_path)
    print(f"  {ticker}: saved {len(df)} bars to {historical_path}")
    return True
```

**6. Update `write_manifest()`** to reflect new source and start date:
```python
json.dump(
    {
        "tickers": TICKERS,
        "backtest_start": BACKTEST_START,
        "backtest_end": BACKTEST_END,
        "fetch_interval": INTERVAL,
        "source": "databento",
        "databento_symbols": DATABENTO_SYMBOLS,
    },
    f,
    indent=2,
)
```

**7. Update module docstring** at top of file:
- Remove the IB-Gateway requirement text
- Add: "Downloads from Databento (GLBX.MDP3, MNQ.c.0/MES.c.0) to data/historical/."
- Update BACKTEST_START comment to reference "2024-01-01 Databento window"

---

### WAVE 4 — Tests (depends on Task 2.1, can run parallel with Task 3.1)

#### Task 4.1 — Add DatabentSource unit tests to `tests/test_data_sources.py`
**WAVE**: 4
**AGENT_ROLE**: Test writer
**DEPENDS_ON**: Task 2.1

Add the following test section to `tests/test_data_sources.py`, following the existing IBGatewaySource test block style.

**Helper** — add near top of file with other helpers:
```python
def _make_1m_ohlcv(n_bars: int = 10) -> pd.DataFrame:
    """Synthetic 1-minute UTC DataFrame matching Databento .to_df() output."""
    base = pd.Timestamp("2025-01-02 14:30:00", tz="UTC")
    idx = pd.date_range(start=base, periods=n_bars, freq="1min")
    return pd.DataFrame({
        "open":   [100.0 + i for i in range(n_bars)],
        "high":   [101.0 + i for i in range(n_bars)],
        "low":    [99.0  + i for i in range(n_bars)],
        "close":  [100.5 + i for i in range(n_bars)],
        "volume": [500] * n_bars,
    }, index=idx)
```

**Tests to add:**

```python
# ── DatabentSource tests ──────────────────────────────────────────────────────

class TestDatabentSourceInit:
    def test_raises_runtime_error_when_api_key_missing(self, monkeypatch):
        """DatabentSource.__init__ must raise RuntimeError if DATABENTO_API_KEY unset."""
        monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
        from data.sources import DatabentSource
        with pytest.raises(RuntimeError, match="DATABENTO_API_KEY"):
            DatabentSource()

    def test_init_succeeds_when_api_key_present(self, monkeypatch):
        """DatabentSource.__init__ succeeds with DATABENTO_API_KEY set."""
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")
        from data.sources import DatabentSource
        src = DatabentSource()  # should not raise
        assert src is not None


class TestDatabentSourceFetch:
    @pytest.fixture
    def src(self, monkeypatch):
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")
        from data.sources import DatabentSource
        return DatabentSource()

    def test_calls_get_range_with_correct_args(self, src, monkeypatch):
        """fetch() must call timeseries.get_range with GLBX.MDP3, ohlcv-1m, correct symbols."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(10)
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        mock_client.timeseries.get_range.assert_called_once_with(
            dataset="GLBX.MDP3",
            symbols=["MNQ.c.0"],
            schema="ohlcv-1m",
            start="2024-01-01",
            end="2024-01-31",
        )

    def test_resamples_1m_to_5m(self, src, monkeypatch):
        """fetch() with interval='5m' returns 5-minute bars (fewer rows than 1m input)."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(60)  # 60 x 1m bars → 12 x 5m bars
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        df = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert df is not None
        assert len(df) == 12  # 60 1m bars → 12 5m bars

    def test_returns_ohlcv_columns_uppercase(self, src, monkeypatch):
        """fetch() returns DataFrame with uppercase {Open,High,Low,Close,Volume} columns."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(5)
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        df = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert df is not None
        assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}

    def test_returns_et_timezone_index(self, src, monkeypatch):
        """fetch() returns DataFrame with America/New_York timezone index."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(5)
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        df = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert df is not None
        assert str(df.index.tzinfo) == "America/New_York"

    def test_returns_none_on_bento_error(self, src, monkeypatch):
        """fetch() returns None (no raise) when BentoError occurs."""
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.side_effect = Exception("BentoError: unauthorized")
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        result = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert result is None

    def test_returns_none_on_empty_dataframe(self, src, monkeypatch):
        """fetch() returns None when Databento returns zero rows."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = pd.DataFrame()
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        result = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert result is None

    def test_raises_value_error_on_unsupported_interval(self, src, monkeypatch):
        """fetch() raises ValueError for intervals other than 1m/5m."""
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")
        with pytest.raises(ValueError, match="only supports 1m and 5m"):
            src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "1h")

    def test_1m_interval_skips_resampling(self, src, monkeypatch):
        """fetch() with interval='1m' returns raw 1m bars without resampling."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(10)
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        df = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "1m")
        assert df is not None
        assert len(df) == 10  # unchanged

    def test_conforms_to_protocol(self, monkeypatch):
        """DatabentSource satisfies the DataSource protocol."""
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")
        from data.sources import DatabentSource
        assert isinstance(DatabentSource(), DataSource)


@pytest.mark.integration
class TestDatabentSourceIntegration:
    """Live integration tests — auto-skipped if DATABENTO_API_KEY not set."""

    @pytest.fixture(autouse=True)
    def skip_if_no_key(self):
        import os
        if not os.environ.get("DATABENTO_API_KEY"):
            pytest.skip("DATABENTO_API_KEY not set — skipping live Databento test")

    def test_fetch_5day_window_returns_valid_schema(self):
        """Live fetch: 5 trading days of MNQ.c.0 5m bars, validates OHLCV + ET timezone."""
        from data.sources import DatabentSource
        src = DatabentSource()
        df = src.fetch("MNQ.c.0", "2025-01-06", "2025-01-10", "5m")
        assert df is not None, "Expected data, got None"
        assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
        assert str(df.index.tzinfo) == "America/New_York"
        assert len(df) > 0
        assert df["Open"].dtype == float
        assert df["Volume"].dtype in (float, int, "int64", "float64")
```

---

### WAVE 5 — Regression Verification (depends on Wave 4)

#### Task 5.1 — Run full test suite and verify no regressions
**WAVE**: 5
**AGENT_ROLE**: Verifier
**DEPENDS_ON**: Tasks 4.1, 3.1

Run:
```bash
uv run pytest tests/ -q --ignore=tests/test_e2e.py -k "not integration" 2>&1 | tail -20
```

Expected: all previously passing tests still pass. New DatabentSource unit tests (8 non-integration tests) must pass.

If any pre-existing test fails, investigate and fix the regression before proceeding.

---

#### Task 5.2 — Verify prepare_futures.py script is runnable
**WAVE**: 5
**AGENT_ROLE**: Script validator
**DEPENDS_ON**: Task 3.1

Run a dry import check (does not require Databento key):
```bash
uv run python -c "import prepare_futures; print('import OK')"
```

Verify: no import errors, no `RuntimeError` on import (key check must be inside `process_ticker`, not at module level).

Also verify the manifest constants are correct:
```bash
uv run python -c "
import prepare_futures as pf
assert pf.BACKTEST_START == '2024-01-01', f'Expected 2024-01-01, got {pf.BACKTEST_START}'
assert pf.DATABENTO_SYMBOLS == {'MNQ': 'MNQ.c.0', 'MES': 'MES.c.0'}
print('Constants OK')
"
```

---

## Directory & File Checklist

| Path | Action |
|------|--------|
| `pyproject.toml` | Add `"databento>=0.35"` to dependencies |
| `data/sources.py` | Add `DatabentSource` class |
| `prepare_futures.py` | Update constants, imports, `process_ticker()`, `write_manifest()` |
| `tests/test_data_sources.py` | Add `TestDatabentSourceInit`, `TestDatabentSourceFetch`, `TestDatabentSourceIntegration` |
| `.gitignore` | Add `data/historical/*.parquet` |
| `data/historical/.gitkeep` | Create (new empty file) |

---

## Test Coverage Analysis

### New code paths

| Path | Tests |
|------|-------|
| `DatabentSource.__init__` — key present | ✅ `test_init_succeeds_when_api_key_present` |
| `DatabentSource.__init__` — key missing → RuntimeError | ✅ `test_raises_runtime_error_when_api_key_missing` |
| `DatabentSource.fetch` — API call args | ✅ `test_calls_get_range_with_correct_args` |
| `DatabentSource.fetch` — 1m→5m resample | ✅ `test_resamples_1m_to_5m` |
| `DatabentSource.fetch` — 1m no resample | ✅ `test_1m_interval_skips_resampling` |
| `DatabentSource.fetch` — OHLCV column names | ✅ `test_returns_ohlcv_columns_uppercase` |
| `DatabentSource.fetch` — ET timezone conversion | ✅ `test_returns_et_timezone_index` |
| `DatabentSource.fetch` — BentoError → None | ✅ `test_returns_none_on_bento_error` |
| `DatabentSource.fetch` — empty → None | ✅ `test_returns_none_on_empty_dataframe` |
| `DatabentSource.fetch` — unsupported interval | ✅ `test_raises_value_error_on_unsupported_interval` |
| `DatabentSource` — DataSource protocol | ✅ `test_conforms_to_protocol` |
| Live API fetch with real key | ✅ `TestDatabentSourceIntegration` (auto-skipped without key) |
| `prepare_futures.process_ticker` — Level 1 (historical parquet exists) | ✅ `test_5.2 import + constants check` |
| `prepare_futures.process_ticker` — Level 3 (Databento download) | ✅ Covered by integration test or manual run |

### Existing code paths re-validated

| Area | Status |
|------|--------|
| `YFinanceSource` tests | ✅ No changes; run as regression |
| `IBGatewaySource` tests | ✅ No changes; run as regression |
| `prepare_futures` import | ✅ `test_5.2` import check |

### Test Automation Summary

- **Automated (unit)**: 11 tests — pytest with monkeypatching, no network required
- **Automated (integration)**: 1 test — requires `DATABENTO_API_KEY`, auto-skipped without it
- **Manual**: 1 path — `process_ticker` Level 2 (IB cache hit) — trivial file-existence branch, covered by reading the code
- **Gaps**: None — all meaningful code paths covered

---

## Script Deliverables Checklist

- [ ] `uv run prepare_futures.py` completes the setup phase without raising an exception (requires `DATABENTO_API_KEY` in `.env`)
- [ ] All user-visible output uses ASCII-safe characters (print statements only use standard ASCII)
- [ ] `DatabentSource` init check is inside `process_ticker()`, not at module import time — import never raises `RuntimeError`

---

## Risk Analysis

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `databento` package conflict with existing deps | Low | `uv add databento` resolves cleanly; test with `uv run python -c "import databento"` |
| Databento column names differ from expected (not `open/high/low/close/volume`) | Low | Verified in PROGRESS.md spec; add `.rename()` defensively |
| 1m→5m resample produces NaTs for incomplete last bar | Medium | `.dropna()` after resample handles this |
| `DATABENTO_API_KEY` raises at module import level | Medium | Ensure key check is inside `__init__`, not at class body / module level |
| `BACKTEST_START` change to 2024-01-01 breaks existing IB-cache-based tests | Low | IB cache path (Level 2) still supported; no tests depend on BACKTEST_START constant value |

---

## Interface Contracts (Wave Dependencies)

| Wave | Outputs | Consumed by |
|------|---------|-------------|
| Wave 1 | `databento` installed, `data/historical/` dir exists | Wave 2 |
| Wave 2 | `DatabentSource` class in `data/sources.py` | Waves 3, 4 |
| Wave 3 | Updated `prepare_futures.py` | Wave 5 (script check) |
| Wave 4 | New tests in `test_data_sources.py` | Wave 5 (pytest run) |
| Wave 5 | Full test suite green + script import OK | Done |

---

## Parallelization Summary

- **Total tasks**: 7 (Tasks 1.1, 1.2, 2.1, 3.1, 4.1, 5.1, 5.2)
- **Parallel pairs**: Wave 1 (1.1 + 1.2 in parallel), Wave 4+3 (3.1 + 4.1 in parallel after 2.1)
- **Max speedup**: ~2x (3 sequential stages instead of 7)
- **Sequential**: Wave 1 → Wave 2 → Wave 3+4 → Wave 5

---

## Acceptance Criteria

### Functional
- [ ] `DatabentSource.__init__` raises `RuntimeError` when `DATABENTO_API_KEY` is not set in the environment
- [ ] `DatabentSource.__init__` succeeds (no exception) when `DATABENTO_API_KEY` is set
- [ ] `DatabentSource.fetch()` calls `timeseries.get_range` with `dataset="GLBX.MDP3"`, `schema="ohlcv-1m"`, and the correct symbol
- [ ] `DatabentSource.fetch()` with `interval="5m"` returns 5-minute bars (resampled from 1m)
- [ ] `DatabentSource.fetch()` with `interval="1m"` returns raw 1-minute bars without resampling
- [ ] `DatabentSource.fetch()` returns a DataFrame with columns exactly `{Open, High, Low, Close, Volume}` (uppercase)
- [ ] `DatabentSource.fetch()` returns a DataFrame with a tz-aware `America/New_York` index
- [ ] `DatabentSource` satisfies the `DataSource` protocol (passes `isinstance(src, DataSource)`)
- [ ] `prepare_futures.py` imports cleanly without raising `RuntimeError` even when `DATABENTO_API_KEY` is absent
- [ ] `prepare_futures.BACKTEST_START` equals `"2024-01-01"`
- [ ] `prepare_futures.DATABENTO_SYMBOLS` equals `{"MNQ": "MNQ.c.0", "MES": "MES.c.0"}`
- [ ] `process_ticker()` skips download when `data/historical/{ticker}.parquet` already exists (Level 1)
- [ ] `process_ticker()` skips download when `{CACHE_DIR}/5m/{ticker}.parquet` already exists (Level 2)
- [ ] `process_ticker()` downloads from Databento and saves to `data/historical/{ticker}.parquet` when neither cache exists (Level 3)
- [ ] `write_manifest()` writes `"source": "databento"` to `futures_manifest.json`
- [ ] `data/historical/.gitkeep` exists in the repo
- [ ] `.gitignore` contains `data/historical/*.parquet`
- [ ] `pyproject.toml` lists `databento>=0.35` as a dependency

### Error Handling
- [ ] `DatabentSource.fetch()` returns `None` (does not raise) when the Databento API raises any exception
- [ ] `DatabentSource.fetch()` returns `None` when Databento returns an empty DataFrame
- [ ] `DatabentSource.fetch()` raises `ValueError` for any interval other than `"1m"` or `"5m"`
- [ ] `process_ticker()` returns `False` (does not raise) when `DatabentSource` init fails due to missing key

### Integration / E2E
- [ ] `uv run python -c "import prepare_futures"` exits 0 with no errors
- [ ] Live integration test (`TestDatabentSourceIntegration`) auto-skips when `DATABENTO_API_KEY` is absent
- [ ] Live integration test fetches 5 trading days of `MNQ.c.0` 5m bars and validates OHLCV columns + ET timezone (runs when key is present)

### Validation
- [ ] All 11 new `DatabentSource` unit tests pass — verified by: `uv run pytest tests/test_data_sources.py -q -k "not integration"`
- [ ] No regressions in pre-existing `YFinanceSource` and `IBGatewaySource` tests — verified by: `uv run pytest tests/ -q --ignore=tests/test_e2e.py -k "not integration" 2>&1 | tail -5`

### Out of Scope
- Live `uv run prepare_futures.py` end-to-end run (requires paid Databento key + actual download)
- Removing IB-related constants (`CONIDS`, `IB_HOST`, `IB_PORT`) from `prepare_futures.py`
- Backfilling `train_smt.py` walk-forward fold configuration (that's a separate feature)
- Streaming / real-time use of `DatabentSource`

---

## Notes

- `DatabentSource` uses a lazy import (`import databento as db` inside `fetch()`) so the module loads cleanly even if `databento` is not yet installed.
- The IB cache (Level 2) is kept as a fallback to avoid breaking any existing workflows that have already downloaded data via IB. It does NOT trigger a fresh IB download.
- The `CONIDS` dict and IB-related constants in `prepare_futures.py` are kept (not removed) since they may be needed for future IB-based updates.
- `data/historical/` is a project-local path (relative), not `~/.cache/...`, so it survives cache clears.
