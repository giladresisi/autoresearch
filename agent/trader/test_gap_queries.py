"""Task 2: timeframe-parameterised gap queries.

`_eligible_gaps` hard-filtered `f.timeframe != PRIMARY_TF`, so §4's widened 1m fallback,
§6 (entirely 1m) and §8's takeover ("any timeframe, 1m included") were all
unimplementable. §11 records two independent manual walks that scanned only 5m and
produced a wrong 07-21 ledger and a mis-bound 07-31 takeover.
"""
import pandas as pd
import pytest

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import Executor

TZ = "America/New_York"
NOW = pd.Timestamp("2026-08-21 09:41", tz=TZ)


def _gap(tf="5min", direction="bear", ref="2026-08-21 09:30",
         exists="2026-08-21 09:40", lo=29472.25, hi=29485.0, suffix=""):
    return Fact(id=f"g-{tf}-{direction}-{ref}{suffix}", cls=FactClass.FVG, ticker="MNQ",
                label="g", name=None,
                reference_ts=pd.Timestamp(ref, tz=TZ), price=None,
                price_low=lo, price_high=hi,
                timeframe=tf, resolution=tf, state=FactState.LIVE,
                state_ts=pd.Timestamp(ref, tz=TZ), provenance={},
                extra={"direction": direction, "max_anti_excursion": 0.0,
                       "exists_from": pd.Timestamp(exists, tz=TZ)})


@pytest.fixture
def ex(tmp_path):
    return Executor(str(tmp_path),
                    {"plan_id": "t", "direction": "DOWN",
                     "dol": {"price": 29000.0},
                     "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-21 09:20", tz=TZ))


def test_gaps_on_5m_returns_only_5m(ex):
    ex._store.upsert(_gap(tf="5min"))
    ex._store.upsert(_gap(tf="1min"))
    got = ex._gaps_on("5min", NOW)
    assert {g.timeframe for g in got} == {"5min"}


def test_gaps_on_1m_returns_only_1m(ex):
    ex._store.upsert(_gap(tf="5min"))
    ex._store.upsert(_gap(tf="1min"))
    got = ex._gaps_on("1min", NOW)
    assert {g.timeframe for g in got} == {"1min"}


def test_direction_defaults_to_the_trade_direction(ex):
    """A SHORT plan trades bear gaps."""
    ex._store.upsert(_gap(direction="bear"))
    ex._store.upsert(_gap(direction="bull"))
    got = ex._gaps_on("5min", NOW)
    assert {g.extra["direction"] for g in got} == {"bear"}


def test_direction_can_be_overridden_for_the_counter_thesis(ex):
    """§4 binds a COUNTER-thesis gap: a bullish FVG from the uptrend when expecting
    down. The override is what makes that expressible."""
    ex._store.upsert(_gap(direction="bull"))
    got = ex._gaps_on("5min", NOW, direction="bull")
    assert len(got) == 1


def test_eligible_gaps_is_the_5m_query(ex):
    """Back-compat: the existing call site must be unchanged in behaviour."""
    ex._store.upsert(_gap(tf="5min"))
    assert [g.id for g in ex._eligible_gaps(NOW)] == \
           [g.id for g in ex._gaps_on("5min", NOW)]


def test_a_1m_query_still_honours_existence_and_the_blacklist(ex):
    """The timeframe parameter must not bypass Task 1's gate or the §8 blacklist."""
    g = _gap(tf="1min", exists="2026-08-21 09:40")
    ex._store.upsert(g)
    assert ex._gaps_on("1min", pd.Timestamp("2026-08-21 09:35", tz=TZ)) == []
    ex._plan["blacklist"] = [g.id]
    assert ex._gaps_on("1min", pd.Timestamp("2026-08-21 09:41", tz=TZ)) == []


def test_the_1m_query_still_honours_the_height_band(ex):
    ex._store.upsert(_gap(tf="1min", lo=29472.25, hi=29474.0))      # 1.75 pts
    assert ex._gaps_on("1min", NOW) == []
    assert len(ex._gaps_on("1min", NOW, initial=False)) == 1


def test_the_5m_distance_invalidation_does_not_extend_to_1m_gaps(ex):
    """§4: 'the §2 5m distance invalidation does NOT extend to 1m-bound gaps
    (close-through eligibility only, for now).'"""
    five = _gap(tf="5min")
    one = _gap(tf="1min")
    for g in (five, one):
        g.extra["max_anti_excursion"] = 90.0
        ex._store.upsert(g)
    assert ex._gaps_on("5min", NOW) == []
    assert len(ex._gaps_on("1min", NOW)) == 1
