"""Plan 35: the initial-target stage — selection (v2), the reached rule, the Executor
wiring and the broker mirror.

The Executor cases use the 2026-09-18 numbers (short 29829.5, T2 = TDO 29755, D = 74.5):
under the v2 rule no level on the recorded list is eligible (band 14.9–59.6 pts, floor 42
after the offset), so the stage is the synthetic 85 % fallback at 29766.175 — the same
pick the replay makes (plan §5). They drive `on_bar` with 1m frames and the
binding/mechanism paths stubbed out, so the only order events are the ones the stage
produces. The selection cases are pure; the six reviewed days replay on the level lists
captured by the study harness (`agent/trader/fixtures/initial_target/`).
"""
import importlib.util
import json
import os
import types

import pandas as pd
import pytest

import agent.trader.executor as executor_mod
import agent.trader.target as tgt
from agent.trader.executor import Executor
from agent.trader.initial_target import (EXTREME_MIN, MID_PREFERENCE, MIN_DIST_PTS,
                                         SYNTHETIC_LEVEL, TIER_MID, TIER_PREVN,
                                         TIER_SESSION_EXTREME, TIER_SYNTHETIC,
                                         InitialTargetTracker, select_initial_target,
                                         variant_label)
from agent.trader.order_sim import OrderSim
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-18"

ENTRY, STOP, T2_PRICE = 29829.5, 29854.5, 29755.0
T2 = {"id": "D1", "level": "TDO", "price": T2_PRICE}
LEVELS_0918 = [("day_mid", 29807.38), ("prev1_day_high", 29793.25),
               ("asia_high", 29764.5), ("ny_morning_low", 29764.0),
               ("TDO", 29755.0), ("london_low", 29754.75)]
#: v2 on that list: day_mid is 22 pts out (floor), prev1_day_high / asia_high are the
#: wrong side for a short, ny_morning_low at 65.5 pts is past the 80 % band (59.6), TDO
#: is the secondary itself and london_low is beyond it -> synthetic 85 %.
INITIAL = ENTRY - 0.85 * (ENTRY - T2_PRICE)          # 29766.175
INITIAL_LEVEL = SYNTHETIC_LEVEL

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                           "initial_target")


def _t(hhmm, sec=0):
    return pd.Timestamp(f"{DATE} {hhmm}:{sec:02d}", tz=TZ)


def _bar(o, h, l, c, label=None):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c}, name=label)


# --------------------------------------------------------------------------- #
# selection (v2)                                                                #
# --------------------------------------------------------------------------- #

#: A short from 30000 to T2 29700: D = 300, band 60..240 pts, EXTREME_MIN (A) 150 pts.
P, S = 30000.0, 29700.0


def _sel(levels, direction="DOWN", anchor=P, secondary=S, **kw):
    return select_initial_target(direction, anchor, secondary, levels, **kw)


def test_defaults_are_variant_a_with_session_mid_first():
    assert EXTREME_MIN == ("pts", 150.0) and MID_PREFERENCE == "session_mid_first"
    assert variant_label() == "pts:150|session_mid_first"
    assert variant_label(("frac", 0.65), "nearest") == "frac:0.65|nearest"


def test_side_filter_a_low_is_never_picked_for_a_long_nor_a_high_for_a_short():
    # short: a _high between the fill and T2 is not a candidate even when it is the
    # only structural level; a _low of the same family is.
    assert _sel([("prev2_day_high", 29850.0)])["level"] == SYNTHETIC_LEVEL
    assert _sel([("prev2_day_low", 29850.0)])["level"] == "prev2_day_low"
    assert _sel([("ny_morning(cur)_high", 29800.0)])["level"] == SYNTHETIC_LEVEL
    assert _sel([("ny_morning(cur)_low", 29800.0)])["level"] == "ny_morning(cur)_low"
    # long: mirror
    up = dict(direction="UP", anchor=29700.0, secondary=30000.0)
    assert _sel([("prev2_day_low", 29850.0)], **up)["level"] == SYNTHETIC_LEVEL
    assert _sel([("prev2_day_high", 29850.0)], **up)["level"] == "prev2_day_high"
    assert _sel([("asia(cur)_low", 29900.0)], **up)["level"] == SYNTHETIC_LEVEL
    assert _sel([("asia(cur)_high", 29900.0)], **up)["level"] == "asia(cur)_high"
    # mids and opens go both ways
    for name in ("day_mid", "week_mid", "ny_morning(cur)_mid", "TDO", "TWO"):
        assert _sel([(name, 29850.0)])["level"] == name
        assert _sel([(name, 29850.0)], **up)["level"] == name


def test_swept_depleted_and_nested_levels_are_excluded():
    lv = {"name": "prev2_day_low", "price": 29850.0}
    assert _sel([lv])["level"] == "prev2_day_low"
    assert _sel([dict(lv, swept=True)])["level"] == SYNTHETIC_LEVEL
    assert _sel([dict(lv, depleted=True)])["level"] == SYNTHETIC_LEVEL
    assert _sel([dict(lv, suppressed=True)])["level"] == SYNTHETIC_LEVEL
    # nested via the caller's set (bare pairs carry no flag)
    assert _sel([("prev2_day_low", 29850.0)], suppressed={"prev2_day_low"})["level"] \
        == SYNTHETIC_LEVEL
    assert _sel([("prev2_day_low", 29850.0)], suppressed={"other"})["level"] == "prev2_day_low"
    # an excluded level is not counted as a candidate either
    assert _sel([dict(lv, swept=True)])["n_candidates"] == 0


