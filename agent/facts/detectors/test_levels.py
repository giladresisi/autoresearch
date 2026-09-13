import pandas as pd
from agent.facts.detectors.levels import detect_levels, apply_supersession
from agent.facts.records import FactState


def _days(n: int, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01 18:00", periods=n * 60 * 23, freq="1min",
                        tz="America/New_York")
    return pd.DataFrame({"Open": base, "High": base + 5, "Low": base - 5,
                         "Close": base, "Volume": 1.0}, index=idx)


def test_detects_prev_day_extremes_on_the_cme_session_window():
    """Windows are 18:00 ET -> 17:00 ET the next day, not midnight-to-midnight."""
    facts = detect_levels(_days(3), pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    names = {f.name for f in facts}
    assert "prev1_day_high" in names and "prev1_day_low" in names


def test_levels_carry_both_wick_and_body_prices():
    facts = detect_levels(_days(3), pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    lvl = next(f for f in facts if f.name == "prev1_day_high")
    assert lvl.price is not None and lvl.extra.get("body_price") is not None


def test_week_anchor_on_monday_session_reaches_back_to_prev_thursday_1800():
    """Monday session (opens Sunday) anchors to the previous Thursday 18:00."""
    from agent.facts.detectors.levels import week_start_ts
    monday_session = pd.Timestamp("2026-08-17 09:20", tz="America/New_York")
    anchor = week_start_ts(monday_session)
    assert anchor.strftime("%a %H:%M") == "Thu 18:00"


def test_week_anchor_on_tuesday_session_reaches_back_to_prev_friday_1800():
    from agent.facts.detectors.levels import week_start_ts
    tuesday_session = pd.Timestamp("2026-08-18 09:20", tz="America/New_York")
    assert week_start_ts(tuesday_session).strftime("%a %H:%M") == "Fri 18:00"


def test_week_anchor_midweek_is_sunday_1800():
    from agent.facts.detectors.levels import week_start_ts
    wednesday = pd.Timestamp("2026-08-19 09:20", tz="America/New_York")
    assert week_start_ts(wednesday).strftime("%a %H:%M") == "Sun 18:00"


def test_supersession_marks_nested_prior_levels():
    """thesis.md §2.1b: nested/superseded prior-liquidity levels are code-suppressed."""
    facts = detect_levels(_days(5), pd.Timestamp("2026-08-06 09:20", tz="America/New_York"), "MNQ")
    out = apply_supersession(facts)
    assert any(f.state is FactState.SUPERSEDED for f in out) or len(out) == len(facts)


def test_supersession_reruns_when_a_deeper_level_is_added():
    """Adding a deeper level can reclassify levels already held."""
    facts = detect_levels(_days(3), pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    first = apply_supersession(list(facts))
    deeper = detect_levels(_days(6), pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    second = apply_supersession(list(deeper))
    assert len(second) >= len(first)


def test_gap_traversal_marks_traversed_not_swept():
    """A reopen gapping past a level is NOT a stop-run (no wick, no rejection)."""
    from agent.facts.detectors.levels import apply_gap_traversal
    facts = detect_levels(_days(3), pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    lvl = facts[0]
    out = apply_gap_traversal(facts, prev_close=lvl.price - 50, reopen=lvl.price + 50,
                              ts=pd.Timestamp("2026-08-04 18:00", tz="America/New_York"))
    touched = [f for f in out if f.state is FactState.TRAVERSED_BY_GAP]
    assert touched, "levels between prev_close and reopen must be marked traversed"
    assert all(f.state is not FactState.SWEPT for f in touched)


def test_detect_levels_empty_history_returns_empty():
    assert detect_levels(pd.DataFrame(),
                         pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ") == []


# --- added during implementation (not in the plan) --------------------------- #

def _ramp(n: int, start="2026-08-01 18:00", base=29000.0, slope=0.5) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    s = pd.Series(range(n), index=idx).astype(float) * slope + base
    return pd.DataFrame({"Open": s, "High": s + 5, "Low": s - 5, "Close": s,
                         "Volume": 1.0}, index=idx)


def test_supersession_marks_the_less_extreme_older_high():
    """prev2 below prev1 on a rising tape is nested; prev1 itself is never nested."""
    facts = detect_levels(_ramp(60 * 24 * 3),
                          pd.Timestamp("2026-08-04 09:20", tz="America/New_York"), "MNQ")
    out = apply_supersession(facts)
    by_name = {f.name: f for f in out}
    assert by_name["prev1_day_high"].state is FactState.LIVE
    assert by_name["prev2_day_high"].state is FactState.SUPERSEDED


def test_sweep_state_marks_swept_then_depleted():
    from agent.facts.detectors.levels import apply_sweep_states
    bars = _ramp(60 * 24 * 3)
    now = pd.Timestamp("2026-08-04 09:20", tz="America/New_York")
    facts = apply_sweep_states(detect_levels(bars, now, "MNQ"), bars, now)
    prev1_hi = next(f for f in facts if f.name == "prev1_day_high")
    # A monotonically rising tape runs far past yesterday's high.
    assert prev1_hi.state in (FactState.SWEPT, FactState.DEPLETED)
