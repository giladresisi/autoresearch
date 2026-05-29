# tests/test_smt_daily.py
# Unit tests for daily.py run_daily_fixed: fixed-for-the-day liquidities
# (TDO, TWO, prev2-day levels, unvisited 1hr/4hr FVGs), ATH update, and
# estimated_dir/opposite_premove. run_daily_fixed performs NO hypothesis or
# position resets.

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pytest

import smt_state
from smt_state import (
    load_daily,
    load_global,
    load_hypothesis,
    load_position,
    save_global,
    save_hypothesis,
    save_position,
)

from daily import run_daily_fixed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drange(start_naive_et: str, periods: int, freq: str = "1min") -> pd.DatetimeIndex:
    """Build a DatetimeIndex in America/New_York from a naive ET time string.

    Always pass a string WITHOUT a UTC offset so pandas can localize it cleanly
    to America/New_York without conflicting inference.
    """
    return pd.date_range(start_naive_et, periods=periods, freq=freq,
                         tz="America/New_York")


def make_bars(
    start_naive_et: str,
    periods: int,
    freq: str = "1min",
    base_price: float = 21000.0,
    high_offset: float = 5.0,
    low_offset: float = 5.0,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame with a tz-aware (America/New_York) DatetimeIndex.

    `start_naive_et` must be a naive datetime string, e.g. '2026-04-27 09:00:00'.
    """
    idx = _drange(start_naive_et, periods, freq)
    n = len(idx)
    return pd.DataFrame(
        {
            "Open":   np.full(n, base_price),
            "High":   np.full(n, base_price + high_offset),
            "Low":    np.full(n, base_price - low_offset),
            "Close":  np.full(n, base_price + 1.0),
            "Volume": np.ones(n),
        },
        index=idx,
    )


def _now(date_str: str = "2026-04-27", hour: int = 9, minute: int = 20) -> datetime.datetime:
    """Return a tz-aware ET datetime for the given date/hour/minute."""
    import pytz
    et = pytz.timezone("America/New_York")
    return et.localize(datetime.datetime(
        *[int(x) for x in date_str.split("-")], hour, minute, 0
    ))


def _make_empty_hourly() -> pd.DataFrame:
    """Minimal 5-bar hourly DataFrame with no FVGs."""
    idx = _drange("2026-04-25 09:00:00", 5, "h")
    return pd.DataFrame(
        {
            "Open":   np.full(5, 21000.0),
            "High":   np.full(5, 21005.0),
            "Low":    np.full(5, 20995.0),
            "Close":  np.full(5, 21001.0),
            "Volume": np.ones(5),
        },
        index=idx,
    )


def _empty_4hr() -> pd.DataFrame:
    """Empty 4hr frame (no FVGs)."""
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ---------------------------------------------------------------------------
# Isolation fixture: redirect all four smt_state paths into tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")


# ---------------------------------------------------------------------------
# Fixtures: standard day data
# ---------------------------------------------------------------------------

@pytest.fixture()
def standard_day():
    """
    Produce a standard fixture day (2026-04-27, Monday) with enough bars to
    exercise TDO/TWO/prev-day levels and at least one 1hr FVG.

    Returns (now, today, hist_mnq_1m, hist_1hr, hist_4hr).
    """
    now = _now("2026-04-27", 9, 20)
    today = now.date()

    # hist_mnq_1m: Sunday 18:00 ET overnight (futures week start) + prior days
    hist_overnight = make_bars("2026-04-26 18:00:00", periods=900, freq="1min",
                               base_price=21000.0)
    hist_fri = make_bars("2026-04-24 09:00:00", periods=480, freq="1min",
                         base_price=20900.0)
    hist_thu = make_bars("2026-04-23 09:00:00", periods=480, freq="1min",
                         base_price=20800.0)

    hist_mnq_1m = pd.concat([hist_thu, hist_fri, hist_overnight]).sort_index()
    hist_mnq_1m = hist_mnq_1m[~hist_mnq_1m.index.duplicated(keep="last")]

    # Build hist_1hr with a bullish FVG at bars[0..2]:
    #   bar[0].High=21000, bar[2].Low=21020 → gap [21000, 21020]
    idx_1h = _drange("2026-04-25 09:00:00", 10, "h")
    highs = [21000.0, 21010.0, 21025.0] + [21015.0] * 7
    lows  = [20995.0, 20998.0, 21020.0] + [21005.0] * 7  # bar[2].Low=21020 > bar[0].High=21000

    hist_1hr = pd.DataFrame(
        {
            "Open":   [21000.0] * 10,
            "High":   highs,
            "Low":    lows,
            "Close":  [21001.0] * 10,
            "Volume": [100.0] * 10,
        },
        index=idx_1h,
    )

    hist_4hr = _empty_4hr()

    return now, today, hist_mnq_1m, hist_1hr, hist_4hr


# ---------------------------------------------------------------------------
# Tests: fixed liquidity names
# ---------------------------------------------------------------------------

class TestWritesFixedLiquidityNames:
    def test_writes_tdo_two_prev_levels_and_fvg(self, standard_day):
        now, today, hist_mnq_1m, hist_1hr, hist_4hr = standard_day
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_names = {entry["name"] for entry in daily["liquidities"]}

        # Fixed-for-the-day reference levels run_daily_fixed always computes.
        required_names = {"TDO", "TWO"}
        for name in required_names:
            assert name in liq_names, f"Missing liquidity: {name}"

        # Prior trading days: prev1 (Friday 2026-04-24) high/low/TDO present.
        assert "prev1_day_high" in liq_names
        assert "prev1_day_low" in liq_names

        # At least one fvg_* entry (the bullish 1hr FVG in the fixture).
        fvg_names = [n for n in liq_names if n.startswith("fvg_")]
        assert len(fvg_names) >= 1, "Expected at least one fvg_* entry in liquidities"

    def test_does_not_write_dynamic_levels(self, standard_day):
        """run_daily_fixed must NOT write day/week/session H/L (those are per-bar now)."""
        now, today, hist_mnq_1m, hist_1hr, hist_4hr = standard_day
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_names = {entry["name"] for entry in daily["liquidities"]}

        for forbidden in (
            "day_high", "day_low", "week_high", "week_low",
            "asia_high", "asia_low", "london_high", "london_low",
            "ny_morning_high", "ny_morning_low",
            "ny_evening_high", "ny_evening_low",
        ):
            assert forbidden not in liq_names, (
                f"run_daily_fixed should not write dynamic level {forbidden}"
            )


# ---------------------------------------------------------------------------
# Tests: TWO (True Week Open)
# ---------------------------------------------------------------------------

class TestTWOIsFirstBarOfWeek:
    def test_two_is_monday_1800_bar_open_of_week(self):
        """TWO should be the Open of the Monday 18:00 ET bar (futures True Week Open)."""
        now = _now("2026-04-27", 9, 20)
        today = now.date()

        # Monday 2026-04-27 18:00 ET is the True Week Open per _compute_two.
        expected_open = 21050.0

        # Build hist_mnq_1m spanning the week start through Monday 18:00+.
        # Start Sunday 18:00 so the futures week is fully represented, with
        # the Monday 18:00 bar carrying the special open.
        hist_idx = _drange("2026-04-26 18:00:00", 1500, "1min")
        n = len(hist_idx)
        opens = np.full(n, 21000.0)
        monday_1800 = pd.Timestamp("2026-04-27 18:00:00", tz="America/New_York")
        opens[hist_idx.get_loc(monday_1800)] = expected_open
        hist_mnq_1m = pd.DataFrame(
            {
                "Open":   opens,
                "High":   np.full(n, 21010.0),
                "Low":    np.full(n, 20990.0),
                "Close":  np.full(n, 21001.0),
                "Volume": np.ones(n),
            },
            index=hist_idx,
        )

        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map, "TWO missing from liquidities"
        assert liq_map["TWO"]["price"] == expected_open, (
            f"TWO.price={liq_map['TWO']['price']}, expected {expected_open}"
        )

    def test_two_fallback_to_monday_midnight_when_1800_absent(self):
        """If Monday 18:00 bar is absent, TWO uses Monday 00:00 ET bar."""
        now = _now("2026-04-27", 9, 20)
        today = now.date()
        expected_open = 21060.0

        # hist starts Monday 00:00 (no Monday 18:00 in range)
        hist_idx = _drange("2026-04-27 00:00:00", 600, "1min")
        n = len(hist_idx)
        opens = np.full(n, 21000.0)
        opens[0] = expected_open  # Monday 00:00 bar
        hist_mnq_1m = pd.DataFrame(
            {
                "Open":   opens,
                "High":   np.full(n, 21010.0),
                "Low":    np.full(n, 20990.0),
                "Close":  np.full(n, 21001.0),
                "Volume": np.ones(n),
            },
            index=hist_idx,
        )

        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map
        assert liq_map["TWO"]["price"] == expected_open

    def test_two_monday_session_before_1800_uses_last_weeks_two(self):
        """Monday session before 18:00 ET: TWO falls back to last week's Monday 18:00 bar."""
        # Monday 2026-04-27 at 21:00 ET = Monday's Asia session (before Monday 18:00 bar exists
        # for THIS week, since now < Monday 18:00 ET of next week — but more precisely this
        # test simulates: it's Monday 2026-04-27 at 21:00 ET, this week's Monday 18:00 bar
        # doesn't exist in hist yet, so we expect last week's Monday 18:00 bar to be used).
        # Use Monday 2026-05-04 as the session date (Asia session = Sunday evening in ET).
        # "now" is Sunday 2026-05-03 at 21:00 ET (Monday's Asia session in CME terms).
        # today (cme_session_date) = 2026-05-04 (Monday).
        now = _now("2026-05-03", 21, 0)  # Sunday 21:00 ET = Monday's Asia session
        today = datetime.date(2026, 5, 4)  # Monday

        prev_two_open = 21075.0

        # hist contains last week's Monday 18:00 bar (2026-04-27 18:00) but NOT this week's
        hist_idx = _drange("2026-04-27 18:00:00", 1560, "1min")  # ~26h, ends before Mon 2026-05-04 18:00
        n = len(hist_idx)
        opens = np.full(n, 21000.0)
        prev_mon_1800 = pd.Timestamp("2026-04-27 18:00:00", tz="America/New_York")
        opens[hist_idx.get_loc(prev_mon_1800)] = prev_two_open
        hist_mnq_1m = pd.DataFrame(
            {
                "Open":   opens,
                "High":   np.full(n, 21010.0),
                "Low":    np.full(n, 20990.0),
                "Close":  np.full(n, 21001.0),
                "Volume": np.ones(n),
            },
            index=hist_idx,
        )

        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map, "TWO missing from liquidities"
        assert liq_map["TWO"]["price"] == prev_two_open, (
            f"TWO.price={liq_map['TWO']['price']}, expected {prev_two_open} (last week's TWO)"
        )

    def test_two_monday_after_1800_uses_this_weeks_two(self):
        """Monday session after 18:00 ET: TWO uses this week's Monday 18:00 bar."""
        # Once Monday 18:00 ET has passed and the bar exists, use it (not last week's).
        now = _now("2026-04-27", 18, 5)  # Monday 18:05 ET — after the 18:00 bar
        today = now.date()
        this_two_open = 21090.0

        hist_idx = _drange("2026-04-20 18:00:00", 14405, "1min")  # 2 weeks
        n = len(hist_idx)
        opens = np.full(n, 21000.0)
        this_mon_1800 = pd.Timestamp("2026-04-27 18:00:00", tz="America/New_York")
        opens[hist_idx.get_loc(this_mon_1800)] = this_two_open
        prev_mon_1800 = pd.Timestamp("2026-04-20 18:00:00", tz="America/New_York")
        opens[hist_idx.get_loc(prev_mon_1800)] = 20900.0  # different value — should NOT be used
        hist_mnq_1m = pd.DataFrame(
            {
                "Open":   opens,
                "High":   np.full(n, 21010.0),
                "Low":    np.full(n, 20990.0),
                "Close":  np.full(n, 21001.0),
                "Volume": np.ones(n),
            },
            index=hist_idx,
        )

        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map
        assert liq_map["TWO"]["price"] == this_two_open, (
            f"TWO.price={liq_map['TWO']['price']}, expected {this_two_open} (this week's TWO)"
        )


# ---------------------------------------------------------------------------
# Tests: all-time high update in global.json
# ---------------------------------------------------------------------------

class TestAllTimeHighUpdate:
    def _make_minimal(self, base_price: float = 21000.0, high_offset: float = 5.0):
        now = _now("2026-04-27", 9, 20)
        today = now.date()
        hist_mnq_1m = make_bars("2026-04-27 00:00:00", periods=60,
                                base_price=base_price, high_offset=high_offset,
                                low_offset=5.0)
        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()
        return now, today, hist_mnq_1m, hist_1hr, hist_4hr

    def test_all_time_high_updates_when_today_higher(self):
        save_global({"all_time_high": 100.0, "confidence": "medium", "trend": "up"})
        now, today, hist_mnq_1m, hist_1hr, hist_4hr = self._make_minimal(
            base_price=200.0, high_offset=5.0
        )  # hist high = 205
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)
        g = load_global()
        assert g["all_time_high"] == 205.0

    def test_all_time_high_unchanged_when_today_lower(self):
        save_global({"all_time_high": 100.0, "confidence": "medium", "trend": "up"})
        now, today, hist_mnq_1m, hist_1hr, hist_4hr = self._make_minimal(
            base_price=45.0, high_offset=5.0
        )  # hist high = 50
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)
        g = load_global()
        assert g["all_time_high"] == 100.0


