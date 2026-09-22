"""Plan 40: unnested weekly / monthly extremes (`agent/facts/htf_extremes.py`).

Synthetic 1m frames only — the real-data pins are in `test_htf_extremes_real.py` (slow).
"""
import numpy as np
import pandas as pd
import pytest

from agent.derive_facts import FactsBundle, build_menus
from agent.facts.htf_extremes import (EXTREMES_START, TZ, compute_unnested_extremes,
                                      merge_extremes, menu_rows, pools_at,
                                      running_extremes_at, running_period_seed,
                                      session_as_of, still_unnested)

START = pd.Timestamp("2026-06-01", tz=TZ)          # a Monday, ISO week 23


def _ts(s):
    return pd.Timestamp(s, tz=TZ)


def _frame(rows):
    """rows: (ts, high, low). Filler bars are not needed — only extremes matter."""
    idx = pd.DatetimeIndex([_ts(t) for t, _h, _l in rows])
    hi = [h for _t, h, _l in rows]
    lo = [l for _t, _h, l in rows]
    df = pd.DataFrame({"Open": lo, "High": hi, "Low": lo, "Close": hi, "Volume": 1.0},
                      index=idx)
    return df.sort_index()


def _two_weeks():
    return [("2026-06-02 10:00", 110.0, 100.0), ("2026-06-03 10:00", 104.0, 90.0),
            ("2026-06-09 10:00", 105.0, 99.0), ("2026-06-10 10:00", 103.0, 95.0),
            ("2026-06-16 10:00", 100.0, 97.0)]


def _by_name(rows):
    return {r.name: r for r in rows}


def _ext(frame, trade_date, start=START, ticker="MNQ"):
    return compute_unnested_extremes(frame, session_as_of(trade_date), ticker=ticker,
                                     start=start)


# --------------------------------------------------------------------------- #
# the completed list                                                            #
# --------------------------------------------------------------------------- #

def test_weekly_and_monthly_extremes_happy_path():
    rows = _ext(_frame(_two_weeks()), "2026-06-17")
    got = {(r.name, r.price, r.side, r.tier, r.period) for r in rows}
    assert got == {
        ("htf_week_high_20260602", 110.0, "above", "htf_week", "2026-W23"),
        ("htf_week_high_20260609", 105.0, "above", "htf_week", "2026-W24"),
        ("htf_week_low_20260603", 90.0, "below", "htf_week", "2026-W23"),
        ("htf_week_low_20260610", 95.0, "below", "htf_week", "2026-W24"),
    }
    # July: June is a completed month now; its extremes share the week-23 bars.
    july = _frame(_two_weeks() + [("2026-07-07 10:00", 101.0, 98.0)])
    names = set(_by_name(_ext(july, "2026-07-08")))
    assert "htf_month_high_202606" in names and "htf_month_low_202606" in names


def test_extreme_dropped_once_a_later_bar_trades_strictly_beyond():
    rows = _two_weeks() + [("2026-06-11 10:00", 110.25, 96.0)]
    got = _by_name(_ext(_frame(rows), "2026-06-17"))
    assert "htf_week_high_20260602" not in got           # 110.25 > 110 later
    assert got["htf_week_high_20260611"].price == 110.25


def test_equal_later_extreme_dedupes_to_one_entry_with_alias():
    rows = _two_weeks() + [("2026-06-11 10:00", 110.0, 96.0)]
    highs = [r for r in _ext(_frame(rows), "2026-06-17") if r.side == "above"]
    at_110 = [r for r in highs if r.price == 110.0]
    assert len(at_110) == 1
    # both weeks: the most recent is kept, the older is an alias
    assert at_110[0].name == "htf_week_high_20260611"
    assert at_110[0].aliases == ("htf_week_high_20260602",)


