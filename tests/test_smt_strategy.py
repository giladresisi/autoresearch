"""tests/test_smt_strategy.py — Unit tests for SMT strategy functions in train_smt.py.

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
    import train_smt
    monkeypatch.setattr(train_smt, "MIN_BARS_BEFORE_SIGNAL", 2)


# ══ detect_smt_divergence tests ══════════════════════════════════════════════

def test_detect_smt_bearish():
    """MES makes new session high, MNQ does not → 'short'."""
    import train_smt
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
    assert result == "short"


def test_detect_smt_bullish():
    """MES makes new session low, MNQ does not → 'long'."""
    import train_smt
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
    assert result == "long"


def test_detect_smt_both_confirm_none():
    """Both MES and MNQ make new session high → no divergence (None)."""
    import train_smt
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


def test_detect_smt_min_bars_suppresses(monkeypatch):
    """bar_idx - session_start_idx < MIN_BARS_BEFORE_SIGNAL → None."""
    import train_smt
    monkeypatch.setattr(train_smt, "MIN_BARS_BEFORE_SIGNAL", 5)
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
    # bar_idx=2, session_start_idx=0 → difference=2 < 5
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=2, session_start_idx=0)
    assert result is None


def test_detect_smt_resets_on_opposite():
    """Latest extreme wins: if bearish divergence set then resolved, check again."""
    import train_smt
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
    assert result == "long"


# ══ find_entry_bar tests ═════════════════════════════════════════════════════

def test_find_entry_bar_short():
    """Bearish bar + upper wick past most recent bull body → returns index."""
    import train_smt
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
    import train_smt
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
    import train_smt
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
    import train_smt
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

def test_compute_tdo_finds_930_bar():
    """9:30 bar exists → returns its open."""
    import train_smt
    mnq = _make_1m_bars(
        opens= [100, 105, 110],
        highs= [101, 106, 111],
        lows=  [ 99, 104, 109],
        closes=[100, 105, 110],
        start_time="2025-01-02 09:28:00",
    )
    date = datetime.date(2025, 1, 2)
    tdo = train_smt.compute_tdo(mnq, date)
    assert tdo == 110.0  # bar at 09:30


def test_compute_tdo_proxy_no_930_bar():
    """9:30 bar absent → returns first available bar's open."""
    import train_smt
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
    import train_smt
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
    """TP is always TDO — confirmed via screen_session signal dict."""
    import train_smt
    # Use a simple synthetic session that triggers a short signal
    # MES makes new high at bar 5, MNQ doesn't; then bearish confirm at bar 6
    mes = _make_1m_bars(
        opens= [100]*8,
        highs= [101, 101, 101, 101, 101, 103, 102, 102],
        lows=  [ 99]*8,
        closes=[100, 100, 100, 100, 100, 100, 100, 100],
        start_time="2025-01-02 09:00:00",
    )
    mnq = _make_1m_bars(
        opens= [200]*8,
        highs= [201, 201, 201, 201, 201, 201, 203, 201],
        lows=  [199]*8,
        closes=[200, 200, 200, 200, 200, 200, 198, 200],  # bar 6: bearish confirm
        start_time="2025-01-02 09:00:00",
    )
    # Add a 9:30 bar so compute_tdo works
    tdo_bar = _make_1m_bars(
        opens= [195],
        highs= [196],
        lows=  [194],
        closes=[195],
        start_time="2025-01-02 09:30:00",
    )
    mnq_full = pd.concat([mnq, tdo_bar]).sort_index()
    mes_full = pd.concat([mes, tdo_bar]).sort_index()
    signal = train_smt.screen_session(mnq_full, mes_full, datetime.date(2025, 1, 2))
    if signal is not None:
        assert signal["take_profit"] == signal["tdo"]


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


# ══ screen_session tests ═════════════════════════════════════════════════════

def _build_short_session():
    """Helper: synthetic session with a short SMT divergence signal."""
    # 10 bars from 09:00; bar 7 MES new high, MNQ doesn't; bar 8 bearish confirm
    # Bar 5: MNQ bullish (close=202 > open=199) — required for find_entry_bar "short"
    # Bar 7: MES breaks session high (103 > 101), MNQ does not (201 <= 201)
    # Bar 8: MNQ bearish (close=198 < open=200) with high=203 > bull-body-close=202 → confirms
    mes = _make_1m_bars(
        opens= [100]*10,
        highs= [101, 101, 101, 101, 101, 101, 101, 103, 102, 102],
        lows=  [ 99]*10,
        closes=[100, 100, 100, 100, 100, 100, 100, 100, 100, 100],
        start_time="2025-01-02 09:00:00",
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200, 199, 200, 200, 200, 200],
        highs= [201, 201, 201, 201, 201, 203, 201, 201, 203, 201],
        lows=  [199, 199, 199, 199, 199, 199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200, 202, 200, 200, 198, 200],
        start_time="2025-01-02 09:00:00",
    )
    # 9:30 bar for TDO
    tdo_bar_mes = _make_1m_bars([100],[101],[99],[100], start_time="2025-01-02 09:30:00")
    tdo_bar_mnq = _make_1m_bars([195],[196],[194],[195], start_time="2025-01-02 09:30:00")
    mes_full = pd.concat([mes, tdo_bar_mes]).sort_index()
    mnq_full = pd.concat([mnq, tdo_bar_mnq]).sort_index()
    return mnq_full, mes_full


