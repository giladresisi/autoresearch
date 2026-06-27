# orchestrator/scheduler.py
# Trading day and session timing utilities backed by the NYSE exchange calendar.
import datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from session_times import SESSION_OPEN, SESSION_CLOSE

_CALENDAR = xcals.get_calendar("XNYS")
_ET = ZoneInfo("America/New_York")


def is_trading_day(date: datetime.date) -> bool:
    return _CALENDAR.is_session(str(date))


def get_et_now() -> datetime.datetime:
    return datetime.datetime.now(tz=_ET)


def is_session_open_day(date: datetime.date) -> bool:
    """True if a CME index-futures session opens on `date` evening (at SESSION_OPEN ET).

    CME equity-index futures trade Sunday 18:00 ET → Friday 17:00 ET. A session that opens
    at SESSION_OPEN on calendar day D carries the NEXT day's CME trade date (D + 1); it is a
    real session iff that trade date is an exchange trading day. This is True for Sun–Thu
    evenings (trade dates Mon–Fri) and False for Fri/Sat evenings (CME closed for the
    weekend), so the orchestrator opens the Sunday-evening session instead of waiting for
    Monday and never spuriously opens on Friday evening.
    """
    return is_trading_day(date + datetime.timedelta(days=1))


def is_session_in_progress(now: datetime.datetime | None = None) -> bool:
    """True if `now` is inside the post-midnight tail of an overnight session that opened the
    PREVIOUS evening and has not yet reached today's SESSION_CLOSE.

    A CME session opens at SESSION_OPEN on day D-1 (carrying trade date D) and runs until
    SESSION_CLOSE on day D, so any moment on a trading day D before SESSION_CLOSE is inside
    that in-progress session — even when is_session_open_day(D) is False (Friday, or the
    morning before a holiday, whose own evening opens nothing). Lets a crash-restart in that
    window resume the live session instead of sleeping to the next open.
    """
    if now is None:
        now = get_et_now()
    today = now.date()
    close_dt = datetime.datetime.combine(today, SESSION_CLOSE, tzinfo=_ET)
    return is_trading_day(today) and now < close_dt


def next_session_open(now: datetime.datetime | None = None) -> datetime.datetime:
    """Return the next CME session-open datetime in ET (SESSION_OPEN, e.g. 18:05).

    The session opens at SESSION_OPEN ET on every day whose evening starts a CME session
    (Sun–Thu; see is_session_open_day). If today is such a day and now is before today's
    open, return today's open; otherwise advance to the next session-open day. This opens
    the Sunday-evening session (trade date Monday) rather than sleeping until Monday.
    """
    if now is None:
        now = get_et_now()
    today_open = datetime.datetime.combine(now.date(), SESSION_OPEN, tzinfo=_ET)
    if now < today_open and is_session_open_day(now.date()):
        return today_open
    d = now.date() + datetime.timedelta(days=1)
    while not is_session_open_day(d):
        d += datetime.timedelta(days=1)
    return datetime.datetime.combine(d, SESSION_OPEN, tzinfo=_ET)
