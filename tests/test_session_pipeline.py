# tests/test_session_pipeline.py
# Unit tests for SessionPipeline: covers all 8 live/backtest behavioral divergences.

from __future__ import annotations

import pandas as pd
import pytest

import smt_detect
import smt_state
from session_pipeline import SessionPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_1m_bars(start: str, n: int, base: float = 21000.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({
        "Open":   [base] * n,
        "High":   [base + 10.0] * n,
        "Low":    [base - 10.0] * n,
        "Close":  [base + 2.0] * n,
        "Volume": [100] * n,
    }, index=idx)


def _bar_row(base: float = 21000.0) -> pd.Series:
    return pd.Series({"Open": base, "High": base + 5, "Low": base - 5, "Close": base + 1})


@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    """Redirect all smt_state paths to tmp_path; disable in-memory mode.

    Also point ACT_GLOBAL_DIR at tmp_path: the live path of on_session_start sets the
    state dir to sessions_dir()/<date> (so state + levels.json land in the session
    folder) and load_global() reads general_live_dir()/global.json — both must be isolated."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY",      False)


# ---------------------------------------------------------------------------
# Test 1: ATH seeding from history
# ---------------------------------------------------------------------------

def test_on_session_start_seeds_ath_from_history(_isolate_state, monkeypatch):
    """Fix #2: on_session_start seeds all_time_high from hist_mnq_1m["High"].max()."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5, base=25000.0)
    # _make_1m_bars sets High = base + 10, so max High = 25010.0
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert smt_state.load_global()["all_time_high"] == 25010.0


def test_yesterday_session_fvg_upgraded_to_keep_when_also_unvisited(_isolate_state, monkeypatch):
    """A yesterday-session 1hr FVG that was ALSO added by the unvisited `_detect_fvgs` pass
    (which returns no `keep`) must be UPGRADED to keep:True, not skipped as a dup — otherwise
    the per-bar visited-prune drops the MES leg once it fills the FVG while the MNQ leg
    survives (keep:True), so `_pair_fvgs` can no longer pair it and the fill SMT is missed
    (e.g. fvg_20260611_1600 fill_b @ 2026-06-12 03:03)."""
    import daily as _daily_mod
    import session_pipeline as _sp

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    # Unvisited pass (MES, session_pipeline line ~575) returns the FVG WITHOUT keep.
    _fvg = {"name": "fvg_TEST_bull", "kind": "fvg", "top": 21090.0, "bottom": 21010.0}
    monkeypatch.setattr(_daily_mod, "_detect_fvgs", lambda *a, **kw: [dict(_fvg)])
    # Yesterday-session pass (MNQ ~527 + MES ~581) returns the SAME name WITH keep.
    monkeypatch.setattr(
        _sp, "_detect_yesterday_session_fvgs", lambda *a, **kw: [{**_fvg, "keep": True}]
    )

    hist_mnq = _make_1m_bars("2026-06-11 09:20", n=5)
    hist_mes = _make_1m_bars("2026-06-11 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline.on_session_start(
        pd.Timestamp("2026-06-12 02:00", tz="America/New_York"),
        _make_1m_bars("2026-06-12 02:00", n=1),
    )

    daily = smt_state.load_daily()
    mes_fvg = next((l for l in daily.get("liquidities_mes", []) if l["name"] == "fvg_TEST_bull"), None)
    mnq_fvg = next((l for l in daily.get("liquidities", []) if l["name"] == "fvg_TEST_bull"), None)
    # MES leg: added first by the unvisited pass without keep → must be upgraded (the fix).
    assert mes_fvg is not None and mes_fvg.get("keep") is True, mes_fvg
    # MNQ leg: kept too (added via the yesterday-session pass with keep).
    assert mnq_fvg is not None and mnq_fvg.get("keep") is True, mnq_fvg


# ---------------------------------------------------------------------------
# Test 1b/1c (GIL-23): session_ath is derived from the PERSISTED all_time_high
# (general_live_dir/global.json), never collapsing to the windowed in-memory frame max.
# ---------------------------------------------------------------------------

def test_session_ath_seeds_from_persisted_ath_not_windowed(_isolate_state, monkeypatch):
    """GIL-23: session_ath must be the persisted true ATH, not the short windowed in-memory
    IB frame max. Reproduces 2026-06-11: persisted all_time_high 30807 carried in global.json,
    windowed frame max 29011.25 -> session_ath must seed to 30807, NOT 29011.25 (the collapse
    that silently disabled rule2b's recovery guard)."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    # The true ATH already persisted cross-session in general_live_dir/global.json (live).
    smt_state.save_global({"all_time_high": 30807.0, "confidence": "medium", "trend": "up"})

    # Windowed in-memory frame: max High == 29011.25 (below the persisted ATH).
    hist_mnq = _make_1m_bars("2026-06-11 09:20", n=5, base=29001.25)  # High = 29011.25
    assert float(hist_mnq["High"].max()) == 29011.25
    hist_mes = _make_1m_bars("2026-06-11 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2026-06-11 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2026-06-11 09:20", n=1))

    _g = smt_state.load_global()
    assert _g["session_ath"] == 30807.0       # persisted ATH, not the windowed 29011.25
    assert _g["all_time_high"] == 30807.0
    assert pipeline._session_ath == 30807.0


def test_backtest_seed_session_ath_equals_window_max(_isolate_state, monkeypatch):
    """GIL-23 regression-safety: in backtest (in-memory) mode global.json starts at DEFAULT
    (all_time_high = 0), so session_ath resolves to max(0, window max) = the 60-day window max
    — byte-identical to the old windowed seed, keeping backtests deterministic."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(smt_state, "_IN_MEMORY", True)
    smt_state._STORE.clear()  # fresh in-memory global (all_time_high defaults to 0)

    # Windowed frame max == 25010.0.
    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5, base=25000.0)  # High = 25010
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    _g = smt_state.load_global()
    assert _g["session_ath"] == 25010.0
    assert _g["all_time_high"] == 25010.0


# ---------------------------------------------------------------------------
# Test 2: State files reset to defaults on session start
# ---------------------------------------------------------------------------

def test_on_session_start_resets_state_files(_isolate_state, monkeypatch):
    """on_session_start(force_reset=True) resets hypothesis and position to DEFAULT values."""
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    # run_daily_fixed is mocked, so daily.json is never repopulated; the real
    # run_hypothesis is mocked out so it cannot mutate hypothesis away from DEFAULT.
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=True)

    assert smt_state.load_hypothesis() == smt_state.DEFAULT_HYPOTHESIS
    assert smt_state.load_position() == smt_state.DEFAULT_POSITION


# ---------------------------------------------------------------------------
# Test 3: Hourly resamples are computed and windowed to 14 days
# ---------------------------------------------------------------------------

def test_on_session_start_computes_hourly_resamples(_isolate_state, monkeypatch):
    """Fix #5: _hist_1hr is non-empty and contains only bars within 14 days of now."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    # 5 days × 8 hours × 60 min: all within 14-day window
    hist_mnq = _make_1m_bars("2025-11-07 09:00", n=60 * 8 * 5)
    hist_mes = _make_1m_bars("2025-11-07 09:00", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert pipeline._hist_1hr is not None
    assert not pipeline._hist_1hr.empty
    _14d_ago = now - pd.Timedelta(days=14)
    assert (pipeline._hist_1hr.index >= _14d_ago).all(), "All 1hr bars should be within 14-day window"


# ---------------------------------------------------------------------------
# Test 4: run_daily_fixed receives hist bars + hist_1hr/hist_4hr resamples
# ---------------------------------------------------------------------------

def test_on_session_start_calls_run_daily_with_filtered_bars(_isolate_state, monkeypatch):
    """run_daily_fixed is called with hist+today combined bars so the midnight bar
    is available for TDO lookup at the London session start trigger.
    """
    import daily as _daily_mod
    import hypothesis as _hyp_mod

    captured = []
    def fake_run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today):
        captured.append({
            "now": now, "hist_mnq_1m": hist_mnq_1m,
            "hist_1hr": hist_1hr, "hist_4hr": hist_4hr, "today": today,
        })

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", fake_run_daily_fixed)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    today_at_open = _make_1m_bars("2025-11-14 09:20", n=1)
    pipeline.on_session_start(now, today_at_open)

    assert len(captured) == 1
    # run_daily_fixed receives hist+today combined so midnight bar is available for TDO.
    combined = pd.concat([hist_mnq, today_at_open]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    assert captured[0]["hist_mnq_1m"].equals(combined)
    assert isinstance(captured[0]["hist_1hr"], pd.DataFrame)
    assert isinstance(captured[0]["hist_4hr"], pd.DataFrame)
    assert captured[0]["today"] == now.date()


# ---------------------------------------------------------------------------
# Test 5: run_trend called on every 1m bar
# ---------------------------------------------------------------------------

def test_on_1m_bar_calls_trend_every_bar(_isolate_state, monkeypatch):
    """run_trend fires on every bar (not only 5m boundaries)."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    trend_calls = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda now, bar, recent: trend_calls.append(now) or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()

    # 09:20 (5m boundary) and 09:21 (non-5m)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mes)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mes)

    assert len(trend_calls) == 2, f"run_trend should be called for every bar, got {len(trend_calls)}"


# ---------------------------------------------------------------------------
# Test 5b: 5m dispatch order is trend → hypothesis → strategy
# (re-homed from former test_smt_dispatch_order.py — GIL-28; asserts ordering
#  directly on on_1m_bar instead of via a full run_backtest_v2 run)
# ---------------------------------------------------------------------------

def test_on_1m_bar_dispatch_order_trend_hypothesis_strategy(_isolate_state, monkeypatch):
    """At a 5m boundary, on_1m_bar dispatch order must be trend → hypothesis → strategy."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    call_order: list[str] = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: call_order.append("trend") or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: call_order.append("hypothesis") or None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: call_order.append("strategy") or None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))
    # on_session_start runs hypothesis once itself; clear so we anchor on per-bar dispatch.
    call_order.clear()

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()

    # 09:20 is a 5m boundary → all three dispatch.
    pipeline.on_1m_bar(now_sess, bar, bar, today_mnq, today_mes)

    assert "trend" in call_order and "hypothesis" in call_order and "strategy" in call_order, (
        f"all three must dispatch on a 5m boundary, got {call_order}"
    )
    assert call_order.index("trend") < call_order.index("hypothesis") < call_order.index("strategy"), (
        f"dispatch order must be trend → hypothesis → strategy, got {call_order}"
    )


# ---------------------------------------------------------------------------
# Test 6: run_hypothesis called only on 5m boundaries
# ---------------------------------------------------------------------------

def test_on_1m_bar_calls_hypothesis_only_on_5m(_isolate_state, monkeypatch):
    """run_hypothesis fires only when now.minute % 5 == 0."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    hyp_calls = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: hyp_calls.append(True) or None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))
    # on_session_start runs hypothesis once itself; clear so we count only per-bar calls.
    hyp_calls.clear()

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()

    # 09:20 (5m boundary) → hypothesis fires; 09:21 (non-5m) → hypothesis skipped
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mes)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mes)

    assert len(hyp_calls) == 1, f"run_hypothesis should fire once (only at 5m boundary), got {len(hyp_calls)}"


# ---------------------------------------------------------------------------
# Test 7: run_strategy called on every 1m bar (Fix #1)
# ---------------------------------------------------------------------------

