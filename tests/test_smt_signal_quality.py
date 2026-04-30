# tests/test_smt_signal_quality.py
# Tests for Plan 1 signal quality features: midnight open, overnight range,
# hidden SMT, structural stop, invalidation exits, silver bullet, and TSV schema.
import csv
import datetime
import os
import tempfile

import pandas as pd
import pytest


def _make_5m_bars(date, n=30, base=20000.0, tz="America/New_York"):
    """Minimal 5m bar fixture for signal quality tests."""
    start = pd.Timestamp(f"{date} 09:00:00", tz=tz)
    idx = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame({
        "Open": [base] * n, "High": [base + 5] * n, "Low": [base - 5] * n,
        "Close": [base] * n, "Volume": [1000.0] * n
    }, index=idx)


def _make_bars_at(timestamps, opens, highs, lows, closes, tz="America/New_York"):
    """Build a bar DataFrame from explicit timestamps."""
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=tz) for t in timestamps])
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": [1000.0] * len(timestamps)
    }, index=idx)


# ══ compute_midnight_open ═════════════════════════════════════════════════════

def test_midnight_open_returns_first_bar_at_midnight():
    """Bar present at 00:00 ET → returns its Open."""
    from strategy_smt import compute_midnight_open
    date = datetime.date(2025, 1, 2)
    df = _make_bars_at(
        ["2025-01-02 00:00:00", "2025-01-02 00:05:00", "2025-01-02 09:30:00"],
        opens=[19999.0, 20001.0, 20010.0],
        highs=[20005.0, 20005.0, 20015.0],
        lows=[19995.0, 19995.0, 20005.0],
        closes=[20000.0, 20002.0, 20012.0],
    )
    result = compute_midnight_open(df, date)
    assert result == 19999.0


def test_midnight_open_returns_first_bar_when_no_midnight():
    """No 00:00 bar → returns first bar's Open."""
    from strategy_smt import compute_midnight_open
    date = datetime.date(2025, 1, 2)
    # Bars start at 01:00, not midnight
    df = _make_bars_at(
        ["2025-01-02 01:00:00", "2025-01-02 02:00:00"],
        opens=[20100.0, 20110.0],
        highs=[20110.0, 20120.0],
        lows=[20090.0, 20100.0],
        closes=[20105.0, 20115.0],
    )
    result = compute_midnight_open(df, date)
    assert result == 20100.0


def test_midnight_open_returns_none_on_empty():
    """Empty DataFrame → None (no exception)."""
    from strategy_smt import compute_midnight_open
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    empty.index = pd.DatetimeIndex([], tz="America/New_York")
    result = compute_midnight_open(empty, datetime.date(2025, 1, 2))
    assert result is None


# ══ compute_overnight_range ═══════════════════════════════════════════════════

def test_overnight_range_correct_hi_lo():
    """Bars at 02:00 and 07:00 → correct high/low."""
    from strategy_smt import compute_overnight_range
    date = datetime.date(2025, 1, 2)
    df = _make_bars_at(
        ["2025-01-02 02:00:00", "2025-01-02 07:00:00", "2025-01-02 09:30:00"],
        opens=[20000.0, 20050.0, 20030.0],
        highs=[20020.0, 20080.0, 20040.0],
        lows=[19990.0, 20040.0, 20025.0],
        closes=[20010.0, 20060.0, 20035.0],
    )
    result = compute_overnight_range(df, date)
    assert result["overnight_high"] == pytest.approx(20080.0)
    assert result["overnight_low"] == pytest.approx(19990.0)


def test_overnight_range_none_when_no_pre9am_bars():
    """All bars at or after 09:00 → both values None."""
    from strategy_smt import compute_overnight_range
    date = datetime.date(2025, 1, 2)
    df = _make_bars_at(
        ["2025-01-02 09:00:00", "2025-01-02 09:30:00"],
        opens=[20000.0, 20010.0],
        highs=[20005.0, 20015.0],
        lows=[19995.0, 20005.0],
        closes=[20002.0, 20012.0],
    )
    result = compute_overnight_range(df, date)
    assert result["overnight_high"] is None
    assert result["overnight_low"] is None


