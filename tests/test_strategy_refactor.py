"""tests/test_strategy_refactor.py — Tests for strategy_refactor plan Phases 1–5.

Phase 1: ScanState, SessionContext, build_synthetic_confirmation_bar
Phase 2: process_scan_bar
Phase 3: parity with backtest (run via separate full-dataset test)
Phase 4: screen_session thin wrapper
Phase 5: signal_smt.py stateful upgrade
"""
import datetime
import numpy as np
import pandas as pd
import pytest


# ══ Phase 1.4: ScanState.reset() and build_synthetic_confirmation_bar ══════


def test_scan_state_initial_values():
    """ScanState fields are set to correct defaults at construction."""
    import strategy_smt as strat
    s = strat.ScanState()
    assert s.scan_state == "IDLE"
    assert s.pending_direction is None
    assert s.anchor_close is None
    assert s.divergence_bar_idx == -1
    assert s.conf_window_start == -1
    assert s.pending_smt_sweep == 0.0
    assert s.pending_smt_miss == 0.0
    assert s.pending_div_bar_high == 0.0
    assert s.pending_div_bar_low == 0.0
    assert s.pending_smt_defended is None
    assert s.pending_smt_type == "wick"
    assert s.pending_fvg_zone is None
    assert s.pending_fvg_detected is False
    assert s.pending_displacement_bar_extreme is None
    assert s.pending_div_score == 0.0
    assert s.pending_div_provisional is False
    assert s.pending_discovery_bar_idx == -1
    assert s.pending_discovery_price == 0.0
    assert s.pending_limit_signal is None
    assert s.limit_bars_elapsed == 0
    assert s.limit_max_bars == 0
    assert s.limit_missed_move == 0.0
    assert s.reentry_count == 0
    assert s.prior_trade_bars_held == 0
    assert s.htf_state == {}


def test_scan_state_reset_clears_all_mutations():
    """ScanState.reset() returns every field to its initial value."""
    import strategy_smt as strat
    s = strat.ScanState()
    # Mutate several fields
    s.scan_state = "WAITING_FOR_ENTRY"
    s.pending_direction = "short"
    s.anchor_close = 20002.0
    s.divergence_bar_idx = 7
    s.conf_window_start = 8
    s.pending_smt_sweep = 25.0
    s.pending_div_score = 3.5
    s.reentry_count = 2
    s.prior_trade_bars_held = 15
    s.htf_state = {15: {"pstart": 1234}}

    s.reset()

    assert s.scan_state == "IDLE"
    assert s.pending_direction is None
    assert s.anchor_close is None
    assert s.divergence_bar_idx == -1
    assert s.conf_window_start == -1
    assert s.pending_smt_sweep == 0.0
    assert s.pending_div_score == 0.0
    assert s.reentry_count == 0
    assert s.prior_trade_bars_held == 0
    assert s.htf_state == {}


def test_scan_state_reset_preserves_prior_trade_fields():
    """After reset(), prior_trade_bars_held starts fresh at 0."""
    import strategy_smt as strat
    s = strat.ScanState()
    s.prior_trade_bars_held = 42
    s.reset()
    assert s.prior_trade_bars_held == 0


def test_session_context_defaults():
    """SessionContext stores passed values and defaults correctly."""
    import strategy_smt as strat
    today = datetime.date(2025, 1, 2)
    ctx = strat.SessionContext(day=today, tdo=19900.0)
    assert ctx.day == today
    assert ctx.tdo == 19900.0
    assert ctx.midnight_open is None
    assert ctx.overnight == {}
    assert ctx.pdh is None
    assert ctx.pdl is None
    assert ctx.hyp_ctx is None
    assert ctx.hyp_dir is None
    assert ctx.bar_seconds == 60.0
    assert ctx.ref_lvls == {}