def test_on_1m_bar_calls_strategy_every_bar(_isolate_state, monkeypatch):
    """Fix #1: run_strategy fires on every bar, not just at 5m boundaries."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    strat_calls = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda now, bar, recent, **kw: strat_calls.append(now) or None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()

    # 09:20 (5m boundary) and 09:21 (non-5m); strategy must fire on both
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mes)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mes)

    assert len(strat_calls) == 2, f"run_strategy should be called for every bar, got {len(strat_calls)}"


# ---------------------------------------------------------------------------
# Test 8: bar_dict passed to trend/strategy includes body_high / body_low (Fix #8)
# ---------------------------------------------------------------------------

def test_on_1m_bar_bar_dict_has_body_fields(_isolate_state, monkeypatch):
    """Fix #8: bar dict includes body_high = max(open, close) and body_low = min(open, close)."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    captured_dicts = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda now, bar_dict, recent: captured_dicts.append(bar_dict) or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    open_price, close_price = 21000.0, 21005.0
    bar = pd.Series({"Open": open_price, "High": 21010.0, "Low": 20990.0, "Close": close_price})
    today_mnq = _make_1m_bars("2025-11-14 09:21", n=1)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=1)

    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mes)

    assert len(captured_dicts) == 1
    d = captured_dicts[0]
    assert "body_high" in d, "bar dict must contain 'body_high'"
    assert "body_low" in d, "bar dict must contain 'body_low'"
    assert d["body_high"] == max(open_price, close_price)
    assert d["body_low"] == min(open_price, close_price)


# ---------------------------------------------------------------------------
# Test 8b: a degenerate (empty) MES bar_row does not crash on_1m_bar
# Regression for the 2026-06-10 KeyError('High') crash: when MES has no partial-bar
# data for the minute, automation.main passes an empty pd.Series(dtype=float) as
# mes_bar_row. The MES liquidity + SMT passes must skip that bar, not raise.
# ---------------------------------------------------------------------------

def test_on_1m_bar_tolerates_empty_mes_bar_row(_isolate_state, monkeypatch):
    """Empty mes_bar_row (MES feed gap) → on_1m_bar skips the MES passes, no KeyError."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    trend_calls = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: trend_calls.append(1) or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    now = pd.Timestamp("2025-11-14 09:21", tz="America/New_York")
    today_mnq = _make_1m_bars("2025-11-14 09:21", n=1)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=1)
    empty_mes_bar = pd.Series(dtype=float)  # exactly what automation.main:236 builds

    # Must not raise (pre-fix: KeyError('High') at _update_instrument_liquidities).
    result = pipeline.on_1m_bar(now, _bar_row(), empty_mes_bar, today_mnq, today_mes)
    assert isinstance(result, list)
    # MNQ-side processing still runs (trend fires every bar) despite the MES gap.
    assert trend_calls, "MNQ trend pass must still run when the MES bar is missing"


def test_bar_row_has_ohlc_helper():
    """_bar_row_has_ohlc: True only when every requested field is present & non-NaN.

    Must work for BOTH a pd.Series (live path) AND a plain dict (backtest path,
    backtest_smt.py) — a dict has no `.index`, so the helper must not touch it.
    """
    import session_pipeline as _sp
    # --- pd.Series (live path) ---
    full = pd.Series({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5})
    assert _sp._bar_row_has_ohlc(full, "High", "Low", "Close")
    assert not _sp._bar_row_has_ohlc(pd.Series(dtype=float), "High")
    assert not _sp._bar_row_has_ohlc(pd.Series({"High": float("nan"), "Low": 1.0}), "High", "Low")
    assert not _sp._bar_row_has_ohlc(pd.Series({"Open": 1.0}), "High")
    # --- plain dict (backtest path) — regression for "'dict' object has no attribute 'index'" ---
    assert _sp._bar_row_has_ohlc({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5},
                                 "High", "Low", "Close")
    assert not _sp._bar_row_has_ohlc({}, "High")                       # empty dict
    assert not _sp._bar_row_has_ohlc({"Open": 1.0}, "High")            # missing key
    assert not _sp._bar_row_has_ohlc({"High": float("nan"), "Low": 1.0}, "High", "Low")  # NaN


def test_on_1m_bar_accepts_dict_bar_rows(_isolate_state, monkeypatch):
    """on_1m_bar must accept plain-dict bar rows (the backtest_smt.py path).

    Regression for `AttributeError: 'dict' object has no attribute 'index'` raised by
    `_bar_row_has_ohlc` when the MES-gap guard used `bar_row.index` (Series-only). The
    backtest replay passes dicts, so every regression/backtest crashed on the first bar.
    """
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))

    now = pd.Timestamp("2025-11-14 09:21", tz="America/New_York")
    # Plain dicts, exactly as backtest_smt.py builds them.
    mnq_row = {"Open": 21000.0, "High": 21010.0, "Low": 20990.0, "Close": 21002.0, "Volume": 100}
    mes_row = {"Open": 21000.0, "High": 21010.0, "Low": 20990.0, "Close": 21002.0, "Volume": 100}
    today_mnq = _make_1m_bars("2025-11-14 09:21", n=1)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=1)

    # Pre-fix: AttributeError('dict' object has no attribute 'index').
    result = pipeline.on_1m_bar(now, mnq_row, mes_row, today_mnq, today_mes)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 9: recent includes all-day bars from midnight (Fix #7)
# ---------------------------------------------------------------------------

def test_on_1m_bar_recent_includes_midnight_bars(_isolate_state, monkeypatch):
    """Fix #7: recent passed to run_trend includes bars from midnight, not just session start."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    captured_recents = []
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend",
                        lambda now, bar_dict, recent: captured_recents.append(recent) or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    # today_mnq starts at midnight — 562 bars from 00:00 to 09:21 inclusive
    today_mnq = _make_1m_bars("2025-11-14 00:00", n=562)
    today_mes = _make_1m_bars("2025-11-14 00:00", n=562)

    now = pd.Timestamp("2025-11-14 09:21", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar_row(), _bar_row(), today_mnq, today_mes)

    assert len(captured_recents) == 1
    recent = captured_recents[0]
    midnight_ts = pd.Timestamp("2025-11-14 00:00", tz="America/New_York")
    assert midnight_ts in recent.index, "recent must include the midnight bar"


# ---------------------------------------------------------------------------
# Test 10: run_hypothesis receives hist_1hr and hist_4hr kwargs (Fix #3)
# ---------------------------------------------------------------------------

def test_on_1m_bar_hypothesis_receives_hist_resamples(_isolate_state, monkeypatch):
    """Fix #3: run_hypothesis is called with hist_1hr and hist_4hr as keyword arguments."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    captured_kwargs: list[dict] = []

    def fake_hyp(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m, **kwargs):
        captured_kwargs.append(kwargs)
        return None

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", fake_hyp)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-07 09:00", n=60 * 5)
    hist_mes = _make_1m_bars("2025-11-07 09:00", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))
    # on_session_start runs hypothesis once itself; clear so we inspect only per-bar calls.
    captured_kwargs.clear()

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=5)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=5)

    # 09:20 is a 5m boundary (20 % 5 == 0)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"),
                       _bar_row(), _bar_row(), today_mnq, today_mes)

    assert len(captured_kwargs) == 1
    kw = captured_kwargs[0]
    assert "hist_1hr" in kw, "run_hypothesis must receive hist_1hr kwarg"
    assert "hist_4hr" in kw, "run_hypothesis must receive hist_4hr kwarg"
    assert isinstance(kw["hist_1hr"], pd.DataFrame)
    assert isinstance(kw["hist_4hr"], pd.DataFrame)


# ---------------------------------------------------------------------------
# Test 11: events are passed to the emit callback and returned
# ---------------------------------------------------------------------------

def test_on_1m_bar_emits_events_via_callback(_isolate_state, monkeypatch):
    """Events from trend and strategy are passed to emit_fn and included in return value."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    fake_trend_event = {"kind": "trend-signal", "price": 21000.0}
    fake_strat_event = {"kind": "market-entry", "price": 21005.0}

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: fake_trend_event)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: fake_strat_event)

    emitted: list[dict] = []
    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, emitted.append)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

    today_mnq = _make_1m_bars("2025-11-14 09:21", n=1)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=1)

    result = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"),
                                _bar_row(), _bar_row(), today_mnq, today_mes)

    assert fake_trend_event in emitted, "trend event must reach emit_fn"
    assert fake_strat_event in emitted, "strategy event must reach emit_fn"
    assert fake_trend_event in result, "trend event must be in return value"
    assert fake_strat_event in result, "strategy event must be in return value"


# ---------------------------------------------------------------------------
# Test 12: on_1m_bar is a no-op before on_session_start
# ---------------------------------------------------------------------------

def test_on_1m_bar_skips_if_daily_not_triggered(_isolate_state, monkeypatch):
    """on_1m_bar returns [] and calls nothing when on_session_start has not been called."""
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    calls: list[str] = []
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: calls.append("trend") or None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: calls.append("hyp") or None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: calls.append("strat") or None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    today_mnq = _make_1m_bars("2025-11-14 09:21", n=1)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=1)

    result = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"),
                                _bar_row(), _bar_row(), today_mnq, today_mes)

    assert result == [], "Should return empty list when on_session_start not yet called"
    assert calls == [], f"No module functions should be called, got: {calls}"


# ---------------------------------------------------------------------------
# Test 13: ATH gate uses seeded value not 0.0 (Fix #2)
# ---------------------------------------------------------------------------

