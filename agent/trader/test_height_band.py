import pandas as pd

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import (MAX_FVG_HEIGHT_PTS, MIN_FVG_HEIGHT_PTS, Executor)

TZ = "America/New_York"


def _gap(lo, hi, direction="bull"):
    return Fact(id=f"g{lo}-{hi}", cls=FactClass.FVG, ticker="MNQ", label="g", name=None,
                reference_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ), price=None,
                price_low=lo, price_high=hi, timeframe="5min", resolution="5min",
                state=FactState.LIVE, state_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ),
                provenance={}, extra={"direction": direction,
                                      "height": round(hi - lo, 4)})


def _ex(tmp_path):
    return Executor(str(tmp_path), {"plan_id": "t", "direction": "UP",
                                    "dol": {"price": 30001.5},
                                    "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-13 09:20", tz=TZ))


def test_the_starting_values_are_the_spec_values():
    assert MIN_FVG_HEIGHT_PTS == 5.0
    assert MAX_FVG_HEIGHT_PTS == 45.0


def test_a_gap_inside_the_band_is_an_allowed_initial_binding(tmp_path):
    assert _ex(tmp_path)._height_ok(_gap(29881.5, 29905.25), initial=True) is True


def test_a_gap_below_the_minimum_is_refused_as_an_initial_binding(tmp_path):
    """08-13's 08:55 gap: 1.75 pts."""
    assert _ex(tmp_path)._height_ok(_gap(29877.0, 29878.75), initial=True) is False


def test_a_second_gap_below_the_minimum_is_refused_as_an_initial_binding(tmp_path):
    """08-13's 08:50 gap: 4.00 pts."""
    assert _ex(tmp_path)._height_ok(_gap(29864.75, 29868.75), initial=True) is False


def test_a_gap_below_the_minimum_is_ALLOWED_as_a_ladder_target(tmp_path):
    """§2: the minimum applies to initial binding only; ladder / deepest-penetration
    targets are exempt — 07-15's 2.75-pt gap was that day's true rejection level."""
    assert _ex(tmp_path)._height_ok(_gap(29877.0, 29878.75), initial=False) is True


def test_a_gap_above_the_maximum_is_refused_in_BOTH_roles(tmp_path):
    """The max is a character filter with no stated exemption."""
    tall = _gap(29800.0, 29850.0)
    assert _ex(tmp_path)._height_ok(tall, initial=True) is False
    assert _ex(tmp_path)._height_ok(tall, initial=False) is False


def test_a_gap_exactly_at_the_minimum_is_allowed(tmp_path):
    assert _ex(tmp_path)._height_ok(_gap(29900.0, 29905.0), initial=True) is True


def test_a_gap_exactly_at_the_maximum_is_allowed(tmp_path):
    assert _ex(tmp_path)._height_ok(_gap(29800.0, 29845.0), initial=True) is True


def test_eligible_gaps_applies_the_initial_band(tmp_path):
    ex = _ex(tmp_path)
    for g in (_gap(29881.5, 29905.25), _gap(29877.0, 29878.75)):
        ex._store.upsert(g)
    ids = {g.id for g in ex._eligible_gaps(pd.Timestamp("2026-08-13 09:31", tz=TZ))}
    assert ids == {"g29881.5-29905.25"}
