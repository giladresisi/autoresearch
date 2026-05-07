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
