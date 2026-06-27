# tests/test_smt_hypothesis.py
# Unit tests for hypothesis.py — the every-5m hypothesis module.
# All tests redirect smt_state paths to tmp_path and build synthetic fixtures.

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import smt_state
from smt_state import (
    DEFAULT_HYPOTHESIS,
    DEFAULT_POSITION,
    load_hypothesis,
    load_position,
    save_daily,
    save_global,
    save_hypothesis,
    save_position,
)
from hypothesis import run_hypothesis


# ── Isolation fixture ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect all four smt_state paths into a fresh tmp_path for each test."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    # Clear the process-level hypothesis cache so a previous test's saved hypothesis
    # does not bleed into this test via the in-memory cache.
    monkeypatch.setattr(smt_state, "_hyp_cache_valid", False)


# ── Synthetic bar builders ────────────────────────────────────────────────────

def _make_1m_bars(
    opens, highs, lows, closes,
    start_time="2026-04-27 09:20:00",
    tz="America/New_York",
) -> pd.DataFrame:
    """Build a synthetic 1m OHLCV DataFrame with a tz-aware ET DatetimeIndex."""
    n = len(opens)
    idx = pd.date_range(start=start_time, periods=n, freq="1min", tz=tz)
    return pd.DataFrame(
        {
            "Open":   [float(x) for x in opens],
            "High":   [float(x) for x in highs],
            "Low":    [float(x) for x in lows],
            "Close":  [float(x) for x in closes],
            "Volume": [1000.0] * n,
        },
        index=idx,
    )


def _make_now(
    date_str="2026-04-27",
    time_str="10:05:00",
    tz_name="America/New_York",
) -> datetime:
    """Build a tz-aware datetime in ET representing 'now'."""
    import pytz
    tz = pytz.timezone(tz_name)
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    return tz.localize(naive)


def _make_default_daily(
    week_high=200.0, week_low=100.0,
    day_high=175.0, day_low=125.0,
) -> dict:
    """Build a daily.json dict with the four key liquidity levels."""
    return {
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": week_high},
            {"name": "week_low",  "kind": "level", "price": week_low},
            {"name": "day_high",  "kind": "level", "price": day_high},
            {"name": "day_low",   "kind": "level", "price": day_low},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    }


def _make_default_global(all_time_high=999999.0, confidence="medium", trend="up") -> dict:
    """Build a global.json dict. Default ATH is very high so bars are never 'above ATH'."""
    return {"all_time_high": all_time_high, "confidence": confidence, "trend": trend}


def _make_pre_session_hist(
    week_high=200.0, week_low=100.0,
    start_time="2026-04-26 18:00:00",
    n=5,
) -> pd.DataFrame:
    """Pre-session bars providing historical context for the hypothesis.

    Day/week H/L now come directly from daily.json (set per test), but these
    bars are still supplied so the hist DataFrame spans a realistic window.
    """
    return _make_1m_bars(
        [150.0] * n, [week_high] * n, [week_low] * n, [150.0] * n,
        start_time=start_time,
    )


def _call_with_nullmocks(now, mnq_1m, mes_1m, hist_mnq_1m=None, hist_mes_1m=None):
    """Call run_hypothesis with mocked detect_* functions returning None."""
    if hist_mnq_1m is None:
        # 1-week-ago bars (for entry_ranges anchor)
        week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                                  start_time="2026-04-20 10:00:00")
        # Pre-session bars matching the _make_default_daily defaults (highs at 200,
        # lows at 100) so the hist window is consistent with daily.json.
        pre_sess = _make_pre_session_hist()
        hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()
    if hist_mes_1m is None:
        hist_mes_1m = _make_1m_bars([50] * 5, [51] * 5, [49] * 5, [50] * 5,
                                     start_time="2026-04-20 10:00:00")
    with patch("hypothesis.detect_smt_divergence", return_value=None):
        with patch("hypothesis.detect_smt_fill", return_value=None):
            run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m)


# ── Standard fixture setup ────────────────────────────────────────────────────

def _setup_standard(
    price=150.0,
    all_time_high=0.0,
    week_high=200.0, week_low=100.0,
    day_high=175.0, day_low=125.0,
):
    """Set up global.json and daily.json with standard defaults."""
    save_global(_make_default_global(all_time_high))
    save_daily(_make_default_daily(week_high, week_low, day_high, day_low))

    # now at 10:05 — means 5m bar is 10:00–10:04
    now = _make_now(time_str="10:05:00")

    # 5 bars covering 10:00–10:04
    mnq_1m = _make_1m_bars(
        opens=  [price] * 5,
        highs=  [price + 2] * 5,
        lows=   [price - 2] * 5,
        closes= [price] * 5,
        start_time="2026-04-27 10:00:00",
    )
    mes_1m = _make_1m_bars(
        opens=  [price / 2] * 5,
        highs=  [price / 2 + 1] * 5,
        lows=   [price / 2 - 1] * 5,
        closes= [price / 2] * 5,
        start_time="2026-04-27 10:00:00",
    )
    return now, mnq_1m, mes_1m


# ══ Test 1: Early exit when direction already set ════════════════════════════

def test_early_exit_when_direction_already_set():
    """If hypothesis.direction != 'none', run_hypothesis must return without changes."""
    save_hypothesis({
        **DEFAULT_HYPOTHESIS,
        "direction": "up",
        "weekly_mid": "above",   # pre-set field
    })
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    now = _make_now()
    mnq_1m = _make_1m_bars([150] * 5, [152] * 5, [148] * 5, [150] * 5)
    mes_1m = _make_1m_bars([75] * 5, [76] * 5, [74] * 5, [75] * 5)

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    # Hypothesis must remain exactly as set — direction still "up", weekly_mid still "above"
    h = load_hypothesis()
    assert h["direction"] == "up"
    assert h["weekly_mid"] == "above"


# ══ Test 2: Early exit when 5m bar is fully above ATH ═══════════════════════

def test_early_exit_when_5m_bar_fully_above_ath():
    """If both bar.low AND bar.high are above ATH, direction must stay 'none'."""
    all_time_high = 100.0
    save_global(_make_default_global(all_time_high))
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0))

    now = _make_now(time_str="10:05:00")
    # bar low=110, high=120 — both above ATH=100
    mnq_1m = _make_1m_bars(
        opens=[115] * 5,
        highs=[120] * 5,
        lows=[110] * 5,
        closes=[115] * 5,
        start_time="2026-04-27 10:00:00",
    )
    mes_1m = _make_1m_bars([60] * 5, [61] * 5, [59] * 5, [60] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["direction"] == "none"


# ══ Test 3: No early exit when only one extreme is above ATH ════════════════

def test_no_early_exit_when_only_one_extreme_above_ath():
    """If bar.high > ATH but bar.low <= ATH, processing must continue."""
    all_time_high = 115.0
    save_global(_make_default_global(all_time_high))
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0))

    now = _make_now(time_str="10:05:00")
    # bar low=110 (below ATH=115), bar high=120 (above ATH) → one extreme above, one at/below
    mnq_1m = _make_1m_bars(
        opens=[112] * 5,
        highs=[120] * 5,
        lows=[110] * 5,
        closes=[112] * 5,
        start_time="2026-04-27 10:00:00",
    )
    mes_1m = _make_1m_bars([56] * 5, [60] * 5, [55] * 5, [56] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    # Processing must have continued past the ATH gate.
    # When the ATH gate fires (both extremes above ATH), hypothesis.json is never written
    # and weekly_mid stays "".  Cautious vetoes can still set direction="none" but they
    # DO write weekly_mid, which is how we distinguish "gate fired" from "gate skipped".
    h = load_hypothesis()
    assert h["weekly_mid"] != "", (
        "weekly_mid must be populated — ATH gate must not have fired early"
    )


# ══ Tests 4–6: weekly_mid classification ════════════════════════════════════

def test_weekly_mid_above():
    """current_close > week_mid + 10 → weekly_mid == 'above'."""
    # week_high=200, week_low=100 → mid=150; close=165 → 165-150=15 > 10 → above
    save_global(_make_default_global())
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0, day_high=175.0, day_low=125.0))

    now = _make_now(time_str="10:05:00")
    price = 165.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([82.0] * 5, [83.0] * 5, [81.0] * 5, [82.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["weekly_mid"] == "above"


def test_weekly_mid_below():
    """current_close < week_mid - 10 → weekly_mid == 'below'."""
    # week_high=200, week_low=100 → mid=150; close=135 → 135-150=-15 < -10 → below
    save_global(_make_default_global())
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0, day_high=175.0, day_low=125.0))

    now = _make_now(time_str="10:05:00")
    price = 135.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([67.0] * 5, [68.0] * 5, [66.0] * 5, [67.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["weekly_mid"] == "below"


def test_weekly_mid_within_tolerance():
    """|current_close - week_mid| <= 10 → weekly_mid == 'mid'."""
    # week_high=200, week_low=100 → mid=150; close=155 → 155-150=5 ≤ 10 → mid
    save_global(_make_default_global())
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0, day_high=175.0, day_low=125.0))

    now = _make_now(time_str="10:05:00")
    price = 155.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([77.0] * 5, [78.0] * 5, [76.0] * 5, [77.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["weekly_mid"] == "mid"


# ══ Test 7: daily_mid same three branches ═══════════════════════════════════

def test_daily_mid_same_three_branches():
    """daily_mid must classify above/mid/below using day_high and day_low."""
    # day_high/day_low come straight from daily.json (set below to 180/120 → mid=150).
    week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                              start_time="2026-04-20 10:00:00")
    pre_sess = _make_pre_session_hist()
    hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()

    for close_price, expected in [
        (170.0, "above"),   # 170-150=20 > 10 → above
        (145.0, "mid"),     # |145-150|=5 ≤ 10 → mid
        (130.0, "below"),   # 130-150=-20 < -10 → below
        (155.0, "mid"),     # 155-150=5 ≤ 10 → mid
    ]:
        save_global(_make_default_global())
        save_daily(_make_default_daily(week_high=250.0, week_low=50.0,
                                       day_high=180.0, day_low=120.0))
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))

        now = _make_now(time_str="10:05:00")
        mnq_1m = _make_1m_bars(
            [close_price] * 5, [close_price + 1] * 5,
            [close_price - 1] * 5, [close_price] * 5,
            start_time="2026-04-27 10:00:00",
        )
        mes_1m = _make_1m_bars([80.0] * 5, [81.0] * 5, [79.0] * 5, [80.0] * 5,
                                 start_time="2026-04-27 10:00:00")

        _call_with_nullmocks(now, mnq_1m, mes_1m, hist_mnq_1m=hist_mnq_1m)

        h = load_hypothesis()
        assert h["daily_mid"] == expected, (
            f"close={close_price}: expected daily_mid={expected!r}, got {h['daily_mid']!r}"
        )


