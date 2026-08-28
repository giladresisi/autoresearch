import pandas as pd

from agent.facts.detectors.extremes import (extreme_age_at, post_0930_since,
                                            running_extremes)

TZ = "America/New_York"


def _frame():
    idx = pd.date_range("2026-08-25 18:00", periods=1000, freq="1min", tz=TZ)
    highs = [29000.0] * len(idx)
    lows = [28900.0] * len(idx)
    highs[100] = 29500.0            # 19:40 — the 24h extreme, set overnight
    lows[900] = 28500.0
    return pd.DataFrame({"Open": 29000.0, "High": highs, "Low": lows,
                         "Close": 29000.0}, index=idx)


def test_running_extremes_tags_its_track():
    facts = running_extremes(_frame(), pd.Timestamp("2026-08-25 18:00", tz=TZ), "MNQ",
                             track="24h")
    assert facts and all(f.extra["track"] == "24h" for f in facts)


def test_post_0930_since_is_0930_of_the_session_trade_date():
    now = pd.Timestamp("2026-08-26 10:15", tz=TZ)
    assert post_0930_since(now) == pd.Timestamp("2026-08-26 09:30", tz=TZ)


def test_post_0930_since_before_0930_returns_that_same_days_0930():
    now = pd.Timestamp("2026-08-26 09:21", tz=TZ)
    assert post_0930_since(now) == pd.Timestamp("2026-08-26 09:30", tz=TZ)


def test_post_0930_since_during_the_overnight_session_uses_the_NEXT_0930():
    """A bar at 20:00 on 08-25 belongs to the 08-26 trade date; its RTH open is 08-26."""
    now = pd.Timestamp("2026-08-25 20:00", tz=TZ)
    assert post_0930_since(now) == pd.Timestamp("2026-08-26 09:30", tz=TZ)


def test_the_two_tracks_disagree_when_the_extreme_is_overnight():
    df = _frame()
    day = running_extremes(df, pd.Timestamp("2026-08-25 18:00", tz=TZ), "MNQ",
                           track="24h")
    rth = running_extremes(df, pd.Timestamp("2026-08-26 09:30", tz=TZ), "MNQ",
                           track="post_0930")
    day_hi = [f for f in day if f.extra["kind"] == "day_high"][0]
    rth_hi = [f for f in rth if f.extra["kind"] == "day_high"][0]
    assert day_hi.price == 29500.0
    assert rth_hi.price < day_hi.price


def test_the_two_tracks_have_distinct_ids():
    df = _frame()
    a = running_extremes(df, pd.Timestamp("2026-08-25 18:00", tz=TZ), "MNQ", track="24h")
    b = running_extremes(df, pd.Timestamp("2026-08-26 09:30", tz=TZ), "MNQ",
                         track="post_0930")
    assert not ({f.id for f in a} & {f.id for f in b})


def test_extreme_age_at_measures_from_the_extremes_own_timestamp():
    df = _frame()
    facts = running_extremes(df, pd.Timestamp("2026-08-25 18:00", tz=TZ), "MNQ")
    hi = [f for f in facts if f.extra["kind"] == "day_high"][0]
    age = extreme_age_at(hi, pd.Timestamp("2026-08-26 09:20", tz=TZ))
    assert age == pd.Timestamp("2026-08-26 09:20", tz=TZ) - hi.reference_ts


def test_extreme_age_at_is_none_without_a_timestamp():
    assert extreme_age_at(None, pd.Timestamp("2026-08-26 09:20", tz=TZ)) is None


def test_both_drivers_emit_both_tracks():
    """The seam invariant covers the new track too."""
    from agent.facts.batch import run_batch
    from agent.facts.incremental import run_incremental
    from agent.facts.records import FactClass
    from agent.facts.requirements import EXECUTOR_REQUIREMENT
    from agent.facts.store import FactStore

    df = _frame()
    now = pd.Timestamp("2026-08-26 10:00", tz=TZ)
    b = FactStore()
    run_batch(b, {"MNQ": df}, EXECUTOR_REQUIREMENT, now)
    tracks = {f.extra.get("track") for f in b.query(cls=FactClass.EXTREME, ticker="MNQ")}
    assert tracks == {"24h", "post_0930"}

    i = FactStore()
    run_incremental(i, {}, {"MNQ": df}, now, True)
    itracks = {f.extra.get("track") for f in i.query(cls=FactClass.EXTREME, ticker="MNQ")}
    assert itracks == {"24h", "post_0930"}
