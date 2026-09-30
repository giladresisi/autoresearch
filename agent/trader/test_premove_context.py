"""Plan 46 T1 — the pre-move classifier on synthetic frames (`agent/trader/premove_context.py`).

The frames are piecewise-linear 1m paths (High = Low = Close), so every pivot is a
waypoint and every size is exact. D = 2026-09-01; the session opens 2026-08-31 18:00 ET.
"""
import math

import pandas as pd
import pytest

from agent.trader import premove_context as pc

TZ = "America/New_York"
D = pd.Timestamp("2026-09-01", tz=TZ)
NOW = D + pd.Timedelta(hours=9, minutes=20)


def _t(days=0.0, hh=0, mm=0):
    return D + pd.Timedelta(days=days, hours=hh, minutes=mm)


def _history(start_days=16, lo=29000.0, hi=29250.0):
    """12-hourly swings lo/hi from D-start_days to D-2 12:00 (ends on `hi`)."""
    out, t, i = [], _t(-start_days), 0
    while t <= _t(-2, 12):
        out.append((t, lo if i % 2 == 0 else hi))
        t += pd.Timedelta(hours=12)
        i += 1
    return out


def _frame(points, end=None, lower=False):
    """1m bars through the waypoints, linearly interpolated, to `end` (default 10:00)."""
    s = pd.Series([p for _, p in points], index=pd.DatetimeIndex([t for t, _ in points]))
    end = end if end is not None else _t(0, 10, 0)
    grid = pd.date_range(s.index[0], end, freq="1min")
    px = s.reindex(s.index.union(grid)).interpolate(method="time").ffill().reindex(grid)
    df = pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px, "Volume": 1.0})
    if lower:
        df.columns = [c.lower() for c in df.columns]
    return df


# today: flat at 29000 from the prior-session low, rally to 29400 by 09:00
_TODAY = [(_t(0, 4), 29000.0), (_t(0, 9), 29400.0), (_t(0, 9, 59), 29400.0)]


def _unrelated_points():
    return _history() + [(_t(-1, 17), 29000.0)] + _TODAY


def _part_points(low=28800.0):
    return _history() + [(_t(-1, 12), low), (_t(-1, 17), 29000.0)] + _TODAY


def _classify(points, now=NOW, **kw):
    return pc.classify(_frame(points), now, **kw)


# --------------------------------------------------------------------------- #
# happy paths                                                                   #
# --------------------------------------------------------------------------- #

def test_a_fresh_big_leg_after_flat_days_is_UNRELATED():
    c = _classify(_unrelated_points())
    assert c.status == pc.UNRELATED, c.reason
    assert c.leg_direction == "UP"
    assert c.origin == 29000.0 and c.extreme == 29400.0 and c.size == 400.0
    assert c.origin_ts == _t(-1, 18)            # the first bar of the session
    assert c.px_0919 == 29400.0
    assert c.cut_pts == pytest.approx(120.0 * 29400.0 / 29000.0)
    assert c.boundary == NOW


def test_a_bigger_same_direction_move_from_an_earlier_session_makes_it_PART():
    c = _classify(_part_points())
    assert c.status == pc.PART, c.reason
    assert c.big_direction == "UP" and c.big_size == 600.0
    assert c.big_origin_ts == _t(-1, 12)


def test_forced_direction_is_the_opposite_of_the_leg():
    up = _classify(_unrelated_points())
    assert up.forced_direction() == "DOWN"
    assert up.mid_at_boundary() == 29200.0
    down_pts = [(t, 58400.0 - p) for t, p in _unrelated_points()]   # mirror
    dn = _classify(down_pts)
    assert dn.status == pc.UNRELATED and dn.leg_direction == "DOWN"
    assert dn.forced_direction() == "UP"
    # PART / NOT_BIG force nothing
    assert _classify(_part_points()).forced_direction() is None


# --------------------------------------------------------------------------- #
# boundaries                                                                    #
# --------------------------------------------------------------------------- #

def _sized(size, px=30000.0):
    """UNRELATED-shaped day whose leg is exactly `size` and whose 09:19 close is `px`."""
    lo = px - size
    return _history(lo=lo, hi=lo + 250.0) + [(_t(-1, 17), lo), (_t(0, 4), lo),
                                             (_t(0, 9), px), (_t(0, 9, 59), px)]


def test_big_is_inclusive_at_exactly_1_14_pct():
    at = _classify(_sized(342.0))                # 342 / 30000 = 1.14%
    assert at.size_pct == pytest.approx(1.14)
    assert at.size_pct >= 1.14 and at.status != pc.NOT_BIG, at.reason
    below = _classify(_sized(341.75))
    assert below.status == pc.NOT_BIG


def test_a_counter_move_of_exactly_the_cut_starts_a_new_leg():
    idx = pd.date_range(_t(0, 1), periods=4, freq="1min")
    fr = pd.DataFrame({"high": [100.0, 300.0, 300.0, 200.0],
                       "low": [100.0, 300.0, 200.0, 200.0]}, index=idx)
    z = pc.leg_zigzag(fr, 100.0)                  # 300 -> 200 is exactly the cut
    assert z[0] == "DOWN" and z[1] == 300.0
    z2 = pc.leg_zigzag(fr.assign(low=[100.0, 300.0, 200.25, 200.25]), 100.0)
    assert z2[0] == "UP"                          # 99.75 < cut: still the UP leg