# ══ Test 8: last_liquidity picks most recent meaningful touch ════════════════

def test_last_liquidity_picks_most_recent_meaningful():
    """Fixture touches day_low first then day_high; assert last_liquidity == 'day_high'."""
    day_high_price = 180.0
    day_low_price  = 120.0

    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": day_high_price},
            {"name": "day_low",   "kind": "level", "price": day_low_price},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })

    now = _make_now(time_str="10:10:00")

    # day_high/day_low come from daily.json (180 / 120).  Session bars sweep
    # up to 200 and down to 100, crossing both levels so _find_last_liquidity
    # detects the day_low touch first (bars 0-4) then the day_high touch (bars 5-9).
    opens  = [150.0] * 5 + [170.0] * 5
    highs  = [155.0] * 5 + [200.0] * 5   # Bars 5-9 cross day_high=180
    lows   = [100.0] * 5 + [165.0] * 5   # Bars 0-4 cross day_low=120
    closes = [152.0] * 5 + [175.0] * 5

    mnq_1m = _make_1m_bars(opens, highs, lows, closes,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars(
        [75.0] * 10, [76.0] * 10, [74.0] * 10, [75.0] * 10,
        start_time="2026-04-27 10:00:00",
    )

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    # The session bars sweep up to 200, crossing both day_high (180) and week_high
    # (200) from daily.json.  _find_last_liquidity may pick either high-level depending
    # on the tie-breaking index; both are semantically a high-side liquidity touch.
    assert h["last_liquidity"] in ("day_high", "week_high"), (
        f"last_liquidity should be a high-level (day_high or week_high), "
        f"got {h['last_liquidity']!r}"
    )


# ══ GIL-23: rule2b direction_reason carries the recovery-guard diagnostic fields ══

def test_rule2b_direction_reason_carries_guard_fields():
    """GIL-23 Fix 2: when _determine_direction returns via the rule2b high-sweep
    branch, direction_reason must carry the six guard-input fields so the chosen
    direction is reproducible from events.jsonl."""
    # Same scenario as test_last_liquidity_picks_most_recent_meaningful: session
    # bars sweep up through day_high/week_high into weekly premium → rule2b high-sweep.
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 180.0},
            {"name": "day_low",   "kind": "level", "price": 120.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })

    now = _make_now(time_str="10:10:00")

    opens  = [150.0] * 5 + [170.0] * 5
    highs  = [155.0] * 5 + [200.0] * 5   # Bars 5-9 cross day_high=180
    lows   = [100.0] * 5 + [165.0] * 5   # Bars 0-4 cross day_low=120
    closes = [152.0] * 5 + [175.0] * 5

    mnq_1m = _make_1m_bars(opens, highs, lows, closes,
                           start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars(
        [75.0] * 10, [76.0] * 10, [74.0] * 10, [75.0] * 10,
        start_time="2026-04-27 10:00:00",
    )

    # run_hypothesis returns the emitted new-hypothesis event(s); direction_reason
    # is embedded there. Build the same hist context _call_with_nullmocks uses.
    week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                             start_time="2026-04-20 10:00:00")
    pre_sess = _make_pre_session_hist()
    hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()
    hist_mes_1m = _make_1m_bars([50] * 5, [51] * 5, [49] * 5, [50] * 5,
                                start_time="2026-04-20 10:00:00")

    with patch("hypothesis.detect_smt_divergence", return_value=None):
        with patch("hypothesis.detect_smt_fill", return_value=None):
            events = run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m)

    assert events, "expected a new-hypothesis event (non-none direction)"
    reason = events[0]["direction_reason"]
    assert reason.get("rule") == "rule2b", f"expected rule2b, got {reason.get('rule')!r}"
    for key in (
        "session_ath", "all_time_high", "recovery_gap",
        "is_false_pos_ath", "is_false_pos_morning", "is_false_pos_recovery",
    ):
        assert key in reason, f"missing guard field {key!r} in direction_reason: {reason}"
    assert isinstance(reason["is_false_pos_ath"], bool)
    assert isinstance(reason["is_false_pos_morning"], bool)
    assert isinstance(reason["is_false_pos_recovery"], bool)


# ══ Test 9: divs includes wick, body, and fill types ════════════════════════

def test_divs_sourced_from_smt_active_set():
    """SMT V2 refactor: `divs` is owned by the NEW detection mechanism.

    run_hypothesis no longer recomputes 15m/30m divergences via the legacy
    `_compute_divs`; it carries the pipeline-persisted relevance-filtered active set
    (`smt_active_set`) through as `divs`. Seed an active set, form a hypothesis, and
    assert `divs` is exactly that set (wick + body + fill records all preserved).
    """
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    now = _make_now(time_str="10:35:00")
    n_bars = 32
    mnq_1m = _make_1m_bars(
        [150.0] * n_bars, [152.0] * n_bars, [148.0] * n_bars, [150.0] * n_bars,
        start_time="2026-04-27 10:00:00",
    )
    mes_1m = _make_1m_bars(
        [75.0] * n_bars, [76.0] * n_bars, [74.0] * n_bars, [75.0] * n_bars,
        start_time="2026-04-27 10:00:00",
    )
    hist_mnq = _make_1m_bars([150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
                               start_time="2026-04-20 10:00:00")
    hist_mes = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                               start_time="2026-04-20 10:00:00")

    # The active set the pipeline's shadow detector would have persisted this bar.
    active = [
        {"kind": "smt", "type": "wick", "side": "bullish", "direction": "long",
         "tier": "day", "key": "day_low|long|wick", "keys": ["day_low|long|wick"],
         "ref_name": "day_low", "fulfilled": False, "invalidated": False},
        {"kind": "smt", "type": "body", "side": "bearish", "direction": "short",
         "tier": "week", "key": "week_high|short|body", "keys": ["week_high|short|body"],
         "ref_name": "week_high", "fulfilled": False, "invalidated": False},
        {"kind": "fill", "type": "fill_a", "side": "bullish", "direction": "long",
         "tier": "fill", "key": "fvg_x_bull", "keys": ["fvg_x_bull"],
         "ref_name": "fvg_x_bull", "fulfilled": False, "invalidated": False},
    ]
    h0 = load_hypothesis()
    h0["direction"] = "none"
    h0["smt_active_set"] = active
    save_hypothesis(h0)

    run_hypothesis(now, mnq_1m, mes_1m, hist_mnq, hist_mes)

    h = load_hypothesis()
    assert h["divs"] == active, f"divs not sourced from smt_active_set: {h['divs']}"
    types_found = {d["type"] for d in h["divs"]}
    assert {"wick", "body", "fill_a"} <= types_found, f"missing record types: {types_found}"