def test_session_context_custom_values():
    """SessionContext stores all provided values."""
    import strategy_smt as strat
    today = datetime.date(2025, 1, 2)
    ctx = strat.SessionContext(
        day=today,
        tdo=19800.0,
        midnight_open=20010.0,
        overnight={"overnight_high": 20050.0, "overnight_low": 19950.0},
        pdh=20100.0,
        pdl=19800.0,
        hyp_ctx={"hypothesis_score": 3},
        hyp_dir="short",
        bar_seconds=1.0,
        ref_lvls={"prev_day_mes_high": 20050.0},
    )
    assert ctx.tdo == 19800.0
    assert ctx.midnight_open == 20010.0
    assert ctx.overnight["overnight_high"] == 20050.0
    assert ctx.pdh == 20100.0
    assert ctx.pdl == 19800.0
    assert ctx.hyp_dir == "short"
    assert ctx.bar_seconds == 1.0
    assert ctx.ref_lvls["prev_day_mes_high"] == 20050.0


def test_build_synthetic_confirmation_bar_ohlcv():
    """Synthetic bar: Open from syn_start, High/Low extremes, Close from bar_idx, Volume sum."""
    import strategy_smt as strat
    opens  = np.array([100.0, 101.0, 102.0, 103.0])
    highs  = np.array([105.0, 108.0, 106.0, 110.0])
    lows   = np.array([ 98.0,  97.0,  96.0,  99.0])
    closes = np.array([101.0, 104.0, 103.0, 107.0])
    vols   = np.array([1000.0, 1200.0, 900.0, 1100.0])
    ts     = pd.Timestamp("2025-01-02 09:03:00", tz="America/New_York")

    # Synthetic bar over bars 1..3 (syn_start=1, bar_idx=3)
    bar = strat.build_synthetic_confirmation_bar(opens, highs, lows, closes, vols, 1, 3, ts)

    assert bar.Open   == 101.0          # opens[1]
    assert bar.High   == 110.0          # max(highs[1:4])
    assert bar.Low    == 96.0           # min(lows[1:4])
    assert bar.Close  == 107.0          # closes[3]
    assert bar.Volume == pytest.approx(1200.0 + 900.0 + 1100.0)
    assert bar.name   is ts


def test_build_synthetic_confirmation_bar_single_bar():
    """Single-bar window: synthetic bar equals the raw bar exactly."""
    import strategy_smt as strat
    opens  = np.array([200.0])
    highs  = np.array([205.0])
    lows   = np.array([198.0])
    closes = np.array([203.0])
    vols   = np.array([500.0])

    bar = strat.build_synthetic_confirmation_bar(opens, highs, lows, closes, vols, 0, 0, None)

    assert bar.Open  == 200.0
    assert bar.High  == 205.0
    assert bar.Low   == 198.0
    assert bar.Close == 203.0
    assert bar.Volume == 500.0


def test_build_synthetic_bar_subscript_access():
    """_BarRow returned by build_synthetic_confirmation_bar supports bar[key] access."""
    import strategy_smt as strat
    opens = highs = lows = closes = vols = np.array([10.0, 11.0])
    bar = strat.build_synthetic_confirmation_bar(opens, highs, lows, closes, vols, 0, 1, None)
    assert bar["Open"]   == bar.Open
    assert bar["High"]   == bar.High
    assert bar["Low"]    == bar.Low
    assert bar["Close"]  == bar.Close
    assert bar["Volume"] == bar.Volume


# ══ Phase 2: process_scan_bar ═════════════════════════════════════════════════

_P2B = 20000.0   # base price for Phase 2 test sessions


def _p2_patch(monkeypatch, strat):
    """Disable all strategy filters not under test so tests stay focused."""
    for attr, val in [
        ("LIMIT_ENTRY_BUFFER_PTS", None),
        ("LIMIT_EXPIRY_SECONDS", None),
        ("MAX_TDO_DISTANCE_PTS", 999.0),
        ("MIN_STOP_POINTS", 0.0),
        ("TDO_VALIDITY_CHECK", False),
        ("MIN_DIV_SCORE", 0.0),
        ("HYPOTHESIS_FILTER", False),
        ("HYPOTHESIS_INVALIDATION_PTS", 999.0),
        ("ALWAYS_REQUIRE_CONFIRMATION", False),
        ("EXPANDED_REFERENCE_LEVELS", False),
        ("HTF_VISIBILITY_REQUIRED", False),
        ("OVERNIGHT_SWEEP_REQUIRED", False),
        ("SILVER_BULLET_WINDOW_ONLY", False),
        ("MIN_RR_FOR_TARGET", 0.0),
        ("MIN_TARGET_PTS", 0.0),
        ("CONFIRMATION_WINDOW_BARS", 1),
        ("DISPLACEMENT_STOP_MODE", False),
        ("PARTIAL_EXIT_ENABLED", False),
        ("SMT_OPTIONAL", False),
        ("SMT_FILL_ENABLED", False),
        ("MIDNIGHT_OPEN_AS_TP", False),
        ("REPLACE_THRESHOLD", 9999.0),
    ]:
        monkeypatch.setattr(strat, attr, val)


