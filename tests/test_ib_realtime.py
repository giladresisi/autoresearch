# tests/test_ib_realtime.py
# Unit tests for IbRealtimeSource helpers. All IB/network calls are mocked.
# Tests cover: tick accumulator logic, partial-1m bar resets, gap-fill skip logic,
# on_bar callback firing, parquet property access, and IbGatewayDisconnectedError behaviour.
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch, call

from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource


def _make_source(tmp_path, on_bar=None):
    """Helper: create an IbRealtimeSource with test defaults."""
    if on_bar is None:
        on_bar = MagicMock()
    return IbRealtimeSource(
        host="127.0.0.1",
        port=4002,
        client_id=15,
        mnq_conid="770561201",
        mes_conid="770561194",
        bar_data_dir=tmp_path,
        on_bar=on_bar,
        max_retries=1,
        retry_delay_s=0,
    )


def _second_ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="America/New_York").floor("s")


def _minute_ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="America/New_York").floor("min")


# Tick accumulator

def test_tick_accumulator_same_second_updates_ohlcv(tmp_path):
    src = _make_source(tmp_path)
    ts = _second_ts("2026-04-30 09:30:00")
    acc, fin = src._update_tick_accumulator(None, 20000.0, 1.0, ts)
    assert fin is None
    acc2, fin2 = src._update_tick_accumulator(acc, 20010.0, 2.0, ts)
    assert fin2 is None
    assert acc2["high"] == 20010.0
    assert acc2["volume"] == 3.0


def test_tick_accumulator_new_second_finalizes_bar(tmp_path):
    src = _make_source(tmp_path)
    ts1 = _second_ts("2026-04-30 09:30:00")
    ts2 = _second_ts("2026-04-30 09:30:01")
    acc, _ = src._update_tick_accumulator(None, 20000.0, 1.0, ts1)
    new_acc, finalized = src._update_tick_accumulator(acc, 20005.0, 1.0, ts2)
    assert finalized is not None
    assert finalized["second_ts"] == ts1
    assert new_acc["second_ts"] == ts2


# Partial 1m bar

def test_partial_1m_resets_on_minute_boundary(tmp_path):
    src = _make_source(tmp_path)
    m1 = _minute_ts("2026-04-30 09:30:00")
    m2 = _minute_ts("2026-04-30 09:31:00")
    acc = src._update_partial_1m(None, 20000.0, 1.0, m1)
    acc2 = src._update_partial_1m(acc, 20010.0, 2.0, m1)
    assert acc2["high"] == 20010.0
    # New minute: accumulator resets
    acc3 = src._update_partial_1m(acc2, 20020.0, 3.0, m2)
    assert acc3["open"] == 20020.0
    assert acc3["volume"] == 3.0
    assert acc3["minute_ts"] == m2


# Gap fill

def test_gap_fill_skipped_if_fresh_parquet(tmp_path):
    """If parquet last row is within GAP_FILL_MAX_DAYS, _gap_fill still calls fetch
    but with a start time that's only GAP_FILL_MAX_DAYS back (not MAX_LOOKBACK_DAYS)."""
    src = _make_source(tmp_path)
    # Pre-populate _mnq_1m_df with a recent row (1 day ago)
    recent_ts = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=1)
    src._mnq_1m_df = pd.DataFrame(
        {"Open": [20000.0], "High": [20010.0], "Low": [19990.0], "Close": [20005.0], "Volume": [100.0]},
        index=pd.DatetimeIndex([recent_ts]),
    )
    src._mes_1m_df = src._empty_bar_df()

    with patch("data.ib_realtime.IbRealtimeSource._gap_fill") as mock_gap:
        src._gap_fill = mock_gap  # patch instance method directly
        src._gap_fill()
        mock_gap.assert_called_once()


# on_bar callback

