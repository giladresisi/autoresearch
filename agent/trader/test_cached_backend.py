# agent/trader/test_cached_backend.py
import pytest

from agent.trader.cached_backend import CachedThesisBackend, NetworkCallRefused
from agent.trader.thesis_cache import ThesisCache

THESIS = {"bias": "DOWN", "dol": {"level": "london(cur)_low", "price": 29157.5}}


def _backend(tmp_path, inner, *, allow_calls=True, model_id="m1", sysp="KB", task="TASK"):
    return CachedThesisBackend(
        inner,
        cache=ThesisCache(root=tmp_path),
        model_id=model_id,
        system_prompt_fn=lambda: sysp,
        task_prompt=task,
        schema_fn=lambda facts: {"type": "object"},
        allow_calls=allow_calls,
    )


def _counting_inner(result=(THESIS, {"latency_sec": 40.0})):
    state = {"n": 0}

    def _inner(facts_text, context_text, facts, *, evidence_magnitude=None):
        state["n"] += 1
        return result
    _inner.state = state
    return _inner


def test_a_miss_calls_the_inner_backend_and_records_the_result(tmp_path):
    inner = _counting_inner()
    b = _backend(tmp_path, inner)
    thesis, meta = b("facts", "", {"boundary": "2026-08-12 09:20:00-04:00"})
    assert thesis == THESIS
    assert inner.state["n"] == 1
    assert b.misses == 1 and b.hits == 0


def test_the_second_identical_call_is_a_hit_and_does_not_call_the_inner_backend(tmp_path):
    inner = _counting_inner()
    b = _backend(tmp_path, inner)
    args = ("facts", "", {"boundary": "2026-08-12 09:20:00-04:00"})
    b(*args)
    thesis, meta = b(*args)
    assert thesis == THESIS
    assert inner.state["n"] == 1, "a warm key must not reach the model"
    assert b.hits == 1


def test_a_hit_reports_served_from_cache_in_its_meta(tmp_path):
    b = _backend(tmp_path, _counting_inner())
    args = ("facts", "", {})
    b(*args)
    _, meta = b(*args)
    assert meta.get("cached") is True


def test_changing_the_facts_text_forces_a_miss(tmp_path):
    inner = _counting_inner()
    b = _backend(tmp_path, inner)
    b("facts A", "", {})
    b("facts B", "", {})
    assert inner.state["n"] == 2


def test_changing_the_kb_forces_a_miss(tmp_path):
    """The KB is the system prompt - a doc edit is a strategy change and must re-record."""
    inner = _counting_inner()
    c = ThesisCache(root=tmp_path)
    b1 = CachedThesisBackend(inner, cache=c, model_id="m1",
                             system_prompt_fn=lambda: "KB v1", task_prompt="T",
                             schema_fn=lambda f: {})
    b2 = CachedThesisBackend(inner, cache=c, model_id="m1",
                             system_prompt_fn=lambda: "KB v2", task_prompt="T",
                             schema_fn=lambda f: {})
    b1("facts", "", {})
    b2("facts", "", {})
    assert inner.state["n"] == 2


def test_changing_the_model_forces_a_miss(tmp_path):
    inner = _counting_inner()
    c = ThesisCache(root=tmp_path)
    b1 = CachedThesisBackend(inner, cache=c, model_id="haiku",
                             system_prompt_fn=lambda: "KB", task_prompt="T",
                             schema_fn=lambda f: {})
    b2 = CachedThesisBackend(inner, cache=c, model_id="sonnet",
                             system_prompt_fn=lambda: "KB", task_prompt="T",
                             schema_fn=lambda f: {})
    b1("facts", "", {})
    b2("facts", "", {})
    assert inner.state["n"] == 2


def test_an_unkeyed_change_does_not_force_a_miss(tmp_path):
    """`evidence_magnitude` is not part of the prompt bytes, so it must not re-key."""
    inner = _counting_inner()
    b = _backend(tmp_path, inner)
    b("facts", "", {}, evidence_magnitude={"x": 1})
    b("facts", "", {}, evidence_magnitude={"x": 2})
    assert inner.state["n"] == 1


def test_allow_calls_false_raises_on_a_miss_rather_than_calling(tmp_path):
    """Gate 3: a warm-cache replay must make no model call. A miss is a loud failure."""
    inner = _counting_inner()
    b = _backend(tmp_path, inner, allow_calls=False)
    with pytest.raises(NetworkCallRefused):
        b("facts", "", {})
    assert inner.state["n"] == 0


def test_allow_calls_false_still_serves_a_hit(tmp_path):
    inner = _counting_inner()
    warm = _backend(tmp_path, inner)
    warm("facts", "", {})
    cold = _backend(tmp_path, inner, allow_calls=False)
    thesis, _ = cold("facts", "", {})
    assert thesis == THESIS
    assert inner.state["n"] == 1


def test_a_none_thesis_from_the_inner_backend_is_not_recorded(tmp_path):
    """A failed call must not poison the cache with a permanent dark day."""
    inner = _counting_inner(result=(None, {}))
    b = _backend(tmp_path, inner)
    b("facts", "", {})
    b("facts", "", {})
    assert inner.state["n"] == 2, "a failure must be retried, not cached"


def test_an_inner_backend_returning_a_bare_dict_is_accepted(tmp_path):
    def _inner(ft, ct, f, *, evidence_magnitude=None):
        return THESIS
    b = _backend(tmp_path, _inner)
    thesis, meta = b("facts", "", {})
    assert thesis == THESIS


