import datetime

import pandas as pd
import pytest

from agent.study.sessions import SessionBars, CONTRACT_SUBDIR, TZ


@pytest.fixture(scope="module")
def sb():
    return SessionBars()


def test_the_contract_subdirectory_is_used_not_the_toplevel_parquet(sb):
    """The top-level parquet stops at 2026-06-12; reading it silently loses most of
    the study range. Pin that the resolved path is the per-contract one."""
    for ticker in ("MNQ", "MES"):
        assert CONTRACT_SUBDIR in str(sb.source_path(ticker))


def test_dates_covers_the_87_full_sessions(sb):
    dates = sb.dates()
    assert len(dates) == 87
    assert dates[0] == datetime.date(2026, 5, 1)
    assert dates[-1] == datetime.date(2026, 8, 31)
    assert all(d.weekday() < 5 for d in dates)


def test_dates_are_sessions_with_a_full_window_on_every_ticker(sb):
    """A session present for MNQ but short on MES must not be listed: a one-sided
    session would silently produce a skeleton with no MES candidates behind it."""
    dates = set(sb.dates())
    for ticker in ("MNQ", "MES"):
        for d in dates:
            assert len(sb.window_1m(ticker, d)) > 0, (ticker, d)


def test_window_1m_is_bounded_to_0930_1300_et(sb):
    w = sb.window_1m("MNQ", datetime.date(2026, 8, 13))
    et = w.index.tz_convert(TZ)
    assert et.min().hour == 9 and et.min().minute == 30
    assert et.max().hour <= 13
    assert (et.max() - et.min()) <= pd.Timedelta(hours=3, minutes=30)


def test_window_5m_yields_42_complete_bins_for_a_full_session(sb):
    """3.5 hours of complete, left-labelled 5m bins."""
    f = sb.window_5m("MNQ", datetime.date(2026, 8, 13))
    assert len(f) == 42
    assert list(f.columns[:4]) == ["Open", "High", "Low", "Close"]
    assert f.index.tz_convert(TZ)[0].strftime("%H:%M") == "09:30"


def test_an_unknown_date_returns_an_empty_frame_rather_than_raising(sb):
    assert len(sb.window_1m("MNQ", datetime.date(1999, 1, 4))) == 0
    assert len(sb.window_5m("MNQ", datetime.date(1999, 1, 4))) == 0
