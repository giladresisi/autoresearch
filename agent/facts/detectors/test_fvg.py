import pandas as pd
from agent.facts.detectors.fvg import detect_fvgs, update_fvg_states
from agent.facts.records import FactState


def _ohlc(rows, start="2026-08-13 09:30"):
    idx = pd.date_range(start, periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"]).assign(Volume=1.0)


def test_detects_bull_fvg_when_bar3_low_above_bar1_high():
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    assert len(facts) == 1
    f = facts[0]
    assert f.extra["direction"] == "bull"
    assert f.price_low == 12 and f.price_high == 15


def test_detects_bear_fvg_when_bar3_high_below_bar1_low():
    df = _ohlc([[22, 24, 20, 21], [18, 19, 13, 14], [12, 15, 10, 11]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    assert len(facts) == 1 and facts[0].extra["direction"] == "bear"
    assert facts[0].price_low == 15 and facts[0].price_high == 20


def test_no_fvg_when_bars_overlap():
    df = _ohlc([[10, 15, 9, 11], [12, 16, 10, 14], [13, 17, 11, 15]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    assert facts == []


def test_fvg_window_never_spans_a_session_boundary():
    """3-bar windows straddling the 16:55-18:00 break must not register the gap as an FVG."""
    pre = _ohlc([[10, 12, 9, 11], [11, 13, 10, 12]], start="2026-08-13 16:45")
    post = _ohlc([[80, 85, 78, 82]], start="2026-08-13 18:00")
    facts, _ = detect_fvgs(pd.concat([pre, post]), "5min", "MNQ", {})
    assert facts == [], "the maintenance gap is not an imbalance"


def test_close_through_far_end_invalidates():
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    later = _ohlc([[14, 15, 8, 9]], start="2026-08-13 09:45")   # closes 9, below low 12
    out = update_fvg_states(facts, pd.concat([df, later]), "5min")
    assert out[0].state is FactState.INVALIDATED


def test_wick_through_without_close_does_not_invalidate():
    """A gap wicked through but repelled on the close is a PROVEN barrier (l2 §2)."""
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    later = _ohlc([[16, 17, 8, 16]], start="2026-08-13 09:45")  # wicks to 8, closes 16
    out = update_fvg_states(facts, pd.concat([df, later]), "5min")
    assert out[0].state is FactState.LIVE


def test_max_anti_excursion_is_tracked_as_running_max():
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    later = _ohlc([[20, 21, 19, 20], [20, 21, 17, 20]], start="2026-08-13 09:45")
    out = update_fvg_states(facts, pd.concat([df, later]), "5min")
    assert out[0].extra["max_anti_excursion"] >= 0.0


def test_detect_is_idempotent_on_the_same_bars():
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    a, st = detect_fvgs(df, "5min", "MNQ", {})
    b, _ = detect_fvgs(df, "5min", "MNQ", st)
    assert [f.id for f in a] == [f.id for f in (a + b)][: len(a)]
    assert len(b) == 0, "already-detected gaps must not be re-emitted"


def test_empty_frame_returns_empty_and_does_not_raise():
    facts, st = detect_fvgs(pd.DataFrame(), "5min", "MNQ", {})
    assert facts == [] and isinstance(st, dict)


# --- added during implementation (not in the plan) --------------------------- #

def test_anti_excursion_records_real_distance_past_the_far_end():
    df = _ohlc([[10, 12, 9, 11], [13, 18, 12, 17], [19, 22, 15, 21]])
    facts, _ = detect_fvgs(df, "5min", "MNQ", {})
    later = _ohlc([[16, 17, 2, 16]], start="2026-08-13 09:45")   # wick to 2, far end 12
    out = update_fvg_states(facts, pd.concat([df, later]), "5min")
    assert out[0].extra["max_anti_excursion"] == 10.0
    assert out[0].state is FactState.LIVE
