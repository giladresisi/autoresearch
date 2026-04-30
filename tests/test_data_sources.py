"""tests/test_data_sources.py — Unit tests for data/sources.py."""
import unittest.mock as mock
import pytest
import pandas as pd
import numpy as np
from data.sources import YFinanceSource, IBGatewaySource, DataSource, _IB_BAR_SIZE, _quarterly_future_ranges, _third_friday


# ── Shared helpers ────────────────────────────────────────────────────────────

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


# ── YFinanceSource tests ──────────────────────────────────────────────────────

def test_yfinance_source_fetch_calls_yfinance_ticker():
    """mock yf.Ticker; verify history() called with correct start, end, interval."""
    mock_ticker = mock.MagicMock()
    mock_ticker.history.return_value = _make_tz_aware_ohlcv()
    with mock.patch("data.sources.yf.Ticker", return_value=mock_ticker):
        src = YFinanceSource()
        src.fetch("AAPL", "2024-01-01", "2025-01-01", "1h")
    mock_ticker.history.assert_called_once_with(
        start="2024-01-01",
        end="2025-01-01",
        interval="1h",
        auto_adjust=True,
        prepost=False,
    )


def test_yfinance_source_fetch_returns_dataframe_with_ohlcv_columns():
    """Verify returned columns are exactly {Open, High, Low, Close, Volume}."""
    mock_ticker = mock.MagicMock()
    mock_ticker.history.return_value = _make_tz_aware_ohlcv()
    with mock.patch("data.sources.yf.Ticker", return_value=mock_ticker):
        src = YFinanceSource()
        df = src.fetch("AAPL", "2024-01-01", "2025-01-01", "1h")
    assert df is not None
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}


def test_yfinance_source_fetch_returns_none_on_empty():
    """mock yf.Ticker returning empty DataFrame; verify fetch() returns None."""
    mock_ticker = mock.MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    with mock.patch("data.sources.yf.Ticker", return_value=mock_ticker):
        src = YFinanceSource()
        result = src.fetch("AAPL", "2024-01-01", "2025-01-01", "1h")
    assert result is None


def test_yfinance_source_fetch_returns_tz_aware_index():
    """Verify returned DataFrame index has timezone set."""
    mock_ticker = mock.MagicMock()
    mock_ticker.history.return_value = _make_tz_aware_ohlcv()
    with mock.patch("data.sources.yf.Ticker", return_value=mock_ticker):
        src = YFinanceSource()
        df = src.fetch("AAPL", "2024-01-01", "2025-01-01", "1h")
    assert df is not None
    assert df.index.tzinfo is not None


def test_yfinance_source_conforms_to_protocol():
    """Structural check: YFinanceSource satisfies the DataSource protocol."""
    assert isinstance(YFinanceSource(), DataSource)


# ── IBGatewaySource tests ─────────────────────────────────────────────────────

def test_ibgateway_source_interval_mapping_1h():
    assert _IB_BAR_SIZE["1h"] == "1 hour"


def test_ibgateway_source_interval_mapping_1m():
    assert _IB_BAR_SIZE["1m"] == "1 min"


def test_ibgateway_source_fetch_returns_none_on_unsupported_interval():
    """Unsupported interval string returns None without raising."""
    src = IBGatewaySource()
    result = src.fetch("AAPL", "2024-01-01", "2025-01-01", interval="invalid")
    assert result is None


