# tests/test_smt_limit_lifecycle.py
# Unit + integration tests for the SMT limit-entry lifecycle events.
# Covers: LIMIT_PLACED, LIMIT_MOVED, LIMIT_CANCELLED, LIMIT_FILLED, LIMIT_EXPIRED,
# signal_type tag, MIN_TARGET_PTS fallback, formatter output, JSON schema, full lifecycle.
# No IB connection — synthetic bars and monkeypatched config only.
import datetime
import json

import numpy as np
import pandas as pd
import pytest


# ── Price constants ───────────────────────────────────────────────────────────

B = 20000.0    # base MNQ price
BUFFER = 5.0   # default LIMIT_ENTRY_BUFFER_PTS for limit-mode tests


# ── Session builder helpers ───────────────────────────────────────────────────

def _make_short_session(n=12):
    """Minimal SHORT SMT divergence session (mirrors test_strategy_refactor.py).

    Bar 3: bullish MNQ (anchor_close = B+1.0 for SHORT)
    Bar 4: divergence  — MES.High sweeps session high; MNQ.High fails
    Bar 5: confirmation — bearish MNQ, high > anchor
    """
    ts = pd.date_range("2025-01-02 09:03", periods=n, freq="1min", tz="America/New_York")
    opens  = np.full(n, B)
    highs  = np.full(n, B + 2.0)
    lows   = np.full(n, B - 2.0)
    closes = np.full(n, B)
    vols   = np.full(n, 1000.0)
    opens[3] = B - 1.0; closes[3] = B + 1.0
    highs[4] = B + 1.5; opens[4] = B; closes[4] = B
    opens[5] = B + 3.0; closes[5] = B - 5.0; highs[5] = B + 4.0; lows[5] = B - 6.0
    m_base = B / 2
    mh = np.full(n, m_base + 1.0)
    ml = np.full(n, m_base - 1.0)
    mc = np.full(n, m_base)
    mh[4] = m_base + 2.0
    mnq_r = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=ts,
    ).reset_index(drop=True)
    mes_r = pd.DataFrame(
        {"Open": np.full(n, m_base), "High": mh, "Low": ml, "Close": mc, "Volume": vols},
        index=ts,
    ).reset_index(drop=True)
    c4 = {
        "mes_h": m_base + 1.0, "mes_l": m_base - 1.0,
        "mnq_h": B + 2.0,      "mnq_l": B - 2.0,
        "mes_ch": m_base,       "mes_cl": m_base,
        "mnq_ch": B + 1.0,      "mnq_cl": B - 1.0,
    }
    c5 = {
        "mes_h": m_base + 2.0, "mes_l": m_base - 1.0,
        "mnq_h": B + 2.0,      "mnq_l": B - 2.0,
        "mes_ch": m_base,       "mes_cl": m_base,
        "mnq_ch": B + 1.0,      "mnq_cl": B - 1.0,
    }
    return mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5


def _ctx():
    """SessionContext for SHORT tests: TDO far below entry (100 pts)."""
    import strategy_smt as strat
    return strat.SessionContext(
        day=datetime.date(2025, 1, 2), tdo=B - 100.0, bar_seconds=60.0,
    )


def _call(strat, state, ctx, i, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, cache):
    """Single process_scan_bar call for bar i."""
    bar = strat._BarRow(
        float(opens[i]), float(highs[i]), float(lows[i]),
        float(closes[i]), float(vols[i]), ts=ts[i],
    )
    return strat.process_scan_bar(
        state, ctx, i, bar, mnq_r, mes_r, cache,
        run_ses_high=-float("inf"), run_ses_low=float("inf"),
        ts=ts[i], min_signal_ts=ts[0],
        mnq_opens=opens, mnq_highs=highs, mnq_lows=lows,
        mnq_closes=closes, mnq_vols=vols,
        mes_highs=mh, mes_lows=ml, mes_closes=mc,
    )


def _patch_base(monkeypatch, strat):
    """Disable all strategy filters that aren't under test."""
    for attr, val in [
        ("LIMIT_ENTRY_BUFFER_PTS",       None),
        ("LIMIT_EXPIRY_SECONDS",         None),
        ("MAX_TDO_DISTANCE_PTS",         999.0),
        ("MIN_STOP_POINTS",              0.0),
        ("TDO_VALIDITY_CHECK",           False),
        ("MIN_DIV_SCORE",                0.0),
        ("HYPOTHESIS_FILTER",            False),
        ("HYPOTHESIS_INVALIDATION_PTS",  999.0),
        ("ALWAYS_REQUIRE_CONFIRMATION",  False),
        ("EXPANDED_REFERENCE_LEVELS",    False),
        ("HTF_VISIBILITY_REQUIRED",      False),
        ("OVERNIGHT_SWEEP_REQUIRED",     False),
        ("SILVER_BULLET_WINDOW_ONLY",    False),
        ("MIN_RR_FOR_TARGET",            0.0),
        ("MIN_TARGET_PTS",               0.0),
        ("CONFIRMATION_WINDOW_BARS",     1),
        ("DISPLACEMENT_STOP_MODE",       False),
        ("PARTIAL_EXIT_ENABLED",         False),
        ("SMT_OPTIONAL",                 False),
        ("SMT_FILL_ENABLED",             False),
        ("MIDNIGHT_OPEN_AS_TP",          False),
        ("REPLACE_THRESHOLD",            9999.0),
        ("STRUCTURAL_STOP_MODE",         False),
        ("LIMIT_RATIO_THRESHOLD",        None),
        ("MOVE_LIMIT_MIN_GAP_BARS",      0),
        ("MAX_REENTRY_COUNT",            999),
        ("MIN_TDO_DISTANCE_PTS",         0.0),
        ("SYMMETRIC_SMT_ENABLED",        False),
    ]:
        monkeypatch.setattr(strat, attr, val)