def _p2_make_short_session(n=12):
    """Minimal session for SHORT SMT divergence tests.

    Bar 3: bullish MNQ  → anchor_close = B+1.0 for SHORT
    Bar 4: divergence   → MES.High sweeps session high; MNQ.High fails
    Bar 5: confirmation → bearish MNQ; high > anchor
    Bar 6+: neutral
    """
    B = _P2B
    ts = pd.date_range("2025-01-02 09:03", periods=n, freq="1min", tz="America/New_York")
    opens  = np.full(n, B)
    highs  = np.full(n, B + 2.0)
    lows   = np.full(n, B - 2.0)
    closes = np.full(n, B)
    vols   = np.full(n, 1000.0)
    opens[3] = B - 1.0;  closes[3] = B + 1.0   # bar3: bullish (anchor for SHORT)
    highs[4] = B + 1.5;  opens[4] = B;  closes[4] = B   # bar4: divergence
    opens[5] = B + 3.0;  closes[5] = B - 5.0;  highs[5] = B + 4.0;  lows[5] = B - 6.0
    m_base = B / 2
    mh = np.full(n, m_base + 1.0)
    ml = np.full(n, m_base - 1.0)
    mc = np.full(n, m_base)
    mh[4] = m_base + 2.0   # MES sweeps session high at bar 4
    mnq_r = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=ts).reset_index(drop=True)
    mes_r = pd.DataFrame({"Open": np.full(n, m_base), "High": mh, "Low": ml, "Close": mc, "Volume": vols}, index=ts).reset_index(drop=True)
    # smt_cache at bar 4: extremes from bars 0-3
    c4 = {"mes_h": m_base + 1.0, "mes_l": m_base - 1.0,
          "mnq_h": B + 2.0, "mnq_l": B - 2.0,
          "mes_ch": m_base, "mes_cl": m_base,
          "mnq_ch": B + 1.0, "mnq_cl": B - 1.0}
    # smt_cache at bar 5: bar 4 now included in session extremes
    c5 = {"mes_h": m_base + 2.0, "mes_l": m_base - 1.0,
          "mnq_h": B + 2.0, "mnq_l": B - 2.0,
          "mes_ch": m_base, "mes_cl": m_base,
          "mnq_ch": B + 1.0, "mnq_cl": B - 1.0}
    return mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5


def _p2_ctx():
    """SessionContext for SHORT tests: TDO below entry (BASE-5 after confirmation)."""
    import strategy_smt as strat
    return strat.SessionContext(
        day=datetime.date(2025, 1, 2), tdo=_P2B - 50.0, bar_seconds=60.0
    )


def _p2_call(strat, state, ctx, i, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, cache):
    """Single process_scan_bar call for bar index i."""
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


def test_process_scan_bar_idle_to_waiting_on_divergence(monkeypatch):
    """IDLE → WAITING_FOR_ENTRY when a divergence bar is detected."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()
    state = strat.ScanState()
    ctx = _p2_ctx()

    result = _p2_call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result is None
    assert state.scan_state == "WAITING_FOR_ENTRY"
    assert state.pending_direction == "short"
    assert state.divergence_bar_idx == 4
    assert state.anchor_close == pytest.approx(_P2B + 1.0)   # find_anchor_close → bar3 close


def test_process_scan_bar_waiting_fires_signal_on_confirmation(monkeypatch):
    """WAITING_FOR_ENTRY → signal dict on first confirmation bar."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()
    state = strat.ScanState()
    ctx = _p2_ctx()

    _p2_call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert state.scan_state == "WAITING_FOR_ENTRY"

    result = _p2_call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)

    assert result is not None
    assert result["type"] == "signal"
    assert result["direction"] == "short"
    assert state.scan_state == "IDLE"
    assert state.reentry_count == 1
    assert result.get("reentry_sequence") == 1


