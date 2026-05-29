# daily.py
# Once per session at 09:20 ET: compute the fixed-for-the-day levels into daily.json
# (TDO, TWO, prior-day highs/lows, unvisited 1hr/4hr FVGs) and update global.json's
# all-time high. Performs no hypothesis or position resets — those are handled elsewhere.

from __future__ import annotations

import datetime
from typing import Optional

import pandas as pd

from smt_state import (
    load_global, save_global,
    load_daily, save_daily,
)
from strategy_smt import compute_tdo

# ---------------------------------------------------------------------------
# Session time windows (ET local times, naive — compared against .time())
# Each is (start_hour, start_min, end_hour, end_min).
# Asia crosses midnight: start = prior calendar day 18:00, end = current day 03:00.
# ---------------------------------------------------------------------------

TIME_WINDOWS = {
    # (start_h, start_m, end_h, end_m) — four equal 6-hour blocks in ET.
    # Asia crosses midnight: start = prior calendar day 18:00, end = current day 00:00.
    "asia":       (18, 0,  0,  0),   # 18:00 prior day → 00:00 current day
    "london":     ( 0, 0,  6,  0),   # 00:00 → 06:00 current day
    "ny_morning": ( 6, 0, 12,  0),   # 06:00 → 12:00 current day
    "ny_evening": (12, 0, 17,  0),   # 12:00 → 17:00 current day (CME maintenance starts 17:00)
}


def _session_bars(mnq_1m: pd.DataFrame, session: str, today: datetime.date) -> pd.DataFrame:
    """Filter mnq_1m to bars belonging to the named session on `today`."""
    start_h, start_m, end_h, end_m = TIME_WINDOWS[session]

    if session == "asia":
        # Prior calendar day 18:00 ET → current day 00:00 ET (midnight)
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


def _compute_two(
    hist_mnq_1m: pd.DataFrame,
    today: datetime.date,
    now: Optional[datetime.datetime] = None,
) -> Optional[float]:
    """Return the Open of the Monday 18:00 ET bar for the current trading week (ICT TWO).

    Per ICT theory the True Week Open is Monday's 18:00 ET bar — the opening of
    Monday's overnight futures session, which TradingView labels as the Monday daily open.

    Priority:
      1. Monday 18:00 ET of the current ISO week (when the bar exists).
      2. If it's Monday's session and Monday 18:00 ET hasn't occurred yet (now < monday_1800),
         use last week's Monday 18:00 ET as the TWO proxy.
      3. Monday 00:00 ET (midnight) of the current ISO week.
      4. First available bar of the ISO week.
    """
    if hist_mnq_1m.empty:
        return None

    today_ts = pd.Timestamp(today)
    today_isocal = today_ts.isocalendar()
    today_iso_week = today_isocal.week
    today_iso_year = today_isocal.year

    # Monday of this ISO week (weekday 1=Mon … 7=Sun)
    today_weekday = today_ts.isocalendar().weekday
    days_since_monday = today_weekday - 1  # 0 on Mon, 6 on Sun
    monday_ts = today_ts - pd.Timedelta(days=days_since_monday)

    # Primary: Monday 18:00 ET
    monday_1800 = pd.Timestamp(
        datetime.datetime(monday_ts.year, monday_ts.month, monday_ts.day, 18, 0, 0),
        tz="America/New_York",
    )
    if monday_1800 in hist_mnq_1m.index:
        return float(hist_mnq_1m.loc[monday_1800, "Open"])

    # Monday session before 18:00 ET: Monday 18:00 bar doesn't exist yet.
    # Use last week's Monday 18:00 ET (previous TWO) as the proxy.
    if now is not None and now < monday_1800:
        prev_monday_ts = monday_ts - pd.Timedelta(days=7)
        prev_monday_1800 = pd.Timestamp(
            datetime.datetime(
                prev_monday_ts.year, prev_monday_ts.month, prev_monday_ts.day, 18, 0, 0
            ),
            tz="America/New_York",
        )
        if prev_monday_1800 in hist_mnq_1m.index:
            return float(hist_mnq_1m.loc[prev_monday_1800, "Open"])

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