def test_surviving_highs_strictly_decrease_and_lows_strictly_increase_in_time():
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-06-01 18:00", "2026-09-10 16:00", freq="37min", tz=TZ)
    px = 1000 + np.cumsum(rng.normal(0, 3, len(idx)))
    df = pd.DataFrame({"Open": px, "High": px + rng.uniform(0, 2, len(idx)),
                       "Low": px - rng.uniform(0, 2, len(idx)), "Close": px,
                       "Volume": 1.0}, index=idx)
    rows = compute_unnested_extremes(df, session_as_of("2026-09-09"), ticker="MNQ",
                                     start=START)
    highs = sorted((r for r in rows if r.side == "above"), key=lambda r: r.ts)
    lows = sorted((r for r in rows if r.side == "below"), key=lambda r: r.ts)
    assert highs and lows
    assert all(a.price > b.price for a, b in zip(highs, highs[1:]))
    assert all(a.price < b.price for a, b in zip(lows, lows[1:]))


def test_completed_list_excludes_the_running_week_and_month():
    rows = _ext(_frame(_two_weeks()), "2026-06-17")
    assert not [r for r in rows if r.period in ("2026-W25", "2026-06")]
    # ...but the running period still nests: a week-25 bar above 105 drops week 24's high
    nest = _two_weeks() + [("2026-06-15 10:00", 106.0, 97.0)]
    assert "htf_week_high_20260609" not in _by_name(_ext(_frame(nest), "2026-06-17"))


def test_week_and_month_sharing_one_bar_is_one_row_with_alias():
    july = _frame(_two_weeks() + [("2026-07-07 10:00", 101.0, 98.0)])
    got = _by_name(_ext(july, "2026-07-08"))
    assert "htf_week_high_20260602" not in got
    assert got["htf_month_high_202606"].aliases == ("htf_week_high_20260602",)
    assert got["htf_month_high_202606"].tier == "htf_month"


def test_window_extreme_flags_max_high_and_min_low():
    got = _by_name(_ext(_frame(_two_weeks()), "2026-06-17"))
    assert got["htf_week_high_20260602"].is_window_extreme
    assert got["htf_week_low_20260603"].is_window_extreme
    assert not got["htf_week_high_20260609"].is_window_extreme
    assert not got["htf_week_low_20260610"].is_window_extreme


def test_sunday_18_00_bar_belongs_to_the_monday_iso_week():
    rows = _two_weeks() + [("2026-06-07 18:00", 120.0, 99.5)]    # Sunday evening
    got = _by_name(_ext(_frame(rows), "2026-06-17"))
    r = got["htf_week_high_20260608"]
    assert r.period == "2026-W24" and r.price == 120.0


def test_month_is_keyed_on_trade_date():
    rows = _two_weeks() + [("2026-06-30 20:00", 130.0, 99.0),     # trade date Jul 1
                           ("2026-07-07 10:00", 101.0, 98.0)]
    got = _by_name(_ext(_frame(rows), "2026-08-04"))
    assert got["htf_month_high_202607"].price == 130.0
    assert got["htf_month_high_202607"].period == "2026-07"
    assert "htf_month_high_202606" not in got or got["htf_month_high_202606"].price != 130.0


def test_bars_before_extremes_start_are_ignored():
    rows = [("2026-05-28 10:00", 500.0, 1.0)] + _two_weeks()
    got = _ext(_frame(rows), "2026-06-17")
    assert max(r.price for r in got if r.side == "above") == 110.0
    assert min(r.price for r in got if r.side == "below") == 90.0


def test_start_is_per_asset_mnq_0616_mes_0611():
    assert EXTREMES_START["MNQ"] == pd.Timestamp("2026-06-16", tz=TZ)
    assert EXTREMES_START["MES"] == pd.Timestamp("2026-06-11", tz=TZ)
    rows = [("2026-06-12 10:00", 200.0, 50.0), ("2026-06-16 10:00", 150.0, 60.0),
            ("2026-06-23 10:00", 140.0, 70.0)]
    df = _frame(rows)
    as_of = session_as_of("2026-07-01")
    mnq = compute_unnested_extremes(df, as_of, ticker="MNQ")
    mes = compute_unnested_extremes(df, as_of, ticker="MES")
    assert max(r.price for r in mnq if r.side == "above") == 150.0   # Jun 12 is before
    assert max(r.price for r in mes if r.side == "above") == 200.0   # Jun 12 is inside
    with pytest.raises(ValueError):
        compute_unnested_extremes(df, as_of, ticker="XYZ")