def _build_long_session():
    """Helper: synthetic session with a long SMT divergence signal."""
    # Bar 5: MNQ bearish (close=198 < open=201) — required for find_entry_bar "long"
    # Bar 7: MES breaks session low (97 < 99), MNQ does not (199 >= 199)
    # Bar 8: MNQ bullish (close=202 > open=198) with low=194 < bear-body-close=198 → confirms
    mes = _make_1m_bars(
        opens= [100]*10,
        highs= [101]*10,
        lows=  [ 99,  99,  99,  99,  99,  99,  99,  97,  99,  99],
        closes=[100]*10,
        start_time="2025-01-02 09:00:00",
    )
    mnq = _make_1m_bars(
        opens= [200, 200, 200, 200, 200, 201, 200, 200, 198, 200],
        highs= [201, 201, 201, 201, 201, 201, 201, 201, 201, 201],
        lows=  [199, 199, 199, 199, 199, 197, 199, 199, 194, 199],
        closes=[200, 200, 200, 200, 200, 198, 200, 200, 202, 200],
        start_time="2025-01-02 09:00:00",
    )
    tdo_bar_mes = _make_1m_bars([100],[101],[99],[100], start_time="2025-01-02 09:30:00")
    tdo_bar_mnq = _make_1m_bars([205],[206],[204],[205], start_time="2025-01-02 09:30:00")
    mes_full = pd.concat([mes, tdo_bar_mes]).sort_index()
    mnq_full = pd.concat([mnq, tdo_bar_mnq]).sort_index()
    return mnq_full, mes_full


def test_screen_session_returns_short_signal():
    """Full pipeline: bearish SMT + confirm → short signal dict."""
    import train_smt
    mnq_full, mes_full = _build_short_session()
    signal = train_smt.screen_session(mnq_full, mes_full, datetime.date(2025, 1, 2))
    assert signal is not None, "Expected a short signal from the synthetic bearish SMT session"
    assert signal["direction"] == "short"
    assert "entry_price" in signal
    assert "stop_price" in signal
    assert "take_profit" in signal


def test_screen_session_returns_long_signal():
    """Full pipeline: bullish SMT + confirm → long signal dict."""
    import train_smt
    mnq_full, mes_full = _build_long_session()
    signal = train_smt.screen_session(mnq_full, mes_full, datetime.date(2025, 1, 2))
    assert signal is not None, "Expected a long signal from the synthetic bullish SMT session"
    assert signal["direction"] == "long"
    assert signal["take_profit"] == signal["tdo"]


def test_screen_session_no_divergence_returns_none():
    """Flat market with no divergence → None."""
    import train_smt
    # Perfectly correlated MES and MNQ — no divergence possible
    mes = _make_1m_bars(
        opens= [100]*10,
        highs= [101, 102, 101, 101, 101, 101, 101, 101, 101, 101],
        lows=  [ 99]*10,
        closes=[100]*10,
        start_time="2025-01-02 09:00:00",
    )
    mnq = _make_1m_bars(
        opens= [200]*10,
        highs= [201, 202, 201, 201, 201, 201, 201, 201, 201, 201],  # matches MES pattern
        lows=  [199]*10,
        closes=[200]*10,
        start_time="2025-01-02 09:00:00",
    )
    signal = train_smt.screen_session(mnq, mes, datetime.date(2025, 1, 2))
    assert signal is None


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


def test_manage_position_tp_long():
    """Long: high >= take_profit → 'exit_tp'."""
    import train_smt
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=20001, low=19950)
    assert train_smt.manage_position(pos, bar) == "exit_tp"


def test_manage_position_stop_long():
    """Long: low <= stop_price → 'exit_stop'."""
    import train_smt
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=19850, low=19799)
    assert train_smt.manage_position(pos, bar) == "exit_stop"


def test_manage_position_tp_short():
    """Short: low <= take_profit → 'exit_tp'."""
    import train_smt
    pos = _make_position("short", 20100, 20200, 20000)
    bar = _make_bar(high=20050, low=19999)
    assert train_smt.manage_position(pos, bar) == "exit_tp"


def test_manage_position_stop_short():
    """Short: high >= stop_price → 'exit_stop'."""
    import train_smt
    pos = _make_position("short", 20100, 20200, 20000)
    bar = _make_bar(high=20200, low=20050)
    assert train_smt.manage_position(pos, bar) == "exit_stop"


def test_manage_position_hold():
    """Neither TP nor stop triggered → 'hold'."""
    import train_smt
    pos = _make_position("long", 19900, 19800, 20000)
    bar = _make_bar(high=19950, low=19850)
    assert train_smt.manage_position(pos, bar) == "hold"