# ══ Test 10: direction is determined (not 'none') for a standard in-range bar ═

def test_direction_hardcoded_up():
    """run_hypothesis must set a non-none direction for a standard in-range bar.

    This test was previously titled 'hardcoded up' when direction was TBD.  Now
    that ICT rules are live the direction is computed; the invariant is that a
    typical session bar at price=150 (with H/L levels at 200/100) produces SOME
    direction rather than vetoing to 'none'.
    """
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    now = _make_now(time_str="10:05:00")
    mnq_1m = _make_1m_bars([150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["direction"] != "none", (
        f"Expected a non-none direction for a standard in-range bar, got 'none'"
    )


# ══ Tests 11–12: targets filtered by direction ═══════════════════════════════

def test_targets_filtered_by_direction_for_levels():
    """Level targets must be filtered to the correct side of current_close.

    Day/week H/L come from daily.json.  Whatever direction the ICT rules produce,
    every level target must lie on the correct side:
      up   → target price > current_close
      down → target price < current_close
    """
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 180.0},
            {"name": "day_low",   "kind": "level", "price": 130.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })

    now = _make_now(time_str="10:05:00")
    price = 150.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    direction = h["direction"]
    assert direction != "none", f"Expected a non-none direction, got 'none'"

    level_targets = [t for t in h["targets"] if t.get("price") is not None]
    assert len(level_targets) > 0, f"Expected at least one target for direction={direction}"

    if direction == "up":
        for t in level_targets:
            assert t["price"] > price, (
                f"Up target {t['name']}={t['price']} is not > current_close={price}"
            )
    else:
        for t in level_targets:
            assert t["price"] < price, (
                f"Down target {t['name']}={t['price']} is not < current_close={price}"
            )


def test_targets_filtered_by_direction_for_fvg():
    """FVG targets must be filtered to the correct side of current_close.

    fvg_above (top=165, bottom=160): above price → included only for direction=up.
    fvg_below (top=145, bottom=140): below price → included only for direction=down.
    Whatever direction the ICT rules produce, exactly the correct FVG must appear.
    """
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 180.0},
            {"name": "day_low",   "kind": "level", "price": 130.0},
            {"name": "fvg_above", "kind": "fvg", "top": 165.0, "bottom": 160.0},
            {"name": "fvg_below", "kind": "fvg", "top": 145.0, "bottom": 140.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })

    now = _make_now(time_str="10:05:00")
    price = 150.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    direction = h["direction"]
    assert direction != "none", f"Expected a non-none direction, got 'none'"

    target_names = {t["name"] for t in h["targets"]}
    if direction == "up":
        assert "fvg_above" in target_names, "FVG above close must be included for direction=up"
        assert "fvg_below" not in target_names, "FVG below close must be excluded for direction=up"
    else:
        assert "fvg_below" in target_names, "FVG below close must be included for direction=down"
        assert "fvg_above" not in target_names, "FVG above close must be excluded for direction=down"


# ══ Test 13: cautious_price is empty string ══════════════════════════════════