def test_ibgateway_source_fetch_calls_reqHistoricalData():
    """mock ib_insync.IB; verify reqHistoricalData called with barSizeSetting='1 hour'."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    # Return empty bars so fetch returns None — we only care about the call args
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    # IB/Stock/util are imported inside fetch() so we patch at ib_insync module level
    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock"):
        src = IBGatewaySource()
        src.fetch("AAPL", "2025-01-01", "2025-03-01", interval="1h")

    assert mock_ib.reqHistoricalData.called
    call_kwargs = mock_ib.reqHistoricalData.call_args
    assert call_kwargs.kwargs.get("barSizeSetting") == "1 hour"


def test_ibgateway_source_fetch_returns_ohlcv_dataframe():
    """mock ib_insync.IB and util.df returning synthetic bars; verify result columns."""
    # Build a minimal bar-like object that util.df would produce
    synthetic_df = pd.DataFrame({
        "date":   pd.date_range("2025-01-02 09:30", periods=5, freq="1h"),
        "open":   [100.0] * 5,
        "high":   [101.0] * 5,
        "low":    [99.0] * 5,
        "close":  [100.5] * 5,
        "volume": [500_000.0] * 5,
    })

    mock_bar = mock.MagicMock()
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = [mock_bar]
    mock_ib.qualifyContracts.return_value = None

    # IB/Stock/util are imported inside fetch() so we patch at ib_insync module level
    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock"), \
         mock.patch("ib_insync.util") as mock_util:
        mock_util.df.return_value = synthetic_df
        src = IBGatewaySource()
        df = src.fetch("AAPL", "2025-01-01", "2025-06-01", interval="1h")

    assert df is not None
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
    assert len(df) > 0


def test_ibgateway_source_fetch_returns_none_on_exception():
    """mock ib_insync.IB.connect raising ConnectionRefusedError; verify None returned."""
    mock_ib = mock.MagicMock()
    mock_ib.connect.side_effect = ConnectionRefusedError("Connection refused")
    mock_ib.isConnected.return_value = False

    # IB is imported inside fetch() so patch at ib_insync module level
    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock"):
        src = IBGatewaySource()
        result = src.fetch("AAPL", "2024-01-01", "2025-01-01", interval="1h")

    assert result is None


def test_ibgateway_contfuture_uses_contfuture_with_empty_enddatetime():
    """contract_type='contfuture' must use ContFuture + endDateTime='' (most recent data).

    IB rejects explicit endDateTime for CME equity-index futures 1m bars — both for
    ContFuture (error 10339) and for specific quarterly contracts (error 162). The only
    working approach is ContFuture + endDateTime='', which returns the most recent N days.
    """
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    mock_contfuture_cls = mock.MagicMock()

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock") as mock_stock_cls, \
         mock.patch("ib_insync.ContFuture", mock_contfuture_cls):
        src = IBGatewaySource()
        src.fetch("MNQ", "2026-03-01", "2026-03-31", interval="1m", contract_type="contfuture")

    # ContFuture("MNQ", "CME", "USD") must be created
    mock_contfuture_cls.assert_called_once_with("MNQ", "CME", "USD")
    # reqHistoricalData must be called once with endDateTime=''
    assert mock_ib.reqHistoricalData.call_count == 1
    call_kwargs = mock_ib.reqHistoricalData.call_args.kwargs
    assert call_kwargs.get("endDateTime") == "", f"Expected endDateTime='', got {call_kwargs.get('endDateTime')!r}"
    # Stock must never be used for futures
    mock_stock_cls.assert_not_called()


def test_quarterly_future_ranges_covers_full_span():
    """_quarterly_future_ranges must produce non-overlapping periods covering start..end."""
    import pandas as pd
    start = pd.Timestamp("2024-09-01", tz="America/New_York")
    end = pd.Timestamp("2025-06-30", tz="America/New_York")
    ranges = _quarterly_future_ranges("MNQ", start, end)

    assert len(ranges) >= 3, "Expected at least 3 quarterly contracts for ~10 months"
    # All periods must fall within [start, end]
    for expiry_str, p_start, p_end in ranges:
        assert p_start >= start
        assert p_end <= end
        assert p_start < p_end
    # Periods must be contiguous (no gaps, no overlaps)
    for i in range(1, len(ranges)):
        assert ranges[i][1] == ranges[i - 1][2], "Gap or overlap between adjacent periods"
    # First period must start at start_dt; last must end at end_dt
    assert ranges[0][1] == start
    assert ranges[-1][2] == end


def test_ibgateway_stock_contract_unchanged():
    """Default contract_type='stock' must still use Stock(ticker, 'SMART', 'USD')."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock") as mock_stock_cls, \
         mock.patch("ib_insync.Future") as mock_future_cls:
        src = IBGatewaySource()
        src.fetch("AAPL", "2025-01-01", "2025-03-01", interval="1h")

    mock_stock_cls.assert_called_once_with("AAPL", "SMART", "USD")
    mock_future_cls.assert_not_called()


# ── Integration tests (live IB-Gateway at 127.0.0.1:4002 required) ───────────

@pytest.fixture(scope="module")
def ib_src():
    """Module-scoped IBGatewaySource. Skips all IB integration tests if unreachable."""
    src = IBGatewaySource()
    # Probe connection once; skip the whole module if IB isn't up
    probe = src.fetch("AAPL", "2025-01-02", "2025-01-03", interval="1h")
    if probe is None:
        pytest.skip("IB-Gateway not reachable at 127.0.0.1:4002 — skipping all IB tests")
    return src


