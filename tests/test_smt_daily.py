# tests/test_smt_daily.py
# Unit tests for daily.py: liquidities computation, ATH update, position reset,
# hypothesis reset, and FVG unvisited filtering.

from __future__ import annotations

import datetime
import json

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

import daily as daily_mod
from daily import run_daily


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
    exercise all session windows and at least one 1hr FVG.

    Returns (now, mnq_1m, hist_mnq_1m, hist_hourly_mnq).
    """
    now = _now("2026-04-27", 9, 20)

    # Today's 1m bars: 09:00 → 17:00 ET (480 bars)
    mnq_1m = make_bars("2026-04-27 09:00:00", periods=480, freq="1min",
                       base_price=21000.0)

    # hist_mnq_1m: Sunday 18:00 ET overnight (futures week start) + prior days
    hist_overnight = make_bars("2026-04-26 18:00:00", periods=900, freq="1min",
                               base_price=21000.0)
    hist_fri = make_bars("2026-04-24 09:00:00", periods=480, freq="1min",
                         base_price=20900.0)
    hist_thu = make_bars("2026-04-23 09:00:00", periods=480, freq="1min",
                         base_price=20800.0)

    hist_mnq_1m = pd.concat([hist_thu, hist_fri, hist_overnight]).sort_index()
    hist_mnq_1m = hist_mnq_1m[~hist_mnq_1m.index.duplicated(keep="last")]

    # Build hist_hourly_mnq with a bullish FVG at bars[0..2]:
    #   bar[0].High=21000, bar[2].Low=21020 → gap [21000, 21020]
    idx_1h = _drange("2026-04-25 09:00:00", 10, "h")
    highs = [21000.0, 21010.0, 21025.0] + [21015.0] * 7
    lows  = [20995.0, 20998.0, 21020.0] + [21005.0] * 7  # bar[2].Low=21020 > bar[0].High=21000

    hist_hourly_mnq = pd.DataFrame(
        {
            "Open":   [21000.0] * 10,
            "High":   highs,
            "Low":    lows,
            "Close":  [21001.0] * 10,
            "Volume": [100.0] * 10,
        },
        index=idx_1h,
    )

    return now, mnq_1m, hist_mnq_1m, hist_hourly_mnq


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWritesAllRequiredLiquidityNames:
    def test_writes_all_required_liquidity_names(self, standard_day):
        now, mnq_1m, hist_mnq_1m, hist_hourly_mnq = standard_day
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq)

        daily = load_daily()
        liq_names = {entry["name"] for entry in daily["liquidities"]}

        required_names = {
            "TDO", "TWO",
            "week_high", "week_low",
            "day_high", "day_low",
            "asia_high", "asia_low",
            "london_high", "london_low",
            "ny_morning_high", "ny_morning_low",
            "ny_evening_high", "ny_evening_low",
        }
        for name in required_names:
            assert name in liq_names, f"Missing liquidity: {name}"

        # At least one fvg_* entry
        fvg_names = [n for n in liq_names if n.startswith("fvg_")]
        assert len(fvg_names) >= 1, "Expected at least one fvg_* entry in liquidities"


class TestTWOIsFirstBarOfWeek:
    def test_two_is_first_1m_bar_open_of_week(self):
        """TWO should be the Open of the Sunday 18:00 ET bar (first bar of futures week)."""
        now = _now("2026-04-27", 9, 20)

        # Sunday 2026-04-26 18:00 ET is the futures-week open
        expected_open = 21050.0

        # Build hist_mnq_1m starting at Sunday 18:00 (960 1m bars ≈ 16 hours)
        hist_idx = _drange("2026-04-26 18:00:00", 960, "1min")
        n = len(hist_idx)
        opens = np.full(n, 21000.0)
        opens[0] = expected_open  # Sunday 18:00 bar has the special open
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

        # Today's bars (minimal)
        mnq_1m = make_bars("2026-04-27 09:00:00", periods=30, base_price=21000.0)

        # Minimal hourly bars (no FVG needed for this test)
        hist_hourly = _make_empty_hourly()

        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map, "TWO missing from liquidities"
        assert liq_map["TWO"]["price"] == expected_open, (
            f"TWO.price={liq_map['TWO']['price']}, expected {expected_open}"
        )

    def test_two_fallback_to_monday_when_sunday_absent(self):
        """If Sunday 18:00 bar is absent, TWO uses Monday 00:00 ET bar."""
        now = _now("2026-04-27", 9, 20)
        expected_open = 21060.0

        # hist starts Monday 00:00 (no Sunday 18:00)
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

        mnq_1m = make_bars("2026-04-27 09:00:00", periods=30, base_price=21000.0)
        hist_hourly = _make_empty_hourly()

        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly)

        daily = load_daily()
        liq_map = {e["name"]: e for e in daily["liquidities"]}
        assert "TWO" in liq_map
        assert liq_map["TWO"]["price"] == expected_open


class TestAllTimeHighUpdate:
    def _make_minimal(self, base_price: float = 21000.0, high_offset: float = 5.0):
        now = _now("2026-04-27", 9, 20)
        mnq_1m = make_bars("2026-04-27 09:00:00", periods=60,
                           base_price=base_price, high_offset=high_offset, low_offset=5.0)
        hist_mnq_1m = make_bars("2026-04-27 00:00:00", periods=60, base_price=base_price)
        hist_hourly = _make_empty_hourly()
        return now, mnq_1m, hist_mnq_1m, hist_hourly

    def test_all_time_high_updates_when_today_higher(self):
        save_global({"all_time_high": 100.0, "trend": "up"})
        now, mnq_1m, hist_mnq_1m, hist_hourly = self._make_minimal(
            base_price=200.0, high_offset=5.0
        )  # day_high = 205
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly)
        g = load_global()
        assert g["all_time_high"] == 205.0

    def test_all_time_high_unchanged_when_today_lower(self):
        save_global({"all_time_high": 100.0, "trend": "up"})
        now, mnq_1m, hist_mnq_1m, hist_hourly = self._make_minimal(
            base_price=45.0, high_offset=5.0
        )  # day_high = 50
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly)
        g = load_global()
        assert g["all_time_high"] == 100.0


class TestEstimatedDirAndOppositePremove:
    def _run_with_global_trend(self, trend: str):
        save_global({"all_time_high": 0.0, "trend": trend})
        now = _now("2026-04-27", 9, 20)
        mnq_1m = make_bars("2026-04-27 09:00:00", periods=60)
        hist_mnq_1m = make_bars("2026-04-27 00:00:00", periods=60)
        hist_hourly = _make_empty_hourly()
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly)

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


class TestPositionReset:
    def test_position_per_session_fields_reset(self, standard_day):
        # Preset position.json with non-default values
        save_position({
            "active": {"fill_price": 21400.0, "direction": "up"},
            "limit_entry": 21000.0,
            "confirmation_bar": {"high": 21410.0, "low": 21390.0},
            "failed_entries": 2,
        })

        now, mnq_1m, hist_mnq_1m, hist_hourly_mnq = standard_day
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq)

        pos = load_position()
        assert pos["active"] == {}
        assert pos["limit_entry"] == ""
        assert pos["confirmation_bar"] == {}
        assert pos["failed_entries"] == 0


class TestHypothesisReset:
    def test_hypothesis_direction_set_to_none(self, standard_day):
        # Preset hypothesis with direction="up"
        save_hypothesis({
            "direction": "up",
            "weekly_mid": "above",
            "daily_mid": "mid",
            "last_liquidity": "day_low",
            "divs": [],
            "targets": [],
            "cautious_price": "",
            "entry_ranges": [],
        })

        now, mnq_1m, hist_mnq_1m, hist_hourly_mnq = standard_day
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq)

        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_hypothesis_other_fields_untouched(self, standard_day):
        """run_daily should only set direction='none'; other fields stay as-is."""
        save_hypothesis({
            "direction": "up",
            "weekly_mid": "above",
            "daily_mid": "mid",
            "last_liquidity": "day_high",
            "divs": [{"type": "wick"}],
            "targets": [{"name": "week_high", "price": 21450.0}],
            "cautious_price": "21410.0",
            "entry_ranges": [{"source": "12hr", "low": 100.0, "high": 110.0}],
        })

        now, mnq_1m, hist_mnq_1m, hist_hourly_mnq = standard_day
        run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq)

        hyp = load_hypothesis()
        assert hyp["direction"] == "none"
        assert hyp["weekly_mid"] == "above"
        assert hyp["daily_mid"] == "mid"
        assert hyp["last_liquidity"] == "day_high"
        assert len(hyp["divs"]) == 1
        assert len(hyp["targets"]) == 1


class TestUnvisitedFvgFilter:
    def test_unvisited_fvg_filter_excludes_filled_gaps(self):
        """
        Set up two 1hr FVGs:
          - FVG1 at bars[0..2]: bullish gap [21000, 21020] — filled by a later 1m bar
          - FVG2 at bars[3..5]: bullish gap [21100, 21120] — NOT filled (unvisited)

        Only FVG2 should appear in liquidities.
        """
        now = _now("2026-04-27", 9, 20)

        # 1hr bars (6 bars starting 2026-04-25 09:00 ET)
        # FVG1: bars[2].Low(21020) > bars[0].High(21000) → bullish gap [21000, 21020]
        # FVG2: bars[5].Low(21120) > bars[3].High(21100) → bullish gap [21100, 21120]
        # Using exactly 6 bars prevents any additional 3-bar triple-bar scan beyond bar[3..5].
        idx_1h = _drange("2026-04-25 09:00:00", 6, "h")
        highs = [21000.0, 21010.0, 21025.0, 21100.0, 21110.0, 21125.0]
        lows  = [20995.0, 20998.0, 21020.0, 21095.0, 21098.0, 21120.0]

        hist_hourly = pd.DataFrame(
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
        # High=21012 >= 21000(bottom), Low=20999 <= 21020(top) → fills FVG1
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

        run_daily(now, today_1m, hist_mnq_1m, hist_hourly)

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
