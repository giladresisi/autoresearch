"""tests/test_prepare.py — Unit tests for prepare.py data layer."""
import os
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
    price_10am = [np.nan if nan_10am and i < 3 else 100.0 + i for i in range(n_rows)]
    return pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1_000_000.0, "price_10am": price_10am,
    }, index=pd.Index(dates, name="date"))


# ── Resampling tests (5) ──────────────────────────────────────────────────────

def test_resample_produces_expected_columns():
    df = resample_to_daily(_make_hourly_df(5))
    assert set(df.columns) == {"open", "high", "low", "close", "volume", "price_10am"}


def test_price_10am_is_open_of_10am_bar():
    hourly = _make_hourly_df(5)
    daily = resample_to_daily(hourly)
    # Convert hourly index to ET for comparison
    hourly_et = hourly.copy()
    hourly_et.index = hourly_et.index.tz_convert("America/New_York")
    for d, row in daily.iterrows():
        # Find the 10am bar for this date in hourly data
        mask = (hourly_et.index.date == d) & (hourly_et.index.time == datetime.time(10, 0))
        assert mask.sum() == 1, f"Expected one 10am bar for {d}"
        expected = hourly_et.loc[mask, "Open"].iloc[0]
        assert row["price_10am"] == pytest.approx(expected)


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
    # Create empty file at expected path to simulate existing cache
    path = tmp_path / "SKIP.parquet"
    path.write_bytes(b"")
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.download_ticker") as mock_dl:
        result = process_ticker("SKIP")
    mock_dl.assert_not_called()
    assert result is True


def test_process_ticker_skips_empty_download(tmp_path):
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.download_ticker", return_value=pd.DataFrame()) as mock_dl, \
         mock.patch("prepare.resample_to_daily") as mock_resample:
        result = process_ticker("EMPTY")
    mock_dl.assert_called_once_with("EMPTY")
    mock_resample.assert_not_called()
    assert result is False


def test_process_ticker_saves_parquet(tmp_path):
    with mock.patch("prepare.CACHE_DIR", str(tmp_path)), \
         mock.patch("prepare.download_ticker", return_value=_make_hourly_df(5)):
        result = process_ticker("TEST")
    assert result is True
    path = tmp_path / "TEST.parquet"
    assert path.exists()
    loaded = pd.read_parquet(path)
    assert "price_10am" in loaded.columns


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
    assert "missing price_10am" in out


# ── Integration test (1, live network) ───────────────────────────────────────

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


# ── Main block test (1, subprocess) ──────────────────────────────────────────

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
