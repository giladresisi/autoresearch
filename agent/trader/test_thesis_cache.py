# agent/trader/test_thesis_cache.py
import json
import pytest

from agent.trader.thesis_cache import ThesisCache, thesis_key, cache_root

_BASE = dict(
    boundary="2026-08-12 09:20:00-04:00",
    facts_text="S0 ... S9",
    context_text="",
    system_prompt="KB DOC BYTES",
    task_prompt="TASK",
    schema={"type": "object"},
    model_id="anthropic/claude-haiku-4.5",
)


def test_the_same_inputs_produce_the_same_key():
    assert thesis_key(**_BASE) == thesis_key(**_BASE)


def test_the_key_is_short_and_hex():
    k = thesis_key(**_BASE)
    assert len(k) == 16
    int(k, 16)


@pytest.mark.parametrize("field,changed", [
    ("boundary", "2026-08-12 09:21:00-04:00"),
    ("facts_text", "S0 ... S9 DIFFERENT"),
    ("context_text", "something"),
    ("system_prompt", "KB DOC BYTES v2"),
    ("task_prompt", "TASK v2"),
    ("model_id", "anthropic/claude-sonnet-5"),
])
def test_changing_any_keyed_input_changes_the_key(field, changed):
    other = dict(_BASE)
    other[field] = changed
    assert thesis_key(**other) != thesis_key(**_BASE)


def test_changing_the_schema_changes_the_key():
    other = dict(_BASE)
    other["schema"] = {"type": "object", "required": ["bias"]}
    assert thesis_key(**other) != thesis_key(**_BASE)


def test_schema_key_order_does_not_change_the_key():
    """The schema is a dict; serialisation order must not create phantom misses."""
    a = dict(_BASE, schema={"a": 1, "b": 2})
    b = dict(_BASE, schema={"b": 2, "a": 1})
    assert thesis_key(**a) == thesis_key(**b)


def test_put_then_get_round_trips_the_thesis(tmp_path):
    c = ThesisCache(root=tmp_path)
    thesis = {"bias": "DOWN", "dol": {"level": "london(cur)_low", "price": 29157.5}}
    c.put("abc123", thesis, meta={"latency_sec": 40.0}, boundary=_BASE["boundary"])
    assert c.get("abc123") == thesis


def test_get_on_a_missing_key_returns_none(tmp_path):
    assert ThesisCache(root=tmp_path).get("nope") is None


def test_the_record_carries_meta_and_boundary_for_debugging(tmp_path):
    c = ThesisCache(root=tmp_path)
    c.put("k1", {"bias": "UP"}, meta={"latency_sec": 9.0, "cost": 0.1},
          boundary=_BASE["boundary"])
    blob = json.loads(c.path_for("k1").read_text(encoding="utf-8"))
    assert blob["meta"]["cost"] == 0.1
    assert blob["boundary"] == _BASE["boundary"]
    assert blob["key"] == "k1"


def test_a_corrupt_entry_reads_as_a_miss_not_an_exception(tmp_path):
    """A half-written file must degrade to a miss so the run recovers by re-calling."""
    c = ThesisCache(root=tmp_path)
    c.path_for("bad").write_text("{ not json", encoding="utf-8")
    assert c.get("bad") is None


def test_put_is_atomic_leaving_no_partial_file(tmp_path):
    c = ThesisCache(root=tmp_path)
    c.put("k2", {"bias": "UP"})
    leftovers = [p for p in tmp_path.iterdir() if p.suffix not in (".json",)]
    assert leftovers == []


def test_clear_removes_every_entry_and_reports_the_count(tmp_path):
    c = ThesisCache(root=tmp_path)
    c.put("a", {"bias": "UP"})
    c.put("b", {"bias": "DOWN"})
    assert c.clear() == 2
    assert c.get("a") is None


def test_cache_root_defaults_under_global_not_the_worktree(monkeypatch, tmp_path):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.delenv("ACT_THESIS_CACHE_DIR", raising=False)
    assert cache_root() == tmp_path / "thesis_cache"


def test_cache_root_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "elsewhere"))
    assert cache_root() == tmp_path / "elsewhere"