def test_process_scan_bar_confirmation_window_n3_fires_at_boundary(monkeypatch):
    """With CONFIRMATION_WINDOW_BARS=3, signal fires only at window close (bar 6, not bar 5)."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "CONFIRMATION_WINDOW_BARS", 3)
    n = 12
    B = _P2B
    ts = pd.date_range("2025-01-02 09:03", periods=n, freq="1min", tz="America/New_York")
    opens  = np.full(n, B)
    highs  = np.full(n, B + 2.0)
    lows   = np.full(n, B - 2.0)
    closes = np.full(n, B)
    vols   = np.full(n, 1000.0)
    opens[3] = B - 1.0;  closes[3] = B + 1.0   # bullish anchor for SHORT
    highs[4] = B + 1.5   # divergence bar
    # Bars 5-7: bearish confirmation window (bars 5,6,7 = conf_window_start=5, window closes at 7)
    for i in range(5, 8):
        opens[i] = B + 3.0; closes[i] = B - 5.0; highs[i] = B + 4.0; lows[i] = B - 6.0
    m_base = B / 2
    mh = np.full(n, m_base + 1.0); ml = np.full(n, m_base - 1.0); mc = np.full(n, m_base)
    mh[4] = m_base + 2.0
    mnq_r = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=ts).reset_index(drop=True)
    mes_r = pd.DataFrame({"Open": np.full(n, m_base), "High": mh, "Low": ml, "Close": mc, "Volume": vols}, index=ts).reset_index(drop=True)
    c4 = {"mes_h": m_base + 1.0, "mes_l": m_base - 1.0, "mnq_h": B + 2.0, "mnq_l": B - 2.0,
          "mes_ch": m_base, "mes_cl": m_base, "mnq_ch": B + 1.0, "mnq_cl": B - 1.0}
    c_after = {"mes_h": m_base + 2.0, "mes_l": m_base - 1.0, "mnq_h": B + 2.0, "mnq_l": B - 2.0,
               "mes_ch": m_base, "mes_cl": m_base, "mnq_ch": B + 1.0, "mnq_cl": B - 1.0}

    state = strat.ScanState()
    ctx = strat.SessionContext(day=datetime.date(2025, 1, 2), tdo=B - 50.0, bar_seconds=60.0)

    _p2_call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    assert state.scan_state == "WAITING_FOR_ENTRY"

    # Bar 5: first bar of window — should not fire (window not yet complete)
    r5 = _p2_call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c_after)
    assert r5 is None
    # Bar 6: second bar — still not complete
    r6 = _p2_call(strat, state, ctx, 6, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c_after)
    assert r6 is None
    # Bar 7: window boundary (conf_window_start=5, bar 7: (7-5+1)%3 == 0) — should fire
    r7 = _p2_call(strat, state, ctx, 7, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c_after)
    assert r7 is not None
    assert r7["type"] == "signal"


def test_process_scan_bar_invalidation_resets_to_idle(monkeypatch):
    """WAITING_FOR_ENTRY → IDLE when adverse move exceeds HYPOTHESIS_INVALIDATION_PTS."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "HYPOTHESIS_INVALIDATION_PTS", 3.0)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()

    state = strat.ScanState()
    state.scan_state = "WAITING_FOR_ENTRY"
    state.pending_direction = "short"
    state.anchor_close = _P2B + 1.0
    state.conf_window_start = 5
    state.divergence_bar_idx = 4
    state.pending_div_bar_high = _P2B + 1.5
    state.pending_div_bar_low = _P2B - 2.0
    state.pending_discovery_price = _P2B   # adverse check: bar.High - discovery_price
    state.pending_div_score = 0.3
    state.pending_discovery_bar_idx = 4

    # Bar where high = BASE+5 → adverse = 5+5 - 20000 = 20000+5 - 20000 = 5 pts > 3 pts threshold
    highs_mod = highs.copy()
    highs_mod[5] = _P2B + 5.0   # bar.High - discovery_price = 5.0 > 3.0

    ctx = _p2_ctx()
    bar = strat._BarRow(
        float(opens[5]), float(highs_mod[5]), float(lows[5]),
        float(closes[5]), float(vols[5]), ts=ts[5],
    )
    result = strat.process_scan_bar(
        state, ctx, 5, bar, mnq_r, mes_r, c5,
        run_ses_high=-float("inf"), run_ses_low=float("inf"),
        ts=ts[5], min_signal_ts=ts[0],
        mnq_opens=opens, mnq_highs=highs_mod, mnq_lows=lows,
        mnq_closes=closes, mnq_vols=vols,
        mes_highs=mh, mes_lows=ml, mes_closes=mc,
    )

    assert result is None
    assert state.scan_state == "IDLE"
    assert state.pending_direction is None


