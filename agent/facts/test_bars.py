import pandas as pd
import pytest
from agent.facts.bars import resample


def _bars(start: str, n: int, freq: str = "1min") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq=freq, tz="America/New_York")
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1.0},
        index=idx,
    )


def test_resample_5m_drops_trailing_partial_bin():
    """A 5m bin with only 3 of 5 minutes present must NOT be emitted."""
    df = _bars("2026-08-13 09:30", n=8)          # 09:30..09:37 → 09:30 full, 09:35 partial
    out = resample(df, "5min")
    assert list(out.index.strftime("%H:%M")) == ["09:30"]


def test_resample_5m_left_labeled_closed_left():
    df = _bars("2026-08-13 09:30", n=10)
    out = resample(df, "5min")
    assert list(out.index.strftime("%H:%M")) == ["09:30", "09:35"]


def test_resample_bins_never_span_the_maintenance_break():
    """16:55-18:00 is a hole; no bin may contain bars from both sides of it."""
    pre = _bars("2026-08-13 16:51", n=4)          # 16:51..16:54
    post = _bars("2026-08-13 18:00", n=5)         # 18:00..18:04
    out = resample(pd.concat([pre, post]), "5min")
    stamps = set(out.index.strftime("%H:%M"))
    assert "16:50" not in stamps, "pre-maintenance partial bin must not be emitted"
    assert "18:00" in stamps


def test_resample_4h_anchored_to_session_open_1800():
    """4h bins align to 18:00 ET, not to midnight."""
    df = _bars("2026-08-12 18:00", n=60 * 9, freq="1min")
    out = resample(df, "4h")
    assert out.index[0].strftime("%H:%M") == "18:00"
    assert out.index[1].strftime("%H:%M") == "22:00"


def test_resample_ohlc_aggregation_is_correct():
    idx = pd.date_range("2026-08-13 09:30", periods=5, freq="1min", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": [10, 11, 12, 13, 14], "High": [20, 21, 22, 23, 24],
         "Low": [1, 2, 3, 4, 5], "Close": [15, 16, 17, 18, 19], "Volume": [1] * 5},
        index=idx,
    )
    out = resample(df, "5min")
    assert out.iloc[0]["Open"] == 10 and out.iloc[0]["Close"] == 19
    assert out.iloc[0]["High"] == 24 and out.iloc[0]["Low"] == 1


def test_resample_empty_frame_returns_empty_not_raise():
    out = resample(_bars("2026-08-13 09:30", n=0), "5min")
    assert len(out) == 0


# --- added during implementation (not in the plan) --------------------------- #

def test_unsupported_timeframe_raises():
    with pytest.raises(ValueError):
        resample(_bars("2026-08-13 09:30", n=10), "3min")


def test_bins_carry_the_session_open_bar_after_the_break():
    """The bin immediately after 18:00 must start AT 18:00, never at 17:xx."""
    pre = _bars("2026-08-13 16:00", n=55)          # 16:00..16:54
    post = _bars("2026-08-13 18:00", n=60)         # 18:00..18:59
    out = resample(pd.concat([pre, post]), "1h")
    stamps = list(out.index.strftime("%m-%d %H:%M"))
    assert "08-13 18:00" in stamps
    assert all(not s.startswith("08-13 17") for s in stamps)