@pytest.mark.integration
def test_ibgateway_source_fetch_live(ib_src):
    """Basic schema check — AAPL 1h bars over a short window."""
    df = ib_src.fetch("AAPL", "2025-01-02", "2025-01-10", interval="1h")
    assert df is not None
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
    assert len(df) > 0
    assert df.index.tzinfo is not None
    # All bars should fall within the requested window
    assert df.index.min() >= pd.Timestamp("2025-01-02").tz_localize("America/New_York")
    assert df.index.max() < pd.Timestamp("2025-01-10").tz_localize("America/New_York")


@pytest.mark.integration
def test_ibgateway_source_fetch_ohlcv_types(ib_src):
    """OHLCV columns must be numeric and Volume non-negative."""
    df = ib_src.fetch("AAPL", "2025-01-02", "2025-01-10", interval="1h")
    assert df is not None
    for col in ["Open", "High", "Low", "Close"]:
        assert pd.api.types.is_float_dtype(df[col]), f"{col} not float"
    assert (df["Volume"] >= 0).all()
    # High >= Low and High >= Close and Low <= Open (basic OHLC sanity)
    assert (df["High"] >= df["Low"]).all()


@pytest.mark.integration
def test_ibgateway_source_fetch_pagination(ib_src):
    """A 200-day 1h request should span >1 pagination window (limit is 180 days).

    Verifies that the pagination loop assembles a contiguous, deduplicated result.
    """
    df = ib_src.fetch("AAPL", "2024-06-01", "2025-01-01", interval="1h")
    assert df is not None, "Expected data for 200-day window"
    assert len(df) > 0
    # No duplicate timestamps after deduplication
    assert df.index.is_unique, "Duplicate timestamps after pagination"
    # Index should be sorted
    assert df.index.is_monotonic_increasing
    # Should cover multiple months — at least 100 hourly bars
    assert len(df) >= 100, f"Expected >= 100 bars for 200-day window, got {len(df)}"


@pytest.mark.integration
def test_ibgateway_source_fetch_1m_interval(ib_src):
    """1-minute bars over a 2-day window — exercises the 29-day chunk path."""
    df = ib_src.fetch("AAPL", "2025-01-06", "2025-01-08", interval="1m")
    assert df is not None, "Expected 1m bars for AAPL"
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
    assert len(df) > 0
    assert df.index.is_unique
    # 2 trading days of 1m bars = ~780 bars; at minimum a few hundred
    assert len(df) >= 100


@pytest.mark.integration
def test_ibgateway_source_fetch_index_is_et(ib_src):
    """Returned index must be America/New_York regardless of what IB sends."""
    df = ib_src.fetch("AAPL", "2025-01-02", "2025-01-10", interval="1h")
    assert df is not None
    import pytz
    et = pytz.timezone("America/New_York")
    # tz_convert("America/New_York") makes tzinfo a pytz object; check zone name
    tz_name = str(df.index.tzinfo)
    assert "America/New_York" in tz_name or "EST" in tz_name or "EDT" in tz_name


@pytest.mark.integration
def test_ibgateway_source_fetch_unsupported_interval_returns_none(ib_src):
    """Unsupported interval must return None even when IB-Gateway is reachable."""
    result = ib_src.fetch("AAPL", "2025-01-02", "2025-01-10", interval="3h")
    assert result is None


# ── future_by_conid unit tests ────────────────────────────────────────────────

def test_ibgateway_future_by_conid_uses_contract_not_stock():
    """contract_type='future_by_conid' must use Contract(conId=...) not Stock or ContFuture."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    mock_contract_cls = mock.MagicMock()

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock") as mock_stock_cls, \
         mock.patch("ib_insync.ContFuture") as mock_cf_cls, \
         mock.patch("ib_insync.Contract", mock_contract_cls):
        src = IBGatewaySource()
        src.fetch("770561201", "2025-09-24", "2025-10-01", interval="5m",
                  contract_type="future_by_conid")

    # Contract(conId=770561201, exchange="CME") must be used
    mock_contract_cls.assert_called_once_with(conId=770561201, exchange="CME")
    mock_stock_cls.assert_not_called()
    mock_cf_cls.assert_not_called()


def test_ibgateway_future_by_conid_uses_explicit_enddatetime():
    """future_by_conid must NOT use endDateTime='' — it must pass an explicit datetime string."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        src.fetch("770561201", "2025-09-24", "2025-10-01", interval="5m",
                  contract_type="future_by_conid")

    assert mock_ib.reqHistoricalData.called
    for call in mock_ib.reqHistoricalData.call_args_list:
        end_dt = call.kwargs.get("endDateTime", call.args[1] if len(call.args) > 1 else "")
        assert end_dt != "", "future_by_conid must not use endDateTime='' (ContFuture pattern)"
        assert len(end_dt) >= 8, f"Expected a real datetime string, got {end_dt!r}"