def test_ath_gate_uses_seeded_ath_not_zero(_isolate_state, monkeypatch):
    """Fix #2: all_time_high in global state is set from hist_mnq, not the DEFAULT 0.0."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5, base=25000.0)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    g = smt_state.load_global()
    assert g["all_time_high"] != 0.0, "ATH must not be the DEFAULT 0.0 when history is present"
    assert g["all_time_high"] == 25010.0  # base=25000 + 10 from _make_1m_bars


# ---------------------------------------------------------------------------
# Test 14: hourly resample excludes Volume column (Fix #5)
# ---------------------------------------------------------------------------

def test_hourly_resample_excludes_volume(_isolate_state, monkeypatch):
    """Fix #5: _hist_1hr columns must not include 'Volume'."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-07 09:00", n=60 * 8 * 5)
    hist_mes = _make_1m_bars("2025-11-07 09:00", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert "Volume" not in pipeline._hist_1hr.columns, "_hist_1hr must not contain 'Volume' column"


# ---------------------------------------------------------------------------
# Test 15: hourly resample uses label="left" (Fix #5)
# ---------------------------------------------------------------------------

def test_hourly_resample_label_left(_isolate_state, monkeypatch):
    """Fix #5: _hist_1hr timestamps are left-aligned (label="left"), not right-aligned."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    # Bars at 09:00–09:59 ET — with label="left" the bar should be labeled 09:00, not 10:00
    hist_mnq = _make_1m_bars("2025-11-13 09:00", n=60)
    hist_mes = _make_1m_bars("2025-11-13 09:00", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert not pipeline._hist_1hr.empty, "_hist_1hr should not be empty"
    # label="left" → resampled bar labeled at the start of the hour (09:00)
    # label="right" (default) → labeled at the end (10:00)
    first_ts = pipeline._hist_1hr.index[0]
    assert first_ts.hour == 9, (
        f"label='left' should produce hour=9 timestamp; got hour={first_ts.hour}. "
        "If hour=10, label='right' is being used instead."
    )


# ---------------------------------------------------------------------------
# Test 16: on_session_start writes levels.json
# ---------------------------------------------------------------------------

def test_on_session_start_writes_levels_json(_isolate_state, monkeypatch, tmp_path):
    """on_session_start creates sessions/{date}/levels.json with liquidities and all_time_high."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.chdir(tmp_path)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5, base=21000.0)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    import json
    levels_path = tmp_path / "sessions" / "2025-11-14" / "levels.json"
    assert levels_path.exists(), "levels.json must be written by on_session_start"
    data = json.loads(levels_path.read_text(encoding="utf-8"))
    assert "liquidities" in data, "levels.json must contain 'liquidities' key"
    assert isinstance(data["liquidities"], list)
    assert "all_time_high" in data, "levels.json must contain 'all_time_high' key"


# ---------------------------------------------------------------------------
# Test 17: on_session_start rewrites levels.json on restart (run_daily reruns)
# ---------------------------------------------------------------------------

def test_on_session_start_levels_json_rewritten_on_restart(_isolate_state, monkeypatch, tmp_path):
    """levels.json is rewritten on a mid-session restart and remains valid.

    The redesign always reruns the daily/startup liquidity computation on restart,
    so levels.json is overwritten (no skip-if-exists guard).
    """
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    import json
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.chdir(tmp_path)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")

    # First call
    pipeline1 = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline1.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    levels_path = tmp_path / "sessions" / "2025-11-14" / "levels.json"
    assert levels_path.exists()

    # Second call (simulates mid-session restart on same date).
    pipeline2 = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline2.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    # File still present and parseable with the expected schema after rewrite.
    assert levels_path.exists()
    data = json.loads(levels_path.read_text(encoding="utf-8"))
    assert "liquidities" in data
    assert "all_time_high" in data


# ---------------------------------------------------------------------------
# Test 18: on_daily_or_startup seeds session_ath from hist max
# ---------------------------------------------------------------------------

def test_on_daily_or_startup_seeds_session_ath(_isolate_state, monkeypatch):
    """on_daily_or_startup seeds global.json session_ath from hist_mnq High max."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)

    # _make_1m_bars sets High = base + 10, so max High = 25010.0
    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5, base=25000.0)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_daily_or_startup(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert smt_state.load_global()["session_ath"] == 25010.0
    # The instance attribute mirrors the persisted value.
    assert pipeline._session_ath == 25010.0


# ---------------------------------------------------------------------------
# Test 19: 09:20 ET bar gate re-fires on_daily_or_startup once per day
# ---------------------------------------------------------------------------

def test_0920_gate_calls_on_daily_or_startup(_isolate_state, monkeypatch):
    """on_1m_bar at 09:20 ET fires on_daily_or_startup once for a new day, not on 09:21."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-12 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-12 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    # Session starts on 2025-11-13 (prior day), so _last_daily_date = 2025-11-13.
    prior = pd.Timestamp("2025-11-13 09:20", tz="America/New_York")
    pipeline.on_session_start(prior, _make_1m_bars("2025-11-13 09:20", n=1), force_reset=True)

    # Spy on on_daily_or_startup AFTER session start so the session-start call is excluded.
    daily_calls: list[pd.Timestamp] = []
    _orig = pipeline.on_daily_or_startup
    def _spy(now, today_mnq, today_mes=None):
        daily_calls.append(now)
        return _orig(now, today_mnq, today_mes)
    monkeypatch.setattr(pipeline, "on_daily_or_startup", _spy)

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    today_mes = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()

    # 09:20 on the NEW day → gate fires on_daily_or_startup once.
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mes)
    assert len(daily_calls) == 1, "09:20 bar on a new day must trigger on_daily_or_startup"

    # 09:21 same day → no re-trigger.
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mes)
    assert len(daily_calls) == 1, "09:21 must NOT re-trigger on_daily_or_startup"


# ---------------------------------------------------------------------------
# Test 20: per-bar update raises day_high when today's bars exceed stored high
# ---------------------------------------------------------------------------

def test_per_bar_updates_day_high(_isolate_state, monkeypatch):
    """on_1m_bar updates day_high in daily.json when today's bars exceed the stored high."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=True)

    # Force the stored day_high down to a known low value.
    _state = smt_state.load_daily()
    _liq = _state["liquidities"]
    _dh = next((l for l in _liq if l["name"] == "day_high"), None)
    if _dh is None:
        _liq.append({"name": "day_high", "kind": "level", "price": 1.0})
    else:
        _dh["price"] = 1.0
    _state["liquidities"] = _liq
    smt_state.save_daily(_state)

    # Today's bars contain a high of base + 10 = 30010.0, well above the forced 1.0.
    today_mnq = _make_1m_bars("2025-11-14 09:21", n=2, base=30000.0)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=2, base=30000.0)
    bar = pd.Series({"Open": 30000.0, "High": 30010.0, "Low": 29990.0, "Close": 30001.0})

    # 09:21 is not a 5m boundary and not the 09:20 gate → no daily rebuild.
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"),
                       bar, bar, today_mnq, today_mes)

    _liq2 = smt_state.load_daily()["liquidities"]
    _dh2 = next((l for l in _liq2 if l["name"] == "day_high"), None)
    assert _dh2 is not None, "day_high level must exist after per-bar update"
    assert _dh2["price"] == 30010.0, f"day_high should rise to today's max High, got {_dh2['price']}"


def test_per_bar_populates_close_price_body_extreme(_isolate_state, monkeypatch):
    """on_1m_bar stores a close_price (body extreme) alongside the wick price for
    day_high/day_low, distinct from the wick price. Highest Close for *_high, lowest
    Close for *_low — never equal to the wick High/Low here."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=True)

    # Today's bars: High 30010, Low 29990, Close 30002 (base+2). The day_high body extreme
    # is the highest CLOSE (30002) and day_low body extreme the lowest CLOSE (30002),
    # both strictly inside the wick range [29990, 30010].
    today_mnq = _make_1m_bars("2025-11-14 09:21", n=2, base=30000.0)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=2, base=30000.0)
    bar = pd.Series({"Open": 30000.0, "High": 30010.0, "Low": 29990.0, "Close": 30002.0})
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"),
                       bar, bar, today_mnq, today_mes)

    _liq = smt_state.load_daily()["liquidities"]
    _dh = next((l for l in _liq if l["name"] == "day_high"), None)
    _dl = next((l for l in _liq if l["name"] == "day_low"), None)
    assert _dh is not None and _dl is not None
    # close_price present and equal to the close extreme, NOT the wick price.
    assert _dh.get("close_price") == 30002.0, f"day_high close_price should be highest Close, got {_dh.get('close_price')}"
    assert _dl.get("close_price") == 30002.0, f"day_low close_price should be lowest Close, got {_dl.get('close_price')}"
    assert _dh["price"] == 30010.0 and _dh["close_price"] != _dh["price"]
    assert _dl["price"] == 29990.0 and _dl["close_price"] != _dl["price"]

    # The MES (liquidities_mes) block carries close_price too.
    _liq_mes = smt_state.load_daily().get("liquidities_mes", [])
    _dl_mes = next((l for l in _liq_mes if l["name"] == "day_low"), None)
    if _dl_mes is not None:
        assert "close_price" in _dl_mes


# ---------------------------------------------------------------------------
# Test 21: per-bar prunes a visited FVG from daily.json
# ---------------------------------------------------------------------------

def test_per_bar_fvg_visited_prune(_isolate_state, monkeypatch):
    """on_1m_bar removes an FVG from daily.json when the bar enters the FVG zone."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=True)

    # Insert an FVG zone [bottom=21000, top=21020].
    _state = smt_state.load_daily()
    _state["liquidities"].append(
        {"name": "fvg_test_bull", "kind": "fvg", "top": 21020.0, "bottom": 21000.0}
    )
    smt_state.save_daily(_state)

    today_mnq = _make_1m_bars("2025-11-14 09:21", n=2)
    today_mes = _make_1m_bars("2025-11-14 09:21", n=2)
    # Bar straddles the FVG zone: High >= bottom and Low <= top.
    bar = pd.Series({"Open": 21005.0, "High": 21015.0, "Low": 21005.0, "Close": 21010.0})

    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"),
                       bar, bar, today_mnq, today_mes)

    _liq2 = smt_state.load_daily()["liquidities"]
    _names = [l["name"] for l in _liq2]
    assert "fvg_test_bull" not in _names, "visited FVG must be pruned from daily.json"


# ---------------------------------------------------------------------------
# Test 22: force_reset=True resets hypothesis direction
# ---------------------------------------------------------------------------

def test_force_reset_true_resets_hypothesis(_isolate_state, monkeypatch):
    """on_session_start(force_reset=True) resets hypothesis direction away from a prior 'up'."""
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    # run_hypothesis mocked out so it cannot repopulate direction after the reset.
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    # Pre-seed an active 'up' hypothesis.
    _hyp = smt_state.load_hypothesis()
    _hyp["direction"] = "up"
    smt_state.save_hypothesis(_hyp)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=True)

    assert smt_state.load_hypothesis()["direction"] == smt_state.DEFAULT_HYPOTHESIS["direction"]
    assert smt_state.load_hypothesis()["direction"] == "none"


# ---------------------------------------------------------------------------
# Test 23: force_reset=False preserves an existing hypothesis direction
# ---------------------------------------------------------------------------

def test_force_reset_false_preserves_hypothesis(_isolate_state, monkeypatch):
    """on_session_start(force_reset=False) does not reset hypothesis to DEFAULT.

    run_hypothesis is mocked to a no-op so it cannot re-derive direction; with no
    explicit reset the pre-existing 'up' direction must survive.
    """
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    # Pre-seed an active 'up' hypothesis.
    _hyp = smt_state.load_hypothesis()
    _hyp["direction"] = "up"
    smt_state.save_hypothesis(_hyp)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1), force_reset=False)

    assert smt_state.load_hypothesis()["direction"] == "up", (
        "force_reset=False must preserve the existing hypothesis direction"
    )


# ---------------------------------------------------------------------------
# Test 24: midnight (00:00 ET) triggers on_daily_or_startup
# ---------------------------------------------------------------------------

def test_midnight_triggers_daily_recompute(_isolate_state, monkeypatch):
    """00:00 ET fires on_daily_or_startup so TDO updates to today's midnight open."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    # Session starts on 2025-11-13 (date after ET midnight = 2025-11-13)
    now_sess = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)
    assert pipeline._last_daily_date == now_sess.date()

    # At 00:00 ET the next day (2025-11-14), trigger should fire
    midnight_bar = _make_1m_bars("2025-11-14 00:00", n=1)
    bar = _bar_row()
    now_mid = pd.Timestamp("2025-11-14 00:00", tz="America/New_York")
    today_mnq_mid = _make_1m_bars("2025-11-13 18:00", n=3)
    pipeline.on_1m_bar(now_mid, bar, bar, today_mnq_mid, today_mnq_mid)

    assert pipeline._last_daily_date == now_mid.date(), (
        "midnight trigger should update _last_daily_date to the new calendar date"
    )


# ---------------------------------------------------------------------------
# Test 25: 09:20 ET does NOT re-trigger if midnight already fired on same date
# ---------------------------------------------------------------------------

