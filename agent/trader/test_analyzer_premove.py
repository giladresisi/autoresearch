"""Plan 46 T3 — the Analyzer's UNRELATED path and the Planner pass-through.

The classifier is monkeypatched to return a chosen `PremoveContext` (its own behaviour is
pinned in `test_premove_context.py`); `assemble_facts` is monkeypatched as in
`test_analyzer_override.py`, whose spy and stretch fixtures this module reuses.
"""
import json

import pandas as pd
import pytest

import agent.stretch_override as so
from agent.trader import premove_context as pc
from agent.trader.analyzer import PREMOVE_FILE, THESIS_FILE, Analyzer, stands
from agent.trader.planner import derive_plan
from agent.trader.test_analyzer_override import _FRESH_DOWN, _STALE, _SpyBackend, _facts

TZ = "America/New_York"
ARM = pd.Timestamp("2026-09-01 09:20", tz=TZ)


def _ctx(status=pc.UNRELATED, leg="UP", origin=29000.0, extreme=29400.0, **kw):
    return pc.PremoveContext(
        status=status, reason=f"synthetic {status}", leg_direction=leg, origin=origin,
        origin_ts=pd.Timestamp("2026-08-31 18:00", tz=TZ), extreme=extreme,
        extreme_ts=pd.Timestamp("2026-09-01 09:00", tz=TZ),
        size=(None if origin is None or extreme is None else abs(extreme - origin)),
        size_pct=1.36, px_0919=29400.0, cut_pts=121.66, boundary=ARM,
        params=dict(pc.DEFAULT_PARAMS.__dict__), **kw)


def _analyzer(tmp_path, monkeypatch, facts, *, mode="on", ctx=None, backend=None,
              raises=None):
    spy = backend or _SpyBackend()
    ax = Analyzer(tmp_path, spy, threaded=False)
    monkeypatch.setattr("agent.trader.analyzer.assemble_facts",
                        lambda store, bars, now: ("facts text", "", facts, {}))
    monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", mode)
    calls = []

    def _classify(frame, now, params=None):
        calls.append(now)
        if raises is not None:
            raise raises
        return ctx if ctx is not None else _ctx()
    monkeypatch.setattr(pc, "classify", _classify)
    return ax, spy, calls


def _artifact(tmp_path):
    p = tmp_path / PREMOVE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _baseline(tmp_path, monkeypatch, facts):
    """What the flag-off Analyzer returns for the same facts (today's path)."""
    ax, spy, _ = _analyzer(tmp_path / "off", monkeypatch, facts, mode="off")
    return ax._call(ARM, {"MNQ": None}), spy.calls


# --------------------------------------------------------------------------- #
# happy path                                                                    #
# --------------------------------------------------------------------------- #

def test_unrelated_forces_against_the_leg_without_a_model_call(tmp_path, monkeypatch):
    ax, spy, calls = _analyzer(tmp_path, monkeypatch, _facts(_STALE))
    th = ax._call(ARM, {"MNQ": None})
    assert spy.calls == 0 and len(calls) == 1
    assert th["bias"] == "DOWN"
    assert th["thesis_source"] == "premove_unrelated"
    assert th["dol"] == {"level": "premove_leg_mid", "price": 29200.0}
    assert th["regime"] is None and th["confidence"] is None
    assert th["falsified_if"] == [] and th["evidence"] == []
    pm = th["premove"]
    assert pm["status"] == "UNRELATED" and pm["leg_direction"] == "UP"
    assert pm["forced_direction"] == "DOWN" and pm["mid_0920"] == 29200.0
    assert pm["origin"] == 29000.0 and pm["extreme"] == 29400.0
    assert pm["boundary"] == ARM.isoformat()
    assert stands(th) is True
    blob = json.loads((tmp_path / THESIS_FILE).read_text(encoding="utf-8"))
    assert blob["call_meta"] == {"verdict": "premove_unrelated"}
    assert blob["thesis"]["premove"]["mid_0920"] == 29200.0
    art = _artifact(tmp_path)
    assert art["mode"] == "on" and art["action"] == "forced"
    assert art["context"]["status"] == "UNRELATED"