def test_band_is_20_to_80_percent_of_d():
    # 85 % out, 79 % in (D = 300 -> 255 vs 237 pts)
    assert _sel([("prev1_day_low", P - 255.0)])["level"] == SYNTHETIC_LEVEL
    sel = _sel([("prev1_day_low", P - 237.0)])
    assert sel["level"] == "prev1_day_low" and sel["band"] == [60.0, 240.0]
    # 19 % out, 20 % in
    assert _sel([("day_mid", P - 57.0)])["level"] == SYNTHETIC_LEVEL
    assert _sel([("day_mid", P - 60.0)])["level"] == "day_mid"
    # the boundaries are inclusive
    assert _sel([("day_mid", P - 240.0)])["level"] == "day_mid"


def test_floor_is_tested_after_the_two_point_offset():
    """A 41-pt level would give a 39-pt initial -> not a stage; 42 pts -> 40.0 exactly.
    D = 100 so the band (20..80) does not interfere."""
    anchor, t2 = 30000.0, 29900.0
    assert _sel([("day_mid", anchor - 41.0)], secondary=t2)["level"] == SYNTHETIC_LEVEL
    sel = _sel([("day_mid", anchor - 42.0)], secondary=t2)
    assert sel["level"] == "day_mid"
    assert sel["price"] == pytest.approx(anchor - 40.0)
    assert (anchor - sel["price"]) == pytest.approx(MIN_DIST_PTS)


def test_tier_order_session_extreme_beats_a_nearer_mid_beats_a_farther_prevn():
    levels = [("ny_morning(cur)_low", P - 160.0),      # tier 1 (>= 150 pts)
              ("day_mid", P - 100.0),                  # tier 2, nearer
              ("prev1_day_low", P - 230.0)]            # tier 3, farther
    sel = _sel(levels)
    assert sel["level"] == "ny_morning(cur)_low" and sel["tier"] == TIER_SESSION_EXTREME
    assert sel["price"] == pytest.approx(P - 158.0) and sel["n_candidates"] == 3
    sel = _sel(levels[1:])
    assert sel["level"] == "day_mid" and sel["tier"] == TIER_MID
    sel = _sel(levels[2:])
    assert sel["level"] == "prev1_day_low" and sel["tier"] == TIER_PREVN
    # tier 3 picks the FARTHEST prevN level from the fill
    sel = _sel([("prev1_day_low", P - 100.0), ("prev3_week_low", P - 200.0),
                ("prev2_day_low", P - 150.0)])
    assert sel["level"] == "prev3_week_low"


def test_tier_one_takes_the_most_recent_session_not_the_farthest():
    levels = [("asia(cur)_low", P - 230.0), ("london(cur)_low", P - 200.0),
              ("ny_morning(cur)_low", P - 170.0), ("rth(cur)_low", P - 155.0)]
    assert _sel(levels)["level"] == "rth(cur)_low"
    assert _sel(levels[:3])["level"] == "ny_morning(cur)_low"
    assert _sel(levels[:2])["level"] == "london(cur)_low"
    assert _sel(levels[:1])["level"] == "asia(cur)_low"
    # a (prev1) session or the day extreme is not a tier-1 family
    assert _sel([("asia(prev1)_low", P - 200.0)])["level"] == SYNTHETIC_LEVEL
    assert _sel([("day_low", P - 200.0)])["level"] == SYNTHETIC_LEVEL


def test_extreme_min_a_session_extreme_too_near_falls_through_to_tier_two():
    levels = [("ny_morning(cur)_low", P - 120.0), ("day_mid", P - 100.0)]
    # variant A: 120 < 150 pts -> the mid wins although the extreme is farther
    sel = _sel(levels)
    assert sel["level"] == "day_mid" and sel["tier"] == TIER_MID
    assert sel["n_candidates"] == 2                    # eligible, just not tier 1
    # ... and with nothing else it is the fallback, not the near extreme
    assert _sel(levels[:1])["level"] == SYNTHETIC_LEVEL
    # variant F: 120 / 300 = 40 % < 65 % -> same; at 66 % (198 pts) it qualifies
    assert _sel(levels, extreme_min=("frac", 0.65))["level"] == "day_mid"
    sel = _sel([("ny_morning(cur)_low", P - 198.0), ("day_mid", P - 100.0)],
               extreme_min=("frac", 0.65))
    assert sel["level"] == "ny_morning(cur)_low" and sel["variant"] == "frac:0.65|session_mid_first"


def test_mid_preference_nearest_farthest_and_session_mid_first():
    mids = [("ny_morning(cur)_mid", P - 90.0), ("week_mid", P - 150.0), ("day_mid", P - 200.0)]
    assert _sel(mids, mid_preference="nearest")["level"] == "ny_morning(cur)_mid"
    assert _sel(mids, mid_preference="farthest")["level"] == "day_mid"
    assert _sel(mids, mid_preference="session_mid_first")["level"] == "ny_morning(cur)_mid"
    # session_mid_first without an eligible session mid: the farthest of the rest
    assert _sel(mids[1:], mid_preference="session_mid_first")["level"] == "day_mid"
    assert _sel(mids[1:], mid_preference="nearest")["level"] == "week_mid"
    # a session mid OUTSIDE the band does not count as "first"
    assert _sel([("ny_morning(cur)_mid", P - 250.0)] + mids[1:],
                mid_preference="session_mid_first")["level"] == "day_mid"
    assert _sel(mids)["variant"] == variant_label()