def _eighteen_hundred(low_ts):
    """The big-scale leg starts at `low_ts`; today's 1m leg starts later (a 150-pt
    pullback at 03:00 is >= the 1m cut but < the 5m threshold of 0.5 x 350)."""
    return _history() + [(low_ts, 28800.0), (_t(0, 2), 29200.0), (_t(0, 3), 29050.0),
                         (_t(0, 9), 29400.0), (_t(0, 9, 59), 29400.0)]


def test_a_big_scale_leg_starting_at_18_00_today_is_not_PART():
    at = _classify(_eighteen_hundred(_t(-1, 18)))
    assert at.origin == 29050.0 and at.size == 350.0
    assert at.big_origin_ts == _t(-1, 18)
    assert at.status == pc.UNRELATED, at.reason   # strict <: 18:00 is today's session
    before = _classify(_eighteen_hundred(_t(-1, 17, 55)))
    assert before.big_origin_ts == _t(-1, 17, 55)
    assert before.status == pc.PART, before.reason


def test_part_mult_boundary_1_25():
    exactly = _classify(_part_points(low=28900.0))     # big 500 = 1.25 x 400
    assert exactly.big_size == 500.0 and exactly.status == pc.PART
    under = _classify(_part_points(low=28900.25))      # 499.75
    assert under.status == pc.UNRELATED


# --------------------------------------------------------------------------- #
# no lookahead                                                                  #
# --------------------------------------------------------------------------- #

def test_bars_at_or_after_09_20_cannot_change_the_result():
    base = _frame(_unrelated_points())
    wild = base.copy()
    after = wild.index >= NOW
    wild.loc[after, ["High", "Low", "Close"]] = [[35000.0, 20000.0, 20000.0]] * int(after.sum())
    a, b = pc.classify(base, NOW), pc.classify(wild, NOW + pd.Timedelta(minutes=30))
    assert a.to_dict() == b.to_dict()


def test_the_in_progress_09_20_row_is_ignored():
    fr = _frame(_unrelated_points(), end=NOW)
    fr.loc[NOW, ["High", "Low"]] = [29900.0, 28000.0]
    assert pc.classify(fr, NOW + pd.Timedelta(seconds=30)).to_dict() == \
        pc.classify(fr.iloc[:-1], NOW).to_dict()


# --------------------------------------------------------------------------- #
# error paths                                                                   #
# --------------------------------------------------------------------------- #

def test_empty_or_missing_columns_is_UNKNOWN_never_raises():
    assert pc.classify(None, NOW).status == pc.UNKNOWN
    assert pc.classify(pd.DataFrame(), NOW).status == pc.UNKNOWN
    assert pc.classify(_frame(_unrelated_points()).drop(columns=["High"]),
                       NOW).status == pc.UNKNOWN
    assert pc.classify("not a frame", NOW).status == pc.UNKNOWN
    assert pc.classify(_frame(_unrelated_points()), None).status == pc.UNKNOWN


def test_nan_rows():
    fr = _frame(_unrelated_points())
    nan = fr.copy()
    nan.iloc[100:110] = float("nan")
    got = pc.classify(nan, NOW)
    assert got.status == pc.UNRELATED
    assert got.origin == 29000.0 and got.extreme == 29400.0
    nan.loc[_t(0, 9, 19)] = float("nan")          # the 09:19 bar itself is unusable
    assert pc.classify(nan, NOW).status == pc.UNKNOWN


def test_no_09_19_bar_is_UNKNOWN():
    fr = _frame(_unrelated_points()).drop(index=_t(0, 9, 19))
    got = pc.classify(fr, NOW)
    assert got.status == pc.UNKNOWN and "09:19" in got.reason


def test_armed_before_09_20_is_UNKNOWN():
    got = pc.classify(_frame(_unrelated_points()), NOW - pd.Timedelta(seconds=1))
    assert got.status == pc.UNKNOWN and got.reason == "armed before boundary"


# --------------------------------------------------------------------------- #
# validation                                                                    #
# --------------------------------------------------------------------------- #

def test_no_confirmed_leg_is_NO_LEG():
    pts = _history() + [(_t(-1, 17), 29000.0), (_t(0, 9, 59), 29050.0)]
    got = _classify(pts)
    assert got.status == pc.NO_LEG and got.leg_direction is None


def test_not_big_still_carries_the_leg():
    pts = _history() + [(_t(-1, 17), 29000.0), (_t(0, 4), 29000.0), (_t(0, 9), 29200.0),
                        (_t(0, 9, 59), 29200.0)]
    got = _classify(pts)
    assert got.status == pc.NOT_BIG and got.size == 200.0 and got.leg_direction == "UP"
    assert got.forced_direction() is None


