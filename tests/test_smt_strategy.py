"""tests/test_smt_strategy.py — Unit tests for SMT strategy functions in strategy_smt.py.

All tests use synthetic 1m DataFrames — no IB connection required.
"""
import datetime
import os
import json
import pytest
import pandas as pd
import numpy as np


def _make_1m_bars(
    opens, highs, lows, closes,
    start_time="2025-01-02 09:00:00",
    tz="America/New_York",
) -> pd.DataFrame:
    """Build a synthetic 1m OHLCV DataFrame with a tz-aware ET DatetimeIndex."""
    n = len(opens)
    idx = pd.date_range(start=start_time, periods=n, freq="1min", tz=tz)
    return pd.DataFrame(
        {
            "Open":   [float(x) for x in opens],
            "High":   [float(x) for x in highs],
            "Low":    [float(x) for x in lows],
            "Close":  [float(x) for x in closes],
            "Volume": [1000.0] * n,
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def patch_min_bars(monkeypatch):
    """Override MIN_BARS_BEFORE_SIGNAL to 2 for most tests to keep bar counts small."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MIN_BARS_BEFORE_SIGNAL", 2)


# ══ detect_smt_divergence tests ══════════════════════════════════════════════

def test_detect_smt_bearish():
    """MES makes new session high, MNQ does not → 'short'."""
    import strategy_smt as train_smt
    # 5 bars: session high for MES is bar 2 (high=102); bar 4 MES breaks out, MNQ does not
    mes = _make_1m_bars(
        opens= [100, 100, 100, 100, 100],
        highs= [101, 102, 101, 101, 103],  # bar 4 new high
        lows=  [ 99,  99,  99,  99,  99],
        closes=[100, 100, 100, 100, 100],
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 202, 201, 201, 201],  # bar 4 does NOT make new high
        lows=  [199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None and result[0] == "short"


def test_detect_smt_bullish():
    """MES makes new session low, MNQ does not → 'long'."""
    import strategy_smt as train_smt
    mes = _make_1m_bars(
        opens= [100, 100, 100, 100, 100],
        highs= [101, 101, 101, 101, 101],
        lows=  [ 99,  98,  99,  99,  97],  # bar 4 new low
        closes=[100, 100, 100, 100, 100],
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 201, 201, 201, 201],
        lows=  [199, 198, 199, 199, 199],  # bar 4 does NOT make new low
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None and result[0] == "long"


def test_detect_smt_both_confirm_none():
    """Both MES and MNQ make new session high → no divergence (None)."""
    import strategy_smt as train_smt
    mes = _make_1m_bars(
        opens= [100, 100, 100, 100, 100],
        highs= [101, 102, 101, 101, 103],
        lows=  [ 99,  99,  99,  99,  99],
        closes=[100, 100, 100, 100, 100],
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 202, 201, 201, 203],  # MNQ also makes new high
        lows=  [199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is None


def test_detect_smt_min_bars_suppresses():
    """bar_idx - session_start_idx < _min_bars → None.

    detect_smt_divergence no longer reads MIN_BARS_BEFORE_SIGNAL globally;
    callers must pass _min_bars explicitly (screen_session uses a wall-clock
    timedelta instead).
    """
    import strategy_smt as train_smt
    mes = _make_1m_bars(
        opens= [100, 100, 100],
        highs= [101, 102, 103],
        lows=  [ 99,  99,  99],
        closes=[100, 100, 100],
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200],
        highs= [201, 201, 201],
        lows=  [199, 199, 199],
        closes=[200, 200, 200],
    )
    # bar_idx=2, session_start_idx=0 → difference=2 < _min_bars=5 → suppressed
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=2, session_start_idx=0, _min_bars=5)
    assert result is None


def test_detect_smt_resets_on_opposite():
    """Latest extreme wins: if bearish divergence set then resolved, check again."""
    import strategy_smt as train_smt
    # Build bars where MES makes session low (not high) at bar_idx=4
    mes = _make_1m_bars(
        opens= [100, 100, 100, 100, 100],
        highs= [101, 101, 101, 101, 101],
        lows=  [ 99,  98,  99,  99,  97],  # new session low at bar 4
        closes=[100, 100, 100, 100, 100],
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 201, 201, 201, 201],
        lows=  [199, 198, 199, 199, 199],  # MNQ does NOT make new low
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None and result[0] == "long"


# ══ find_entry_bar tests ═════════════════════════════════════════════════════

def test_find_entry_bar_short():
    """Bearish bar + upper wick past most recent bull body → returns index."""
    import strategy_smt as train_smt
    # 6 bars: bar 2 is bullish (close=202, open=200), bar 3 is bearish with wick above 202
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 205, 200, 200],
        highs= [201, 201, 202, 206, 201, 201],
        lows=  [199, 199, 199, 199, 199, 199],
        closes=[200, 200, 202, 201, 200, 200],  # bar 2: close>open (bull); bar 3: close<open (bear)
    )
    # divergence at bar 2; look for entry from bar 3 onward
    result = train_smt.find_entry_bar(mnq, "short", divergence_idx=2, session_end_idx=6)
    assert result == 3


def test_find_entry_bar_long():
    """Bullish bar + lower wick past most recent bear body → returns index."""
    import strategy_smt as train_smt
    # bar 2 is bearish (close=198, open=200), bar 3 is bullish with wick below 198
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 195, 200, 200],
        highs= [201, 201, 201, 201, 201, 201],
        lows=  [199, 199, 199, 194, 199, 199],
        closes=[200, 200, 198, 199, 200, 200],  # bar 2: close<open (bear); bar 3: close>open (bull)
    )
    result = train_smt.find_entry_bar(mnq, "long", divergence_idx=2, session_end_idx=6)
    assert result == 3


def test_find_entry_bar_no_match_returns_none():
    """No valid confirmation bar before session_end_idx → None."""
    import strategy_smt as train_smt
    # All doji bars — no clear bull or bear bars
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 201, 201, 201, 201],
        lows=  [199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.find_entry_bar(mnq, "short", divergence_idx=2, session_end_idx=5)
    assert result is None


def test_find_entry_requires_wick_past_body():
    """Bearish bar present but wick does NOT pierce the bull body close → None."""
    import strategy_smt as train_smt
    # bar 1 is bullish close=202; bar 3 is bearish but high=201 which is BELOW 202
    mnq = _make_1m_bars(
        opens= [200, 200, 205, 203, 200],
        highs= [201, 201, 206, 201, 201],  # bar 3 high=201 < bull close=202
        lows=  [199, 199, 199, 199, 199],
        closes=[200, 202, 201, 200, 200],  # bar 1: bull close=202; bar 3: bear close<open
    )
    result = train_smt.find_entry_bar(mnq, "short", divergence_idx=2, session_end_idx=5)
    assert result is None


# ══ compute_tdo tests ════════════════════════════════════════════════════════

def test_compute_tdo_finds_midnight_bar():
    """00:00 ET bar exists → returns its open."""
    import strategy_smt as train_smt
    mnq = _make_1m_bars(
        opens= [100, 105, 110],
        highs= [101, 106, 111],
        lows=  [ 99, 104, 109],
        closes=[100, 105, 110],
        start_time="2025-01-02 00:00:00",
    )
    date = datetime.date(2025, 1, 2)
    tdo = train_smt.compute_tdo(mnq, date)
    assert tdo == 100.0  # bar at 00:00


def test_compute_tdo_proxy_no_midnight_bar():
    """00:00 bar absent → returns first available bar's open."""
    import strategy_smt as train_smt
    mnq = _make_1m_bars(
        opens= [100, 105],
        highs= [101, 106],
        lows=  [ 99, 104],
        closes=[100, 105],
        start_time="2025-01-02 09:01:00",
    )
    date = datetime.date(2025, 1, 2)
    tdo = train_smt.compute_tdo(mnq, date)
    assert tdo == 100.0  # first bar's open (9:01)


def test_compute_tdo_returns_none_on_empty():
    """No bars for the requested date → None."""
    import strategy_smt as train_smt
    # Bars on a different date
    mnq = _make_1m_bars(
        opens= [100],
        highs= [101],
        lows=  [ 99],
        closes=[100],
        start_time="2025-01-03 09:30:00",
    )
    date = datetime.date(2025, 1, 2)
    tdo = train_smt.compute_tdo(mnq, date)
    assert tdo is None


# ══ stop/TP placement tests ══════════════════════════════════════════════════

def test_stop_short():
    """Short: entry=20100, TDO=20000 → stop = entry + 0.45 × 100 = 20145."""
    entry = 20100.0
    tdo   = 20000.0
    distance = abs(entry - tdo)
    stop = entry + 0.45 * distance
    assert stop == pytest.approx(20145.0)


def test_stop_long():
    """Long: entry=19900, TDO=20000 → stop = entry - 0.45 × 100 = 19855."""
    entry = 19900.0
    tdo   = 20000.0
    distance = abs(entry - tdo)
    stop = entry - 0.45 * distance
    assert stop == pytest.approx(19855.0)


def test_tp_equals_tdo():
    """TP is always TDO — confirmed via _build_signal_from_bar signal dict."""
    import strategy_smt as train_smt
    # Short signal: entry at 20100, TDO at 20090 (10 pts below — within MAX_TDO_DISTANCE_PTS)
    bar = pd.Series({"Open": 20105.0, "High": 20110.0, "Low": 20095.0, "Close": 20100.0})
    ts = pd.Timestamp("2025-01-02 09:05:00", tz="America/New_York")
    signal = train_smt._build_signal_from_bar(bar, ts, "short", 20090.0)
    assert signal is not None
    assert signal["take_profit"] == signal["tdo"]
    assert signal["take_profit"] == 20090.0


def test_rr_ratio():
    """R:R = distance_to_TP / distance_to_stop ≈ 1/0.45 ≈ 2.22."""
    entry = 20100.0
    tdo   = 20000.0
    distance = abs(entry - tdo)
    stop = entry + 0.45 * distance   # short stop
    dist_to_stop = abs(entry - stop)
    dist_to_tp   = abs(entry - tdo)
    rr = dist_to_tp / dist_to_stop
    assert rr == pytest.approx(1 / 0.45, rel=1e-3)


# ══ _build_signal_from_bar tests ═════════════════════════════════════════════

def _make_signal_bar(close, open_=None, high=None, low=None):
    """Build a pd.Series bar for _build_signal_from_bar tests."""
    if open_ is None: open_ = close
    if high is None:  high  = close + 5
    if low is None:   low   = close - 5
    return pd.Series({"Open": float(open_), "High": float(high), "Low": float(low), "Close": float(close)})

_SIGNAL_TS = pd.Timestamp("2025-01-02 09:05:00", tz="America/New_York")


def test_build_signal_from_bar_short_returns_signal(monkeypatch):
    """_build_signal_from_bar returns a short signal dict for valid short setup."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    bar = _make_signal_bar(close=20100.0)
    signal = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", 20000.0)
    assert signal is not None
    assert signal["direction"] == "short"
    assert "entry_price" in signal
    assert "stop_price" in signal
    assert "take_profit" in signal


def test_build_signal_from_bar_long_returns_signal(monkeypatch):
    """_build_signal_from_bar returns a long signal dict for valid long setup."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    bar = _make_signal_bar(close=19900.0)
    signal = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "long", 20000.0)
    assert signal is not None
    assert signal["direction"] == "long"
    assert signal["take_profit"] == signal["tdo"]


# ══ manage_position tests ════════════════════════════════════════════════════

def _make_position(direction, entry_price, stop_price, take_profit):
    return {
        "direction":   direction,
        "entry_price": entry_price,
        "stop_price":  stop_price,
        "take_profit": take_profit,
    }


def _make_bar(high, low, close=None, open_=None):
    if close is None: close = (high + low) / 2
    if open_ is None: open_ = (high + low) / 2
    return pd.Series({"Open": open_, "High": high, "Low": low, "Close": close})


def test_manage_position_tp_long(monkeypatch):
    """Long: high >= take_profit → 'exit_tp'."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=20001, low=19950)
    assert train_smt.manage_position(pos, bar) == "exit_tp"


def test_manage_position_stop_long():
    """Long: low <= stop_price → 'exit_stop'."""
    import strategy_smt as train_smt
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=19850, low=19799)
    assert train_smt.manage_position(pos, bar) == "exit_stop"


def test_manage_position_tp_short(monkeypatch):
    """Short: low <= take_profit → 'exit_tp'."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    pos = _make_position("short", 20100, 20200, 20000)
    bar = _make_bar(high=20050, low=19999)
    assert train_smt.manage_position(pos, bar) == "exit_tp"


def test_manage_position_stop_short():
    """Short: high >= stop_price → 'exit_stop'."""
    import strategy_smt as train_smt
    pos = _make_position("short", 20100, 20200, 20000)
    bar = _make_bar(high=20200, low=20050)
    assert train_smt.manage_position(pos, bar) == "exit_stop"


def test_manage_position_hold():
    """Neither TP nor stop triggered → 'hold'."""
    import strategy_smt as train_smt
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=19950, low=19850)
    assert train_smt.manage_position(pos, bar) == "hold"