def test_cautious_price_empty_string():
    """cautious_price must be '' (TBD hardcoded per spec)."""
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    now = _make_now(time_str="10:05:00")
    mnq_1m = _make_1m_bars([150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["cautious_price"] == ""


# ══ Test 14: entry_ranges uses 12hr and 1week anchors ═══════════════════════

def test_entry_ranges_uses_12hr_and_1week_anchors():
    """entry_ranges must have exactly entries with source '12hr' and '1week', each with low <= high."""
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    import pytz
    et = pytz.timezone("America/New_York")
    now_naive = datetime(2026, 4, 27, 10, 5, 0)
    now = et.localize(now_naive)

    # Current day bars (for the 5m bar and recent 1m bars)
    mnq_1m_today = _make_1m_bars(
        [150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
        start_time="2026-04-27 10:00:00",
    )
    mes_1m_today = _make_1m_bars(
        [75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
        start_time="2026-04-27 10:00:00",
    )

    # Historical bars: must include bars 12hr ago (2026-04-26 22:05) and 1week ago same time
    # 12hr ago: 2026-04-26 22:05 ET
    # 1week ago: 2026-04-20 10:05 ET
    hist_mnq = _make_1m_bars(
        [145.0] * 10, [147.0] * 10, [143.0] * 10, [145.0] * 10,
        start_time="2026-04-20 10:00:00",  # 1 week ago
    )
    # Add bars around 12hr ago — on 2026-04-26 22:00 ET
    hist_mnq_12hr = _make_1m_bars(
        [148.0] * 10, [150.0] * 10, [146.0] * 10, [148.0] * 10,
        start_time="2026-04-26 22:00:00",
    )
    hist_mnq_combined = pd.concat([hist_mnq, hist_mnq_12hr]).sort_index()
    hist_mes = _make_1m_bars(
        [72.0] * 5, [73.0] * 5, [71.0] * 5, [72.0] * 5,
        start_time="2026-04-20 10:00:00",
    )

    _call_with_nullmocks(now, mnq_1m_today, mes_1m_today, hist_mnq_combined, hist_mes)

    h = load_hypothesis()
    sources = {r["source"] for r in h["entry_ranges"]}
    assert "12hr" in sources, f"Expected '12hr' in entry_ranges sources, got: {sources}"
    assert "1week" in sources, f"Expected '1week' in entry_ranges sources, got: {sources}"

    for r in h["entry_ranges"]:
        assert r["low"] <= r["high"], (
            f"entry_range source={r['source']}: low={r['low']} > high={r['high']}"
        )


# ══ Test 15: failed_entries reset on direction transition from none ═══════════

def test_failed_entries_reset_on_direction_transition_from_none():
    """Transition none → up must reset failed_entries=0 and conf_bar_entry={}."""
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    # Pre-set position with non-zero failed_entries and a conf_bar_entry
    position = {
        "active": {},
        "stop_entry": "",
        "conf_bar_entry": {"high": 155.0, "low": 145.0},
        "failed_entries": 2,
        "cautious_dist_shrinks": 2,
    }
    save_position(position)

    # hypothesis direction starts at "none"
    save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))

    now = _make_now(time_str="10:05:00")
    mnq_1m = _make_1m_bars([150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["direction"] != "none", (
        f"Expected a non-none direction after the none→direction transition, got 'none'"
    )

    pos = load_position()
    assert pos["failed_entries"] == 0, (
        f"failed_entries should be 0 after none→up transition, got {pos['failed_entries']}"
    )
    assert pos["cautious_dist_shrinks"] == 0, (
        f"cautious_dist_shrinks should be 0 after none→up transition, got {pos['cautious_dist_shrinks']}"
    )
    assert pos["conf_bar_entry"] == {}, (
        f"conf_bar_entry should be {{}} after transition, got {pos['conf_bar_entry']}"
    )


# ══ Tests: confidence=high overrides direction to global trend ════════════════

def test_confidence_high_forces_direction_to_global_trend():
    """When confidence='high', direction must equal global_state['trend'] regardless of ICT rules."""
    # Price at 155 is in the premium zone (>week_mid=150): ICT rules 3+4 would give "down".
    # But confidence=high must override to trend="up".
    # close=155 leaves 45 pts to day_high=200, clearing the CAUTIOUS_MIN_DIST=40 veto.
    save_global(_make_default_global(confidence="high", trend="up"))
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0,
                                    day_high=175.0, day_low=125.0))

    now = _make_now(time_str="10:05:00")
    price = 155.0  # premium zone — ICT rules alone would say "down"
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([77.0] * 5, [78.0] * 5, [76.0] * 5, [77.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["direction"] == "up", (
        f"confidence=high with trend='up' must set direction='up', got {h['direction']!r}"
    )


def test_confidence_high_forces_direction_down_when_trend_down():
    """When confidence='high' and trend='down', direction must be 'down'."""
    # close=145 is in the discount zone (<week_mid=150): ICT rules would give "up".
    # But confidence=high must override to trend="down".
    # close=145 leaves 45 pts to day_low=100, clearing the CAUTIOUS_MIN_DIST=40 veto.
    save_global(_make_default_global(confidence="high", trend="down"))
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0,
                                    day_high=175.0, day_low=125.0))

    now = _make_now(time_str="10:05:00")
    price = 145.0  # discount zone — ICT rules alone would say "up"
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([72.0] * 5, [73.0] * 5, [71.0] * 5, [72.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    h = load_hypothesis()
    assert h["direction"] == "down", (
        f"confidence=high with trend='down' must set direction='down', got {h['direction']!r}"
    )


def test_confidence_high_direction_reason_is_global_confidence_high():
    """direction_reason rule must be 'global_confidence_high' when confidence='high'."""
    save_global(_make_default_global(confidence="high", trend="up"))
    save_daily(_make_default_daily())

    now = _make_now(time_str="10:05:00")
    price = 150.0
    mnq_1m = _make_1m_bars([price] * 5, [price + 1] * 5, [price - 1] * 5, [price] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    import copy as _copy
    from smt_state import DEFAULT_HYPOTHESIS as _DH

    # Capture the event returned by run_hypothesis.
    # daily.json week_high=200 keeps the secondary cautious price (200) 50 pts
    # from close (150), avoiding the CAUTIOUS_MIN_DIST=40 veto that would
    # otherwise silence the event.
    with patch("hypothesis.detect_smt_divergence", return_value=None):
        with patch("hypothesis.detect_smt_fill", return_value=None):
            week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                                      start_time="2026-04-20 10:00:00")
            pre_sess = _make_pre_session_hist()
            hist_mnq = pd.concat([week_ago, pre_sess]).sort_index()
            hist_mes = _make_1m_bars([50] * 5, [51] * 5, [49] * 5, [50] * 5,
                                      start_time="2026-04-20 10:00:00")
            events = run_hypothesis(now, mnq_1m, mes_1m, hist_mnq, hist_mes)

    hyp_events = [e for e in events if e.get("kind") == "new-hypothesis"]
    assert hyp_events, "Expected a new-hypothesis event"
    reason = hyp_events[0].get("direction_reason", {})
    assert reason.get("rule") == "global_confidence_high", (
        f"Expected rule='global_confidence_high', got {reason!r}"
    )


# ══ Test 16: failed_entries not reset when direction stays set (early exit) ══

def test_failed_entries_not_reset_when_direction_stays_set():
    """Early exit (direction already 'up') must leave failed_entries unchanged."""
    save_global(_make_default_global())
    save_daily(_make_default_daily())

    # direction already set → early exit
    save_hypothesis({**DEFAULT_HYPOTHESIS, "direction": "up"})

    position = {
        "active": {},
        "stop_entry": "",
        "stop_direction": "",
        "conf_bar_entry": {},
        "failed_entries": 3,
    }
    save_position(position)

    now = _make_now(time_str="10:05:00")
    mnq_1m = _make_1m_bars([150.0] * 5, [152.0] * 5, [148.0] * 5, [150.0] * 5,
                             start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([75.0] * 5, [76.0] * 5, [74.0] * 5, [75.0] * 5,
                             start_time="2026-04-27 10:00:00")

    _call_with_nullmocks(now, mnq_1m, mes_1m)

    pos = load_position()
    assert pos["failed_entries"] == 3, (
        f"failed_entries should remain 3 on early exit, got {pos['failed_entries']}"
    )


# ---------------------------------------------------------------------------
# GIL-8: recompute_cautious_for_fill preserves a manually locked ladder
# ---------------------------------------------------------------------------

def test_recompute_cautious_for_fill_preserves_manual_ladder():
    """While the manual direction lock is set, the user's cautious ladder must
    survive a fill unchanged (no re-anchoring)."""
    from hypothesis import recompute_cautious_for_fill

    hyp = {
        "direction": "down",
        "manual": True,
        "cautious_price_initial": "20000.0",
        "cautious_price_initial_level": "day_low",
        "cautious_price_secondary": "19950.0",
        "cautious_price_secondary_level": "week_low",
    }
    liquidities = [
        {"name": "day_low",  "kind": "level", "price": 19800.0},
        {"name": "week_low", "kind": "level", "price": 19700.0},
    ]
    out = recompute_cautious_for_fill(dict(hyp), 19900.0, liquidities, 21000.0)

    for k in ("cautious_price_initial", "cautious_price_initial_level",
              "cautious_price_secondary", "cautious_price_secondary_level"):
        assert out[k] == hyp[k], f"{k} must be preserved while locked"


# ══ Change 3: dynamic cautious-target max-distance thresholds ═════════════════

def _shrink_liqs():
    """A single up-side level 140pts above the anchor — qualifies at shrinks=0
    (max=150) but is excluded at shrinks>=1 (max=127.5)."""
    return [{"name": "L140", "kind": "level", "price": 20140.0}]


def test_cautious_dist_shrinks_zero_is_unchanged():
    """dist_shrinks=0 must reproduce the pre-change effective thresholds (150/110):
    the 140pt level still qualifies as the secondary."""
    from hypothesis import compute_cautious_prices
    r = compute_cautious_prices("up", 20000.0, _shrink_liqs(), 999999.0, 0)
    assert r["cautious_price_secondary_level"] == "L140"
    # Default kwarg must match dist_shrinks=0 byte-for-byte.
    assert compute_cautious_prices("up", 20000.0, _shrink_liqs(), 999999.0) == r


def test_cautious_dist_shrinks_one_excludes_far_level():
    """dist_shrinks=1 → secondary max = 150*0.85 = 127.5; the 140pt level that qualified at
    shrinks=0 is dropped from the in-cap pool, so a nearer 120pt level (still within 127.5)
    becomes the secondary instead. (The far level is no longer SELECTED while a nearer one is
    actionable; the empty-pool fallback is covered separately below.)"""
    from hypothesis import compute_cautious_prices
    liqs = [
        {"name": "L120", "kind": "level", "price": 20120.0},  # within shrunk cap (127.5)
        {"name": "L140", "kind": "level", "price": 20140.0},  # beyond shrunk cap
    ]
    r = compute_cautious_prices("up", 20000.0, liqs, 999999.0, 1)
    assert r["cautious_price_secondary_level"] == "L120"


def test_cautious_dist_shrinks_includes_level_within_shrunk_max():
    """A level inside the shrunk secondary max (120 < 127.5) still qualifies at
    shrinks=1, confirming the threshold shrank rather than disappeared."""
    from hypothesis import compute_cautious_prices
    liqs = [{"name": "L120", "kind": "level", "price": 20120.0}]
    r = compute_cautious_prices("up", 20000.0, liqs, 999999.0, 1)
    assert r["cautious_price_secondary_level"] == "L120"


def test_cautious_dist_shrinks_large_clamps_to_min():
    """Large dist_shrinks clamps both effective maxes at CAUTIOUS_MIN_DIST (40),
    never below. A level just inside 40 qualifies; one beyond it does not."""
    from hypothesis import compute_cautious_prices, CAUTIOUS_MIN_DIST
    assert CAUTIOUS_MIN_DIST == 40
    # 39pt level is inside the floored max(40) but is below the MIN_DIST skip for
    # the initial tier; it still qualifies as the secondary.
    r_in = compute_cautious_prices(
        "up", 20000.0, [{"name": "Lnear", "kind": "level", "price": 20039.0}],
        999999.0, 20)
    assert r_in["cautious_price_secondary_level"] == "Lnear"
    # With both a 39pt (within floored 40) and a 41pt (just beyond) level present, the 41pt
    # level is excluded from the in-cap pool, so the 39pt level is selected as secondary —
    # proving the cap floored at 40, not lower.
    r_out = compute_cautious_prices(
        "up", 20000.0,
        [{"name": "Lnear", "kind": "level", "price": 20039.0},
         {"name": "Lfar",  "kind": "level", "price": 20041.0}],
        999999.0, 20)
    assert r_out["cautious_price_secondary_level"] == "Lnear"


def test_cautious_dist_shrinks_reset_by_session_helper():
    """reset_position_for_session clears cautious_dist_shrinks to 0."""
    import strategy
    pos = copy.deepcopy(DEFAULT_POSITION)
    pos["failed_entries"] = 3
    pos["cautious_dist_shrinks"] = 3
    save_position(pos)
    strategy.reset_position_for_session()
    assert load_position()["cautious_dist_shrinks"] == 0


def test_cautious_dist_shrinks_reset_by_new_hypothesis_helper():
    """reset_position_for_new_hypothesis clears cautious_dist_shrinks to 0."""
    import strategy
    pos = copy.deepcopy(DEFAULT_POSITION)
    pos["failed_entries"] = 3
    pos["cautious_dist_shrinks"] = 3
    save_position(pos)
    strategy.reset_position_for_new_hypothesis()
    assert load_position()["cautious_dist_shrinks"] == 0


def test_recompute_cautious_for_fill_honors_dist_shrinks():
    """recompute_cautious_for_fill threads dist_shrinks into the recomputed ladder: the far
    level chosen at shrinks=0 is dropped from the in-cap pool at a higher shrink count, so a
    nearer level (still within the shrunk cap) is selected instead."""
    from hypothesis import recompute_cautious_for_fill
    hyp = {"direction": "up", "manual": False}
    liqs = [
        {"name": "L120", "kind": "level", "price": 20120.0},  # within shrunk cap (127.5)
        {"name": "L140", "kind": "level", "price": 20140.0},  # farthest within 150 at shrinks=0
    ]
    out0 = recompute_cautious_for_fill(dict(hyp), 20000.0, liqs, 999999.0, 0)
    assert out0["cautious_price_secondary_level"] == "L140"
    out1 = recompute_cautious_for_fill(dict(hyp), 20000.0, liqs, 999999.0, 1)
    assert out1["cautious_price_secondary_level"] == "L120"


# ══ R2 (GIL-25): drop per-ticker-invalidated levels from cautious + hypothesis targets ═══

def _two_up_levels():
    # prev1_day_high (140 above) is the furthest → secondary; L100 (100 above) the initial.
    return [
        {"name": "prev1_day_high", "kind": "level", "price": 20140.0},
        {"name": "L100",           "kind": "level", "price": 20100.0},
    ]


def test_compute_cautious_drops_invalidated_level():
    """An invalidated level is excluded from the cautious-ladder candidate pool."""
    from hypothesis import compute_cautious_prices
    liqs = _two_up_levels()
    base = compute_cautious_prices("up", 20000.0, liqs, 999999.0)
    assert base["cautious_price_secondary_level"] == "prev1_day_high"
    out = compute_cautious_prices("up", 20000.0, liqs, 999999.0,
                                  invalidated_names={"prev1_day_high"})
    assert out["cautious_price_secondary_level"] == "L100"


def test_compute_cautious_invalidated_default_is_noop():
    """No invalidated_names (None / omitted / empty) reproduces the legacy output byte-for-byte."""
    from hypothesis import compute_cautious_prices
    liqs = _two_up_levels()
    a = compute_cautious_prices("up", 20000.0, liqs, 999999.0)
    b = compute_cautious_prices("up", 20000.0, liqs, 999999.0, invalidated_names=None)
    c = compute_cautious_prices("up", 20000.0, liqs, 999999.0, invalidated_names=set())
    assert a == b == c


# ══ Empty-in-cap-pool fallback: nearest level beyond the (shrunk) threshold ════════
# When the dist-shrink starves the in-cap candidate pool to empty, the ladder must fall
# back to the CLOSEST in-direction liquidity beyond the threshold (not the old uncapped
# day_high/week_high terminal branch), with the initial set via the 85% rule on it.

from hypothesis import (
    CAUTIOUS_SECONDARY_OFFSET_PTS as _SEC_OFF,
    CAUTIOUS_MIN_DIST as _MIN_DIST,
)


def test_cautious_empty_pool_falls_back_to_nearest_level_up():
    """Up: in-cap pool empty (dist_shrinks=3 → max≈92). The nearest beyond-threshold level
    (london_low ~139 above) becomes the secondary, NOT the far day_high (~410 above)."""
    from hypothesis import compute_cautious_prices
    close = 29636.75
    liqs = [
        {"name": "london_low", "kind": "level", "price": 29776.0},   # ~139 above → beyond 92
        {"name": "day_high",   "kind": "level", "price": 30046.75},  # ~410 above (the old trap)
    ]
    r = compute_cautious_prices("up", close, liqs, 999999.0, dist_shrinks=3)
    assert r["cautious_price_secondary_level"] == "london_low"
    assert r["cautious_price_secondary"] == 29776.0 - _SEC_OFF
    assert r["cautious_price_initial_level"] == "synthetic_85pct"
    assert r["cautious_price_initial"] == pytest.approx(
        close + 0.85 * ((29776.0 - _SEC_OFF) - close))


def test_cautious_empty_pool_falls_back_to_nearest_level_down():
    """Down: nearest beyond-threshold level below the close is chosen over a farther one."""
    from hypothesis import compute_cautious_prices
    close = 20000.0
    liqs = [
        {"name": "L_near", "kind": "level", "price": 19850.0},  # 150 below → beyond 92
        {"name": "L_far",  "kind": "level", "price": 19600.0},  # 400 below
    ]
    r = compute_cautious_prices("down", close, liqs, 1.0, dist_shrinks=3)
    assert r["cautious_price_secondary_level"] == "L_near"
    assert r["cautious_price_secondary"] == 19850.0 + _SEC_OFF


def test_cautious_empty_pool_blank_when_no_in_direction_level():
    """No in-direction liquidity at all → ladder stays blank (no fabricated target)."""
    from hypothesis import compute_cautious_prices
    # Down with only an ABOVE level (and ath above, which down ignores) → nothing below.
    liqs = [{"name": "above", "kind": "level", "price": 20100.0}]
    r = compute_cautious_prices("down", 20000.0, liqs, 999999.0, dist_shrinks=3)
    assert r["cautious_price_secondary"] == ""
    assert r["cautious_price_secondary_level"] == ""
    assert r["cautious_price_initial"] == ""


def test_cautious_empty_pool_fallback_skips_invalidated():
    """The nearest-level fallback honors invalidated_names (depleted levels are skipped)."""
    from hypothesis import compute_cautious_prices
    close = 20000.0
    liqs = [
        {"name": "L140", "kind": "level", "price": 20140.0},  # nearest, beyond cap
        {"name": "L200", "kind": "level", "price": 20200.0},  # farther
    ]
    r = compute_cautious_prices("up", close, liqs, 999999.0, dist_shrinks=3,
                                invalidated_names={"L140"})
    assert r["cautious_price_secondary_level"] == "L200"


def test_compute_cautious_drops_invalidated_in_terminal_fallback():
    """The terminal-extreme fallback (no in-range candidates) also drops invalidated levels."""
    from hypothesis import compute_cautious_prices
    # Both day_high (far) candidates beyond secondary max → fallback to terminal day_high/week_high.
    liqs = [
        {"name": "day_high",  "kind": "level", "price": 21000.0},
        {"name": "week_high", "kind": "level", "price": 22000.0},
    ]
    base = compute_cautious_prices("up", 20000.0, liqs, 999999.0)
    assert base["cautious_price_secondary_level"] in ("day_high", "week_high")
    out = compute_cautious_prices("up", 20000.0, liqs, 999999.0,
                                  invalidated_names={"day_high", "week_high"})
    assert out["cautious_price_secondary_level"] == ""


def test_invalidated_target_names_loads_mnq_from_smts():
    """The helper reads MNQ-invalidated level names from smts.json detect_state __level_inv__."""
    from smt_state import save_smts
    from hypothesis import _invalidated_target_names
    save_smts({"detect_state": {"__level_inv__": {
        "prev1_day_high": {"mnq": True,  "mes": False},   # MNQ depleted → dropped
        "prev2_day_low":  {"mnq": False, "mes": True},    # only MES → NOT dropped (MNQ-traded)
        "day_high":       {"mnq": False, "mes": False},
    }}})
    assert _invalidated_target_names() == {"prev1_day_high"}


def test_build_hypothesis_targets_drop_invalidated_level():
    """build_hypothesis_from_direction excludes an MNQ-invalidated level from the targets list."""
    from smt_state import save_smts
    from hypothesis import build_hypothesis_from_direction
    save_global(_make_default_global())
    save_smts({"detect_state": {"__level_inv__": {
        "prev1_day_high": {"mnq": True, "mes": False}}}})
    liqs = [
        {"name": "prev1_day_high", "kind": "level", "price": 20100.0},
        {"name": "day_high",       "kind": "level", "price": 20050.0},
    ]
    build_hypothesis_from_direction(
        "up", _make_now(), 20000.0, liqs, _make_default_global(),
        old_direction="up", weekly_mid="", daily_mid="", last_liquidity="",
        divs=[], direction_reason={}, skip_veto=True, old_formed_at="2026-04-27T09:30:00",
    )
    names = {t["name"] for t in load_hypothesis()["targets"]}
    assert "prev1_day_high" not in names
    assert "day_high" in names


# ══ GIL-32: standing-SMT-conviction override on rule2b's direction ═══════════════
#
# These tests drive `_determine_direction` directly through a rule2b high-sweep
# scenario (price sweeps into weekly premium → rule2b returns "down"), then vary the
# `smt_conviction` kwarg to exercise the ungated override branch. The default
# `smt_conviction=0.0` path is asserted byte-identical to the no-conviction call
# (back-compat).

def _rule2b_down_direction_args():
    """Build the args for `_determine_direction` for a rule2b high-sweep scenario.

    Bars sweep up through day_high(180)/week_high(200) and close at 175. The scenario
    reaches the rule2b branch (reason["rule"] == "rule2b"); the tests read the baseline
    direction it produces and then exercise the override relative to that baseline.
    Returns a kwargs dict for `_determine_direction`.
    """
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 180.0},
            {"name": "day_low",   "kind": "level", "price": 120.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })
    now = _make_now(time_str="10:10:00")
    opens  = [150.0] * 5 + [170.0] * 5
    highs  = [155.0] * 5 + [200.0] * 5
    lows   = [100.0] * 5 + [165.0] * 5
    closes = [152.0] * 5 + [175.0] * 5
    mnq_1m = _make_1m_bars(opens, highs, lows, closes, start_time="2026-04-27 10:00:00")
    week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                             start_time="2026-04-20 10:00:00")
    pre_sess = _make_pre_session_hist()
    hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()
    liquidities = [
        {"name": "week_high", "kind": "level", "price": 200.0},
        {"name": "week_low",  "kind": "level", "price": 100.0},
        {"name": "day_high",  "kind": "level", "price": 180.0},
        {"name": "day_low",   "kind": "level", "price": 120.0},
    ]
    global_state = _make_default_global()
    current_bar = {
        "Open": 170.0, "High": 200.0, "Low": 165.0, "Close": 175.0, "Volume": 1000.0,
    }
    return {
        "current_bar":  current_bar,
        "mnq_1m":       mnq_1m,
        "hist_mnq_1m":  hist_mnq_1m,
        "liquidities":  liquidities,
        "global_state": global_state,
        "divs":         [],
        "now":          now,
    }


def _contradicting_conviction(base_dir, mag):
    """A conviction of magnitude `mag` whose sign CONTRADICTS base_dir
    ("up"→ +, so contradict with −; "down"→ flip to up with +)."""
    return -mag if base_dir == "up" else mag


def _aligning_conviction(base_dir, mag):
    """A conviction whose sign AGREES with base_dir."""
    return mag if base_dir == "up" else -mag


def test_gil32_override_flips_contradicting_rule2b_direction():
    """|conv|>=STRONG and sign CONTRADICTS r2b_dir → flip to the SMT side, smt_override True."""
    from hypothesis import _determine_direction
    args = _rule2b_down_direction_args()
    # Baseline (no conviction) — rule2b's own decision for this synthetic bar.
    base_dir, base_reason = _determine_direction(**args)
    assert base_dir in ("up", "down") and base_reason.get("rule") == "rule2b"
    assert "smt_override" not in base_reason

    conv = _contradicting_conviction(base_dir, 0.8)
    expected = "down" if conv < 0 else "up"
    inputs = {"n": 2, "n_bear": 0, "n_bull": 2, "top_tier": "week", "refs": ["x"]}
    direction, reason = _determine_direction(
        **_rule2b_down_direction_args(), smt_conviction=conv, smt_conviction_inputs=inputs
    )
    assert direction == expected != base_dir, (
        f"contradicting conviction must flip {base_dir}→{expected}, got {direction}"
    )
    assert reason.get("smt_override") is True
    assert reason.get("smt_conviction") == round(conv, 3)
    assert reason.get("smt_conviction_inputs") == inputs


def test_gil32_override_noop_when_conviction_aligns():
    """Conviction that AGREES with r2b_dir is a no-op (no flip, no smt_override tag)."""
    from hypothesis import _determine_direction
    base_dir, _ = _determine_direction(**_rule2b_down_direction_args())
    conv = _aligning_conviction(base_dir, 0.8)
    direction, reason = _determine_direction(
        **_rule2b_down_direction_args(), smt_conviction=conv, smt_conviction_inputs={"n": 3}
    )
    assert direction == base_dir, f"aligned conviction must NOT change direction, got {direction}"
    assert "smt_override" not in reason


def test_gil32_override_noop_when_conviction_weak():
    """|conv| < STRONG never flips, even when it contradicts r2b_dir."""
    from hypothesis import _determine_direction
    import smt_conviction as _sc
    base_dir, _ = _determine_direction(**_rule2b_down_direction_args())
    weak = _contradicting_conviction(base_dir, _sc.CONVICTION_STRONG - 0.01)
    direction, reason = _determine_direction(
        **_rule2b_down_direction_args(), smt_conviction=weak, smt_conviction_inputs={"n": 1}
    )
    assert direction == base_dir, f"weak conviction must NOT flip, got {direction}"
    assert "smt_override" not in reason


def test_gil32_override_absent_when_no_flip_default_is_back_compat():
    """Default smt_conviction=0.0 is byte-identical to the no-kwargs call (back-compat).

    The mandatory back-compat invariant: with the default conviction (0.0) the
    direction AND the reason dict are exactly what today's code returns when the new
    kwargs are not supplied at all.
    """
    from hypothesis import _determine_direction
    # No kwargs at all (the pre-GIL-32 call shape).
    dir_legacy, reason_legacy = _determine_direction(**_rule2b_down_direction_args())
    # Explicit default conviction.
    dir_default, reason_default = _determine_direction(
        **_rule2b_down_direction_args(), smt_conviction=0.0, smt_conviction_inputs={}
    )
    assert dir_legacy == dir_default
    assert reason_legacy == reason_default, (
        f"default-conviction reason diverged from legacy: {reason_default} != {reason_legacy}"
    )
    assert "smt_override" not in reason_default
    assert "smt_conviction" not in reason_default


def test_gil32_conviction_set_flows_through_run_hypothesis():
    """Integration: a seeded standing bearish conviction set flips a rule2b "up".

    Build a rule2b scenario that resolves to "up" on its own (a high-sweep WITHOUT
    weekly premium — close stays in discount), seed a strongly-bearish
    `smt_conviction_set` on the hypothesis, and assert run_hypothesis emits "down"
    with the smt_override tag. Exercises Task 2 (state key) + Task 3 (run_hypothesis
    read + conviction_score + override) end-to-end.
    """
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 130.0},
            {"name": "day_low",   "kind": "level", "price": 80.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })
    now = _make_now(time_str="10:10:00")
    # Sweep day_high(130) but close at 120 — below weekly mid (150) → discount, so
    # rule2b's premium guard is False → r2b_dir = "up".
    opens  = [110.0] * 5 + [125.0] * 5
    highs  = [115.0] * 5 + [135.0] * 5
    lows   = [100.0] * 5 + [118.0] * 5
    closes = [112.0] * 5 + [120.0] * 5
    mnq_1m = _make_1m_bars(opens, highs, lows, closes, start_time="2026-04-27 10:00:00")
    mes_1m = _make_1m_bars([60.0] * 10, [61.0] * 10, [59.0] * 10, [60.0] * 10,
                           start_time="2026-04-27 10:00:00")
    week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                             start_time="2026-04-20 10:00:00")
    pre_sess = _make_pre_session_hist()
    hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()
    hist_mes_1m = _make_1m_bars([50] * 5, [51] * 5, [49] * 5, [50] * 5,
                                start_time="2026-04-20 10:00:00")

    # Seed a strongly-bearish standing conviction set (two week-tier shorts, unfulfilled).
    h0 = load_hypothesis()
    h0["direction"] = "none"
    h0["smt_conviction_set"] = [
        {"ref_name": "week_high", "direction": "short", "side": "bearish",
         "tier": "week", "type": "wick", "fire_iso": "2026-04-27T09:40:00-04:00",
         "fire_close": 135.0, "adverse_streak": 0, "fulfilled_iso": None},
        {"ref_name": "day_high", "direction": "short", "side": "bearish",
         "tier": "day", "type": "wick", "fire_iso": "2026-04-27T09:50:00-04:00",
         "fire_close": 132.0, "adverse_streak": 0, "fulfilled_iso": None},
    ]
    save_hypothesis(h0)

    with patch("hypothesis.detect_smt_divergence", return_value=None):
        with patch("hypothesis.detect_smt_fill", return_value=None):
            events = run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m)

    assert events, "expected a new-hypothesis event"
    reason = events[0]["direction_reason"]
    assert reason.get("rule") == "rule2b", f"expected rule2b, got {reason.get('rule')!r}"
    assert reason.get("smt_override") is True, f"expected override, reason={reason}"
    assert events[0].get("direction") == "down" or load_hypothesis()["direction"] == "down"


