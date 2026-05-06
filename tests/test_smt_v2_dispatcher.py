# tests/test_smt_v2_dispatcher.py
# Tests for SmtV2Dispatcher in signal_smt and automation.main.
# Verifies that the dispatcher correctly:
#   - creates SessionPipeline lazily on the first 09:20 bar
#   - is idempotent per trading day (won't reinitialize on same day)
#   - resets pipeline on a new trading day
#   - guards on_1m_bar before session_start
#   - passes fresh DFs (not stale stored refs) to the pipeline

from __future__ import annotations

import pandas as pd
import pytest

import smt_state


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
        "Volume": [100.0] * n,
    }, index=idx)


def _bar_row(base: float = 21000.0) -> pd.Series:
    return pd.Series({"Open": base, "High": base + 5, "Low": base - 5, "Close": base + 1})


@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")
    monkeypatch.setattr(smt_state, "_IN_MEMORY",      False)


@pytest.fixture(params=["signal_smt", "automation.main"])
def DispatcherCls(request, monkeypatch):
    """Parameterised fixture returning SmtV2Dispatcher from each live module."""
    # Stub out modules that call external services at import time
    if request.param == "signal_smt":
        import signal_smt
        return signal_smt.SmtV2Dispatcher
    else:
        import automation.main as auto_main
        return auto_main.SmtV2Dispatcher


# ---------------------------------------------------------------------------
# Test 1: init produces no-op pipeline
# ---------------------------------------------------------------------------

def test_init_pipeline_is_none(DispatcherCls):
    d = DispatcherCls()
    assert d._pipeline is None
    assert d._session_date is None


# ---------------------------------------------------------------------------
# Test 2: on_1m_bar before on_session_start is a no-op
# ---------------------------------------------------------------------------

def test_on_1m_bar_before_session_start_is_noop(DispatcherCls, monkeypatch, _isolate_state):
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

    d = DispatcherCls()
    hist = _make_1m_bars("2025-11-13 09:20", n=5)
    now = pd.Timestamp("2025-11-14 09:25", tz="America/New_York")
    # Should not raise and should not trigger any pipeline calls
    d.on_1m_bar(now, _bar_row(), _bar_row(), hist, hist)
    assert d._pipeline is None


# ---------------------------------------------------------------------------
# Test 3: on_session_start creates pipeline and calls SessionPipeline.on_session_start
# ---------------------------------------------------------------------------

def test_on_session_start_creates_pipeline(_isolate_state, DispatcherCls, monkeypatch):
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

    hist = _make_1m_bars("2025-11-13 09:20", n=10)
    now = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")

    d = DispatcherCls()
    d.on_session_start(now, hist, hist)

    assert d._pipeline is not None
    assert d._session_date == now.date()
    # pipeline should have triggered daily (global ATH seeded)
    assert smt_state.load_global()["all_time_high"] == pytest.approx(21010.0)


# ---------------------------------------------------------------------------
# Test 4: on_session_start is idempotent per day
# ---------------------------------------------------------------------------

def test_on_session_start_idempotent_same_day(_isolate_state, DispatcherCls, monkeypatch):
    call_count = {"n": 0}
    import daily as _daily_mod
    def _fake_run_daily(*a, **kw):
        call_count["n"] += 1
    monkeypatch.setattr(_daily_mod, "run_daily", _fake_run_daily)

    hist = _make_1m_bars("2025-11-13 09:20", n=5)
    now1 = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    now2 = pd.Timestamp("2025-11-14 09:21", tz="America/New_York")

    d = DispatcherCls()
    d.on_session_start(now1, hist, hist)
    pipeline_first = d._pipeline
    d.on_session_start(now2, hist, hist)

    assert d._pipeline is pipeline_first  # same object — not recreated
    assert call_count["n"] == 1           # run_daily called once, not twice


# ---------------------------------------------------------------------------
# Test 5: on_session_start resets for a new trading day
# ---------------------------------------------------------------------------

def test_on_session_start_resets_on_new_day(_isolate_state, DispatcherCls, monkeypatch):
    import daily as _daily_mod
    monkeypatch.setattr(_daily_mod, "run_daily", lambda *a, **kw: None)

    hist = _make_1m_bars("2025-11-13 09:20", n=5)
    day1 = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    day2 = pd.Timestamp("2025-11-15 09:20", tz="America/New_York")
    hist2 = _make_1m_bars("2025-11-14 09:20", n=5)

    d = DispatcherCls()
    d.on_session_start(day1, hist, hist)
    pipeline_day1 = d._pipeline
    d.on_session_start(day2, hist2, hist2)

    assert d._pipeline is not pipeline_day1  # new pipeline for new day
    assert d._session_date == day2.date()