# ---------------------------------------------------------------------------
# Tests: estimated_dir / opposite_premove
# ---------------------------------------------------------------------------

class TestEstimatedDirAndOppositePremove:
    def _run_with_global_trend(self, trend: str):
        save_global({"all_time_high": 0.0, "confidence": "medium", "trend": trend})
        now = _now("2026-04-27", 9, 20)
        today = now.date()
        hist_mnq_1m = make_bars("2026-04-27 00:00:00", periods=60)
        hist_1hr = _make_empty_hourly()
        hist_4hr = _empty_4hr()
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

    def test_estimated_dir_copied_from_global_trend_up(self):
        self._run_with_global_trend("up")
        daily = load_daily()
        assert daily["estimated_dir"] == "up"

    def test_estimated_dir_copied_from_global_trend_down(self):
        self._run_with_global_trend("down")
        daily = load_daily()
        assert daily["estimated_dir"] == "down"

    def test_opposite_premove_hardcoded_no(self):
        self._run_with_global_trend("up")
        daily = load_daily()
        assert daily["opposite_premove"] == "no"


# ---------------------------------------------------------------------------
# Tests: run_daily_fixed performs NO resets
# ---------------------------------------------------------------------------

class TestNoResets:
    def test_run_daily_fixed_does_not_reset_hypothesis(self, standard_day):
        save_hypothesis({
            "direction": "up",
            "weekly_mid": "above",
            "daily_mid": "mid",
            "last_liquidity": "day_low",
            "divs": [{"type": "wick"}],
            "targets": [{"name": "week_high", "price": 21450.0}],
            "cautious_price": "21410.0",
            "entry_ranges": [],
        })

        now, today, hist_mnq_1m, hist_1hr, hist_4hr = standard_day
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        hyp = load_hypothesis()
        assert hyp["direction"] == "up"
        assert hyp["weekly_mid"] == "above"
        assert hyp["daily_mid"] == "mid"
        assert hyp["last_liquidity"] == "day_low"
        assert len(hyp["divs"]) == 1
        assert len(hyp["targets"]) == 1

    def test_run_daily_fixed_does_not_reset_position(self, standard_day):
        save_position({
            "active": {"fill_price": 21400.0, "direction": "up"},
            "stop_entry": 21000.0,
            "stop_direction": "up",
            "conf_bar_entry": {"high": 21410.0, "low": 21390.0},
            "conf_bar_exit": {},
            "pending_stop": None,
            "failed_entries": 2,
            "session_mid_crosses": 1,
        })

        now, today, hist_mnq_1m, hist_1hr, hist_4hr = standard_day
        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        pos = load_position()
        assert pos["active"] == {"fill_price": 21400.0, "direction": "up"}
        assert pos["stop_entry"] == 21000.0
        assert pos["conf_bar_entry"] == {"high": 21410.0, "low": 21390.0}
        assert pos["failed_entries"] == 2