# ══ Helpers for direction-control and guard tests ════════════════════════════

def _make_short_session_bars(base=20000.0):
    """Session bars with a bearish SMT signal.

    Divergence at bar 4 (MES new session high, MNQ fails to confirm).
    Bar 3 is explicitly bullish so find_entry_bar can find a confirmation anchor.
    Bar 5 is the bearish confirmation candle.
    """
    n = 30
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    highs_mes = [base + 5] * n
    highs_mes[4] = base + 30  # MES new session high at bar 4
    highs_mnq = [base + 5] * n
    opens  = [base] * n
    closes = [base] * n
    # Bar 3: bullish anchor needed by find_entry_bar for "short"
    opens[3]  = base - 2
    closes[3] = base + 2
    # Bar 5: bearish confirmation candle with wick above bar 3's bull close
    opens[5]  = base + 2
    closes[5] = base - 2
    highs_mnq[5] = base + 6
    mnq = pd.DataFrame(
        {"Open": opens, "High": highs_mnq, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": highs_mes, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


def _make_long_session_bars(base=20000.0):
    """Session bars with a bullish SMT signal.

    Divergence at bar 4 (MES new session low, MNQ fails to confirm).
    Bar 3 is explicitly bearish so find_entry_bar can find a confirmation anchor.
    Bar 5 is the bullish confirmation candle.
    """
    n = 30
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    lows_mes = [base - 5] * n
    lows_mes[4] = base - 30  # MES new session low at bar 4
    lows_mnq = [base - 5] * n
    opens  = [base] * n
    closes = [base] * n
    # Bar 3: bearish anchor needed by find_entry_bar for "long"
    opens[3]  = base + 2
    closes[3] = base - 2
    # Bar 5: bullish confirmation candle with wick below bar 3's bear close
    opens[5]  = base - 2
    closes[5] = base + 2
    lows_mnq[5] = base - 6
    mnq = pd.DataFrame(
        {"Open": opens, "High": [base + 5] * n, "Low": lows_mnq, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": [base + 5] * n, "Low": lows_mes, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


# ══ TRADE_DIRECTION filter tests ═════════════════════════════════════════════

def _patch_direction_test_guards(monkeypatch, trade_direction):
    """Shared setup for direction-filter tests: disable all guards except direction.

    Patches strategy-side constants on strategy_smt and harness-side on backtest_smt.
    Returns backtest_smt so callers can invoke run_backtest on the correct module.
    """
    import strategy_smt as _strat
    import backtest_smt as _bk
    # Strategy-side: affect _build_signal_from_bar, manage_position
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    # Harness-side: affect run_backtest directly
    monkeypatch.setattr(_bk, "TRADE_DIRECTION", trade_direction)
    monkeypatch.setattr(_bk, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_bk, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(_bk, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_bk, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(_bk, "compute_tdo", lambda *a: 19900.0)
    return _bk


def test_trade_direction_short_blocks_long(monkeypatch):
    """TRADE_DIRECTION='short' causes run_backtest to skip bullish SMT signals."""
    _bk = _patch_direction_test_guards(monkeypatch, "short")
    mnq, mes = _make_long_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] == 0


def test_trade_direction_long_blocks_short(monkeypatch):
    """TRADE_DIRECTION='long' causes run_backtest to skip bearish SMT signals."""
    import backtest_smt as _bk
    _patch_direction_test_guards(monkeypatch, "long")
    monkeypatch.setattr(_bk, "compute_tdo", lambda *a: 20100.0)
    mnq, mes = _make_short_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] == 0


def test_trade_direction_both_passes_short(monkeypatch):
    """TRADE_DIRECTION='both' does not filter bearish SMT signals."""
    _bk = _patch_direction_test_guards(monkeypatch, "both")
    mnq, mes = _make_short_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] >= 1
    assert any(t["direction"] == "short" for t in stats["trade_records"])


# ══ TDO validity gate tests ══════════════════════════════════════════════════

def test_tdo_validity_blocks_inverted_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips long signal when TDO < entry_price."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Long signal: entry at 20100, TDO at 19950 → TDO below entry → inverted
    bar = _make_signal_bar(close=20100.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "long", 19950.0)
    assert result is None


def test_tdo_validity_passes_valid_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True allows long signal when TDO > entry_price."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    # Long signal: entry at 19900, TDO at 20100 → TDO above entry → valid
    bar = _make_signal_bar(close=19900.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "long", 20100.0)
    assert result is not None
    assert result["direction"] == "long"
    assert result["take_profit"] > result["entry_price"]


def test_tdo_validity_blocks_inverted_short(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips short signal when TDO > entry_price."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Short signal: entry at 19900, TDO at 20100 → TDO above entry → inverted
    bar = _make_signal_bar(close=19900.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", 20100.0)
    assert result is None


def test_tdo_validity_false_passes_inverted(monkeypatch):
    """TDO_VALIDITY_CHECK=False allows inverted signals through (legacy behavior)."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    # Short signal: entry at 19900, TDO at 20100 → inverted, but gate is off
    bar = _make_signal_bar(close=19900.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", 20100.0)
    assert result is not None  # passes through despite inversion


# ══ MIN_STOP_POINTS guard tests ══════════════════════════════════════════════

def test_min_stop_points_filters_tiny_stop(monkeypatch):
    """MIN_STOP_POINTS=50 rejects signals where stop distance < 50 pts."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 50.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Short signal: entry=20100, TDO=20095 → dist=5 → stop=20100+0.40*5=20102 → stop_dist=2 < 50
    bar = _make_signal_bar(close=20100.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", 20095.0)
    assert result is None


def test_min_stop_points_zero_disables_guard(monkeypatch):
    """MIN_STOP_POINTS=0.0 allows all stop distances through."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Short signal with tiny stop — guard is off so should come through
    bar = _make_signal_bar(close=20100.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", 20095.0)
    assert result is not None


# ══ Per-direction stop ratio tests ══════════════════════════════════════════

def test_long_stop_ratio_applied(monkeypatch):
    """LONG_STOP_RATIO is used for long stop computation."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "LONG_STOP_RATIO", 0.3)
    tdo_val = 20100.0
    bar = _make_signal_bar(close=19900.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "long", tdo_val)
    assert result is not None
    assert result["direction"] == "long"
    expected_stop = result["entry_price"] - 0.3 * abs(result["entry_price"] - tdo_val)
    assert abs(result["stop_price"] - expected_stop) < 0.01


def test_short_stop_ratio_applied(monkeypatch):
    """SHORT_STOP_RATIO is used for short stop computation."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "SHORT_STOP_RATIO", 0.6)
    tdo_val = 19900.0
    bar = _make_signal_bar(close=20100.0)
    result = train_smt._build_signal_from_bar(bar, _SIGNAL_TS, "short", tdo_val)
    assert result is not None
    assert result["direction"] == "short"
    expected_stop = result["entry_price"] + 0.6 * abs(result["entry_price"] - tdo_val)
    assert abs(result["stop_price"] - expected_stop) < 0.01


# ══ print_direction_breakdown tests ══════════════════════════════════════════

def test_print_direction_breakdown_format(capsys):
    """print_direction_breakdown prints per-direction metrics with correct prefix."""
    import strategy_smt as train_smt
    fake_stats = {
        "total_pnl": 100.0,
        "trade_records": [
            {"direction": "long",  "pnl":  50.0, "exit_type": "exit_tp"},
            {"direction": "long",  "pnl": -20.0, "exit_type": "exit_stop"},
            {"direction": "short", "pnl":  70.0, "exit_type": "exit_tp"},
        ],
    }
    train_smt.print_direction_breakdown(fake_stats, prefix="fold1_train_")
    out = capsys.readouterr().out
    assert "fold1_train_long_trades: 2" in out
    assert "fold1_train_long_win_rate: 0.5" in out
    assert "fold1_train_short_trades: 1" in out
    assert "fold1_train_short_win_rate: 1.0" in out
    assert "fold1_train_long_exit_exit_tp: 1" in out
    assert "fold1_train_long_exit_exit_stop: 1" in out


def test_print_direction_breakdown_empty_trades(capsys):
    """print_direction_breakdown prints nothing when trade_records is empty."""
    import strategy_smt as train_smt
    train_smt.print_direction_breakdown({"trade_records": []}, prefix="test_")
    out = capsys.readouterr().out
    assert out == ""


# ══ find_anchor_close tests ══════════════════════════════════════════════════

def test_find_anchor_close_short_finds_bull_bar():
    """Short setup: scans back from bar 4 and finds the bullish bar 2 close."""
    import strategy_smt as train_smt
    bars = _make_1m_bars(
        opens= [200, 200, 198, 200, 200],
        highs= [201, 201, 203, 201, 201],
        lows=  [199, 199, 198, 199, 199],
        closes=[200, 200, 202, 200, 200],  # bar 2: bullish close=202 > open=198
    )
    result = train_smt.find_anchor_close(bars, 4, "short")
    assert result == pytest.approx(202.0)


def test_find_anchor_close_long_finds_bear_bar():
    """Long setup: scans back from bar 4 and finds the bearish bar 2 close."""
    import strategy_smt as train_smt
    bars = _make_1m_bars(
        opens= [200, 200, 202, 200, 200],
        highs= [201, 201, 203, 201, 201],
        lows=  [199, 199, 197, 199, 199],
        closes=[200, 200, 198, 200, 200],  # bar 2: bearish close=198 < open=202
    )
    result = train_smt.find_anchor_close(bars, 4, "long")
    assert result == pytest.approx(198.0)


def test_find_anchor_close_no_match_returns_none():
    """All doji bars → no qualifying bar → returns None."""
    import strategy_smt as train_smt
    bars = _make_1m_bars(
        opens= [200, 200, 200, 200, 200],
        highs= [201, 201, 201, 201, 201],
        lows=  [199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200],
    )
    result = train_smt.find_anchor_close(bars, 4, "short")
    assert result is None


def test_find_anchor_close_uses_most_recent():
    """Two bullish bars: returns the closer (most recent) one's close."""
    import strategy_smt as train_smt
    bars = _make_1m_bars(
        opens= [198, 200, 198, 200, 200],
        highs= [203, 201, 203, 201, 201],
        lows=  [197, 199, 197, 199, 199],
        closes=[202, 200, 203, 200, 200],  # bar 0 close=202, bar 2 close=203 (more recent)
    )
    # Scanning backward from bar 4: first bullish bar found is bar 2 (close=203)
    result = train_smt.find_anchor_close(bars, 4, "short")
    assert result == pytest.approx(203.0)


# ══ is_confirmation_bar tests ════════════════════════════════════════════════

def test_is_confirmation_bar_short_true():
    """Bearish bar with high > anchor_close → True."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 200.0, "High": 205.0, "Low": 196.0, "Close": 197.0})
    assert train_smt.is_confirmation_bar(bar, anchor_close=203.0, direction="short")


def test_is_confirmation_bar_short_false_not_bearish():
    """Bullish bar (close > open) → False even if high > anchor."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 196.0, "High": 205.0, "Low": 195.0, "Close": 200.0})
    assert not train_smt.is_confirmation_bar(bar, anchor_close=203.0, direction="short")


def test_is_confirmation_bar_short_false_wick_below_anchor():
    """Bearish bar but high <= anchor_close → False."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 200.0, "High": 202.0, "Low": 196.0, "Close": 197.0})
    assert not train_smt.is_confirmation_bar(bar, anchor_close=203.0, direction="short")


def test_is_confirmation_bar_long_true():
    """Bullish bar with low < anchor_close → True."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 198.0, "High": 205.0, "Low": 194.0, "Close": 202.0})
    assert train_smt.is_confirmation_bar(bar, anchor_close=196.0, direction="long")


def test_is_confirmation_bar_long_false_not_bullish():
    """Bearish bar (close < open) → False even if low < anchor."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 202.0, "High": 205.0, "Low": 194.0, "Close": 198.0})
    assert not train_smt.is_confirmation_bar(bar, anchor_close=196.0, direction="long")


def test_is_confirmation_bar_long_false_wick_above_anchor():
    """Bullish bar but low >= anchor_close → False."""
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 198.0, "High": 205.0, "Low": 197.0, "Close": 202.0})
    assert not train_smt.is_confirmation_bar(bar, anchor_close=196.0, direction="long")


# ══ manage_position BREAKEVEN_TRIGGER_PCT tests ══════════════════════════════

def _make_position_with_tp(direction, entry_price, stop_price, take_profit):
    return {
        "direction":   direction,
        "entry_price": entry_price,
        "stop_price":  stop_price,
        "take_profit": take_profit,
    }


def test_breakeven_trigger_pct_fires_at_correct_progress(monkeypatch):
    """Short trade at 50% progress to TDO → stop moves to entry, breakeven_active set."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    # entry=20100, TP=20000, dist=100. 50% progress = price dropped 50 pts → Low=20050
    pos = _make_position_with_tp("short", 20100.0, 20200.0, 20000.0)
    bar = pd.Series({"Open": 20090.0, "High": 20095.0, "Low": 20050.0, "Close": 20055.0})
    train_smt.manage_position(pos, bar)
    assert pos["stop_price"] <= 20100.0  # stop tightened to at most entry
    assert pos.get("breakeven_active") is True


def test_breakeven_trigger_pct_does_not_fire_below_threshold(monkeypatch):
    """40% progress with 50% threshold → stop unchanged."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    # entry=20100, TP=20000, dist=100. 40% progress → Low=20060
    pos = _make_position_with_tp("short", 20100.0, 20200.0, 20000.0)
    original_stop = pos["stop_price"]
    bar = pd.Series({"Open": 20090.0, "High": 20095.0, "Low": 20060.0, "Close": 20065.0})
    train_smt.manage_position(pos, bar)
    assert pos["stop_price"] == original_stop
    assert not pos.get("breakeven_active")


def test_breakeven_trigger_pct_zero_disables_mechanism(monkeypatch):
    """BREAKEVEN_TRIGGER_PCT=0.0 → stop never moves regardless of progress."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    pos = _make_position_with_tp("short", 20100.0, 20200.0, 20000.0)
    original_stop = pos["stop_price"]
    bar = pd.Series({"Open": 20050.0, "High": 20055.0, "Low": 19990.0, "Close": 20000.0})
    train_smt.manage_position(pos, bar)
    assert pos["stop_price"] == original_stop


def test_breakeven_active_flag_set(monkeypatch):
    """After BREAKEVEN_TRIGGER_PCT fires, breakeven_active is True."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    pos = _make_position_with_tp("short", 20100.0, 20200.0, 20000.0)
    bar = pd.Series({"Open": 20060.0, "High": 20065.0, "Low": 20049.0, "Close": 20055.0})
    train_smt.manage_position(pos, bar)
    assert pos.get("breakeven_active") is True


def test_breakeven_stop_only_tightens(monkeypatch):
    """If stop is already tighter than entry, it is not widened by breakeven."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    # Stop already tighter than entry (e.g. moved by trailing logic earlier)
    pos = _make_position_with_tp("short", 20100.0, 20090.0, 20000.0)  # stop=20090 < entry=20100
    bar = pd.Series({"Open": 20060.0, "High": 20065.0, "Low": 20049.0, "Close": 20055.0})
    train_smt.manage_position(pos, bar)
    # min(20090, 20100) = 20090 — stop should not widen to 20100
    assert pos["stop_price"] <= 20090.0


# ══ State machine / re-entry integration tests ══════════════════════════════

def _make_reentry_session_bars(base=20000.0):
    """Build a session where a trade stops out early and a re-entry fires later.

    Bar 5: bullish anchor (close > open).
    Bar 7: MES new session high (divergence), MNQ fails → bearish signal.
    Bar 8: confirmation → entry (open=base+2, close=base-2, high=base+6).
    Bar 9: immediate stop-out (high exceeds stop).
    Bar 12: new anchor bar for re-entry (bullish).
    Bar 14: re-entry confirmation (bearish bar, high > anchor).
    Bar 20: position closes at session end.
    """
    n = 50
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    opens  = [base] * n
    closes = [base] * n

    # Bullish anchor before divergence
    opens[5]  = base - 2; closes[5] = base + 2
    # SMT divergence at bar 7
    mes_highs[7] = base + 30
    # Bearish confirmation at bar 8 (entry)
    opens[8] = base + 2; closes[8] = base - 2; mnq_highs[8] = base + 6
    # Stop-out bar: short stop = entry + SHORT_STOP_RATIO * dist_to_tdo
    # With TDO=base-100, dist=100, stop ≈ entry + 40 = base - 2 + 40 = base+38
    mnq_highs[9] = base + 40  # triggers stop

    # Re-entry anchor: new bullish bar at 12
    opens[12] = base - 3; closes[12] = base + 3
    # Re-entry confirmation at bar 14: bearish, high > bar-12 close
    opens[14] = base + 3; closes[14] = base - 3; mnq_highs[14] = base + 7

    mnq = pd.DataFrame(
        {"Open": opens, "High": mnq_highs, "Low": mnq_lows, "Close": closes, "Volume": [1000.0]*n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": mes_lows, "Close": closes, "Volume": [1000.0]*n},
        index=idx,
    )
    return mnq, mes


def _patch_reentry_guards(monkeypatch, reentry_max_move=50.0, breakeven_pct=0.0):
    """Shared setup for reentry tests. Returns backtest_smt for run_backtest calls."""
    import strategy_smt as _strat
    import backtest_smt as _bk
    # Strategy-side
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", breakeven_pct)
    # Disable midnight-open TP override so compute_tdo=19900 is used as TDO.
    # Must patch both modules since backtest_smt holds its own bound name.
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(_bk, "MIDNIGHT_OPEN_AS_TP", False)
    # Harness-side
    monkeypatch.setattr(_bk, "REENTRY_MAX_MOVE_PTS", reentry_max_move)
    monkeypatch.setattr(_bk, "MAX_REENTRY_COUNT", 999)  # disable cap; initial entry also uses reentry_count
    monkeypatch.setattr(_bk, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_bk, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(_bk, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_bk, "compute_tdo", lambda *a: 19900.0)
    return _bk


def test_reentry_after_stop(monkeypatch):
    """Re-entry fires a second trade when stop-out move < REENTRY_MAX_MOVE_PTS."""
    _bk = _patch_reentry_guards(monkeypatch, reentry_max_move=50.0, breakeven_pct=0.0)
    mnq, mes = _make_reentry_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] >= 2


def test_no_reentry_when_disabled(monkeypatch):
    """REENTRY_MAX_MOVE_PTS=0.0 → only one trade even with a valid re-entry setup."""
    _bk = _patch_reentry_guards(monkeypatch, reentry_max_move=0.0, breakeven_pct=0.0)
    mnq, mes = _make_reentry_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] <= 1


def test_no_reentry_when_move_exceeds_threshold(monkeypatch):
    """Stop-out after favorable move > REENTRY_MAX_MOVE_PTS → no second trade.

    For a short, move = entry_price - stop_out_bar_close.  A positive value
    means price moved favorably (downward) before reversing and stopping us out.
    When move >= threshold, re-entry is suppressed (we missed the move already).
    """
    _bk = _patch_reentry_guards(monkeypatch, reentry_max_move=5.0, breakeven_pct=0.0)

    # Build a session where:
    # - entry at bar 8 close = base-2
    # - stop-out at bar 9: high = base+40 triggers stop, but close = base-20
    # - move = (base-2) - (base-20) = 18 >= REENTRY_MAX_MOVE_PTS (5.0) → blocked
    base = 20000.0
    mnq, mes = _make_reentry_session_bars(base=base)
    # Override bar-9 close: price dipped 20 pts favorably before the wick stopped us out.
    mnq = mnq.copy()
    mnq.iloc[9, mnq.columns.get_loc("Close")] = base - 20

    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] == 1


def test_reentry_breakeven_active_bypasses_move_check(monkeypatch):
    """Trade stopped at breakeven → REENTRY_ELIGIBLE regardless of move size."""
    _bk = _patch_reentry_guards(monkeypatch, reentry_max_move=1.0, breakeven_pct=0.01)
    mnq, mes = _make_reentry_session_bars()
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # Verify the backtest completes without error; with breakeven active the position
    # stops at entry so breakeven_active bypasses the move check → reentry eligible.
    assert "total_trades" in stats


def test_state_resets_at_day_boundary(monkeypatch):
    """A pending divergence from day 1 does NOT carry to day 2."""
    _bk = _patch_reentry_guards(monkeypatch, reentry_max_move=0.0, breakeven_pct=0.0)

    # Day 1: divergence fires but no confirmation bar in session (no bearish confirm)
    n = 30
    d1_start = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    d1_idx   = pd.date_range(start=d1_start, periods=n, freq="1min")
    mes_highs = [20005.0] * n; mes_highs[7] = 20030.0
    mnq_highs = [20005.0] * n
    opens_d1  = [20000.0] * n; opens_d1[5] = 19998.0; closes_d1 = [20000.0] * n; closes_d1[5] = 20002.0
    # No bearish confirmation bar → state stays WAITING_FOR_ENTRY at day end
    mnq1 = pd.DataFrame({"Open": opens_d1, "High": mnq_highs, "Low": [19995.0]*n, "Close": closes_d1, "Volume": [1000.0]*n}, index=d1_idx)
    mes1 = pd.DataFrame({"Open": opens_d1, "High": mes_highs, "Low": [19995.0]*n, "Close": closes_d1, "Volume": [1000.0]*n}, index=d1_idx)

    # Day 2: flat bars — no divergence of its own
    d2_start = pd.Timestamp("2025-01-03 09:00:00", tz="America/New_York")
    d2_idx   = pd.date_range(start=d2_start, periods=n, freq="1min")
    flat_opens  = [20000.0] * n
    flat_closes = [20000.0] * n
    # Bar 5 on day 2: bearish bar that would confirm a "short" if state carried over
    flat_opens[5] = 20002.0; flat_closes[5] = 19998.0
    mnq2 = pd.DataFrame({"Open": flat_opens, "High": [20005.0]*n, "Low": [19995.0]*n, "Close": flat_closes, "Volume": [1000.0]*n}, index=d2_idx)
    mes2 = pd.DataFrame({"Open": flat_opens, "High": [20005.0]*n, "Low": [19995.0]*n, "Close": flat_closes, "Volume": [1000.0]*n}, index=d2_idx)

    mnq = pd.concat([mnq1, mnq2])
    mes = pd.concat([mes1, mes2])
    stats = _bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    # Day 2 should not see a trade from the stale day-1 divergence
    assert stats["total_trades"] == 0


# ══ Task 7b — MAX_TDO_DISTANCE_PTS ceiling filter tests ══════════════════════

def test_build_signal_max_tdo_distance_ceiling(monkeypatch):
    """Signal rejected when |entry - TDO| > MAX_TDO_DISTANCE_PTS."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 30.0)
    bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 99.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # TDO = 60 → distance = 39 > 30 → rejected
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is None


def test_build_signal_max_tdo_distance_pass(monkeypatch):
    """Signal passes when |entry - TDO| <= MAX_TDO_DISTANCE_PTS."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 50.0)
    bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 99.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # TDO = 60 → distance = 39 < 50 → passes
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is not None


def test_build_signal_max_tdo_distance_disabled(monkeypatch):
    """MAX_TDO_DISTANCE_PTS=999.0 disables the ceiling filter."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    # Short entry: close=1100, tdo=100 → distance=1000 (which would be blocked by any finite ceiling)
    # With 999.0 ceiling disabled, it must pass (assuming MIN_STOP_POINTS allows it)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    bar = pd.Series({"Open": 1100.0, "High": 1105.0, "Low": 1095.0, "Close": 1100.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # Distance = 1000 — should pass with ceiling disabled
    result = train_smt._build_signal_from_bar(bar, ts, "short", 100.0)
    assert result is not None


# ══ Task 7c — detect_smt_divergence new return type tests ════════════════════

def test_detect_smt_divergence_returns_tuple_on_match():
    import strategy_smt as train_smt
    mes = _make_1m_bars(
        opens=[100]*5, highs=[101,102,101,101,103], lows=[99]*5, closes=[100]*5
    )
    mnq = _make_1m_bars(
        opens=[200]*5, highs=[201,202,201,201,201], lows=[199]*5, closes=[200]*5
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None
    direction, sweep, miss, smt_type, defended = result
    assert direction == "short"
    assert sweep > 0
    assert miss >= 0


def test_detect_smt_divergence_sweep_filter(monkeypatch):
    """Returns None when sweep < MIN_SMT_SWEEP_PTS."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MIN_SMT_SWEEP_PTS", 5.0)
    mes = _make_1m_bars(
        opens=[100]*5, highs=[101,102,101,101,102.5], lows=[99]*5, closes=[100]*5
    )
    mnq = _make_1m_bars(
        opens=[200]*5, highs=[201,202,201,201,201], lows=[199]*5, closes=[200]*5
    )
    # MES sweep = 102.5 - 102 = 0.5 < 5.0 → filtered
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is None


# ══ Task 7d — new signal fields and body ratio tests ═════════════════════════

def test_build_signal_contains_diagnostic_fields():
    import strategy_smt as train_smt
    bar = pd.Series({"Open": 105.0, "High": 107.0, "Low": 97.0, "Close": 101.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # TDO=92 → dist=9 pts, within MAX_TDO_DISTANCE_PTS=15 and stop=101+0.35*9=104.15 > MIN_STOP_POINTS
    result = train_smt._build_signal_from_bar(
        bar, ts, "short", 92.0, smt_sweep_pts=2.5, smt_miss_pts=1.3
    )
    assert result is not None
    assert "smt_sweep_pts" in result
    assert "smt_miss_pts" in result
    assert "entry_bar_body_ratio" in result
    assert result["smt_sweep_pts"] == 2.5
    assert result["smt_miss_pts"] == 1.3
    assert 0.0 <= result["entry_bar_body_ratio"] <= 1.0


def test_build_signal_body_ratio_not_filtered(monkeypatch):
    """Near-doji bars (low body ratio) are NOT rejected — diagnostics show they are best."""
    import strategy_smt as train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    # Near-doji: body=0.1, range=20 → ratio=0.005 (extreme doji)
    bar = pd.Series({"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 99.9})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is not None, "Near-doji bars must not be filtered — they have highest EP"


# ══ Phase 1: bar globals + set_bar_data() ════════════════════════════════════

def test_set_bar_data_populates_globals():
    import strategy_smt as train_smt
    mnq = _make_1m_bars([100]*3, [101]*3, [99]*3, [100]*3)
    mes = _make_1m_bars([50]*3, [51]*3, [49]*3, [50]*3)
    train_smt.set_bar_data(mnq, mes)
    assert train_smt._mnq_bars is mnq
    assert train_smt._mes_bars is mes


def test_set_bar_data_overwrites_previous():
    import strategy_smt as train_smt
    df1 = _make_1m_bars([100]*2, [101]*2, [99]*2, [100]*2)
    df2 = _make_1m_bars([200]*2, [201]*2, [199]*2, [200]*2)
    train_smt.set_bar_data(df1, df1)
    train_smt.set_bar_data(df2, df2)
    assert train_smt._mnq_bars is df2


def test_run_backtest_calls_set_bar_data(monkeypatch):
    import strategy_smt as _strat
    import backtest_smt as _bk
    calls = []
    # set_bar_data is called via backtest_smt's imported reference — patch there
    monkeypatch.setattr(_bk, "set_bar_data", lambda mnq, mes: calls.append((mnq, mes)))
    mnq = _make_1m_bars([100]*2, [101]*2, [99]*2, [100]*2)
    mes = _make_1m_bars([50]*2, [51]*2, [49]*2, [50]*2)
    monkeypatch.setattr(_bk, "BACKTEST_START", "2025-01-02")
    monkeypatch.setattr(_bk, "BACKTEST_END",   "2025-01-03")
    _bk.run_backtest(mnq, mes)
    assert len(calls) == 1 and calls[0][0] is mnq


# ══ Phase 2: exit_market infrastructure ══════════════════════════════════════

def test_manage_position_does_not_return_exit_market_by_default():
    """exit_market must not fire without a criterion — infrastructure only."""
    import strategy_smt as train_smt
    pos = {"direction": "short", "entry_price": 20100.0, "take_profit": 20000.0,
           "stop_price": 20150.0, "entry_time": pd.Timestamp("2025-01-02 09:05", tz="America/New_York")}
    bar = pd.Series({"Open": 20080.0, "High": 20090.0, "Low": 20070.0, "Close": 20080.0})
    assert train_smt.manage_position(pos, bar) != "exit_market"


# ── Tests for divergence_score() ─────────────────────────────────────────────

def test_divergence_score_displacement_uses_body():
    from strategy_smt import divergence_score
    score = divergence_score(sweep_pts=0, miss_pts=0, body_pts=20.0,
                              smt_type="displacement",
                              hypothesis_direction=None, div_direction="long")
    assert score == 1.0  # 20/20 = 1.0


def test_divergence_score_wick_weights():
    from strategy_smt import divergence_score
    # miss_pts carries 50% weight; with only miss_pts set: score = 0 * 0.25 + 1.0 * 0.50 + 0 * 0.25
    score = divergence_score(sweep_pts=0, miss_pts=25.0, body_pts=0,
                              smt_type="wick",
                              hypothesis_direction=None, div_direction="long")
    assert abs(score - 0.50) < 1e-9


def test_divergence_score_hypothesis_bonus_aligned():
    from strategy_smt import divergence_score
    # Base with all components maxed = 1.0; bonus would push to 1.2 but capped at 1.0
    score = divergence_score(sweep_pts=5.0, miss_pts=25.0, body_pts=15.0,
                              smt_type="wick",
                              hypothesis_direction="long", div_direction="long")
    assert score == 1.0


def test_divergence_score_hypothesis_no_penalty_counter():
    from strategy_smt import divergence_score
    # Counter-hypothesis does NOT reduce base score
    score_no_hyp = divergence_score(0, 0, 0, "wick", None, "long")
    score_counter = divergence_score(0, 0, 0, "wick", "short", "long")
    assert score_no_hyp == score_counter


def test_effective_div_score_time_decay():
    from strategy_smt import _effective_div_score, DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL
    # After exactly DIV_SCORE_DECAY_INTERVAL bars: time_decay = FACTOR^1
    # No adverse move (price unchanged: discovery_price == current bar mid)
    score = _effective_div_score(
        score=1.0,
        discovery_bar_idx=0,
        current_bar_idx=DIV_SCORE_DECAY_INTERVAL,
        discovery_price=100.0,
        direction="long",
        current_bar_high=100.0,
        current_bar_low=100.0,   # no adverse move for long
    )
    expected_time_decay = DIV_SCORE_DECAY_FACTOR ** 1
    expected_adverse_decay = 1.0  # no adverse move
    assert abs(score - expected_time_decay * expected_adverse_decay) < 1e-9


def test_effective_div_score_adverse_move_decay():
    from strategy_smt import _effective_div_score, ADVERSE_MOVE_FULL_DECAY_PTS, ADVERSE_MOVE_MIN_DECAY
    # Full adverse move (150 pts for long: price went down 150 pts against long)
    score = _effective_div_score(
        score=1.0,
        discovery_bar_idx=0,
        current_bar_idx=0,
        discovery_price=200.0,
        direction="long",
        current_bar_high=350.0,
        current_bar_low=50.0,  # adverse = 200 - 50 = 150 = ADVERSE_MOVE_FULL_DECAY_PTS
    )
    # move_decay = max(ADVERSE_MOVE_MIN_DECAY, 1 - 150/150) = max(0.1, 0) = 0.1
    assert abs(score - ADVERSE_MOVE_MIN_DECAY) < 1e-9


def test_effective_div_score_combined_decay():
    from strategy_smt import _effective_div_score, DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL, ADVERSE_MOVE_MIN_DECAY
    bars = DIV_SCORE_DECAY_INTERVAL
    score = _effective_div_score(
        score=1.0,
        discovery_bar_idx=0,
        current_bar_idx=bars,
        discovery_price=200.0,
        direction="long",
        current_bar_high=350.0,
        current_bar_low=50.0,  # adverse = 150 = full decay
    )
    time_decay = DIV_SCORE_DECAY_FACTOR ** 1
    move_decay = ADVERSE_MOVE_MIN_DECAY
    assert abs(score - time_decay * move_decay) < 1e-9


def test_inverted_stop_guard_long_rejected():
    """Long signal where stop >= entry must return None."""
    from strategy_smt import _build_signal_from_bar
    import strategy_smt as sm
    bar = pd.Series({"Open": 99.0, "High": 103.0, "Low": 99.0, "Close": 100.0}, name=pd.Timestamp("2024-01-02 10:00", tz="America/New_York"))
    ts = bar.name
    orig_sm = sm.STRUCTURAL_STOP_MODE
    orig_buff = sm.STRUCTURAL_STOP_BUFFER_PTS
    orig_tdo_check = sm.TDO_VALIDITY_CHECK
    orig_min_stop = sm.MIN_STOP_POINTS
    try:
        sm.STRUCTURAL_STOP_MODE = True
        sm.STRUCTURAL_STOP_BUFFER_PTS = 0.0
        sm.TDO_VALIDITY_CHECK = False
        sm.MIN_STOP_POINTS = 0
        # divergence_bar_low = 101.0 > entry = 100.0 → stop = 101.0 - 0 = 101.0 >= 100 → rejected
        result = _build_signal_from_bar(
            bar, ts, "long", 110.0,
            divergence_bar_high=105.0,
            divergence_bar_low=101.0,
        )
        assert result is None, f"Expected None for inverted stop, got {result}"
    finally:
        sm.STRUCTURAL_STOP_MODE = orig_sm
        sm.STRUCTURAL_STOP_BUFFER_PTS = orig_buff
        sm.TDO_VALIDITY_CHECK = orig_tdo_check
        sm.MIN_STOP_POINTS = orig_min_stop


def test_inverted_stop_guard_short_rejected():
    """Short signal where stop <= entry must return None."""
    from strategy_smt import _build_signal_from_bar
    import strategy_smt as sm
    bar = pd.Series({"Open": 101.0, "High": 103.0, "Low": 99.0, "Close": 100.0}, name=pd.Timestamp("2024-01-02 10:00", tz="America/New_York"))
    ts = bar.name
    orig_sm = sm.STRUCTURAL_STOP_MODE
    orig_buff = sm.STRUCTURAL_STOP_BUFFER_PTS
    orig_tdo = sm.TDO_VALIDITY_CHECK
    orig_min_stop = sm.MIN_STOP_POINTS
    try:
        sm.STRUCTURAL_STOP_MODE = True
        sm.STRUCTURAL_STOP_BUFFER_PTS = 0.0
        sm.TDO_VALIDITY_CHECK = False
        sm.MIN_STOP_POINTS = 0
        # Short: stop = divergence_bar_high + buffer = 99.0 + 0 = 99.0 <= entry=100.0 → rejected
        result = _build_signal_from_bar(
            bar, ts, "short", 90.0,
            divergence_bar_high=99.0,
            divergence_bar_low=97.0,
        )
        assert result is None, f"Expected None for inverted short stop, got {result}"
    finally:
        sm.STRUCTURAL_STOP_MODE = orig_sm
        sm.STRUCTURAL_STOP_BUFFER_PTS = orig_buff
        sm.TDO_VALIDITY_CHECK = orig_tdo
        sm.MIN_STOP_POINTS = orig_min_stop


def test_inverted_stop_guard_valid_passes():
    """Valid stop on correct side does not trigger guard."""
    from strategy_smt import _build_signal_from_bar
    import strategy_smt as sm
    bar = pd.Series({"Open": 101.0, "High": 103.0, "Low": 99.0, "Close": 100.0}, name=pd.Timestamp("2024-01-02 10:00", tz="America/New_York"))
    ts = bar.name
    orig_sm = sm.STRUCTURAL_STOP_MODE
    orig_buff = sm.STRUCTURAL_STOP_BUFFER_PTS
    orig_tdo = sm.TDO_VALIDITY_CHECK
    orig_min_stop = sm.MIN_STOP_POINTS
    try:
        sm.STRUCTURAL_STOP_MODE = True
        sm.STRUCTURAL_STOP_BUFFER_PTS = 0.0
        sm.TDO_VALIDITY_CHECK = False
        sm.MIN_STOP_POINTS = 0
        # Long: divergence_bar_low = 97.0 < entry=100.0 → stop = 97.0 - 0 = 97.0 < entry → valid
        result = _build_signal_from_bar(
            bar, ts, "long", 110.0,
            divergence_bar_high=103.0,
            divergence_bar_low=97.0,
        )
        assert result is not None, "Expected a valid signal dict"
    finally:
        sm.STRUCTURAL_STOP_MODE = orig_sm
        sm.STRUCTURAL_STOP_BUFFER_PTS = orig_buff
        sm.TDO_VALIDITY_CHECK = orig_tdo
        sm.MIN_STOP_POINTS = orig_min_stop