def test_overnight_range_excludes_0900_bar():
    """Bar at exactly 09:00 is excluded (strict < 09:00)."""
    from strategy_smt import compute_overnight_range
    date = datetime.date(2025, 1, 2)
    df = _make_bars_at(
        ["2025-01-02 08:59:00", "2025-01-02 09:00:00"],
        opens=[20000.0, 20100.0],
        highs=[20010.0, 20200.0],
        lows=[19990.0, 20050.0],
        closes=[20005.0, 20150.0],
    )
    result = compute_overnight_range(df, date)
    # Only the 08:59 bar should contribute
    assert result["overnight_high"] == pytest.approx(20010.0)
    assert result["overnight_low"] == pytest.approx(19990.0)


# ══ detect_smt_divergence 5-tuple ════════════════════════════════════════════

def _make_1m_bars(opens, highs, lows, closes, start="2025-01-02 09:00:00"):
    idx = pd.date_range(start=start, periods=len(opens), freq="1min", tz="America/New_York")
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": [1000.0] * len(opens)
    }, index=idx)


def test_smt_divergence_returns_5_tuple():
    """Valid bearish divergence → 5-element tuple with smt_type == 'wick'."""
    from strategy_smt import detect_smt_divergence
    mes = _make_1m_bars(
        opens=[100] * 5, highs=[101, 102, 101, 101, 103],
        lows=[99] * 5, closes=[100] * 5,
    )
    mnq = _make_1m_bars(
        opens=[200] * 5, highs=[201, 202, 201, 201, 201],
        lows=[199] * 5, closes=[200] * 5,
    )
    result = detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None
    assert len(result) == 5
    direction, sweep, miss, smt_type, defended = result
    assert direction == "short"
    assert smt_type == "wick"
    assert defended == pytest.approx(202.0)


def test_smt_divergence_hidden_body_when_enabled(monkeypatch):
    """Wick fails but close diverges, HIDDEN_SMT_ENABLED=True → smt_type == 'body'."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HIDDEN_SMT_ENABLED", True)
    # Wick SMT will NOT fire: MES high does NOT exceed session high (101)
    # Body SMT WILL fire: MES close exceeds session close high (100), MNQ close does not
    mes = _make_1m_bars(
        opens=[100, 100, 100, 100, 100],
        highs=[101, 101, 101, 101, 101],     # MES high stays at 101 = session high, no sweep
        lows=[99, 99, 99, 99, 99],
        closes=[100, 100, 100, 100, 101],    # MES close makes new session close high
    )
    mnq = _make_1m_bars(
        opens=[200, 200, 200, 200, 200],
        highs=[201, 201, 201, 201, 201],
        lows=[199, 199, 199, 199, 199],
        closes=[200, 200, 200, 200, 200],   # MNQ close does NOT make new close high
    )
    result = strategy_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None
    assert result[3] == "body"
    assert result[0] == "short"


# ══ _build_signal_from_bar structural stop ═══════════════════════════════════

def _make_single_bar(open_=20000.0, high=20010.0, low=19990.0, close=20005.0,
                     ts="2025-01-02 10:00:00", tz="America/New_York"):
    idx = pd.DatetimeIndex([pd.Timestamp(ts, tz=tz)])
    df = pd.DataFrame({"Open": [open_], "High": [high], "Low": [low],
                       "Close": [close], "Volume": [1000.0]}, index=idx)
    return df.iloc[0], idx[0]


def test_structural_stop_short(monkeypatch):
    """STRUCTURAL_STOP_MODE=True, short; stop = div_bar_high + buffer."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "STRUCTURAL_STOP_MODE", True)
    monkeypatch.setattr(strategy_smt, "STRUCTURAL_STOP_BUFFER_PTS", 2.0)
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Short: entry at 20005, TDO below (e.g. 19990), div_bar_high = 20020
    bar, ts = _make_single_bar(close=20005.0)
    tdo = 19990.0
    sig = strategy_smt._build_signal_from_bar(
        bar, ts, "short", tdo,
        divergence_bar_high=20020.0, divergence_bar_low=19980.0,
    )
    assert sig is not None
    assert sig["stop_price"] == pytest.approx(20022.0)  # 20020 + 2.0