def test_a_failsafe_verdict_is_not_recorded(tmp_path):
    """CYCLE-2 FINDING. `decide_thesis` never returns None on failure: past MAX_RETRIES
    it substitutes a synthetic NEUTRAL block and stamps verdict='failsafe'. The plan's
    None-guard therefore never fires on the real path, and the 2026-08-12 seeding run
    cached a failsafe as a permanent dark day. A failsafe is a call failure."""
    inner = _counting_inner(result=({"bias": "NEUTRAL", "dol": None},
                                    {"verdict": "failsafe", "retries": 2}))
    b = _backend(tmp_path, inner)
    b("facts", "", {})
    b("facts", "", {})
    assert inner.state["n"] == 2, "a failsafe must be retried, not frozen into the cache"


def test_a_clean_verdict_is_recorded(tmp_path):
    inner = _counting_inner(result=(THESIS, {"verdict": "clean"}))
    b = _backend(tmp_path, inner)
    b("facts", "", {})
    b("facts", "", {})
    assert inner.state["n"] == 1


def test_a_refusal_is_counted_as_well_as_raised(tmp_path):
    """`Analyzer._call` swallows every exception, so the raise alone is invisible to the
    replay runner. The counter is what makes gate 3 falsifiable."""
    b = _backend(tmp_path, _counting_inner(), allow_calls=False)
    with pytest.raises(NetworkCallRefused):
        b("facts", "", {})
    assert b.refusals == 1


def test_the_boundary_hint_reaches_the_record_when_facts_carry_none(tmp_path):
    """CYCLE-2 FINDING. `assemble_facts` returns exactly DECIDE_THESIS_KEYS and
    `boundary` is NOT one of them, so on the real path `facts["boundary"]` is always
    absent and every recording stored `boundary: null`."""
    import json
    c = ThesisCache(root=tmp_path)
    b = CachedThesisBackend(_counting_inner(), cache=c, model_id="m",
                            system_prompt_fn=lambda: "KB", task_prompt="T",
                            schema_fn=lambda f: {}, boundary_hint="2026-08-12")
    b("facts", "", {})
    blob = json.loads(c.path_for(b.last_key).read_text(encoding="utf-8"))
    assert blob["boundary"] == "2026-08-12"


def test_facts_boundary_still_wins_over_the_hint(tmp_path):
    c = ThesisCache(root=tmp_path)
    with_hint = CachedThesisBackend(_counting_inner(), cache=c, model_id="m",
                                    system_prompt_fn=lambda: "KB", task_prompt="T",
                                    schema_fn=lambda f: {}, boundary_hint="HINT")
    with_hint("facts", "", {"boundary": "REAL"})
    plain = CachedThesisBackend(_counting_inner(), cache=c, model_id="m",
                                system_prompt_fn=lambda: "KB", task_prompt="T",
                                schema_fn=lambda f: {})
    plain("facts", "", {"boundary": "REAL"})
    assert plain.last_key == with_hint.last_key


def test_two_dates_with_identical_facts_do_not_collide(tmp_path):
    """The hint is part of the KEY too, so the boundary separation the design claims
    holds even in the pathological case of byte-identical facts text."""
    c = ThesisCache(root=tmp_path)
    kw = dict(cache=c, model_id="m", system_prompt_fn=lambda: "KB", task_prompt="T",
              schema_fn=lambda f: {})
    a = CachedThesisBackend(_counting_inner(), boundary_hint="2026-08-12", **kw)
    b = CachedThesisBackend(_counting_inner(), boundary_hint="2026-08-13", **kw)
    a("facts", "", {})
    b("facts", "", {})
    assert a.last_key != b.last_key


def test_a_hit_replays_the_recorded_latency_and_usage(tmp_path):
    """CODE-REVIEW FINDING. `ThesisCache.get` returned only the thesis, so every warm
    replay reported `call_meta = {}` -- the "recorded on BOTH paths" claim held only for
    the seeding run, and gate 8 passed solely because seeding directories were on disk."""
    inner = _counting_inner(result=(THESIS, {"latency_sec": 41.5, "verdict": "clean",
                                             "usage": {"input_tokens": 94000}}))
    b = _backend(tmp_path, inner)
    b("facts", "", {})
    _, meta = b("facts", "", {})
    assert meta["cached"] is True
    assert meta["latency_sec"] == 41.5
    assert meta["usage"]["input_tokens"] == 94000
    assert meta["verdict"] == "clean"


def test_a_hit_reaching_the_analyzer_keeps_the_recorded_provenance(tmp_path,
                                                                   monkeypatch):
    """End-to-end: a warm hit must land in `Analyzer.call_meta()`, not an empty dict."""
    import pandas as pd
    import agent.trader.analyzer as an
    monkeypatch.setattr(an, "NEAR_MATURITY_WAIT", False)
    monkeypatch.setattr(an, "assemble_facts",
                        lambda store, bars, now: ("facts", "", {"levels": {"a": 1}}, {}))
    b = _backend(tmp_path, _counting_inner(
        result=(THESIS, {"latency_sec": 41.5, "verdict": "clean"})))
    b("facts", "", {})                                   # seed
    a = an.Analyzer(tmp_path / "state", b, threaded=False)
    a.maybe_run(pd.Timestamp("2026-08-12 09:20", tz="America/New_York"),
                {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()})
    assert a.call_meta()["latency_sec"] == 41.5


def test_last_key_is_exposed_for_debugging(tmp_path):
    b = _backend(tmp_path, _counting_inner())
    b("facts", "", {})
    assert isinstance(b.last_key, str) and len(b.last_key) == 16