def _patch_limit(monkeypatch, strat, buffer=BUFFER, expiry=120):
    """Enable limit mode on top of base patches."""
    _patch_base(monkeypatch, strat)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", buffer)
    monkeypatch.setattr(strat, "LIMIT_EXPIRY_SECONDS", expiry)


# ══ Module-level constants ════════════════════════════════════════════════════

def test_evt_constants_defined():
    """All EVT_* lifecycle constants are defined with expected string values."""
    import strategy_smt as strat
    assert strat.EVT_SIGNAL          == "signal"
    assert strat.EVT_LIMIT_PLACED    == "limit_placed"
    assert strat.EVT_LIMIT_MOVED     == "limit_moved"
    assert strat.EVT_LIMIT_CANCELLED == "limit_cancelled"
    assert strat.EVT_LIMIT_EXPIRED   == "limit_expired"
    assert strat.EVT_LIMIT_FILLED    == "limit_filled"


def test_scan_state_new_slots_initialized():
    """ScanState has last_limit_move_bar_idx=-999 and last_limit_signal_snapshot=None after init."""
    import strategy_smt as strat
    s = strat.ScanState()
    assert s.last_limit_move_bar_idx == -999
    assert s.last_limit_signal_snapshot is None


def test_scan_state_new_slots_after_reset():
    """ScanState.reset() reinitializes both new lifecycle slots correctly."""
    import strategy_smt as strat
    s = strat.ScanState()
    s.last_limit_move_bar_idx = 42
    s.last_limit_signal_snapshot = {"direction": "short"}
    s.reset()
    assert s.last_limit_move_bar_idx == -999
    assert s.last_limit_signal_snapshot is None


def test_move_limit_min_gap_bars_default():
    """MOVE_LIMIT_MIN_GAP_BARS module default is 0 (no rate limiting by default)."""
    import strategy_smt as strat
    assert strat.MOVE_LIMIT_MIN_GAP_BARS == 0


def test_default_min_target_pts_is_15():
    """MIN_TARGET_PTS module default is 15.0."""
    import strategy_smt as strat
    assert strat.MIN_TARGET_PTS == pytest.approx(15.0)


def test_default_min_rr_is_1p5():
    """MIN_RR_FOR_TARGET module default is 1.5."""
    import strategy_smt as strat
    assert strat.MIN_RR_FOR_TARGET == pytest.approx(1.5)


# ══ Item 2: LIMIT_PLACED at divergence ═══════════════════════════════════════

def test_limit_placed_on_divergence_idle_transition(monkeypatch):
    """IDLE→WAITING_FOR_ENTRY emits limit_placed when LIMIT_ENTRY_BUFFER_PTS is set."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result is not None
    assert result["type"] == strat.EVT_LIMIT_PLACED
    assert state.scan_state == "WAITING_FOR_ENTRY"
    assert state.last_limit_signal_snapshot is not None


def test_limit_placed_entry_price_is_anchor_minus_buffer_short(monkeypatch):
    """For SHORT: entry_price in limit_placed = anchor_close - LIMIT_ENTRY_BUFFER_PTS."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    sig = result["signal"]
    # anchor_close = closes[3] = B + 1.0; entry = anchor - buffer
    assert sig["entry_price"] == pytest.approx((B + 1.0) - BUFFER, abs=0.01)
    assert sig["direction"] == "short"


def test_limit_placed_payload_includes_entry_stop_tp(monkeypatch):
    """LIMIT_PLACED signal contains numeric entry_price, stop_price, and take_profit."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    sig = result["signal"]
    for key in ("entry_price", "stop_price", "take_profit"):
        assert key in sig, f"Missing key: {key}"
        assert isinstance(sig[key], (int, float)), f"{key} not numeric"


def test_limit_placed_signal_type_is_entry_limit(monkeypatch):
    """Preliminary signal in limit_placed carries signal_type=ENTRY_LIMIT."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result["signal"]["signal_type"] == "ENTRY_LIMIT"


def test_limit_placed_suppressed_when_tp_selection_fails(monkeypatch):
    """MIN_TARGET_PTS=99999 makes all draws fail: no limit_placed; divergence is preserved."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 99999.0)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result is None
    assert state.scan_state == "WAITING_FOR_ENTRY"   # divergence preserved
    assert state.pending_direction == "short"


def test_market_mode_no_limit_placed_on_divergence(monkeypatch):
    """LIMIT_ENTRY_BUFFER_PTS=None (market mode): divergence returns None, no lifecycle event."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, _ = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    result = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result is None
    assert state.scan_state == "WAITING_FOR_ENTRY"


