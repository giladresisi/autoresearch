# tests/test_smt_trend.py
# Unit tests for trend.py — cautious-mode management and trend invalidation.
# All JSON state files are redirected to tmp_path via monkeypatching smt_state PATHs.

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import smt_state
from smt_state import (
    DEFAULT_DAILY,
    DEFAULT_HYPOTHESIS,
    DEFAULT_POSITION,
    save_daily,
    save_hypothesis,
    save_position,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 27, 10, 1, 0, tzinfo=timezone.utc)


def make_1m_bar(
    open_=100.0,
    high=110.0,
    low=90.0,
    close=105.0,
    time_str="2026-04-27T10:01:00-04:00",
) -> dict:
    return {"time": time_str, "open": open_, "high": high, "low": low, "close": close}


def make_recent_bars(
    closes,
    opens,
    highs=None,
    lows=None,
    start="2026-04-27 09:30:00",
    tz="America/New_York",
) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="1min", tz=tz)
    if highs is None:
        highs = [max(o, c) + 2 for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) - 2 for o, c in zip(opens, closes)]
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes}, index=idx
    )


def _active_position(cautious="no") -> dict:
    """Return a minimal active position sub-dict."""
    return {
        "time": "2026-04-27T10:00:00-04:00",
        "fill_price": 100.0,
        "direction": "up",
        "stop": 95.0,
        "contracts": 2,
        "cautious": cautious,
    }


