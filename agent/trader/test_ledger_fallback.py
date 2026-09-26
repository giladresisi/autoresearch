"""ACT_THESIS_FAILSAFE: a thesis call that exhausts its retries degrades to a
deterministic ledger-fallback thesis (default) or to the NEUTRAL failsafe (flag on)."""
import pytest

from agent import run_agent as ra
from agent.trader.cached_backend import CachedThesisBackend
from agent.trader.thesis_cache import ThesisCache

import validate_contracts   # on sys.path via agent.run_agent

FALSIFIER_UP = {"type": "n_closes_beyond", "price": 30839.25, "side": "below", "tf": "5m", "n": 2}
MENUS = {
    "dol": {"UP": [{"id": "D1", "level": "prev2_day_high", "price": 31094.75},
                   {"id": "D2", "level": "week_high", "price": 31200.0}],
            "DOWN": [{"id": "D1", "level": "london(cur)_low", "price": 30766.5}]},
    "predicates": {
        "UP": [{"id": "E1", "family": "evidence", "predicate": {"type": "x"}},
               {"id": "F1", "family": "falsification", "predicate": FALSIFIER_UP,
                "recommended": True}],
        "DOWN": [],
    },
}


def _attempt(bias="DOWN"):
    return {"bias": bias, "regime": "HYBRID", "recall": {"events": [], "max_age_min": 60},
            "evidence": [{"criterion": "P3"}]}


@pytest.fixture
def net(monkeypatch):
    """Pin the code net score the fallback reads, independent of real scoring."""
    def _set(value, no_liquidity=False):
        bias = "UP" if value > 0 else "DOWN" if value < 0 else "NEUTRAL"
        if no_liquidity:
            bias = "NEUTRAL"
        monkeypatch.setattr(validate_contracts, "score_thesis_evidence",
                            lambda ev, **kw: {"net_score": value, "expected_bias": bias,
                                              "no_liquidity": no_liquidity, "contradiction": False,
                                              "confidence_ceiling": "LOW",
                                              "scored_evidence": list(ev)})
    return _set


def test_fallback_takes_the_net_score_side_with_menu_d1_and_recommended_falsifier(net):
    net(0.75)
    t = ra._ledger_fallback_thesis([_attempt("DOWN")], MENUS, {})
    assert t["bias"] == "UP"                       # ledger wins over the model's DOWN
    assert t["dol"] == {"level": "prev2_day_high", "price": 31094.75}
    assert t["falsified_if"] == [FALSIFIER_UP]
    assert t["thesis_source"] == "ledger_fallback" and t["confidence"] == "LOW"
    from agent.trader.analyzer import stands
    assert stands(t)


def test_exact_tie_uses_the_models_last_directional_bias(net):
    net(0.0)
    t = ra._ledger_fallback_thesis([_attempt("DOWN")], MENUS, {})
    assert t["bias"] == "DOWN" and t["dol"]["price"] == 30766.5
    assert t["falsified_if"] == []                 # no recommended F-row on that side


@pytest.mark.parametrize("case", ["no_menu_rows", "no_liquidity", "tie_and_neutral_model",
                                  "no_menus"])
def test_unbuildable_fallback_returns_none(net, case):
    menus = MENUS
    attempt = _attempt("DOWN")
    if case == "no_menu_rows":
        net(0.75)
        menus = {"dol": {"UP": [], "DOWN": MENUS["dol"]["DOWN"]}}
    elif case == "no_liquidity":
        net(0.75, no_liquidity=True)
    elif case == "tie_and_neutral_model":
        net(0.0)
        attempt = _attempt("NEUTRAL")
    else:
        net(0.75)
        menus = None
    assert ra._ledger_fallback_thesis([attempt], menus, {}) is None


class _Always:
    def __init__(self, ok):
        self.ok = ok

    def messages(self):
        return [] if self.ok else ["[X] invalid"]


def _run(fallback):
    backend = ra.StubBackend(responses=[{"bias": "DOWN"}] * (ra.MAX_RETRIES + 1))
    return ra._run_call(backend, "sys", "user", {}, validate_block=lambda d: _Always(False),
                        failsafe_block={"bias": "NEUTRAL"}, fallback_block=fallback)


def test_run_call_uses_the_fallback_block_after_exhausting_retries():
    out = _run(lambda attempts: {"bias": "UP", "n": len(attempts)})
    assert out.verdict == "ledger_fallback" and out.fallback
    assert out.block == {"bias": "UP", "n": ra.MAX_RETRIES + 1}


def test_run_call_keeps_the_failsafe_when_the_fallback_cannot_build():
    out = _run(lambda attempts: None)
    assert out.verdict == "failsafe" and out.block == {"bias": "NEUTRAL"}


def test_run_call_never_consults_the_fallback_on_a_clean_answer():
    backend = ra.StubBackend(responses=[{"bias": "NEUTRAL"}])
    out = ra._run_call(backend, "sys", "user", {}, validate_block=lambda d: _Always(True),
                       failsafe_block={}, fallback_block=lambda a: pytest.fail("consulted"))
    assert out.verdict == "clean" and out.block["bias"] == "NEUTRAL"


@pytest.mark.parametrize("value,enabled", [(None, False), ("", False), ("0", False),
                                           ("false", False), ("true", True), ("1", True),
                                           ("ON", True)])
def test_flag_defaults_to_off(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("ACT_THESIS_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("ACT_THESIS_FAILSAFE", value)
    assert ra.thesis_failsafe_enabled() is enabled


@pytest.mark.parametrize("enabled,expected", [(False, "ledger_fallback"), (True, "failsafe")])
def test_decide_thesis_honours_the_flag(monkeypatch, net, enabled, expected):
    net(0.75)
    monkeypatch.setattr(ra, "thesis_failsafe_enabled", lambda: enabled)
    monkeypatch.setattr(ra, "build_system_prompt", lambda docs_root=None: "sys")
    monkeypatch.setattr(validate_contracts, "validate_thesis", lambda d, f: _Always(False))
    backend = ra.StubBackend(responses=[_attempt("DOWN")] * (ra.MAX_RETRIES + 1))
    out = ra.decide_thesis("facts", "", {"menus": MENUS}, backend)
    assert out.verdict == expected
    assert out.block["bias"] == ("UP" if not enabled else "NEUTRAL")


def test_a_ledger_fallback_is_not_recorded_in_the_thesis_cache(tmp_path):
    cache = ThesisCache(tmp_path)
    backend = CachedThesisBackend(
        lambda *a, **k: ({"bias": "UP", "dol": {"level": "x", "price": 1.0}},
                         {"verdict": "ledger_fallback"}),
        cache=cache, model_id="m", system_prompt_fn=lambda: "s", task_prompt="t",
        schema_fn=lambda f: {}, boundary_hint="2026-09-25")
    thesis, meta = backend("facts", "", {})
    assert thesis["bias"] == "UP" and meta["cached"] is False
    assert cache.get_record(backend.last_key) is None
