# daily.py
# Once per session at 09:20 ET: compute daily.json liquidities, update global.json,
# reset per-session position.json fields, set hypothesis.direction = "none".

from __future__ import annotations

import datetime
from typing import Optional

import pandas as pd

from smt_state import (
    load_global, save_global,
    load_daily, save_daily,
    load_hypothesis, save_hypothesis,
    load_position, save_position,
)
from strategy_smt import compute_tdo
from hypothesis import compute_live_hl_mid

# ---------------------------------------------------------------------------
# Session time windows (ET local times, naive — compared against .time())
# Each is (start_hour, start_min, end_hour, end_min).
# Asia crosses midnight: start = prior calendar day 18:00, end = current day 03:00.
# ---------------------------------------------------------------------------

TIME_WINDOWS = {
    # (start_h, start_m, end_h, end_m)  — midnight-crossing sessions use negative start
    # We handle asia specially (prior day 18:00 → current 03:00)
    "asia":       (18, 0,  3,  0),   # 18:00 prior day → 03:00 current day
    "london":     ( 3, 0,  8,  0),   # 03:00 → 08:00 current day
    "ny_morning": ( 8, 0, 12,  0),   # 08:00 → 12:00 current day
    "ny_evening": (12, 0, 17,  0),   # 12:00 → 17:00 current day
}


def _session_bars(mnq_1m: pd.DataFrame, session: str, today: datetime.date) -> pd.DataFrame:
    """Filter mnq_1m to bars belonging to the named session on `today`."""
    start_h, start_m, end_h, end_m = TIME_WINDOWS[session]

    if session == "asia":
        # Prior calendar day 18:00 ET → current day 03:00 ET
        prior_day = today - datetime.timedelta(days=1)
        start_ts = pd.Timestamp(
            datetime.datetime(prior_day.year, prior_day.month, prior_day.day,
                              start_h, start_m, 0),
            tz="America/New_York",
        )
        end_ts = pd.Timestamp(
            datetime.datetime(today.year, today.month, today.day, end_h, end_m, 0),
            tz="America/New_York",
        )
    else:
        start_ts = pd.Timestamp(
            datetime.datetime(today.year, today.month, today.day, start_h, start_m, 0),
            tz="America/New_York",
        )
        end_ts = pd.Timestamp(
            datetime.datetime(today.year, today.month, today.day, end_h, end_m, 0),
            tz="America/New_York",
        )

    return mnq_1m[(mnq_1m.index >= start_ts) & (mnq_1m.index < end_ts)]


def _compute_two(hist_mnq_1m: pd.DataFrame, today: datetime.date) -> Optional[float]:
    """Return the Open of the first 1m bar of the current futures week.

    Futures week starts Sunday 18:00 ET (before ISO Monday). ISO week numbering
    puts Sunday in the *previous* ISO week, so we search across both the current
    ISO week AND the prior calendar Sunday.

    Priority:
      1. Sunday 18:00 ET (prior calendar day if today is Monday, or walk back to
         the most-recent Sunday).
      2. Monday 00:00 ET (start of ISO week).
      3. First available bar of the ISO week.
    """
    if hist_mnq_1m.empty:
        return None

    today_ts = pd.Timestamp(today)
    today_isocal = today_ts.isocalendar()
    today_iso_week = today_isocal.week
    today_iso_year = today_isocal.year

    # Compute Monday and Sunday of this futures week.
    # Futures week opens Sunday 18:00 ET, so "this week's Sunday" is today when today IS Sunday.
    today_weekday = today_ts.isocalendar().weekday  # 1=Mon … 7=Sun
    days_since_monday = today_weekday - 1           # 0 on Mon, 6 on Sun
    monday_ts = today_ts - pd.Timedelta(days=days_since_monday)
    if today_weekday == 7:
        sunday_ts = today_ts  # today is the futures-week open Sunday
    else:
        sunday_ts = monday_ts - pd.Timedelta(days=1)

    # Try Sunday 18:00 ET — look in hist_mnq_1m directly (no ISO-week filter needed)
    sunday_1800 = pd.Timestamp(
        datetime.datetime(
            sunday_ts.year, sunday_ts.month, sunday_ts.day, 18, 0, 0,
        ),
        tz="America/New_York",
    )
    if sunday_1800 in hist_mnq_1m.index:
        return float(hist_mnq_1m.loc[sunday_1800, "Open"])

    # Filter to ISO-week bars for fallback paths
    _iso = hist_mnq_1m.index.isocalendar()
    mask = (_iso["year"] == today_iso_year) & (_iso["week"] == today_iso_week)
    week_bars = hist_mnq_1m[mask]

    # Fallback: Monday 00:00 ET
    monday_0000 = pd.Timestamp(
        datetime.datetime(monday_ts.year, monday_ts.month, monday_ts.day, 0, 0, 0),
        tz="America/New_York",
    )
    if monday_0000 in hist_mnq_1m.index:
        return float(hist_mnq_1m.loc[monday_0000, "Open"])

    if week_bars.empty:
        return None

    # Ultimate fallback: first available bar of the ISO week
    return float(week_bars.iloc[0]["Open"])