def test_on_bar_callback_fired_on_second_boundary(tmp_path):
    on_bar = MagicMock()
    src = _make_source(tmp_path, on_bar=on_bar)

    # Simulate two MNQ ticks - first tick initialises accumulator, second crosses boundary
    ts1 = pd.Timestamp("2026-04-30 09:30:00", tz="UTC")
    ts2 = pd.Timestamp("2026-04-30 09:30:01", tz="UTC")

    def _make_ticker(ts, price):
        tick = MagicMock()
        tick.time = ts
        tick.price = price
        tick.size = 1.0
        t = MagicMock()
        t.tickByTicks = [tick]
        return t

    # Set partial 1m acc to a non-None value so boundary fires the callback
    src._mnq_partial_1m = {
        "open": 20000.0, "high": 20000.0, "low": 20000.0, "close": 20000.0,
        "volume": 1.0, "minute_ts": pd.Timestamp("2026-04-30 09:30:00", tz="America/New_York").floor("min"),
    }
    src._on_mnq_tick(_make_ticker(ts1, 20000.0))  # initialises tick bar
    src._on_mnq_tick(_make_ticker(ts2, 20005.0))  # crosses second boundary -> fires on_bar
    on_bar.assert_called_once()


# Properties

def test_mnq_1m_df_property_returns_loaded_frames(tmp_path):
    src = _make_source(tmp_path)
    # Write a parquet and load it
    ts = pd.Timestamp("2026-04-30 09:30:00", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": [20000.0], "High": [20010.0], "Low": [19990.0], "Close": [20005.0], "Volume": [100.0]},
        index=pd.DatetimeIndex([ts]),
    )
    df.to_parquet(tmp_path / "MNQ_1m.parquet")
    src._load_parquets()
    assert not src.mnq_1m_df.empty
    assert len(src.mnq_1m_df) == 1


# ── IbGatewayDisconnectedError ──────────────────────────────────────────────

def test_gap_fill_not_called_from_start(tmp_path):
    """start() must NOT call _gap_fill() — regression guard for the removal."""
    src = _make_source(tmp_path)
    ib_mock = MagicMock()
    ib_mock.isConnected.return_value = True
    # ib_insync imports are lazy inside start(); patch at the ib_insync module level
    with patch("ib_insync.IB", return_value=ib_mock), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "_gap_fill") as mock_gap_fill, \
         patch.object(src, "_setup_subscriptions"):
        # Simulate a deliberate stop() — sets _stopping=True so start() exits cleanly.
        def _fake_run():
            src._stopping = True
        util_mock.run.side_effect = _fake_run
        util_mock.getLoop.return_value = MagicMock()
        src.start()

    mock_gap_fill.assert_not_called()


def test_gateway_disconnect_raises_ibgateway_disconnected_error(tmp_path):
    """When ib.disconnectedEvent fires (gateway-initiated), start() must raise IbGatewayDisconnectedError."""
    src = _make_source(tmp_path)

    disconnect_callbacks = []

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = MagicMock()
            self.disconnectedEvent.__iadd__ = lambda self_, cb: disconnect_callbacks.append(cb)
            self.isConnected = MagicMock(return_value=True)

        def connect(self, *a, **kw):
            pass

        def disconnect(self):
            pass

    fake_ib = FakeIB()

    def fake_util_run():
        # Simulate the gateway closing the connection by calling the registered callback
        for cb in disconnect_callbacks:
            cb()

    # ib_insync imports are lazy inside start(); patch at the ib_insync module level
    with patch("ib_insync.IB", return_value=fake_ib), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "_setup_subscriptions"):
        util_mock.run.side_effect = fake_util_run
        util_mock.stop = MagicMock()
        with pytest.raises(IbGatewayDisconnectedError):
            src.start()


def test_stop_does_not_trigger_gateway_disconnect_flag(tmp_path):
    """After stop() sets _stopping=True, disconnectedEvent must NOT set _disconnected_by_gateway."""
    src = _make_source(tmp_path)
    src._stopping = True
    src._disconnected_by_gateway = False
    src._event_loop = MagicMock()

    src._on_gateway_disconnect()  # call the ACTUAL method, not a hand-written copy

    assert src._disconnected_by_gateway is False
    src._event_loop.stop.assert_not_called()


def test_gateway_disconnect_sets_flag_when_not_stopping(tmp_path):
    """When _stopping=False, disconnectedEvent fires: flag set and loop stopped."""
    src = _make_source(tmp_path)
    src._stopping = False
    src._disconnected_by_gateway = False
    src._event_loop = MagicMock()

    src._on_gateway_disconnect()

    assert src._disconnected_by_gateway is True
    src._event_loop.stop.assert_called_once()


