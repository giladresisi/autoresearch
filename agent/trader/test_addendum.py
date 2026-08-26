"""Cycle-1 addendum: FactsMaintainer ownership, FACTS_ALL_SESSION, facts-after-death,
the thesis.md 3a near-maturity deferral, and view provenance.

Every scenario here is driven at the LIVE CADENCE -- `bar_complete` left unset, minutes
rolling -- because that is what `automation/main.py` actually does. The explicit
`bar_complete=True` path is the special case and is covered where it is the subject.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.facts.maintainer import FactsMaintainer
from agent.facts.records import FactClass
from agent.facts.store import FactStore
from agent.trader import graft as graft_mod
from agent.trader.executor import Executor

TZ = "America/New_York"


def _bars(start="2026-08-13 09:00", minutes=120, seed=3):
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), periods=minutes, freq="1min")
    rng = np.random.default_rng(seed)
    close = 30000 + np.cumsum(rng.normal(0, 6, minutes))
    return pd.DataFrame({"Open": close, "High": close + 9.0,
                         "Low": close - 9.0, "Close": close,
                         "Volume": 100.0}, index=idx)


BARS = {"MNQ": _bars(), "MES": _bars(seed=4)}
PLAN = {"plan_id": "p1", "direction": "DOWN", "dol": {"price": 29000.0},
        "armed_classes": ["fvg_negation_reversal"], "max_attempts": 3,
        "attempts_used": 0}


def _live(obj, tss, bars=BARS, **kw):
    """Drive at the live cadence: no bar_complete, minute rollover does the work."""
    for ts in tss:
        obj.on_bar(ts, {tk: df[df.index <= ts] for tk, df in bars.items()}, **kw)


def _minutes(first="09:20", last="10:00"):
    lo = pd.Timestamp("2026-08-13 " + first, tz=TZ)
    hi = pd.Timestamp("2026-08-13 " + last, tz=TZ)
    return list(pd.date_range(lo, hi, freq="1min"))


# --------------------------------------------------------------------------- #
# A -- the maintainer owns the store and the cascade
# --------------------------------------------------------------------------- #

def test_maintainer_accumulates_facts_on_the_live_cadence():
    m = FactsMaintainer()
    _live(m, _minutes(), bar_complete=False)
    assert len(list(m.store.query(cls=FactClass.EXTREME, ticker="MNQ"))) > 0


def test_maintainer_refreshes_avg_range_on_bar_close_only():
    m = FactsMaintainer()
    m.on_bar(_minutes()[0], BARS, False)
    assert m.avg_range_1h is None                   # per-second path does no ATR work
    _live(m, _minutes(), bar_complete=True)
    assert m.avg_range_1h is not None


def test_executor_without_a_maintainer_creates_and_drives_its_own(tmp_path):
    ex = Executor(tmp_path, PLAN, arm_ts=_minutes()[0])
    _live(ex, _minutes())
    assert ex.maintainer.last_cascade is not None


def test_executor_with_an_injected_maintainer_does_not_drive_it(tmp_path):
    """The ownership rule: whoever created it drives it. Otherwise the cascade would
    run twice per bar."""
    m = FactsMaintainer()
    ex = Executor(tmp_path, PLAN, arm_ts=_minutes()[0], maintainer=m)
    _live(ex, _minutes())
    assert m.last_cascade is None                   # nobody drove it -- by design


def test_store_kwarg_is_still_an_injection_seam(tmp_path):
    store = FactStore()
    ex = Executor(tmp_path, PLAN, arm_ts=_minutes()[0], store=store)
    _live(ex, _minutes())
    assert ex.maintainer.store is store
    assert len(list(store.query(cls=FactClass.EXTREME, ticker="MNQ"))) > 0


# --------------------------------------------------------------------------- #
# C -- a dead plan must not freeze the store
# --------------------------------------------------------------------------- #

def test_facts_keep_accumulating_after_the_plan_dies(tmp_path):
    # DOWN death is Low.min() <= dol, so a DOL far above price is reached on bar one.
    dead = dict(PLAN, dol={"price": 31_000.0}, direction="DOWN")
    ex = Executor(tmp_path, dead, arm_ts=_minutes()[0])
    _live(ex, _minutes())
    assert ex.bind_state()["plan_alive"] is False
    before = ex.maintainer.last_cascade
    _live(ex, _minutes("10:01", "10:20"))
    assert ex.bind_state()["plan_alive"] is False
    assert ex.maintainer.last_cascade != before, "store froze when the plan died"


def test_a_dead_plan_still_places_no_binding(tmp_path):
    dead = dict(PLAN, dol={"price": 31_000.0}, direction="DOWN")
    ex = Executor(tmp_path, dead, arm_ts=_minutes()[0])
    _live(ex, _minutes())
    assert ex.bind_state()["bound_id"] is None


# --------------------------------------------------------------------------- #
# B -- FACTS_ALL_SESSION, both values
# --------------------------------------------------------------------------- #

class _NoThesis:
    def __call__(self, *a, **kw):
        return None


def _graft(tmp_path, all_session, monkeypatch):
    monkeypatch.setattr(graft_mod, "FACTS_ALL_SESSION", all_session)
    return graft_mod.TraderGraft(tmp_path, _NoThesis(), threaded=False)


def _drive_graft(g, tss):
    for ts in tss:
        g.on_bar(ts, BARS["MNQ"][BARS["MNQ"].index <= ts],
                 BARS["MES"][BARS["MES"].index <= ts])


def test_default_switch_leaves_the_store_cold_until_a_plan_arms(tmp_path, monkeypatch):
    g = _graft(tmp_path, False, monkeypatch)
    _drive_graft(g, _minutes())
    assert g.plan() is None                                  # dark day, no thesis
    assert len(list(g.store.query(cls=FactClass.EXTREME, ticker="MNQ"))) == 0


def test_all_session_switch_fills_the_store_with_no_plan_at_all(tmp_path, monkeypatch):
    g = _graft(tmp_path, True, monkeypatch)
    _drive_graft(g, _minutes())
    assert g.plan() is None                                  # still a dark day...
    assert len(list(g.store.query(cls=FactClass.EXTREME, ticker="MNQ"))) > 0, \
        "FACTS_ALL_SESSION must accumulate facts without a plan"


def test_flipping_the_switch_is_the_only_edit_required(tmp_path, monkeypatch):
    """The switch alone changes behaviour -- nothing else needs touching.

    Each arm is DRIVEN under its own flag value: `FACTS_ALL_SESSION` is a module global
    read at call time, so constructing both up front would run both under whichever
    value was set last.
    """
    cold = _graft(tmp_path / "a", False, monkeypatch)
    _drive_graft(cold, _minutes())
    assert len(list(cold.store.query(cls=FactClass.EXTREME, ticker="MNQ"))) == 0

    warm = _graft(tmp_path / "b", True, monkeypatch)
    _drive_graft(warm, _minutes())
    assert len(list(warm.store.query(cls=FactClass.EXTREME, ticker="MNQ"))) > 0


def test_the_switch_defaults_to_todays_behaviour():
    assert graft_mod.FACTS_ALL_SESSION is False


# --------------------------------------------------------------------------- #
# D -- thesis.md 3a near-maturity WAIT (a deferral live, not a jump)
# --------------------------------------------------------------------------- #

class _Recorder:
    def __init__(self, thesis=None):
        self.calls = []
        self._thesis = thesis or {"bias": "DOWN", "dol": {"price": 29000.0}}

    def __call__(self, facts_text, context_text, facts, *, evidence_magnitude=None):
        self.calls.append(True)
        return self._thesis


def _analyzer_with_candidates(tmp_path, monkeypatch, cands, backend):
    import agent.trader.analyzer as an

    # Deferral tests must ENABLE deferral explicitly. `NEAR_MATURITY_WAIT` is
    # False in production (cycle-1 decision: all ten recorded panel dates armed
    # at exactly 09:20 and none ever retargeted), so a test that relied on the
    # module default would silently pass vacuously.
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", True)

    def fake_assemble(store, bars, now):
        return "facts", "", {"near_maturity_candidates": cands, "levels": {"a": 1}}, {}
    monkeypatch.setattr(an, "assemble_facts", fake_assemble)
    return an.Analyzer(tmp_path, backend, threaded=False)


def test_non_eligible_near_maturity_candidate_defers_the_call(tmp_path, monkeypatch):
    resolves = "2026-08-13 10:00:00-04:00"
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": False, "resolves_at": resolves}], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert rec.calls == [], "should have waited, not called"
    assert a.deferred_until() == pd.Timestamp(resolves)


def test_the_deferred_call_fires_once_the_close_has_happened(tmp_path, monkeypatch):
    resolves = "2026-08-13 10:00:00-04:00"
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": False, "resolves_at": resolves}], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    a.maybe_run(pd.Timestamp("2026-08-13 09:59", tz=TZ), BARS)
    assert rec.calls == []                                   # still waiting
    a.maybe_run(pd.Timestamp("2026-08-13 10:00", tz=TZ), BARS)
    assert len(rec.calls) == 1
    assert a.standing_thesis() is not None


def test_a_preconfirm_eligible_candidate_does_not_defer(tmp_path, monkeypatch):
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": True, "resolves_at": "2026-08-13 10:00:00-04:00"}], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert len(rec.calls) == 1
    assert a.deferred_until() is None


def test_a_wait_beyond_the_cap_calls_now_instead(tmp_path, monkeypatch):
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": False, "resolves_at": "2026-08-13 23:00:00-04:00"}], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert len(rec.calls) == 1, "an unbounded wait would cost the whole session"


def test_the_wait_can_be_switched_off(tmp_path, monkeypatch):
    import agent.trader.analyzer as an
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": False, "resolves_at": "2026-08-13 10:00:00-04:00"}], rec)
    # AFTER the helper, which enables the wait — this test owns the off case.
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert len(rec.calls) == 1


def test_only_the_earliest_pending_close_is_awaited(tmp_path, monkeypatch):
    rec = _Recorder()
    a = _analyzer_with_candidates(tmp_path, monkeypatch, [
        {"preconfirm_eligible": False, "resolves_at": "2026-08-13 10:00:00-04:00"},
        {"preconfirm_eligible": False, "resolves_at": "2026-08-13 09:45:00-04:00"},
    ], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert a.deferred_until() == pd.Timestamp("2026-08-13 09:45:00-04:00")


def test_a_degraded_facts_build_never_becomes_a_silent_all_day_hold(tmp_path, monkeypatch):
    import agent.trader.analyzer as an

    def boom(store, bars, now):
        raise RuntimeError("degraded")
    monkeypatch.setattr(an, "assemble_facts", boom)
    a = an.Analyzer(tmp_path, _Recorder(), threaded=False)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert a.deferred_until() is None


def test_a_deferral_does_not_survive_into_the_next_session(tmp_path, monkeypatch):
    rec = _Recorder()
    a = _analyzer_with_candidates(
        tmp_path, monkeypatch,
        [{"preconfirm_eligible": False, "resolves_at": "2026-08-13 10:00:00-04:00"}], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert a.deferred_until() is not None
    a.maybe_run(pd.Timestamp("2026-08-14 09:20", tz=TZ), BARS)
    assert len(rec.calls) >= 1


# --------------------------------------------------------------------------- #
# F -- provenance describes the view that was actually sent
# --------------------------------------------------------------------------- #

def test_provenance_counts_the_view_that_was_sent(tmp_path, monkeypatch):
    rec = _Recorder()
    a = _analyzer_with_candidates(tmp_path, monkeypatch, [], rec)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    h = a.facts_health()
    assert h["levels"] == 1
    assert h["facts_text_chars"] > 0
    assert h["degraded"] is False


def test_an_empty_level_universe_is_visible_in_provenance(tmp_path, monkeypatch):
    """The 2026-07-08 signature: structurally valid, materially empty."""
    import agent.trader.analyzer as an
    monkeypatch.setattr(an, "assemble_facts",
                        lambda s, b, n: ("", "", {"degraded": True}, {}))
    a = an.Analyzer(tmp_path, _Recorder(), threaded=False)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    h = a.facts_health()
    assert h["levels"] == 0 and h["degraded"] is True


def test_the_analyzer_never_builds_a_factstore():
    import inspect
    import agent.trader.analyzer as an
    src = inspect.getsource(an)
    assert "FactStore(" not in src and "run_batch" not in src