def test_extreme_on_the_start_date_is_included():
    # start Tue Jun 16; its week (W25) is truncated at the start, not dropped. The
    # Jun 15 18:00 bar has trade date Jun 16, so it counts; Jun 15 10:00 does not.
    rows = [("2026-06-15 10:00", 999.0, 1.0), ("2026-06-15 18:00", 150.0, 95.0),
            ("2026-06-16 07:20", 160.0, 96.0), ("2026-06-23 10:00", 140.0, 97.0)]
    got = _by_name(compute_unnested_extremes(
        _frame(rows), session_as_of("2026-06-30"), ticker="MNQ"))
    assert got["htf_week_high_20260616"].price == 160.0
    assert got["htf_week_low_20260616"].price == 95.0


def test_no_lookahead_bars_at_or_after_as_of_are_ignored():
    as_of = session_as_of("2026-06-17")
    rows = _two_weeks() + [(str(as_of.tz_localize(None)), 999.0, 1.0)]
    got = compute_unnested_extremes(_frame(rows), as_of, ticker="MNQ", start=START)
    assert max(r.price for r in got if r.side == "above") == 110.0
    assert min(r.price for r in got if r.side == "below") == 90.0


def test_identical_with_the_future_deleted():
    full = _frame(_two_weeks() + [("2026-06-17 10:00", 999.0, 1.0),
                                  ("2026-06-25 10:00", 50.0, 20.0)])
    as_of = session_as_of("2026-06-17")
    cut = full[full.index < as_of]
    a = compute_unnested_extremes(full, as_of, ticker="MNQ", start=START)
    b = compute_unnested_extremes(cut, as_of, ticker="MNQ", start=START)
    assert a == b
    assert running_period_seed(full, as_of, ticker="MNQ", start=START) == \
        running_period_seed(cut, as_of, ticker="MNQ", start=START)


def test_maintenance_bars_dropped():
    rows = _two_weeks() + [("2026-06-03 17:30", 999.0, 1.0),
                           ("2026-06-03 16:56", 998.0, 2.0)]
    got = _ext(_frame(rows), "2026-06-17")
    assert max(r.price for r in got if r.side == "above") == 110.0
    assert min(r.price for r in got if r.side == "below") == 90.0


def test_deterministic_repeat_call():
    df = _frame(_two_weeks())
    assert _ext(df, "2026-06-17") == _ext(df, "2026-06-17")


# --------------------------------------------------------------------------- #
# the running week / month                                                      #
# --------------------------------------------------------------------------- #

def test_running_seed_uses_only_bars_before_as_of():
    as_of = session_as_of("2026-06-17")             # Tue Jun 16 18:00
    rows = _two_weeks() + [(str(as_of.tz_localize(None)), 999.0, 1.0)]
    seed = running_period_seed(_frame(rows), as_of, ticker="MNQ", start=START)
    assert seed["week"]["period"] == "2026-W25"
    assert seed["week"]["high"][0] == 100.0 and seed["week"]["low"][0] == 97.0
    assert seed["month"]["high"][0] == 110.0 and seed["month"]["low"][0] == 90.0


def test_running_week_and_month_extremes_combine_seed_and_todays_bars():
    as_of = session_as_of("2026-06-17")
    seed = running_period_seed(_frame(_two_weeks()), as_of, ticker="MNQ", start=START)
    today = _frame([("2026-06-17 08:00", 102.0, 98.0), ("2026-06-17 09:00", 101.0, 96.5),
                    ("2026-06-17 09:40", 500.0, 0.0)])               # at `now`: excluded
    rows = _by_name(running_extremes_at(seed, today, as_of, _ts("2026-06-17 09:40")))
    assert rows["htf_week_running_high"].price == 102.0              # today beats the seed
    assert rows["htf_week_running_low"].price == 96.5
    assert rows["htf_month_running_high"].price == 110.0             # the seed stands
    assert rows["htf_month_running_low"].price == 90.0
    # a Monday session: no seed week, today's bars are the whole running week
    mon = session_as_of("2026-06-15")
    seed_m = running_period_seed(_frame(_two_weeks()), mon, ticker="MNQ", start=START)
    assert seed_m["week"]["high"] is None
    got = _by_name(running_extremes_at(seed_m, _frame([("2026-06-15 08:00", 101.0, 99.0)]),
                                       mon, _ts("2026-06-15 09:00")))
    assert got["htf_week_running_high"].price == 101.0


