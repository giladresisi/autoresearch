import pandas as pd

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import DISTANCE_INVALIDATION_PTS, Executor

TZ = "America/New_York"


def _gap(lo, hi, excursion, direction="bull"):
    return Fact(id=f"g{lo}-{excursion}", cls=FactClass.FVG, ticker="MNQ", label="g",
                name=None, reference_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ),
                price=None, price_low=lo, price_high=hi, timeframe="5min",
                resolution="5min", state=FactState.LIVE,
                state_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ), provenance={},
                extra={"direction": direction, "max_anti_excursion": excursion})


def _ex(tmp_path):
    return Executor(str(tmp_path), {"plan_id": "t", "direction": "UP",
                                    "dol": {"price": 30001.5},
                                    "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-13 09:20", tz=TZ))


def test_the_threshold_is_the_spec_value():
    assert DISTANCE_INVALIDATION_PTS == 60.0


def test_a_gap_never_escaped_is_alive(tmp_path):
    assert _ex(tmp_path)._distance_dead(_gap(29880.0, 29900.0, 0.0)) is False


def test_a_gap_escaped_by_less_than_the_threshold_is_alive(tmp_path):
    """§2 pins this: 07-23's ladder rally peaked 56.25 pts beyond its bound gap and the
    +304 winner had to survive."""
    assert _ex(tmp_path)._distance_dead(_gap(29880.0, 29900.0, 56.25)) is False


def test_a_gap_escaped_by_more_than_the_threshold_is_dead(tmp_path):
    """The 07-21 / 08-05 judas excursions ran 68-90+ pts."""
    assert _ex(tmp_path)._distance_dead(_gap(29880.0, 29900.0, 68.0)) is True


def test_the_kill_is_permanent_and_does_not_revive_when_price_returns(tmp_path):
    """The whole point: unlike the max-distance guard, this never revives."""
    ex = _ex(tmp_path)
    gap = _gap(29880.0, 29900.0, 90.0)
    ex._store.upsert(gap)
    now = pd.Timestamp("2026-08-13 09:40", tz=TZ)
    assert gap.id not in {g.id for g in ex._eligible_gaps(now)}
    ex._state["now_price"] = 29890.0          # price back INSIDE the gap
    assert gap.id not in {g.id for g in ex._eligible_gaps(now)}


def test_a_gap_exactly_at_the_threshold_is_alive(tmp_path):
    """'more than ~60' — 60 itself does not kill."""
    assert _ex(tmp_path)._distance_dead(_gap(29880.0, 29900.0, 60.0)) is False


def test_a_gap_with_no_excursion_recorded_is_treated_as_alive(tmp_path):
    g = _gap(29880.0, 29900.0, 0.0)
    g.extra.pop("max_anti_excursion")
    assert _ex(tmp_path)._distance_dead(g) is False


def test_eligible_gaps_excludes_distance_dead_gaps(tmp_path):
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29880.0, 29900.0, 10.0))
    ex._store.upsert(_gap(29850.0, 29870.0, 90.0))
    ids = {g.id for g in ex._eligible_gaps(pd.Timestamp("2026-08-13 09:40", tz=TZ))}
    assert ids == {"g29880.0-10.0"}