def test_process_scan_bar_adverse_anchor_update(monkeypatch):
    """Adverse confirmation bar updates anchor_close to its close."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()

    state = strat.ScanState()
    state.scan_state = "WAITING_FOR_ENTRY"
    state.pending_direction = "short"
    state.anchor_close = _P2B + 1.0
    state.conf_window_start = 5
    state.divergence_bar_idx = 4
    state.pending_div_bar_high = _P2B + 1.5
    state.pending_div_bar_low = _P2B - 2.0

    # Bar 5: bullish (open < close) — adverse for a SHORT pending; high > anchor
    opens_mod = opens.copy(); closes_mod = closes.copy()
    highs_mod = highs.copy()
    opens_mod[5] = _P2B - 2.0;  closes_mod[5] = _P2B + 3.0;  highs_mod[5] = _P2B + 4.0

    ctx = _p2_ctx()
    bar = strat._BarRow(
        float(opens_mod[5]), float(highs_mod[5]), float(lows[5]),
        float(closes_mod[5]), float(vols[5]), ts=ts[5],
    )
    result = strat.process_scan_bar(
        state, ctx, 5, bar, mnq_r, mes_r, c5,
        run_ses_high=-float("inf"), run_ses_low=float("inf"),
        ts=ts[5], min_signal_ts=ts[0],
        mnq_opens=opens_mod, mnq_highs=highs_mod, mnq_lows=lows,
        mnq_closes=closes_mod, mnq_vols=vols,
        mes_highs=mh, mes_lows=ml, mes_closes=mc,
    )

    assert result is None
    assert state.scan_state == "WAITING_FOR_ENTRY"   # still waiting
    assert state.anchor_close == pytest.approx(_P2B + 3.0)   # updated to bar5 close


def test_process_scan_bar_limit_fill_returns_signal(monkeypatch):
    """WAITING_FOR_LIMIT_FILL → signal when price reaches entry_price."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()

    ep = _P2B - 1.0   # limit entry for SHORT (below confirmation close)
    pending_sig = {
        "direction": "short", "entry_price": ep, "stop_price": ep + 5.0,
        "take_profit": ep - 40.0, "tp_name": "tdo",
        "entry_time": ts[5], "entry_bar": 5,
        "secondary_target": None, "secondary_target_name": None,
    }
    state = strat.ScanState()
    state.scan_state = "WAITING_FOR_LIMIT_FILL"
    state.pending_limit_signal = pending_sig
    state.limit_bars_elapsed = 0
    state.limit_max_bars = 5
    state.limit_missed_move = 0.0
    state.reentry_count = 0

    ctx = _p2_ctx()
    # Bar where HIGH >= entry_price (SHORT fills when High >= limit entry)
    bar = strat._BarRow(_P2B, ep + 0.5, ep - 3.0, ep - 1.0, 1000.0, ts=ts[6])
    result = strat.process_scan_bar(
        state, ctx, 6, bar, mnq_r, mes_r, c5,
        run_ses_high=-float("inf"), run_ses_low=float("inf"),
        ts=ts[6], min_signal_ts=ts[0],
        mnq_opens=opens, mnq_highs=highs, mnq_lows=lows,
        mnq_closes=closes, mnq_vols=vols,
        mes_highs=mh, mes_lows=ml, mes_closes=mc,
    )

    assert result is not None
    assert result["type"] == "signal"
    assert result["direction"] == "short"
    assert result["limit_fill_bars"] == 1
    assert state.scan_state == "IDLE"
    assert state.pending_limit_signal is None
    assert state.reentry_count == 1
    assert result.get("reentry_sequence") == 1