def test_fallback_and_none_when_the_secondary_is_too_close_for_a_stage():
    sel = _sel([])
    assert sel["level"] == SYNTHETIC_LEVEL and sel["tier"] == TIER_SYNTHETIC
    assert sel["price"] == pytest.approx(P - 255.0) and sel["level_price"] is None
    # 0.85 x D - 2 >= 40  <=>  D >= 49.41: 49.5 away is a stage, 49.0 is not
    assert _sel([], secondary=P - 49.5) is not None
    assert _sel([], secondary=P - 49.0) is None
    # a near level cannot rescue it either
    assert _sel([("day_mid", P - 20.0)], secondary=P - 45.0) is None
    # no secondary / wrong side of the fill / missing anchor: no stage, no raise
    assert _sel(LEVELS_0918, secondary=None) is None
    assert _sel(LEVELS_0918, secondary=P + 100.0) is None
    assert _sel(LEVELS_0918, anchor=None) is None


def test_long_side_mirror():
    entry, t2 = 29700.0, 30000.0
    levels = [("day_mid", 29800.0), ("asia(cur)_low", 29900.0), ("london(cur)_high", 29880.0),
              ("prev1_day_high", 29920.0), ("TDO", 30000.0), ("above", 30050.0)]
    sel = _sel(levels, direction="UP", anchor=entry, secondary=t2)
    assert sel["level"] == "london(cur)_high"                  # tier 1, 180 pts
    assert sel["price"] == pytest.approx(29878.0)              # 2 pts toward the fill
    assert _sel([], direction="UP", anchor=entry, secondary=entry + 45.0) is None
    assert _sel([], direction="UP", anchor=entry, secondary=entry + 60.0)["price"] \
        == pytest.approx(entry + 51.0)


def test_attempts_are_echoed_but_no_longer_shrink_anything():
    levels = [("day_mid", P - 200.0)]
    a0 = _sel(levels, attempts_used=0)
    a3 = _sel(levels, attempts_used=3)
    assert a0["level"] == a3["level"] == "day_mid" and a0["price"] == a3["price"]
    assert a0["attempts_used"] == 0 and a3["attempts_used"] == 3
    assert "max_dist" not in a0


def test_invalid_knobs_raise_instead_of_selecting_by_some_other_rule():
    with pytest.raises(ValueError):
        _sel([], mid_preference="closest")
    with pytest.raises(ValueError):
        _sel([], extreme_min=("pct", 0.5))
    with pytest.raises(ValueError):
        _sel([], extreme_min=150.0)


def test_dict_levels_junk_and_wrong_side_of_the_fill_are_ignored():
    levels = [{"name": "day_mid", "price": P - 100.0},
              {"name": "above_entry", "price": P + 50.0},
              {"name": "beyond_t2", "price": S - 10.0},
              {"name": "junk", "price": None}, {"name": "nan", "price": float("nan")},
              ("short",), None]
    sel = _sel(levels)
    assert sel["level"] == "day_mid" and sel["n_candidates"] == 1


# --------------------------------------------------------------------------- #
# the six reviewed days, on their captured level lists                          #
# --------------------------------------------------------------------------- #

