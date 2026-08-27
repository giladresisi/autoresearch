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


# ── identity vs existence (l2-mechanisms.md §2, pinned 2026-08-27) ──────────────
# added during implementation (not in the plan)

def _bull_fvg_frame():
    """1m bars 16:29..16:34 with a bull FVG on 16:30 / 16:31 / 16:32.

    bar1 16:30 High=101, bar3 16:32 Low=103 -> gap [101, 103], middle bar 16:31.
    """
    tz = "America/New_York"
    rows = [("16:29", 100, 101, 99, 100), ("16:30", 100, 101, 99, 100),
            ("16:31", 101, 108, 101, 107), ("16:32", 105, 112, 103, 110),
            # Lows kept BELOW 16:31's high (108) and 16:32's high (112) so the tail
            # forms no second imbalance — this frame must contain exactly one gap.
            ("16:33", 106, 111, 104, 110), ("16:34", 106, 111, 104, 110)]
    idx = [pd.Timestamp("2026-08-21 " + t, tz=tz) for t, *_ in rows]
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c} for _, o, h, l, c in rows],
        index=idx)


def test_fvg_identity_is_the_middle_bar():
    """The gap is named for the bar it is VISIBLE across — what a chart shows."""
    facts, _ = detect_fvgs(_bull_fvg_frame(), "1min", "MNQ", {})
    assert len(facts) == 1
    assert facts[0].reference_ts == pd.Timestamp("2026-08-21 16:31",
                                                 tz="America/New_York")


def test_fvg_exists_from_is_the_third_bars_completion_not_its_label():
    """EXISTENCE is third-bar COMPLETION: label(16:32) + 1min = 16:33:00.

    A consumer reading the third bar's LABEL as the instant is early by one bar-width
    — the §11 08-21 erratum (a fill recorded 09:36:04 on a gap that existed at 09:40).
    """
    facts, _ = detect_fvgs(_bull_fvg_frame(), "1min", "MNQ", {})
    tz = "America/New_York"
    assert facts[0].extra["confirm_bar_ts"] == pd.Timestamp("2026-08-21 16:32", tz=tz)
    assert facts[0].extra["exists_from"] == pd.Timestamp("2026-08-21 16:33", tz=tz)


def test_the_gap_is_not_detected_before_its_third_bar_completes():
    """Detection timing must match existence. `bars.resample` emits COMPLETED bins only,
    so a frame whose last bin is 16:32 means 16:32 has closed — i.e. it is 16:33:00."""
    df = _bull_fvg_frame()
    seen_at = [df.index[n - 1] for n in range(3, len(df) + 1)
               if detect_fvgs(df.iloc[:n], "1min", "MNQ", {})[0]]
    assert seen_at, "the gap must be detected at some point"
    assert seen_at[0] == pd.Timestamp("2026-08-21 16:32", tz="America/New_York")


def test_exists_from_scan_start_matches_the_legacy_confirm_bar_ts_behaviour():
    """The switch to `exists_from` is behaviour-preserving: it selects the same first bar
    the old `searchsorted(confirm_bar_ts, side='right')` did."""
    df = _bull_fvg_frame()
    facts, _ = detect_fvgs(df, "1min", "MNQ", {})
    f = facts[0]
    legacy = int(df.index.searchsorted(f.extra["confirm_bar_ts"], side="right"))
    current = int(df.index.searchsorted(f.extra["exists_from"], side="left"))
    assert legacy == current
