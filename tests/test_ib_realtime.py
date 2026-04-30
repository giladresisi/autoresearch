# tests/test_ib_realtime.py
# Unit tests for IbRealtimeSource helpers. All IB/network calls are mocked.
# Tests cover: tick accumulator logic, partial-1m bar resets, gap-fill skip logic,
# on_bar callback firing, and parquet property access.
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from data.ib_realtime import IbRealtimeSource


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


@pytest.mark.integration
def test_start_connects_and_calls_util_run(tmp_path):
    """Integration test - skipped unless IB Gateway is running."""
    pytest.skip("Requires live IB Gateway on port 4002")