def _last_n_trading_dates(today: datetime.date, n: int) -> list[datetime.date]:
    """Return the last n trading dates (Mon–Fri) strictly before today."""
    dates: list[datetime.date] = []
    d = today - datetime.timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5:  # Mon=0…Fri=4; skip Sat=5, Sun=6
            dates.append(d)
        d -= datetime.timedelta(days=1)
    return dates


def _detect_fvgs(
    hourly_bars: pd.DataFrame,
    mnq_1m: pd.DataFrame,
) -> list[dict]:
    """Detect unvisited 1hr FVGs using inline triple-bar test.

    A bullish FVG: bars[i+2].Low > bars[i].High
    A bearish FVG: bars[i+2].High < bars[i].Low

    "Unvisited" = no subsequent 1m bar re-entered the gap zone after formation.
    """
    if len(hourly_bars) < 3:
        return []

    highs = hourly_bars["High"].values
    lows = hourly_bars["Low"].values
    idx = hourly_bars.index

    # Pre-filter 1m bars to only those after the earliest possible FVG formation.
    earliest_formation = idx[2]
    later_1m_all = mnq_1m[mnq_1m.index > earliest_formation]
    later_high = later_1m_all["High"].values if not later_1m_all.empty else None
    later_low  = later_1m_all["Low"].values  if not later_1m_all.empty else None
    later_idx  = later_1m_all.index

    result = []

    for i in range(len(hourly_bars) - 2):
        bar1_h = highs[i]
        bar1_l = lows[i]
        bar3_h = highs[i + 2]
        bar3_l = lows[i + 2]

        fvg_top = None
        fvg_bottom = None
        side = None

        if bar3_l > bar1_h:
            fvg_top    = float(bar3_l)
            fvg_bottom = float(bar1_h)
            side = "bull"
        elif bar3_h < bar1_l:
            fvg_top    = float(bar1_l)
            fvg_bottom = float(bar3_h)
            side = "bear"

        if side is None:
            continue

        formation_ts = idx[i + 2]

        if later_high is not None:
            # Slice to bars after this FVG's formation using searchsorted (O(log n))
            pos = later_idx.searchsorted(formation_ts, side="right")
            h = later_high[pos:]
            lo = later_low[pos:]
            if len(h) > 0 and ((h >= fvg_bottom) & (lo <= fvg_top)).any():
                continue  # Visited — exclude

        ts_str = formation_ts.strftime("%Y%m%d_%H%M")
        result.append({
            "name":   f"fvg_{ts_str}_{side}",
            "kind":   "fvg",
            "top":    fvg_top,
            "bottom": fvg_bottom,
        })

    return result