# ---------------------------------------------------------------------------
# Test 6: on_1m_bar after session_start delegates to pipeline
# ---------------------------------------------------------------------------

def test_on_1m_bar_delegates_to_pipeline(_isolate_state, DispatcherCls, monkeypatch):
    import daily as _daily_mod
    import trend as _trend_mod
    import strategy as _strat_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily",       lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend",       lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy",    lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod,   "run_hypothesis",  lambda *a, **kw: [])

    strategy_calls = []
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: strategy_calls.append(a) or None)

    hist = _make_1m_bars("2025-11-13 00:00", n=600)
    now_start = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    now_bar   = pd.Timestamp("2025-11-14 09:25", tz="America/New_York")

    today = _make_1m_bars("2025-11-14 00:00", n=400)

    d = DispatcherCls()
    d.on_session_start(now_start, hist, hist)
    d.on_1m_bar(now_bar, _bar_row(), _bar_row(), today, today)

    assert len(strategy_calls) == 1  # strategy was called for the bar


# ---------------------------------------------------------------------------
# Test 7: fresh DFs passed on each on_1m_bar call (no stale stored refs)
# ---------------------------------------------------------------------------

def test_on_1m_bar_passes_fresh_dfs_not_stale(_isolate_state, DispatcherCls, monkeypatch):
    """After init, appending a new row to the df outside the dispatcher and
    passing the new df to on_1m_bar must reflect the update (not use a stale copy)."""
    import daily as _daily_mod
    import trend as _trend_mod
    import strategy as _strat_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily",      lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod,   "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_trend_mod, "run_trend",      lambda *a, **kw: None)

    captured_recent: list = []
    def _capture_strategy(now, bar, recent, fill_check_only=False):
        captured_recent.append(recent)
        return None
    monkeypatch.setattr(_strat_mod, "run_strategy", _capture_strategy)

    hist = _make_1m_bars("2025-11-13 00:00", n=600)
    now_start = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")

    d = DispatcherCls()
    d.on_session_start(now_start, hist, hist)

    # Simulate IbRealtimeSource appending a new bar (replacement, not mutation)
    extra_bar = _make_1m_bars("2025-11-14 09:25", n=1, base=21500.0)
    today_v1 = _make_1m_bars("2025-11-14 00:00", n=390)
    today_v2 = pd.concat([today_v1, extra_bar])  # new object, simulating ib_source behavior

    now_bar = pd.Timestamp("2025-11-14 09:25", tz="America/New_York")
    d.on_1m_bar(now_bar, extra_bar.iloc[0], extra_bar.iloc[0], today_v2, today_v2)

    assert len(captured_recent) == 1
    # recent = today_v2[index <= now_bar]; the extra bar at 09:25 is included
    assert len(captured_recent[0]) == len(today_v2[today_v2.index <= now_bar])


# ---------------------------------------------------------------------------
# Test 8: today_mnq slice contains only today's bars
# ---------------------------------------------------------------------------

def test_on_1m_bar_slices_today_bars(_isolate_state, DispatcherCls, monkeypatch):
    import daily as _daily_mod
    import trend as _trend_mod
    import strategy as _strat_mod
    import hypothesis as _hyp_mod
    monkeypatch.setattr(_daily_mod, "run_daily",      lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend",      lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod,   "run_hypothesis", lambda *a, **kw: [])

    captured: list = []
    def _capture(now, bar, recent, fill_check_only=False):
        captured.append(recent)
        return None
    monkeypatch.setattr(_strat_mod, "run_strategy", _capture)

    # hist has bars from yesterday + today
    hist_yesterday = _make_1m_bars("2025-11-13 00:00", n=500)
    hist_today     = _make_1m_bars("2025-11-14 00:00", n=100)
    combined = pd.concat([hist_yesterday, hist_today])

    now_start = pd.Timestamp("2025-11-14 09:20", tz="America/New_York")
    d = DispatcherCls()
    d.on_session_start(now_start, combined, combined)

    now_bar = pd.Timestamp("2025-11-14 09:30", tz="America/New_York")
    d.on_1m_bar(now_bar, _bar_row(), _bar_row(), combined, combined)

    assert len(captured) == 1
    # recent should only contain today's bars up to now_bar
    recent = captured[0]
    assert all(ts.date() == pd.Timestamp("2025-11-14").date() for ts in recent.index)