def test_structural_stop_long(monkeypatch):
    """STRUCTURAL_STOP_MODE=True, long; stop = div_bar_low - buffer."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "STRUCTURAL_STOP_MODE", True)
    monkeypatch.setattr(strategy_smt, "STRUCTURAL_STOP_BUFFER_PTS", 2.0)
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    # Long: entry at 20005, TDO above (e.g. 20020), div_bar_low = 19980
    bar, ts = _make_single_bar(close=20005.0)
    tdo = 20020.0
    sig = strategy_smt._build_signal_from_bar(
        bar, ts, "long", tdo,
        divergence_bar_high=20020.0, divergence_bar_low=19980.0,
    )
    assert sig is not None
    assert sig["stop_price"] == pytest.approx(19978.0)  # 19980 - 2.0


def test_ratio_stop_unchanged_when_structural_disabled(monkeypatch):
    """STRUCTURAL_STOP_MODE=False → original ratio behavior."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(strategy_smt, "SHORT_STOP_RATIO", 0.5)
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    # Short: entry at 20010, TDO at 19990 → distance = 20, stop = 20010 + 0.5*20 = 20020
    bar, ts = _make_single_bar(close=20010.0)
    tdo = 19990.0
    sig = strategy_smt._build_signal_from_bar(bar, ts, "short", tdo)
    assert sig is not None
    assert sig["stop_price"] == pytest.approx(20020.0)


# ══ manage_position invalidation exits ═══════════════════════════════════════

def _make_position(direction="long", entry=20000.0, stop=19990.0, tp=20020.0,
                   div_high=20015.0, div_low=19985.0, midnight_open=20005.0,
                   defended=19988.0, smt_type="wick"):
    return {
        "direction": direction,
        "entry_price": entry,
        "stop_price": stop,
        "take_profit": tp,
        "tdo": tp,
        "divergence_bar_high": div_high,
        "divergence_bar_low": div_low,
        "midnight_open": midnight_open,
        "smt_defended_level": defended,
        "smt_type": smt_type,
    }


def _make_bar_series(open_=20000.0, high=20010.0, low=19995.0, close=20002.0):
    return pd.Series({"Open": open_, "High": high, "Low": low, "Close": close})