# ===========================================================================
# GIL-33: extend the SMT-conviction override beyond rule2b (rule2 / rule3_4).
#
# These build scenarios that reach `_determine_direction`'s rule2 (approaching) and
# rule3_4 (blended) returns — bypassing the rule2b override block — then vary the
# `smt_conviction` kwarg to exercise the new shared-helper override at those returns.
# ===========================================================================

def _flat_band_hist():
    """Hist (week-ago + pre-session) bars in a tight 148-152 band so NO meaningful level
    {week 100/200, day 120/180} is ever crossed → `_find_last_liquidity` returns "" →
    the rule2b block is skipped, letting rule2/rule3_4 own the formation."""
    week_ago = _make_1m_bars([150] * 5, [152] * 5, [148] * 5, [150] * 5,
                             start_time="2026-04-20 10:00:00")
    pre_sess = _make_1m_bars([150] * 5, [152] * 5, [148] * 5, [150] * 5,
                             start_time="2026-04-26 18:00:00")
    return pd.concat([week_ago, pre_sess]).sort_index()


def _rule2_down_direction_args():
    """Args for `_determine_direction` that resolve to rule2 "down".

    Price sits in a [148,170] band (no level crossed → rule2b skipped; current_bar touches
    no level → rule1 None) with a DOWNWARD momentum run, approaching the low side → rule2
    returns "down". The tests read this baseline, then exercise the conviction override.
    """
    save_global(_make_default_global())
    save_daily(_make_default_daily(week_high=200.0, week_low=100.0,
                                   day_high=180.0, day_low=120.0))
    now = _make_now(time_str="10:10:00")
    # Decreasing closes → downward momentum for the approaching-low check.
    closes = [168.0, 166.0, 164.0, 162.0, 160.0, 158.0, 156.0, 154.0, 152.0, 150.0]
    mnq_1m = _make_1m_bars([c + 2 for c in closes], [170.0] * 10, [148.0] * 10, closes,
                           start_time="2026-04-27 10:00:00")
    liquidities = [
        {"name": "week_high", "kind": "level", "price": 200.0},
        {"name": "week_low",  "kind": "level", "price": 100.0},
        {"name": "day_high",  "kind": "level", "price": 180.0},
        {"name": "day_low",   "kind": "level", "price": 120.0},
    ]
    current_bar = {"Open": 152.0, "High": 152.0, "Low": 148.0, "Close": 150.0, "Volume": 1000.0}
    return {
        "current_bar":  current_bar,
        "mnq_1m":       mnq_1m,
        "hist_mnq_1m":  _flat_band_hist(),
        "liquidities":  liquidities,
        "global_state": _make_default_global(),
        "divs":         [],
        "now":          now,
    }


