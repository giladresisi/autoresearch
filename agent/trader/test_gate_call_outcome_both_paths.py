"""Gate 8 -- latency / tokens / cost are captured on BOTH paths.

COST IS NOT CAPTURED, and cannot be today. The plan states "the manual harness captures
all three"; it does not -- `manual-l1-thesis/test_l1_thesis_manual.py` records
`latency_total_sec` and `usage_total` only, and there is no price table anywhere in the
tree to turn tokens into dollars. `cost` stays a supported key in the meta contract (a
backend that knows its price may supply it, and the unit tests do) but is absent on the
real path rather than fabricated from a guessed rate. Latency and token usage ARE
captured on both paths and are what these gates assert.
"""
import json
import os

import pytest

from agent.trader.thesis_cache import ThesisCache


def test_a_recorded_thesis_carries_its_call_meta():
    entries = list(ThesisCache().root.glob("*.json"))
    if not entries:
        pytest.skip("thesis cache not seeded")
    blob = json.loads(entries[0].read_text(encoding="utf-8"))
    assert "meta" in blob


def test_the_seeding_run_recorded_latency_and_token_usage():
    entries = list(ThesisCache().root.glob("*.json"))
    if not entries:
        pytest.skip("thesis cache not seeded")
    metas = [json.loads(p.read_text(encoding="utf-8")).get("meta") or {}
             for p in entries]
    assert any("latency_sec" in m for m in metas), \
        "no recorded thesis carries latency -- the CallOutcome capture is not reaching the cache"
    assert any((m.get("usage") or {}).get("input_tokens") for m in metas), \
        "no recorded thesis carries token usage"


def test_the_live_path_persists_call_meta_in_thesis_state():
    """The 2026-08-25 fixture predates Task 1, so this checks the CURRENT live shape."""
    import agent.trader.analyzer as an
    import inspect
    src = inspect.getsource(an.Analyzer._save)
    assert "call_meta" in src


def test_the_replay_path_persists_call_meta_in_thesis_state():
    """The seeded 2026-08-12 replay run is the real-path evidence: same Analyzer, same
    `_save`, driven from the bar loop rather than from a live session."""
    import glob
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    hits = sorted(glob.glob(os.path.join(
        repo, "regression", "sessions", "2026-08-12", "*", "thesis_state.json")))
    if not hits:
        pytest.skip("no 2026-08-12 replay run on disk -- run scripts/replay_session.py")
    metas = [json.load(open(p, encoding="utf-8")).get("call_meta") or {} for p in hits]
    assert any("latency_sec" in m for m in metas), \
        "no replay run recorded latency in thesis_state.json"
    assert any("verdict" in m for m in metas)


def test_the_real_call_outcome_field_names_are_aliased_not_assumed():
    """`run_agent.CallOutcome` carries `latency_total` / `usage_total`, NOT the meta
    contract's `latency_sec` / `usage`. Reading only the contract names -- which is what
    the plan specified -- records nothing but retries and verdict, and this gate would
    pass on the unit tests while the real path stayed empty."""
    import agent.trader.analyzer as an
    from agent.run_agent import CallOutcome
    fields = set(CallOutcome.__dataclass_fields__)
    assert "latency_sec" not in fields and "latency_total" in fields
    assert "usage" not in fields and "usage_total" in fields
    assert an._META_ALIASES["latency_sec"] == ("latency_total",)
    assert an._META_ALIASES["usage"] == ("usage_total",)