# ---------------------------------------------------------------------------
# Tests: 4hr FVG detection
# ---------------------------------------------------------------------------

class TestFourHourFvgDetection:
    def test_run_daily_fixed_4hr_fvg_detected(self):
        """A bullish 3-bar 4hr FVG is detected and written to liquidities."""
        now = _now("2026-04-27", 9, 20)
        today = now.date()

        # 4hr bars with a bullish FVG: bar[2].Low(21120) > bar[0].High(21100)
        #   → gap bottom=21100, top=21120
        idx_4hr = _drange("2026-04-25 00:00:00", 3, "4h")
        hist_4hr = pd.DataFrame(
            {
                "Open":  [21090.0, 21105.0, 21118.0],
                "High":  [21100.0, 21110.0, 21130.0],
                "Low":   [21080.0, 21100.0, 21120.0],
                "Close": [21095.0, 21108.0, 21125.0],
                "Volume": [100.0] * 3,
            },
            index=idx_4hr,
        )

        # hist_1hr with no FVG so the only fvg_* entry comes from hist_4hr.
        hist_1hr = _make_empty_hourly()

        # hist_mnq_1m minimal so TDO/TWO can be computed; no bar fills the 4hr gap.
        idx_1m = _drange("2026-04-27 00:00:00", 60, "1min")
        hist_mnq_1m = pd.DataFrame(
            {
                "Open":  np.full(60, 21000.0),
                "High":  np.full(60, 21005.0),
                "Low":   np.full(60, 20995.0),
                "Close": np.full(60, 21001.0),
                "Volume": np.ones(60),
            },
            index=idx_1m,
        )

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        state = load_daily()
        fvgs = [l for l in state["liquidities"] if l.get("kind") == "fvg"]
        assert len(fvgs) >= 1, f"Expected at least 1 FVG, got {fvgs}"

        bull_fvg = next(
            (f for f in fvgs if abs(f.get("bottom", 0) - 21100.0) < 1.0), None
        )
        assert bull_fvg is not None, f"No bullish 4hr FVG found in {fvgs}"
        assert bull_fvg["top"] == pytest.approx(21120.0, abs=0.1)
        assert bull_fvg["bottom"] == pytest.approx(21100.0, abs=0.1)


