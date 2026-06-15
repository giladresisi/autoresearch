import datetime
from zoneinfo import ZoneInfo

from orchestrator.scheduler import is_session_open_day, is_trading_day, next_session_open
from session_times import SESSION_OPEN

ET = ZoneInfo("America/New_York")

# Reference week (2026-04): Fri 04-17, Sat 04-18, Sun 04-19, Mon 04-20, Tue 04-21, Wed 04-22.


def _open(date: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(date, SESSION_OPEN, tzinfo=ET)


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


# --- is_session_open_day: a CME session opens this evening iff tomorrow is a trade date ---

def test_is_session_open_day_sunday_opens_monday_session():
    # Sunday evening opens the Monday trade-date session.
    assert is_session_open_day(datetime.date(2026, 4, 19)) is True


def test_is_session_open_day_weekday():
    assert is_session_open_day(datetime.date(2026, 4, 20)) is True


def test_is_session_open_day_friday_closed():
    # Friday evening is the CME weekend close — no session opens.
    assert is_session_open_day(datetime.date(2026, 4, 17)) is False


def test_is_session_open_day_saturday_closed():
    assert is_session_open_day(datetime.date(2026, 4, 18)) is False


# --- next_session_open: returns the next evening open (SESSION_OPEN ET), not 09:00 ---

def test_next_session_open_before_open_today():
    # Monday before the evening open → today's evening open.
    now = datetime.datetime(2026, 4, 20, 8, 0, tzinfo=ET)
    assert next_session_open(now) == _open(datetime.date(2026, 4, 20))


def test_next_session_open_after_open_today():
    # Monday after the evening open → next session day (Tuesday) open.
    now = datetime.datetime(2026, 4, 20, 19, 0, tzinfo=ET)
    assert next_session_open(now) == _open(datetime.date(2026, 4, 21))


def test_next_session_open_sunday_opens_today():
    # Sunday afternoon → Sunday evening open (the Monday trade-date session).
    now = datetime.datetime(2026, 4, 19, 12, 0, tzinfo=ET)
    assert next_session_open(now) == _open(datetime.date(2026, 4, 19))


def test_next_session_open_friday_evening_skips_to_sunday():
    # Friday after the close → skips the closed Fri/Sat evenings to the Sunday open.
    now = datetime.datetime(2026, 4, 17, 19, 0, tzinfo=ET)
    assert next_session_open(now) == _open(datetime.date(2026, 4, 19))


def test_next_session_open_saturday_skips_to_sunday():
    now = datetime.datetime(2026, 4, 18, 12, 0, tzinfo=ET)
    assert next_session_open(now) == _open(datetime.date(2026, 4, 19))