def run_daily(
    now: datetime.datetime,
    mnq_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    hist_hourly_mnq: pd.DataFrame,
) -> None:
    """Once-per-session entry point called at 09:20 ET.

    Parameters
    ----------
    now           : current wall-clock / bar time (tz-aware, ET)
    mnq_1m        : 1m bars for today's session (tz-aware ET index)
    hist_mnq_1m   : historical 1m bars (multiple prior days + current week)
    hist_hourly_mnq: 1hr bars over last ~3 trading days (for FVG scan)
    """
    today = now.date()

    # ------------------------------------------------------------------ #
    # Step 1: read existing daily.json (recomputed anyway, kept for ref)  #
    # ------------------------------------------------------------------ #
    _daily = load_daily()  # noqa: not used after this

    # ------------------------------------------------------------------ #
    # Step 2: compute liquidities                                          #
    # ------------------------------------------------------------------ #
    liquidities: list[dict] = []

    # TDO — True Day Open via strategy_smt helper
    tdo_price = compute_tdo(mnq_1m, today)
    if tdo_price is None and not hist_mnq_1m.empty:
        tdo_price = compute_tdo(hist_mnq_1m, today)
    if tdo_price is not None:
        liquidities.append({"name": "TDO", "kind": "level", "price": float(tdo_price)})

    # TWO — True Week Open (inline)
    # Combine hist_mnq_1m and mnq_1m for the current-week lookup
    combined_1m = pd.concat([hist_mnq_1m, mnq_1m]).sort_index()
    combined_1m = combined_1m[~combined_1m.index.duplicated(keep="last")]
    two_price = _compute_two(combined_1m, today)
    if two_price is not None:
        liquidities.append({"name": "TWO", "kind": "level", "price": float(two_price)})

    # week / day high, low, mid — delegated to compute_live_hl_mid (shared with hypothesis.py).
    _now_ts = pd.Timestamp(now)
    if _now_ts.tzinfo is None:
        _now_ts = _now_ts.tz_localize("America/New_York")
    else:
        _now_ts = _now_ts.tz_convert("America/New_York")
    _live_hl = compute_live_hl_mid(combined_1m, _now_ts)

    # Fallback for day: if no bars from overnight start, try the calendar-date filter.
    if "day_high" not in _live_hl:
        _fb = combined_1m[combined_1m.index.date == today]
        if not _fb.empty:
            _dh, _dl = float(_fb["High"].max()), float(_fb["Low"].min())
            _live_hl.update({"day_high": _dh, "day_low": _dl, "day_mid": (_dh + _dl) / 2.0})

    for _name in ("week_high", "week_low", "week_mid", "day_high", "day_low", "day_mid"):
        if _name in _live_hl:
            liquidities.append({"name": _name, "kind": "level", "price": _live_hl[_name]})

    # Prior 2 trading days: high, low, TDO
    for i, prior_date in enumerate(_last_n_trading_dates(today, 2), start=1):
        _pmid = pd.Timestamp(prior_date, tz="America/New_York")
        _ps = combined_1m.index.searchsorted(_pmid,                             side="left")
        _pe = combined_1m.index.searchsorted(_pmid + pd.Timedelta(days=1),      side="left")
        prior_bars = combined_1m.iloc[_ps:_pe]
        if not prior_bars.empty:
            liquidities.append({"name": f"prev{i}_day_high", "kind": "level",
                                "price": float(prior_bars["High"].max())})
            liquidities.append({"name": f"prev{i}_day_low", "kind": "level",
                                "price": float(prior_bars["Low"].min())})
        prior_tdo = compute_tdo(combined_1m, prior_date)
        if prior_tdo is not None:
            liquidities.append({"name": f"prev{i}_TDO", "kind": "level",
                                "price": float(prior_tdo)})

    # Session highs/lows — use combined_1m so we can look back into hist for asia
    for session in ("asia", "london", "ny_morning", "ny_evening"):
        session_bars = _session_bars(combined_1m, session, today)
        if not session_bars.empty:
            liquidities.append({
                "name": f"{session}_high",
                "kind": "level",
                "price": float(session_bars["High"].max()),
            })
            liquidities.append({
                "name": f"{session}_low",
                "kind": "level",
                "price": float(session_bars["Low"].min()),
            })

    # Recent unvisited 1hr FVGs
    fvgs = _detect_fvgs(hist_hourly_mnq, combined_1m)
    liquidities.extend(fvgs)

    # ------------------------------------------------------------------ #
    # Step 3: update global.json all_time_high if today's high exceeds it #
    # ------------------------------------------------------------------ #
    global_state = load_global()
    if "day_high" in _live_hl and _live_hl["day_high"] > global_state["all_time_high"]:
        global_state["all_time_high"] = _live_hl["day_high"]
        save_global(global_state)

    # ------------------------------------------------------------------ #
    # Step 4 + 5: estimated_dir and opposite_premove (TBD hardcoded)      #
    # ------------------------------------------------------------------ #
    estimated_dir = global_state["trend"]  # TBD
    opposite_premove = "no"                # TBD

    # ------------------------------------------------------------------ #
    # Write daily.json                                                     #
    # ------------------------------------------------------------------ #
    daily_state = {
        "date": str(today),
        "liquidities": liquidities,
        "estimated_dir": estimated_dir,
        "opposite_premove": opposite_premove,
    }
    save_daily(daily_state)

    # ------------------------------------------------------------------ #
    # Step 6: hypothesis.json.direction = "none"                          #
    # ------------------------------------------------------------------ #
    hyp = load_hypothesis()
    hyp["direction"] = "none"
    save_hypothesis(hyp)

    # ------------------------------------------------------------------ #
    # Step 7: reset position.json per-session fields                      #
    # ------------------------------------------------------------------ #
    pos = load_position()
    pos["active"] = {}
    pos["limit_entry"] = ""
    pos["confirmation_bar"] = {}
    pos["failed_entries"] = 0
    save_position(pos)