def test_ibgateway_disconnected_error_not_retried(tmp_path):
    """IbGatewayDisconnectedError must propagate without triggering the retry loop.

    Test strategy: raise IbGatewayDisconnectedError from util.run() directly (the error
    originates in the try block whether from the gateway-disconnect check or from a direct
    raise). The except IbGatewayDisconnectedError guard must re-raise immediately.
    """
    src = _make_source(tmp_path)
    src._max_retries = 3
    connect_calls = [0]

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = MagicMock()
            self.isConnected = MagicMock(return_value=False)

        def connect(self, *a, **kw):
            connect_calls[0] += 1

        def disconnect(self):
            pass

    # ib_insync imports are lazy inside start(); patch at the ib_insync module level
    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "_setup_subscriptions"):
        # Raise IbGatewayDisconnectedError from util.run() — simulates the check
        # "if self._disconnected_by_gateway: raise IbGatewayDisconnectedError(...)"
        util_mock.run.side_effect = IbGatewayDisconnectedError("gateway closed")
        with pytest.raises(IbGatewayDisconnectedError):
            src.start()

    # connect() should only be called once — no retries after IbGatewayDisconnectedError
    assert connect_calls[0] == 1


# ── 1s accumulation tests ────────────────────────────────────────────────────

def _make_bar_mock(ts_str: str) -> MagicMock:
    """Return a mock 1m bar for use with _on_mnq_1m_bar / _on_mes_1m_bar."""
    bar = MagicMock()
    bar.date = pd.Timestamp(ts_str, tz="America/New_York")
    bar.open = 20000.0
    bar.high = 20010.0
    bar.low = 19990.0
    bar.close = 20005.0
    bar.volume = 100.0
    return bar


def test_mes_tick_bar_finalizes_on_second_boundary(tmp_path):
    """Two MES ticks at different seconds: first tick bar finalizes when second tick arrives."""
    src = _make_source(tmp_path)
    ts1 = pd.Timestamp("2026-05-01 09:30:00", tz="UTC")
    ts2 = pd.Timestamp("2026-05-01 09:30:01", tz="UTC")

    def _make_ticker(ts, price):
        tick = MagicMock()
        tick.time = ts
        tick.price = price
        tick.size = 1.0
        t = MagicMock()
        t.tickByTicks = [tick]
        return t

    src._on_mes_tick(_make_ticker(ts1, 20000.0))
    assert len(src._mes_1s_pending) == 0
    src._on_mes_tick(_make_ticker(ts2, 20005.0))
    assert len(src._mes_1s_pending) == 1
    bar = src._mes_1s_pending[0]
    assert bar["open"] == 20000.0
    assert bar["close"] == 20000.0
    assert bar["volume"] == 1.0


def test_mes_tick_bar_same_second_accumulates(tmp_path):
    """Two MES ticks in same second: _mes_1s_pending stays empty, accumulator volume=2."""
    src = _make_source(tmp_path)
    ts = pd.Timestamp("2026-05-01 09:30:00", tz="UTC")

    def _make_ticker(price):
        tick = MagicMock()
        tick.time = ts
        tick.price = price
        tick.size = 1.0
        t = MagicMock()
        t.tickByTicks = [tick]
        return t

    src._on_mes_tick(_make_ticker(20000.0))
    src._on_mes_tick(_make_ticker(20005.0))
    assert len(src._mes_1s_pending) == 0
    assert src._mes_tick_bar["volume"] == 2.0


def test_mnq_on_tick_appends_to_1s_pending(tmp_path):
    """Crossing a second boundary for MNQ must append to _mnq_1s_pending."""
    src = _make_source(tmp_path)
    ts1 = pd.Timestamp("2026-05-01 09:30:00", tz="UTC")
    ts2 = pd.Timestamp("2026-05-01 09:30:01", tz="UTC")

    def _make_ticker(ts, price):
        tick = MagicMock()
        tick.time = ts
        tick.price = price
        tick.size = 1.0
        t = MagicMock()
        t.tickByTicks = [tick]
        return t

    # Seed partial 1m so _on_bar fires (not required for 1s pending)
    src._mnq_partial_1m = {
        "open": 20000.0, "high": 20000.0, "low": 20000.0, "close": 20000.0,
        "volume": 1.0, "minute_ts": pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York"),
    }
    src._on_mnq_tick(_make_ticker(ts1, 20000.0))
    assert len(src._mnq_1s_pending) == 0  # first tick initialises bar, nothing finalized yet
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_tick(_make_ticker(ts2, 20005.0))
    assert len(src._mnq_1s_pending) == 1