def test_0920_skipped_if_midnight_already_ran(_isolate_state, monkeypatch):
    """After midnight trigger sets _last_daily_date to today, 09:20 ET is suppressed."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod

    call_count = [0]
    def counting_run_daily_fixed(*a, **kw):
        call_count[0] += 1

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", counting_run_daily_fixed)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    # Session start on 2025-11-13 → first call (count=1)
    now_sess = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)
    assert call_count[0] == 1

    # Midnight trigger on 2025-11-14 → second call (count=2)
    bar = _bar_row()
    now_mid = pd.Timestamp("2025-11-14 00:00", tz="America/New_York")
    pipeline.on_1m_bar(now_mid, bar, bar,
                       _make_1m_bars("2025-11-13 18:00", n=3),
                       _make_1m_bars("2025-11-13 18:00", n=3))
    assert call_count[0] == 2

    # 09:20 ET on same date → should NOT fire again
    now_0920 = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_1m_bar(now_0920, bar, bar,
                       _make_1m_bars("2025-11-14 09:00", n=20),
                       _make_1m_bars("2025-11-14 09:00", n=20))
    assert call_count[0] == 2, "09:20 ET trigger must be suppressed when midnight already ran"


# ---------------------------------------------------------------------------
# Test 26: per-bar day H/L uses extended lookback during Asia session
# ---------------------------------------------------------------------------

def test_per_bar_day_hl_asia_extends_to_previous_ny_morning(_isolate_state, monkeypatch):
    """During Asia session (≥18:00 ET), day H/L lookback reaches back to 06:00 ET."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    # hist bar at 06:00 ET with an extreme high — should be included in day_high
    hist_bars_base = pd.date_range("2025-11-13 06:00", periods=2, freq="1h", tz="America/New_York")
    hist_mnq = pd.DataFrame({
        "Open":   [21000.0, 21000.0],
        "High":   [25000.0, 21010.0],  # 25000 at 06:00 ET — extreme hist bar
        "Low":    [20990.0, 20990.0],
        "Close":  [21002.0, 21002.0],
        "Volume": [100, 100],
    }, index=hist_bars_base)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now_sess = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)

    # On a bar at 21:00 ET (Asia session) — hist extreme at 06:00 ET should be included
    now = pd.Timestamp("2025-11-13 21:00", tz="America/New_York")
    today_mnq = _make_1m_bars("2025-11-13 18:00", n=3, base=21000.0)  # high = 21010
    bar = _bar_row(base=21000.0)
    pipeline.on_1m_bar(now, bar, bar, today_mnq, today_mnq)

    _liq = smt_state.load_daily()["liquidities"]
    _dh = next((l for l in _liq if l["name"] == "day_high"), None)
    assert _dh is not None, "day_high must be set"
    assert _dh["price"] == 25000.0, (
        f"Asia session day_high should include 06:00 ET hist bar (25000), got {_dh['price']}"
    )


# ---------------------------------------------------------------------------
# Test 26b: per-bar day H/L during London looks back to prior NY evening (12:00 ET)
# ---------------------------------------------------------------------------

def test_per_bar_day_hl_london_extends_to_prev_ny_evening(_isolate_state, monkeypatch):
    """During London session (<06:00 ET), day H/L lookback reaches back to 12:00 ET
    (prior NY evening open) — 2 sessions back — and EXCLUDES the prior NY morning
    (06:00 ET) extreme."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    # hist bars: 06:00 ET (prior NY morning, EXCLUDED) extreme 25000;
    #            12:00 ET (prior NY evening, INCLUDED) 23000
    hist_idx = pd.DatetimeIndex([
        pd.Timestamp("2025-11-13 06:00", tz="America/New_York"),
        pd.Timestamp("2025-11-13 12:00", tz="America/New_York"),
    ])
    hist_mnq = pd.DataFrame({
        "Open":   [21000.0, 21000.0],
        "High":   [25000.0, 23000.0],   # 25000 @06:00 excluded, 23000 @12:00 included
        "Low":    [20990.0, 20990.0],
        "Close":  [21002.0, 21002.0],
        "Volume": [100, 100],
    }, index=hist_idx)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now_sess = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)

    # Bar at 03:00 ET next day (London) — 06:00 extreme EXCLUDED, 12:00 (23000) INCLUDED
    now = pd.Timestamp("2025-11-14 03:00", tz="America/New_York")
    today_mnq = _make_1m_bars("2025-11-13 18:00", n=3, base=21000.0)  # high = 21010
    bar = _bar_row(base=21000.0)
    pipeline.on_1m_bar(now, bar, bar, today_mnq, today_mnq)

    _liq = smt_state.load_daily()["liquidities"]
    _dh = next((l for l in _liq if l["name"] == "day_high"), None)
    assert _dh is not None, "day_high must be set"
    assert _dh["price"] == 23000.0, (
        f"London day_high should look back to 12:00 ET (23000) and exclude the 06:00 ET "
        f"bar (25000), got {_dh['price']}"
    )


# ---------------------------------------------------------------------------
# Test 27: per-bar day H/L uses standard (18:00 ET) lookback during NY morning
# ---------------------------------------------------------------------------

def test_per_bar_day_hl_ny_morning_excludes_previous_ny_morning(_isolate_state, monkeypatch):
    """From 06:00 ET onwards (NY morning), day H/L lookback starts at 18:00 ET only."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    # hist bar at 06:00 ET with an extreme high — must be EXCLUDED during NY morning
    hist_bars_base = pd.date_range("2025-11-13 06:00", periods=2, freq="1h", tz="America/New_York")
    hist_mnq = pd.DataFrame({
        "Open":   [21000.0, 21000.0],
        "High":   [25000.0, 21010.0],
        "Low":    [20990.0, 20990.0],
        "Close":  [21002.0, 21002.0],
        "Volume": [100, 100],
    }, index=hist_bars_base)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    # Session actually started the previous evening; for this test use a
    # mid-session force_reset at 18:00 ET on 2025-11-13.
    now_sess = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)

    # Bar at 09:00 ET next day (NY morning) — hist extreme at 06:00 ET should be EXCLUDED
    now = pd.Timestamp("2025-11-14 09:00", tz="America/New_York")
    today_mnq = _make_1m_bars("2025-11-13 18:00", n=3, base=21000.0)  # high = 21010
    bar = _bar_row(base=21000.0)
    pipeline.on_1m_bar(now, bar, bar, today_mnq, today_mnq)

    _liq = smt_state.load_daily()["liquidities"]
    _dh = next((l for l in _liq if l["name"] == "day_high"), None)
    assert _dh is not None, "day_high must be set"
    assert _dh["price"] < 25000.0, (
        f"NY morning day_high should NOT include 06:00 ET hist bar, got {_dh['price']}"
    )


# ---------------------------------------------------------------------------
# Test 28-30: _week_start_ts — extended lookback for Mon/Tue sessions
# ---------------------------------------------------------------------------

def _pipeline_for_week_ts_test():
    """Minimal pipeline fixture; state isolation not needed for pure _week_start_ts tests."""
    import daily as _daily_mod
    hist = _make_1m_bars("2025-11-10 00:00", n=1)
    pipeline = SessionPipeline.__new__(SessionPipeline)
    pipeline._hist_mnq_1m = hist
    pipeline._hist_mes_1m = hist
    pipeline._hist_1hr = hist
    pipeline._hist_4hr = hist
    return pipeline


def test_week_start_ts_monday_session_uses_prev_thursday():
    """Monday session (Sunday 21:00 ET): _week_start_ts returns prev Thursday 18:00 ET."""
    pipeline = _pipeline_for_week_ts_test()
    # Sunday 2025-11-09 21:00 ET = start of Monday's CME session
    now = pd.Timestamp("2025-11-09 21:00", tz="America/New_York")
    result = pipeline._week_start_ts(now)
    expected = pd.Timestamp("2025-11-06 18:00", tz="America/New_York")  # prev Thursday
    assert result == expected, f"Monday session week start expected {expected}, got {result}"


def test_week_start_ts_tuesday_session_uses_prev_friday():
    """Tuesday session (Monday 21:00 ET): _week_start_ts returns prev Friday 18:00 ET."""
    pipeline = _pipeline_for_week_ts_test()
    # Monday 2025-11-10 21:00 ET = start of Tuesday's CME session
    now = pd.Timestamp("2025-11-10 21:00", tz="America/New_York")
    result = pipeline._week_start_ts(now)
    expected = pd.Timestamp("2025-11-07 18:00", tz="America/New_York")  # prev Friday
    assert result == expected, f"Tuesday session week start expected {expected}, got {result}"


def test_week_start_ts_wednesday_session_uses_this_sunday():
    """Wednesday session (Tuesday 21:00 ET): _week_start_ts returns Sunday 18:00 ET."""
    pipeline = _pipeline_for_week_ts_test()
    # Tuesday 2025-11-11 21:00 ET = start of Wednesday's CME session
    now = pd.Timestamp("2025-11-11 21:00", tz="America/New_York")
    result = pipeline._week_start_ts(now)
    expected = pd.Timestamp("2025-11-09 18:00", tz="America/New_York")  # this Sunday
    assert result == expected, f"Wednesday session week start expected {expected}, got {result}"


# ---------------------------------------------------------------------------
# Pause: entry-side strategy gate (skip run_strategy when paused AND flat)
# ---------------------------------------------------------------------------

def _stub_pipeline_deps(monkeypatch):
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)


def test_on_1m_bar_skips_strategy_when_paused_and_flat(_isolate_state, monkeypatch):
    """Paused + flat: the entry-side strategy is skipped entirely (no phantom state churn)."""
    import strategy as _strat_mod
    _stub_pipeline_deps(monkeypatch)
    strat_calls = []
    monkeypatch.setattr(_strat_mod, "run_strategy",
                        lambda now, bar, recent, **kw: strat_calls.append(now) or None)
    monkeypatch.setattr(smt_state, "is_paused", lambda: True)

    pipeline = SessionPipeline(_make_1m_bars("2025-11-13 09:20", n=5),
                               _make_1m_bars("2025-11-13 09:20", n=5), lambda e: None)
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))
    # position is flat (default after on_session_start)
    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mnq)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:21", tz="America/New_York"), bar, bar, today_mnq, today_mnq)

    assert strat_calls == [], "paused + flat must skip run_strategy entirely"


def test_on_1m_bar_runs_strategy_when_paused_but_active(_isolate_state, monkeypatch):
    """Paused but holding a real active position: run_strategy still runs so exits are managed."""
    import strategy as _strat_mod
    _stub_pipeline_deps(monkeypatch)
    strat_calls = []
    monkeypatch.setattr(_strat_mod, "run_strategy",
                        lambda now, bar, recent, **kw: strat_calls.append(now) or None)
    monkeypatch.setattr(smt_state, "is_paused", lambda: True)

    pipeline = SessionPipeline(_make_1m_bars("2025-11-13 09:20", n=5),
                               _make_1m_bars("2025-11-13 09:20", n=5), lambda e: None)
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))
    # Set a real active position — exits must still be managed while paused.
    _pos = smt_state.load_position()
    _pos["active"] = {"direction": "long", "fill_price": 21000.0, "stop": 20980.0,
                      "contracts": 2, "cautious": "no"}
    smt_state.save_position(_pos)

    today_mnq = _make_1m_bars("2025-11-14 09:20", n=10)
    bar = _bar_row()
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), bar, bar, today_mnq, today_mnq)

    assert len(strat_calls) == 1, "paused + active must still call run_strategy (management/exits)"


# ---------------------------------------------------------------------------
# Live FVG detection (frozen-frame fix, 2026-06-05): _extend_fvg_frames appends
# just-completed 1hr/4hr bars from live 1m and detects FVGs completing at them.
# ---------------------------------------------------------------------------

