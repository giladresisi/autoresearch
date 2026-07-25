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

# CME equity-futures holiday closures, ET close hour (fractional; 0 = closed all day).
# May-Jul entries observed in live data; H2 entries encoded 2026-07-25 from CME's
# published pattern — CME finalizes exact times ~2 weeks before each holiday and they
# can shift 15-30 min, so RE-VERIFY each entry against cmegroup.com shortly before the
# holiday. Maintenance chore: append next year's dates every January.
EARLY_CLOSES_ET: dict[date, float] = {
    date(2026, 5, 25): 13,      # Memorial Day (Monday) — observed
    date(2026, 6, 19): 13,      # Juneteenth (Friday) — observed
    date(2026, 7, 3): 13,       # Independence Day observed (Friday) — observed
    date(2026, 9, 7): 13,       # Labor Day (Monday)
    date(2026, 11, 26): 13.25,  # Thanksgiving (Thursday), 13:15 ET
    date(2026, 11, 27): 13.25,  # Day after Thanksgiving (Friday), 13:15 ET
    date(2026, 12, 25): 0,      # Christmas (Friday) — full closure
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
        if close_h == 0:  # full-closure day has no close of its own
            continue
        cand = pd.Timestamp(d, tz=_ET) + pd.Timedelta(hours=close_h)
        if cand <= et:
            return cand
    return et