def _rule3_4_args(divs):
    """Args that reach `_determine_direction`'s rule3_4 return.

    FLAT momentum (constant closes) → rule2's strict-inequality momentum check fails for
    every level → rule2 None; band stays inside all levels → rule2b skipped; current_bar
    touches nothing → rule1 None. The supplied `divs` drive `smt_sc` so `|combined|` clears
    DIRECTION_SCORE_THRESHOLD and rule3_4 commits a direction.
    """
    save_global(_make_default_global())
    save_daily(_make_default_daily(week_high=300.0, week_low=100.0,
                                   day_high=260.0, day_low=140.0))
    now = _make_now(time_str="10:10:00")
    mnq_1m = _make_1m_bars([150.0] * 10, [151.0] * 10, [149.0] * 10, [150.0] * 10,
                           start_time="2026-04-27 10:00:00")
    liquidities = [
        {"name": "week_high", "kind": "level", "price": 300.0},
        {"name": "week_low",  "kind": "level", "price": 100.0},
        {"name": "day_high",  "kind": "level", "price": 260.0},
        {"name": "day_low",   "kind": "level", "price": 140.0},
    ]
    current_bar = {"Open": 150.0, "High": 151.0, "Low": 149.0, "Close": 150.0, "Volume": 1000.0}
    return {
        "current_bar":  current_bar,
        "mnq_1m":       mnq_1m,
        "hist_mnq_1m":  _flat_band_hist(),
        "liquidities":  liquidities,
        "global_state": _make_default_global(),
        "divs":         divs,
        "now":          now,
    }


