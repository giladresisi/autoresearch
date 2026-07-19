# tests/test_trading_calendar.py
# Unit tests for data.trading_calendar — holiday-aware CME Globex calendar helpers.
import pandas as pd

from data.trading_calendar import is_market_closed, prev_trading_close

ET = "America/New_York"


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=ET)


# ── is_market_closed ─────────────────────────────────────────────────────────

def test_open_weekday_minute_is_not_closed():
    assert not is_market_closed(_ts("2026-06-16 10:30"))  # Tuesday RTH


def test_daily_maintenance_break_is_closed():
    assert is_market_closed(_ts("2026-06-16 17:30"))  # Tue 17:00-18:00 ET


def test_weekend_is_closed():
    assert is_market_closed(_ts("2026-06-13 12:00"))  # Saturday
    assert is_market_closed(_ts("2026-06-14 17:59"))  # Sunday pre-open
    assert not is_market_closed(_ts("2026-06-14 18:00"))  # Sunday reopen


def test_juneteenth_early_close_afternoon_is_closed():
    assert is_market_closed(_ts("2026-06-19 14:00"))  # Fri Jun 19, closed from 13:00


def test_ordinary_friday_afternoon_is_open():
    assert not is_market_closed(_ts("2026-06-12 14:00"))  # ordinary Friday


# ── prev_trading_close ───────────────────────────────────────────────────────

def test_prev_close_passthrough_when_open():
    ts = _ts("2026-06-16 10:30")
    assert prev_trading_close(ts) == ts


def test_prev_close_sunday_noon_is_friday_close():
    assert prev_trading_close(_ts("2026-06-14 12:00")) == _ts("2026-06-12 17:00")


def test_prev_close_daily_break_is_same_day_close():
    assert prev_trading_close(_ts("2026-06-17 17:30")) == _ts("2026-06-17 17:00")


def test_prev_close_saturday_after_july3_early_close():
    assert prev_trading_close(_ts("2026-07-04 09:00")) == _ts("2026-07-03 13:00")


def test_prev_close_juneteenth_afternoon_is_1pm():
    assert prev_trading_close(_ts("2026-06-19 15:00")) == _ts("2026-06-19 13:00")