def test_process_scan_bar_limit_expired_returns_expired(monkeypatch):
    """WAITING_FOR_LIMIT_FILL → expired dict when limit_bars_elapsed >= limit_max_bars."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()

    ep = _P2B + 10.0   # limit entry far above current bars — won't fill
    pending_sig = {
        "direction": "short", "entry_price": ep, "stop_price": ep + 5.0,
        "take_profit": ep - 40.0, "tp_name": "tdo",
        "entry_time": ts[5], "entry_bar": 5,
        "secondary_target": None, "secondary_target_name": None,
    }
    state = strat.ScanState()
    state.scan_state = "WAITING_FOR_LIMIT_FILL"
    state.pending_limit_signal = pending_sig
    state.limit_bars_elapsed = 4    # one call will make it 5 = max_bars
    state.limit_max_bars = 5
    state.limit_missed_move = 2.0

    ctx = _p2_ctx()
    # Bar where High < ep so no fill, and after increment elapsed=5 >= max_bars=5
    bar = strat._BarRow(_P2B, _P2B + 2.0, _P2B - 2.0, _P2B, 1000.0, ts=ts[6])
    result = strat.process_scan_bar(
        state, ctx, 6, bar, mnq_r, mes_r, c5,
        run_ses_high=-float("inf"), run_ses_low=float("inf"),
        ts=ts[6], min_signal_ts=ts[0],
        mnq_opens=opens, mnq_highs=highs, mnq_lows=lows,
        mnq_closes=closes, mnq_vols=vols,
        mes_highs=mh, mes_lows=ml, mes_closes=mc,
    )

    assert result is not None
    assert result["type"] == "expired"
    assert result["signal"]["direction"] == "short"
    # ep=_P2B+10, Low=_P2B-2 → ep-Low=12 > pre-existing 2.0, so max updates to 12.0
    assert result["limit_missed_move"] == pytest.approx(12.0)
    assert state.scan_state == "IDLE"
    assert state.pending_limit_signal is None


def test_process_scan_bar_min_div_score_gate_blocks_signal(monkeypatch):
    """IDLE → stays IDLE when divergence score is below MIN_DIV_SCORE."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "MIN_DIV_SCORE", 0.99)   # near-max threshold
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()

    state = strat.ScanState()
    ctx = _p2_ctx()

    result = _p2_call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)

    assert result is None
    assert state.scan_state == "IDLE"
    assert state.pending_direction is None