def run_daily_fixed(
    now: datetime.datetime,
    hist_mnq_1m: pd.DataFrame,
    hist_1hr: pd.DataFrame,
    hist_4hr: pd.DataFrame,
    today: datetime.date,
) -> None:
    """Once-per-session entry point: compute the fixed-for-the-day levels.

    Parameters
    ----------
    now         : current wall-clock / bar time (tz-aware, ET)
    hist_mnq_1m : historical 1m bars (multiple prior days + current week)
    hist_1hr    : 1hr bars over recent trading days (for FVG scan)
    hist_4hr    : 4hr bars over recent trading days (for FVG scan)
    today       : the trading date these levels apply to
    """

    # ------------------------------------------------------------------ #
    # Step 2: compute liquidities                                          #
    # ------------------------------------------------------------------ #
    liquidities: list[dict] = []

    # TDO — True Day Open via strategy_smt helper
    tdo_price = compute_tdo(hist_mnq_1m, today)
    if tdo_price is not None:
        liquidities.append({"name": "TDO", "kind": "level", "price": float(tdo_price)})

    # TWO — True Week Open (inline)
    two_price = _compute_two(hist_mnq_1m, today, now)
    if two_price is not None:
        liquidities.append({"name": "TWO", "kind": "level", "price": float(two_price)})

    # Prior 2 trading days: high, low, TDO
    # Window = CME session: (prior_date-1) 18:00 ET → prior_date 17:00 ET.
    # Midnight-to-midnight would include the evening bars of the NEXT session.
    for i, prior_date in enumerate(_last_n_trading_dates(today, 2), start=1):
        _pmid  = pd.Timestamp(prior_date, tz="America/New_York")
        _ps_dt = _pmid - pd.Timedelta(hours=6)   # prior_date-1 18:00 ET
        _pe_dt = _pmid + pd.Timedelta(hours=17)  # prior_date   17:00 ET
        _ps = hist_mnq_1m.index.searchsorted(_ps_dt, side="left")
        _pe = hist_mnq_1m.index.searchsorted(_pe_dt, side="left")
        prior_bars = hist_mnq_1m.iloc[_ps:_pe]
        if not prior_bars.empty:
            liquidities.append({"name": f"prev{i}_day_high", "kind": "level",
                                "price": float(prior_bars["High"].max())})
            liquidities.append({"name": f"prev{i}_day_low", "kind": "level",
                                "price": float(prior_bars["Low"].min())})
        prior_tdo = compute_tdo(hist_mnq_1m, prior_date)
        if prior_tdo is not None:
            liquidities.append({"name": f"prev{i}_TDO", "kind": "level",
                                "price": float(prior_tdo)})

    # Recent unvisited 1hr FVGs
    fvgs = _detect_fvgs(hist_1hr, hist_mnq_1m)
    liquidities.extend(fvgs)
    # Recent unvisited 4hr FVGs
    fvgs_4hr = _detect_fvgs(hist_4hr, hist_mnq_1m)
    liquidities.extend(fvgs_4hr)

    # ------------------------------------------------------------------ #
    # Step 3: update global.json all_time_high if today's high exceeds it #
    # ------------------------------------------------------------------ #
    global_state = load_global()
    _hist_ath = float(hist_mnq_1m["High"].max()) if not hist_mnq_1m.empty else 0.0
    global_state["all_time_high"] = max(_hist_ath, float(global_state.get("all_time_high", 0.0)))
    save_global(global_state)

    # ------------------------------------------------------------------ #
    # Step 4 + 5: estimated_dir and opposite_premove (TBD hardcoded)      #
    # ------------------------------------------------------------------ #
    estimated_dir = global_state.get("trend", "up")  # TBD
    opposite_premove = "no"                # TBD

    # ------------------------------------------------------------------ #
    # Write daily.json                                                     #
    # ------------------------------------------------------------------ #
    daily_state = {
        "formed_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "liquidities": liquidities,
        "estimated_dir": estimated_dir,
        "opposite_premove": opposite_premove,
    }
    save_daily(daily_state)
