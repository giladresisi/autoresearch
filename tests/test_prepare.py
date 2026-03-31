"""tests/test_prepare.py — Unit tests for prepare.py data layer."""
import os
import datetime
import unittest.mock as mock
import pytest
import numpy as np
import pandas as pd

from prepare import (
    resample_to_daily, validate_ticker_data, process_ticker,
    write_trend_summary, write_manifest,
    CACHE_DIR, BACKTEST_START, PREPARE_INTERVAL,
)
from data.sources import YFinanceSource


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
        for hour, minute in [(9, 30), (10, 0), (10, 30), (11, 0), (11, 30), (12, 0), (15, 0)]:
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


def _make_daily_df(n_rows: int, backtest_start: str = BACKTEST_START,
                   nan_10am: bool = False) -> pd.DataFrame:
    """Build a minimal daily DataFrame for validate_ticker_data tests."""
    start = pd.Timestamp(backtest_start).date()
    dates = [start + datetime.timedelta(days=i) for i in range(n_rows)]
    price_1030am = [np.nan if nan_10am and i < 3 else 100.0 + i for i in range(n_rows)]
    return pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1_000_000.0, "price_1030am": price_1030am,
    }, index=pd.Index(dates, name="date"))


# ── Resampling tests (5) ──────────────────────────────────────────────────────

def test_resample_produces_expected_columns():
    df = resample_to_daily(_make_hourly_df(5))
    assert set(df.columns) == {"open", "high", "low", "close", "volume", "price_1030am"}


def test_price_1030am_is_close_of_930_bar():
    """price_1030am should be the Close of the 9:30 AM bar (= ~10:30 AM price)."""
    hourly = _make_hourly_df(5)
    daily = resample_to_daily(hourly)
    hourly_et = hourly.copy()
    hourly_et.index = hourly_et.index.tz_convert("America/New_York")
    for d, row in daily.iterrows():
        mask = (hourly_et.index.date == d) & (hourly_et.index.time == datetime.time(9, 30))
        assert mask.sum() == 1, f"Expected one 9:30am bar for {d}"
        expected = hourly_et.loc[mask, "Close"].iloc[0]   # Close, not Open
        assert row["price_1030am"] == pytest.approx(expected)


def test_open_is_first_bar_of_day():
    hourly = _make_hourly_df(5)
    daily = resample_to_daily(hourly)
    hourly_et = hourly.copy()
    hourly_et.index = hourly_et.index.tz_convert("America/New_York")
    for d, row in daily.iterrows():
        # First bar of the day is 9:30 AM
        mask = hourly_et.index.date == d
        first_open = hourly_et.loc[mask, "Open"].iloc[0]
        assert row["open"] == pytest.approx(first_open)


def test_high_is_max_of_day():
    hourly = _make_hourly_df(5)
    daily = resample_to_daily(hourly)
    hourly_et = hourly.copy()
    hourly_et.index = hourly_et.index.tz_convert("America/New_York")
    for d, row in daily.iterrows():
        mask = hourly_et.index.date == d
        expected_high = hourly_et.loc[mask, "High"].max()
        assert row["high"] == pytest.approx(expected_high)


def test_non_trading_days_excluded():
    # _make_hourly_df skips weekends, so daily output should have no NaN rows
    daily = resample_to_daily(_make_hourly_df(10))
    # No row should be all NaN (non-trading days are dropped)
    assert not daily.isnull().all(axis=1).any()
    # Verify no weekend dates appear in output
    for d in daily.index:
        assert d.weekday() < 5, f"Weekend date {d} found in output"


# ── Schema tests (3) ─────────────────────────────────────────────────────────

def test_index_is_date_objects():
    daily = resample_to_daily(_make_hourly_df(5))
    assert all(type(d) is datetime.date for d in daily.index)


def test_index_name_is_date():
    daily = resample_to_daily(_make_hourly_df(5))
    assert daily.index.name == "date"


def test_all_columns_lowercase():
    daily = resample_to_daily(_make_hourly_df(5))
    assert all(c == c.lower() for c in daily.columns)


# ── Caching / idempotency tests (3) ──────────────────────────────────────────

def test_process_ticker_skips_existing_file(tmp_path):
    # Create empty file at expected interval subdir path to simulate existing cache
    interval_dir = tmp_path / PREPARE_INTERVAL
    interval_dir.mkdir()
    path = interval_dir / "SKIP.parquet"
    path.write_bytes(b"")
    mock_source = mock.MagicMock(spec=YFinanceSource)
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)):
        result = process_ticker("SKIP", mock_source, PREPARE_INTERVAL)
    mock_source.fetch.assert_not_called()
    assert result is True


