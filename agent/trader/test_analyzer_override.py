"""Plan 37 D2/D3 — the Analyzer bypasses the model when the override fires."""
import json

import pandas as pd
import pytest

from agent.trader.analyzer import Analyzer, THESIS_FILE, stands

TZ = "America/New_York"
ARM = pd.Timestamp("2026-09-01 09:20", tz=TZ)


class _SpyBackend:
    """Records whether the model was reached, and what it was asked."""

    def __init__(self, thesis=None):
        self.calls = 0
        self._thesis = thesis or {"bias": "DOWN", "regime": "TREND",
                                  "confidence": "MEDIUM",
                                  "dol": {"level": "x", "price": 29000.0},
                                  "falsified_if": [], "evidence": [{"criterion": "P3"}]}

    def __call__(self, facts_text, context_text, facts, *, evidence_magnitude=None):
        self.calls += 1
        return self._thesis, {"verdict": "clean", "latency_sec": 1.0}


def _facts(stretch=None, up_menu=True, down_menu=True):
    dol = {}
    if up_menu:
        dol["UP"] = [{"id": "D1", "level": "TDO", "price": 29486.25, "band": "FAR"}]
    if down_menu:
        dol["DOWN"] = [{"id": "D1", "level": "london(cur)_low", "price": 29016.75}]
    return {"session_stretch": stretch or {}, "menus": {"dol": dol},
            "levels": {}, "fvg_zones": {}, "now_price": 29097.5}


_FRESH_DOWN = {"MNQ": {"direction": "DOWN", "size": 488.25, "age_minutes": 2,
                       "retrace_pct": 3.0, "mid": 29326.88, "beyond_mid": True}}
_STALE = {"MNQ": {"direction": "DOWN", "size": 488.25, "age_minutes": 300,
                  "retrace_pct": 80.0, "mid": 29326.88, "beyond_mid": False}}


def _analyzer(tmp_path, backend, monkeypatch, facts):
    ax = Analyzer(tmp_path, backend, threaded=False)
    monkeypatch.setattr("agent.trader.analyzer.assemble_facts",
                        lambda store, bars, now: ("facts text", "", facts, {}))
    return ax


def test_8_a_firing_override_never_reaches_the_backend(tmp_path, monkeypatch):
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(_FRESH_DOWN))
    th = ax._call(ARM, {})
    assert spy.calls == 0, "the model must not be asked when the override fires"
    assert th["bias"] == "UP"


def test_9_the_persisted_override_thesis_stands_and_carries_its_provenance(tmp_path,
                                                                          monkeypatch):
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(_FRESH_DOWN))
    th = ax._call(ARM, {})

    assert th["thesis_source"] == "stretch_override"
    assert stands(th) is True
    # the DOL is build_menus' own D1 for the FORCED side, not the model's pick
    assert th["dol"] == {"level": "TDO", "price": 29486.25}
    assert th["evidence"] == [] and th["falsified_if"] == []
    assert th["regime"] is None and th["confidence"] is None
    assert "still extending" in th["override_reason"]

    blob = json.loads((tmp_path / THESIS_FILE).read_text(encoding="utf-8"))
    assert blob["stands"] is True
    assert blob["thesis"]["thesis_source"] == "stretch_override"
    # provenance says which path produced it; no latency, no usage, no retries
    assert blob["call_meta"] == {"verdict": "stretch_override"}


def test_10_a_non_firing_day_reaches_the_backend_unchanged(tmp_path, monkeypatch):
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(_STALE))
    th = ax._call(ARM, {})
    assert spy.calls == 1
    assert th["bias"] == "DOWN"
    assert "thesis_source" not in th


def test_11_degraded_facts_fall_through_to_the_model_rather_than_guess(tmp_path,
                                                                      monkeypatch):
    """A missing stretch must LOSE the override, never invent a direction from it."""
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(None))
    ax._call(ARM, {})
    assert spy.calls == 1


def test_an_empty_menu_on_the_forced_side_falls_through(tmp_path, monkeypatch):
    """`stands()` needs a DOL. No UP menu -> the override cannot produce a standing
    thesis, so it hands back to the model instead of arming a DOL-less plan."""
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(_FRESH_DOWN, up_menu=False))
    ax._call(ARM, {})
    assert spy.calls == 1


def test_12_an_override_day_neither_reads_nor_writes_the_thesis_cache(tmp_path,
                                                                     monkeypatch):
    """The cache sits inside the backend (`CachedThesisBackend`). Not reaching the backend
    is what guarantees no read and no write — asserted through the spy so a future
    refactor that cached at a different seam would fail here."""
    spy = _SpyBackend()
    ax = _analyzer(tmp_path, spy, monkeypatch, _facts(_FRESH_DOWN))
    ax._call(ARM, {})
    assert spy.calls == 0


def test_the_override_thesis_is_exempt_from_the_bias_vs_net_score_check():
    """An override thesis declares UP against facts whose own mid reads score DOWN. The
    ARI_THESIS_BIAS exemption is keyed on `thesis_source`, stated explicitly so a future
    refactor that validates inside the Analyzer does not silently reject every override."""
    import sys, os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contracts"))
    from agent.contracts.validate_contracts import validate_thesis

    facts = {"levels": {"up_pool": {"price": 110.0, "side": "high", "swept": False,
                                    "depleted": False}},
             "now_price": 100.0, "mid_position": {}}
    base = {"bias": "UP", "regime": None, "confidence": None,
            "dol": {"level": "up_pool", "price": 110.0},
            "falsified_if": [{"type": "price_beyond", "price": 90.0, "side": "below"}],
            "evidence": []}

    exempt = validate_thesis({**base, "thesis_source": "stretch_override"}, facts)
    assert "ARI_THESIS_BIAS" not in exempt.codes()
    # and the exemption is narrow: without the stamp the same thesis is still judged
    plain = validate_thesis(dict(base), facts)
    assert "ARI_THESIS_BIAS" in plain.codes()