def test_mss_exit_long_fires_on_close_below_div_low(monkeypatch):
    """Close < divergence_bar_low → exit_invalidation_mss for long."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", True)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = _make_position(direction="long", stop=19980.0, div_low=19985.0)
    # Close at 19984 — below div_low=19985, but above stop=19980 (no stop hit)
    bar = _make_bar_series(high=19990.0, low=19983.0, close=19984.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "exit_invalidation_mss"


def test_mss_exit_does_not_fire_on_wick_only(monkeypatch):
    """Low < div_low but close > div_low → no MSS exit (close-based only)."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", True)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = _make_position(direction="long", stop=19970.0, div_low=19985.0)
    # Low=19980 pierces div_low, but close=19987 is above div_low
    bar = _make_bar_series(high=19995.0, low=19980.0, close=19987.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "hold"


def test_cisd_exit_long_fires_on_close_below_midnight_open(monkeypatch):
    """Close < midnight_open → exit_invalidation_cisd for long."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_CISD_EXIT", True)
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = _make_position(direction="long", stop=19970.0, midnight_open=20005.0)
    # Close at 20004 — below midnight_open=20005
    bar = _make_bar_series(high=20010.0, low=20000.0, close=20004.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "exit_invalidation_cisd"


def test_cisd_exit_disabled_when_constant_false(monkeypatch):
    """INVALIDATION_CISD_EXIT=False → no cisd exit even when close < midnight_open."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = _make_position(direction="long", stop=19970.0, midnight_open=20005.0)
    bar = _make_bar_series(high=20010.0, low=20000.0, close=20004.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "hold"


def test_smt_invalidation_exit_long(monkeypatch):
    """Close < smt_defended_level → exit_invalidation_smt for long."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_SMT_EXIT", True)
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(strategy_smt, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    # defended=19988, stop much lower so stop won't fire
    pos = _make_position(direction="long", stop=19970.0, defended=19988.0)
    # Close at 19987 — below defended=19988
    bar = _make_bar_series(high=19995.0, low=19985.0, close=19987.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "exit_invalidation_smt"


def test_invalidation_fires_before_stop(monkeypatch):
    """Bar hits both MSS invalidation and stop → invalidation fires first."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "INVALIDATION_MSS_EXIT", True)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    # Long: stop=19985, div_low=19990; close=19983 is below both
    pos = _make_position(direction="long", stop=19985.0, div_low=19990.0)
    bar = _make_bar_series(high=20000.0, low=19980.0, close=19983.0)
    result = strategy_smt.manage_position(pos, bar)
    # Invalidation check comes before stop check in manage_position
    assert result == "exit_invalidation_mss"


# ══ Silver bullet window ══════════════════════════════════════════════════════

def _make_session_bars_around(date="2025-01-02", base=20000.0):
    """30 bars starting at 09:30, one per minute."""
    start = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    idx = pd.date_range(start, periods=30, freq="1min")
    return pd.DataFrame({
        "Open":   [base] * 30,
        "High":   [base + 5] * 30,
        "Low":    [base - 5] * 30,
        "Close":  [base] * 30,
        "Volume": [1000.0] * 30,
    }, index=idx)


def test_silver_bullet_blocks_signal_outside_window(monkeypatch):
    """Divergence at 09:35 → no signal when SILVER_BULLET_WINDOW_ONLY=True."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "SILVER_BULLET_WINDOW_ONLY", True)
    monkeypatch.setattr(strategy_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)

    # Build bars: bar at 09:35 (index 5) makes a bearish SMT divergence
    n = 20
    base = 20000.0
    start = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")
    highs = [base + 5] * n
    highs[4] = base + 8  # bar 4 (09:34) sets session high
    highs[5] = base + 10  # bar 5 (09:35) MES sweeps high

    mes = pd.DataFrame({"Open": [base]*n, "High": highs, "Low": [base-5]*n,
                        "Close": [base]*n, "Volume": [1000.0]*n}, index=idx)
    mnq_highs = [base + 5] * n
    # MNQ bar 5 does NOT make new high — SMT fires
    mnq = pd.DataFrame({"Open": [base]*n, "High": mnq_highs, "Low": [base-5]*n,
                        "Close": [base]*n, "Volume": [1000.0]*n}, index=idx)

    result = strategy_smt.screen_session(mnq, mes, tdo=base - 20)
    # Signal at 09:35 is outside 09:50–10:10 window, should be blocked
    assert result is None


def test_silver_bullet_allows_signal_inside_window(monkeypatch):
    """Divergence at 09:54 (inside 09:50–10:10) → signal fires when SILVER_BULLET_WINDOW_ONLY=True."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "SILVER_BULLET_WINDOW_ONLY", True)
    monkeypatch.setattr(strategy_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_END", "")

    # Bars starting at 09:50; divergence at bar 4 (09:54), confirmation at bar 5 (09:55)
    n = 10
    base = 20000.0
    start = pd.Timestamp("2025-01-02 09:50:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")

    # Bar 2 (09:52): bullish (close > open) → provides anchor_close=20003 for short setup
    opens = [base] * n
    closes = [base] * n
    closes[2] = base + 3   # bar 2 bullish → anchor_close = 20003

    highs = [base + 5] * n
    highs[3] = base + 8    # bar 3 (09:53) sets session high for MES = 20008
    highs[4] = base + 10   # bar 4 (09:54) MES sweeps session high = 20010 > 20008 → SMT bearish

    # Bar 5 (09:55): confirmation — bearish, and high > anchor_close=20003
    closes[5] = base - 2   # close < open → bearish
    highs[5] = base + 6    # high > anchor_close=20003 ✓

    mes = pd.DataFrame({"Open": opens, "High": highs, "Low": [base - 5]*n,
                        "Close": closes, "Volume": [1000.0]*n}, index=idx)
    # MNQ: same opens/closes, but highs never exceed session high (stays at base+5 = 20005)
    mnq_highs = [base + 5] * n  # MNQ never makes new high past 20005
    mnq = pd.DataFrame({"Open": opens, "High": mnq_highs, "Low": [base - 5]*n,
                        "Close": closes, "Volume": [1000.0]*n}, index=idx)

    result = strategy_smt.screen_session(mnq, mes, tdo=base - 30)
    assert result is not None
    assert result["direction"] == "short"


# ══ Overnight sweep gate ══════════════════════════════════════════════════════

def test_overnight_sweep_required_blocks_when_not_swept(monkeypatch):
    """OVERNIGHT_SWEEP_REQUIRED=True, overnight high not exceeded before signal bar → signal skipped."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "OVERNIGHT_SWEEP_REQUIRED", True)
    monkeypatch.setattr(strategy_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_END", "")

    # Overnight high = 20030; session bars never exceed 20030 before the signal bar
    overnight_range = {"overnight_high": 20030.0, "overnight_low": 19970.0}

    n = 10
    base = 20000.0
    start = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")
    highs = [base + 5] * n  # max high = 20005, never exceeds overnight_high=20030
    highs[7] = base + 8     # bar 7 sets session high
    highs[8] = base + 10    # bar 8 MES sweeps

    mes = pd.DataFrame({"Open": [base]*n, "High": highs, "Low": [base-5]*n,
                        "Close": [base]*n, "Volume": [1000.0]*n}, index=idx)
    mnq = pd.DataFrame({"Open": [base]*n, "High": [base+5]*n, "Low": [base-5]*n,
                        "Close": [base]*n, "Volume": [1000.0]*n}, index=idx)

    result = strategy_smt.screen_session(mnq, mes, tdo=base - 20,
                                         overnight_range=overnight_range)
    assert result is None


def test_overnight_sweep_required_passes_when_swept(monkeypatch):
    """Overnight high exceeded before signal bar → signal fires."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "OVERNIGHT_SWEEP_REQUIRED", True)
    monkeypatch.setattr(strategy_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strategy_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strategy_smt, "SIGNAL_BLACKOUT_END", "")

    # Overnight high = 20005; session bar 0 has high 20012 > 20005 (swept early)
    overnight_range = {"overnight_high": 20005.0, "overnight_low": 19970.0}

    n = 12
    base = 20000.0
    start = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")

    opens = [base] * n
    closes = [base] * n
    # Bar 2: bullish bar to provide anchor_close for short setup
    closes[2] = base + 3   # bar 2 bullish → anchor_close = 20003

    highs = [base + 5] * n
    highs[0] = base + 12   # bar 0 sweeps overnight high (20012 > 20005)
    highs[7] = base + 14   # bar 7 sets session high for MES = 20014
    highs[8] = base + 16   # bar 8 MES sweeps session high → SMT fires (8 > 7's high)

    # Bar 9: bearish confirmation — close < open and high > anchor_close=20003
    closes[9] = base - 2   # bearish
    highs[9] = base + 6    # high > anchor_close=20003 ✓

    mes = pd.DataFrame({"Open": opens, "High": highs, "Low": [base - 5]*n,
                        "Close": closes, "Volume": [1000.0]*n}, index=idx)
    # MNQ highs stay at base+5=20005, never exceed session high of 20014
    mnq_highs = [base + 5] * n
    mnq_highs[0] = base + 12   # MNQ also sweeps overnight high on bar 0
    mnq = pd.DataFrame({"Open": opens, "High": mnq_highs, "Low": [base - 5]*n,
                        "Close": closes, "Volume": [1000.0]*n}, index=idx)

    result = strategy_smt.screen_session(mnq, mes, tdo=base - 20,
                                         overnight_range=overnight_range)
    assert result is not None
    assert result["direction"] == "short"


# ══ TSV schema ════════════════════════════════════════════════════════════════

def test_write_trades_tsv_includes_smt_type(tmp_path, monkeypatch):
    """_write_trades_tsv writes smt_type column to trades.tsv."""
    import backtest_smt
    trade = {
        "entry_date": "2025-01-02", "entry_time": "09:55", "exit_time": "10:30",
        "direction": "short", "entry_price": 20000.0, "exit_price": 19980.0,
        "tdo": 19980.0, "stop_price": 20020.0, "contracts": 1, "pnl": 40.0,
        "exit_type": "exit_tp", "divergence_bar": 5, "entry_bar": 6,
        "stop_bar_wick_pts": 0.0, "reentry_sequence": 1, "prior_trade_bars_held": 0,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 2.0, "smt_miss_pts": 1.0,
        "bars_since_divergence": 1, "matches_hypothesis": True, "smt_type": "wick",
    }
    tsv_path = tmp_path / "trades.tsv"
    monkeypatch.chdir(tmp_path)
    backtest_smt._write_trades_tsv([trade])
    with open(tsv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
    assert len(rows) == 1
    assert "smt_type" in rows[0]
    assert rows[0]["smt_type"] == "wick"
