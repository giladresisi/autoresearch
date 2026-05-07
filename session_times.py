# session_times.py
# Single source of truth for V2 trading session window times (America/New_York).
import datetime

SESSION_OPEN  = datetime.time(9, 20)   # V2 daily trigger; run_daily fires, bars start
SESSION_CLOSE = datetime.time(16, 0)   # session end; positions force-closed, no new signals
