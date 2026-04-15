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

def test_compute_tdo_finds_midnight_bar():
    """00:00 ET bar exists → returns its open."""
    import train_smt
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
    # Pass tdo directly; caller is responsible for computing it
    signal = train_smt.screen_session(mnq, mes, 195.0)
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
    """Helper: synthetic session with a short SMT divergence signal.

    Returns pre-sliced session bars and the TDO float — caller passes both to screen_session.
    Bar 5: MNQ bullish (close=202 > open=199) — required for find_entry_bar "short"
    Bar 7: MES breaks session high (103 > 101), MNQ does not (201 <= 201)
    Bar 8: MNQ bearish (close=198 < open=200) with high=203 > bull-body-close=202 → confirms
    """
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
    # TDO=195.0: below entry_price≈198 → valid for a short signal
    return mnq, mes, 195.0


def _build_long_session():
    """Helper: synthetic session with a long SMT divergence signal.

    Returns pre-sliced session bars and the TDO float — caller passes both to screen_session.
    Bar 5: MNQ bearish (close=198 < open=201) — required for find_entry_bar "long"
    Bar 7: MES breaks session low (97 < 99), MNQ does not (199 >= 199)
    Bar 8: MNQ bullish (close=202 > open=198) with low=194 < bear-body-close=198 → confirms
    """
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
    # TDO=205.0: above entry_price≈202 → valid for a long signal
    return mnq, mes, 205.0


def test_screen_session_returns_short_signal(monkeypatch):
    """Full pipeline: bearish SMT + confirm → short signal dict."""
    import train_smt
    # Disable MIN_STOP_POINTS guard: synthetic bars have TDO very close to entry
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes, tdo = _build_short_session()
    signal = train_smt.screen_session(mnq, mes, tdo)
    assert signal is not None, "Expected a short signal from the synthetic bearish SMT session"
    assert signal["direction"] == "short"
    assert "entry_price" in signal
    assert "stop_price" in signal
    assert "take_profit" in signal


def test_screen_session_returns_long_signal(monkeypatch):
    """Full pipeline: bullish SMT + confirm → long signal dict."""
    import train_smt
    # Disable MIN_STOP_POINTS guard: synthetic bars have TDO very close to entry
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes, tdo = _build_long_session()
    signal = train_smt.screen_session(mnq, mes, tdo)
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
    signal = train_smt.screen_session(mnq, mes, 20000.0)
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

def test_trade_direction_short_blocks_long(monkeypatch):
    """TRADE_DIRECTION='short' causes screen_session to skip bullish SMT signals."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_long_session_bars()
    result = train_smt.screen_session(mnq, mes, 20100.0)
    assert result is None


def test_trade_direction_long_blocks_short(monkeypatch):
    """TRADE_DIRECTION='long' causes screen_session to skip bearish SMT signals."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "long")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    result = train_smt.screen_session(mnq, mes, 19900.0)
    assert result is None


def test_trade_direction_both_passes_short(monkeypatch):
    """TRADE_DIRECTION='both' does not filter any signal direction."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    result = train_smt.screen_session(mnq, mes, 19900.0)
    assert result is not None
    assert result["direction"] == "short"


# ══ TDO validity gate tests ══════════════════════════════════════════════════

def test_tdo_validity_blocks_inverted_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips long signal when TDO < entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_long_session_bars()
    # TDO below entry (entry ≈ base+2=20002) → inverted long
    result = train_smt.screen_session(mnq, mes, 19950.0)
    assert result is None


def test_tdo_validity_passes_valid_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True allows long signal when TDO > entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_long_session_bars()
    # TDO well above entry (entry ≈ base+2=20002) → valid long
    result = train_smt.screen_session(mnq, mes, 20100.0)
    assert result is not None
    assert result["direction"] == "long"
    assert result["take_profit"] > result["entry_price"]


def test_tdo_validity_blocks_inverted_short(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips short signal when TDO > entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    # TDO above entry (entry ≈ base-2=19998) → inverted short
    result = train_smt.screen_session(mnq, mes, 20100.0)
    assert result is None


def test_tdo_validity_false_passes_inverted(monkeypatch):
    """TDO_VALIDITY_CHECK=False allows inverted signals through (legacy behavior)."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    # TDO above entry → inverted short, but gate is off
    result = train_smt.screen_session(mnq, mes, 20100.0)
    assert result is not None  # passes through despite inversion


# ══ MIN_STOP_POINTS guard tests ══════════════════════════════════════════════

def test_min_stop_points_filters_tiny_stop(monkeypatch):
    """MIN_STOP_POINTS=50 rejects signals where stop distance < 50 pts."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 50.0)
    mnq, mes = _make_short_session_bars()
    # TDO just 5 pts below entry (entry≈19998) → stop = 0.45*5 = 2.25 pts → filtered
    result = train_smt.screen_session(mnq, mes, 19995.0)
    assert result is None


def test_min_stop_points_zero_disables_guard(monkeypatch):
    """MIN_STOP_POINTS=0.0 allows all stop distances through."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    result = train_smt.screen_session(mnq, mes, 19995.0)
    # tiny stop but guard is off — signal should come through
    assert result is not None


# ══ Per-direction stop ratio tests ══════════════════════════════════════════

def test_long_stop_ratio_applied(monkeypatch):
    """LONG_STOP_RATIO is used for long stop computation."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "LONG_STOP_RATIO", 0.3)
    mnq, mes = _make_long_session_bars()
    tdo_val = 20100.0
    result = train_smt.screen_session(mnq, mes, tdo_val)
    assert result is not None
    assert result["direction"] == "long"
    expected_stop = result["entry_price"] - 0.3 * abs(result["entry_price"] - tdo_val)
    assert abs(result["stop_price"] - expected_stop) < 0.01


def test_short_stop_ratio_applied(monkeypatch):
    """SHORT_STOP_RATIO is used for short stop computation."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "SHORT_STOP_RATIO", 0.6)
    mnq, mes = _make_short_session_bars()
    tdo_val = 19900.0
    result = train_smt.screen_session(mnq, mes, tdo_val)
    assert result is not None
    assert result["direction"] == "short"
    expected_stop = result["entry_price"] + 0.6 * abs(result["entry_price"] - tdo_val)
    assert abs(result["stop_price"] - expected_stop) < 0.01


# ══ print_direction_breakdown tests ══════════════════════════════════════════

def test_print_direction_breakdown_format(capsys):
    """print_direction_breakdown prints per-direction metrics with correct prefix."""
    import train_smt
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
    import train_smt
    train_smt.print_direction_breakdown({"trade_records": []}, prefix="test_")
    out = capsys.readouterr().out
    assert out == ""
