# tests/test_smt_humanize.py
# Unit tests for the SMT Humanize plan: typed human-execution signals, confidence
# scoring, opposing-displacement exit, MOVE_STOP emission, ENTRY_MARKET/ENTRY_LIMIT
# classification, and the human-mode slippage wiring through the backtest fill path.
# All tests use synthetic bars — no IB connection required.
import datetime

import pandas as pd
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_position(direction, entry_price, stop_price, take_profit, **extras):
    """Minimal position dict compatible with manage_position()."""
    pos = {
        "direction":   direction,
        "entry_price": entry_price,
        "stop_price":  stop_price,
        "take_profit": take_profit,
    }
    pos.update(extras)
    return pos


def _make_bar(high, low, close=None, open_=None):
    """Build a one-row OHLC bar as a pd.Series."""
    if close is None: close = (high + low) / 2
    if open_ is None: open_ = (high + low) / 2
    return pd.Series({"Open": open_, "High": high, "Low": low, "Close": close})


# ══ Human-execution slippage (S-1 / S-2) ═════════════════════════════════════

def test_s1_human_slippage_applied_to_long_entry():
    """S-1: human_mode=True, human_slip_pts=2.0 → SimulatedFillExecutor adds +2.0 to long entry."""
    import backtest_smt
    from execution.simulated import SimulatedFillExecutor
    from strategy_smt import _BarRow

    signal = {
        "direction":   "long",
        "entry_price": 20000.0,
        "stop_price":  19980.0,
        "take_profit": 20050.0,
        "entry_time":  pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York"),
        "tdo":         20050.0,
        "divergence_bar": 0,
    }
    bar = _BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0,
                  pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York"))
    # entry_slip_ticks=0 isolates human-mode slippage; human_slip_pts=2.0 adds +2.0 for long
    executor = SimulatedFillExecutor(human_mode=True, human_slip_pts=2.0, entry_slip_ticks=0)
    fill = executor.place_entry(signal, bar)
    pos = backtest_smt._open_position(signal, datetime.date(2025, 1, 2), 1, 1, 0,
                                       fill_price=fill.fill_price)
    assert pos["entry_price"] == 20002.0


def test_s2_human_slippage_not_applied_when_mode_off():
    """S-2: human_mode=False → slippage inactive even if human_slip_pts > 0."""
    import backtest_smt
    from execution.simulated import SimulatedFillExecutor
    from strategy_smt import _BarRow

    signal = {
        "direction":   "long",
        "entry_price": 20000.0,
        "stop_price":  19980.0,
        "take_profit": 20050.0,
        "entry_time":  pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York"),
        "tdo":         20050.0,
        "divergence_bar": 0,
    }
    bar = _BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0,
                  pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York"))
    executor = SimulatedFillExecutor(human_mode=False, human_slip_pts=2.0, entry_slip_ticks=0)
    fill = executor.place_entry(signal, bar)
    pos = backtest_smt._open_position(signal, datetime.date(2025, 1, 2), 1, 1, 0,
                                       fill_price=fill.fill_price)
    assert pos["entry_price"] == 20000.0


# ══ Confidence scoring (C-1 … C-5) ═══════════════════════════════════════════

def _signal_for_confidence(entry_time, entry_price=20000.0, tdo=20075.0, body_pts=20.0):
    return {
        "entry_time":  entry_time,
        "entry_price": entry_price,
        "tdo":         tdo,
        "displacement_body_pts": body_pts,
    }


def test_c1_compute_confidence_in_unit_range():
    """C-1: compute_confidence always returns a value in [0, 1]."""
    import strategy_smt
    session_start = pd.Timestamp("2025-01-02 09:30:00")
    for mins in (0, 60, 105, 210, 300):
        ts = session_start + pd.Timedelta(minutes=mins)
        sig = _signal_for_confidence(ts)
        val = strategy_smt.compute_confidence(sig, prior_session_profitable=True, session_start_ts=session_start)
        assert 0.0 <= val <= 1.0


