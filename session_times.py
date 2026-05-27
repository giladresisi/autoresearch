# session_times.py
# Single source of truth for V2 trading session window times (America/New_York).
import datetime

SESSION_OPEN  = datetime.time(18, 0)  # 18:00 ET on the previous trading day
SESSION_CLOSE = datetime.time(17, 0)  # 17:00 ET (maintenance window 17:00–18:00)

# Time windows (America/New_York) during which the strategy is allowed to open
# new positions (new-stop-entry and market-entry signals).  All other strategy
# activity (hypotheses, stop/limit management, exits) runs unrestricted.
# Format: "HH:MM-HH:MM" using 24-hour clock.  Ranges that cross midnight are
# supported (e.g. "22:00-02:00").  Leave the list empty to block all entries.
ENTRY_ALLOWED_WINDOWS: list[str] = ["01:00-04:00","09:35-11:00","12:00-15:00","23:00-00:00"]


def is_entry_allowed(t: datetime.time) -> bool:
    """Return True if time t falls within any ENTRY_ALLOWED_WINDOWS interval."""
    for window in ENTRY_ALLOWED_WINDOWS:
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
