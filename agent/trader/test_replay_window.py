# agent/trader/test_replay_window.py
import pandas as pd
import pytest

from backtest_smt import replay_slice

TZ = "America/New_York"


def _bars(start, periods, freq):
    idx = pd.date_range(start, periods=periods, freq=freq, tz=TZ)
    return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
                         "Volume": 10.0}, index=idx)


def _session():
    """A session open at 18:00 the prior day through 11:30 the next morning."""
    bars_1s = _bars("2026-08-11 18:00:00", periods=int(17.5 * 3600), freq="1s")
    hist_1m = _bars("2026-07-12 18:00:00", periods=30 * 24 * 60, freq="1min")
    hist_1m = hist_1m[hist_1m.index < pd.Timestamp("2026-08-11 18:00", tz=TZ)]
    return bars_1s, hist_1m


def test_the_slice_starts_at_the_window_start():
    b, h = _session()
    s, _ = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                        window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert s.index[0] == pd.Timestamp("2026-08-12 09:20:00", tz=TZ)


def test_the_slice_ends_at_the_window_end_exclusive():
    b, h = _session()
    s, _ = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                        window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert s.index[-1] < pd.Timestamp("2026-08-12 11:00:00", tz=TZ)


def test_the_history_is_extended_to_the_window_start():
    """THE critical assertion. Without this the overnight session vanishes from the
    Analyzer's facts and the Executor's store -- the cycle-1 B2 defect, recurring."""
    b, h = _session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert hx.index[-1] >= pd.Timestamp("2026-08-12 09:19:00", tz=TZ)


def test_the_extended_history_covers_the_overnight_session_with_no_gap():
    b, h = _session()
    s, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    covered = hx[(hx.index >= pd.Timestamp("2026-08-11 18:00", tz=TZ)) &
                 (hx.index < pd.Timestamp("2026-08-12 09:20", tz=TZ))]
    assert len(covered) > 900, "the 15h20m overnight session must be present as 1m bars"


def test_history_and_slice_meet_without_overlap():
    """`_frames` concatenates hist[index < today[0]] + today; an overlap would duplicate
    bars and a gap would lose them."""
    b, h = _session()
    s, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert hx.index[-1] < s.index[0]


def test_the_extended_history_is_1m_resolution_not_1s():
    b, h = _session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    deltas = hx.index.to_series().diff().dropna().unique()
    assert all(d >= pd.Timedelta(minutes=1) for d in deltas)


def test_a_window_start_at_the_session_open_leaves_history_untouched():
    b, h = _session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-11 18:00", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert hx.index[-1] == h.index[-1]


def test_an_empty_window_returns_empty_bars_without_raising():
    b, h = _session()
    s, _ = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 20:00", tz=TZ),
                        window_end=pd.Timestamp("2026-08-12 21:00", tz=TZ))
    assert len(s) == 0


def _realistic_session():
    """The shape the REAL 1s parquet has, which `_session` does not reproduce.

    `hist_1m` is resampled FROM the same 1s bars, so its last label (16:59) still has 59
    seconds of 1s bars sitting inside it. `_session` builds hist from a separate
    `date_range` that stops cleanly at 18:00, so no 1s bar ever falls inside hist's final
    minute -- which is precisely why it missed the duplicate-bar defect.
    """
    bars_1s = _bars("2026-08-11 16:59:00", periods=int(16.5 * 3600), freq="1s")
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last",
           "Volume": "sum"}
    hist_1m = bars_1s.resample("1min", label="left").agg(agg).dropna(subset=["Open"])
    hist_1m = hist_1m[hist_1m.index <= pd.Timestamp("2026-08-11 16:59", tz=TZ)]
    return bars_1s, hist_1m


def test_the_bridge_does_not_duplicate_the_hists_final_minute():
    """REGRESSION. `hist_end` is a bin LABEL, not the bin's end. Selecting 1s source bars
    with `index > hist_end` readmits the 59 seconds of a minute hist already carries, and
    the resample re-stamps them onto the SAME label -- a duplicate 1m row with a truncated
    Open and Volume, on the frame handed straight to SessionPipeline."""
    b, h = _realistic_session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    assert hx.index.is_unique, "a duplicated bar makes reindex/asof raise inside the graft"


def test_the_bridge_preserves_the_hists_own_final_bar_untouched():
    b, h = _realistic_session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    last = h.index[-1]
    assert hx.loc[last]["Volume"] == h.loc[last]["Volume"]


def test_the_bridge_still_starts_at_the_minute_after_hist_ends():
    """The duplicate fix must not open a GAP -- the bridge has to resume at hist_end+1m."""
    b, h = _realistic_session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    after = hx[hx.index > h.index[-1]]
    assert after.index[0] == h.index[-1] + pd.Timedelta(minutes=1)


def test_the_ohlc_of_the_resampled_history_aggregates_correctly():
    b, h = _session()
    _, hx = replay_slice(b, h, window_start=pd.Timestamp("2026-08-12 09:20", tz=TZ),
                         window_end=pd.Timestamp("2026-08-12 11:00", tz=TZ))
    row = hx.loc[pd.Timestamp("2026-08-12 09:00:00", tz=TZ)]
    assert row["High"] == 2.0 and row["Low"] == 0.5