# ══ Item 3: LIMIT_FILLED ══════════════════════════════════════════════════════

def test_limit_filled_emitted_on_forward_fill(monkeypatch):
    """Forward limit mode: fill bar returns lifecycle_batch with [limit_filled, signal]."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    assert r5 is None
    assert state.scan_state == "WAITING_FOR_LIMIT_FILL"

    # Fill bar: short fills when Low <= entry_price
    entry_price = state.pending_limit_signal["entry_price"]
    lows[6] = entry_price - 2.0; highs[6] = B; opens[6] = B; closes[6] = entry_price - 1.0

    r6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert r6 is not None
    assert r6["type"] == "lifecycle_batch"
    types = [e["type"] for e in r6["events"]]
    assert "limit_filled" in types
    assert "signal" in types
    # limit_filled must come before signal
    assert types.index("limit_filled") < types.index("signal")


def test_limit_filled_same_bar_when_expiry_none(monkeypatch):
    """LIMIT_EXPIRY_SECONDS=None: confirmation bar returns lifecycle_batch([limit_filled, signal])."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", BUFFER)
    # LIMIT_EXPIRY_SECONDS stays None → same-bar fill path
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert r5 is not None
    assert r5["type"] == "lifecycle_batch"
    types = [e["type"] for e in r5["events"]]
    assert "limit_filled" in types
    assert "signal" in types


def test_limit_filled_payload_time_in_queue_secs(monkeypatch):
    """LIMIT_FILLED time_in_queue_secs = limit_bars_elapsed * bar_seconds (60s bars)."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()   # bar_seconds = 60.0

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    entry_price = state.pending_limit_signal["entry_price"]
    lows[6] = entry_price - 2.0; highs[6] = B; opens[6] = B; closes[6] = entry_price - 1.0

    r6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    filled_evt = next(e for e in r6["events"] if e["type"] == "limit_filled")
    # 1 bar elapsed × 60 s/bar = 60 s
    assert filled_evt["time_in_queue_secs"] == pytest.approx(60.0)


def test_limit_filled_original_limit_price_equals_entry_price(monkeypatch):
    """Same-bar fill: filled_price = buffered limit entry; original_limit_price = anchor close."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", BUFFER)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert r5["type"] == "lifecycle_batch"
    filled = next(e for e in r5["events"] if e["type"] == "limit_filled")
    # filled_price = anchor_close - BUFFER; original_limit_price = anchor_close (not the buffered price)
    assert filled["filled_price"] == pytest.approx((B + 1.0) - BUFFER)
    assert filled["original_limit_price"] == pytest.approx(B + 1.0)


# ══ Item 4: CANCEL_LIMIT / LIMIT_EXPIRED ══════════════════════════════════════

def test_cancel_on_hypothesis_invalidation(monkeypatch):
    """Adverse move past HYPOTHESIS_INVALIDATION_PTS → limit_cancelled with correct reason."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    monkeypatch.setattr(strat, "HYPOTHESIS_INVALIDATION_PTS", 3.0)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert r4 is not None and r4["type"] == "limit_placed"
    assert state.last_limit_signal_snapshot is not None
    discovery_price = state.pending_discovery_price

    # Bar 5: strong bullish move — adverse > 3 pts for SHORT
    highs[5] = discovery_price + 10.0
    lows[5] = B - 1.0; opens[5] = B + 5.0; closes[5] = B + 8.0

    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert r5 is not None
    assert r5["type"] == strat.EVT_LIMIT_CANCELLED
    assert r5.get("reason") == "hypothesis_invalidated"
    assert state.scan_state == "IDLE"
    assert state.last_limit_signal_snapshot is None


def test_cancel_on_hypothesis_filter_miss(monkeypatch):
    """Confirmation bar that fails HYPOTHESIS_FILTER → limit_cancelled with reason='hypothesis_filter_miss'."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    monkeypatch.setattr(strat, "HYPOTHESIS_FILTER", True)
    # Always mark signal as non-matching
    monkeypatch.setattr(strat, "_annotate_hypothesis",
                        lambda sig, hyp_ctx, hyp_dir: sig.update({"matches_hypothesis": False}))
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert r4 is not None and r4["type"] == "limit_placed"

    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert r5 is not None
    assert r5["type"] == strat.EVT_LIMIT_CANCELLED
    assert r5.get("reason") == "hypothesis_filter_miss"
    assert state.scan_state == "IDLE"


