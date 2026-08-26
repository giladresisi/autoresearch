import pandas as pd
from agent.facts.detectors.legs import segment_legs


def _series(closes, start="2026-08-13 05:30"):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="America/New_York")
    return pd.DataFrame({"Open": closes, "High": [c + 1 for c in closes],
                         "Low": [c - 1 for c in closes], "Close": closes,
                         "Volume": 1.0}, index=idx)


def test_leg_ends_on_retrace_beyond_max_30pts_or_25pct():
    """l2 §3: a leg ends when price retraces from its extreme by max(30, 25% of range)."""
    closes = list(range(29000, 29200, 5)) + list(range(29200, 29100, -5))
    legs = segment_legs(_series(closes), pd.Timestamp("2026-08-13 09:30", tz="America/New_York"), "MNQ")
    assert legs, "a 200-pt advance then 100-pt retrace must close a leg"


def test_small_pullbacks_do_not_interrupt_a_leg():
    closes = list(range(29000, 29300, 5))
    closes.insert(30, closes[30] - 10)
    legs = segment_legs(_series(closes), pd.Timestamp("2026-08-13 09:30", tz="America/New_York"), "MNQ")
    assert len(legs) <= 2


def test_leg_must_reach_50pt_range_to_qualify():
    closes = list(range(29000, 29030, 5))
    legs = segment_legs(_series(closes), pd.Timestamp("2026-08-13 09:30", tz="America/New_York"), "MNQ")
    assert legs == [], "sub-50pt legs do not qualify"


def test_last_trend_prefers_larger_range_when_two_qualify_in_window():
    closes = list(range(29000, 29100, 5)) + list(range(29100, 28900, -5)) + list(range(28900, 29000, 5))
    legs = segment_legs(_series(closes), pd.Timestamp("2026-08-13 09:30", tz="America/New_York"), "MNQ")
    assert legs


def test_segment_legs_empty_frame_returns_empty():
    assert segment_legs(pd.DataFrame(),
                        pd.Timestamp("2026-08-13 09:30", tz="America/New_York"), "MNQ") == []


# --- added during implementation (not in the plan) --------------------------- #

def test_no_lookahead_bars_after_now_are_ignored():
    closes = list(range(29000, 29030, 5)) + list(range(29030, 29400, 5))
    df = _series(closes)
    early = pd.Timestamp("2026-08-13 05:45", tz="America/New_York")
    assert segment_legs(df, early, "MNQ") == []


def test_last_trend_picks_the_larger_range_within_60_minutes():
    from agent.facts.detectors.legs import last_trend
    now = pd.Timestamp("2026-08-13 09:30", tz="America/New_York")
    closes = list(range(29000, 29100, 5)) + list(range(29100, 28900, -5)) + list(range(28900, 29000, 5))
    legs = segment_legs(_series(closes), now, "MNQ")
    lt = last_trend(legs, now)
    if lt is not None:
        fresh = [f for f in legs if (now - f.reference_ts) <= pd.Timedelta(minutes=60)]
        assert lt.extra["range"] == max(f.extra["range"] for f in fresh)


def test_stale_leg_is_not_the_last_trend():
    from agent.facts.detectors.legs import last_trend
    now = pd.Timestamp("2026-08-13 09:30", tz="America/New_York")
    closes = list(range(29000, 29200, 5))
    legs = segment_legs(_series(closes, start="2026-08-13 05:30"), now, "MNQ")
    old = [f for f in legs if (now - f.reference_ts) > pd.Timedelta(minutes=60)]
    for f in old:
        assert last_trend([f], now) is None