def _fvg_session_bars() -> pd.DataFrame:
    """Today-session 1m bars forming a bull 1hr FVG completing at 21:00 ET:
    bar1 (19:00) High=21010; bar3 (21:00) Low=21020 > bar1 High -> gap 21010-21020.
    bar3's low prints at its first minute (21:00, excluded from the visited check by
    the strict index > formation_ts slice); later lows stay above the gap top."""
    rows = [
        ("2025-11-13 19:00", 21000.0, 21010.0, 20990.0, 21005.0),
        ("2025-11-13 19:30", 21005.0, 21008.0, 20995.0, 21000.0),
        ("2025-11-13 20:00", 21016.0, 21030.0, 21015.0, 21028.0),
        ("2025-11-13 21:00", 21030.0, 21035.0, 21020.0, 21032.0),
        ("2025-11-13 21:01", 21032.0, 21040.0, 21025.0, 21038.0),
    ]
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="America/New_York") for t, *_ in rows])
    return pd.DataFrame({
        "Open":   [r[1] for r in rows],
        "High":   [r[2] for r in rows],
        "Low":    [r[3] for r in rows],
        "Close":  [r[4] for r in rows],
        "Volume": [100] * len(rows),
    }, index=idx)


def _bare_fvg_pipeline(fvg_1hr="empty", fvg_4hr=None) -> SessionPipeline:
    """Minimal pipeline carrying only the rolling-FVG state (_extend_fvg_frames units)."""
    _cols = ["Open", "High", "Low", "Close"]
    p = SessionPipeline.__new__(SessionPipeline)
    p._fvg_1hr = pd.DataFrame(columns=_cols) if isinstance(fvg_1hr, str) else fvg_1hr
    p._fvg_4hr = fvg_4hr
    p._fvg_done_1hr = None
    p._fvg_done_4hr = None
    return p


def test_extend_fvg_frames_detects_live_1hr_fvg():
    """A 1hr FVG completing intra-session is detected from live 1m bars — at any bar
    time after the boundary (timestamp-based catch-up, not a minute==0 tick)."""
    pipeline = _bare_fvg_pipeline()
    now = pd.Timestamp("2025-11-13 22:37", tz="America/New_York")  # NOT minute 0

    found = pipeline._extend_fvg_frames(now, _fvg_session_bars())

    assert [f["name"] for f in found] == ["fvg_20251113_2100_bull"]
    assert found[0]["top"] == 21020.0 and found[0]["bottom"] == 21010.0
    # Frame extended with the completed 19/20/21 bars only (22:00 still forming).
    assert list(pipeline._fvg_1hr.index.hour) == [19, 20, 21]


def test_extend_fvg_frames_excludes_forming_bar():
    """The still-forming TF bar must not complete an FVG (its H/L is not final)."""
    pipeline = _bare_fvg_pipeline()
    bars = _fvg_session_bars()

    # 21:30 — the 21:00 bar (the FVG's completing bar) is still forming -> nothing.
    found = pipeline._extend_fvg_frames(
        pd.Timestamp("2025-11-13 21:30", tz="America/New_York"), bars)
    assert found == []
    assert list(pipeline._fvg_1hr.index.hour) == [19, 20]

    # Next boundary passed — the completed 21:00 bar now finishes the FVG.
    found = pipeline._extend_fvg_frames(
        pd.Timestamp("2025-11-13 22:05", tz="America/New_York"), bars)
    assert [f["name"] for f in found] == ["fvg_20251113_2100_bull"]


def test_extend_fvg_frames_no_redetection():
    """Windows ending at already-processed bars are never re-tested — a detected
    (or later pruned) FVG cannot be returned twice / resurrect."""
    pipeline = _bare_fvg_pipeline()
    bars = _fvg_session_bars()
    now1 = pd.Timestamp("2025-11-13 22:37", tz="America/New_York")
    assert len(pipeline._extend_fvg_frames(now1, bars)) == 1

    # Add a neutral 22:00 hour (no new FVG) and advance past the next boundary.
    extra = pd.DataFrame(
        {"Open": [21038.0], "High": [21045.0], "Low": [21025.0],
         "Close": [21040.0], "Volume": [100]},
        index=pd.DatetimeIndex([pd.Timestamp("2025-11-13 22:00", tz="America/New_York")]))
    bars2 = pd.concat([bars, extra])
    now2 = pd.Timestamp("2025-11-13 23:10", tz="America/New_York")

    assert pipeline._extend_fvg_frames(now2, bars2) == []


def test_extend_fvg_frames_joins_hist_and_live_bars():
    """An FVG whose first two bars are hist and whose completing bar is live is found
    (the rolling frame joins the session-init seed with live appends)."""
    hist_idx = pd.DatetimeIndex([
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        pd.Timestamp("2025-11-13 20:00", tz="America/New_York"),
    ])
    hist_1hr = pd.DataFrame({
        "Open": [21000.0, 21016.0], "High": [21010.0, 21030.0],
        "Low": [20990.0, 21015.0], "Close": [21005.0, 21028.0],
    }, index=hist_idx)
    pipeline = _bare_fvg_pipeline(fvg_1hr=hist_1hr)
    live = _fvg_session_bars().loc["2025-11-13 21:00":]  # only the completing hour

    found = pipeline._extend_fvg_frames(
        pd.Timestamp("2025-11-13 22:00", tz="America/New_York"), live)

    assert [f["name"] for f in found] == ["fvg_20251113_2100_bull"]


def test_extend_fvg_frames_detects_live_4hr_fvg():
    """Same mechanism on the 4hr frame (bull FVG completing at the 12:00 4hr bar)."""
    rows = [
        ("2025-11-13 04:00", 21000.0, 21010.0, 20990.0, 21005.0),
        ("2025-11-13 08:30", 21016.0, 21030.0, 21015.0, 21028.0),
        ("2025-11-13 12:00", 21030.0, 21035.0, 21020.0, 21032.0),
        ("2025-11-13 12:01", 21032.0, 21040.0, 21025.0, 21038.0),
    ]
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="America/New_York") for t, *_ in rows])
    bars = pd.DataFrame({
        "Open":   [r[1] for r in rows],
        "High":   [r[2] for r in rows],
        "Low":    [r[3] for r in rows],
        "Close":  [r[4] for r in rows],
        "Volume": [100] * len(rows),
    }, index=idx)
    _cols = ["Open", "High", "Low", "Close"]
    pipeline = _bare_fvg_pipeline(fvg_1hr=None,  # 1hr side disabled for isolation
                                  fvg_4hr=pd.DataFrame(columns=_cols))

    found = pipeline._extend_fvg_frames(
        pd.Timestamp("2025-11-13 16:45", tz="America/New_York"), bars)

    assert [f["name"] for f in found] == ["fvg_20251113_1200_bull"]


def test_live_fvg_lands_in_daily_json(_isolate_state, monkeypatch):
    """End-to-end: an FVG forming intra-session is appended to daily.json liquidities
    by _update_dynamic_liquidities (the old frozen-frame scan never found these)."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist = _make_1m_bars("2025-11-12 09:20", n=5)
    pipeline = SessionPipeline(hist, hist, lambda e: None)
    pipeline.on_session_start(
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        _fvg_session_bars().iloc[:1], force_reset=True)

    today_mnq = _fvg_session_bars()
    bar = _bar_row(base=21030.0)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-13 22:37", tz="America/New_York"),
                       bar, bar, today_mnq, today_mnq)

    _names = [l["name"] for l in smt_state.load_daily()["liquidities"]]
    assert "fvg_20251113_2100_bull" in _names


def test_daily_reset_preserves_live_fvgs(_isolate_state):
    """The 09:20 daily reset rebuilds the FVG frames from hist + today's COMPLETED
    bars, so FVGs formed live earlier in the session survive the wholesale
    liquidity recompute (previously they were dropped: run_daily_fixed re-scanned
    the session-init hist-only frames)."""
    hist = _make_1m_bars("2025-11-12 09:20", n=5)
    pipeline = SessionPipeline(hist, hist, lambda e: None)

    pipeline.on_daily_or_startup(
        pd.Timestamp("2025-11-14 09:20", tz="America/New_York"), _fvg_session_bars())

    _names = [l["name"] for l in smt_state.load_daily()["liquidities"]]
    assert "fvg_20251113_2100_bull" in _names


# ---------------------------------------------------------------------------
# GIL-8: manual direction lock — a level sweep must not reset the hypothesis
# ---------------------------------------------------------------------------

def _level_swept_sig(now: pd.Timestamp) -> dict:
    return {
        "kind": "level-swept", "time": now.isoformat(), "price": 21000.0,
        "level_name": "day_low", "level_price": 20990.0,
        "cooldown_active": True,  # cooldown path resets direction deterministically
        "bar_low": 20985.0, "bar_high": 21005.0,
    }


def _lock_pipeline(monkeypatch, emitted: list) -> tuple:
    """Pipeline with run_trend stubbed to emit a level-swept signal."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    now = pd.Timestamp("2025-11-14 02:00", tz="America/New_York")
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: _level_swept_sig(now))
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist = _make_1m_bars("2025-11-12 09:20", n=5)
    pipeline = SessionPipeline(hist, hist, lambda e: emitted.append(e))
    pipeline.on_session_start(
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        _make_1m_bars("2025-11-13 19:00", n=1), force_reset=True)
    return pipeline, now


def test_level_sweep_resets_direction_without_lock(_isolate_state, monkeypatch):
    """Sanity: cooldown-path level sweep resets direction when NOT locked."""
    emitted: list = []
    pipeline, now = _lock_pipeline(monkeypatch, emitted)
    hyp = smt_state.load_hypothesis()
    hyp["direction"] = "up"
    smt_state.save_hypothesis(hyp)

    today = _make_1m_bars("2025-11-13 19:00", n=10)
    bar = _bar_row()
    pipeline.on_1m_bar(now, bar, bar, today, today)

    assert smt_state.load_hypothesis()["direction"] == "none"
    assert any(e["kind"] == "trend-broken" for e in emitted)


def test_level_sweep_skipped_with_manual_lock(_isolate_state, monkeypatch):
    """GIL-8: the same sweep is absorbed as a non-event while the lock is set."""
    emitted: list = []
    pipeline, now = _lock_pipeline(monkeypatch, emitted)
    hyp = smt_state.load_hypothesis()
    hyp["direction"] = "up"
    hyp["manual"] = True
    smt_state.save_hypothesis(hyp)

    today = _make_1m_bars("2025-11-13 19:00", n=10)
    bar = _bar_row()
    pipeline.on_1m_bar(now, bar, bar, today, today)

    hyp = smt_state.load_hypothesis()
    assert hyp["direction"] == "up"
    assert hyp["manual"] is True
    assert not any(e["kind"] == "trend-broken" for e in emitted)


# ===========================================================================
# SMT V2 integration tests (detection + buffers + cadence + consumer + persist)
# ===========================================================================

def _smt_v2_pipeline(monkeypatch, emitted=None):
    """A SessionPipeline with daily/trend/hypothesis/strategy stubbed out so only the
    SMT V2 additive block runs. Seeded at 19:00 (Asia) force_reset."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)
    emitted = emitted if emitted is not None else []
    hist_mnq = _make_1m_bars("2025-11-12 09:20", n=5, base=21000.0)
    hist_mes = _make_1m_bars("2025-11-12 09:20", n=5, base=3000.0)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: emitted.append(e))
    pipeline.on_session_start(
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        _make_1m_bars("2025-11-13 19:00", n=1), force_reset=True)
    return pipeline, emitted


def _mnq_mes_today(start, n, mnq_base=21000.0, mes_base=3000.0):
    return (_make_1m_bars(start, n, base=mnq_base),
            _make_1m_bars(start, n, base=mes_base))


def _bar(base, high_off=5.0, low_off=5.0, close_off=1.0):
    return pd.Series({"Open": base, "High": base + high_off,
                      "Low": base - low_off, "Close": base + close_off})


def test_liquidities_mes_populated(_isolate_state, monkeypatch):
    """After seeding + a few bars, liquidities_mes has MES level entries; MNQ
    liquidities is independently populated."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 19:00", n=10)
    for i in range(10):
        now = pd.Timestamp("2025-11-13 19:00", tz="America/New_York") + pd.Timedelta(minutes=i)
        pipeline.on_1m_bar(now, _bar(21000.0), _bar(3000.0),
                           today_mnq.iloc[:i + 1], today_mes.iloc[:i + 1])
    daily = smt_state.load_daily()
    mes_levels = [l for l in daily["liquidities_mes"] if l.get("kind") == "level"]
    assert mes_levels, "MES levels should be populated"
    mnq_levels = [l for l in daily["liquidities"] if l.get("kind") == "level"]
    assert mnq_levels, "MNQ levels still populated"
    # MES level prices are in the MES scale (~3000), distinct from MNQ (~21000).
    assert any(2000.0 < l["price"] < 4000.0 for l in mes_levels)


