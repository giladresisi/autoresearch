import pandas as pd
import pytest
from agent.trader.planner import derive_plan, MECHANISM_CLASSES
from agent.trader.plan_store import PlanStore

NOW = pd.Timestamp("2026-08-13 09:20", tz="America/New_York")
THESIS = {"thesis_id": "t1", "bias": "DOWN", "confidence": "MEDIUM",
          "dol": {"level": "prev1_week_low", "price": 29533.5},
          "falsified_if": []}


def _leg(direction, extreme_ts, rng=120.0):
    from agent.facts.records import Fact, FactClass, FactState
    return Fact(id="leg1", cls=FactClass.LEG, ticker="MNQ", label="", name=None,
                reference_ts=extreme_ts, price=None, price_low=None, price_high=None,
                timeframe="5min", resolution="1min", state=FactState.LIVE,
                state_ts=extreme_ts, provenance={},
                extra={"direction": direction, "range": rng})


def test_thesis_opposing_last_trend_arms_the_reversal_class():
    plan = derive_plan(THESIS, [_leg("up", NOW - pd.Timedelta(minutes=20))], NOW)
    assert "fvg_negation_reversal" in plan["armed_classes"]


def test_thesis_agreeing_with_last_trend_arms_the_continuation_class():
    plan = derive_plan(THESIS, [_leg("down", NOW - pd.Timedelta(minutes=20))], NOW)
    assert "fvg_return_continuation" in plan["armed_classes"]


def test_self_gating_classes_are_always_armed():
    """l2 §6/§7 verify their own preconditions continuously."""
    plan = derive_plan(THESIS, [_leg("up", NOW - pd.Timedelta(minutes=20))], NOW)
    assert "fvg_1m_post_extreme" in plan["armed_classes"]
    assert "extreme_reject_close" in plan["armed_classes"]


def test_stale_leg_beyond_60min_does_not_count_as_last_trend():
    """l2 §3: last trend = most recent qualifying leg whose extreme formed <= 60 min ago."""
    plan = derive_plan(THESIS, [_leg("up", NOW - pd.Timedelta(minutes=90))], NOW)
    assert "fvg_negation_reversal" not in plan["armed_classes"]


def test_plan_carries_dol_and_valid_while_from_the_thesis():
    plan = derive_plan(THESIS, [], NOW)
    assert plan["dol"]["price"] == 29533.5
    assert "valid_while" in plan


def test_plan_starts_with_zero_attempts_empty_blacklist_no_cooldown():
    plan = derive_plan(THESIS, [], NOW)
    assert plan["attempts_used"] == 0 and plan["blacklist"] == [] and plan["cooldown_until"] is None


def test_planner_never_vetoes_on_a_near_dol():
    """Step-8b is deliberately NOT carried — see Global Constraints."""
    near = dict(THESIS, dol={"level": "x", "price": 29000.0})
    plan = derive_plan(near, [], NOW)
    assert plan is not None and plan["direction"] == "DOWN"


def test_plan_store_is_a_keyed_collection_not_a_singleton(tmp_path):
    s = PlanStore(tmp_path)
    s.put({"plan_id": "p1", "direction": "DOWN"})
    s.put({"plan_id": "p2", "direction": "UP"})
    assert len(s.all()) == 2


def test_plan_store_persists_across_instances(tmp_path):
    PlanStore(tmp_path).put({"plan_id": "p1", "direction": "DOWN"})
    assert PlanStore(tmp_path).get("p1") is not None


def test_plan_store_writes_no_legacy_file(tmp_path):
    PlanStore(tmp_path).put({"plan_id": "p1", "direction": "DOWN"})
    for forbidden in ("events.jsonl", "position.json", "hypothesis.json", "daily.json"):
        assert not (tmp_path / forbidden).exists()


# --- added during implementation (not in the plan) --------------------------- #

def test_only_one_fvg_class_is_ever_armed_at_a_time():
    """Reversal and continuation are mutually exclusive readings of the same leg."""
    for d in ("up", "down"):
        armed = derive_plan(THESIS, [_leg(d, NOW - pd.Timedelta(minutes=20))],
                            NOW)["armed_classes"]
        assert not ({"fvg_negation_reversal", "fvg_return_continuation"} <= set(armed))


def test_every_armed_class_is_a_known_mechanism():
    plan = derive_plan(THESIS, [_leg("up", NOW - pd.Timedelta(minutes=20))], NOW)
    assert set(plan["armed_classes"]) <= set(MECHANISM_CLASSES)


def test_no_leg_at_all_arms_only_the_self_gating_classes():
    plan = derive_plan(THESIS, [], NOW)
    assert set(plan["armed_classes"]) == {"fvg_1m_post_extreme", "extreme_reject_close"}


def test_larger_range_leg_wins_when_two_are_fresh():
    small = _leg("up", NOW - pd.Timedelta(minutes=10), rng=60.0)
    big = _leg("down", NOW - pd.Timedelta(minutes=30), rng=250.0)
    big.id = "leg2"
    plan = derive_plan(THESIS, [small, big], NOW)
    assert plan["last_trend"]["direction"] == "down"
    assert "fvg_return_continuation" in plan["armed_classes"]


def test_valid_while_carries_falsifiers_only_and_drops_exhaustion():
    """Exhaustion is DROPPED: reaching the DOL is the exhaustion. Every recorded thesis
    sets `exhausted_if` to exactly the DOL price — 08-25's rationale says so outright,
    "london(cur)_low, the DOL itself". Carrying it as a second predicate only duplicated
    the DOL touch. Removed from L1's SCHEMA too (2026-08-29), so a thesis carrying it is
    now rejected outright; this pins that the Planner ignores it even if one appears."""
    f = {"type": "price_beyond", "price": 29420.0, "side": "above"}
    x = {"type": "price_beyond", "price": 29157.5, "side": "below"}
    plan = derive_plan({"bias": "DOWN", "dol": {"price": 29157.5},
                        "falsified_if": [f], "exhausted_if": [x]}, [], NOW)
    assert plan["valid_while"] == [f]
    assert x not in plan["valid_while"], "exhaustion must not reach the plan"

def test_plan_id_is_stable_for_the_same_thesis_and_time():
    a = derive_plan(THESIS, [], NOW)["plan_id"]
    b = derive_plan(THESIS, [], NOW)["plan_id"]
    assert a == b and len(a) == 12


def test_neutral_thesis_arms_no_directional_class():
    plan = derive_plan(dict(THESIS, bias="NEUTRAL"),
                       [_leg("up", NOW - pd.Timedelta(minutes=20))], NOW)
    assert "fvg_negation_reversal" not in plan["armed_classes"]
    assert "fvg_return_continuation" not in plan["armed_classes"]
