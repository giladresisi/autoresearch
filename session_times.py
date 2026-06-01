# session_times.py
# Single source of truth for V2 trading session window times (America/New_York).
import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TH = ZoneInfo("Asia/Bangkok")

SESSION_OPEN  = datetime.time(18, 5)   # 18:05 ET on the previous trading day
SESSION_CLOSE = datetime.time(16, 55)  # 16:55 ET (maintenance window 16:55–18:05)


def cme_session_date(ts: datetime.datetime) -> datetime.date:
    """Return the TH (Asia/Bangkok) calendar date for the CME session containing ts.

    The CME session opens at 18:00 ET. We convert that session-open moment to
    Thailand time to get the "session date" — stable for the entire session and
    season-independent (works for both EDT and EST).

    18:00 ET = 05:00 TH (summer/EDT) = 06:00 TH (winter/EST), always the next TH
    calendar day relative to the ET session-open date.  So:
        TH session date = ET session-open date + 1 day

    This is consistent with the user's "use TH today unless 0:00–4:00 TH" rule,
    because 18:00 ET → 05:00/06:00 TH is always after the 4:00 TH threshold.
    """
    et = ts.astimezone(_ET) if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc).astimezone(_ET)
    d = et.date()
    if et.hour < 18:
        d -= datetime.timedelta(days=1)
    session_open_et = datetime.datetime(d.year, d.month, d.day, 18, 0, tzinfo=_ET)
    return session_open_et.astimezone(_TH).date()


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
