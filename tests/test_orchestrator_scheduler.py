import datetime
from zoneinfo import ZoneInfo

from orchestrator.scheduler import is_trading_day, next_session_open

ET = ZoneInfo("America/New_York")


def test_is_trading_day_weekday():
    assert is_trading_day(datetime.date(2026, 4, 20)) is True


def test_is_trading_day_saturday():
    assert is_trading_day(datetime.date(2026, 4, 18)) is False


def test_is_trading_day_sunday():
    assert is_trading_day(datetime.date(2026, 4, 19)) is False


def test_is_trading_day_mlk_day_2026():
    assert is_trading_day(datetime.date(2026, 1, 19)) is False


def test_is_trading_day_july4_2025():
    assert is_trading_day(datetime.date(2025, 7, 4)) is False


def test_next_session_open_before_open_today():
    now = datetime.datetime(2026, 4, 21, 8, 0, tzinfo=ET)
    result = next_session_open(now)
    assert result == datetime.datetime(2026, 4, 21, 9, 0, tzinfo=ET)


def test_next_session_open_after_open_today():
    now = datetime.datetime(2026, 4, 21, 10, 0, tzinfo=ET)
    result = next_session_open(now)
    assert result == datetime.datetime(2026, 4, 22, 9, 0, tzinfo=ET)


def test_next_session_open_weekend():
    now = datetime.datetime(2026, 4, 18, 12, 0, tzinfo=ET)
    result = next_session_open(now)
    assert result == datetime.datetime(2026, 4, 20, 9, 0, tzinfo=ET)
