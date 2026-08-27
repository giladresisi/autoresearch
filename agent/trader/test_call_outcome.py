# agent/trader/test_call_outcome.py
import json
import pandas as pd
import pytest

TZ = "America/New_York"
BARS = {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()}


def _analyzer(tmp_path, monkeypatch, backend):
    import agent.trader.analyzer as an
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    monkeypatch.setattr(an, "assemble_facts",
                        lambda store, bars, now: ("facts", "", {"levels": {"a": 1}}, {}))
    return an.Analyzer(tmp_path, backend, threaded=False)


def test_a_backend_returning_a_bare_dict_still_works(tmp_path, monkeypatch):
    """Back-compat: the cycle-1 contract (bare dict) must keep working."""
    a = _analyzer(tmp_path, monkeypatch,
                  lambda ft, ct, f, **kw: {"bias": "DOWN", "dol": {"price": 1.0}})
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert a.standing_thesis() is not None
    assert a.call_meta() == {}


def test_latency_tokens_and_cost_are_captured_when_the_backend_supplies_them(tmp_path, monkeypatch):
    meta = {"latency_sec": 41.5, "usage": {"input_tokens": 94000, "output_tokens": 1500},
            "cost": 0.1097, "retries": 0, "verdict": "ok"}
    a = _analyzer(tmp_path, monkeypatch,
                  lambda ft, ct, f, **kw: ({"bias": "DOWN", "dol": {"price": 1.0}}, meta))
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    got = a.call_meta()
    assert got["latency_sec"] == 41.5
    assert got["usage"]["input_tokens"] == 94000
    assert got["cost"] == 0.1097


def test_call_meta_is_persisted_to_thesis_state_json(tmp_path, monkeypatch):
    meta = {"latency_sec": 12.0, "usage": {"input_tokens": 10}, "cost": 0.01,
            "retries": 2, "verdict": "ok"}
    a = _analyzer(tmp_path, monkeypatch,
                  lambda ft, ct, f, **kw: ({"bias": "UP", "dol": {"price": 2.0}}, meta))
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    blob = json.loads(open(a.path, encoding="utf-8").read())
    assert blob["call_meta"]["latency_sec"] == 12.0
    assert blob["call_meta"]["retries"] == 2


def test_call_meta_survives_a_reload(tmp_path, monkeypatch):
    """A mid-session restart must not lose the provenance of the standing thesis."""
    import agent.trader.analyzer as an
    meta = {"latency_sec": 30.0, "usage": {}, "cost": 0.1, "retries": 1, "verdict": "ok"}
    a = _analyzer(tmp_path, monkeypatch,
                  lambda ft, ct, f, **kw: ({"bias": "UP", "dol": {"price": 2.0}}, meta))
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    b = an.Analyzer(tmp_path, lambda *args, **kw: None, threaded=False)
    assert b.call_meta()["latency_sec"] == 30.0


def test_a_failing_backend_records_no_meta_and_leaves_no_thesis(tmp_path, monkeypatch):
    def _boom(ft, ct, f, **kw):
        raise RuntimeError("model down")
    a = _analyzer(tmp_path, monkeypatch, _boom)
    a.maybe_run(pd.Timestamp("2026-08-13 09:20", tz=TZ), BARS)
    assert a.standing_thesis() is None
    assert a.call_meta() == {}


def test_decide_thesis_adapter_extracts_meta_from_the_call_outcome(monkeypatch):
    """`thesis_via_decide_thesis` must widen to (thesis, meta), not drop the wrapper."""
    import agent.trader.analyzer as an

    class _Outcome:
        block = {"bias": "DOWN", "dol": {"price": 1.0}}
        latency_sec = 55.0
        usage = {"input_tokens": 5}
        cost = 0.2
        retries = 1
        verdict = "ok"

    import agent.run_agent as ra
    monkeypatch.setattr(ra, "decide_thesis", lambda *a, **k: _Outcome())
    call = an.thesis_via_decide_thesis(object())
    thesis, meta = call("facts", "", {}, evidence_magnitude={})
    assert thesis["bias"] == "DOWN"
    assert meta["latency_sec"] == 55.0
    assert meta["cost"] == 0.2


def test_the_real_call_outcome_shape_yields_latency_and_usage():
    """DIVERGENCE GUARD (cycle-2 finding): the REAL `run_agent.CallOutcome` has no
    `latency_sec` / `usage` / `cost` attributes -- it carries `latency_total` and
    `usage_total`. Without an alias the adapter records nothing on the real path and
    gate 8 is vacuous. This pins the alias against the real dataclass."""
    from agent.run_agent import CallOutcome
    import agent.trader.analyzer as an
    oc = CallOutcome(block={"bias": "UP", "dol": {"price": 1.0}}, reasoning=None,
                     retries=2, fallback=False, verdict="clean", attempts=[],
                     latency_total=41.5, usage_total={"input_tokens": 94000})
    meta = an._meta_from_outcome(oc)
    assert meta["latency_sec"] == 41.5
    assert meta["usage"]["input_tokens"] == 94000
    assert meta["retries"] == 2
    assert meta["verdict"] == "clean"