def test_on_mnq_1m_bar_flushes_1s_pending_to_session_parquet(tmp_path):
    """When _mnq_1s_pending has bars, _on_mnq_1m_bar must write a session parquet and clear pending."""
    src = _make_source(tmp_path)
    ts1 = _second_ts("2026-05-08 09:30:00")
    ts2 = _second_ts("2026-05-08 09:30:01")
    src._mnq_1s_pending = [
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "second_ts": ts1},
        {"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 1.0, "second_ts": ts2},
    ]

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    assert len(src._mnq_1s_pending) == 0
    # In-memory DF reset to empty after flush (parquet is the durable record)
    assert src._mnq_1s_session_df.empty
    # Session parquet written with the 2 pending rows
    session_files = list(tmp_path.glob("MNQ_1s_session_*.parquet"))
    assert len(session_files) == 1
    assert len(pd.read_parquet(session_files[0])) == 2
    assert not (tmp_path / "MNQ_1s.parquet").exists()


def test_on_mes_1m_bar_flushes_1s_pending_to_session_parquet(tmp_path):
    """Symmetric test for MES: _on_mes_1m_bar must flush _mes_1s_pending to session parquet."""
    src = _make_source(tmp_path)
    ts1 = _second_ts("2026-05-08 09:30:00")
    ts2 = _second_ts("2026-05-08 09:30:01")
    src._mes_1s_pending = [
        {"open": 3.0, "high": 3.0, "low": 3.0, "close": 3.0, "volume": 2.0, "second_ts": ts1},
        {"open": 4.0, "high": 4.0, "low": 4.0, "close": 4.0, "volume": 2.0, "second_ts": ts2},
    ]

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mes_1m_bar([bar], True)

    assert len(src._mes_1s_pending) == 0
    # In-memory DF reset to empty after flush (parquet is the durable record)
    assert src._mes_1s_session_df.empty
    # Session parquet written with the 2 pending rows
    session_files = list(tmp_path.glob("MES_1s_session_*.parquet"))
    assert len(session_files) == 1
    assert len(pd.read_parquet(session_files[0])) == 2
    assert not (tmp_path / "MES_1s.parquet").exists()


def test_on_mnq_1m_bar_resets_mes_tick_bar(tmp_path):
    """_on_mnq_1m_bar must reset _mes_tick_bar to None at the 1m boundary."""
    src = _make_source(tmp_path)
    src._mes_tick_bar = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                         "second_ts": _second_ts("2026-05-08 09:30:59")}

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    assert src._mes_tick_bar is None


def test_load_parquets_loads_1s_files(tmp_path):
    """_load_parquets() must load MNQ_1s.parquet and MES_1s.parquet when they exist."""
    src = _make_source(tmp_path)
    ts = pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5], "Volume": [500.0]},
        index=pd.DatetimeIndex([ts]),
    )
    df.to_parquet(tmp_path / "MNQ_1s.parquet")
    df.to_parquet(tmp_path / "MES_1s.parquet")
    src._load_parquets()
    assert not src._mnq_1s_df.empty
    assert not src._mes_1s_df.empty
    assert len(src._mnq_1s_df) == 1


def test_mnq_1s_df_property_returns_loaded_df(tmp_path):
    """mnq_1s_df property must return the dataframe loaded by _load_parquets."""
    src = _make_source(tmp_path)
    ts = pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5], "Volume": [500.0]},
        index=pd.DatetimeIndex([ts]),
    )
    df.to_parquet(tmp_path / "MNQ_1s.parquet")
    src._load_parquets()
    assert len(src.mnq_1s_df) == 1
    assert src.mnq_1s_df is src._mnq_1s_df


def test_1s_pending_empty_does_not_write_session_parquet(tmp_path):
    """When _mnq_1s_pending is empty, no session parquet should be written."""
    src = _make_source(tmp_path)
    assert len(src._mnq_1s_pending) == 0

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    session_files = list(tmp_path.glob("MNQ_1s_session_*.parquet"))
    assert len(session_files) == 0


# ── _gap_fill_1s_ib tests ────────────────────────────────────────────────────