def test_mnq_liquidities_unchanged_regression(_isolate_state, monkeypatch):
    """The additive MES refactor leaves MNQ `liquidities` byte-identical to a run that
    drives identical MNQ bars — verified by replaying and snapshotting the MNQ key."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 19:00", n=8)
    for i in range(8):
        now = pd.Timestamp("2025-11-13 19:00", tz="America/New_York") + pd.Timedelta(minutes=i)
        pipeline.on_1m_bar(now, _bar(21000.0), _bar(3000.0),
                           today_mnq.iloc[:i + 1], today_mes.iloc[:i + 1])
    mnq_liq = {l["name"]: l.get("price") for l in smt_state.load_daily()["liquidities"]
               if l.get("kind") == "level"}
    # The refactored MNQ pass must produce the SAME extremes as before: hist bars
    # (_make_1m_bars base 21000 → High 21010 / Low 20990) dominate the day/week window
    # (the live 21000±5 bars sit inside it). MES (~3000) must never bleed into MNQ.
    assert {"day_high", "day_low", "week_high", "week_low"} <= set(mnq_liq)
    for n in ("day_high", "week_high"):
        assert abs(mnq_liq[n] - 21010.0) < 1e-6
    for n in ("day_low", "week_low"):
        assert abs(mnq_liq[n] - 20990.0) < 1e-6
    # No MES-scale price leaked into the MNQ block.
    assert all(p > 10000.0 for p in mnq_liq.values())


def _freeze_liquidities(monkeypatch, pipeline):
    """Stop the per-bar dynamic-liquidity passes from overwriting test-injected daily.json
    so the detection block sees exactly the levels/FVGs the test crafted. Also clears the
    additive universe (prev-day/week) blocks the seed populates, so a test that crafts a
    single level isn't joined by seeded prev-levels via the detection-time merge."""
    monkeypatch.setattr(pipeline, "_update_dynamic_liquidities", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, "_update_mes_liquidities", lambda *a, **kw: [])
    _d = smt_state.load_daily()
    _d["liquidities_universe"] = []
    _d["liquidities_universe_mes"] = []
    smt_state.save_daily(_d)


def test_detection_runs_every_1m(_isolate_state, monkeypatch):
    """A crafted MNQ-only wick divergence on a 1m bar populates the per-minute buffer."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    # Seed an MNQ + MES day_high level both instruments share.
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)

    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=1)
    now = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    # MNQ wick touches 21000; MES wick stays below 3000.
    pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0, high_off=5.0),
                       today_mnq, today_mes)
    per_min = pipeline._smt_buffer.get_new("1m")
    assert any(r["ref_name"] == "day_high" and r["type"] == "wick" for r in per_min)


def test_universe_prev_day_smt_fires(_isolate_state, monkeypatch):
    """Universe (B): a prev-day FIXED level supplied via the additive liquidities_universe
    block is SMT-eligible. An MNQ-only wick take-out of prev1_day_high yields a wick SMT —
    proving the detection-time merge of the universe block works end-to-end."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)  # also clears the seeded universe blocks
    daily = smt_state.load_daily()
    daily["liquidities"] = []
    daily["liquidities_mes"] = []
    daily["liquidities_universe"] = [
        {"name": "prev1_day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_universe_mes"] = [
        {"name": "prev1_day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)

    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=1)
    now = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    # MNQ wick takes out 21000; MES wick stays below 3000 → MNQ-led bearish SMT.
    pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0, high_off=5.0),
                       today_mnq, today_mes)
    per_min = pipeline._smt_buffer.get_new("1m")
    assert any(r["ref_name"] == "prev1_day_high" and r["type"] == "wick" for r in per_min)


def test_hidden_on_1m_boundary(_isolate_state, monkeypatch):
    """Hidden (body) SMTs now run on the 1m cadence: a close-vs-level divergence in the
    just-COMPLETED 1m bar yields a body record on the next 1m boundary, tagged '1m'."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)

    # Build 1m bars whose CLOSE is > 21000 (MNQ) and < 3000 (MES) — a body divergence — but
    # whose WICKS do NOT take out the level (so no wick SMT interferes).
    idx = pd.date_range("2025-11-13 20:00", periods=16, freq="1min", tz="America/New_York")
    mnq = pd.DataFrame({"Open": [20990.0] * 16, "High": [21002.0] * 16,
                        "Low": [20980.0] * 16, "Close": [21005.0] * 16,
                        "Volume": [100] * 16}, index=idx)
    mes = pd.DataFrame({"Open": [2990.0] * 16, "High": [2999.0] * 16,
                        "Low": [2980.0] * 16, "Close": [2995.0] * 16,
                        "Volume": [100] * 16}, index=idx)

    # On the 20:07 boundary the just-completed [20:06,20:07) 1m bar is evaluated → body,
    # tagged "1m". (Hidden no longer waits for a 15m/30m boundary.)
    now = pd.Timestamp("2025-11-13 20:07", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar(20990.0), _bar(2990.0),
                       mnq.loc[:now], mes.loc[:now])
    assert any(r["type"] == "body" and r["timeframe"] == "1m"
               for r in pipeline._smt_buffer.get_new("5m"))


def test_cadence_morning_1m(_isolate_state, monkeypatch):
    """At 09:45 ET, flat, a new SMT is ingested by the reference consumer (1m cadence)."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)
    today_mnq, today_mes = _mnq_mes_today("2025-11-14 09:45", n=1)
    now = pd.Timestamp("2025-11-14 09:46", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0),
                       today_mnq, today_mes)
    assert len(pipeline._pending_watch.retained()) >= 1


def test_cadence_offhours_5m(_isolate_state, monkeypatch):
    """At 11:00 ET (off-hours), the consumer ingests only on the 5m boundary, and the 5m
    read returns the accumulated window."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)

    # 11:01 (not a 5m boundary): detection fires but the consumer does NOT ingest.
    today_mnq, today_mes = _mnq_mes_today("2025-11-14 11:00", n=2)
    now1 = pd.Timestamp("2025-11-14 11:01", tz="America/New_York")
    pipeline.on_1m_bar(now1, _bar(20996.0, high_off=10.0), _bar(2990.0),
                       today_mnq.iloc[:1], today_mes.iloc[:1])
    assert pipeline._pending_watch.retained() == []
    assert pipeline._smt_buffer.get_new("5m"), "accumulator holds the off-boundary record"


def test_cadence_boundaries(_isolate_state, monkeypatch):
    """Cadence = 5m at 09:29, 1m at 09:30 and 10:30, 5m at 10:31."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    _ET = _Z("America/New_York")

    def _cadence_at(hh, mm):
        now = pd.Timestamp(f"2025-11-14 {hh:02d}:{mm:02d}", tz="America/New_York")
        t = now.tz_convert(_ET).time()
        return "1m" if (_dt.time(9, 30) <= t <= _dt.time(10, 30)) else "5m"

    assert _cadence_at(9, 29) == "5m"
    assert _cadence_at(9, 30) == "1m"
    assert _cadence_at(10, 30) == "1m"
    assert _cadence_at(10, 31) == "5m"


def test_flat_gating(_isolate_state, monkeypatch):
    """With an active position, the reference consumer does NOT ingest."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)
    pos = smt_state.load_position()
    pos["active"] = {"direction": "up", "fill_price": 21000.0}
    smt_state.save_position(pos)

    today_mnq, today_mes = _mnq_mes_today("2025-11-14 09:45", n=1)
    now = pd.Timestamp("2025-11-14 09:46", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0),
                       today_mnq, today_mes)
    assert pipeline._pending_watch.retained() == [], "no ingest while a position is active"


def test_buffer_drains_after_5m_consumer(_isolate_state, monkeypatch):
    """The accumulator is cleared after the 5m-boundary consumer runs."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)

    today_mnq, today_mes = _mnq_mes_today("2025-11-14 11:00", n=6)
    # 11:01..11:04 accumulate (no boundary), 11:05 is the boundary → drain after consumer.
    for i, mm in enumerate((1, 2, 3, 4)):
        now = pd.Timestamp(f"2025-11-14 11:0{mm}", tz="America/New_York")
        pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0),
                           today_mnq.iloc[:i + 1], today_mes.iloc[:i + 1])
    assert pipeline._smt_buffer.get_new("5m"), "accumulated before the boundary"
    now5 = pd.Timestamp("2025-11-14 11:05", tz="America/New_York")
    pipeline.on_1m_bar(now5, _bar(20996.0, high_off=10.0), _bar(2990.0),
                       today_mnq, today_mes)
    assert pipeline._smt_buffer.get_new("5m") == [], "accumulator drained after the 5m consumer"


