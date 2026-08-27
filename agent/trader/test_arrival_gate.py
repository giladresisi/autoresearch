# agent/trader/test_arrival_gate.py
import pandas as pd
import pytest

TZ = "America/New_York"
BARS = {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()}
THESIS = {"bias": "DOWN", "dol": {"price": 1.0}}


def _analyzer(tmp_path, monkeypatch, latency):
    import agent.trader.analyzer as an
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    monkeypatch.setattr(an, "assemble_facts",
                        lambda store, bars, now: ("facts", "", {"levels": {"a": 1}}, {}))
    return an.Analyzer(tmp_path, lambda *a, **k: THESIS, threaded=False,
                       arrival_latency_sec=latency)


def test_zero_latency_is_visible_immediately_preserving_cycle_1_behaviour(tmp_path, monkeypatch):
    a = _analyzer(tmp_path, monkeypatch, 0.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.standing_thesis(now=t) is not None


def test_the_thesis_is_invisible_before_the_latency_has_elapsed(tmp_path, monkeypatch):
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.standing_thesis(now=t) is None
    assert a.standing_thesis(now=t + pd.Timedelta(seconds=39)) is None


def test_the_thesis_becomes_visible_exactly_at_the_latency_boundary(tmp_path, monkeypatch):
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.standing_thesis(now=t + pd.Timedelta(seconds=40)) is not None


def test_the_thesis_stays_visible_after_the_boundary(tmp_path, monkeypatch):
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.standing_thesis(now=t + pd.Timedelta(minutes=30)) is not None


def test_calling_without_now_bypasses_the_gate(tmp_path, monkeypatch):
    """Back-compat: cycle-1 callers pass no `now` and must keep working."""
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    a.maybe_run(pd.Timestamp("2026-08-12 09:20:00", tz=TZ), BARS)
    assert a.standing_thesis() is not None


def test_armed_at_records_the_bar_time_of_the_arm(tmp_path, monkeypatch):
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.armed_at() == t


def test_armed_at_survives_a_reload_so_the_gate_holds_across_a_restart(tmp_path, monkeypatch):
    import agent.trader.analyzer as an
    a = _analyzer(tmp_path, monkeypatch, 40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    b = an.Analyzer(tmp_path, lambda *x, **k: None, threaded=False,
                    arrival_latency_sec=40.0)
    assert b.armed_at() == t
    assert b.standing_thesis(now=t + pd.Timedelta(seconds=10)) is None
    assert b.standing_thesis(now=t + pd.Timedelta(seconds=60)) is not None


def test_a_dark_day_stays_dark_regardless_of_the_gate(tmp_path, monkeypatch):
    import agent.trader.analyzer as an
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    monkeypatch.setattr(an, "assemble_facts",
                        lambda store, bars, now: ("f", "", {"levels": {}}, {}))
    a = an.Analyzer(tmp_path, lambda *x, **k: {"bias": "NEUTRAL", "dol": None},
                    threaded=False, arrival_latency_sec=40.0)
    t = pd.Timestamp("2026-08-12 09:20:00", tz=TZ)
    a.maybe_run(t, BARS)
    assert a.standing_thesis(now=t + pd.Timedelta(minutes=5)) is None


def test_the_graft_passes_bar_time_into_standing_thesis(monkeypatch):
    """Without this the gate is inert in the only place it matters."""
    import inspect
    import agent.trader.graft as g
    src = inspect.getsource(g.TraderGraft._run)
    assert "standing_thesis(now=" in src