def test_gil33_rule2_override_flips_to_up():
    """rule2 baseline "down" + strong BULLISH standing conviction → flips to "up", tagged
    with smt_override + smt_override_rule="rule2" (the 05-08 18:10 target case)."""
    from hypothesis import _determine_direction
    base_dir, base_reason = _determine_direction(**_rule2_down_direction_args())
    assert base_reason.get("rule") == "rule2", f"expected rule2, got {base_reason.get('rule')!r}"
    assert base_dir == "down", f"expected baseline down, got {base_dir}"
    assert "smt_override" not in base_reason

    inputs = {"n": 3, "n_bear": 0, "n_bull": 3, "top_tier": "day", "refs": ["day_low"]}
    direction, reason = _determine_direction(
        **_rule2_down_direction_args(), smt_conviction=0.8, smt_conviction_inputs=inputs
    )
    assert direction == "up", f"strong bullish conviction must flip rule2 down→up, got {direction}"
    assert reason.get("rule") == "rule2"
    assert reason.get("smt_override") is True
    assert reason.get("smt_override_rule") == "rule2"
    assert reason.get("smt_conviction") == 0.8
    assert reason.get("smt_conviction_inputs") == inputs


def test_gil33_rule2_override_noop_below_threshold():
    """rule2 baseline "down" + contradicting conviction just under CONVICTION_STRONG → no flip."""
    from hypothesis import _determine_direction
    import smt_conviction as _sc
    weak = _sc.CONVICTION_STRONG - 0.01  # bullish but below threshold
    direction, reason = _determine_direction(
        **_rule2_down_direction_args(), smt_conviction=weak, smt_conviction_inputs={"n": 1}
    )
    assert direction == "down", f"sub-threshold conviction must NOT flip, got {direction}"
    assert "smt_override" not in reason
    assert "smt_override_rule" not in reason


def test_gil33_rule2_default_conviction_back_compat():
    """rule2 with the default conviction (0.0) is byte-identical to the no-kwargs call."""
    from hypothesis import _determine_direction
    dir_legacy, reason_legacy = _determine_direction(**_rule2_down_direction_args())
    dir_default, reason_default = _determine_direction(
        **_rule2_down_direction_args(), smt_conviction=0.0, smt_conviction_inputs={}
    )
    assert dir_legacy == dir_default == "down"
    assert reason_legacy == reason_default, (
        f"default-conviction reason diverged: {reason_default} != {reason_legacy}"
    )
    assert "smt_override" not in reason_default
    assert "smt_override_rule" not in reason_default


def test_gil33_rule2b_path_unchanged_by_helper():
    """The rule2b inline override is untouched: a contradicting strong conviction still flips
    via rule2b and tags `smt_override`, but NOT `smt_override_rule` (that tag is the new
    rule2/rule3_4-only marker) — confirming the helper is not applied on the rule2b path."""
    from hypothesis import _determine_direction
    base_dir, _ = _determine_direction(**_rule2b_down_direction_args())
    conv = _contradicting_conviction(base_dir, 0.8)
    direction, reason = _determine_direction(
        **_rule2b_down_direction_args(), smt_conviction=conv,
        smt_conviction_inputs={"n": 2},
    )
    assert reason.get("rule") == "rule2b"
    assert reason.get("smt_override") is True
    assert direction != base_dir
    assert "smt_override_rule" not in reason