def test_process_scan_bar_reentry_count_increments_each_signal(monkeypatch):
    """Reentry count increments for each same-bar signal returned from WAITING/REENTRY states."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4, c5 = _p2_make_short_session()
    ctx = _p2_ctx()

    # First entry cycle: IDLE → WAITING at bar 4 → signal at bar 5
    state = strat.ScanState()
    _p2_call(strat, state, ctx, 4, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c4)
    r1 = _p2_call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    assert r1 is not None and r1["type"] == "signal"
    assert state.reentry_count == 1
    assert r1["reentry_sequence"] == 1

    # Simulate re-entry: manually set REENTRY_ELIGIBLE (caller would do this after stop-out)
    state.scan_state = "REENTRY_ELIGIBLE"
    state.pending_direction = "short"
    state.anchor_close = _P2B + 1.0
    state.conf_window_start = 5
    state.divergence_bar_idx = 4
    state.pending_div_bar_high = _P2B + 1.5
    state.pending_div_bar_low = _P2B - 2.0
    monkeypatch.setattr(strat, "MAX_REENTRY_COUNT", 999)

    r2 = _p2_call(strat, state, ctx, 5, mnq_r, mes_r, ts, opens, highs, lows, closes, vols, mh, ml, mc, c5)
    assert r2 is not None and r2["type"] == "signal"
    assert state.reentry_count == 2
    assert r2["reentry_sequence"] == 2


# ══ Phase 4: screen_session thin wrapper ══════════════════════════════════════

def _p4_make_dataframes(n=12):
    """Build time-indexed DataFrames (as screen_session receives) for the short session."""
    B = _P2B
    ts = pd.date_range("2025-01-02 09:03", periods=n, freq="1min", tz="America/New_York")
    opens  = np.full(n, B)
    highs  = np.full(n, B + 2.0)
    lows   = np.full(n, B - 2.0)
    closes = np.full(n, B)
    vols   = np.full(n, 1000.0)
    opens[3] = B - 1.0;  closes[3] = B + 1.0
    highs[4] = B + 1.5;  opens[4] = B;  closes[4] = B
    opens[5] = B + 3.0;  closes[5] = B - 5.0;  highs[5] = B + 4.0;  lows[5] = B - 6.0
    m_base = B / 2
    mh = np.full(n, m_base + 1.0)
    ml = np.full(n, m_base - 1.0)
    mc = np.full(n, m_base)
    mh[4] = m_base + 2.0
    mnq = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=ts)
    mes = pd.DataFrame({"Open": np.full(n, m_base), "High": mh, "Low": ml, "Close": mc, "Volume": vols}, index=ts)
    return mnq, mes, _P2B - 50.0  # (mnq_df, mes_df, tdo)


def test_screen_session_returns_signal_on_clear_divergence(monkeypatch):
    """screen_session finds the short signal on the synthetic session."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(strat, "TRADE_DIRECTION", "both")
    mnq, mes, tdo = _p4_make_dataframes()
    sig = strat.screen_session(mnq, mes, tdo)
    assert sig is not None
    assert sig["direction"] == "short"
    assert sig["entry_bar"] == 5
    assert sig["divergence_bar"] == 4