def test_fill_pairing_end_to_end(_isolate_state, monkeypatch):
    """A 1hr FVG present in BOTH instruments on the same bar enables a fill; a one-sided
    FVG never does."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    # Paired bull FVG in both; MNQ enters its zone, MES nowhere near → Fill-A.
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "fvg_20251113_2000_bull", "kind": "fvg",
                             "top": 21010.0, "bottom": 21000.0}]
    daily["liquidities_mes"] = [{"name": "fvg_20251113_2000_bull", "kind": "fvg",
                                 "top": 3010.0, "bottom": 3000.0}]
    smt_state.save_daily(daily)
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:30", n=1)
    now = pd.Timestamp("2025-11-13 20:31", tz="America/New_York")
    # MNQ high reaches 21005 (entered), MES near 2950 (not reached). Avoid the visited
    # prune removing the MNQ FVG: keep MNQ bar from straddling fully (low above bottom).
    mnq_bar = pd.Series({"Open": 21002.0, "High": 21005.0, "Low": 21001.0, "Close": 21003.0})
    mes_bar = pd.Series({"Open": 2950.0, "High": 2955.0, "Low": 2945.0, "Close": 2950.0})
    pipeline.on_1m_bar(now, mnq_bar, mes_bar, today_mnq, today_mes)
    assert any(r["kind"] == "fill" for r in pipeline._smt_buffer.get_new("1m"))

    # One-sided: MES FVG only → no pair → no fill.
    pipeline2, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline2)
    daily2 = smt_state.load_daily()
    daily2["liquidities"] = []
    daily2["liquidities_mes"] = [{"name": "fvg_20251113_2000_bull", "kind": "fvg",
                                  "top": 3010.0, "bottom": 3000.0}]
    smt_state.save_daily(daily2)
    pipeline2.on_1m_bar(now, mnq_bar, mes_bar, today_mnq, today_mes)
    assert not any(r["kind"] == "fill" for r in pipeline2._smt_buffer.get_new("1m"))


def test_v2_emits_smt_div_for_constructed_smt(_isolate_state, monkeypatch):
    """on_1m_bar emits a v2 smt-div SIGNAL for a constructed wick SMT: MNQ sweeps its
    day_high while MES fails to sweep its own → bearish wick divergence. The emitted event
    carries source=="v2", the MNQ close as price, and the MNQ level price as mnq_div_price."""
    emitted: list = []
    pipeline, _ = _smt_v2_pipeline(monkeypatch, emitted)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)
    emitted.clear()
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=1)
    now = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    # MNQ wick reaches 21006 (> 21000 day_high); MES wick stays below 3000.
    events = pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0, high_off=5.0),
                                today_mnq, today_mes)
    sd = [e for e in events if e.get("kind") == "smt-div"]
    assert sd, "expected a v2 smt-div for the constructed wick SMT"
    e = next(d for d in sd if d.get("type") == "wick" and d.get("ref_name") == "day_high")
    assert e["source"] == "v2"
    assert e["side"] == "bearish"
    assert e["timeframe"] == "1m"
    assert e["mnq_div_price"] == 21000.0   # MNQ level price for a wick/body SMT
    assert e["leader"] == "mnq"
    assert emitted == events               # _emit and returned list agree


# ---------------------------------------------------------------------------
# Adverse-run invalidation trail wiring (Contract INV-2)
# ---------------------------------------------------------------------------
def _seed_day_high(pipeline):
    """Seed a shared MNQ/MES day_high level for an invalidation scenario."""
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)


def _fire_then_invalidate(pipeline):
    """Fire a bearish (short) day_high wick SMT, then drive adverse-up bars whose MNQ close
    runs past fire_mnq_close + INVALIDATE_PTS["day"] → invalidation trips. Threshold-relative
    (reads the live INVALIDATE_PTS) so it stays correct as the default is tuned. Returns the
    list of on_1m_bar event lists. The adverse bars do NOT re-touch the level (high stays
    below 21000) so no re-fire / re-arm interferes."""
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=6)
    # Bar 1 (20:01): MNQ wick takes out day_high (21006), MES stays below → bearish fire.
    now1 = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    ev1 = pipeline.on_1m_bar(now1, _bar(20996.0, high_off=10.0), _bar(2990.0, high_off=5.0),
                             today_mnq.iloc[:1], today_mes.iloc[:1])
    # Adverse-up bars climbing PAST fire_close + INVALIDATE_PTS["day"]. Highs stay below the
    # 21000 level so the level is NOT re-touched (no re-arm / no re-fire).
    _fc = next(st["fire_mnq_close"] for k, st in pipeline._detect_state.items()
               if k.startswith("day_high|short") and isinstance(st, dict)
               and st.get("fire_mnq_close"))
    _inv = smt_detect.INVALIDATE_PTS_MNQ["day"]
    _trip = _fc + _inv + 5.0  # comfortably past the threshold
    ev_rest = []
    for i, (mm, mclose) in enumerate(((2, _fc + _inv * 0.5), (3, _trip), (4, _trip + 5.0)), start=1):
        now = pd.Timestamp(f"2025-11-13 20:0{mm}", tz="America/New_York")
        # close=mclose but high kept just below level via a Series with explicit fields.
        mnq_bar = pd.Series({"Open": mclose - 1.0, "High": 20999.0,
                             "Low": mclose - 2.0, "Close": mclose})
        mes_bar = pd.Series({"Open": 2988.0, "High": 2989.0, "Low": 2985.0, "Close": 2988.0})
        ev_rest.append(
            pipeline.on_1m_bar(now, mnq_bar, mes_bar,
                               today_mnq.iloc[:i + 1], today_mes.iloc[:i + 1]))
    return [ev1] + ev_rest


def test_smt_invalidations_written_to_state_dir(_isolate_state, monkeypatch):
    """Phase 1.1.5 (GIL-25) REMOVED adverse-run invalidation. Driving a bearish (short) day_high
    SMT then adverse-up bars (MNQ close runs past the old INVALIDATE_PTS threshold, but the bar
    highs stay below the level so neither depletion nor re-arm trips) must NOT write
    smt_invalidations.json, and the fired record must stay non-terminal (still pending/carryable)."""
    import paths
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_day_high(pipeline)

    _fire_then_invalidate(pipeline)

    inv_path = paths.state_dir() / "smt_invalidations.json"
    assert not inv_path.exists(), (
        "Phase 1.1.5 removed adverse-run invalidation: no smt_invalidations.json should be written")
    # The fired record is no longer retired by the adverse run — it stays pending.
    st = next(s for k, s in pipeline._detect_state.items()
              if k.startswith("day_high|short") and isinstance(s, dict) and s.get("fired"))
    assert st.get("fired") is True
    assert not st.get("invalidated")
    assert not st.get("retired_depleted")
    assert not st.get("fulfilled")


def test_invalidation_trail_not_in_sd_events(_isolate_state, monkeypatch):
    """Phase 1.1.5 (GIL-25) REMOVED the adverse-run __invalidations__ trail. No emitted smt-div /
    event carries an invalidation record (it never did), AND the producer detect_state no longer
    holds an adverse_run trail (the key may be absent or empty)."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_day_high(pipeline)

    ev_lists = _fire_then_invalidate(pipeline)

    # No emitted event mentions invalidation in any field.
    for evs in ev_lists:
        for e in evs:
            assert "invalidated" not in e
            assert e.get("reason") != "adverse_run"
            assert e.get("kind") != "smt-invalidation"
    # The adverse-run trail is gone: no adverse_run entry is recorded on the producer side.
    trail = pipeline._detect_state.get("__invalidations__", [])
    assert not any(e.get("reason") == "adverse_run" for e in trail)


def test_no_trail_file_when_no_invalidations(_isolate_state, monkeypatch):
    """A clean run with no adverse runs writes no invalidation trail file (the impl only
    writes when the list is non-empty)."""
    import paths
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_day_high(pipeline)

    # Fire a bearish SMT but never run adverse — just hold below the level (no re-touch, no
    # adverse-up). No invalidation should occur.
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=3)
    now1 = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    pipeline.on_1m_bar(now1, _bar(20996.0, high_off=10.0), _bar(2990.0, high_off=5.0),
                       today_mnq.iloc[:1], today_mes.iloc[:1])
    # A small favorable-down drift (still well within threshold) — no invalidation.
    now2 = pd.Timestamp("2025-11-13 20:02", tz="America/New_York")
    pipeline.on_1m_bar(now2, _bar(20990.0, high_off=2.0), _bar(2988.0, high_off=2.0),
                       today_mnq.iloc[:2], today_mes.iloc[:2])

    assert not pipeline._detect_state.get("__invalidations__"), "no invalidation expected"
    inv_path = paths.state_dir() / "smt_invalidations.json"
    assert not inv_path.exists(), "no trail file should be written for a clean run"


def test_v2_emits_smt_div_for_constructed_fill(_isolate_state, monkeypatch):
    """on_1m_bar emits a v2 smt-div SIGNAL for a constructed FILL: a paired 1hr FVG in both
    instruments, MNQ enters its zone while MES does not → fill_a. The emitted event has
    type in {fill_a, fill_b}, source=="v2", and mnq_div_price is None (fills reference an
    FVG zone, not a level)."""
    emitted: list = []
    pipeline, _ = _smt_v2_pipeline(monkeypatch, emitted)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "fvg_20251113_2000_bull", "kind": "fvg",
                             "top": 21010.0, "bottom": 21000.0}]
    daily["liquidities_mes"] = [{"name": "fvg_20251113_2000_bull", "kind": "fvg",
                                 "top": 3010.0, "bottom": 3000.0}]
    smt_state.save_daily(daily)
    emitted.clear()
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:30", n=1)
    now = pd.Timestamp("2025-11-13 20:31", tz="America/New_York")
    mnq_bar = pd.Series({"Open": 21002.0, "High": 21005.0, "Low": 21001.0, "Close": 21003.0})
    mes_bar = pd.Series({"Open": 2950.0, "High": 2955.0, "Low": 2945.0, "Close": 2950.0})
    events = pipeline.on_1m_bar(now, mnq_bar, mes_bar, today_mnq, today_mes)
    sd = [e for e in events if e.get("kind") == "smt-div"]
    assert sd, "expected a v2 smt-div for the constructed fill"
    fill_ev = next(d for d in sd if d.get("type") in ("fill_a", "fill_b"))
    assert fill_ev["source"] == "v2"
    assert fill_ev["mnq_div_price"] is None   # fills reference an FVG zone, not a level
    assert fill_ev["ref_name"] == "fvg_20251113_2000_bull"
    assert emitted == events


def test_restart_reload(_isolate_state, monkeypatch):
    """Edge/re-arm state + retained set persist to smts.json and reload on a fresh
    SessionPipeline."""
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)
    today_mnq, today_mes = _mnq_mes_today("2025-11-14 09:45", n=1)
    now = pd.Timestamp("2025-11-14 09:46", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0),
                       today_mnq, today_mes)
    saved = smt_state.load_smts()
    assert saved["detect_state"], "edge state persisted"
    assert saved["watch"]["retained"], "retained set persisted"

    # Fresh pipeline reloads from smts.json at session start. Use the SAME session date
    # (2025-11-13) so on_session_start resolves the same sessions/<date> state folder that
    # holds the smts.json written above.
    hist_mnq = _make_1m_bars("2025-11-12 09:20", n=5, base=21000.0)
    hist_mes = _make_1m_bars("2025-11-12 09:20", n=5, base=3000.0)
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: [])
    pipeline2 = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline2.on_session_start(
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        _make_1m_bars("2025-11-13 19:00", n=1))
    assert pipeline2._detect_state, "edge state restored on restart"
    assert len(pipeline2._pending_watch.retained()) >= 1, "retained set restored on restart"


def test_smt_v2_uses_prior_bar_levels_not_just_updated_extreme(_isolate_state, monkeypatch):
    """Refinement #2: detection evaluates against the PRE-update (prior-bar) levels.

    Because _update_dynamic_liquidities folds the current bar into the running extreme,
    loading daily.json AFTER it makes the leader trivially "touch" its own just-updated
    extreme every bar that the extreme advances. Using the PRE-update snapshot instead, a
    "touch" means the wick reached the PRIOR-bar extreme (a genuine take-out). We exercise
    this by passing the pre-update snapshot explicitly into _run_smt_v2_detection (mirroring
    on_1m_bar, which captures _pre_daily before _update_dynamic_liquidities) and contrasting
    two prior-bar level prices for the SAME 21005 wick.
    """
    pipeline, _ = _smt_v2_pipeline(monkeypatch)

    now = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=2)

    # MNQ wick reaches 21005 (high), MES does NOT reach its high → MNQ-leading divergence.
    mnq_row = _bar(21000.0, high_off=5.0)   # High = 21005
    mes_row = _bar(3000.0, high_off=1.0)    # High = 3001 (short of the 3010 MES level below)

    # Case A — prior-bar day_high sits ABOVE the wick (21010). The 21005 wick does NOT reach
    # the prior extreme, so there is no genuine take-out → must NOT fire. (Under the old
    # post-update load, the running extreme would have been pulled down to the bar's own
    # 21005 wick and fired spuriously.)
    pre_above = {
        "liquidities": [{"name": "day_high", "kind": "level", "price": 21010.0}],
        "liquidities_mes": [{"name": "day_high", "kind": "level", "price": 3010.0}],
    }
    pipeline._detect_state = {}
    recs_a = pipeline._run_smt_v2_detection(
        now, mnq_row, mes_row, today_mnq, today_mes, is_5m=False, pre_daily=pre_above)
    assert recs_a == [], "not reaching the prior-bar extreme must NOT fire"

    # Case B — genuine take-out: the prior-bar day_high was BELOW the wick (21000), so the
    # 21005 wick EXCEEDS the prior extreme. Must fire exactly one MNQ-leading short wick SMT.
    pre_below = {
        "liquidities": [{"name": "day_high", "kind": "level", "price": 21000.0}],
        "liquidities_mes": [{"name": "day_high", "kind": "level", "price": 3010.0}],
    }
    pipeline._detect_state = {}
    pipeline._smt_buffer = type(pipeline._smt_buffer)()  # fresh buffer
    recs_b = pipeline._run_smt_v2_detection(
        now, mnq_row, mes_row, today_mnq, today_mes, is_5m=False, pre_daily=pre_below)
    divs_b = [e for e in recs_b if e.get("kind") == "smt-div"]
    assert len(divs_b) == 1, "exceeding the prior-bar extreme is a genuine take-out → fires"
    assert divs_b[0]["side"] == "bearish" and divs_b[0]["type"] == "wick"
    assert divs_b[0]["leader"] == "mnq" and divs_b[0]["ref_name"] == "day_high"


