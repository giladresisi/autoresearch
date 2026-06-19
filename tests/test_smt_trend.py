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
    save_global,
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


def _active_position(
    cautious="no",
    direction="up",
    cautious_initial="",
    cautious_initial_level="",
    cautious_secondary="",
    cautious_secondary_level="",
) -> dict:
    """Return a minimal active position sub-dict.

    SMT-v2 Phase 1: also writes the FROZEN management snapshot (mgmt_direction +
    the four cautious ladder fields + backing_tier) so existing trend management
    tests exercise the frozen path. The frozen ladder is left "" by default so the
    back-compat fallback pulls it from the live hypothesis (frozen == live), keeping
    pre-existing assertions byte-equivalent. Tests that want to pin the frozen ladder
    pass the values explicitly.
    """
    _mgmt = "up" if direction == "long" else ("down" if direction == "short" else direction)
    _cs_l = cautious_secondary_level or ""
    if _cs_l.startswith("week"):
        _tier = "week"
    else:
        _tier = "day"
    return {
        "time": "2026-04-27T10:00:00-04:00",
        "fill_price": 100.0,
        "direction": direction,
        "stop": 95.0,
        "contracts": 2,
        "cautious": cautious,
        # Frozen management snapshot (Contract A)
        "mgmt_direction": _mgmt,
        "cautious_initial": cautious_initial,
        "cautious_initial_level": cautious_initial_level,
        "cautious_secondary": cautious_secondary,
        "cautious_secondary_level": cautious_secondary_level,
        "backing_tier": _tier,
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
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(smt_state, "_hyp_cache_valid", False)

    # trend.py imports these at the top of each call; we must also patch the
    # names that trend.py imported directly.
    import trend
    monkeypatch.setattr(trend, "load_global",    smt_state.load_global)
    monkeypatch.setattr(trend, "load_hypothesis", smt_state.load_hypothesis)
    monkeypatch.setattr(trend, "save_hypothesis", smt_state.save_hypothesis)
    monkeypatch.setattr(trend, "load_position", smt_state.load_position)
    monkeypatch.setattr(trend, "save_position", smt_state.save_position)
    monkeypatch.setattr(trend, "load_daily_ro", smt_state.load_daily_ro)

    # Default global: confidence="medium" so existing tests are unaffected by the
    # new global-trend invalidation branch (which only fires on confidence="high").
    save_global({"all_time_high": 0.0, "confidence": "medium", "trend": "up"})


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
        # Frozen ladder == live ladder (byte-equivalence): pin the frozen initial.
        active = _active_position(
            cautious="no", direction=direction,
            cautious_initial=str(cautious_price),
        )
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

        # cautious_price must be >= fill_price + INITIAL_STOP_MIN_DIST_PTS (50 pts)
        self._setup_active_cautious_no(direction="up", cautious_price=160.0)
        bar = make_1m_bar(open_=100, high=162, low=98, close=161)
        recent = make_recent_bars(closes=[100, 161], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        pos = load_position()
        assert pos["active"]["cautious"] == "initial"

    def test_cautious_wick_only_sets_midpoint_stop(self):
        """direction=up, bar.high>=cautious_price_initial BUT close<cautious_price_initial
        → wick-only: stop moved to midpoint between original stop and initial target."""
        from trend import run_trend
        from smt_state import load_position

        # cautious_price must be >= fill_price + INITIAL_STOP_MIN_DIST_PTS (50 pts)
        self._setup_active_cautious_no(direction="up", cautious_price=160.0)
        bar = make_1m_bar(open_=100, high=162, low=98, close=159)
        recent = make_recent_bars(closes=[100, 159], opens=[99, 100])
        result = run_trend(NOW, bar, recent)
        # stop=95, cautious_initial=160 → midpoint=127.5
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial_mid"
        assert result["cautious_break_price"] == 127.5
        pos = load_position()
        assert pos["active"]["cautious"] == "initial_surpassed"
        assert pos["active"]["cautious_break_price"] == 127.5

    def test_cautious_arming_short_close_beyond(self):
        """direction=down, bar.low<=cautious_price AND close<cautious_price → cautious-armed (initial)."""
        from trend import run_trend
        from smt_state import load_position

        # cautious_price must be <= fill_price - INITIAL_STOP_MIN_DIST_PTS (50 pts)
        self._setup_active_cautious_no(direction="down", cautious_price=40.0)
        bar = make_1m_bar(open_=95, high=97, low=38, close=39)
        recent = make_recent_bars(closes=[95, 39], opens=[96, 95])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "new-stop-exit"
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
        # Frozen ladder == live ladder (byte-equivalence): pin the frozen secondary.
        active = _active_position(
            cautious="yes", direction=direction,
            cautious_secondary=str(cautious_price),
        )
        pos["active"] = active
        save_position(pos)

        save_daily(_daily_with_levels([]))

    def test_cautious_yes_reversal_long(self):
        """direction=up, cautious="yes", all recent bars bullish → no opposite bar → no signal."""
        from trend import run_trend

        self._setup_cautious_yes(direction="up", cautious_price=110.0)
        # bar.low=109 <= 110, but recent bars are all bullish so no 1m-break bar exists
        bar = make_1m_bar(open_=112, high=115, low=109, close=113)
        recent = make_recent_bars(closes=[112, 113], opens=[111, 112])
        result = run_trend(NOW, bar, recent)
        assert result is None

    def test_cautious_yes_reversal_short(self):
        """direction=down, cautious="yes", cautious_break_price=89, bar.close=91 > 89 → stop-exit.

        Secondary exits use bar *close*, not intrabar high — wick alone does not trigger.
        """
        from trend import run_trend
        from smt_state import load_hypothesis, load_position, save_position

        self._setup_cautious_yes(direction="down", cautious_price=90.0)
        pos = load_position()
        pos["active"]["cautious_break_price"] = 89.0
        save_position(pos)
        # bar.close=91 > 89 → stop-exit fires; bar.high=95 is irrelevant (wick-only ignored)
        bar = make_1m_bar(open_=88, high=95, low=86, close=91)
        recent = make_recent_bars(closes=[88, 91], opens=[89, 88])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-secondary-break"
        assert result["price"] == 89.0
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_cautious_yes_1m_break_long(self):
        """direction=up, cautious="yes", cautious_break_price=115, bar.close=113 < 115 → stop-exit.

        Secondary exits use bar *close* — a wick below the break price (bar.low=111) without
        closing below it does NOT trigger. Exit fires only when the bar closes below 115.
        """
        from trend import run_trend
        from smt_state import load_hypothesis, load_position, save_position

        self._setup_cautious_yes(direction="up", cautious_price=110.0)
        pos = load_position()
        pos["active"]["cautious_break_price"] = 115.0
        save_position(pos)

        # recent: bullish bar (open=115, close=120) and bearish bar; bar.close=113 < 115
        recent = make_recent_bars(
            closes=[120, 113],
            opens=[115, 120],
            highs=[122, 122],
            lows=[111, 111],
        )
        bar = make_1m_bar(open_=116, high=119, low=111, close=113)
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-secondary-break"
        assert result["price"] == 115.0
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_cautious_yes_1m_break_short(self):
        """direction=down, cautious="yes", cautious_break_price=88, bar.close=90 > 88 → stop-exit.

        Secondary exits use bar *close* — a wick above the break price (bar.high=92) without
        closing above it does NOT trigger. Exit fires only when the bar closes above 88.
        """
        from trend import run_trend
        from smt_state import load_hypothesis, load_position, save_position

        self._setup_cautious_yes(direction="down", cautious_price=90.0)
        pos = load_position()
        pos["active"]["cautious_break_price"] = 88.0
        save_position(pos)

        # recent: bearish bar (open=88, close=83) and bullish bar; bar.close=90 > 88
        recent = make_recent_bars(
            closes=[83, 90],
            opens=[88, 82],
            highs=[92, 92],
            lows=[81, 80],
        )
        bar = make_1m_bar(open_=84, high=92, low=82, close=90)
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-secondary-break"
        assert result["price"] == 88.0
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

    def test_no_position_opposite_liquidity_break_emits_level_swept(self):
        """direction=up, day_low level at 50 below current close=60, bar.low=48 → level-swept.

        run_trend returns level-swept for high-priority level crossings; the pipeline
        (session_pipeline.py) translates this to trend-broken after hypothesis re-evaluation.
        run_trend does NOT clear hypothesis direction — that is the pipeline's responsibility.
        """
        from trend import run_trend
        from smt_state import load_hypothesis

        levels = [
            {"name": "day_low", "kind": "level", "price": 50.0},
        ]
        self._setup_no_position(direction="up", levels=levels)
        # close=60 > level_price=50, so level is "opposite direction" (below); bar.low=48 <= 50
        bar = make_1m_bar(open_=55, high=62, low=48, close=60)
        recent = make_recent_bars(closes=[58, 60], opens=[55, 55])
        result = run_trend(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "level-swept", f"expected level-swept, got {result['kind']}"
        assert result["level_name"] == "day_low"
        # run_trend must not clear hypothesis direction for level-swept — pipeline owns that.
        hyp = load_hypothesis()
        assert hyp["direction"] == "up"


class TestSignalShape:
    def test_signal_record_shape(self):
        """Any returned signal must have kind, time, price and be JSON-serializable."""
        from trend import run_trend
        import json

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["cautious_price_initial"] = "160"
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _active_position(cautious="no", cautious_initial="160")
        save_position(pos)

        save_daily(_daily_with_levels([]))

        # Trigger cautious-armed
        bar = make_1m_bar(open_=100, high=162, low=98, close=161)
        recent = make_recent_bars(closes=[100, 111], opens=[99, 100])
        signal = run_trend(NOW, bar, recent)
        assert signal is not None
        assert "kind" in signal
        assert "time" in signal
        assert "price" in signal
        # Must be JSON-serializable
        serialized = json.dumps(signal)
        assert isinstance(serialized, str)


class TestGlobalTrendInvalidation:
    """confidence='high' + direction opposing global trend → trend-broken, direction reset."""

    def _setup(self, direction: str, global_trend: str, confidence: str = "high"):
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = direction
        save_hypothesis(hyp)

        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([]))
        save_global({"all_time_high": 0.0, "confidence": confidence, "trend": global_trend})

    def test_confidence_high_opposing_direction_returns_trend_broken(self):
        """direction='down', global trend='up', confidence='high' → trend-broken signal."""
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup(direction="down", global_trend="up", confidence="high")
        bar = make_1m_bar(open_=100, high=105, low=95, close=102)
        recent = make_recent_bars(closes=[100, 102], opens=[99, 100])
        result = run_trend(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "trend-broken"
        assert result["level_name"] == "global_trend"
        assert result["broken_direction"] == "down"
        hyp = load_hypothesis()
        assert hyp["direction"] == "none"

    def test_confidence_high_aligned_direction_no_invalidation(self):
        """direction='up', global trend='up', confidence='high' → no invalidation."""
        from trend import run_trend

        self._setup(direction="up", global_trend="up", confidence="high")
        bar = make_1m_bar(open_=100, high=105, low=95, close=102)
        recent = make_recent_bars(closes=[100, 102], opens=[99, 100])
        result = run_trend(NOW, bar, recent)

        # The bar doesn't trigger any existing rules (no cautious, no level breach),
        # so result must be None — the global-trend check must not have fired.
        assert result is None

    def test_confidence_medium_opposing_direction_no_invalidation(self):
        """direction='down', global trend='up', confidence='medium' → no invalidation."""
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup(direction="down", global_trend="up", confidence="medium")
        bar = make_1m_bar(open_=100, high=105, low=95, close=102)
        recent = make_recent_bars(closes=[100, 102], opens=[99, 100])
        run_trend(NOW, bar, recent)

        hyp = load_hypothesis()
        assert hyp["direction"] == "down", (
            "confidence='medium' must not trigger global-trend invalidation"
        )

    def test_global_trend_invalidation_clears_limit_and_conf_bar(self):
        """On invalidation, stop_entry and conf_bar_entry must be cleared."""
        from trend import run_trend
        from smt_state import load_position

        self._setup(direction="down", global_trend="up", confidence="high")
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["stop_entry"] = 95.0
        pos["conf_bar_entry"] = {"time": "T", "high": 100.0, "low": 90.0,
                                   "body_high": 98.0, "body_low": 92.0}
        save_position(pos)

        bar = make_1m_bar(open_=100, high=105, low=95, close=102)
        recent = make_recent_bars(closes=[100, 102], opens=[99, 100])
        run_trend(NOW, bar, recent)

        p = load_position()
        assert p["stop_entry"] == ""
        assert p["conf_bar_entry"] == {}


class TestManualDirectionLock:
    """GIL-8: while hypothesis['manual'] is set (trade.py set-direction), the automatic
    reset paths must leave the manually forced hypothesis alone."""

    def _setup_mid_break(self, manual: bool):
        """direction=up formed above the daily mid; bar close falls below mid -> the
        daily-mid invalidation would normally fire trend-broken."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["daily_mid"] = "above"   # _mid_cross_guard arms for direction=up
        hyp["manual"] = manual
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([
            {"name": "day_high", "kind": "level", "price": 120.0},
            {"name": "day_low",  "kind": "level", "price": 80.0},
        ]))  # mid = 100

    def test_daily_mid_break_fires_without_lock(self):
        """Sanity: same setup without the lock -> daily-mid trend-broken fires."""
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup_mid_break(manual=False)
        bar = make_1m_bar(open_=101, high=102, low=94, close=95)  # close < mid 100
        recent = make_recent_bars(closes=[101, 95], opens=[102, 101])
        result = run_trend(NOW, bar, recent)

        assert result is not None and result["kind"] == "trend-broken"
        assert result["level_name"] == "daily_mid"
        assert load_hypothesis()["direction"] == "none"

    def test_daily_mid_break_skipped_with_lock(self):
        """Locked: the identical mid-break bar must NOT reset the manual hypothesis."""
        from trend import run_trend
        from smt_state import load_hypothesis

        self._setup_mid_break(manual=True)
        bar = make_1m_bar(open_=101, high=102, low=94, close=95)
        recent = make_recent_bars(closes=[101, 95], opens=[102, 101])
        result = run_trend(NOW, bar, recent)

        assert result is None
        hyp = load_hypothesis()
        assert hyp["direction"] == "up"
        assert hyp["manual"] is True

    def test_global_trend_invalidation_skipped_with_lock(self):
        """confidence=high + opposing global trend normally resets; not while locked."""
        from trend import run_trend
        from smt_state import load_hypothesis

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "down"
        hyp["manual"] = True
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([]))
        save_global({"all_time_high": 0.0, "confidence": "high", "trend": "up"})

        bar = make_1m_bar(open_=100, high=105, low=95, close=102)
        recent = make_recent_bars(closes=[100, 102], opens=[99, 100])
        result = run_trend(NOW, bar, recent)

        assert result is None
        hyp = load_hypothesis()
        assert hyp["direction"] == "down"
        assert hyp["manual"] is True

    def test_session_ath_straddle_skipped_with_lock(self):
        """A session-ATH straddle bar normally emits ath-crossed (direction reset);
        not while locked."""
        from trend import run_trend
        from smt_state import load_hypothesis

        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["manual"] = True
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([]))
        save_global({"all_time_high": 100.0, "session_ath": 100.0,
                     "confidence": "medium", "trend": "up"})

        bar = make_1m_bar(open_=98, high=105, low=95, close=102)  # straddles 100
        recent = make_recent_bars(closes=[98, 102], opens=[97, 98])
        result = run_trend(NOW, bar, recent)

        assert result is None or result["kind"] != "ath-crossed"
        hyp = load_hypothesis()
        assert hyp["direction"] == "up"
        assert hyp["manual"] is True

    def test_clear_position_and_hypothesis_releases_lock(self):
        """Any position-close path clears direction AND the lock (manual=True with
        direction='none' would freeze future automatic resets)."""
        from trend import _clear_position_and_hypothesis

        pos = copy.deepcopy(DEFAULT_POSITION)
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "down"
        hyp["manual"] = True
        _clear_position_and_hypothesis(pos, hyp, clear_active=True)

        assert hyp["direction"] == "none"
        assert hyp["manual"] is False


# ---------------------------------------------------------------------------
# SMT-v2 Phase 1: frozen-snapshot byte-equivalence regression
# ---------------------------------------------------------------------------

class TestFrozenSnapshotRegression:
    """A non-flipping trade (frozen == live) must produce signals byte-equivalent to
    the pre-change behavior; flipping the LIVE hypothesis mid-trade must NOT change the
    emitted management signals (the frozen snapshot insulates management)."""

    def _setup_long_initial_arm(self, live_direction="up"):
        """direction=up trade, frozen initial cautious=160. The arming bar (high=162,
        close=161) closes beyond the initial level -> 'new-stop-exit' (initial)."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = live_direction
        # Live ladder: when not flipped, equals the frozen ladder.
        hyp["cautious_price_initial"] = "160" if live_direction == "up" else ""
        save_hypothesis(hyp)

        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _active_position(
            cautious="no", direction="up", cautious_initial="160",
            cautious_initial_level="day_high",
        )
        save_position(pos)
        save_daily(_daily_with_levels([]))

    def test_normal_trade_management_byte_equivalent(self):
        """frozen == live, direction matches -> the captured baseline signal."""
        from trend import run_trend
        from smt_state import load_position

        self._setup_long_initial_arm(live_direction="up")
        bar = make_1m_bar(open_=100, high=162, low=98, close=161)
        recent = make_recent_bars(closes=[100, 161], opens=[99, 100])
        result = run_trend(NOW, bar, recent)

        # Captured baseline (pre-change behavior for this exact scenario).
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial"
        assert result["level_name"] == "day_high"
        assert load_position()["active"]["cautious"] == "initial"
        # snapshot the full emitted dict for the flip comparison
        TestFrozenSnapshotRegression._baseline = result

    def test_flip_does_not_change_normal_management(self):
        """Same scenario but the LIVE hypothesis is flipped to the opposite direction
        ('down') and its live ladder wiped — management must be identical because it
        keys off the frozen up-side snapshot."""
        from trend import run_trend
        from smt_state import load_position

        self._setup_long_initial_arm(live_direction="down")  # live flipped
        bar = make_1m_bar(open_=100, high=162, low=98, close=161)
        recent = make_recent_bars(closes=[100, 161], opens=[99, 100])
        result = run_trend(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial"
        assert result["level_name"] == "day_high"
        assert load_position()["active"]["cautious"] == "initial"
        # Identical to the non-flipped baseline (frozen snapshot insulates management).
        self._setup_long_initial_arm(live_direction="up")
        baseline = run_trend(NOW, bar, recent)
        assert result == baseline


class TestOpenWindowDailyMidSuspend:
    """O1 (GIL-34): the daily-mid invalidation is suspended inside the 09:15-11:30 ET
    open window when OPEN_WINDOW_DAILY_MID_SUSPEND is ON, so a fresh directional
    hypothesis survives the open mayhem. Default OFF → byte-identical to master."""

    # tz-aware ET timestamps: in-window (09:50:02 ET) and out-of-window (08:00 ET).
    _NOW_IN  = pd.Timestamp("2026-06-16T09:50:02-04:00")
    _NOW_OUT = pd.Timestamp("2026-06-16T08:00:00-04:00")

    def _setup_down_mid_break(self):
        """direction=down formed at the daily mid; bar close rises above mid → the
        daily-mid invalidation would normally fire trend-broken (the O1 occurrence)."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "down"
        hyp["daily_mid"] = "mid"   # _mid_cross_guard arms for direction=down
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([
            {"name": "day_high", "kind": "level", "price": 120.0},
            {"name": "day_low",  "kind": "level", "price": 80.0},
        ]))  # mid = 100

    # bar close=102 > mid 100 → contradicts the down thesis
    _BAR = staticmethod(lambda: make_1m_bar(open_=99, high=103, low=98, close=102))
    _RECENT = staticmethod(lambda: make_recent_bars(closes=[99, 102], opens=[98, 99]))

    def test_flag_off_default_fires_trend_broken(self):
        """Default OFF: identical to master — daily-mid trend-broken fires in-window."""
        from trend import run_trend
        from smt_state import load_hypothesis

        assert __import__("trend").OPEN_WINDOW_DAILY_MID_SUSPEND is False
        self._setup_down_mid_break()
        result = run_trend(self._NOW_IN, self._BAR(), self._RECENT())
        assert result is not None and result["kind"] == "trend-broken"
        assert result["level_name"] == "daily_mid"
        assert load_hypothesis()["direction"] == "none"

    def test_flag_on_in_window_suspends_invalidation(self, monkeypatch):
        """ON + in-window: no trend-broken, the down hypothesis survives."""
        import trend
        from trend import run_trend
        from smt_state import load_hypothesis

        monkeypatch.setattr(trend, "OPEN_WINDOW_DAILY_MID_SUSPEND", True)
        self._setup_down_mid_break()
        result = run_trend(self._NOW_IN, self._BAR(), self._RECENT())
        assert result is None or result.get("level_name") != "daily_mid"
        assert load_hypothesis()["direction"] == "down"

    def test_flag_on_out_of_window_still_fires(self, monkeypatch):
        """ON but OUTSIDE the window: daily-mid trend-broken still fires (unchanged)."""
        import trend
        from trend import run_trend
        from smt_state import load_hypothesis

        monkeypatch.setattr(trend, "OPEN_WINDOW_DAILY_MID_SUSPEND", True)
        self._setup_down_mid_break()
        result = run_trend(self._NOW_OUT, self._BAR(), self._RECENT())
        assert result is not None and result["kind"] == "trend-broken"
        assert load_hypothesis()["direction"] == "none"


class TestSecondaryTakeProfitOnTouch0930:
    """O2 (GIL-34) Rule 1: during [09:30,09:45] ET, the instant the SECONDARY cautious
    level is touched (wick reach), market-close the position. Default OFF → byte-identical.
    No reverse entry (deferred) — only the take-profit close is implemented here."""

    _NOW_IN  = pd.Timestamp("2026-06-16T09:35:00-04:00")   # in [09:30,09:45]
    _NOW_OUT = pd.Timestamp("2026-06-16T10:19:00-04:00")   # outside the window

    def _setup_short_secondary(self):
        """Active SHORT, secondary cautious = day_low @ 80, unarmed."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "down"
        save_hypothesis(hyp)
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _active_position(
            cautious="no", direction="short",
            cautious_secondary="80", cautious_secondary_level="day_low",
        )
        save_position(pos)
        save_daily(_daily_with_levels([
            {"name": "day_high", "kind": "level", "price": 120.0},
            {"name": "day_low",  "kind": "level", "price": 80.0},
        ]))

    # wick-only touch of secondary: low 78 <= 80 (surpassed), close 84 not < 80 (no close-beyond)
    _TOUCH = staticmethod(lambda: make_1m_bar(open_=85, high=86, low=78, close=84))
    _NO_TOUCH = staticmethod(lambda: make_1m_bar(open_=85, high=86, low=82, close=84))  # low 82 > 80
    _RECENT = staticmethod(lambda: make_recent_bars(closes=[90, 84], opens=[92, 85]))

    def test_flag_off_default_no_tp_close(self):
        """Default OFF: O2 path inert — a wick-only secondary touch does NOT market-close
        (normal behavior: position waits in secondary_surpassed)."""
        import trend
        from trend import run_trend
        from smt_state import load_position

        assert trend.SECONDARY_TP_ON_TOUCH_0930 is False
        self._setup_short_secondary()
        result = run_trend(self._NOW_IN, self._TOUCH(), self._RECENT())
        # Not the O2 take-profit close; position still active.
        assert result is None or result.get("close_reason") != "secondary-tp-touch"
        assert load_position()["active"] != {}

    def test_flag_on_in_window_market_close_on_touch(self, monkeypatch):
        """ON + in-window + secondary touched → market-close (take profit), position cleared."""
        import trend
        from trend import run_trend
        from smt_state import load_position

        monkeypatch.setattr(trend, "SECONDARY_TP_ON_TOUCH_0930", True)
        self._setup_short_secondary()
        result = run_trend(self._NOW_IN, self._TOUCH(), self._RECENT())
        assert result is not None and result["kind"] == "market-close"
        assert result["close_reason"] == "secondary-tp-touch"
        assert load_position()["active"] == {}

    def test_flag_on_out_of_window_no_tp_close(self, monkeypatch):
        """ON but OUTSIDE the window: the O2 take-profit close must NOT fire."""
        import trend
        from trend import run_trend
        from smt_state import load_position

        monkeypatch.setattr(trend, "SECONDARY_TP_ON_TOUCH_0930", True)
        self._setup_short_secondary()
        result = run_trend(self._NOW_OUT, self._TOUCH(), self._RECENT())
        assert result is None or result.get("close_reason") != "secondary-tp-touch"
        assert load_position()["active"] != {}

    def test_flag_on_in_window_no_touch_no_close(self, monkeypatch):
        """ON + in-window but secondary NOT touched → no take-profit close."""
        import trend
        from trend import run_trend
        from smt_state import load_position

        monkeypatch.setattr(trend, "SECONDARY_TP_ON_TOUCH_0930", True)
        self._setup_short_secondary()
        result = run_trend(self._NOW_IN, self._NO_TOUCH(), self._RECENT())
        assert result is None or result.get("close_reason") != "secondary-tp-touch"
        assert load_position()["active"] != {}


class TestSuppressWeeklyMidTrendBroken:
    """GIL-39 Change B: when SUPPRESS_WEEKLY_MID_TREND_BROKEN is ON, the weekly-mid
    invalidation is suppressed (no trend-broken / market-close on a weekly-mid cross);
    the daily-mid and every other invalidation are untouched. Default OFF → byte-identical."""

    _NOW = pd.Timestamp("2026-06-16T13:00:00-04:00")  # outside the open window

    def _setup_up_weekly_break(self):
        """Flat (no active position). direction=up formed at the weekly mid; bar close
        falls below the weekly mid → the weekly-mid invalidation would normally fire
        trend-broken (the Change B occurrence). week_high=120/week_low=80 → weekly mid=100."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["weekly_mid"] = "mid"   # _weekly_mid_cross_guard arms for direction=up
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))  # no active → Step-4 flat scan
        save_daily(_daily_with_levels([
            {"name": "week_high", "kind": "level", "price": 120.0},
            {"name": "week_low",  "kind": "level", "price": 80.0},
        ]))

    def _setup_up_daily_break(self):
        """Flat (no active position). direction=up at BOTH the daily and weekly mid; bar
        close falls below both. Used to prove the daily-mid invalidation still fires while
        the weekly-mid one is suppressed. day_high=110/day_low=90 → daily mid=100;
        week_high=130/week_low=70 → weekly mid=100."""
        hyp = copy.deepcopy(DEFAULT_HYPOTHESIS)
        hyp["direction"] = "up"
        hyp["daily_mid"] = "mid"
        hyp["weekly_mid"] = "mid"
        save_hypothesis(hyp)
        save_position(copy.deepcopy(DEFAULT_POSITION))
        save_daily(_daily_with_levels([
            {"name": "day_high",  "kind": "level", "price": 110.0},
            {"name": "day_low",   "kind": "level", "price": 90.0},
            {"name": "week_high", "kind": "level", "price": 130.0},
            {"name": "week_low",  "kind": "level", "price": 70.0},
        ]))

    # bar close=95 < mid 100 → contradicts the up thesis (crosses the mid downward)
    _BAR = staticmethod(lambda: make_1m_bar(open_=101, high=102, low=94, close=95))
    _RECENT = staticmethod(lambda: make_recent_bars(closes=[101, 95], opens=[100, 101]))

    def test_weekly_mid_trend_broken_default_off(self):
        """Default OFF: assert the flag is False and the weekly-mid trend-broken fires
        (baseline behavior preserved)."""
        import trend
        from trend import run_trend
        from smt_state import load_hypothesis

        assert trend.SUPPRESS_WEEKLY_MID_TREND_BROKEN is False
        self._setup_up_weekly_break()
        result = run_trend(self._NOW, self._BAR(), self._RECENT())
        assert result is not None and result["kind"] == "trend-broken"
        assert result["level_name"] == "weekly_mid"
        assert load_hypothesis()["direction"] == "none"

    def test_weekly_mid_trend_broken_suppressed_when_on(self, monkeypatch):
        """REQUIRED suppression case: flag ON → no weekly-mid signal, the up hypothesis
        PERSISTS (not reset to 'none')."""
        import trend
        from trend import run_trend
        from smt_state import load_hypothesis

        monkeypatch.setattr(trend, "SUPPRESS_WEEKLY_MID_TREND_BROKEN", True)
        self._setup_up_weekly_break()
        result = run_trend(self._NOW, self._BAR(), self._RECENT())
        assert result is None or result.get("level_name") != "weekly_mid"
        assert load_hypothesis()["direction"] == "up"

    def test_daily_mid_still_fires_when_weekly_suppressed(self, monkeypatch):
        """Flag ON: a DAILY-mid cross still emits its trend-broken (Change B must not
        suppress daily-mid)."""
        import trend
        from trend import run_trend
        from smt_state import load_hypothesis

        monkeypatch.setattr(trend, "SUPPRESS_WEEKLY_MID_TREND_BROKEN", True)
        self._setup_up_daily_break()
        result = run_trend(self._NOW, self._BAR(), self._RECENT())
        assert result is not None and result["kind"] == "trend-broken"
        assert result["level_name"] == "daily_mid"
        assert load_hypothesis()["direction"] == "none"