def test_unrelated_day_neither_reads_nor_writes_the_thesis_cache(tmp_path, monkeypatch):
    from agent.trader.cached_backend import CachedThesisBackend
    from agent.trader.thesis_cache import ThesisCache
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(cache_dir))
    spy = _SpyBackend()
    backend = CachedThesisBackend(spy, cache=ThesisCache(), allow_calls=True,
                                  boundary_hint="2026-09-01")
    ax, _, _ = _analyzer(tmp_path / "st", monkeypatch, _facts(_STALE), backend=backend)
    th = ax._call(ARM, {"MNQ": None})
    assert th["thesis_source"] == "premove_unrelated"
    assert (backend.hits, backend.misses, backend.calls) == (0, 0, 0)
    assert list(cache_dir.iterdir()) == []


# --------------------------------------------------------------------------- #
# fall-through                                                                  #
# --------------------------------------------------------------------------- #

def test_part_falls_through_to_the_model_and_arm1_is_suppressed(tmp_path, monkeypatch):
    ax, spy, _ = _analyzer(tmp_path, monkeypatch, _facts(_FRESH_DOWN),
                           ctx=_ctx(pc.PART))
    th = ax._call(ARM, {"MNQ": None})
    assert spy.calls == 1                        # arm 1 would have fired; it did not
    assert th["bias"] == "DOWN" and "thesis_source" not in th
    art = _artifact(tmp_path)
    assert art["action"] == "arm1_suppressed" and art["arm1_would_fire"] is True


@pytest.mark.parametrize("status", [pc.NOT_BIG, pc.NO_LEG, pc.UNKNOWN])
def test_not_big_keeps_arm1_exactly(tmp_path, monkeypatch, status):
    want, want_calls = _baseline(tmp_path, monkeypatch, _facts(_FRESH_DOWN))
    ax, spy, _ = _analyzer(tmp_path / "on", monkeypatch, _facts(_FRESH_DOWN),
                           ctx=_ctx(status))
    got = ax._call(ARM, {"MNQ": None})
    assert got == want and got["thesis_source"] == "stretch_override"
    assert spy.calls == want_calls == 0
    assert _artifact(tmp_path / "on")["action"] == "today"


def test_missing_history_falls_through_to_todays_path(tmp_path, monkeypatch):
    short = pc.PremoveContext(status=pc.UNKNOWN, reason="history 7.0 d < 15 d",
                              leg_direction="UP", origin=29000.0, extreme=29400.0,
                              size=400.0)
    for facts in (_facts(_FRESH_DOWN), _facts(_STALE)):
        want, want_calls = _baseline(tmp_path / str(id(facts)), monkeypatch, facts)
        ax, spy, _ = _analyzer(tmp_path / f"on{id(facts)}", monkeypatch, facts, ctx=short)
        assert ax._call(ARM, {"MNQ": None}) == want and spy.calls == want_calls


def test_classifier_raising_falls_through(tmp_path, monkeypatch):
    want, _ = _baseline(tmp_path, monkeypatch, _facts(_FRESH_DOWN))
    ax, spy, _ = _analyzer(tmp_path / "on", monkeypatch, _facts(_FRESH_DOWN),
                           raises=RuntimeError("boom"))
    assert ax._call(ARM, {"MNQ": None}) == want
    art = _artifact(tmp_path / "on")
    assert art["context"]["status"] == "UNKNOWN" and "boom" in art["context"]["reason"]


def test_degraded_facts_fall_through(tmp_path, monkeypatch):
    facts = dict(_facts(_STALE), degraded=True)
    ax, spy, _ = _analyzer(tmp_path, monkeypatch, facts)
    th = ax._call(ARM, {"MNQ": None})
    assert spy.calls == 1 and "thesis_source" not in th
    assert _artifact(tmp_path)["note"] == "degraded facts: not forced"


def test_unbuildable_forced_thesis_goes_to_the_model_not_arm1(tmp_path, monkeypatch):
    ax, spy, _ = _analyzer(tmp_path, monkeypatch, _facts(_FRESH_DOWN),
                           ctx=_ctx(origin=None))
    th = ax._call(ARM, {"MNQ": None})
    assert spy.calls == 1 and "thesis_source" not in th      # not arm 1's forced UP
    art = _artifact(tmp_path)
    assert art["action"] == "arm1_suppressed" and "could not be built" in art["note"]


# --------------------------------------------------------------------------- #
# flag                                                                          #
# --------------------------------------------------------------------------- #

