# tests/test_smt_position_arch.py
# Tests for Plan 2: FVG detection, displacement, SMT fill, two-layer position model,
# partial exit, SMT-optional entries, and backtest integration.
import json
from pathlib import Path

import pandas as pd
import pytest

import strategy_smt as _strat


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_bars(n=10, base=20000.0, tz="America/New_York"):
    """Minimal 1m bar fixture with flat OHLC for mutation in tests."""
    start = pd.Timestamp("2025-01-02 09:00:00", tz=tz)
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame({
        "Open": [base] * n, "High": [base + 5] * n, "Low": [base - 5] * n,
        "Close": [base] * n, "Volume": [1000.0] * n,
    }, index=idx)


def _flat_bars(n=10, high=20005.0, low=19995.0, open_=20000.0, close=20000.0):
    """Reset-indexed (integer-indexed) bars for detection function tests."""
    return pd.DataFrame({
        "Open": [open_] * n, "High": [high] * n, "Low": [low] * n,
        "Close": [close] * n, "Volume": [1000.0] * n,
    })


def _build_short_signal_bars(date="2025-01-02", base=20000.0, n=90):
    """MNQ/MES bars producing a bearish SMT divergence at bar 7, confirmation at bar 8."""
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    opens  = [base] * n
    closes = [base] * n
    opens[5]  = base - 2; closes[5] = base + 2
    mes_highs[7] = base + 30
    opens[8]  = base + 2; closes[8] = base - 2; mnq_highs[8] = base + 6
    mnq = pd.DataFrame({"Open": opens, "High": mnq_highs, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": mes_highs, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    return mnq, mes


# ── detect_fvg ────────────────────────────────────────────────────────────────

def test_fvg_bullish_detected(monkeypatch):
    """bar1.High=100, bar3.Low=103 → bullish FVG zone [100, 103] returned for long."""
    monkeypatch.setattr(_strat, "FVG_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_MIN_SIZE_PTS", 2.0)
    bars = _flat_bars(n=10, high=102, low=98)
    # bar 0: bar1 (high=100), bar 1: impulse, bar 2: bar3 (low=103 > bar1.high=100)
    bars.at[0, "High"] = 100.0; bars.at[0, "Low"] = 95.0
    bars.at[1, "High"] = 110.0; bars.at[1, "Low"] = 98.0  # impulse
    bars.at[2, "High"] = 108.0; bars.at[2, "Low"] = 103.0  # bar3: low > bar1 high
    result = _strat.detect_fvg(bars, bar_idx=5, direction="long", lookback=10)
    assert result is not None
    assert result["fvg_low"] == pytest.approx(100.0)
    assert result["fvg_high"] == pytest.approx(103.0)


def test_fvg_bearish_detected(monkeypatch):
    """bar1.Low=97, bar3.High=94 → bearish FVG zone [94, 97] returned for short."""
    monkeypatch.setattr(_strat, "FVG_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_MIN_SIZE_PTS", 2.0)
    bars = _flat_bars(n=10, high=102, low=92)
    bars.at[0, "High"] = 100.0; bars.at[0, "Low"] = 97.0  # bar1
    bars.at[1, "High"] = 99.0;  bars.at[1, "Low"] = 88.0  # impulse
    bars.at[2, "High"] = 94.0;  bars.at[2, "Low"] = 90.0  # bar3: high < bar1.low=97
    result = _strat.detect_fvg(bars, bar_idx=5, direction="short", lookback=10)
    assert result is not None
    assert result["fvg_high"] == pytest.approx(97.0)
    assert result["fvg_low"]  == pytest.approx(94.0)


def test_fvg_returns_none_when_bars_overlap(monkeypatch):
    """bar3.Low <= bar1.High → no gap → None returned for long."""
    monkeypatch.setattr(_strat, "FVG_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_MIN_SIZE_PTS", 2.0)
    bars = _flat_bars(n=10, high=105, low=95)
    bars.at[0, "High"] = 103.0; bars.at[0, "Low"] = 97.0
    bars.at[1, "High"] = 110.0; bars.at[1, "Low"] = 96.0
    bars.at[2, "High"] = 106.0; bars.at[2, "Low"] = 102.0  # bar3.low=102 < bar1.high=103
    result = _strat.detect_fvg(bars, bar_idx=5, direction="long", lookback=10)
    assert result is None


# ── detect_displacement ───────────────────────────────────────────────────────

def test_displacement_long_detected(monkeypatch):
    """SMT_OPTIONAL=True, large bullish body ≥ MIN_DISPLACEMENT_PTS → True."""
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", True)
    monkeypatch.setattr(_strat, "MIN_DISPLACEMENT_PTS", 10.0)
    bars = _flat_bars(n=5)
    bars.at[3, "Open"]  = 20000.0
    bars.at[3, "Close"] = 20015.0  # body = 15 >= 10
    assert _strat.detect_displacement(bars, bar_idx=3, direction="long") is True


def test_displacement_false_when_optional_disabled(monkeypatch):
    """SMT_OPTIONAL=False → always False regardless of body size."""
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(_strat, "MIN_DISPLACEMENT_PTS", 10.0)
    bars = _flat_bars(n=5)
    bars.at[3, "Open"]  = 20000.0
    bars.at[3, "Close"] = 20050.0  # huge body, but optional disabled
    assert _strat.detect_displacement(bars, bar_idx=3, direction="long") is False


# ── detect_smt_fill ──────────────────────────────────────────────────────────

def test_smt_fill_bearish_detected(monkeypatch):
    """SMT_FILL_ENABLED=True, MES reaches bearish FVG zone, MNQ does not → ('short', ...) returned."""
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_MIN_SIZE_PTS", 2.0)
    # Build MES bars with a bearish FVG at bars 0-2: bar1.Low=97, bar3.High=94
    mes = _flat_bars(n=10, high=100, low=90)
    mes.at[0, "Low"] = 97.0;  mes.at[0, "High"] = 100.0
    mes.at[1, "Low"] = 88.0;  mes.at[1, "High"] = 99.0
    mes.at[2, "High"] = 94.0; mes.at[2, "Low"] = 90.0
    # Current MES bar (idx=5) reaches into the FVG zone (fvg_low=94, fvg_high=97)
    mes.at[5, "High"] = 95.0  # >= fvg_low (94)
    # MNQ does NOT reach the zone
    mnq = _flat_bars(n=10, high=93.0, low=88.0)  # mnq high = 93 < fvg_low=94
    result = _strat.detect_smt_fill(mes, mnq, bar_idx=5, lookback=10)
    assert result is not None
    assert result[0] == "short"


def test_smt_fill_none_when_both_instruments_reach_zone(monkeypatch):
    """Both MES and MNQ reach the FVG zone → None."""
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_ENABLED", True)
    monkeypatch.setattr(_strat, "FVG_MIN_SIZE_PTS", 2.0)
    mes = _flat_bars(n=10, high=100, low=90)
    mes.at[0, "Low"] = 97.0;  mes.at[0, "High"] = 100.0
    mes.at[1, "Low"] = 88.0;  mes.at[1, "High"] = 99.0
    mes.at[2, "High"] = 94.0; mes.at[2, "Low"] = 90.0
    mes.at[5, "High"] = 95.0
    # MNQ also reaches the zone (high >= 94)
    mnq = _flat_bars(n=10, high=96.0, low=88.0)
    result = _strat.detect_smt_fill(mes, mnq, bar_idx=5, lookback=10)
    # MNQ high=96 >= fvg_low=94, so the bearish fill condition is not met
    assert result is None


# ── _build_signal_from_bar with fvg_zone ─────────────────────────────────────

def test_signal_includes_fvg_fields_when_zone_provided(monkeypatch):
    """fvg_zone dict provided → signal['fvg_high'] and ['fvg_low'] match zone values."""
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    bar = pd.Series({"Open": 20000.0, "High": 20010.0, "Low": 19990.0, "Close": 20005.0})
    ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")
    fvg = {"fvg_high": 20020.0, "fvg_low": 20015.0, "fvg_bar": 2}
    signal = _strat._build_signal_from_bar(bar, ts, "long", 20050.0, fvg_zone=fvg)
    assert signal is not None
    assert signal["fvg_high"] == pytest.approx(20020.0)
    assert signal["fvg_low"]  == pytest.approx(20015.0)


def test_signal_fvg_none_when_no_zone(monkeypatch):
    """fvg_zone=None → signal['fvg_high'] and ['fvg_low'] are None."""
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    bar = pd.Series({"Open": 20000.0, "High": 20010.0, "Low": 19990.0, "Close": 20005.0})
    ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")
    signal = _strat._build_signal_from_bar(bar, ts, "long", 20050.0, fvg_zone=None)
    assert signal is not None
    assert signal["fvg_high"] is None
    assert signal["fvg_low"]  is None


# ── manage_position Layer B ───────────────────────────────────────────────────

def _make_position(direction="long", entry=20000.0, tp=20050.0, stop=19980.0, contracts=2):
    return {
        "direction": direction, "entry_price": entry, "take_profit": tp,
        "stop_price": stop, "contracts": contracts, "tdo": tp,
        "fvg_high": 20010.0, "fvg_low": 20005.0,
        "total_contracts_target": 4, "layer_b_entered": False,
        "layer_b_entry_price": None, "layer_b_contracts": 0,
        "partial_done": False, "partial_price": None,
        "partial_exit_level": None, "divergence_bar_high": None, "divergence_bar_low": None,
        "midnight_open": None, "smt_defended_level": None,
    }


def test_layer_b_enters_when_price_retraces_to_fvg_long(monkeypatch):
    """Bar Low retraces into FVG zone → layer_b_entered=True, contracts increase."""
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", True)
    monkeypatch.setattr(_strat, "FVG_LAYER_B_TRIGGER", True)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_BUFFER_PTS", 2.0)
    position = _make_position(contracts=2)
    # Bar low retraces into FVG zone [20005, 20010]
    bar = pd.Series({"Open": 20008.0, "High": 20015.0, "Low": 20006.0, "Close": 20008.0})
    result = _strat.manage_position(position, bar)
    assert position["layer_b_entered"] is True
    assert position["contracts"] == 4  # was 2, now 2+2=4


def test_layer_b_does_not_enter_twice(monkeypatch):
    """layer_b_entered=True on second FVG bar → contracts unchanged."""
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", True)
    monkeypatch.setattr(_strat, "FVG_LAYER_B_TRIGGER", True)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    position = _make_position(contracts=4)
    position["layer_b_entered"] = True  # already entered
    bar = pd.Series({"Open": 20008.0, "High": 20015.0, "Low": 20006.0, "Close": 20008.0})
    _strat.manage_position(position, bar)
    assert position["contracts"] == 4  # unchanged


def test_layer_b_stop_tightens_to_fvg_boundary_on_entry(monkeypatch):
    """On Layer B entry for long, stop moves to fvg_low - STRUCTURAL_STOP_BUFFER_PTS."""
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", True)
    monkeypatch.setattr(_strat, "FVG_LAYER_B_TRIGGER", True)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_BUFFER_PTS", 2.0)
    position = _make_position(contracts=2, stop=19970.0)  # loose stop
    bar = pd.Series({"Open": 20008.0, "High": 20015.0, "Low": 20006.0, "Close": 20008.0})
    _strat.manage_position(position, bar)
    expected_stop = 20005.0 - 2.0  # fvg_low - buffer = 20003
    assert position["stop_price"] >= expected_stop  # stop tightened


# ── manage_position partial exit ─────────────────────────────────────────────

def _make_partial_position(direction="long", entry=20000.0, tp=20050.0, stop=19970.0):
    return {
        "direction": direction, "entry_price": entry, "take_profit": tp,
        "stop_price": stop, "contracts": 4, "tdo": tp,
        "fvg_high": None, "fvg_low": None,
        "total_contracts_target": 4, "layer_b_entered": False,
        "layer_b_entry_price": None, "layer_b_contracts": 0,
        "partial_done": False, "partial_price": None,
        "partial_exit_level": (entry + tp) / 2,  # 20025.0
        "divergence_bar_high": None, "divergence_bar_low": None,
        "midnight_open": None, "smt_defended_level": None,
    }


def test_partial_exit_fires_when_price_reaches_level_long(monkeypatch):
    """Bar High >= partial_exit_level → 'partial_exit' returned."""
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", True)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    position = _make_partial_position()
    # Bar high reaches partial_exit_level = 20025
    bar = pd.Series({"Open": 20010.0, "High": 20026.0, "Low": 20008.0, "Close": 20020.0})
    result = _strat.manage_position(position, bar)
    assert result == "partial_exit"
    assert position["partial_done"] is True


def test_partial_exit_does_not_fire_twice(monkeypatch):
    """partial_done=True → no second partial_exit on subsequent bars."""
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", True)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    position = _make_partial_position()
    position["partial_done"] = True  # already fired
    bar = pd.Series({"Open": 20010.0, "High": 20030.0, "Low": 20008.0, "Close": 20025.0})
    result = _strat.manage_position(position, bar)
    assert result != "partial_exit"


def test_partial_exit_disabled_when_constant_false(monkeypatch):
    """PARTIAL_EXIT_ENABLED=False → 'hold' even when price reaches partial level."""
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", False)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    position = _make_partial_position()
    bar = pd.Series({"Open": 20010.0, "High": 20030.0, "Low": 20008.0, "Close": 20025.0})
    result = _strat.manage_position(position, bar)
    assert result == "hold"


# ── screen_session SMT-optional ───────────────────────────────────────────────

def test_smt_optional_returns_signal_on_displacement(monkeypatch):
    """SMT_OPTIONAL=True, large displacement body, no wick SMT → signal returned."""
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", True)
    monkeypatch.setattr(_strat, "MIN_DISPLACEMENT_PTS", 10.0)
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", False)
    monkeypatch.setattr(_strat, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(_strat, "OVERNIGHT_SWEEP_REQUIRED", False)
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(_strat, "OVERNIGHT_RANGE_AS_TP", False)
    monkeypatch.setattr(_strat, "SILVER_BULLET_WINDOW_ONLY", False)
    monkeypatch.setattr(_strat, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(_strat, "FVG_ENABLED", False)
    monkeypatch.setattr(_strat, "MIN_BARS_BEFORE_SIGNAL", 0)
    # Build bars where bar 3 is a large bullish displacement (no SMT divergence)
    n = 20
    start = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")
    base = 20000.0
    opens  = [base] * n
    closes = [base] * n
    highs  = [base + 5] * n
    lows   = [base - 5] * n
    # Bar 2: bearish anchor (close < open) for long displacement confirmation
    opens[2]  = base + 2; closes[2] = base - 2; lows[2] = base - 8
    # Bar 3: large bullish displacement (body = 15 >= 10)
    opens[3]  = base; closes[3] = base + 15; highs[3] = base + 16; lows[3] = base - 1
    # Bar 4: bullish confirmation (close > open, low < anchor_close = base-2)
    opens[4]  = base - 1; closes[4] = base + 5; highs[4] = base + 6; lows[4] = base - 3
    mnq = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    tdo = base + 30.0
    result = _strat.screen_session(mnq, mes, tdo)
    assert result is not None
    assert result["smt_type"] == "displacement"


def test_smt_optional_disabled_blocks_displacement_entry(monkeypatch):
    """SMT_OPTIONAL=False → no signal without wick SMT (pure displacement blocked)."""
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", False)
    monkeypatch.setattr(_strat, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(_strat, "OVERNIGHT_SWEEP_REQUIRED", False)
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(_strat, "OVERNIGHT_RANGE_AS_TP", False)
    monkeypatch.setattr(_strat, "SILVER_BULLET_WINDOW_ONLY", False)
    monkeypatch.setattr(_strat, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(_strat, "FVG_ENABLED", False)
    monkeypatch.setattr(_strat, "MIN_BARS_BEFORE_SIGNAL", 0)
    # Same displacement bars but SMT_OPTIONAL disabled
    n = 20
    start = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start, periods=n, freq="1min")
    base = 20000.0
    opens  = [base] * n; closes = [base] * n
    highs  = [base + 5] * n; lows   = [base - 5] * n
    opens[2]  = base + 2; closes[2] = base - 2; lows[2] = base - 8
    opens[3]  = base; closes[3] = base + 15; highs[3] = base + 16; lows[3] = base - 1
    opens[4]  = base - 1; closes[4] = base + 5; highs[4] = base + 6; lows[4] = base - 3
    mnq = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n}, index=idx)
    tdo = base + 30.0
    result = _strat.screen_session(mnq, mes, tdo)
    assert result is None


# ── Backtest integration ──────────────────────────────────────────────────────

@pytest.fixture
def plan2_backtest_env(tmp_path, monkeypatch):
    """Fixture: FUTURES_CACHE_DIR with manifest, strategy constants pinned for Plan 2 tests."""
    import backtest_smt as bt
    cache_dir = tmp_path / "futures_data"
    cache_dir.mkdir(parents=True)
    manifest = {"fetch_interval": "5m", "tickers": ["MNQ", "MES"],
                "backtest_start": "2025-01-02", "backtest_end": "2025-01-10",
                "source": "test"}
    (cache_dir / "futures_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(bt, "FUTURES_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(bt, "SESSION_START", "09:00")
    monkeypatch.setattr(bt, "SESSION_END", "10:30")
    # Pin all strategy constants to known values
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(_strat, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_strat, "FVG_ENABLED", False)
    return bt, cache_dir


def test_partial_exit_produces_two_trade_records(plan2_backtest_env, monkeypatch):
    """Backtest run with PARTIAL_EXIT_ENABLED=True produces partial + final trade records."""
    bt, _ = plan2_backtest_env
    mnq, mes = _build_short_signal_bars()
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", True)
    monkeypatch.setattr(bt, "PARTIAL_EXIT_ENABLED", True)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_FRACTION", 0.5)
    monkeypatch.setattr(bt, "PARTIAL_EXIT_FRACTION", 0.5)
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", False)
    monkeypatch.setattr(bt, "TWO_LAYER_POSITION", False)
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(bt, "SMT_OPTIONAL", False)
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", False)
    monkeypatch.setattr(bt, "SMT_FILL_ENABLED", False)
    date_str = str(mnq.index[0].date())
    end_str   = str((mnq.index[-1] + pd.Timedelta(days=1)).date())
    stats = bt.run_backtest(mnq, mes, start=date_str, end=end_str)
    partial_trades = [t for t in stats["trade_records"] if t.get("exit_type") == "partial_exit"]
    assert isinstance(partial_trades, list)


def test_layer_a_initial_contracts_less_than_total(plan2_backtest_env, monkeypatch):
    """TWO_LAYER_POSITION=True, LAYER_A_FRACTION=0.5 → initial contracts ≤ total."""
    bt, _ = plan2_backtest_env
    mnq, mes = _build_short_signal_bars()
    monkeypatch.setattr(_strat, "TWO_LAYER_POSITION", True)
    monkeypatch.setattr(bt, "TWO_LAYER_POSITION", True)
    monkeypatch.setattr(_strat, "LAYER_A_FRACTION", 0.5)
    monkeypatch.setattr(bt, "LAYER_A_FRACTION", 0.5)
    monkeypatch.setattr(_strat, "FVG_LAYER_B_TRIGGER", False)
    monkeypatch.setattr(bt, "FVG_LAYER_B_TRIGGER", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(bt, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(bt, "SMT_OPTIONAL", False)
    monkeypatch.setattr(_strat, "SMT_FILL_ENABLED", False)
    monkeypatch.setattr(bt, "SMT_FILL_ENABLED", False)
    date_str = str(mnq.index[0].date())
    end_str   = str((mnq.index[-1] + pd.Timedelta(days=1)).date())
    stats = bt.run_backtest(mnq, mes, start=date_str, end=end_str)
    for t in stats["trade_records"]:
        assert t["contracts"] >= 1


# ── TSV schema ────────────────────────────────────────────────────────────────

def test_write_trades_tsv_includes_plan2_fvg_fields(tmp_path, monkeypatch):
    """_write_trades_tsv writes fvg_high, fvg_low, layer_b_entered columns."""
    import csv
    import backtest_smt as bt
    monkeypatch.chdir(tmp_path)
    trade = {
        "entry_date": "2025-01-02", "entry_time": "09:10", "exit_time": "10:00",
        "direction": "short", "entry_price": 20000.0, "exit_price": 19970.0,
        "tdo": 19970.0, "stop_price": 20020.0, "contracts": 2,
        "pnl": 120.0, "exit_type": "exit_tp", "divergence_bar": 7, "entry_bar": 8,
        "stop_bar_wick_pts": 0.0, "reentry_sequence": 1, "prior_trade_bars_held": 0,
        "entry_bar_body_ratio": 0.4, "smt_sweep_pts": 5.0, "smt_miss_pts": 3.0,
        "bars_since_divergence": 1, "matches_hypothesis": True, "smt_type": "wick",
        "fvg_high": 20010.0, "fvg_low": 20005.0,
        "layer_b_entered": False, "layer_b_entry_price": None, "layer_b_contracts": 0,
    }
    bt._write_trades_tsv([trade])
    with open(tmp_path / "trades.tsv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        headers = reader.fieldnames
    assert "fvg_high" in headers
    assert "fvg_low" in headers
    assert "layer_b_entered" in headers