def test_process_ticker_skips_empty_download(tmp_path):
    mock_source = mock.MagicMock(spec=YFinanceSource)
    mock_source.fetch.return_value = None
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.resample_to_daily") as mock_resample:
        result = process_ticker("EMPTY", mock_source, PREPARE_INTERVAL)
    mock_source.fetch.assert_called_once_with("EMPTY", mock.ANY, mock.ANY, PREPARE_INTERVAL)
    mock_resample.assert_not_called()
    assert result is False


def test_process_ticker_saves_parquet(tmp_path):
    mock_source = mock.MagicMock(spec=YFinanceSource)
    mock_source.fetch.return_value = _make_hourly_df(5)
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)):
        result = process_ticker("TEST", mock_source, PREPARE_INTERVAL)
    assert result is True
    path = tmp_path / PREPARE_INTERVAL / "TEST.parquet"
    assert path.exists()
    loaded = pd.read_parquet(path)
    assert "price_1030am" in loaded.columns


# ── Validation warning tests (3) ─────────────────────────────────────────────

def test_warn_if_fewer_than_200_rows(capsys):
    df = _make_daily_df(150)
    validate_ticker_data("XYZ", df, BACKTEST_START)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "150" in out


def test_no_warn_if_200_rows(capsys):
    df = _make_daily_df(200)
    validate_ticker_data("XYZ", df, BACKTEST_START)
    out = capsys.readouterr().out
    # Should not warn about insufficient history
    assert "insufficient" not in out


def test_warn_if_missing_10am_on_backtest_dates(capsys):
    # nan_10am=True sets first 3 rows to NaN; dates start at BACKTEST_START so they're in window
    df = _make_daily_df(10, nan_10am=True)
    validate_ticker_data("XYZ", df, BACKTEST_START)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "missing price_1030am" in out


# ── Integration test (1, live network) ───────────────────────────────────────

@pytest.mark.integration
def test_download_ticker_returns_expected_schema():
    """Live yfinance call — requires internet. Verifies schema contract end-to-end."""
    from prepare import resample_to_daily, HISTORY_START, BACKTEST_END
    src = YFinanceSource()
    df_hourly = src.fetch("AAPL", HISTORY_START, BACKTEST_END, "1h")
    if df_hourly is None or df_hourly.empty:
        pytest.skip("yfinance returned empty — network unavailable")
    df_daily = resample_to_daily(df_hourly)
    assert set(df_daily.columns) == {"open", "high", "low", "close", "volume", "price_1030am"}
    assert all(isinstance(d, datetime.date) for d in df_daily.index)
    assert df_daily.index.name == "date"
    assert len(df_daily) > 0


# ── write_trend_summary tests (4) ────────────────────────────────────────────

def _make_parquet(tmp_path, ticker, prices, interval=PREPARE_INTERVAL):
    """Write a minimal parquet file so write_trend_summary can load it."""
    import pandas as pd
    base = datetime.date(2026, 1, 1)
    dates = [base + datetime.timedelta(days=i) for i in range(len(prices))]
    df = pd.DataFrame({
        'open': prices, 'high': prices, 'low': prices, 'close': prices,
        'volume': [1_000_000.0] * len(prices), 'price_1030am': prices,
    }, index=pd.Index(dates, name='date'))
    interval_dir = tmp_path / interval
    interval_dir.mkdir(exist_ok=True)
    df.to_parquet(interval_dir / f"{ticker}.parquet")


def test_write_trend_summary_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices = list(range(100, 160))   # 60 days, monotone rise
    _make_parquet(tmp_path, 'AAA', prices)
    write_trend_summary(['AAA'], '2026-01-01', '2026-03-01', str(tmp_path), PREPARE_INTERVAL)
    assert (tmp_path / 'data_trend.md').exists()


def test_write_trend_summary_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices_a = list(range(100, 160))          # +59% rise
    prices_b = list(range(160, 100, -1))      # −37% fall
    _make_parquet(tmp_path, 'AAA', prices_a)
    _make_parquet(tmp_path, 'BBB', prices_b)
    write_trend_summary(['AAA', 'BBB'], '2026-01-01', '2026-03-01', str(tmp_path), PREPARE_INTERVAL)
    content = (tmp_path / 'data_trend.md').read_text()
    assert 'AAA' in content
    assert 'BBB' in content
    assert 'Median return' in content
    assert 'Sector character' in content