# ---------------------------------------------------------------------------
# Tests: unvisited FVG filtering
# ---------------------------------------------------------------------------

class TestUnvisitedFvgFilter:
    def test_unvisited_fvg_filter_excludes_filled_gaps(self):
        """
        Set up two 1hr FVGs:
          - FVG1 at bars[0..2]: bullish gap [21000, 21020] — filled by a later 1m bar
          - FVG2 at bars[3..5]: bullish gap [21100, 21120] — NOT filled (unvisited)

        Only FVG2 should appear in liquidities.
        """
        now = _now("2026-04-27", 9, 20)
        today = now.date()

        # 1hr bars (6 bars starting 2026-04-25 09:00 ET)
        # FVG1: bars[2].Low(21020) > bars[0].High(21000) → bullish gap [21000, 21020]
        # FVG2: bars[5].Low(21120) > bars[3].High(21100) → bullish gap [21100, 21120]
        idx_1h = _drange("2026-04-25 09:00:00", 6, "h")
        highs = [21000.0, 21010.0, 21025.0, 21100.0, 21110.0, 21125.0]
        lows  = [20995.0, 20998.0, 21020.0, 21095.0, 21098.0, 21120.0]

        hist_1hr = pd.DataFrame(
            {
                "Open":   [21000.0] * 6,
                "High":   highs,
                "Low":    lows,
                "Close":  [21001.0] * 6,
                "Volume": [100.0] * 6,
            },
            index=idx_1h,
        )

        # FVG1 formation timestamp = idx_1h[2] (bar[2] creates the gap)
        # FVG2 formation timestamp = idx_1h[5]
        fvg1_formation_ts = idx_1h[2]
        fvg2_formation_ts = idx_1h[5]

        # After FVG1 formation: a 1m bar that FILLS FVG1 (re-enters [21000, 21020])
        fill_ts = fvg1_formation_ts + pd.Timedelta(minutes=5)
        fill_bar = pd.DataFrame(
            {"Open": [21010.0], "High": [21012.0], "Low": [20999.0],
             "Close": [21011.0], "Volume": [10.0]},
            index=pd.DatetimeIndex([fill_ts], tz="America/New_York"),
        )

        # After FVG2 formation: a 1m bar that does NOT fill FVG2 (stays below 21100)
        safe_ts = fvg2_formation_ts + pd.Timedelta(minutes=5)
        safe_bar = pd.DataFrame(
            {"Open": [21090.0], "High": [21095.0], "Low": [21088.0],
             "Close": [21091.0], "Volume": [10.0]},
            index=pd.DatetimeIndex([safe_ts], tz="America/New_York"),
        )

        # Today's 1m bars
        today_1m = make_bars("2026-04-27 09:00:00", periods=30, base_price=21000.0)

        # hist_mnq_1m: combine fill_bar, safe_bar, plus today
        hist_mnq_1m = pd.concat([fill_bar, safe_bar, today_1m]).sort_index()
        hist_mnq_1m = hist_mnq_1m[~hist_mnq_1m.index.duplicated(keep="last")]

        hist_4hr = _empty_4hr()

        run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)

        daily = load_daily()
        fvg_entries = [e for e in daily["liquidities"] if e["name"].startswith("fvg_")]

        # Only FVG2 should be unvisited
        assert len(fvg_entries) == 1, (
            f"Expected 1 unvisited FVG, got {len(fvg_entries)}: "
            f"{[e['name'] for e in fvg_entries]}"
        )
        # Verify it's the FVG2 zone [21100, 21120]
        surviving = fvg_entries[0]
        assert surviving["bottom"] == 21100.0
        assert surviving["top"] == 21120.0