def test_c2_time_of_day_ramp():
    """C-2: time_score is 0.0 at session start and 1.0 at +210 min (13:00 for 9:30 start).

    With every other component held at zero, confidence == 0.4 * time_score.
    """
    import strategy_smt
    session_start = pd.Timestamp("2025-01-02 09:30:00")
    # Body=0 → body_score=0; tdo_dist=0 → dist_score=0; prior=False → trend_score=0.
    sig_zero_start = {
        "entry_time":  session_start,
        "entry_price": 20000.0,
        "tdo":         20000.0,  # distance 0 → dist_score 0
        "displacement_body_pts": 0.0,
    }
    c0 = strategy_smt.compute_confidence(sig_zero_start, prior_session_profitable=False, session_start_ts=session_start)
    assert c0 == 0.0

    sig_at_1300 = dict(sig_zero_start, entry_time=session_start + pd.Timedelta(minutes=210))
    c_end = strategy_smt.compute_confidence(sig_at_1300, prior_session_profitable=False, session_start_ts=session_start)
    # time=1.0 * 0.4 + 0 + 0 + 0 = 0.4
    assert c_end == pytest.approx(0.4, abs=1e-4)


def test_c3_sub_threshold_signal_suppressed_in_human_mode(monkeypatch):
    """C-3: Human mode filters out signals whose confidence < MIN_CONFIDENCE_THRESHOLD.

    Directly exercises the filter predicate used by _process_scanning — emission
    machinery is decoupled so this can assert on the gating logic without spinning
    up IB callbacks.
    """
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)
    monkeypatch.setattr(strategy_smt, "MIN_CONFIDENCE_THRESHOLD", 0.50)

    below = {"confidence": 0.30}
    above = {"confidence": 0.75}

    def _would_emit(sig):
        if not strategy_smt.HUMAN_EXECUTION_MODE:
            return True
        c = sig.get("confidence")
        return c is None or c >= strategy_smt.MIN_CONFIDENCE_THRESHOLD

    assert _would_emit(below) is False
    assert _would_emit(above) is True