def _fixture(date):
    with open(os.path.join(FIXTURE_DIR, f"{date}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _run_fixture(date, **kw):
    fx = _fixture(date)
    return fx, select_initial_target(fx["direction"], fx["anchor"], fx["secondary"],
                                     fx["levels"], **kw)


#: (date, direction, anchor, secondary, level, level_price, tier) under the default
#: variant A + session_mid_first — the plan §2.3 table. The fixtures were captured with
#: `now` = the entry bar's 1m LABEL on 1m frames, so `ny_morning(cur)_*` and the
#: `ny_morning(cur)_mid` prices embed the entry minute's full range (a live 1s fill sees
#: only the seconds up to the fill); `rth(cur)_*` excludes that minute in both.
REVIEWED = [
    ("2026-09-01", "UP", 29040.0, 29486.25, "ny_morning(cur)_high", 29258.0, TIER_SESSION_EXTREME),
    ("2026-08-27", "UP", 29424.0, 29661.25, "rth(cur)_high", 29605.0, TIER_SESSION_EXTREME),
    ("2026-08-12", "DOWN", 30001.5, 29694.5, "ny_morning(cur)_mid", 29873.25, TIER_MID),
    ("2026-09-08", "DOWN", 29686.75, 29478.5, "ny_morning(cur)_low", 29534.0, TIER_SESSION_EXTREME),
    ("2026-09-15", "DOWN", 29452.75, 29231.25, "ny_morning(cur)_mid", 29407.875, TIER_MID),
    ("2026-08-25", "DOWN", 29416.0, 29164.0, "ny_morning(cur)_mid", 29347.625, TIER_MID),
]


@pytest.mark.parametrize("date,direction,anchor,secondary,level,level_price,tier", REVIEWED)
def test_reviewed_day_selects_as_the_plan_table_says(date, direction, anchor, secondary,
                                                     level, level_price, tier):
    fx, sel = _run_fixture(date)
    assert (fx["direction"], fx["anchor"], fx["secondary"]) == (direction, anchor, secondary)
    assert sel is not None
    assert sel["level"] == level and sel["tier"] == tier
    assert sel["level_price"] == pytest.approx(level_price)
    side = -1 if direction == "DOWN" else 1
    assert sel["price"] == pytest.approx(level_price - side * 2.0)
    assert sel["variant"] == "pts:150|session_mid_first"


def test_0827_the_ny_morning_high_is_out_of_band_so_the_rth_high_is_the_stage():
    fx, sel = _run_fixture("2026-08-27")
    names = {lv["name"]: lv["price"] for lv in fx["levels"]}
    d = fx["secondary"] - fx["anchor"]
    assert (names["ny_morning(cur)_high"] - fx["anchor"]) / d > 0.80        # 87 %
    assert 0.65 < (names["rth(cur)_high"] - fx["anchor"]) / d < 0.80        # 76 %
    assert sel["level"] == "rth(cur)_high"


def test_0812_no_rth_extreme_at_a_0930_fill_and_the_ny_morning_low_is_out_of_band():
    fx, sel = _run_fixture("2026-08-12")
    names = {lv["name"] for lv in fx["levels"]}
    assert fx["entry_time"] == "09:30" and not any(n.startswith("rth(cur)") for n in names)
    prices = {lv["name"]: lv["price"] for lv in fx["levels"]}
    assert (fx["anchor"] - prices["ny_morning(cur)_low"]) / (fx["anchor"] - fx["secondary"]) > 0.80
    assert sel["level"] == "ny_morning(cur)_mid" and sel["price"] == pytest.approx(29875.25)
    # "farthest" would take week_mid (77 %), farther than the operator's day_mid (63 %)
    _fx, far = _run_fixture("2026-08-12", mid_preference="farthest")
    assert far["level"] == "week_mid"


def test_0908_the_most_recent_session_beats_the_asia_low_even_when_it_is_not_nested():
    fx, sel = _run_fixture("2026-09-08")
    assert sel["level"] == "ny_morning(cur)_low"
    asia = next(lv for lv in fx["levels"] if lv["name"] == "asia(cur)_low")
    assert asia["price"] == 29521.75 and asia["suppressed"] is True
    # un-nest it: decision (b) still prefers the NY-morning low (most recent session)
    levels = [dict(lv, suppressed=False, swept=False, depleted=False) for lv in fx["levels"]]
    sel2 = select_initial_target(fx["direction"], fx["anchor"], fx["secondary"], levels)
    assert sel2["level"] == "ny_morning(cur)_low" and sel2["level_price"] == 29534.0


def test_0915_the_ny_morning_low_is_excluded_by_extreme_min_under_both_variants():
    fx, sel = _run_fixture("2026-09-15")
    lo = next(lv["price"] for lv in fx["levels"] if lv["name"] == "ny_morning(cur)_low")
    along = fx["anchor"] - lo
    assert lo == 29331.5 and along == pytest.approx(121.25)          # < 150 pts, 55 % < 65 %
    assert sel["level"] == "ny_morning(cur)_mid" and sel["price"] == pytest.approx(29409.875)
    _fx, f = _run_fixture("2026-09-15", extreme_min=("frac", 0.65))
    assert f["level"] == "ny_morning(cur)_mid"
    _fx, far = _run_fixture("2026-09-15", mid_preference="farthest")
    assert far["level"] == "day_mid" and far["level_price"] == 29363.5


def test_0825_the_0930_low_is_excluded_and_session_mid_first_takes_the_ny_morning_mid():
    fx, sel = _run_fixture("2026-08-25")
    prices = {lv["name"]: lv["price"] for lv in fx["levels"]}
    assert prices["ny_morning(cur)_low"] == prices["rth(cur)_low"] == 29279.25    # 136.75 pts
    assert sel["level"] == "ny_morning(cur)_mid" and sel["price"] == pytest.approx(29349.625)
    _fx, far = _run_fixture("2026-08-25", mid_preference="farthest")
    assert far["level"] == "day_mid" and far["level_price"] == 29218.38   # the operator's read


def test_0901_falls_to_tier_two_under_variant_f():
    _fx, a = _run_fixture("2026-09-01")
    assert a["level"] == "ny_morning(cur)_high" and a["tier"] == TIER_SESSION_EXTREME
    _fx, f = _run_fixture("2026-09-01", extreme_min=("frac", 0.65))
    assert f["tier"] == TIER_MID and f["level"] == "ny_morning(cur)_mid"
    assert f["level_price"] == 29149.0 and f["variant"] == "frac:0.65|session_mid_first"
    _fx, ff = _run_fixture("2026-09-01", extreme_min=("frac", 0.65), mid_preference="farthest")
    assert ff["level"] == "day_mid" and ff["level_price"] == 29313.0


# --------------------------------------------------------------------------- #
# level_universe: the v2 families                                               #
# --------------------------------------------------------------------------- #

def _minute_frame(start="06:00", end="09:45", base=29800.0):
    """1m bars 06:00..end whose High/Low step so each family's extreme is distinct:
    bar k has High = base + k, Low = base - k (k from 0)."""
    rows, idx = [], []
    ts, k = _t(start), 0
    while ts <= _t(end):
        rows.append({"Open": base, "High": base + k, "Low": base - k, "Close": base, "Volume": 1.0})
        idx.append(ts)
        ts += pd.Timedelta(minutes=1)
        k += 1
    return pd.DataFrame(rows, index=idx)


def test_rth_running_excludes_the_entry_bar_and_emits_nothing_before_0931():
    frame = _minute_frame()
    # at 09:45:30 the completed RTH bars are 09:30..09:44 (k = 210..224)
    lv = {d["name"]: d["price"] for d in tgt._rth_running(frame, _t("09:45", 30))}
    assert lv == {"rth(cur)_high": 29800.0 + 224, "rth(cur)_low": 29800.0 - 224}
    # on the minute, the bar that just closed counts; the study's `now = bar label`
    lv = {d["name"]: d["price"] for d in tgt._rth_running(frame, _t("09:45"))}
    assert lv["rth(cur)_high"] == 29800.0 + 224
    # nothing before the first RTH bar has closed
    assert tgt._rth_running(frame, _t("09:30")) == []
    assert tgt._rth_running(frame, _t("09:30", 45)) == []
    assert tgt._rth_running(frame, _t("09:20")) == []
    assert tgt._rth_running(frame, _t("09:31")) != []


def test_ny_morning_mid_is_the_running_block_midpoint():
    frame = _minute_frame()
    # symmetric bars: the mid is the base whatever the window; make it asymmetric
    frame.loc[_t("09:10"), "High"] = 30100.0
    out = tgt._ny_morning_mid(frame, _t("09:45"))
    assert len(out) == 1 and out[0]["name"] == "ny_morning(cur)_mid"
    lo = float(frame.loc[_t("06:00"):_t("09:45"), "Low"].min())
    assert out[0]["price"] == pytest.approx((30100.0 + lo) / 2.0)
    # before the block opens: nothing; before the asymmetric bar: the symmetric mid
    assert tgt._ny_morning_mid(frame, _t("05:59")) == []
    assert tgt._ny_morning_mid(frame, _t("09:05"))[0]["price"] == pytest.approx(29800.0)


def test_level_universe_carries_the_suppressed_flag_and_the_v2_families(monkeypatch):
    stub = types.SimpleNamespace(
        levels={"MNQ": {"prev7_day_low": (29700.0, None, "below", "day", None),
                        "prev1_day_low": (29650.0, None, "below", "day", None),
                        "TDO": (29750.0, None, None, "session", None)}},
        suppressed_p1_levels={"MNQ": {"prev7_day_low"}},
        day_hi={"MNQ": 30024.0}, day_lo={"MNQ": 29576.0}, day_mid=29800.0,
        week_hi={}, week_lo={}, weekly_mid=None)
    monkeypatch.setattr(tgt, "build_bundle", lambda bars, now: stub)
    monkeypatch.setattr(tgt, "facts_to_validator_dict",
                        lambda b: {"levels": {"prev1_day_low": {"swept": True}}})
    frame = _minute_frame()
    out = tgt.level_universe({"MNQ": frame, "MES": frame}, _t("09:45", 30), "MNQ")
    by = {d["name"]: d for d in out}
    assert by["prev7_day_low"]["suppressed"] is True and by["prev1_day_low"]["suppressed"] is False
    assert by["prev1_day_low"]["swept"] is True and by["TDO"]["swept"] is False
    assert all("suppressed" in d for d in out)
    assert by["rth(cur)_high"]["price"] == 29800.0 + 224
    assert by["ny_morning(cur)_mid"]["price"] == pytest.approx(29800.0)
    assert by["ny_morning(cur)_high"]["price"] == 29800.0 + 225      # the running block incl. 09:45
    assert by["day_mid"]["price"] == 29800.0


# --------------------------------------------------------------------------- #
# the reached rule                                                              #
# --------------------------------------------------------------------------- #

def test_touch_without_close_is_not_reached_and_close_beyond_is():
    tr = InitialTargetTracker("DOWN", INITIAL, level=INITIAL_LEVEL)
    assert tr.on_bar_close(_bar(29790.0, 29793.0, 29766.0, 29777.25, _t("09:59"))) is None
    assert tr.on_bar_close(_bar(29777.0, 29779.0, 29765.5, 29775.0, _t("10:00"))) is None
    assert tr.reached is False
    ev = tr.on_bar_close(_bar(29775.0, 29776.0, 29751.5, 29751.5, _t("10:01")))
    assert ev is not None and ev["price"] == INITIAL and ev["bar"] == _t("10:01")
    assert tr.reached and tr.reached_at == _t("10:01")


def test_close_beyond_without_a_touch_cannot_happen_but_a_touch_is_still_required():
    """A long: high >= initial AND close > initial. A bar closing above without its high
    reaching (impossible on real OHLC, but the two tests are independent) stays False."""
    tr = InitialTargetTracker("UP", 29763.5)
    assert tr.on_bar_close(_bar(29750.0, 29763.0, 29745.0, 29762.0)) is None
    assert tr.on_bar_close(_bar(29760.0, 29770.0, 29755.0, 29768.0)) is not None


def test_same_bar_stop_touch_wins_over_close_beyond():
    tr = InitialTargetTracker("DOWN", INITIAL)
    bar = _bar(29775.0, STOP, 29751.5, 29751.5, _t("10:01"))     # touches the stop too
    assert tr.on_bar_close(bar, stop=STOP) is None
    assert tr.reached is False and tr.stopped is True
    # and it stays dead: a later clean bar does not flip it
    assert tr.on_bar_close(_bar(29760.0, 29761.0, 29740.0, 29741.0), stop=STOP) is None


def test_the_flip_is_one_way():
    tr = InitialTargetTracker("DOWN", INITIAL)
    assert tr.on_bar_close(_bar(29775.0, 29776.0, 29760.0, 29761.0, _t("10:01"))) is not None
    # back above the initial: no un-flip, no second event
    assert tr.on_bar_close(_bar(29761.0, 29790.0, 29760.0, 29789.0, _t("10:02"))) is None
    assert tr.reached is True
    assert tr.on_bar_close(_bar(29789.0, 29790.0, 29750.0, 29751.0, _t("10:03"))) is None


def test_post_flip_reports_each_counterfactual_once_and_never_on_the_flip_bar():
    tr = InitialTargetTracker("DOWN", INITIAL)
    flip = _bar(29775.0, 29776.0, 29760.0, 29761.0, _t("10:01"))
    assert tr.post_flip(flip) == []                                # before the flip
    tr.on_bar_close(flip)
    assert tr.post_flip(flip) == []                                # the flip bar itself
    # 10:02: touches the initial from below (A's stop) AND closes up (B's exit)
    evs = tr.post_flip(_bar(29761.0, 29768.0, 29758.0, 29764.0, _t("10:02")))
    assert [e["kind"] for e in evs] == ["cf_stop", "cf_opp_close"]
    assert evs[0]["price"] == INITIAL and evs[1]["price"] == 29764.0
    assert tr.post_flip(_bar(29764.0, 29770.0, 29762.0, 29769.0, _t("10:03"))) == []


# --------------------------------------------------------------------------- #
# the Executor                                                                  #
# --------------------------------------------------------------------------- #

PLAN = {"plan_id": "p35", "thesis_id": "t", "direction": "DOWN",
        "dol": {"level": "TDO", "price": T2_PRICE},
        "valid_while": [], "armed_classes": ["fvg_return_continuation"],
        "attempts_used": 0, "blacklist": [], "cooldown_until": None}

FLAT_HI = (29812.0, 29815.0, 29809.0, 29812.0)
FLAT_LO = (29769.0, 29771.0, 29767.0, 29769.0)
#: 09:59 touches without closing beyond, 10:00 again, 10:01 closes beyond (above T2),
#: 10:02 is the first opposite close (and touches the initial from below), 10:03 trades
#: through the initial again.
TAPE = {
    "09:58": (29812.0, 29815.0, 29790.0, 29792.0),
    "09:59": (29792.0, 29810.0, 29766.0, 29777.25),
    "10:00": (29777.0, 29810.0, 29765.5, 29775.0),
    "10:01": (29775.0, 29776.0, 29760.0, 29761.0),
    "10:02": (29761.0, 29768.0, 29758.0, 29764.0),
    "10:03": (29764.0, 29770.0, 29762.0, 29769.0),
}


def _frame(overrides=None):
    rows, idx = [], []
    ts = _t("09:00")
    while ts <= _t("10:15"):
        hhmm = ts.strftime("%H:%M")
        o, h, l, c = (overrides or TAPE).get(hhmm) or (FLAT_HI if ts < _t("09:58") else
                                                         FLAT_LO if ts > _t("10:03")
                                                         else TAPE[hhmm])
        if hhmm == "09:41":
            o, h, l, c = 29812.0, 29836.0, 29812.0, 29830.0
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0})
        idx.append(ts)
        ts += pd.Timedelta(minutes=1)
    return pd.DataFrame(rows, index=idx)