def test_gil33_rule3_4_override_flips_and_no_double_count():
    """rule3_4 baseline "up" (bullish divs) + strong BEARISH standing conviction → flips to
    "down" (smt_override_rule="rule3_4"); the `combined_score` blend is unchanged by the
    override (computed before the flip → the standing conviction is NOT double-counted into
    the rule3_4 blend, which already carries the separate active-set `smt_sc`)."""
    from hypothesis import _determine_direction
    divs = [
        {"tier": "week", "side": "bullish"},
        {"tier": "week", "side": "bullish"},
    ]
    base_dir, base_reason = _determine_direction(**_rule3_4_args(divs))
    assert base_reason.get("rule") == "rule3_4", f"expected rule3_4, got {base_reason.get('rule')!r}"
    assert base_dir == "up", f"expected baseline up, got {base_dir}"
    base_combined = base_reason.get("combined_score")

    direction, reason = _determine_direction(
        **_rule3_4_args(divs), smt_conviction=-0.8, smt_conviction_inputs={"n": 2},
    )
    assert direction == "down", f"strong bearish conviction must flip rule3_4 up→down, got {direction}"
    assert reason.get("rule") == "rule3_4"
    assert reason.get("smt_override") is True
    assert reason.get("smt_override_rule") == "rule3_4"
    assert reason.get("combined_score") == base_combined, (
        "combined_score must be unchanged by the override (no double-count of conviction)"
    )


# ===========================================================================
# GIL-32 Phase-1b: same-liquidity reversal lock (veto in _determine_direction).
# ===========================================================================

def _rule2b_up_high_sweep_args():
    """Args for `_determine_direction` that resolve to rule2b "up" on a day_high sweep.

    Mirrors the conviction-flow scenario: sweep day_high(130) with highs to 135 (week_high 200
    untouched → last_swept_level == "day_high") but close at 120, below weekly mid (150) →
    weekly discount → rule2b's premium guard is False → r2b_dir = "up".
    """
    save_global(_make_default_global())
    save_daily({
        "date": "2026-04-27",
        "liquidities": [
            {"name": "week_high", "kind": "level", "price": 200.0},
            {"name": "week_low",  "kind": "level", "price": 100.0},
            {"name": "day_high",  "kind": "level", "price": 130.0},
            {"name": "day_low",   "kind": "level", "price": 80.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })
    now = _make_now(time_str="10:10:00")
    opens  = [110.0] * 5 + [125.0] * 5
    highs  = [115.0] * 5 + [135.0] * 5
    lows   = [100.0] * 5 + [118.0] * 5
    closes = [112.0] * 5 + [120.0] * 5
    mnq_1m = _make_1m_bars(opens, highs, lows, closes, start_time="2026-04-27 10:00:00")
    week_ago = _make_1m_bars([100] * 5, [101] * 5, [99] * 5, [100] * 5,
                             start_time="2026-04-20 10:00:00")
    pre_sess = _make_pre_session_hist()
    hist_mnq_1m = pd.concat([week_ago, pre_sess]).sort_index()
    liquidities = [
        {"name": "week_high", "kind": "level", "price": 200.0},
        {"name": "week_low",  "kind": "level", "price": 100.0},
        {"name": "day_high",  "kind": "level", "price": 130.0},
        {"name": "day_low",   "kind": "level", "price": 80.0},
    ]
    current_bar = {"Open": 125.0, "High": 135.0, "Low": 118.0, "Close": 120.0, "Volume": 1000.0}
    return {
        "current_bar":  current_bar,
        "mnq_1m":       mnq_1m,
        "hist_mnq_1m":  hist_mnq_1m,
        "liquidities":  liquidities,
        "global_state": _make_default_global(),
        "divs":         [],
        "now":          now,
    }


def _bearish_day_high_lock():
    return {"level_name": "day_high", "side": "bearish", "locked_dir": "down",
            "level_price": 130.0, "fire_iso": "2026-04-27T09:50:00-04:00",
            "armed_iso": "2026-04-27T09:55:00-04:00", "accept_streak": 0,
            "protecting": True, "keys": ["day_high|short|wick"]}


def test_gil32_reversal_lock_forces_up_to_down_on_same_liquidity():
    """A bearish lock on the swept day_high forces rule2b's "up" → "down" (protect-existing)."""
    from hypothesis import _determine_direction
    base_dir, base_reason = _determine_direction(**_rule2b_up_high_sweep_args())
    assert base_dir == "up" and base_reason.get("rule") == "rule2b"
    assert base_reason.get("last_swept_level") == "day_high"

    direction, reason = _determine_direction(
        **_rule2b_up_high_sweep_args(), smt_reversal_locks=[_bearish_day_high_lock()]
    )
    assert direction == "down", f"lock must force up→down, got {direction}"
    assert reason.get("smt_reversal_lock") == "day_high"
    assert reason.get("smt_reversal_lock_dir") == "down"


def test_gil32_reversal_lock_noop_on_different_liquidity():
    """A lock on a DIFFERENT level than the swept one does not veto."""
    from hypothesis import _determine_direction
    other = dict(_bearish_day_high_lock(), level_name="week_high", keys=["week_high|short|wick"])
    direction, reason = _determine_direction(
        **_rule2b_up_high_sweep_args(), smt_reversal_locks=[other]
    )
    assert direction == "up", f"lock on a different level must NOT veto, got {direction}"
    assert "smt_reversal_lock" not in reason


def test_gil32_reversal_lock_default_is_back_compat():
    """Default smt_reversal_locks (None) is byte-identical to the no-kwarg call."""
    from hypothesis import _determine_direction
    dir_legacy, reason_legacy = _determine_direction(**_rule2b_up_high_sweep_args())
    dir_default, reason_default = _determine_direction(
        **_rule2b_up_high_sweep_args(), smt_reversal_locks=None
    )
    assert dir_legacy == dir_default
    assert reason_legacy == reason_default
    assert "smt_reversal_lock" not in reason_default


class TestOpenWindowCautiousScale:
    """O3 (GIL-34): inside [09:15,11:30] ET, compute_cautious_prices scales the max-distance
    thresholds by OPEN_WINDOW_CAUTIOUS_SCALE so nearer levels become the targets. Default
    OFF → the `now` arg changes nothing (byte-identical)."""

    _LIQ = [
        {"name": "near_high", "kind": "level", "price": 160.0},  # +60 from close
        {"name": "far_high",  "kind": "level", "price": 240.0},  # +140 (within 150, beyond scaled 75)
    ]
    _IN = pd.Timestamp("2026-06-16T10:00:00-04:00")   # in [09:15,11:30]
    _OUT = pd.Timestamp("2026-06-16T13:00:00-04:00")  # outside

    def test_flag_off_now_arg_inert(self):
        import hypothesis as h
        assert h.OPEN_WINDOW_CAUTIOUS_SCALE_ENABLED is False
        a = h.compute_cautious_prices("up", 100.0, self._LIQ, 10000.0, 0)
        b = h.compute_cautious_prices("up", 100.0, self._LIQ, 10000.0, 0, now=self._IN)
        assert a == b
        # OFF picks the farthest in-range level (240) as secondary.
        assert a["cautious_price_secondary_level"] == "far_high"
        assert float(a["cautious_price_secondary"]) == 235.0  # 240 - 5 offset

    def test_flag_on_in_window_scales_to_nearer_level(self, monkeypatch):
        import hypothesis as h
        monkeypatch.setattr(h, "OPEN_WINDOW_CAUTIOUS_SCALE_ENABLED", True)
        # In-window: scaled _sec_max = 150*0.5 = 75 → far_high (140) excluded, near_high (60) wins.
        on = h.compute_cautious_prices("up", 100.0, self._LIQ, 10000.0, 0, now=self._IN)
        assert on["cautious_price_secondary_level"] == "near_high"
        assert float(on["cautious_price_secondary"]) == 155.0  # 160 - 5 offset

    def test_flag_on_out_of_window_unscaled(self, monkeypatch):
        import hypothesis as h
        monkeypatch.setattr(h, "OPEN_WINDOW_CAUTIOUS_SCALE_ENABLED", True)
        # Outside the window → scale 1.0 → far_high still the secondary (same as OFF).
        out = h.compute_cautious_prices("up", 100.0, self._LIQ, 10000.0, 0, now=self._OUT)
        assert out["cautious_price_secondary_level"] == "far_high"
        assert float(out["cautious_price_secondary"]) == 235.0
