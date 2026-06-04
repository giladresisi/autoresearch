# tests/test_session_pipeline.py
# Unit tests for SessionPipeline: covers all 8 live/backtest behavioral divergences.

from __future__ import annotations

import pandas as pd
import pytest

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
    """Redirect all smt_state paths to tmp_path; disable in-memory mode."""
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")
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
    def _spy(now, today_mnq):
        daily_calls.append(now)
        return _orig(now, today_mnq)
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