def _daily_with_levels(levels: list[dict]) -> dict:
    d = copy.deepcopy(DEFAULT_DAILY)
    d["liquidities"] = levels
    return d


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def redirect_paths(tmp_path, monkeypatch):
    """Redirect all smt_state path constants to tmp_path so tests are isolated."""
    monkeypatch.setattr(smt_state, "DATA_DIR", tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH", tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH", tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH", tmp_path / "position.json")

    # trend.py imports these at the top of each call; we must also patch the
    # names that trend.py imported directly.
    import trend
    monkeypatch.setattr(trend, "load_hypothesis", smt_state.load_hypothesis)
    monkeypatch.setattr(trend, "save_hypothesis", smt_state.save_hypothesis)
    monkeypatch.setattr(trend, "load_position", smt_state.load_position)
    monkeypatch.setattr(trend, "save_position", smt_state.save_position)
    monkeypatch.setattr(trend, "load_daily", smt_state.load_daily)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEarlyExit:
    def test_early_exit_when_direction_none(self):
        """direction="none" → return None immediately."""
        from trend import run_trend

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "none"
        save_hypothesis(hyp)

        bar = make_1m_bar()
        recent = make_recent_bars(closes=[100, 101], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is None


class TestCautiousArming:
    def _setup_active_cautious_no(self, direction="up", cautious_price=110.0):
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = direction
        hyp["cautious_price_initial"] = str(cautious_price)
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        active = _active_position(cautious="no")
        active["direction"] = direction
        pos["active"] = active
        save_position(pos)

        daily = _daily_with_levels([])
        save_daily(daily)

    def test_with_position_no_cautious_no_signal_when_below_threshold(self):
        """Bar does not reach cautious_price → no signal."""
        from trend import run_trend

        self._setup_active_cautious_no(direction="up", cautious_price=110.0)
        # bar.high=105 < 110
        bar = make_1m_bar(open_=100, high=105, low=98, close=103)
        recent = make_recent_bars(closes=[100, 103], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is None

    def test_cautious_arming_long_close_beyond(self):
        """direction=up, bar.high>=cautious_price AND close>cautious_price → cautious-armed (initial)."""
        from trend import run_trend
        from smt_state import load_position

        self._setup_active_cautious_no(direction="up", cautious_price=110.0)
        bar = make_1m_bar(open_=100, high=112, low=98, close=111)
        recent = make_recent_bars(closes=[100, 111], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "cautious-armed"
        pos = load_position()
        assert pos["active"]["cautious"] == "initial"

    def test_cautious_rejected_long_close_below(self):
        """direction=up, bar.high>=cautious_price_initial BUT close<cautious_price_initial
        → wick-only reach of initial level: no rejection, returns None."""
        from trend import run_trend

        self._setup_active_cautious_no(direction="up", cautious_price=110.0)
        bar = make_1m_bar(open_=100, high=112, low=98, close=109)
        recent = make_recent_bars(closes=[100, 109], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is None

    def test_cautious_arming_short_close_beyond(self):
        """direction=down, bar.low<=cautious_price AND close<cautious_price → cautious-armed (initial)."""
        from trend import run_trend
        from smt_state import load_position

        self._setup_active_cautious_no(direction="down", cautious_price=90.0)
        bar = make_1m_bar(open_=95, high=97, low=88, close=89)
        recent = make_recent_bars(closes=[95, 89], opens=[96, 95])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "cautious-armed"
        pos = load_position()
        assert pos["active"]["cautious"] == "initial"

    def test_cautious_price_empty_string_skips_arming(self):
        """cautious_price="" → arming step skipped regardless of price action."""
        from trend import run_trend

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["cautious_price"] = ""
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _active_position(cautious="no")
        save_position(pos)

        save_daily(_daily_with_levels([]))

        # A bar that would cross any price
        bar = make_1m_bar(open_=100, high=200, low=50, close=180)
        recent = make_recent_bars(closes=[100, 180], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is None


class TestCautiousYes:
    def _setup_cautious_yes(self, direction="up", cautious_price=110.0):
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = direction
        hyp["cautious_price_secondary"] = str(cautious_price)
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        active = _active_position(cautious="yes")
        active["direction"] = direction
        pos["active"] = active
        save_position(pos)

        save_daily(_daily_with_levels([]))

    def test_cautious_yes_reversal_long(self):
        """direction=up, cautious="yes", bar.low<=cautious_price → cautious-reversal."""
        from trend import run_trend
        from smt_state import load_hypothesis, load_position

        self._setup_cautious_yes(direction="up", cautious_price=110.0)
        # bar.low=109 <= 110
        bar = make_1m_bar(open_=112, high=115, low=109, close=113)
        recent = make_recent_bars(closes=[112, 113], opens=[111, 112])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-close"
        assert result["reason"] == "cautious-reversal"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"
        pos = load_position()
        assert pos["active"] == {}

    def test_cautious_yes_reversal_short(self):
        """direction=down, cautious="yes", bar.high>=cautious_price → cautious-reversal."""
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup_cautious_yes(direction="down", cautious_price=90.0)
        # bar.high=91 >= 90
        bar = make_1m_bar(open_=88, high=91, low=86, close=89)
        recent = make_recent_bars(closes=[88, 89], opens=[89, 88])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-close"
        assert result["reason"] == "cautious-reversal"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_cautious_yes_1m_break_long(self):
        """direction=up, cautious="yes", last bearish bar Low=115, bar.low=114 → cautious-1m-break.

        For direction=up, cautious_price is above entry (e.g. 110). The reversal check fires when
        bar.low <= cautious_price, so we must keep bar.low > cautious_price to avoid triggering
        reversal first. We set cautious_price=110, and the 1m-break reference bar has Low=115
        (above cautious_price). bar.low=114 breaks below that recent bearish bar's Low but
        stays above cautious_price, so only cautious-1m-break fires.
        """
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup_cautious_yes(direction="up", cautious_price=110.0)

        # recent bars: first bullish, second bearish (Low=115 — above cautious_price=110)
        recent = make_recent_bars(
            closes=[118, 113],
            opens=[112, 120],
            highs=[122, 122],
            lows=[111, 115],
        )
        # current bar: bar.low=114 <= last bearish Low=115; bar.low=114 > cautious_price=110
        bar = make_1m_bar(open_=116, high=119, low=114, close=117)
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-close"
        assert result["reason"] == "cautious-1m-break"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_cautious_yes_1m_break_short(self):
        """direction=down, cautious="yes", last bullish bar High=85, bar.high=86 → cautious-1m-break.

        For direction=down, cautious_price is below entry (e.g. 90). The reversal check fires when
        bar.high >= cautious_price, so we keep bar.high < cautious_price. We set cautious_price=90,
        last bullish bar has High=85 (below cautious_price). bar.high=86 breaks above that bar's
        High but stays below cautious_price=90.
        """
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup_cautious_yes(direction="down", cautious_price=90.0)

        # recent bars: first bearish, second bullish (High=85 — below cautious_price=90)
        recent = make_recent_bars(
            closes=[83, 87],
            opens=[88, 82],
            highs=[89, 85],
            lows=[81, 80],
        )
        # current bar: bar.high=86 >= last bullish High=85; bar.high=86 < cautious_price=90
        bar = make_1m_bar(open_=84, high=86, low=82, close=83)
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-close"
        assert result["reason"] == "cautious-1m-break"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"


class TestNoPosition:
    def _setup_no_position(self, direction="up", levels=None):
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = direction
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = {}
        save_position(pos)

        daily = _daily_with_levels(levels or [])
        save_daily(daily)

    def test_no_position_no_opposite_liquidity_no_signal(self):
        """No levels below current price → no signal."""
        from trend import run_trend

        # Levels are all *above* current price for direction=up → no downside break possible
        levels = [
            {"name": "day_high", "kind": "level", "price": 200.0},
        ]
        self._setup_no_position(direction="up", levels=levels)
        # bar: close=105, low=103 — no level below 105
        bar = make_1m_bar(open_=103, high=107, low=103, close=105)
        recent = make_recent_bars(closes=[100, 105], opens=[99, 103])
        result = run_trend(NOW, bar, recent)
        assert result is None

    def test_no_position_opposite_liquidity_break_emits_trend_broken(self):
        """direction=up, day_low level at 50 below current close=60, bar.low=48 → trend-broken."""
        from trend import run_trend
        from smt_state import load_hypothesis, load_position

        levels = [
            {"name": "day_low", "kind": "level", "price": 50.0},
        ]
        self._setup_no_position(direction="up", levels=levels)
        # close=60 > level_price=50, so level is "opposite direction" (below); bar.low=48 <= 50
        bar = make_1m_bar(open_=55, high=62, low=48, close=60)
        recent = make_recent_bars(closes=[58, 60], opens=[55, 55])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "trend-broken"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"
        pos = load_position()
        assert pos["confirmation_bar"] == {}
        assert pos["limit_entry"] == ""


class TestSignalShape:
    def test_signal_record_shape(self):
        """Any returned signal must have kind, time, price and be JSON-serializable."""
        from trend import run_trend
        import json

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["cautious_price_initial"] = "110"
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _active_position(cautious="no")
        save_position(pos)

        save_daily(_daily_with_levels([]))

        # Trigger cautious-armed
        bar = make_1m_bar(open_=100, high=112, low=98, close=111)
        recent = make_recent_bars(closes=[100, 111], opens=[99, 100])
        signal = run_trend(NOW, bar, recent)
        assert signal is not None
        assert "kind" in signal
        assert "time" in signal
        assert "price" in signal
        # Must be JSON-serializable
        serialized = json.dumps(signal)
        assert isinstance(serialized, str)
