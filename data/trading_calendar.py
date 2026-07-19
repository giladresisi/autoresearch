"""Holiday-aware CME Globex (MNQ/MES) trading-calendar helpers.

Regular schedule (ET):
  Opens:       Sunday 18:00
  Daily break: 17:00-18:00 Mon-Thu
  Weekend:     Friday 17:00 through Sunday 18:00

Holiday early closes are listed in EARLY_CLOSES_ET (date -> close hour ET); the
market reopens at the next regular open (18:00 same day Mon-Thu, Sunday 18:00
for a Friday early close).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

_ET = "America/New_York"

# Observed 2026 CME equity-futures early closes (13:00 ET). Extend as holidays approach.
EARLY_CLOSES_ET: dict[date, int] = {
    date(2026, 5, 25): 13,   # Memorial Day (Monday)
    date(2026, 6, 19): 13,   # Juneteenth (Friday)
    date(2026, 7, 3): 13,    # Independence Day observed (Friday)
}


def _to_et(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_convert(_ET) if ts.tzinfo is not None else ts.tz_localize(_ET)


def is_market_closed(ts: pd.Timestamp) -> bool:
    """True if the market is closed at ts (weekend, daily break, or holiday closure)."""
    et = _to_et(ts)
    dow = et.weekday()  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun
    t = et.hour + et.minute / 60.0 + et.second / 3600.0
    if (dow == 4 and t >= 17) or dow == 5 or (dow == 6 and t < 18):
        return True
    if 17 <= t < 18:  # daily maintenance break (Mon-Thu; Fri covered above)
        return True
    close_h = EARLY_CLOSES_ET.get(et.date())
    if close_h is not None and t >= close_h:
        return True
    return False


def prev_trading_close(ts: pd.Timestamp) -> pd.Timestamp:
    """Return ts if the market is open at ts, else the most recent close before ts."""
    et = _to_et(ts)
    if not is_market_closed(et):
        return et
    for days_back in range(8):
        d = et.date() - timedelta(days=days_back)
        if d.weekday() in (5, 6):  # Sat/Sun have no close
            continue
        close_h = EARLY_CLOSES_ET.get(d, 17)
        cand = pd.Timestamp(d, tz=_ET) + pd.Timedelta(hours=close_h)
        if cand <= et:
            return cand
    return et