def test_on_1m_bar_events_only_adds_v2_smt_div(_isolate_state, monkeypatch):
    """SMT V2 is now the SOLE source of smt-div SIGNALS. For a scenario that produces a
    divergence, the ONLY new events vs the pre-V2 baseline are v2 smt-div signals
    (kind=="smt-div", source=="v2"); the hypothesis-originated smt-div events are gone, and
    no raw smt/fill record leaks into the stream.

    Baseline = the event list with every v2 smt-div removed. That baseline must contain NO
    smt-div at all (the old hypothesis path no longer emits them), confirming v2 is additive
    and exclusive.
    """
    emitted: list = []
    pipeline, _ = _smt_v2_pipeline(monkeypatch, emitted)
    _freeze_liquidities(monkeypatch, pipeline)
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": 21000.0}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": 3000.0}]
    smt_state.save_daily(daily)
    emitted.clear()
    today_mnq, today_mes = _mnq_mes_today("2025-11-13 20:00", n=1)
    now = pd.Timestamp("2025-11-13 20:01", tz="America/New_York")
    events = pipeline.on_1m_bar(now, _bar(20996.0, high_off=10.0), _bar(2990.0),
                                today_mnq, today_mes)

    # emitted (via the _emit callback) and the returned events list agree.
    assert emitted == events

    smt_divs = [e for e in events if e.get("kind") == "smt-div"]
    # This scenario crafts an MNQ-leading wick divergence at day_high, so v2 must emit it.
    assert smt_divs, "v2 detector should emit at least one smt-div for this divergence"
    # Every smt-div is a v2 signal; none is a leftover hypothesis-originated event.
    assert all(e.get("source") == "v2" for e in smt_divs)
    # Hypothesis-originated smt-div carried `mes_div_price`; v2 signals never do.
    assert all("mes_div_price" not in e for e in smt_divs)
    # v2 smt-div fields are the new per-1m detector schema.
    assert all(e.get("timeframe") in ("1m", "15m", "30m", "1h") for e in smt_divs)
    assert all(e.get("type") in ("wick", "body", "fill_a", "fill_b") for e in smt_divs)

    # Baseline (everything except the v2 smt-div) has NO smt-div — the old hypothesis path
    # no longer emits them, so v2 is the only source.
    baseline = [e for e in events if not (e.get("kind") == "smt-div" and e.get("source") == "v2")]
    assert all(e.get("kind") != "smt-div" for e in baseline)
    # No raw smt/fill record leaks into the emitted/returned stream.
    assert all(e.get("kind") not in ("smt", "fill") for e in emitted)
    assert all(e.get("kind") not in ("smt", "fill") for e in events)


# ===========================================================================
# Yesterday-session 1hr FVG fill universe (Theme A)
# ===========================================================================

def _hist_with_yesterday_bull_fvg(base: float = 21000.0):
    """1m hist bars (one per hour) spanning the yesterday-session window for a
    session opening 2025-11-13 18:00 ET (yesterday = 2025-11-12 18:00 → 2025-11-13
    17:00). Engineered so the 3-bar 1hr window ending at 2025-11-13 01:00 forms a
    BULLISH FVG (bar3.Low > bar1.High), and that gap is LATER re-entered (filled) — so
    a plain unvisited scan would drop it, but the yesterday-session universe keeps it.
    """
    rows = {}
    # Hourly anchor bars across the window; default flat band well below the gap.
    cur = pd.Timestamp("2025-11-12 18:00", tz="America/New_York")
    end = pd.Timestamp("2025-11-13 17:00", tz="America/New_York")
    while cur <= end:
        rows[cur] = (base, base + 2.0, base - 2.0, base + 1.0)
        cur += pd.Timedelta(hours=1)
    # Craft the 3-bar bull FVG ending at 01:00 (bars at 23:00, 00:00, 01:00):
    #   bar1 (23:00) High = base+5 ; bar3 (01:00) Low = base+20 > base+5 → bull gap.
    b1 = pd.Timestamp("2025-11-12 23:00", tz="America/New_York")
    b3 = pd.Timestamp("2025-11-13 01:00", tz="America/New_York")
    rows[b1] = (base, base + 5.0, base - 2.0, base + 1.0)
    rows[b3] = (base + 25.0, base + 30.0, base + 20.0, base + 25.0)
    # Re-enter (fill) the gap later in the window so an unvisited scan would EXCLUDE it.
    fill_ts = pd.Timestamp("2025-11-13 10:00", tz="America/New_York")
    rows[fill_ts] = (base + 10.0, base + 12.0, base + 8.0, base + 11.0)
    idx = pd.DatetimeIndex(sorted(rows))
    data = [rows[t] for t in idx]
    return pd.DataFrame(
        {"Open": [d[0] for d in data], "High": [d[1] for d in data],
         "Low": [d[2] for d in data], "Close": [d[3] for d in data],
         "Volume": [100] * len(data)},
        index=idx,
    )


def test_detect_yesterday_session_fvgs_unit():
    """The helper detects a yesterday-session 1hr FVG even when it was later filled, and
    flags it keep:True. FVGs whose formation falls OUTSIDE the window are excluded."""
    from session_pipeline import _detect_yesterday_session_fvgs
    hist = _hist_with_yesterday_bull_fvg()
    now = pd.Timestamp("2025-11-13 18:00", tz="America/New_York")
    fvgs = _detect_yesterday_session_fvgs(hist, now)
    names = {f["name"] for f in fvgs}
    assert "fvg_20251113_0100_bull" in names, names
    assert all(f.get("keep") is True for f in fvgs)
    assert all(f.get("kind") == "fvg" for f in fvgs)
    target = next(f for f in fvgs if f["name"] == "fvg_20251113_0100_bull")
    assert target["bottom"] == 21005.0 and target["top"] == 21020.0


def _seed_pipeline_with_yesterday_fvgs(monkeypatch):
    """SessionPipeline seeded (real on_daily_or_startup, no stub) from MNQ+MES hist both
    carrying the engineered yesterday-session bull FVG, opening 2025-11-13 18:00 ET."""
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)
    hist_mnq = _hist_with_yesterday_bull_fvg(base=21000.0)
    hist_mes = _hist_with_yesterday_bull_fvg(base=3000.0)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    pipeline.on_session_start(
        pd.Timestamp("2025-11-13 18:00", tz="America/New_York"),
        _make_1m_bars("2025-11-13 18:00", n=1), force_reset=True)
    return pipeline


def test_yesterday_session_fvgs_seeded_into_both_blocks(_isolate_state, monkeypatch):
    """on_daily_or_startup adds the (filled) yesterday-session 1hr FVG to BOTH liquidities
    and liquidities_mes, keep-flagged, so _pair_fvgs can pair them by name."""
    _seed_pipeline_with_yesterday_fvgs(monkeypatch)
    daily = smt_state.load_daily()
    for key, scale in (("liquidities", 21005.0), ("liquidities_mes", 3005.0)):
        keep = [l for l in daily[key]
                if l.get("kind") == "fvg" and l.get("keep")
                and l["name"] == "fvg_20251113_0100_bull"]
        assert keep, f"{key} missing keep-flagged yesterday FVG: {daily[key]}"
        assert abs(keep[0]["bottom"] - scale) < 1e-6
    # Both blocks share the FVG name → it pairs.
    paired = SessionPipeline._pair_fvgs(daily["liquidities"], daily["liquidities_mes"])
    assert any(p["name"] == "fvg_20251113_0100_bull" for p in paired)


def test_keep_flagged_yesterday_fvg_survives_prune(_isolate_state, monkeypatch):
    """A bar that straddles a keep-flagged yesterday FVG zone does NOT prune it (it must
    remain a fill target all session), whereas a non-keep FVG in the same zone is pruned."""
    pipeline = _seed_pipeline_with_yesterday_fvgs(monkeypatch)
    # Inject a non-keep FVG covering the same zone to prove the prune still works for it.
    daily = smt_state.load_daily()
    daily["liquidities"].append(
        {"name": "fvg_20251112_2300_bull", "kind": "fvg", "top": 21020.0, "bottom": 21005.0})
    smt_state.save_daily(daily)

    today_mnq, today_mes = _mnq_mes_today("2025-11-13 18:00", n=1)
    # A bar straddling [21005, 21020] (High 21030 / Low 21000) re-enters the gap.
    now = pd.Timestamp("2025-11-13 18:01", tz="America/New_York")
    pipeline.on_1m_bar(now, _bar(21010.0, high_off=20.0, low_off=10.0), _bar(3010.0),
                       today_mnq, today_mes)
    names = {l["name"] for l in smt_state.load_daily()["liquidities"]
             if l.get("kind") == "fvg"}
    assert "fvg_20251113_0100_bull" in names, "keep-flagged FVG must survive the prune"
    assert "fvg_20251112_2300_bull" not in names, "non-keep FVG should be pruned when visited"


def test_fill_fires_against_yesterday_session_fvg(_isolate_state, monkeypatch):
    """End-to-end: with the paired yesterday-session FVG present in both blocks, a bar
    where MNQ enters the FVG but MES does not yields a fill_a smt-div from the pipeline."""
    pipeline = _seed_pipeline_with_yesterday_fvgs(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)  # keep the seeded FVGs static

    emitted: list = []
    monkeypatch.setattr(pipeline, "_emit", emitted.append)

    today_mnq, today_mes = _mnq_mes_today("2025-11-13 18:00", n=1)
    now = pd.Timestamp("2025-11-13 18:01", tz="America/New_York")
    # Bull FVG zone: MNQ [21005,21020], MES [3005,3020]. A bull FVG is filled by a retrace
    # DOWN. MNQ low dips into its zone (Low 21006 <= top 21020 → entered); MES stays ABOVE
    # its zone (Low 3030 > top 3020 → NOT entered) → leader=mnq fill_a.
    mnq_row = pd.Series({"Open": 21010.0, "High": 21015.0, "Low": 21006.0, "Close": 21010.0})
    mes_row = pd.Series({"Open": 3040.0, "High": 3045.0, "Low": 3030.0, "Close": 3040.0})
    events = pipeline.on_1m_bar(now, mnq_row, mes_row, today_mnq, today_mes)

    fills = [e for e in events if e.get("kind") == "smt-div"
             and e.get("type") in ("fill_a", "fill_b")]
    assert fills, f"expected a fill against the yesterday FVG; got {events}"
    f = fills[0]
    assert f["ref_name"] == "fvg_20251113_0100_bull"
    assert f["leader"] == "mnq"
    assert f["price"] is not None  # plot y-coordinate must be set
    assert emitted == events
