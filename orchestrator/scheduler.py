# orchestrator/scheduler.py
# Trading day and session timing utilities backed by the NYSE exchange calendar.
import datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

_CALENDAR = xcals.get_calendar("XNYS")
_ET = ZoneInfo("America/New_York")
_SESSION_OPEN = datetime.time(9, 0)


def is_trading_day(date: datetime.date) -> bool:
    return _CALENDAR.is_session(str(date))


def get_et_now() -> datetime.datetime:
    return datetime.datetime.now(tz=_ET)


def next_session_open(now: datetime.datetime | None = None) -> datetime.datetime:
    """Return the next market open datetime in ET.

    If today is a trading day and now < 09:00 ET, return today 09:00 ET.
    Otherwise advance to the next trading session.
    """
    if now is None:
        now = get_et_now()
    today = now.date()
    if is_trading_day(today) and now.time() < _SESSION_OPEN:
        return datetime.datetime(today.year, today.month, today.day, 9, 0, tzinfo=_ET)
    if is_trading_day(today):
        next_session = _CALENDAR.next_session(str(today))
    else:
        next_session = _CALENDAR.date_to_session(str(today), "next")
    next_date = next_session.date()
    return datetime.datetime(next_date.year, next_date.month, next_date.day, 9, 0, tzinfo=_ET)