def test_cancel_on_max_reentry_fallthrough(monkeypatch):
    """REENTRY_ELIGIBLE + reentry_count >= MAX_REENTRY_COUNT → limit_cancelled(reason=max_reentry)."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    monkeypatch.setattr(strat, "MAX_REENTRY_COUNT", 1)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert state.last_limit_signal_snapshot is not None
    # Force into REENTRY_ELIGIBLE with count at the limit
    state.scan_state = "REENTRY_ELIGIBLE"
    state.reentry_count = 1

    result = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert result is not None
    assert result["type"] == strat.EVT_LIMIT_CANCELLED
    assert result.get("reason") == "max_reentry"
    assert state.scan_state == "IDLE"


def test_move_limit_rate_limited_suppresses_event_but_updates_state(monkeypatch):
    """MOVE_LIMIT_MIN_GAP_BARS=5: replacement within 3 bars suppresses event; state still updated."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    monkeypatch.setattr(strat, "MOVE_LIMIT_MIN_GAP_BARS", 5)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    # Divergence at bar 4 — LIMIT_PLACED emitted; last_limit_move_bar_idx = 4
    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert state.last_limit_move_bar_idx == 4

    # Mock detect_smt_divergence so bar 6 looks like a new same-dir divergence
    real_detect = strat.detect_smt_divergence
    def _patched_detect(mes_df, mnq_df, bar_idx, offset, _cached=None, **kwargs):
        if bar_idx == 6:
            return ("short", 1.5, 0.5, "wick", B + 2.0)
        return real_detect(mes_df, mnq_df, bar_idx, offset, _cached=_cached, **kwargs)
    monkeypatch.setattr(strat, "detect_smt_divergence", _patched_detect)
    monkeypatch.setattr(strat, "find_anchor_close", lambda *a, **kw: B + 0.5)

    old_snap = dict(state.last_limit_signal_snapshot)
    result_bar6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    # bar 6 - bar 4 = 2 < MOVE_LIMIT_MIN_GAP_BARS=5 → event suppressed (no return value)
    assert result_bar6 is None, "MOVE_LIMIT must be rate-limited"
    # State snapshot is updated even when emission is suppressed
    assert state.last_limit_signal_snapshot is not None
    assert state.last_limit_signal_snapshot["entry_price"] != old_snap["entry_price"]


def test_move_limit_emitted_on_same_direction_replacement(monkeypatch):
    """Same-direction replacement with different anchor → limit_moved with old/new prices."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert r4 is not None and r4["type"] == "limit_placed"
    old_entry = state.last_limit_signal_snapshot["entry_price"]

    # bar 6: force a new SHORT divergence with a different anchor (→ different entry_price)
    real_detect = strat.detect_smt_divergence
    def _patched_detect(mes_df, mnq_df, bar_idx, offset, _cached=None, **kwargs):
        if bar_idx == 6:
            return ("short", 2.0, 1.0, "wick", B + 3.0)
        return real_detect(mes_df, mnq_df, bar_idx, offset, _cached=_cached, **kwargs)
    monkeypatch.setattr(strat, "detect_smt_divergence", _patched_detect)
    # New anchor at B+3.0 → new entry B+3.0 - BUFFER = different from old_entry
    monkeypatch.setattr(strat, "find_anchor_close", lambda *a, **kw: B + 3.0)

    result = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert result is not None
    assert result["type"] == strat.EVT_LIMIT_MOVED
    assert result["old_signal"]["entry_price"] == pytest.approx(old_entry)
    assert result["new_signal"]["entry_price"] == pytest.approx((B + 3.0) - BUFFER)


def test_move_limit_not_emitted_when_entry_unchanged(monkeypatch):
    """Same-direction replacement with identical anchor → prices unchanged → no EVT_LIMIT_MOVED."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert r4 is not None and r4["type"] == "limit_placed"

    # bar 6: same-direction SHORT divergence but with the SAME anchor (B+1.0) → identical entry
    real_detect = strat.detect_smt_divergence
    def _patched_detect(mes_df, mnq_df, bar_idx, offset, _cached=None, **kwargs):
        if bar_idx == 6:
            return ("short", 2.0, 1.0, "wick", B + 1.0)
        return real_detect(mes_df, mnq_df, bar_idx, offset, _cached=_cached, **kwargs)
    monkeypatch.setattr(strat, "detect_smt_divergence", _patched_detect)
    monkeypatch.setattr(strat, "find_anchor_close", lambda *a, **kw: B + 1.0)
    # Bullish close so bar 6 is not a confirmation bar
    highs[6] = B + 2.0; lows[6] = B - 1.0; opens[6] = B; closes[6] = B + 1.0

    result = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert result is None  # prices unchanged → no LIMIT_MOVED emitted