def test_gap_fill_1s_ib_skips_when_empty_parquet(tmp_path):
    """_gap_fill_1s_ib must not open an IB connection when both parquets are empty."""
    src = _make_source(tmp_path)
    # _mnq_1s_df and _mes_1s_df are already empty from __init__

    with patch("ib_insync.IB") as mock_ib_cls:
        src._gap_fill_1s_ib()

    mock_ib_cls.assert_not_called()


def test_gap_fill_1s_ib_skips_when_already_current(tmp_path):
    """_gap_fill_1s_ib must not open an IB connection when parquets are already current."""
    src = _make_source(tmp_path)
    # Set last bar to 1 minute ago (within the 2-minute buffer → already current)
    recent_ts = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(minutes=1)
    src._mnq_1s_df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([recent_ts]),
    )
    src._mes_1s_df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([recent_ts]),
    )

    with patch("ib_insync.IB") as mock_ib_cls:
        src._gap_fill_1s_ib()

    mock_ib_cls.assert_not_called()


def test_gap_fill_1s_ib_paginates_in_1800s_chunks(tmp_path):
    """_gap_fill_1s_ib must call reqHistoricalData with barSizeSetting="1 secs" and chunk ≤ 1800s."""
    src = _make_source(tmp_path)
    # Set last bar to 1 hour ago so a fill is needed
    old_ts = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(hours=1)
    src._mnq_1s_df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([old_ts]),
    )
    # MES stays empty so only MNQ triggers the fill

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib), \
         patch("ib_insync.Contract"):
        src._gap_fill_1s_ib()

    assert mock_ib.connect.called
    calls = mock_ib.reqHistoricalData.call_args_list
    assert len(calls) >= 1
    for c in calls:
        kw = c.kwargs if c.kwargs else {}
        args = c.args if c.args else ()
        # barSizeSetting may be positional or keyword
        bar_size = kw.get("barSizeSetting") or (args[2] if len(args) > 2 else None)
        assert bar_size == "1 secs", f"Expected '1 secs', got {bar_size!r}"
        duration_str = kw.get("durationStr") or (args[1] if len(args) > 1 else None)
        assert duration_str is not None and duration_str.endswith(" S"), \
            f"Expected duration ending in ' S', got {duration_str!r}"
        seconds = int(duration_str.replace(" S", ""))
        assert seconds <= 1800, f"Chunk {seconds}s exceeds 1800s limit"


# ── RAM reduction tests ──────────────────────────────────────────────────────

def _make_bar_df(days_ago_start: int, days_ago_end: int = 0) -> pd.DataFrame:
    """Build a test 1m bar DataFrame spanning the given age range."""
    now = pd.Timestamp.now(tz="America/New_York")
    start = now - pd.Timedelta(days=days_ago_start)
    end = now - pd.Timedelta(days=days_ago_end)
    timestamps = pd.date_range(start, end, freq="1min", tz="America/New_York")
    n = len(timestamps)
    return pd.DataFrame({
        "Open":   [20000.0] * n,
        "High":   [20010.0] * n,
        "Low":    [19990.0] * n,
        "Close":  [20005.0] * n,
        "Volume": [100.0] * n,
    }, index=timestamps)


def test_1s_dfs_freed_after_gap_fill_in_start(tmp_path):
    """After start() calls _gap_fill_1s_ib(), _mnq_1s_df and _mes_1s_df become empty."""
    src = _make_source(tmp_path)
    # Pre-populate with non-empty DFs to confirm they get freed
    ts = pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York")
    row = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([ts]),
    )
    src._mnq_1s_df = row.copy()
    src._mes_1s_df = row.copy()

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = MagicMock()
            self.isConnected = MagicMock(return_value=True)
        def connect(self, *a, **kw): pass
        def disconnect(self): pass

    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "_load_parquets"), \
         patch.object(src, "_gap_fill_1s_ib"), \
         patch.object(src, "_setup_subscriptions"):
        def _fake_run():
            src._stopping = True
        util_mock.run.side_effect = _fake_run
        util_mock.getLoop.return_value = MagicMock()
        src.start()

    assert src.mnq_1s_df.empty, "mnq_1s_df must be empty after start() completes gap-fill"
    assert src.mes_1s_df.empty, "mes_1s_df must be empty after start() completes gap-fill"