def test_screen_session_matches_process_scan_bar_loop(monkeypatch):
    """screen_session signal fields match those from a manual process_scan_bar loop."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(strat, "TRADE_DIRECTION", "both")
    mnq, mes, tdo = _p4_make_dataframes()
    sig_ss = strat.screen_session(mnq, mes, tdo)
    assert sig_ss is not None

    # Manual process_scan_bar loop on same data
    n = min(len(mnq), len(mes))
    mes_r = mes.reset_index(drop=True)
    mnq_r = mnq.reset_index(drop=True)
    mnq_idx = mnq.index
    min_signal_ts = mnq_idx[0]
    opens  = mnq_r["Open"].values
    highs  = mnq_r["High"].values
    lows   = mnq_r["Low"].values
    closes = mnq_r["Close"].values
    vols   = mnq_r["Volume"].values
    mh = mes_r["High"].values
    ml = mes_r["Low"].values
    mc = mes_r["Close"].values
    ses_mes_h = ses_mes_l = float("nan")
    ses_mnq_h = ses_mnq_l = float("nan")
    ses_mes_ch = ses_mes_cl = float("nan")
    ses_mnq_ch = ses_mnq_cl = float("nan")
    run_high = -float("inf"); run_low = float("inf")
    smt_cache = {"mes_h": float("nan"), "mes_l": float("nan"),
                 "mnq_h": float("nan"), "mnq_l": float("nan"),
                 "mes_ch": float("nan"), "mes_cl": float("nan"),
                 "mnq_ch": float("nan"), "mnq_cl": float("nan")}
    state = strat.ScanState()
    ctx = strat.SessionContext(
        day=mnq_idx[0].date(), tdo=tdo, bar_seconds=60.0,
    )
    sig_loop = None
    for i in range(n):
        if i > 0:
            p = i - 1
            import math as _math
            v = float(mh[p]); ses_mes_h = v if _math.isnan(ses_mes_h) else max(ses_mes_h, v)
            v = float(ml[p]); ses_mes_l = v if _math.isnan(ses_mes_l) else min(ses_mes_l, v)
            v = float(highs[p]); ses_mnq_h = v if _math.isnan(ses_mnq_h) else max(ses_mnq_h, v)
            run_high = max(run_high, v)
            v = float(lows[p]); ses_mnq_l = v if _math.isnan(ses_mnq_l) else min(ses_mnq_l, v)
            run_low = min(run_low, v)
            v = float(mc[p]); ses_mes_ch = v if _math.isnan(ses_mes_ch) else max(ses_mes_ch, v)
            ses_mes_cl = v if _math.isnan(ses_mes_cl) else min(ses_mes_cl, v)
            v = float(closes[p]); ses_mnq_ch = v if _math.isnan(ses_mnq_ch) else max(ses_mnq_ch, v)
            ses_mnq_cl = v if _math.isnan(ses_mnq_cl) else min(ses_mnq_cl, v)
        smt_cache["mes_h"] = ses_mes_h; smt_cache["mes_l"] = ses_mes_l
        smt_cache["mnq_h"] = ses_mnq_h; smt_cache["mnq_l"] = ses_mnq_l
        smt_cache["mes_ch"] = ses_mes_ch; smt_cache["mes_cl"] = ses_mes_cl
        smt_cache["mnq_ch"] = ses_mnq_ch; smt_cache["mnq_cl"] = ses_mnq_cl
        ts_i = mnq_idx[i]
        bar = strat._BarRow(float(opens[i]), float(highs[i]), float(lows[i]),
                            float(closes[i]), float(vols[i]), ts_i)
        r = strat.process_scan_bar(
            state, ctx, i, bar, mnq_r, mes_r, smt_cache,
            run_high, run_low, ts_i, min_signal_ts,
            opens, highs, lows, closes, vols, mh, ml, mc,
        )
        if r is not None and r["type"] == "signal":
            sig_loop = r
            break

    assert sig_loop is not None
    assert sig_ss["direction"]   == sig_loop["direction"]
    assert sig_ss["entry_bar"]   == sig_loop["entry_bar"]
    assert sig_ss["divergence_bar"] == sig_loop["divergence_bar"]
    assert sig_ss["entry_price"] == pytest.approx(sig_loop["entry_price"])
    assert sig_ss["stop_price"]  == pytest.approx(sig_loop["stop_price"])
    assert sig_ss["take_profit"] == pytest.approx(sig_loop["take_profit"])


def test_screen_session_returns_none_no_divergence(monkeypatch):
    """screen_session returns None when session has no SMT divergence."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_END", "")
    B = _P2B
    n = 10
    ts = pd.date_range("2025-01-02 09:03", periods=n, freq="1min", tz="America/New_York")
    # Flat session — no sweep, no divergence
    mnq = pd.DataFrame({"Open": np.full(n, B), "High": np.full(n, B + 1.0),
                        "Low": np.full(n, B - 1.0), "Close": np.full(n, B),
                        "Volume": np.full(n, 1000.0)}, index=ts)
    m_base = B / 2
    mes = pd.DataFrame({"Open": np.full(n, m_base), "High": np.full(n, m_base + 1.0),
                        "Low": np.full(n, m_base - 1.0), "Close": np.full(n, m_base),
                        "Volume": np.full(n, 1000.0)}, index=ts)
    result = strat.screen_session(mnq, mes, tdo=B - 50.0)
    assert result is None


def test_screen_session_accepts_pdh_pdl_hyp_params(monkeypatch):
    """screen_session forwards pdh/pdl/hyp_ctx/hyp_dir to SessionContext without error."""
    import strategy_smt as strat
    _p2_patch(monkeypatch, strat)
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(strat, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(strat, "TRADE_DIRECTION", "both")
    mnq, mes, tdo = _p4_make_dataframes()
    sig = strat.screen_session(
        mnq, mes, tdo,
        pdh=_P2B + 100.0, pdl=_P2B - 100.0,
        hyp_ctx={"hypothesis_score": 3, "direction": "short"},
        hyp_dir="short",
    )
    assert sig is not None
    assert sig["direction"] == "short"