def test_running_month_extreme_equal_to_completed_week_dedupes_month_preferred():
    as_of = session_as_of("2026-06-17")
    df = _frame(_two_weeks())
    ctx = {"as_of": as_of, "extremes": compute_unnested_extremes(
        df, as_of, ticker="MNQ", start=START),
        "seed": running_period_seed(df, as_of, ticker="MNQ", start=START)}
    got = _by_name(pools_at(ctx, _frame([("2026-06-17 08:00", 101.0, 98.0)]),
                            _ts("2026-06-17 09:40")))
    # June's running month high IS week 23's 110: one row, the month, the week an alias
    assert "htf_week_high_20260602" not in got
    assert "htf_week_high_20260602" in got["htf_month_running_high"].aliases
    assert got["htf_month_running_high"].price == 110.0


def _menu(price, pools, ar=20.0, day_hi=None, day_lo=None):
    b = FactsBundle()
    b.now_price = price
    b.day_mid = price
    b.avg_range_1h = {"MNQ": ar}
    b.day_hi = {"MNQ": day_hi if day_hi is not None else price + 1}
    b.day_lo = {"MNQ": day_lo if day_lo is not None else price - 30}
    b.levels = {"MNQ": {}}
    return build_menus(b, {"now_price": price, "levels": {}},
                       extra_pools=menu_rows(pools))["dol"]


def test_running_extreme_at_price_is_filtered_by_the_draw_floor():
    as_of = session_as_of("2026-06-17")
    seed = running_period_seed(_frame(_two_weeks()), as_of, ticker="MNQ", start=START)
    today = _frame([("2026-06-17 08:00", 150.0, 98.0)])               # new week high 150
    rows = running_extremes_at(seed, today, as_of, _ts("2026-06-17 09:40"))
    up = _menu(145.0, rows)["UP"]                                    # 5 pts < 20 floor
    assert not [r for r in up if r["level"].startswith("htf_")]
    assert [r["level"] for r in up] == ["projection_up"]             # Q1 refinement


def test_running_week_high_after_a_pullback_is_a_t2_candidate():
    as_of = session_as_of("2026-06-17")
    seed = running_period_seed(_frame(_two_weeks()), as_of, ticker="MNQ", start=START)
    today = _frame([("2026-06-17 08:00", 150.0, 98.0)])
    rows = running_extremes_at(seed, today, as_of, _ts("2026-06-17 09:40"))
    up = _menu(120.0, rows, day_hi=150.0)["UP"]                      # 30 pts away
    assert up[0]["level"] == "htf_month_running_high"                # month preferred
    assert "htf_week_running_high" in [a for r in rows for a in r.aliases]
    assert up[0]["price"] == 150.0 and up[0]["band"] == "BAND"


# --------------------------------------------------------------------------- #
# intraday pruning                                                              #
# --------------------------------------------------------------------------- #

def test_still_unnested_prunes_on_bars_in_as_of_to_now_strictly_before_now():
    as_of = session_as_of("2026-06-17")
    ext = compute_unnested_extremes(_frame(_two_weeks()), as_of, ticker="MNQ", start=START)
    now = _ts("2026-06-17 09:40:00")
    at_now = _frame([("2026-06-17 09:40:00", 200.0, 99.0)])
    assert still_unnested(ext, at_now, as_of, now) == ext           # the bar AT now: no
    before = _frame([("2026-06-17 09:39:59", 106.0, 99.0)])
    names = {e.name for e in still_unnested(ext, before, as_of, now)}
    assert "htf_week_high_20260609" not in names                     # 106 > 105
    assert "htf_week_high_20260602" in names                         # 106 < 110
    pre = _frame([("2026-06-16 17:59:00", 200.0, 1.0)])              # before as_of: no
    assert still_unnested(ext, pre, as_of, now) == ext


def test_merge_extremes_is_order_independent():
    ext = _ext(_frame(_two_weeks()), "2026-06-17")
    assert merge_extremes(list(reversed(ext))) == merge_extremes(ext)