def test_cancel_and_place_on_opposite_direction_replacement(monkeypatch):
    """Opposite-direction replacement → lifecycle_batch([limit_cancelled, limit_placed])."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert r4 is not None and r4["type"] == "limit_placed"
    assert state.last_limit_signal_snapshot["direction"] == "short"

    # Force a LONG divergence at bar 6 that beats the existing short score
    real_detect = strat.detect_smt_divergence
    def _patched_detect(mes_df, mnq_df, bar_idx, offset, _cached=None, **kwargs):
        if bar_idx == 6:
            return ("long", 5.0, 5.0, "wick", B - 3.0)
        return real_detect(mes_df, mnq_df, bar_idx, offset, _cached=_cached, **kwargs)
    monkeypatch.setattr(strat, "detect_smt_divergence", _patched_detect)
    monkeypatch.setattr(strat, "find_anchor_close", lambda *a, **kw: B - 3.0)
    monkeypatch.setattr(strat, "REPLACE_THRESHOLD", 0.0)  # allow any opposite-dir replacement
    # TDO=B-100 is invalid for a LONG TP, so bypass preliminary signal computation
    monkeypatch.setattr(strat, "_build_preliminary_limit_signal",
        lambda st, ctx_, ts_, h, l: {
            "direction": st.pending_direction,
            "entry_price": (B - 3.0) + BUFFER,
            "stop_price": B - 20.0,
            "take_profit": B + 50.0,
            "signal_type": "ENTRY_LIMIT",
            "limit_fill_bars": None,
        }
    )

    result = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert result is not None
    assert result["type"] == "lifecycle_batch"
    types = [e["type"] for e in result["events"]]
    assert types == [strat.EVT_LIMIT_CANCELLED, strat.EVT_LIMIT_PLACED]
    assert result["events"][0]["reason"] == "direction_replaced"
    assert result["events"][1]["signal"]["direction"] == "long"


def test_limit_expired_emitted_on_timeout(monkeypatch):
    """Forward limit: expiry after limit_max_bars returns limit_expired with missed_move_pts."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat, expiry=60)   # 60 s / 60 s-per-bar = 1 bar
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    assert state.scan_state == "WAITING_FOR_LIMIT_FILL"
    entry_price = state.pending_limit_signal["entry_price"]

    # Bar 6: price stays ABOVE entry — no fill for short; hits timeout (limit_bars_elapsed=1 >= max=1)
    lows[6] = entry_price + 2.0; highs[6] = B + 10.0; opens[6] = B + 8.0; closes[6] = B + 9.0

    result = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert result is not None
    assert result["type"] == strat.EVT_LIMIT_EXPIRED
    # Price stayed above entry the whole time — no partial approach → missed_move is 0
    assert result["limit_missed_move"] == pytest.approx(0.0)
    assert state.scan_state == "IDLE"


def test_limit_expired_backward_compat_string():
    """EVT_LIMIT_EXPIRED == 'limit_expired' (string form used as backward-compat key)."""
    import strategy_smt as strat
    assert strat.EVT_LIMIT_EXPIRED == "limit_expired"


# ══ Item 5: signal_type tag ═══════════════════════════════════════════════════

def test_signal_tagged_with_none_fill_bars_in_market_mode(monkeypatch):
    """LIMIT_ENTRY_BUFFER_PTS=None: signal limit_fill_bars is None (market-mode indicator)."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    result = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert result is not None and result["type"] == "signal"
    assert result.get("limit_fill_bars") is None


def test_signal_tagged_limit_fill_bars_zero_on_same_bar_fill(monkeypatch):
    """Same-bar limit mode: signal inside lifecycle_batch has limit_fill_bars=0."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", BUFFER)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert r5["type"] == "lifecycle_batch"
    sig_evt = next(e for e in r5["events"] if e["type"] == "signal")
    assert sig_evt.get("limit_fill_bars") == 0


def test_signal_tagged_limit_fill_bars_positive_on_forward_fill(monkeypatch):
    """Forward limit fill: signal inside lifecycle_batch has limit_fill_bars > 0."""
    import strategy_smt as strat
    _patch_limit(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    entry_price = state.pending_limit_signal["entry_price"]
    lows[6] = entry_price - 2.0; highs[6] = B; opens[6] = B; closes[6] = entry_price - 1.0
    r6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))

    assert r6["type"] == "lifecycle_batch"
    sig_evt = next(e for e in r6["events"] if e["type"] == "signal")
    assert sig_evt.get("limit_fill_bars") is not None and sig_evt["limit_fill_bars"] > 0


def test_format_signal_line_includes_entry_market_tag():
    """_format_signal_line output contains 'type ENTRY_MARKET' when signal_type=ENTRY_MARKET."""
    import signal_smt
    ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    signal = {
        "direction": "short", "entry_price": 20000.0, "stop_price": 20020.0,
        "take_profit": 19950.0, "entry_time": ts, "signal_type": "ENTRY_MARKET",
    }
    line = signal_smt._format_signal_line(ts, signal, 20000.0)
    assert "type ENTRY_MARKET" in line


def test_format_signal_line_includes_entry_limit_tag():
    """_format_signal_line output contains 'type ENTRY_LIMIT' when signal_type=ENTRY_LIMIT."""
    import signal_smt
    ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    signal = {
        "direction": "short", "entry_price": 20000.0, "stop_price": 20020.0,
        "take_profit": 19950.0, "entry_time": ts, "signal_type": "ENTRY_LIMIT",
    }
    line = signal_smt._format_signal_line(ts, signal, 20000.0)
    assert "type ENTRY_LIMIT" in line


# ══ Item 8: MIN_TARGET_PTS fallback ══════════════════════════════════════════

def test_select_draw_returns_valid_list_sorted_by_distance():
    """select_draw_on_liquidity returns 5-tuple; valid_draws is sorted ascending by distance."""
    from strategy_smt import select_draw_on_liquidity
    draws = {
        "tdo":       19950.0,   # 50 pts from entry
        "overnight": 19970.0,   # 30 pts from entry
        "session":   19960.0,   # 40 pts from entry
    }
    _, _, _, _, valid = select_draw_on_liquidity(
        "short", 20000.0, 20020.0, draws, min_rr=0.0, min_pts=15.0
    )
    assert len(valid) == 3
    dists = [v[2] for v in valid]
    assert dists == sorted(dists)