def test_short_history_is_UNKNOWN():
    fr = _frame(_unrelated_points())
    got = pc.classify(fr[fr.index >= D - pd.Timedelta(days=7)], NOW)
    assert got.status == pc.UNKNOWN and got.reason.startswith("history")
    assert got.size == 400.0                     # the leg is still reported


def test_fewer_than_three_big_scale_pivots_is_UNKNOWN():
    pts = [(_t(-16), 29000.0)] + _TODAY           # 15 flat days, then today's leg
    got = _classify(pts)
    assert got.status == pc.UNKNOWN and "pivots" in got.reason


def test_lowercase_and_capitalised_columns_agree():
    up = pc.classify(_frame(_unrelated_points()), NOW)
    lo = pc.classify(_frame(_unrelated_points(), lower=True), NOW)
    assert up.to_dict() == lo.to_dict()


def test_duplicate_index_keeps_last():
    fr = _frame(_unrelated_points())
    dup = fr.loc[[_t(0, 9, 5)]].copy()
    dup[["High", "Close"]] = 29500.0
    got = pc.classify(pd.concat([fr, dup]), NOW)
    assert got.extreme == 29500.0 and got.extreme_ts == _t(0, 9, 5)


def test_a_tz_naive_frame_is_read_as_ET():
    fr = _frame(_unrelated_points())
    naive = fr.set_axis(fr.index.tz_localize(None))
    assert pc.classify(naive, NOW).to_dict() == pc.classify(fr, NOW).to_dict()


# --------------------------------------------------------------------------- #
# params / flag                                                                 #
# --------------------------------------------------------------------------- #

def test_overriding_part_mult_flips_PART_to_UNRELATED():
    import dataclasses
    assert _classify(_part_points()).status == pc.PART
    p = dataclasses.replace(pc.DEFAULT_PARAMS, part_mult=2.0)
    assert _classify(_part_points(), params=p).status == pc.UNRELATED


def test_default_params_are_read_at_call_time(monkeypatch):
    import dataclasses
    monkeypatch.setattr(pc, "DEFAULT_PARAMS",
                        dataclasses.replace(pc.DEFAULT_PARAMS, part_mult=2.0))
    assert _classify(_part_points()).status == pc.UNRELATED


def test_mode_is_read_at_call_time(monkeypatch):
    assert pc.UNRELATED_PATH_MODE == "off"        # the shipped default
    assert pc.path_mode() == "off"
    monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", "shadow")
    assert pc.path_mode() == "shadow"
    monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", " ON ")
    assert pc.path_mode() == "on"
    monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", "yes")   # a typo is never a live path
    assert pc.path_mode() == "off"


def test_leg_mid_and_extend_extreme():
    assert pc.leg_mid(29000.0, 29400.0) == 29200.0
    assert pc.leg_mid(29400, 29001) == 29200.5
    assert pc.extend_extreme("UP", 29400.0, [29350.0, 29410.25, float("nan")]) == 29410.25
    assert pc.extend_extreme("DOWN", 29000.0, [29050.0, 28990.5]) == 28990.5
    assert pc.extend_extreme("UP", 29400.0, []) == 29400.0
    assert pc.extend_extreme("DOWN", 29000.0, None) == 29000.0


def test_to_dict_is_json_safe():
    import json
    blob = json.loads(json.dumps(_classify(_unrelated_points()).to_dict()))
    assert blob["status"] == "UNRELATED" and blob["origin_ts"].startswith("2026-08-31T18:00")
    assert blob["params"]["big_pct"] == 1.14 and blob["origin_source"] == "auto"


# --------------------------------------------------------------------------- #
# arm D (harness only): the operator's leg start                                #
# --------------------------------------------------------------------------- #

def test_empty_leg_start_overrides_change_nothing(monkeypatch):
    base = _classify(_unrelated_points()).to_dict()
    monkeypatch.setattr(pc, "LEG_START_OVERRIDES", {})
    assert _classify(_unrelated_points()).to_dict() == base
    monkeypatch.setattr(pc, "LEG_START_OVERRIDES", {"2026-08-28": "07:00"})   # another day
    assert _classify(_unrelated_points()).to_dict() == base


def test_a_leg_start_override_moves_the_origin_and_the_mid(monkeypatch):
    auto = _classify(_unrelated_points())
    monkeypatch.setattr(pc, "LEG_START_OVERRIDES", {"2026-09-01": "06:00"})
    op = _classify(_unrelated_points())
    # 04:00 29000 -> 09:00 29400 is linear: 06:00 = 29160
    assert op.origin_source == "operator" and op.origin_ts == _t(0, 6)
    assert op.origin == 29160.0 and op.extreme == 29400.0 and op.size == 240.0
    assert op.mid_at_boundary() == 29280.0 != auto.mid_at_boundary()
    assert op.auto_leg["origin"] == 29000.0
    assert op.status == pc.NOT_BIG               # big / PART are computed on the new leg
    monkeypatch.setattr(pc, "LEG_START_OVERRIDES", {"2026-09-01": "20:00"})   # prior evening
    ev = _classify(_unrelated_points())
    assert ev.origin_ts == _t(-1, 20) and ev.status == pc.UNRELATED