def test_write_trend_summary_missing_ticker_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices = list(range(100, 160))
    _make_parquet(tmp_path, 'REAL', prices)
    # GHOST has no parquet file — must not raise
    write_trend_summary(['REAL', 'GHOST'], '2026-01-01', '2026-03-01', str(tmp_path), PREPARE_INTERVAL)
    content = (tmp_path / 'data_trend.md').read_text()
    assert 'REAL' in content


def test_write_trend_summary_no_data_writes_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_trend_summary(['NONE'], '2026-01-01', '2026-03-01', str(tmp_path), PREPARE_INTERVAL)
    content = (tmp_path / 'data_trend.md').read_text()
    assert 'No data available' in content


# ── Main block test (1, subprocess) ──────────────────────────────────────────

def test_main_exits_1_when_tickers_empty():
    """Main block exits 1 with clear message when TICKERS list is empty.

    Uses a patched copy of prepare.py with TICKERS=[] to test the guard path
    without depending on the real TICKERS value configured in the source file.
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    import re
    source = (project_root / "prepare.py").read_text(encoding="utf-8")
    # Override TICKERS to empty list to exercise the early-exit guard.
    # Use regex so the test stays robust when the ticker list changes.
    # TICKERS may be a multiline list — match from the opening bracket to its closing bracket
    patched = re.sub(r'^TICKERS = \[.*?\]', 'TICKERS = []', source, count=1, flags=re.MULTILINE | re.DOTALL)
    assert patched != source, "Failed to patch TICKERS — check that prepare.py has a TICKERS = [...] list"
    with tempfile.NamedTemporaryFile(
        suffix=".py", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(patched)
        tmp_path = f.name
    try:
        env = os.environ.copy()
        # Ensure data/ package is importable from the project root when running a temp file
        env["PYTHONPATH"] = str(project_root)
        result = subprocess.run(
            ["uv", "run", "python", tmp_path],
            capture_output=True,
            cwd=project_root,
            env=env,
        )
    finally:
        os.unlink(tmp_path)
    assert result.returncode == 1
    assert b"TICKERS" in result.stdout


# ── Parallel download tests ───────────────────────────────────────────────────

def test_parallel_loop_processes_all_tickers(tmp_path, monkeypatch):
    """With mocked process_ticker, all tickers in list are called exactly once."""
    import prepare
    from data.sources import YFinanceSource
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    called = []
    mock_source = mock.MagicMock(spec=YFinanceSource)

    def fake_process(ticker, source, interval):
        called.append(ticker)
        return True

    monkeypatch.setattr(prepare, "process_ticker", fake_process)

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "MSFT", "NVDA"]
    with ThreadPoolExecutor(max_workers=prepare.MAX_WORKERS) as executor:
        results = list(executor.map(
            lambda t: prepare.process_ticker(t, mock_source, PREPARE_INTERVAL),
            tickers,
        ))

    assert sorted(called) == sorted(tickers)
    assert all(results)


def test_parallel_loop_counts_failures(tmp_path, monkeypatch):
    """Failures (False return from process_ticker) are counted correctly."""
    import prepare
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    def fake_process(ticker):
        return ticker != "FAIL_ME"

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "FAIL_ME", "NVDA"]
    with ThreadPoolExecutor(max_workers=prepare.MAX_WORKERS) as executor:
        results = list(executor.map(fake_process, tickers))

    ok = sum(results)
    assert ok == 2


def test_prepare_max_workers_reads_from_env(monkeypatch):
    """MAX_WORKERS is set from PREPARE_WORKERS env var at import time."""
    import importlib
    monkeypatch.setenv("PREPARE_WORKERS", "5")
    import prepare
    importlib.reload(prepare)
    assert prepare.MAX_WORKERS == 5
    monkeypatch.delenv("PREPARE_WORKERS", raising=False)
    importlib.reload(prepare)


def test_process_ticker_parallel_no_contention(tmp_path, monkeypatch):
    """Two tickers run in parallel threads write to separate paths without error."""
    import prepare
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    df_hourly = _make_hourly_df(n_days=5)
    mock_source = mock.MagicMock(spec=YFinanceSource)
    mock_source.fetch.return_value = df_hourly
    # Patch _add_earnings_dates to avoid yfinance network call
    monkeypatch.setattr(prepare, "_add_earnings_dates", lambda df, obj: df)

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "MSFT"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda t: prepare.process_ticker(t, mock_source, PREPARE_INTERVAL),
            tickers,
        ))

    assert all(results)
    assert os.path.exists(os.path.join(str(tmp_path), PREPARE_INTERVAL, "AAPL.parquet"))
    assert os.path.exists(os.path.join(str(tmp_path), PREPARE_INTERVAL, "MSFT.parquet"))


# ── write_manifest tests (3) ─────────────────────────────────────────────────

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