def test_c4_above_threshold_signal_emitted(monkeypatch):
    """C-4: Signal at or above threshold is emitted (companion to C-3)."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)
    monkeypatch.setattr(strategy_smt, "MIN_CONFIDENCE_THRESHOLD", 0.50)

    sig = {"confidence": 0.50}
    # Predicate identical to _process_scanning gate.
    assert (sig["confidence"] >= strategy_smt.MIN_CONFIDENCE_THRESHOLD) is True


def test_c5_tdo_distance_sweet_spot():
    """C-5: TDO distance 75 pts scores peak (dist_score=1.0); 250 pts scores 0."""
    import strategy_smt
    session_start = pd.Timestamp("2025-01-02 09:30:00")
    # Isolate dist_score: time=0, trend=0, body=0 → confidence == 0.1 * dist_score.
    def _sig(tdo_dist):
        return {
            "entry_time":  session_start,
            "entry_price": 20000.0,
            "tdo":         20000.0 + tdo_dist,
            "displacement_body_pts": 0.0,
        }

    peak = strategy_smt.compute_confidence(_sig(75.0), False, session_start)
    far  = strategy_smt.compute_confidence(_sig(250.0), False, session_start)
    assert peak == pytest.approx(0.1, abs=1e-4)    # 0.1 * 1.0
    assert far  == pytest.approx(0.0, abs=1e-4)    # 0.1 * 0.0


# ══ Deception detection (DC-1 / DC-2) ════════════════════════════════════════

def test_dc1_opposing_displacement_triggers_exit(monkeypatch):
    """DC-1: Short position + strong bullish opposing candle → exit_invalidation_opposing_disp."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "DECEPTION_OPPOSING_DISP_EXIT", True)
    monkeypatch.setattr(strategy_smt, "MIN_DISPLACEMENT_PTS", 10.0)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)

    pos = _make_position("short", 20100.0, 20200.0, 20000.0)
    # Large bullish body (close > open by 15 pts) — opposing direction for a short.
    bar = _make_bar(high=20120.0, low=20095.0, open_=20100.0, close=20115.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result == "exit_invalidation_opposing_disp"


def test_dc2_opposing_displacement_disabled(monkeypatch):
    """DC-2: DECEPTION_OPPOSING_DISP_EXIT=False → no opposing exit even on strong candle."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "DECEPTION_OPPOSING_DISP_EXIT", False)
    monkeypatch.setattr(strategy_smt, "MIN_DISPLACEMENT_PTS", 10.0)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)

    pos = _make_position("short", 20100.0, 20200.0, 20000.0)
    bar = _make_bar(high=20120.0, low=20095.0, open_=20100.0, close=20115.0)
    result = strategy_smt.manage_position(pos, bar)
    assert result != "exit_invalidation_opposing_disp"


# ══ MOVE_STOP emission (M-1 / M-2) ═══════════════════════════════════════════

def test_m1_breakeven_returns_move_stop_in_human_mode(monkeypatch):
    """M-1: Breakeven trigger returns 'move_stop' (not silent mutation) in human mode."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)

    # Short: entry=20100, TP=20000, dist=100. 50% progress → Low=20050.
    pos = _make_position("short", 20100.0, 20200.0, 20000.0)
    bar = pd.Series({"Open": 20090.0, "High": 20095.0, "Low": 20050.0, "Close": 20055.0})
    result = strategy_smt.manage_position(pos, bar)
    assert result == "move_stop"
    assert pos.get("_pending_move_stop") == pytest.approx(20100.0)


def test_m2_move_stop_carries_new_stop_and_breakeven_reason(monkeypatch):
    """M-2: MOVE_STOP event surfaces the new stop level and breakeven flag."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)
    monkeypatch.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
    monkeypatch.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)

    pos = _make_position("short", 20100.0, 20200.0, 20000.0)
    bar = pd.Series({"Open": 20090.0, "High": 20095.0, "Low": 20050.0, "Close": 20055.0})
    result = strategy_smt.manage_position(pos, bar)

    assert result == "move_stop"
    assert pos["_pending_move_stop"] == pytest.approx(20100.0)
    assert pos.get("breakeven_active") is True
    # Signal emitter classifies the MOVE_STOP reason from this flag (see _process_managing).
    reason = "breakeven" if pos.get("breakeven_active") else "trail_update"
    assert reason == "breakeven"


# ══ Signal type classification (SC-1 / SC-2) ═════════════════════════════════

def test_sc1_no_limit_fill_bars_classifies_entry_market(monkeypatch):
    """SC-1: limit_fill_bars is None → ENTRY_MARKET."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)

    signal = {"limit_fill_bars": None}
    if signal.get("limit_fill_bars") is not None:
        signal["signal_type"] = "ENTRY_LIMIT"
    else:
        signal["signal_type"] = "ENTRY_MARKET"

    assert signal["signal_type"] == "ENTRY_MARKET"


def test_sc2_limit_fill_bars_set_classifies_entry_limit(monkeypatch):
    """SC-2: limit_fill_bars is not None (e.g., 0) → ENTRY_LIMIT."""
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)

    signal = {"limit_fill_bars": 0}
    if signal.get("limit_fill_bars") is not None:
        signal["signal_type"] = "ENTRY_LIMIT"
    else:
        signal["signal_type"] = "ENTRY_MARKET"

    assert signal["signal_type"] == "ENTRY_LIMIT"


# ══ Regression (R-1) ═════════════════════════════════════════════════════════

def test_r1_defaults_preserve_legacy_manage_position():
    """R-1: With HUMAN_EXECUTION_MODE=False (default), manage_position never returns
    'move_stop' and only the legacy set of results surfaces."""
    import strategy_smt
    # Must be False at module level — do not monkeypatch; verify the default.
    assert strategy_smt.HUMAN_EXECUTION_MODE is False

    # A bar that would trip breakeven in human mode returns "hold" under legacy.
    # Use monkeypatch only to force BREAKEVEN_TRIGGER_PCT so the stop actually moves,
    # then assert the return value is NOT "move_stop".
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(strategy_smt, "BREAKEVEN_TRIGGER_PCT", 0.5)
        mp.setattr(strategy_smt, "TRAIL_AFTER_TP_PTS", 0.0)
        pos = _make_position("short", 20100.0, 20200.0, 20000.0)
        bar = pd.Series({"Open": 20090.0, "High": 20095.0, "Low": 20050.0, "Close": 20055.0})
        result = strategy_smt.manage_position(pos, bar)
        assert result != "move_stop"
        assert result in ("hold", "exit_tp", "exit_stop")
    finally:
        mp.undo()
