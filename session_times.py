# session_times.py
# Single source of truth for V2 trading session window times (America/New_York).
import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TH = ZoneInfo("Asia/Bangkok")

SESSION_OPEN  = datetime.time(18, 0)   # 18:00 ET — the CME reopen, right after the maintenance break
SESSION_CLOSE = datetime.time(16, 55)  # 16:55 ET (maintenance window 16:55–18:00)


def cme_session_date(ts: datetime.datetime) -> datetime.date:
    """ET-based CME trade date for the session containing ts.

    CME index futures trade 18:00 ET → 17:00 ET the next day. The session's trade
    date is its CLOSE date = (ET session-open date) + 1 day. This is stable across
    the midnight roll, so an overnight session keeps a single date, and it is
    season-independent (works for both EDT and EST).

    Computed purely from ET — no timezone-conversion detour. (Equivalent to the
    former "18:00 ET → next TH calendar day" rule, since 18:00 ET = 05:00/06:00 TH
    is always the next TH day, i.e. ET session-open date + 1.)
    """
    et = ts.astimezone(_ET) if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc).astimezone(_ET)
    d = et.date()
    if et.hour < 18:                       # before 18:00 ET → session opened yesterday
        d -= datetime.timedelta(days=1)
    return d + datetime.timedelta(days=1)  # trade date = ET session-open date + 1


def session_date_str() -> str:
    """Current session date as YYYY-MM-DD (TH calendar, stable within a CME session)."""
    return cme_session_date(datetime.datetime.now(tz=_ET)).isoformat()


def cme_session_start(ts: datetime.datetime) -> datetime.datetime:
    """Return 18:00 ET on the day the CME session containing ts opened.

    Before 18:00 ET: session opened yesterday at 18:00.
    At/after 18:00 ET: session opened today at 18:00.
    """
    et = ts.astimezone(_ET) if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc).astimezone(_ET)
    d = et.date()
    if et.hour < 18:
        d -= datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, 18, 0, tzinfo=_ET)


def is_entry_allowed(t: datetime.time) -> bool:
    """Entry-window gating is disabled — entries are unrestricted within the session.

    The configurable entry-allowed-windows feature was retired: the processed session
    window (SESSION_OPEN..SESSION_CLOSE = 18:05..16:55 ET) now *is* the allowed window,
    so per-bar gating is redundant. Kept as an always-True shim so existing callers in
    strategy.py / session_pipeline.py / execution paths keep working without change.
    Removing the per-call session_config.json stat() also eliminated a large 1s-mode
    backtest cost (~33s/day from ~125k stat syscalls).
    """
    return True