def test_tp_below_min_target_pts_uses_next_valid_draw():
    """Nearest draw below MIN_TARGET_PTS is skipped; next draw (>= min_pts) is selected."""
    from strategy_smt import select_draw_on_liquidity
    draws = {
        "tdo":       19990.0,   # 10 pts — below min_pts=15
        "overnight": 19970.0,   # 30 pts — passes
    }
    pri_name, pri_price, _, _, valid = select_draw_on_liquidity(
        "short", 20000.0, 20020.0, draws, min_rr=0.0, min_pts=15.0
    )
    assert pri_name == "overnight"
    assert pri_price == pytest.approx(19970.0)
    assert len(valid) == 1


def test_no_valid_draw_preserves_divergence_and_state(monkeypatch):
    """_build_draws_and_select returns None TP: process_scan_bar returns None; divergence preserved."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session()
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert state.scan_state == "WAITING_FOR_ENTRY"
    div_idx = state.divergence_bar_idx

    # Force TP selection to fail on the confirmation bar
    monkeypatch.setattr(strat, "_build_draws_and_select",
                        lambda *a, **kw: (None, None, None, None, []))

    result = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert result is None
    assert state.scan_state == "WAITING_FOR_ENTRY"
    assert state.divergence_bar_idx == div_idx   # divergence preserved


def test_later_confirmation_fires_signal_after_valid_draw_exists(monkeypatch):
    """Blocked draws at bar 5 preserve state; valid draw on bar 6 fires signal (retry path)."""
    import strategy_smt as strat
    _patch_base(monkeypatch, strat)
    n = 12
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
    state = strat.ScanState()
    ctx = _ctx()

    _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    div_idx = state.divergence_bar_idx

    # Phase 1: block TP selection at bar 5 → no signal, divergence preserved
    real_bds = strat._build_draws_and_select
    monkeypatch.setattr(strat, "_build_draws_and_select",
                        lambda *a, **kw: (None, None, None, None, []))
    result5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    assert result5 is None
    assert state.scan_state == "WAITING_FOR_ENTRY"
    assert state.divergence_bar_idx == div_idx

    # Phase 2: restore draws; bar 6 as a confirmation bar → signal fires
    monkeypatch.setattr(strat, "_build_draws_and_select", real_bds)
    highs[6] = B + 4.0; lows[6] = B - 6.0; opens[6] = B + 3.0; closes[6] = B - 5.0
    result6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))
    assert result6 is not None
    assert result6["type"] == "signal"
    assert state.scan_state == "IDLE"


# ══ signal_smt.py formatter tests ════════════════════════════════════════════

def _ts():
    return pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")


def _sig():
    return {"direction": "short", "entry_price": 20000.0, "stop_price": 20020.0, "take_profit": 19950.0}


def test_format_limit_placed_line_content_and_ascii():
    """_format_limit_placed_line contains key fields and uses ASCII-safe characters only."""
    import signal_smt
    line = signal_smt._format_limit_placed_line(_ts(), _sig())
    assert "LIMIT_PLACED" in line
    assert "short" in line
    assert "20000.00" in line
    assert "RR" in line
    line.encode("ascii")   # must not raise UnicodeEncodeError


def test_format_limit_moved_line_content_and_ascii():
    """_format_limit_moved_line uses ASCII arrow '->' and shows old/new prices."""
    import signal_smt
    old = _sig()
    new = {**_sig(), "entry_price": 19998.0, "stop_price": 20018.0, "take_profit": 19948.0}
    line = signal_smt._format_limit_moved_line(_ts(), old, new)
    assert "LIMIT_MOVED" in line
    assert "->" in line
    assert "20000.00" in line and "19998.00" in line
    line.encode("ascii")


def test_format_limit_cancelled_line_content_and_ascii():
    """_format_limit_cancelled_line includes entry price and reason; ASCII-safe."""
    import signal_smt
    line = signal_smt._format_limit_cancelled_line(_ts(), _sig(), "hypothesis_invalidated")
    assert "LIMIT_CANCELLED" in line
    assert "hypothesis_invalidated" in line
    assert "20000.00" in line
    line.encode("ascii")


def test_format_limit_expired_line_content_and_ascii():
    """_format_limit_expired_line includes missed pts value; ASCII-safe."""
    import signal_smt
    line = signal_smt._format_limit_expired_line(_ts(), _sig(), 12.5)
    assert "LIMIT_EXPIRED" in line
    assert "12.5" in line
    assert "20000.00" in line
    line.encode("ascii")


def test_format_limit_filled_line_content_and_ascii():
    """_format_limit_filled_line includes filled price and queue_s; ASCII-safe."""
    import signal_smt
    evt = {"direction": "short", "filled_price": 20000.0,
           "original_limit_price": 20000.0, "time_in_queue_secs": 120.0}
    line = signal_smt._format_limit_filled_line(_ts(), evt)
    assert "LIMIT_FILLED" in line
    assert "120" in line
    assert "20000.00" in line
    line.encode("ascii")


# ══ _dispatch_event tests ═════════════════════════════════════════════════════

def test_dispatch_limit_placed_prints_formatted_line_and_json(capsys):
    """_dispatch_event for limit_placed prints a human-readable line and a JSON object."""
    import signal_smt
    evt = {"type": "limit_placed", "signal": {**_sig(), "tp_name": "tdo", "confidence": None, "divergence_bar_time": None}}
    signal_smt._dispatch_event(_ts(), evt)
    out = capsys.readouterr().out
    assert "LIMIT_PLACED" in out
    json_lines = [l for l in out.strip().split("\n") if l.strip().startswith("{")]
    assert len(json_lines) >= 1
    payload = json.loads(json_lines[0])
    assert payload["signal_type"] == "LIMIT_PLACED"


def test_dispatch_lifecycle_batch_emits_sub_events_in_order(capsys):
    """lifecycle_batch passed to _dispatch_event dispatches sub-events in declared order."""
    import signal_smt
    batch = {
        "type": "lifecycle_batch",
        "events": [
            {"type": "limit_filled", "direction": "short", "filled_price": 20000.0,
             "original_limit_price": 20000.0, "time_in_queue_secs": 60.0},
            {"type": "limit_placed", "signal": {**_sig(), "tp_name": "tdo",
             "confidence": None, "divergence_bar_time": None}},
        ],
    }
    signal_smt._dispatch_event(_ts(), batch)
    out = capsys.readouterr().out
    assert out.index("LIMIT_FILLED") < out.index("LIMIT_PLACED")


def test_dispatch_single_sub_event_does_not_crash(capsys):
    """Dispatching a single lifecycle_batch element does not raise."""
    import signal_smt
    evt = {"type": "limit_cancelled", "signal": _sig(), "reason": "test_reason"}
    signal_smt._dispatch_event(_ts(), evt)   # must not raise
    out = capsys.readouterr().out
    assert "LIMIT_CANCELLED" in out


def test_dispatch_unknown_event_type_safe(capsys):
    """Unknown event type: dispatcher emits a warning without raising."""
    import signal_smt
    evt = {"type": "totally_unknown_event_xyz", "data": 42}
    signal_smt._dispatch_event(_ts(), evt)   # must not raise
    out = capsys.readouterr().out
    assert out   # something was printed (warning)


# ══ JSON payload schema tests ═════════════════════════════════════════════════

def _json_payloads(capsys_out):
    """Parse all JSON-object lines from captured output."""
    return [json.loads(l) for l in capsys_out.strip().split("\n")
            if l.strip().startswith("{")]


def test_json_payload_limit_placed_schema(capsys):
    """LIMIT_PLACED JSON payload has required fields with correct types."""
    import signal_smt
    sig = {**_sig(), "tp_name": "tdo", "confidence": None, "divergence_bar_time": "2025-01-02"}
    signal_smt._dispatch_event(_ts(), {"type": "limit_placed", "signal": sig})
    payloads = _json_payloads(capsys.readouterr().out)
    assert payloads
    p = payloads[0]
    assert p["signal_type"] == "LIMIT_PLACED"
    for key in ("direction", "entry_price", "stop_price", "take_profit"):
        assert key in p
    assert isinstance(p["entry_price"], (int, float))


def test_json_payload_move_limit_schema(capsys):
    """MOVE_LIMIT JSON payload has old/new price fields."""
    import signal_smt
    old_sig = _sig()
    new_sig = {**_sig(), "entry_price": 19998.0, "stop_price": 20018.0, "take_profit": 19948.0}
    signal_smt._dispatch_event(_ts(), {"type": "limit_moved", "old_signal": old_sig, "new_signal": new_sig})
    payloads = _json_payloads(capsys.readouterr().out)
    assert payloads
    p = payloads[0]
    assert p["signal_type"] == "MOVE_LIMIT"
    for key in ("old_entry_price", "new_entry_price", "old_stop_price", "new_stop_price", "direction"):
        assert key in p


def test_json_payload_limit_filled_schema(capsys):
    """LIMIT_FILLED JSON payload has required fields."""
    import signal_smt
    evt = {"type": "limit_filled", "direction": "short",
           "filled_price": 20000.0, "original_limit_price": 20000.0, "time_in_queue_secs": 60.0}
    signal_smt._dispatch_event(_ts(), evt)
    payloads = _json_payloads(capsys.readouterr().out)
    assert payloads
    p = payloads[0]
    assert p["signal_type"] == "LIMIT_FILLED"
    for key in ("direction", "filled_price", "original_limit_price", "time_in_queue_secs"):
        assert key in p


def test_json_payload_cancel_limit_schema(capsys):
    """CANCEL_LIMIT JSON payload has direction, entry_price, and reason."""
    import signal_smt
    signal_smt._dispatch_event(_ts(), {"type": "limit_cancelled", "signal": _sig(), "reason": "hypothesis_invalidated"})
    payloads = _json_payloads(capsys.readouterr().out)
    assert payloads
    p = payloads[0]
    assert p["signal_type"] == "CANCEL_LIMIT"
    for key in ("direction", "entry_price", "reason"):
        assert key in p


def test_json_payload_limit_expired_schema(capsys):
    """LIMIT_EXPIRED JSON payload has direction, entry_price, and missed_move_pts."""
    import signal_smt
    signal_smt._dispatch_event(_ts(), {
        "type": "limit_expired", "signal": _sig(), "limit_missed_move": 8.5,
    })
    payloads = _json_payloads(capsys.readouterr().out)
    assert payloads
    p = payloads[0]
    assert p["signal_type"] == "LIMIT_EXPIRED"
    for key in ("direction", "entry_price", "missed_move_pts"):
        assert key in p


# ══ Integration: Full lifecycle ═══════════════════════════════════════════════

class TestFullLifecycle:
    """End-to-end lifecycle drives — no IB, synthetic bars."""

    def test_idle_to_limit_placed_to_limit_filled(self, monkeypatch):
        """Happy path: divergence → limit_placed → confirmation → limit_filled + signal."""
        import strategy_smt as strat
        _patch_limit(monkeypatch, strat)
        n = 12
        mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
        state = strat.ScanState()
        ctx = _ctx()

        r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
        assert r4 is not None and r4["type"] == "limit_placed"
        assert state.scan_state == "WAITING_FOR_ENTRY"

        r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
        assert r5 is None
        assert state.scan_state == "WAITING_FOR_LIMIT_FILL"

        entry_price = state.pending_limit_signal["entry_price"]
        lows[6] = entry_price - 2.0; highs[6] = B; opens[6] = B; closes[6] = entry_price - 1.0

        r6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))
        assert r6 is not None and r6["type"] == "lifecycle_batch"
        types = [e["type"] for e in r6["events"]]
        assert "limit_filled" in types and "signal" in types
        assert state.scan_state == "IDLE"

    def test_expired_without_fill(self, monkeypatch):
        """Divergence → limit_placed → no fill within expiry → limit_expired → IDLE."""
        import strategy_smt as strat
        _patch_limit(monkeypatch, strat, expiry=60)   # 1 bar timeout
        n = 12
        mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
        state = strat.ScanState()
        ctx = _ctx()

        _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
        _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
        assert state.scan_state == "WAITING_FOR_LIMIT_FILL"

        entry_price = state.pending_limit_signal["entry_price"]
        lows[6] = entry_price + 2.0; highs[6] = B + 10.0; opens[6] = B + 8.0; closes[6] = B + 9.0

        r6 = _call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, dict(c5))
        assert r6 is not None and r6["type"] == "limit_expired"
        assert state.scan_state == "IDLE"
        assert state.last_limit_signal_snapshot is None

    def test_cancel_on_hypothesis_invalidation_full(self, monkeypatch):
        """Divergence → limit_placed → adverse move → limit_cancelled → IDLE."""
        import strategy_smt as strat
        _patch_limit(monkeypatch, strat)
        monkeypatch.setattr(strat, "HYPOTHESIS_INVALIDATION_PTS", 3.0)
        n = 12
        mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _make_short_session(n)
        state = strat.ScanState()
        ctx = _ctx()

        r4 = _call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
        assert r4["type"] == "limit_placed"
        discovery_price = state.pending_discovery_price
        highs[5] = discovery_price + 10.0
        lows[5] = B - 1.0; opens[5] = B + 5.0; closes[5] = B + 9.0

        r5 = _call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
        assert r5 is not None and r5["type"] == "limit_cancelled"
        assert r5["reason"] == "hypothesis_invalidated"
        assert state.scan_state == "IDLE"
        assert state.last_limit_signal_snapshot is None

    def test_backtest_lifecycle_batch_unwrapping(self):
        """_resolve_scan_result extracts signals and suppresses lifecycle-only events."""
        import backtest_smt
        import strategy_smt as strat

        # lifecycle_batch with signal → returns the embedded signal
        batch_with_signal = {
            "type": "lifecycle_batch",
            "events": [
                {"type": "limit_filled", "direction": "short", "filled_price": 20000.0,
                 "original_limit_price": 20000.0, "time_in_queue_secs": 60.0},
                {"type": "signal", "direction": "short", "entry_price": 20000.0,
                 "stop_price": 20020.0, "take_profit": 19950.0},
            ],
        }
        result = backtest_smt._resolve_scan_result(batch_with_signal)
        assert result is not None
        assert result["type"] == "signal"
        assert result["direction"] == "short"

        # lifecycle_batch without signal → None (lifecycle-only batch)
        batch_no_signal = {
            "type": "lifecycle_batch",
            "events": [{"type": "limit_cancelled", "signal": {}, "reason": "test"}],
        }
        assert backtest_smt._resolve_scan_result(batch_no_signal) is None

        # Pure lifecycle events are suppressed (do not open positions)
        for evt_type in (strat.EVT_LIMIT_PLACED, strat.EVT_LIMIT_MOVED,
                         strat.EVT_LIMIT_CANCELLED, strat.EVT_LIMIT_FILLED):
            assert backtest_smt._resolve_scan_result({"type": evt_type}) is None, evt_type

        # Signal passes through unchanged
        sig = {"type": "signal", "direction": "long"}
        assert backtest_smt._resolve_scan_result(sig) is sig

        # None stays None
        assert backtest_smt._resolve_scan_result(None) is None