def test_flag_off_never_calls_the_classifier_and_writes_no_artifact(tmp_path, monkeypatch):
    ax, spy, calls = _analyzer(tmp_path, monkeypatch, _facts(_FRESH_DOWN), mode="off",
                               raises=AssertionError("must not be called"))
    th = ax._call(ARM, {"MNQ": None})
    assert calls == [] and _artifact(tmp_path) is None
    assert th["thesis_source"] == "stretch_override"
    assert ax._premove({"MNQ": None}, ARM) is None


def test_shadow_writes_the_artifact_and_changes_nothing(tmp_path, monkeypatch):
    for facts in (_facts(_FRESH_DOWN), _facts(_STALE)):
        tag = str(id(facts))
        want, want_calls = _baseline(tmp_path / tag, monkeypatch, facts)
        ax, spy, calls = _analyzer(tmp_path / f"sh{tag}", monkeypatch, facts, mode="shadow")
        got = ax._call(ARM, {"MNQ": None})
        assert got == want and spy.calls == want_calls and len(calls) == 1
        art = _artifact(tmp_path / f"sh{tag}")
        assert art["mode"] == "shadow" and art["action"] == "shadow"
        assert art["context"]["status"] == "UNRELATED"
        assert art["would_force"] == "DOWN"


def test_arm1_disabled_constant(tmp_path, monkeypatch):
    assert so.ARM1_ENABLED is True                       # the shipped default
    fresh = so.stretch_override(_FRESH_DOWN)
    assert fresh["fires"] is True
    monkeypatch.setattr(so, "ARM1_ENABLED", False)
    off = so.stretch_override(_FRESH_DOWN)
    assert off["fires"] is False and off["reason"] == "arm 1 disabled"
    ax, spy, _ = _analyzer(tmp_path, monkeypatch, _facts(_FRESH_DOWN), mode="off")
    ax._call(ARM, {"MNQ": None})
    assert spy.calls == 1


# --------------------------------------------------------------------------- #
# planner                                                                       #
# --------------------------------------------------------------------------- #

def test_premove_block_is_carried_only_when_present():
    base = {"bias": "DOWN", "dol": {"level": "premove_leg_mid", "price": 29200.0},
            "falsified_if": [], "thesis_id": None}
    block = {"status": "UNRELATED", "forced_direction": "DOWN", "mid_0920": 29200.0}
    with_pm = derive_plan(dict(base, premove=block), [], ARM)
    without = derive_plan(dict(base), [], ARM)
    assert with_pm["premove"] == block and with_pm["premove"] is not block
    assert "premove" not in without
    assert with_pm["plan_id"] == without["plan_id"]
    assert {k: v for k, v in with_pm.items() if k != "premove"} == without
    assert "premove" not in derive_plan(dict(base, premove=None), [], ARM)


def test_a_failing_route_falls_back_to_todays_path_not_a_dark_day(tmp_path, monkeypatch):
    """An UNRELATED day whose route raises is treated like an unbuildable forced thesis:
    arm 1 stays suppressed and the MODEL decides -- never a dark day."""
    ax, spy, _ = _analyzer(tmp_path, monkeypatch, _facts(_FRESH_DOWN))

    def _boom(*a, **k):
        raise RuntimeError("route")
    monkeypatch.setattr(ax, "_premove_route_inner", _boom)
    got = ax._call(ARM, {"MNQ": None})
    assert spy.calls == 1 and got is not None and got == ax.standing_thesis()
    assert "thesis_source" not in got


def test_the_forced_thesis_is_exempt_from_the_bias_vs_net_score_check():
    """Same explicit exemption as plan 37's override (`validate_contracts`)."""
    from agent.contracts.validate_contracts import validate_thesis
    facts = {"levels": {"up_pool": {"price": 110.0, "side": "high", "swept": False,
                                    "depleted": False}},
             "now_price": 100.0, "mid_position": {}}
    base = {"bias": "UP", "regime": None, "confidence": None,
            "dol": {"level": "up_pool", "price": 110.0},
            "falsified_if": [{"type": "price_beyond", "price": 90.0, "side": "below"}],
            "evidence": []}
    got = validate_thesis({**base, "thesis_source": "premove_unrelated"}, facts)
    assert "ARI_THESIS_BIAS" not in got.codes()
    assert "ARI_THESIS_BIAS" in validate_thesis(dict(base), facts).codes()
