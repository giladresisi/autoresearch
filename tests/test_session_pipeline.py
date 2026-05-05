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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

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
    """on_session_start resets daily, hypothesis, and position to their DEFAULT values."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now, _make_1m_bars("2025-11-14 09:20", n=1))

    assert smt_state.load_daily() == smt_state.DEFAULT_DAILY
    assert smt_state.load_hypothesis() == smt_state.DEFAULT_HYPOTHESIS
    assert smt_state.load_position() == smt_state.DEFAULT_POSITION


# ---------------------------------------------------------------------------
# Test 3: Hourly resamples are computed and windowed to 14 days
# ---------------------------------------------------------------------------

def test_on_session_start_computes_hourly_resamples(_isolate_state, monkeypatch):
    """Fix #5: _hist_1hr is non-empty and contains only bars within 14 days of now."""
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

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
# Test 4: run_daily receives the today_at_open bars (≤ 09:20)
# ---------------------------------------------------------------------------

def test_on_session_start_calls_run_daily_with_filtered_bars(_isolate_state, monkeypatch):
    """Fix #6: run_daily is called with today_mnq_at_open (bars ≤ 09:20), not all-day bars."""
    import daily as _daily_mod

    captured = []
    def fake_run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq):
        captured.append({"mnq_1m": mnq_1m, "hist_hourly": hist_hourly_mnq})

    monkeypatch.setattr(_daily_mod, "run_daily", fake_run_daily)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)

    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    today_at_open = _make_1m_bars("2025-11-14 09:20", n=1)
    pipeline.on_session_start(now, today_at_open)

    assert len(captured) == 1
    # run_daily should receive exactly the today_at_open slice
    assert captured[0]["mnq_1m"].equals(today_at_open)
    # hist_hourly should be the 14d-windowed resample (a DataFrame)
    assert isinstance(captured[0]["hist_hourly"], pd.DataFrame)


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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: hyp_calls.append(True) or None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
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

    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", fake_hyp)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-07 09:00", n=60 * 5)
    hist_mes = _make_1m_bars("2025-11-07 09:00", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)

    now_sess = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    pipeline.on_session_start(now_sess, _make_1m_bars("2025-11-14 09:20", n=1))

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

    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)
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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

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
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

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
