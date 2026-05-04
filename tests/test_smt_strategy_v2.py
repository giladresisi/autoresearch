# tests/test_smt_strategy_v2.py
# Unit tests for strategy.py (SMT v2 pipeline per-5m-bar logic).
# Uses monkeypatch to redirect smt_state paths to tmp_path.
# All fixtures are synthetic — no parquet loading, no IB connection.

import copy
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import smt_state
from strategy import run_strategy


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 27, 10, 5, 0, tzinfo=timezone.utc)


def make_5m_bar(
    open_=100.0,
    high=110.0,
    low=90.0,
    close=95.0,
    time="2026-04-27T10:00:00-04:00",
):
    """Build a synthetic 5m bar dict with body_high / body_low computed."""
    body_high = max(open_, close)
    body_low  = min(open_, close)
    return {
        "time":      time,
        "open":      open_,
        "high":      high,
        "low":       low,
        "close":     close,
        "body_high": body_high,
        "body_low":  body_low,
    }


def make_empty_1m_recent() -> pd.DataFrame:
    """Build a minimal empty 1m DataFrame (unused by strategy.py but required by sig)."""
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def make_opp_1m_recent(
    direction: str = "up",
    open_: float = 105.0,
    close_: float = 95.0,
    high: float = 110.0,
    low: float = 90.0,
) -> pd.DataFrame:
    """Build 5 1m bars at 10:00–10:04 UTC forming a completed 5m bar opposite to direction.

    strategy._find_last_opposite_5m_bar uses first-bar Open and last-bar Close to decide
    body direction, so all bars share the same O/H/L/C values here for simplicity.
    NOW = 10:05 UTC, so the last completed 5m period is 10:00–10:04 UTC.
    """
    start = pd.Timestamp("2026-04-27 10:00:00", tz="UTC")
    idx = pd.date_range(start, periods=5, freq="1min")
    return pd.DataFrame(
        {
            "Open":  [open_]  * 5,
            "High":  [high]   * 5,
            "Low":   [low]    * 5,
            "Close": [close_] * 5,
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect all four smt_state paths into a fresh tmp_path for each test."""
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")


def write_hypothesis(direction="none", **kwargs):
    h = copy.deepcopy(smt_state.DEFAULT_HYPOTHESIS)
    h["direction"] = direction
    h.update(kwargs)
    smt_state.save_hypothesis(h)
    return h


def write_position(
    active=None,
    limit_entry="",
    confirmation_bar=None,
    failed_entries=0,
):
    p = copy.deepcopy(smt_state.DEFAULT_POSITION)
    p["active"]            = active if active is not None else {}
    p["limit_entry"]       = limit_entry
    p["confirmation_bar"]  = confirmation_bar if confirmation_bar is not None else {}
    p["failed_entries"]    = failed_entries
    smt_state.save_position(p)
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEarlyExits:

    def test_early_exit_when_direction_none(self):
        """direction=none → return None with no position mutations."""
        write_hypothesis(direction="none")
        write_position()
        bar = make_5m_bar()
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is None
        # Position file should be unchanged (no save should occur)
        pos = smt_state.load_position()
        assert pos == smt_state.DEFAULT_POSITION

    def test_early_exit_when_failed_entries_above_two(self):
        """failed_entries=3 → return None."""
        write_hypothesis(direction="up")
        write_position(failed_entries=3)
        bar = make_5m_bar()
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is None

    def test_failed_entries_exactly_two_still_allowed(self):
        """failed_entries=2 is NOT above 2 (gate is > 2) — must not early-exit.

        We supply a bearish 1m recent so _find_last_opposite_5m_bar returns an
        opposite bar and a signal is emitted.
        """
        write_hypothesis(direction="up")
        write_position(failed_entries=2)
        # close=100 so CPR=(100-90)/20=0.5>=0.40 passes the entry quality filter.
        bar = make_5m_bar(open_=105.0, close=100.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, make_opp_1m_recent("up"))

        assert result is not None


class TestNoPositionOppositeBar:

    def test_new_opposite_5m_emits_new_limit_entry(self):
        """Empty position, direction=up, bearish 1m bars → new-limit-entry at body_high."""
        write_hypothesis(direction="up")
        write_position()
        # 1m bars: open=105, close=95 → body_high=105 (bearish for up direction)
        # bar_open=99 so approach = 105-99 = 6 ≥ 5 → limit entry (not market)
        bar = make_5m_bar(open_=99.0, high=110.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "new-limit-entry"
        assert result["price"] == pytest.approx(105.0)  # body_high of bearish 5m bar

        pos = smt_state.load_position()
        assert pos["limit_entry"] == pytest.approx(105.0)
        assert pos["confirmation_bar"] != {}
        assert pos["confirmation_bar"]["body_high"] == pytest.approx(105.0)

    def test_second_opposite_5m_emits_move_limit_entry(self):
        """Existing limit_entry + new opposite 1m bars → move-limit-entry, limit updated."""
        write_hypothesis(direction="up")
        write_position(
            limit_entry=105.0,
            confirmation_bar={
                "time": "2026-04-27T09:55:00-04:00",
                "high": 108.0, "low": 92.0,
                "body_high": 105.0, "body_low": 102.0,
            },
        )
        # 1m bars: open=107, close=97 → body_high=107 (new opposite bar)
        # bar_open=101 so approach = 107-101 = 6 ≥ 5 → limit entry
        bar = make_5m_bar(open_=101.0, high=112.0, low=88.0, close=97.0)
        recent = make_opp_1m_recent("up", open_=107.0, close_=97.0, high=112.0, low=88.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "move-limit-entry"
        assert result["price"] == pytest.approx(107.0)  # body_high of new 1m-based 5m bar

        pos = smt_state.load_position()
        assert pos["limit_entry"] == pytest.approx(107.0)

    def test_non_opposite_5m_no_signal_no_mutation(self):
        """direction=up, bullish bar (close > open) → None, no JSON changes."""
        write_hypothesis(direction="up")
        write_position()
        # Bullish bar — same direction as hypothesis
        bar = make_5m_bar(open_=95.0, high=110.0, low=90.0, close=105.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is None
        pos = smt_state.load_position()
        assert pos["limit_entry"] == ""
        assert pos["confirmation_bar"] == {}


class TestFill:

    def test_5m_bar_crossing_limit_emits_filled_and_writes_active(self):
        """Bar range spans limit_entry with no new opposite bar → limit-entry-filled.

        body_low=94 so stop distance = |100-94| = 6 ≥ MIN_STOP_DISTANCE(5).
        """
        write_hypothesis(direction="up")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 105.0, "low": 95.0,
            "body_high": 103.0, "body_low": 94.0,
        }
        write_position(limit_entry=100.0, confirmation_bar=conf)
        # Bullish bar (non-opposite for direction=up) whose range spans 100
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "limit-entry-filled"
        assert result["price"] == pytest.approx(100.0)

        pos = smt_state.load_position()
        assert pos["active"] != {}
        assert pos["active"]["fill_price"] == pytest.approx(100.0)
        assert pos["active"]["direction"] == "up"
        assert pos["active"]["contracts"] == 2
        assert pos["active"]["cautious"] == "no"
        assert pos["limit_entry"] == ""
        assert pos["confirmation_bar"] == {}

    def test_stop_side_short(self):
        """SHORT fill: stop = confirmation_bar body_high (not wick high).

        body_high=105, stop distance = |100-105| = 5, NOT < 5 → fills.
        """
        write_hypothesis(direction="down")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 110.0, "low": 90.0,
            "body_high": 105.0, "body_low": 98.0,
        }
        write_position(limit_entry=100.0, confirmation_bar=conf)
        bar = make_5m_bar(open_=101.0, high=103.0, low=98.0, close=99.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "limit-entry-filled"

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(105.0)  # confirmation_bar body_high for short

    def test_stop_side_long(self):
        """LONG fill: stop = confirmation_bar body_low (not wick low).

        body_low=94, stop distance = |100-94| = 6 ≥ MIN_STOP_DISTANCE(5) → fills.
        """
        write_hypothesis(direction="up")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 110.0, "low": 95.0,
            "body_high": 105.0, "body_low": 94.0,
        }
        write_position(limit_entry=100.0, confirmation_bar=conf)
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "limit-entry-filled"

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(94.0)  # confirmation_bar body_low for long


class TestActivePosition:

    def test_in_position_direction_mismatch_emits_market_close(self):
        """active.direction=up, hypothesis.direction=down → market-close + direction-mismatch."""
        write_hypothesis(direction="down")
        write_position(active={
            "time": NOW.isoformat(), "fill_price": 100.0, "direction": "up",
            "stop": 95.0, "contracts": 2, "cautious": "no",
        })
        bar = make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "market-close"
        assert result.get("reason") == "direction-mismatch"

        pos = smt_state.load_position()
        assert pos["active"] == {}
        assert pos["limit_entry"] == ""

    def test_in_position_direction_none_emits_market_close(self):
        """active.direction=up, hypothesis.direction=none → market-close."""
        write_hypothesis(direction="none")
        write_position(active={
            "time": NOW.isoformat(), "fill_price": 100.0, "direction": "up",
            "stop": 95.0, "contracts": 2, "cautious": "no",
        })
        bar = make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "market-close"
        assert result.get("reason") == "direction-mismatch"

        pos = smt_state.load_position()
        assert pos["active"] == {}

    def test_in_position_stop_crossed_emits_stopped_out_and_increments_failed(self):
        """LONG: bar.low <= stop → stopped-out, failed_entries incremented."""
        write_hypothesis(direction="up")
        write_position(
            active={
                "time": NOW.isoformat(), "fill_price": 105.0, "direction": "up",
                "stop": 100.0, "contracts": 2, "cautious": "no",
            },
            failed_entries=0,
        )
        # Bar whose low crosses the stop
        bar = make_5m_bar(open_=103.0, high=106.0, low=99.0, close=102.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stopped-out"

        pos = smt_state.load_position()
        assert pos["failed_entries"] == 1
        assert pos["active"] == {}


class TestSameBarOverride:

    def test_same_bar_new_confirmation_and_fill_emits_only_move(self):
        """New opposite 1m bars take priority over an existing limit crossing:
        must emit move-limit-entry, not limit-entry-filled.

        1m bars: open=112, close=102 → body_high=112.
        bar_open=105, approach=7 ≥ 5 → limit entry at 112 (not market).
        The bar's range [90, 110] spans old limit 100, but the new-opposite
        code path fires first and returns before the fill check.
        """
        write_hypothesis(direction="up")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 108.0, "low": 92.0,
            "body_high": 105.0, "body_low": 98.0,
        }
        write_position(limit_entry=100.0, confirmation_bar=conf)
        bar = make_5m_bar(open_=105.0, high=110.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=112.0, close_=102.0, high=115.0, low=88.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "move-limit-entry"

        pos = smt_state.load_position()
        assert pos["active"] == {}
        assert pos["limit_entry"] == pytest.approx(112.0)


class TestSignalShape:

    def test_signal_record_shape(self):
        """Any returned signal must have kind, time, price keys and be JSON-serialisable."""
        write_hypothesis(direction="up")
        write_position()
        # close=100 so CPR=(100-90)/20=0.5>=0.40 passes the entry quality filter.
        bar = make_5m_bar(open_=105.0, high=110.0, low=90.0, close=100.0)
        # Bearish 1m bars so _find_last_opposite_5m_bar returns an opposite bar → signal emitted.
        result = run_strategy(NOW, bar, make_opp_1m_recent("up"))

        assert result is not None
        assert "kind" in result
        assert "time" in result
        assert "price" in result
        # Must be JSON-serialisable
        json.dumps(result)
