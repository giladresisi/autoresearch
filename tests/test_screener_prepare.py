"""tests/test_screener_prepare.py — Unit tests for screener_prepare.py logic."""
import datetime
import os

import pandas as pd
import pytest

from tests.test_screener import make_pivot_signal_df


@pytest.fixture()
def screener_cache_tmpdir(tmp_path, monkeypatch):
    """Function-scoped fixture: empty SCREENER_CACHE_DIR in a temp directory."""
    monkeypatch.setenv("AUTORESEARCH_SCREENER_CACHE_DIR", str(tmp_path))
    # Re-import to pick up the new env var
    import importlib
    import screener_prepare
    monkeypatch.setattr(screener_prepare, "SCREENER_CACHE_DIR", str(tmp_path))
    return tmp_path


def _write_parquet(path, last_date):
    """Write a minimal parquet with last row at last_date."""
    df = make_pivot_signal_df(250)
    df.index = pd.Index(
        [last_date - datetime.timedelta(days=i) for i in range(len(df) - 1, -1, -1)],
        name="date",
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path)


def test_is_ticker_current_false_if_no_file(screener_cache_tmpdir, monkeypatch):
    """Missing parquet → not current."""
    import screener_prepare
    assert screener_prepare.is_ticker_current("AAPL") is False


def test_is_ticker_current_false_if_stale(screener_cache_tmpdir, monkeypatch):
    """Parquet with last row 5 days ago → not current."""
    import screener_prepare
    stale_date = datetime.date.today() - datetime.timedelta(days=5)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, stale_date)
    assert screener_prepare.is_ticker_current("AAPL") is False


def test_is_ticker_current_true_if_yesterday(screener_cache_tmpdir, monkeypatch):
    """Parquet with last row = yesterday → current."""
    import screener_prepare
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, yesterday)
    assert screener_prepare.is_ticker_current("AAPL") is True


def test_download_and_cache_creates_parquet(screener_cache_tmpdir, monkeypatch):
    """With mocked yfinance, download_and_cache writes a parquet."""
    import screener_prepare

    df_hourly = _make_hourly_df()

    class FakeTicker:
        def history(self, **kwargs):
            return df_hourly

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    screener_prepare.download_and_cache("AAPL", "2024-01-01")
    parquet_path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    assert os.path.exists(parquet_path)


def test_download_and_cache_output_has_price_1030am(screener_cache_tmpdir, monkeypatch):
    """Downloaded and cached parquet has price_1030am column (resample_to_daily was used)."""
    import screener_prepare

    df_hourly = _make_hourly_df()

    class FakeTicker:
        def history(self, **kwargs):
            return df_hourly

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    screener_prepare.download_and_cache("AAPL", "2024-01-01")
    parquet_path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    df = pd.read_parquet(parquet_path)
    assert "price_1030am" in df.columns


def test_screener_prepare_main_skips_current_tickers(screener_cache_tmpdir, monkeypatch, capsys):
    """main() skips a ticker whose parquet is already current."""
    import screener_prepare

    # Pre-seed a current parquet for AAPL
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, yesterday)

    # Mock universe to just AAPL and mock yfinance (shouldn't be called)
    called = []

    class FakeTicker:
        def history(self, **kwargs):
            called.append(True)
            return pd.DataFrame()

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    monkeypatch.setattr("screener_prepare.fetch_screener_universe", lambda: ["AAPL"])

    # Run main block logic inline (simulating __main__)
    import datetime as _dt
    today = _dt.date.today()
    history_start = (today - _dt.timedelta(days=screener_prepare.HISTORY_DAYS)).strftime("%Y-%m-%d")
    universe = screener_prepare.fetch_screener_universe()
    for ticker in universe:
        if screener_prepare.is_ticker_current(ticker):
            continue  # should skip AAPL
        screener_prepare.download_and_cache(ticker, history_start)

    assert not called, "download_and_cache should not be called for a current ticker"


def test_download_and_cache_incremental_appends_new_rows(screener_cache_tmpdir, monkeypatch):
    """Existing parquet + new daily data → combined result has rows from both."""
    import screener_prepare

    # Seed an existing parquet with data ending 2024-01-10
    existing_end = datetime.date(2024, 1, 10)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, existing_end)
    existing_row_count = len(pd.read_parquet(path))

    # New download returns 3 days of data starting from 2024-01-12
    new_hourly = _make_hourly_df(start="2024-01-12 09:30", periods=21)  # 3 trading days

    class FakeTicker:
        def history(self, **kwargs):
            return new_hourly

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    screener_prepare.download_and_cache("AAPL", "2023-10-01")

    result = pd.read_parquet(path)
    # Must have more rows than either input alone (old rows preserved + new rows added)
    assert len(result) > existing_row_count
    assert len(result) > 3


def test_download_and_cache_deduplicates_overlap(screener_cache_tmpdir, monkeypatch):
    """Overlapping date in new download replaces the existing row (keep newest data)."""
    import screener_prepare
    import numpy as np

    # Seed parquet with last row on 2024-01-10 with close=100.0
    existing_end = datetime.date(2024, 1, 10)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, existing_end)
    existing_df = pd.read_parquet(path)
    existing_df.iloc[-1, existing_df.columns.get_loc("close")] = 100.0
    existing_df.to_parquet(path)

    # New download covers 2024-01-10 with updated close=150.0
    # Start on 2024-01-10 so resample_to_daily produces a row for that date
    overlap_hourly = _make_hourly_df(start="2024-01-10 09:30", periods=7)  # 1 trading day
    overlap_hourly["Close"] = 150.0

    class FakeTicker:
        def history(self, **kwargs):
            return overlap_hourly

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    screener_prepare.download_and_cache("AAPL", "2023-10-01")

    result = pd.read_parquet(path)
    # The overlapping date should use the newest value (150.0), not the old one (100.0)
    last_close = float(result.loc[existing_end, "close"]) if existing_end in result.index else None
    assert last_close is not None, "2024-01-10 row should be present"
    assert abs(last_close - 150.0) < 1.0


def test_download_and_cache_falls_back_to_full_on_corrupt_existing(screener_cache_tmpdir, monkeypatch):
    """If existing parquet is unreadable, falls back to a full download without crashing."""
    import screener_prepare

    # Write a corrupt file
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    with open(path, "wb") as f:
        f.write(b"not a parquet")

    new_hourly = _make_hourly_df()

    class FakeTicker:
        def history(self, **kwargs):
            return new_hourly

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    screener_prepare.download_and_cache("AAPL", "2024-01-01")

    result = pd.read_parquet(path)
    assert not result.empty


def _make_hourly_df(start="2024-01-02 09:30", periods=70):
    """Create a minimal hourly DataFrame that resample_to_daily can process."""
    import numpy as np
    import pandas as pd
    timestamps = pd.date_range(start, periods=periods, freq="1h", tz="America/New_York")
    n = len(timestamps)
    return pd.DataFrame({
        "Open":   np.linspace(100, 110, n),
        "High":   np.linspace(101, 111, n),
        "Low":    np.linspace(99, 109, n),
        "Close":  np.linspace(100, 110, n),
        "Volume": np.full(n, 100_000.0),
    }, index=timestamps)
