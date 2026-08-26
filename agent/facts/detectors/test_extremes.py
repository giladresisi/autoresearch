import pandas as pd
from agent.facts.detectors.extremes import running_extremes


def _bars(highs, lows, start="2026-08-13 18:00"):
    idx = pd.date_range(start, periods=len(highs), freq="1min", tz="America/New_York")
    return pd.DataFrame({"Open": lows, "High": highs, "Low": lows,
                         "Close": lows, "Volume": 1.0}, index=idx)


def test_day_extreme_uses_the_24h_session_from_prior_1800():
    """l2 §7: 'day' means the 24h session opening at the prior 18:00 ET, not RTH."""
    df = _bars([100, 120, 110], [90, 95, 80])
    since = pd.Timestamp("2026-08-13 18:00", tz="America/New_York")
    facts = running_extremes(df, since, "MNQ")
    hi = next(f for f in facts if f.extra["kind"] == "day_high")
    lo = next(f for f in facts if f.extra["kind"] == "day_low")
    assert hi.price == 120 and lo.price == 80


def test_post_arm_extreme_ignores_bars_before_the_arm():
    df = _bars([200, 100, 110], [190, 90, 95])
    since = df.index[1]
    facts = running_extremes(df, since, "MNQ")
    hi = next(f for f in facts if f.extra["kind"] == "day_high")
    assert hi.price == 110, "the pre-arm 200 must be excluded"


def test_running_extremes_empty_frame_returns_empty():
    assert running_extremes(pd.DataFrame(),
                            pd.Timestamp("2026-08-13 18:00", tz="America/New_York"), "MNQ") == []


# --- added during implementation (not in the plan) --------------------------- #

def test_extreme_id_changes_with_the_arm_time():
    """The post-arm extreme is a DIFFERENT fact from the day extreme, not a mutation."""
    df = _bars([100, 120, 110], [90, 95, 80])
    a = running_extremes(df, df.index[0], "MNQ")
    b = running_extremes(df, df.index[1], "MNQ")
    assert {f.id for f in a}.isdisjoint({f.id for f in b})