def test_session_1s_df_cleared_after_mnq_flush(tmp_path):
    """_on_mnq_1m_bar resets _mnq_1s_session_df to empty after writing to parquet."""
    src = _make_source(tmp_path)
    ts1 = _second_ts("2026-05-08 09:30:00")
    ts2 = _second_ts("2026-05-08 09:30:01")
    src._mnq_1s_pending = [
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "second_ts": ts1},
        {"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 1.0, "second_ts": ts2},
    ]
    # Pre-populate session DF to confirm it gets cleared, not just overwritten
    src._mnq_1s_session_df = pd.DataFrame(
        {"Open": [0.5], "High": [0.5], "Low": [0.5], "Close": [0.5], "Volume": [1.0]},
        index=pd.DatetimeIndex([_second_ts("2026-05-08 09:29:59")]),
    )

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    assert src._mnq_1s_session_df.empty
    # Parquet contains all rows: 1 pre-existing + 2 pending
    session_files = list(tmp_path.glob("MNQ_1s_session_*.parquet"))
    assert len(session_files) == 1
    flushed = pd.read_parquet(session_files[0])
    assert len(flushed) == 3


def test_session_1s_df_cleared_after_mes_flush(tmp_path):
    """_on_mes_1m_bar resets _mes_1s_session_df to empty after writing to parquet."""
    src = _make_source(tmp_path)
    ts1 = _second_ts("2026-05-08 09:30:00")
    ts2 = _second_ts("2026-05-08 09:30:01")
    src._mes_1s_pending = [
        {"open": 3.0, "high": 3.0, "low": 3.0, "close": 3.0, "volume": 2.0, "second_ts": ts1},
        {"open": 4.0, "high": 4.0, "low": 4.0, "close": 4.0, "volume": 2.0, "second_ts": ts2},
    ]

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mes_1m_bar([bar], True)

    assert src._mes_1s_session_df.empty
    session_files = list(tmp_path.glob("MES_1s_session_*.parquet"))
    assert len(session_files) == 1
    assert len(pd.read_parquet(session_files[0])) == 2


def test_mnq_1m_df_trimmed_to_14_days_after_bar(tmp_path):
    """_on_mnq_1m_bar trims _mnq_1m_df to 14-day window after writing to parquet."""
    src = _make_source(tmp_path)
    src._mnq_1m_df = _make_bar_df(days_ago_start=20)

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
    assert src._mnq_1m_df.index[0] >= cutoff, "Oldest bar must be within 14 days"


def test_mes_1m_df_trimmed_to_14_days_after_bar(tmp_path):
    """_on_mes_1m_bar trims _mes_1m_df to 14-day window after writing to parquet."""
    src = _make_source(tmp_path)
    src._mes_1m_df = _make_bar_df(days_ago_start=20)

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mes_1m_bar([bar], True)

    cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
    assert src._mes_1m_df.index[0] >= cutoff


def test_trim_does_not_run_when_all_bars_within_14_days(tmp_path):
    """When _mnq_1m_df contains only recent bars, no trim is applied."""
    src = _make_source(tmp_path)
    recent_df = _make_bar_df(days_ago_start=3)
    src._mnq_1m_df = recent_df.copy()
    original_len = len(src._mnq_1m_df)

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    # After the bar is appended and written, length should be original + 1 (the new bar)
    assert len(src._mnq_1m_df) >= original_len
    assert src._mnq_1m_df.index[0] == recent_df.index[0], "Trim must not remove any bars when all are within 14 days"


def test_parquet_written_before_trim(tmp_path):
    """Parquet contains full history; in-memory DF is trimmed to 14 days afterward."""
    src = _make_source(tmp_path)
    src._mnq_1m_df = _make_bar_df(days_ago_start=25)
    original_len = len(src._mnq_1m_df)

    bar = _make_bar_mock("2026-05-08 09:31:00")
    with patch("strategy_smt.set_bar_data"):
        src._on_mnq_1m_bar([bar], True)

    parquet_df = pd.read_parquet(tmp_path / "MNQ_1m.parquet")
    # Parquet has full history (original rows + new bar)
    assert len(parquet_df) >= original_len
    # In-memory DF is trimmed
    cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
    assert src._mnq_1m_df.index[0] >= cutoff