def _recs(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _kinds(tmp_path):
    return [r["kind"] for r in _recs(tmp_path)]


def _executor(tmp_path, monkeypatch, action="record", levels=LEVELS_0918, pick=T2,
              **kw):
    monkeypatch.setattr(executor_mod, "INITIAL_TARGET_ACTION", action)
    monkeypatch.setattr(executor_mod, "select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    monkeypatch.setattr(executor_mod, "level_universe",
                        lambda *a, **k: [{"name": n, "price": p} for n, p in levels])
    ex = Executor(tmp_path, plan=dict(PLAN), arm_ts=_t("09:20"), **kw)
    # Only the stage under test may produce order events: no binding, no §6/§7.
    monkeypatch.setattr(ex, "_rebind_and_guard", lambda now: None)
    monkeypatch.setattr(ex, "_drive_market_mechanisms", lambda now, mnq, bar=None: None)
    monkeypatch.setattr(ex, "_place_on_fresh_retrace", lambda now, opened: None)
    return ex


def _run(ex, frame, start="09:40", end="10:06", fill_at="09:41", hook=None):
    bars = {"MNQ": frame, "MES": frame}
    ts = _t(start)
    while ts <= _t(end):
        if hook is not None:
            hook(ts)
        ex.on_bar(ts, bars)
        if ts == _t(fill_at):
            ex._enter_by_market(ts, {"mechanism": "fvg_return_continuation",
                                     "direction": "DOWN", "price": ENTRY, "stop": STOP,
                                     "gap_id": "g0918"})
        ts += pd.Timedelta(minutes=1)


def test_events_are_recorded_in_order_and_record_changes_no_order(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, action="record")
    _run(ex, _frame())

    kinds = _kinds(tmp_path)
    order = [k for k in kinds if k in ("fill", "target_selected", "initial_target_selected",
                                       "initial_target_reached")]
    assert order == ["fill", "target_selected", "initial_target_selected",
                     "initial_target_reached"]
    sel = next(r for r in _recs(tmp_path) if r["kind"] == "initial_target_selected")
    assert sel["price"] == pytest.approx(INITIAL) and sel["level"] == INITIAL_LEVEL
    assert sel["secondary"] == T2_PRICE and sel["anchor"] == ENTRY
    assert sel["action"] == "record"
    assert sel["tier"] == TIER_SYNTHETIC and sel["variant"] == variant_label()
    assert sel["band"] == [pytest.approx(14.9), pytest.approx(59.6)]
    reached = next(r for r in _recs(tmp_path) if r["kind"] == "initial_target_reached")
    assert reached["bar"].startswith(f"{DATE}T10:01")
    assert reached["price"] == pytest.approx(INITIAL) and reached["position_open"] is True
    # the counterfactuals of A and B are observed on the 10:02 bar, once each
    assert kinds.count("initial_target_cf_stop") == 1
    assert kinds.count("initial_target_cf_opp_close") == 1
    # ... and NOTHING was done: no stop move, no close, position still open, stop as set
    assert "stop_moved" not in kinds and "initial_opp_close" not in kinds
    assert ex._sim.position is not None and ex._sim.position["stop"] == STOP
    assert ex.bind_state()["initial_target"]["reached"] is True


def test_a_structural_pick_records_its_tier_and_level(tmp_path, monkeypatch):
    """day_mid 49.5 pts out (band 14.9..59.6, floor 42): tier 2, initial 29782."""
    ex = _executor(tmp_path, monkeypatch, levels=[("day_mid", 29780.0), ("TDO", T2_PRICE)])
    _run(ex, _frame())
    sel = next(r for r in _recs(tmp_path) if r["kind"] == "initial_target_selected")
    assert sel["level"] == "day_mid" and sel["level_price"] == 29780.0
    assert sel["price"] == pytest.approx(29782.0) and sel["tier"] == TIER_MID
    assert sel["n_candidates"] == 1 and sel["variant"] == variant_label()
    reached = next(r for r in _recs(tmp_path) if r["kind"] == "initial_target_reached")
    assert reached["price"] == pytest.approx(29782.0) and reached["level"] == "day_mid"


def test_not_reached_on_a_touch_alone_before_the_close_beyond(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    _run(ex, _frame(), end="10:01")       # the 10:01 bar is not yet COMPLETED
    assert "initial_target_reached" not in _kinds(tmp_path)
    assert ex.bind_state()["initial_target"]["reached"] is False


def test_no_stage_when_the_menu_has_no_target(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, pick=None)
    _run(ex, _frame())
    sel = [r for r in _recs(tmp_path) if r["kind"] == "initial_target_selected"]
    assert len(sel) == 1 and sel[0]["price"] is None and sel[0]["secondary"] is None
    assert "initial_target_reached" not in _kinds(tmp_path)


def test_action_a_moves_the_stop_exactly_once_and_it_then_protects(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, action="be_structure")
    _run(ex, _frame())
    recs = _recs(tmp_path)
    moved = [r for r in recs if r["kind"] == "stop_moved"]
    assert len(moved) == 1
    assert moved[0]["price"] == pytest.approx(INITIAL)
    assert moved[0]["prev_stop"] == pytest.approx(STOP)
    assert moved[0]["reason"] == "initial_target"
    # the flip is judged on the 10:02 tick (bar 10:01 completed); 10:03 trades through
    # 29766 from below, so the moved stop books an exit AT the initial price — as
    # `stop_out_initial`, a profit-side exit that is NOT a failed attempt.
    outs = [r for r in recs if r["kind"] == "stop_out_initial"]
    assert len(outs) == 1 and outs[0]["price"] == pytest.approx(INITIAL)
    assert outs[0]["time"].startswith(f"{DATE}T10:03")
    assert "stop_out" not in _kinds(tmp_path)
    kinds = _kinds(tmp_path)
    assert kinds.index("initial_target_reached") < kinds.index("stop_moved")         < kinds.index("stop_out_initial")
    assert ex._sim.position is None
    # §8 / §2: no attempt spent, no cooldown seeded, no takeover window opened
    assert int(ex._plan.get("attempts_used") or 0) == 0
    assert ex._sim.last_stop_out is None
    assert ex._tk_window is None


def test_a_stage_whose_exit_bar_is_pending_survives_a_same_minute_refill(tmp_path,
                                                                          monkeypatch):
    """Exit and re-fill inside one minute: the outgoing stage's exit bar (10:01, the
    flip) is still judged once, and the new fill starts its own stage."""
    ex = _executor(tmp_path, monkeypatch)
    tape = dict(TAPE, **{"10:01": (29775.0, 29776.0, 29751.5, 29751.5)})
    frame = _frame(tape)
    bars = {"MNQ": frame, "MES": frame}
    ts = _t("09:40")
    while ts <= _t("10:06"):
        ex.on_bar(ts, bars)
        if ts == _t("09:41"):
            ex._enter_by_market(ts, {"mechanism": "fvg_return_continuation",
                                     "direction": "DOWN", "price": ENTRY, "stop": STOP,
                                     "gap_id": "g1"})
        if ts == _t("10:01"):
            # the 10:01 row already booked the take-profit inside this on_bar call;
            # a second market entry lands in the SAME minute
            assert ex._sim.position is None
            ex._plan["attempts_used"] = 0
            ex._enter_by_market(ts, {"mechanism": "fvg_return_continuation",
                                     "direction": "DOWN", "price": ENTRY, "stop": STOP,
                                     "gap_id": "g2"})
            assert ex._it_pending is not None and ex._it is not None
            assert ex._it is not ex._it_pending
        ts += pd.Timedelta(minutes=1)
    recs = _recs(tmp_path)
    reached = [r for r in recs if r["kind"] == "initial_target_reached"]
    # the outgoing stage flips on its exit bar (position gone); the new stage's own
    # fill bar is the same 10:01 bar, judged over its full minute (position open)
    assert [r["bar"][:16] for r in reached] == [f"{DATE}T10:01"] * 2
    assert [r["position_open"] for r in reached] == [False, True]
    assert ex._it_pending is None and ex._it is not None
    assert [r["kind"] for r in recs].count("initial_target_selected") == 2


def test_bundle_memo_matches_on_identity_not_on_a_reused_id(monkeypatch):
    import agent.trader.target as tgt
    calls = []
    monkeypatch.setattr(tgt, "build_bundle", lambda bars, now: calls.append(id(bars)) or object())
    now = _t("09:41")
    a = {"MNQ": None}
    b1 = tgt._bundle_for(a, now)
    assert tgt._bundle_for(a, now) is b1 and len(calls) == 1          # same dict: memo hit
    b = {"MNQ": None}
    assert tgt._bundle_for(b, now) is not b1 and len(calls) == 2      # other dict: rebuilt
    assert tgt._bundle_for(b, _t("09:42")) is not None and len(calls) == 3


def test_action_b_closes_on_the_first_opposite_close_after_the_flip_and_not_before(
        tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, action="opp_close")
    frame = _frame()
    seen = {}

    def hook(ts):
        seen[ts] = ex._sim.position is not None

    _run(ex, frame, hook=hook)
    recs = _recs(tmp_path)
    closes = [r for r in recs if r["kind"] == "initial_opp_close"]
    assert len(closes) == 1
    # the 10:02 bar (first opposite close after the 10:01 flip) is complete at 10:03
    assert closes[0]["time"].startswith(f"{DATE}T10:03")
    assert seen[_t("10:02")] is True and seen[_t("10:03")] is True   # open going INTO 10:03
    assert ex._sim.position is None
    assert "stop_moved" not in _kinds(tmp_path)
    # the flip bar (10:01 closed DOWN) was not itself an opposite close
    assert not any(r["kind"] == "initial_opp_close" and r["time"] < f"{DATE}T10:03"
                   for r in recs)


def test_same_bar_stop_out_wins_over_the_flip(tmp_path, monkeypatch):
    """The 10:01 bar closes beyond the initial AND trades through the stop: the
    simulator books the stop on that bar, and the stage records no flip."""
    ex = _executor(tmp_path, monkeypatch, action="be_structure")
    tape = dict(TAPE, **{"10:01": (29775.0, STOP + 0.5, 29760.0, 29761.0)})
    _run(ex, _frame(tape))
    kinds = _kinds(tmp_path)
    assert "stop_out" in kinds
    assert "initial_target_reached" not in kinds and "stop_moved" not in kinds
    assert ex.bind_state()["initial_target"]["reached"] is False


def test_the_flip_is_still_recorded_on_the_bar_that_also_reached_the_target(
        tmp_path, monkeypatch):
    """2026-09-18's shape: the 10:01 bar closes below 29766 AND sweeps TDO 29755. The
    take-profit books on the tick; the flip is recorded when the bar completes, with
    the position already gone, and under A nothing moves."""
    ex = _executor(tmp_path, monkeypatch, action="be_structure")
    tape = dict(TAPE, **{"10:01": (29775.0, 29776.0, 29751.5, 29751.5)})
    _run(ex, _frame(tape))
    recs = _recs(tmp_path)
    tp = [r for r in recs if r["kind"] == "take_profit"]
    assert len(tp) == 1 and tp[0]["time"].startswith(f"{DATE}T10:01")
    reached = [r for r in recs if r["kind"] == "initial_target_reached"]
    assert len(reached) == 1 and reached[0]["bar"].startswith(f"{DATE}T10:01")
    assert reached[0]["position_open"] is False
    # the plan died on the target before this was written; the mechanism survives
    assert reached[0]["mechanism"] == "fvg_return_continuation"
    assert "stop_moved" not in _kinds(tmp_path)
    assert ex._it is None                        # nothing left to judge


def test_a_bar_completed_before_an_exit_on_a_later_tick_is_still_judged(tmp_path,
                                                                        monkeypatch):
    """The 10:01 bar flips; the position exits on the 10:02 row (through T2). Reading
    the 10:01 bar happens on the 10:02 call, after that exit — it must still count."""
    ex = _executor(tmp_path, monkeypatch)
    tape = dict(TAPE, **{"10:02": (29761.0, 29762.0, 29750.0, 29752.0)})
    _run(ex, _frame(tape))
    recs = _recs(tmp_path)
    assert [r["kind"] for r in recs if r["kind"] in ("take_profit", "initial_target_reached")] \
        == ["take_profit", "initial_target_reached"]
    reached = next(r for r in recs if r["kind"] == "initial_target_reached")
    assert reached["bar"].startswith(f"{DATE}T10:01")
    assert ex._it is None


# --------------------------------------------------------------------------- #
# the broker mirror                                                             #
# --------------------------------------------------------------------------- #

def test_plain_sim_move_stop_returns_none_with_nothing_open():
    sim = OrderSim()
    assert sim.move_stop(_t("10:02"), INITIAL) is None


def _report_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "report_replay_pnl.py")
    spec = importlib.util.spec_from_file_location("report_replay_pnl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_shows_initial_reached_and_the_counterfactuals_per_trade(tmp_path, monkeypatch):
    """A record-only run: the booked exit is the target on the 10:05 bar; the 10:02 bar
    both touched the initial from below (A would have stopped there) and closed up (B
    would have exited at its close)."""
    ex = _executor(tmp_path, monkeypatch, action="record")
    tape = dict(TAPE, **{"10:05": (29769.0, 29770.0, 29750.0, 29752.0)})
    _run(ex, _frame(tape))
    rep = _report_module()
    s = rep.summarize(str(tmp_path))
    assert s["n_trades"] == 1 and s["n_initial_selected"] == 1 and s["n_initial_reached"] == 1
    t = s["trades"][0]
    assert t["exit_kind"] == "take_profit" and t["points"] == pytest.approx(ENTRY - T2_PRICE)
    assert t["initial"] == pytest.approx(INITIAL) and t["initial_level"] == INITIAL_LEVEL
    assert t["initial_tier"] == TIER_SYNTHETIC
    assert t["initial_reached"].startswith(f"{DATE}T10:01")
    assert t["points_A"] == pytest.approx(ENTRY - INITIAL)
    assert t["points_B"] == pytest.approx(ENTRY - 29764.0)
    assert s["total_pts_A"] == pytest.approx(ENTRY - INITIAL)
    assert s["initial_action"] == "record"
    text = rep.render(s)
    assert f"initial {INITIAL} ({INITIAL_LEVEL}) reached 10:01" in text
    assert "counterfactual A" in text