# ---------------------------------------------------------------------------
# Tests for compute_live_hl_mid: extended day H/L lookback during Asia/London
# ---------------------------------------------------------------------------

def test_compute_live_hl_mid_asia_extends_to_0600():
    """During Asia session (≥18:00 ET) day_high includes bars back to 06:00 ET."""
    from hypothesis import compute_live_hl_mid

    idx = pd.date_range("2025-11-13 06:00", periods=4, freq="6h", tz="America/New_York")
    # Bar at 06:00 has extreme High=25000; bar at 18:00 starts the Asia session
    bars = pd.DataFrame({
        "Open":  [21000.0] * 4,
        "High":  [25000.0, 21010.0, 21010.0, 21010.0],
        "Low":   [20990.0] * 4,
        "Close": [21002.0] * 4,
    }, index=idx)

    now = pd.Timestamp("2025-11-13 21:00", tz="America/New_York")  # Asia session
    result = compute_live_hl_mid(bars, now)

    assert "day_high" in result
    assert result["day_high"] == 25000.0, (
        f"Asia session day_high should include 06:00 ET bar (25000), got {result['day_high']}"
    )


def test_compute_live_hl_mid_london_extends_to_0600():
    """During London session (<06:00 ET) day_high includes bars back to 06:00 ET previous day."""
    from hypothesis import compute_live_hl_mid

    # Bars: session-open day 06:00, 12:00, 18:00 (Asia start), then 00:00 next day (London)
    idx = pd.DatetimeIndex([
        pd.Timestamp("2025-11-13 06:00", tz="America/New_York"),  # prev NY morning
        pd.Timestamp("2025-11-13 18:00", tz="America/New_York"),  # Asia start
        pd.Timestamp("2025-11-14 00:00", tz="America/New_York"),  # London start
        pd.Timestamp("2025-11-14 03:00", tz="America/New_York"),  # mid London
    ])
    bars = pd.DataFrame({
        "Open":  [21000.0] * 4,
        "High":  [25000.0, 21010.0, 21010.0, 21010.0],
        "Low":   [20990.0] * 4,
        "Close": [21002.0] * 4,
    }, index=idx)

    now = pd.Timestamp("2025-11-14 03:00", tz="America/New_York")  # London session
    result = compute_live_hl_mid(bars, now)

    assert "day_high" in result
    assert result["day_high"] == 25000.0, (
        f"London session day_high should include 06:00 ET bar (25000), got {result['day_high']}"
    )


def test_compute_live_hl_mid_ny_morning_excludes_0600():
    """During NY morning (06:00–18:00 ET) day_high starts at 18:00 ET, 06:00 excluded."""
    from hypothesis import compute_live_hl_mid

    idx = pd.DatetimeIndex([
        pd.Timestamp("2025-11-13 06:00", tz="America/New_York"),  # prev NY morning — excluded
        pd.Timestamp("2025-11-13 18:00", tz="America/New_York"),  # Asia start
        pd.Timestamp("2025-11-14 00:00", tz="America/New_York"),  # London
        pd.Timestamp("2025-11-14 09:00", tz="America/New_York"),  # NY morning
    ])
    bars = pd.DataFrame({
        "Open":  [21000.0] * 4,
        "High":  [25000.0, 21010.0, 21010.0, 21010.0],
        "Low":   [20990.0] * 4,
        "Close": [21002.0] * 4,
    }, index=idx)

    now = pd.Timestamp("2025-11-14 09:00", tz="America/New_York")  # NY morning
    result = compute_live_hl_mid(bars, now)

    assert "day_high" in result
    assert result["day_high"] < 25000.0, (
        f"NY morning day_high should NOT include 06:00 ET bar (25000), got {result['day_high']}"
    )
