# session_times.py
# Single source of truth for V2 trading session window times (America/New_York).
import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TH = ZoneInfo("Asia/Bangkok")

SESSION_OPEN  = datetime.time(18, 0)  # 18:00 ET on the previous trading day
SESSION_CLOSE = datetime.time(17, 0)  # 17:00 ET (maintenance window 17:00–18:00)

# Mutable runtime config — edit session_config.json while the orchestrator is
# running; changes are picked up on the next is_entry_allowed() call.
_CONFIG_PATH = Path(__file__).with_name("session_config.json")
_config_cache: dict = {"mtime": None, "windows": []}


def _get_entry_windows() -> list[str]:
    try:
        mt = _CONFIG_PATH.stat().st_mtime
        if mt != _config_cache["mtime"]:
            data = json.loads(_CONFIG_PATH.read_text())
            _config_cache["mtime"] = mt
            _config_cache["windows"] = data.get("entry_allowed_windows", [])
    except Exception:
        pass
    return _config_cache["windows"]


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
    """Return True if time t falls within any entry_allowed_windows interval."""
    for window in _get_entry_windows():
        start_str, end_str = window.split("-")
        start = datetime.time(int(start_str[:2]), int(start_str[3:]))
        end   = datetime.time(int(end_str[:2]), int(end_str[3:]))
        if start <= end:
            if start <= t < end:
                return True
        else:  # window crosses midnight
            if t >= start or t < end:
                return True
    return False
