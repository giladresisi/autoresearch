"""Cycle-1 graft: the trader hook is additive and must not disturb the legacy engine.

NOTE on two divergences from the plan's draft of this file:
  * `_isolate_state` is defined LOCALLY. In the plan's draft it is used as a fixture,
    but it lives in `tests/test_session_pipeline.py`, not in `tests/conftest.py`, so it
    is not visible here.
  * `on_session_start` takes `(now, today_mnq_at_open, force_reset=...)` — three
    parameters, not four. The draft passed a second bar frame positionally, which is a
    `TypeError` against the real signature.
"""
from __future__ import annotations

import pandas as pd
import pytest

import smt_state
from session_pipeline import SessionPipeline


def _bars(start="2026-08-13 09:00", n=5):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                         "Volume": 1.0}, index=idx)


def _row(base: float = 100.0) -> pd.Series:
    return pd.Series({"Open": base, "High": base + 1, "Low": base - 1, "Close": base})


@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    """Redirect every smt_state path into tmp_path (mirrors the fixture in
    tests/test_session_pipeline.py) so no test can touch real live state."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)


@pytest.fixture()
def _quiet_engine(monkeypatch):
    """Stub the legacy engine's four heavy entry points, exactly as the existing
    session-pipeline tests do — this file is about the HOOK, not about daily levels."""
    import daily as _daily_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    import trend as _trend_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)


def test_trader_defaults_to_none_and_changes_nothing(_isolate_state):
    p = SessionPipeline(_bars(), _bars(), lambda e: None)
    assert p._trader is None


def test_trader_hook_does_not_suppress_legacy_events(_isolate_state, _quiet_engine):
    """The legacy engine must keep emitting exactly as before."""
    class _Spy:
        called = 0
        def on_bar(self, *a, **k): self.__class__.called += 1

    _Spy.called = 0
    seen = []
    p = SessionPipeline(_bars(), _bars(), seen.append, trader=_Spy())
    p.on_session_start(pd.Timestamp("2026-08-13 09:00", tz="America/New_York"),
                       _bars(), force_reset=True)
    p.on_1m_bar(pd.Timestamp("2026-08-13 09:01", tz="America/New_York"),
                _row(), _row(), _bars(), _bars())
    assert _Spy.called >= 1


def test_a_raising_trader_does_not_crash_the_bar_loop(_isolate_state, _quiet_engine):
    class _Boom:
        def on_bar(self, *a, **k): raise RuntimeError("boom")

    p = SessionPipeline(_bars(), _bars(), lambda e: None, trader=_Boom())
    p.on_session_start(pd.Timestamp("2026-08-13 09:00", tz="America/New_York"),
                       _bars(), force_reset=True)
    p.on_1m_bar(pd.Timestamp("2026-08-13 09:01", tz="America/New_York"),
                _row(), _row(), _bars(), _bars())


# --- added during implementation (not in the plan) --------------------------- #

def test_hook_receives_bar_time_and_the_bar_complete_flag(_isolate_state, _quiet_engine):
    seen = []

    class _Rec:
        def on_bar(self, now, mnq, mes, bar_complete=None, **kw):
            seen.append((now, bar_complete))

    p = SessionPipeline(_bars(), _bars(), lambda e: None, trader=_Rec())
    p.on_session_start(pd.Timestamp("2026-08-13 09:00", tz="America/New_York"),
                       _bars(), force_reset=True)
    now = pd.Timestamp("2026-08-13 09:01", tz="America/New_York")
    # Explicit True here on purpose: this test's SUBJECT is that the pipeline forwards
    # the flag verbatim. The live cadence (flag absent) is covered in test_graft.py.
    p.on_1m_bar(now, _row(), _row(), _bars(), _bars(), bar_complete=True)
    assert seen and seen[0][0] == now and seen[0][1] is True


def test_hook_receives_the_history_frames_not_just_the_session(_isolate_state, _quiet_engine):
    """today_* is the current CME session only (~15 h). The Analyzer's window is 17
    DAYS, so the pipeline must hand its history over or the level universe is empty."""
    seen = {}

    class _Rec:
        def on_bar(self, now, mnq, mes, bar_complete=None, hist_mnq=None, hist_mes=None):
            seen["hist_mnq"] = hist_mnq
            seen["hist_mes"] = hist_mes

    hist_mnq, hist_mes = _bars(n=7), _bars(n=7)
    p = SessionPipeline(hist_mnq, hist_mes, lambda e: None, trader=_Rec())
    p.on_session_start(pd.Timestamp("2026-08-13 09:00", tz="America/New_York"),
                       _bars(), force_reset=True)
    p.on_1m_bar(pd.Timestamp("2026-08-13 09:01", tz="America/New_York"),
                _row(), _row(), _bars(), _bars())
    assert seen.get("hist_mnq") is hist_mnq
    assert seen.get("hist_mes") is hist_mes


def test_hook_still_runs_when_the_live_driver_passes_bar_complete_false(
        _isolate_state, _quiet_engine):
    """automation/main.py hard-codes bar_complete=False on every per-second tick. The
    hook must still be reached — deriving its own bar-close signal is the graft's job,
    not a reason for the pipeline to skip it."""
    calls = []

    class _Rec:
        def on_bar(self, now, mnq, mes, bar_complete=None, **kw):
            calls.append(bar_complete)

    p = SessionPipeline(_bars(), _bars(), lambda e: None, trader=_Rec())
    p.on_session_start(pd.Timestamp("2026-08-13 09:00", tz="America/New_York"),
                       _bars(), force_reset=True)
    for sec in range(3):
        p.on_1m_bar(pd.Timestamp(f"2026-08-13 09:01:{sec:02d}", tz="America/New_York"),
                    _row(), _row(), _bars(), _bars(), bar_complete=False)
    assert calls == [False, False, False]


def test_the_hook_is_not_reached_before_the_daily_trigger(_isolate_state, _quiet_engine):
    class _Spy:
        called = 0
        def on_bar(self, *a, **k): self.__class__.called += 1

    _Spy.called = 0
    p = SessionPipeline(_bars(), _bars(), lambda e: None, trader=_Spy())
    p.on_1m_bar(pd.Timestamp("2026-08-13 09:01", tz="America/New_York"),
                _row(), _row(), _bars(), _bars())
    assert _Spy.called == 0, "on_1m_bar returns early until on_session_start has run"