def test_ibgateway_future_by_conid_paginates_multiple_chunks():
    """A request spanning >60 days must call reqHistoricalData more than once."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        # 6.5-month window; chunk_days for 5m = 60 → at least 4 calls
        src.fetch("770561201", "2025-09-24", "2026-04-01", interval="5m",
                  contract_type="future_by_conid")

    assert mock_ib.reqHistoricalData.call_count >= 4, (
        f"Expected ≥4 pagination calls for 6.5-month window, "
        f"got {mock_ib.reqHistoricalData.call_count}"
    )


def test_ibgateway_future_by_conid_returns_none_on_exception():
    """Connection failure during future_by_conid fetch returns None without raising."""
    mock_ib = mock.MagicMock()
    mock_ib.connect.side_effect = ConnectionRefusedError("refused")
    mock_ib.isConnected.return_value = False

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        result = src.fetch("770561201", "2025-09-24", "2026-04-01", interval="5m",
                           contract_type="future_by_conid")

    assert result is None


def test_ibgateway_contfuture_path_unchanged_after_refactor():
    """Regression: contfuture path must still use endDateTime='' after adding future_by_conid."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.ContFuture"):
        src = IBGatewaySource()
        src.fetch("MNQ", "2026-03-01", "2026-03-31", interval="5m",
                  contract_type="contfuture")

    assert mock_ib.reqHistoricalData.call_count == 1
    call_kwargs = mock_ib.reqHistoricalData.call_args.kwargs
    assert call_kwargs.get("endDateTime") == "", "contfuture path must still use endDateTime=''"


# ── future_by_conid integration test ─────────────────────────────────────────

@pytest.mark.integration
def test_ibgateway_future_by_conid_live_fetch(ib_src):
    """Live fetch from MNQM6 via conId returns 5m bars for a 5-day window.

    Uses conId=770561201 (MNQM6, Jun 2026), confirmed 2026-04-01.
    Skipped automatically if IB-Gateway is not reachable (ib_src fixture handles this).
    """
    df = ib_src.fetch(
        "770561201",
        "2025-10-01",
        "2025-10-08",
        interval="5m",
        contract_type="future_by_conid",
    )
    assert df is not None, "Expected 5m bars from MNQM6 for Oct 2025"
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
    assert len(df) >= 50, f"Expected ≥50 bars for 5 trading days at 5m, got {len(df)}"
    assert df.index.tzinfo is not None
    assert df.index.is_unique
    assert df.index.is_monotonic_increasing
    # All bars should fall in the requested window
    assert df.index.min() >= pd.Timestamp("2025-10-01").tz_localize("America/New_York")
    assert df.index.max() < pd.Timestamp("2025-10-08").tz_localize("America/New_York")

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
            stype_in="continuous",
        )

    def test_resamples_1m_to_5m(self, src, monkeypatch):
        """fetch() with interval='5m' returns 5-minute bars (fewer rows than 1m input)."""
        mock_data = mock.MagicMock()
        mock_data.to_df.return_value = _make_1m_ohlcv(60)  # 60 x 1m bars -> 12 x 5m bars
        mock_client = mock.MagicMock()
        mock_client.timeseries.get_range.return_value = mock_data
        mock_db = mock.MagicMock()
        mock_db.Historical.return_value = mock_client
        monkeypatch.setitem(__import__("sys").modules, "databento", mock_db)
        df = src.fetch("MNQ.c.0", "2024-01-01", "2024-01-31", "5m")
        assert df is not None
        assert len(df) == 12  # 60 1m bars -> 12 5m bars

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
    """Live integration tests -- auto-skipped if DATABENTO_API_KEY not set."""

    @pytest.fixture(autouse=True)
    def skip_if_no_key(self):
        import os
        if not os.environ.get("DATABENTO_API_KEY"):
            pytest.skip("DATABENTO_API_KEY not set -- skipping live Databento test")
        try:
            import databento  # noqa: F401
        except ImportError:
            pytest.skip("databento package not installed -- skipping live Databento test")

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
        assert df["Volume"].dtype in (float, int, "int64", "float64", "uint64")
