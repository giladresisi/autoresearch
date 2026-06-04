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
from strategy import (
    run_strategy,
    _session_mids,
    _first_target_ahead,
    _nearest_opposing_level,
    _headroom_ok,
    MIN_HEADROOM_PTS,
)


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
    # Write global.json with confidence="medium" so existing tests are unaffected
    # by the new confidence="high" entry-blocking branch in strategy.py.
    (tmp_path / "global.json").write_text(
        '{"all_time_high": 0.0, "confidence": "medium", "trend": "up"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")


def write_hypothesis(direction="none", **kwargs):
    h = copy.deepcopy(smt_state.DEFAULT_HYPOTHESIS)
    h["direction"] = direction
    h.update(kwargs)
    smt_state.save_hypothesis(h)
    return h


def write_daily(day_high=None, day_low=None, week_high=None, week_low=None):
    """Write daily.json with level liquidities so the R2/R3 headroom gate has mids."""
    d = copy.deepcopy(smt_state.DEFAULT_DAILY)
    liq = []
    for name, price in (("day_high", day_high), ("day_low", day_low),
                        ("week_high", week_high), ("week_low", week_low)):
        if price is not None:
            liq.append({"name": name, "price": price, "kind": "level"})
    d["liquidities"] = liq
    smt_state.save_daily(d)
    return d


def write_position(
    active=None,
    stop_entry="",
    conf_bar_entry=None,
    pending_stop=None,
    failed_entries=0,
):
    p = copy.deepcopy(smt_state.DEFAULT_POSITION)
    p["active"]            = active if active is not None else {}
    p["stop_entry"]        = stop_entry
    p["conf_bar_entry"]  = conf_bar_entry if conf_bar_entry is not None else {}
    p["pending_stop"]      = pending_stop
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
        # bar_open=88 so approach = 105-88 = 17 ≥ 15 → limit entry (not market)
        bar = make_5m_bar(open_=88.0, high=110.0, low=75.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "new-stop-entry"
        assert result["price"] == pytest.approx(105.0)  # body_high of bearish 5m bar
        # Stop = max(low=90, body_low-cap=80) = 90 (wick within cap → uses actual low).
        assert result["stop"] == pytest.approx(90.0)

        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(105.0)
        assert pos["conf_bar_entry"] != {}
        assert pos["conf_bar_entry"]["body_high"] == pytest.approx(105.0)

    def test_second_opposite_5m_emits_move_limit_entry(self):
        """Existing limit_entry + new opposite 1m bars → move-limit-entry, limit updated.

        bar_high=104 < existing limit 105 so the fill check does NOT fire.
        New opp bar body_high=122; approach = 122-101 = 21 ≥ 15 → limit entry path.
        """
        write_hypothesis(direction="up")
        write_position(
            stop_entry=105.0,
            conf_bar_entry={
                "time": "2026-04-27T09:55:00-04:00",
                "high": 108.0, "low": 92.0,
                "body_high": 105.0, "body_low": 102.0,
            },
        )
        # bar_high=104 keeps bar below existing limit=105 → fill check skipped.
        # New opp bar body_high=122; approach = 122-101 = 21 ≥ 15 → limit path.
        bar = make_5m_bar(open_=101.0, high=104.0, low=88.0, close=97.0)
        recent = make_opp_1m_recent("up", open_=122.0, close_=102.0, high=125.0, low=85.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "move-stop-entry"
        assert result["price"] == pytest.approx(122.0)
        # Stop = max(low=85, body_low-cap=87) = 87 (wick 17 pts > cap 15 → capped).
        assert "stop" in result
        assert result["stop"] == pytest.approx(87.0)

        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(122.0)

    def test_non_opposite_5m_no_signal_no_mutation(self):
        """direction=up, bullish bar (close > open) → None, no JSON changes."""
        write_hypothesis(direction="up")
        write_position()
        # Bullish bar — same direction as hypothesis
        bar = make_5m_bar(open_=95.0, high=110.0, low=90.0, close=105.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is None
        pos = smt_state.load_position()
        assert pos["stop_entry"] == ""
        assert pos["conf_bar_entry"] == {}


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
        write_position(stop_entry=100.0, conf_bar_entry=conf)
        # Bullish bar (non-opposite for direction=up) whose range spans 100
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stop-entry-filled"
        assert result["price"] == pytest.approx(100.0)

        pos = smt_state.load_position()
        assert pos["active"] != {}
        assert pos["active"]["fill_price"] == pytest.approx(100.0)
        assert pos["active"]["direction"] == "up"
        assert pos["active"]["contracts"] == 2
        assert pos["active"]["cautious"] == "no"
        assert pos["stop_entry"] == ""
        # conf_bar_entry is intentionally preserved after fill so the same bar
        # cannot be reused as confirmation for re-entry after a stop-out.
        assert pos["conf_bar_entry"] != {}

    def test_stop_side_short(self):
        """SHORT fill: stop = min(conf.high, body_high + 10pt cap).

        conf.high=110, body_high=105 → stop=min(110, 115)=110; distance=|100-110|=10 → fills.
        """
        write_hypothesis(direction="down")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 110.0, "low": 90.0,
            "body_high": 105.0, "body_low": 98.0,
        }
        write_position(stop_entry=100.0, conf_bar_entry=conf)
        bar = make_5m_bar(open_=101.0, high=103.0, low=98.0, close=99.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stop-entry-filled"

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(110.0)  # wick end capped at body_high+10

    def test_stop_side_long(self):
        """LONG fill: stop = max(conf.low, body_low - 10pt cap).

        conf.low=95, body_low=94 → stop=max(95, 84)=95; distance=|100-95|=5 → fills.
        """
        write_hypothesis(direction="up")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 110.0, "low": 95.0,
            "body_high": 105.0, "body_low": 94.0,
        }
        write_position(stop_entry=100.0, conf_bar_entry=conf)
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stop-entry-filled"

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(95.0)  # wick end capped at body_low-10

    def test_fill_uses_bar_state_when_conf_bar_empty(self, tmp_path, monkeypatch):
        """Manual stop entry (empty conf_bar_entry): fill uses bar_state.potential_stop_long."""
        write_hypothesis(direction="up")
        # stop_entry set but conf_bar_entry empty (manual path via trade.py)
        write_position(stop_entry=100.0, conf_bar_entry={})

        # Write bar_state.json with potential_stop_long
        monkeypatch.chdir(tmp_path)
        import datetime as _dt
        today = _dt.date.today().isoformat()
        (tmp_path / "sessions" / today).mkdir(parents=True, exist_ok=True)
        (tmp_path / "sessions" / today / "bar_state.json").write_text(
            '{"time": "x", "potential_stop_long": 93.0, "potential_stop_short": 107.0}',
            encoding="utf-8",
        )

        # Bar that crosses the stop entry; distance |100-93|=7 ≥ MIN_STOP_DISTANCE
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stop-entry-filled"
        assert result["price"] == pytest.approx(100.0)

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(93.0)

    def test_fill_uses_pending_stop_when_set(self):
        """Fill path reads pending_stop from position.json when present, ignoring conf_bar."""
        write_hypothesis(direction="up")
        # pending_stop=92.0 stored; conf_bar would compute stop=95 if used instead
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 105.0, "low": 95.0,
            "body_high": 103.0, "body_low": 94.0,
        }
        write_position(stop_entry=100.0, conf_bar_entry=conf, pending_stop=92.0)
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        assert result is not None
        assert result["kind"] == "stop-entry-filled"
        assert result["stop"] == pytest.approx(92.0)

        pos = smt_state.load_position()
        assert pos["active"]["stop"] == pytest.approx(92.0)

    def test_fill_skips_when_conf_bar_empty_and_no_bar_state(self, tmp_path, monkeypatch):
        """Manual stop entry + no bar_state.json → fill is skipped (returns None)."""
        write_hypothesis(direction="up")
        write_position(stop_entry=100.0, conf_bar_entry={})

        # No bar_state.json exists in tmp_path
        monkeypatch.chdir(tmp_path)

        # Bar that crosses the stop entry
        bar = make_5m_bar(open_=99.0, high=102.0, low=98.0, close=101.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        # Fill skipped due to missing bar_state.json
        assert result is None
        pos = smt_state.load_position()
        assert pos["active"] == {}
        # stop_entry preserved (no fill, no clear)
        assert pos["stop_entry"] == pytest.approx(100.0)


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
        assert pos["stop_entry"] == ""

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

    def test_in_position_manual_entry_exempt_from_direction_mismatch_close(self):
        """D7: a manual position (source='manual') is NOT auto-closed on a direction
        mismatch — discretionary trades are left to the user / their own broker stop."""
        write_hypothesis(direction="down")
        write_position(active={
            "time": NOW.isoformat(), "fill_price": 100.0, "direction": "up",
            "stop": 95.0, "contracts": 2, "cautious": "no", "source": "manual",
        })
        bar = make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())

        # No market-close fires; the manual position is preserved untouched.
        assert result is None
        pos = smt_state.load_position()
        assert pos["active"].get("source") == "manual"
        assert pos["active"].get("direction") == "up"

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

    def test_fill_beats_new_opposite_bar_on_same_bar(self):
        """Fill check runs before opposite-bar check: when bar spans existing limit AND
        a new opposite bar exists, the fill is detected and limit-entry-filled is returned.

        1m bars: open=112, close=102 → body_high=112 (new opposite bar).
        Bar range [90, 110] spans old limit 100 → fill fires first (code section 2.4).
        """
        write_hypothesis(direction="up")
        conf = {
            "time": "2026-04-27T09:55:00-04:00",
            "high": 108.0, "low": 92.0,
            "body_high": 105.0, "body_low": 98.0,
        }
        write_position(stop_entry=100.0, conf_bar_entry=conf)
        bar = make_5m_bar(open_=105.0, high=110.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=112.0, close_=102.0, high=115.0, low=88.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] == "stop-entry-filled"
        assert result["price"] == pytest.approx(100.0)

        pos = smt_state.load_position()
        assert pos["active"] != {}
        assert pos["stop_entry"] == ""


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


class TestConfidenceHighBlocksEntry:

    def _write_confidence_high(self, tmp_path_fixture=None):
        """Overwrite global.json with confidence='high' for this test."""
        smt_state.save_global({"all_time_high": 0.0, "confidence": "high", "trend": "up"})

    def test_confidence_high_blocks_limit_entry(self, tmp_path):
        """confidence='high' → no new-limit-entry signal even with a valid opposite 5m bar."""
        self._write_confidence_high()
        write_hypothesis(direction="up")
        write_position()
        bar = make_5m_bar(open_=99.0, high=110.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)
        assert result is None, (
            f"confidence='high' must block new limit entries, got {result}"
        )

    def test_confidence_high_blocks_market_entry(self, tmp_path):
        """confidence='high' → no market-entry signal even when price is at the limit."""
        self._write_confidence_high()
        write_hypothesis(direction="up")
        # approach < threshold triggers market entry normally
        bar = make_5m_bar(open_=104.0, high=110.0, low=90.0, close=100.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)
        assert result is None, (
            f"confidence='high' must block market entries, got {result}"
        )

    def test_confidence_medium_allows_entry(self, tmp_path):
        """confidence='medium' (default fixture) → entry proceeds normally."""
        # global.json already has confidence="medium" from the _isolate fixture.
        write_hypothesis(direction="up")
        write_position()
        # close=100 → CPR=(100-90)/20=0.50 ≥ 0.40 passes the entry quality filter.
        bar = make_5m_bar(open_=99.0, high=110.0, low=90.0, close=100.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None, "confidence='medium' must not block entries"


class TestStopEntryMinApproach:
    """Stop entry price is pushed forward when the natural level is too close to bar open.

    If the confirmation bar body_end_price is within MIN_APPROACH_PTS (10 pts) of the
    current bar's open, the strategy must push the entry further in the trade direction
    so Tradovate does not reject the order due to market price already being at/past
    the trigger level by the time the order reaches the exchange.
    """

    def test_up_direction_pushes_entry_when_too_close(self):
        """UP: body_high only 6 pts above bar_open → entry pushed to bar_open + 10."""
        write_hypothesis(direction="up")
        write_position()
        # bar_open=100; opp bar body_high=106 → approach=6 < MIN_APPROACH_PTS(10)
        # Expected: entry_price = max(106, 100+10) = 110
        bar = make_5m_bar(open_=100.0, high=120.0, low=80.0, close=115.0)
        # Bearish opp bar: open=106 > close=102 → body_high=106
        recent = make_opp_1m_recent("up", open_=106.0, close_=102.0, high=110.0, low=98.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(110.0), (
            f"entry should be pushed to bar_open+10=110, got {result['price']}"
        )
        assert smt_state.load_position()["stop_entry"] == pytest.approx(110.0)

    def test_up_direction_no_push_when_already_far_enough(self):
        """UP: body_high 17 pts above bar_open → natural level kept, no push."""
        write_hypothesis(direction="up")
        write_position()
        # bar_open=88; opp bar body_high=105 → approach=17 ≥ MIN_APPROACH_PTS(10)
        # Expected: entry_price = max(105, 88+10) = 105 (no push)
        bar = make_5m_bar(open_=88.0, high=110.0, low=75.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=110.0, low=90.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(105.0), (
            f"natural entry 105 is already ≥10 pts away from open 88, got {result['price']}"
        )

    def test_down_direction_pushes_entry_when_too_close(self):
        """DOWN: body_low only 6 pts below bar_open → entry pushed to bar_open - 10."""
        write_hypothesis(direction="down")
        write_position()
        # bar_open=200; opp bar body_low=194 → approach=6 < MIN_APPROACH_PTS(10)
        # Expected: entry_price = min(194, 200-10) = 190
        bar = make_5m_bar(open_=200.0, high=220.0, low=180.0, close=185.0)
        # Bullish opp bar: open=194 < close=198 → body_low=194
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(190.0), (
            f"entry should be pushed to bar_open-10=190, got {result['price']}"
        )


class TestStopEntryMktDowngradeSafety:
    """STP->MKT downgrade safety (feature.md fixes 1-3).

    When a stop entry would be market-filled by the executor (entry within
    STP_MKT_PROXIMITY_PTS of bar_mid), the strategy must re-anchor the protective
    stop to the expected market fill (bar_mid) so the stop survives async drift,
    and record entry_price = bar_mid so position.json matches the market fill.
    Far (non-proximity) resting stop entries must be unchanged.
    """

    # --- Fix 1: re-anchor stop to expected market fill -----------------------

    def test_fix1_short_anchors_stop_to_bar_mid_plus_risk(self):
        """SHORT: bar_mid has REACHED the sell trigger (R1) -> stop = bar_mid + risk,
        entry = bar_mid. Intended risk = |stop_loss - entry_price| = |202 - 190| = 12.
        R1 update: bar_mid must now be at/below entry (190), not merely within 5 pts above."""
        write_hypothesis(direction="down")
        write_position()
        # bar_open=200; opp(bullish) body_low=194 -> entry_price=min(194,190)=190
        # opp_high=202 -> stop_loss=min(202, 198+15)=202 ; risk=12
        # bar high=193 low=185 -> bar_mid=189 <= entry 190 (trigger reached) and > 180 (no chase)
        bar = make_5m_bar(open_=200.0, high=193.0, low=185.0, close=190.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(189.0), "entry re-anchored to bar_mid"
        assert result["stop"] == pytest.approx(201.0), "stop = bar_mid(189) + risk(12)"
        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(189.0)
        assert pos["pending_stop"] == pytest.approx(201.0)

    def test_fix1_long_anchors_stop_to_bar_mid_minus_risk(self):
        """LONG mirror: bar_mid has REACHED the buy trigger (R1) -> stop = bar_mid - risk,
        entry = bar_mid. risk = |stop_loss - entry_price| = |198 - 210| = 12.
        R1 update: bar_mid must now be at/above entry (210), not merely within 5 pts below."""
        write_hypothesis(direction="up")
        write_position()
        # bar_open=200; opp(bearish) body_high=206 -> entry_price=max(206,210)=210
        # opp_low=198 -> stop_loss=max(198, 202-15)=198 ; risk=12
        # bar high=222 low=200 -> bar_mid=211 >= entry 210 (trigger reached) and < 220 (no chase)
        bar = make_5m_bar(open_=200.0, high=222.0, low=200.0, close=210.0)
        recent = make_opp_1m_recent("up", open_=206.0, close_=202.0, high=210.0, low=198.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(211.0), "entry re-anchored to bar_mid"
        assert result["stop"] == pytest.approx(199.0), "stop = bar_mid(211) - risk(12)"
        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(211.0)
        assert pos["pending_stop"] == pytest.approx(199.0)

    # --- Fix 2: floor the re-anchored stop at MKT_FILL_MIN_STOP_DISTANCE -----

    def test_fix2_floor_widens_stop_when_risk_below_floor(self):
        """SHORT: intended risk (7) < MKT_FILL_MIN_STOP_DISTANCE -> stop distance from
        bar_mid equals the floor (10), not the smaller intended risk.
        R1 update: bar_mid must now be at/below entry (190) to market-fill."""
        write_hypothesis(direction="down")
        write_position()
        # opp body_low=194, body_high=196, high=197 -> stop_loss=min(197,211)=197
        # bar_open=200 -> entry_price=min(194,190)=190 ; risk=|197-190|=7 (< floor 10)
        # bar high=193 low=185 -> bar_mid=189 <= entry 190 (trigger reached) and > 180 (no chase)
        bar = make_5m_bar(open_=200.0, high=193.0, low=185.0, close=190.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=196.0, high=197.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["price"] == pytest.approx(189.0)
        assert result["stop"] == pytest.approx(199.0), "stop = bar_mid(189) + floor(10)"
        assert abs(result["stop"] - result["price"]) == pytest.approx(10.0), (
            "stop distance from market fill must equal MKT_FILL_MIN_STOP_DISTANCE"
        )

    # --- Fix 3: skip the chase when market ran past the intended entry -------

    def test_fix3_short_skips_when_market_ran_below_entry(self):
        """SHORT: bar_mid more than MAX_ENTRY_CHASE_PTS below the sell entry -> no signal."""
        write_hypothesis(direction="down")
        write_position()
        # entry_price=min(194,190)=190 ; bar high=185 low=170 -> bar_mid=177.5
        # 177.5 < 190 - 10 -> chase guard fires
        bar = make_5m_bar(open_=200.0, high=185.0, low=170.0, close=175.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is None, "market ran > MAX_ENTRY_CHASE_PTS past entry -> skip"

    def test_fix3_long_skips_when_market_ran_above_entry(self):
        """LONG: bar_mid more than MAX_ENTRY_CHASE_PTS above the buy entry -> no signal."""
        write_hypothesis(direction="up")
        write_position()
        # entry_price=max(206,210)=210 ; bar high=240 low=205 -> bar_mid=222.5
        # 222.5 > 210 + 10 -> chase guard fires
        bar = make_5m_bar(open_=200.0, high=240.0, low=205.0, close=230.0)
        recent = make_opp_1m_recent("up", open_=206.0, close_=202.0, high=210.0, low=198.0)
        result = run_strategy(NOW, bar, recent)

        assert result is None, "market ran > MAX_ENTRY_CHASE_PTS past entry -> skip"

    # --- Regression: far resting stop entries are untouched ------------------

    def test_far_resting_stop_entry_unchanged(self):
        """SHORT, bar_mid above the sell trigger (un-reached) -> rests; entry/stop unchanged."""
        write_hypothesis(direction="down")
        write_position()
        # entry_price=min(194,190)=190 ; stop_loss=min(202,213)=202 (as Fix1 case)
        # bar high=220 low=200 -> bar_mid=210 ; 210 <= 190? no -> trigger un-reached -> resting stop
        bar = make_5m_bar(open_=200.0, high=220.0, low=200.0, close=205.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(190.0), "far entry unchanged"
        assert result["stop"] == pytest.approx(202.0), "far stop unchanged"

    def test_proximity_entry_still_rejected_when_anchored_stop_too_close(self):
        """Un-reached entry whose natural stop is < MIN_STOP_DISTANCE returns None
        (the existing relative-to-entry check still fires)."""
        write_hypothesis(direction="down")
        write_position()
        # opp bar range tiny: body_low=194, body_high=196, high=197 -> stop_loss=197
        # bar_open=210 -> entry_price=min(194,200)=194 ; stop_loss-entry=197-194=3 < 5
        # bar high=215 low=205 -> bar_mid=210 ; 210<=194? no -> no re-anchor -> reject
        bar = make_5m_bar(open_=210.0, high=215.0, low=205.0, close=208.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=196.0, high=197.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is None, "stop within MIN_STOP_DISTANCE of entry must reject"

    # --- R1: will_market_fill mirror — downgrade only when trigger reached ----

    def test_r1_wmf_long_below_trigger_stays_resting_stop(self):
        """LONG: bar_mid below the buy trigger -> NOT market-filled; rests at the natural
        entry/stop (no re-anchor to bar_mid). The backtest mirror of the 00:30 case."""
        write_hypothesis(direction="up")
        write_position()
        # opp(bearish) body_high=206 -> entry_price=max(206, 200+10)=210
        # opp_low=198 -> stop_loss=max(198, 202-15)=198
        # bar high=205 low=200 -> bar_mid=202.5 < entry 210 -> trigger un-reached -> resting stop
        bar = make_5m_bar(open_=200.0, high=205.0, low=200.0, close=203.0)
        recent = make_opp_1m_recent("up", open_=206.0, close_=202.0, high=210.0, low=198.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(210.0), "entry NOT re-anchored (resting stop)"
        assert result["stop"] == pytest.approx(198.0), "natural stop unchanged"
        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(210.0)
        assert pos["pending_stop"] == pytest.approx(198.0)

    def test_r1_wmf_short_above_trigger_stays_resting_stop(self):
        """SHORT mirror: bar_mid above the sell trigger -> resting stop, no re-anchor."""
        write_hypothesis(direction="down")
        write_position()
        # entry_price=min(194,190)=190 ; stop_loss=min(202,213)=202
        # bar high=200 low=196 -> bar_mid=198 > entry 190 -> trigger un-reached -> resting stop
        bar = make_5m_bar(open_=200.0, high=200.0, low=196.0, close=198.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(190.0), "entry NOT re-anchored (resting stop)"
        assert result["stop"] == pytest.approx(202.0), "natural stop unchanged"


# ---------------------------------------------------------------------------
# R3 foundation: headroom / mid helpers (pure functions)
# ---------------------------------------------------------------------------

def _levels(day_high=None, day_low=None, week_high=None, week_low=None):
    """Build a liquidities list of level entries from the supplied bounds."""
    out = []
    for name, price in (
        ("day_high", day_high), ("day_low", day_low),
        ("week_high", week_high), ("week_low", week_low),
    ):
        if price is not None:
            out.append({"name": name, "price": price, "kind": "level"})
    return out


class TestHeadroomHelpers:

    def test_session_mids_from_liquidities(self):
        liq = _levels(day_high=120.0, day_low=100.0, week_high=130.0, week_low=90.0)
        daily_mid, weekly_mid = _session_mids(liq)
        assert daily_mid == pytest.approx(110.0)
        assert weekly_mid == pytest.approx(110.0)

    def test_session_mids_missing_week_bound_is_none(self):
        liq = _levels(day_high=120.0, day_low=100.0, week_high=130.0)  # no week_low
        daily_mid, weekly_mid = _session_mids(liq)
        assert daily_mid == pytest.approx(110.0)
        assert weekly_mid is None

    def test_first_target_ahead_up_picks_nearest_above(self):
        targets = [{"name": "a", "price": 130.0}, {"name": "b", "price": 115.0},
                   {"name": "behind", "price": 90.0}]
        assert _first_target_ahead(100.0, "up", targets) == pytest.approx(115.0)

    def test_first_target_ahead_down_picks_nearest_below(self):
        targets = [{"name": "a", "price": 70.0}, {"name": "b", "price": 85.0},
                   {"name": "behind", "price": 130.0}]
        assert _first_target_ahead(100.0, "down", targets) == pytest.approx(85.0)

    def test_first_target_ahead_none_when_all_behind(self):
        assert _first_target_ahead(100.0, "up", [{"name": "x", "price": 90.0}]) is None

    def test_nearest_opposing_level_up_picks_nearest_ahead(self):
        # daily_mid=140 (ahead), weekly_mid=115 (ahead, nearest), target=130 (ahead)
        lvl = _nearest_opposing_level(100.0, "up", 140.0, 115.0,
                                      [{"name": "t", "price": 130.0}])
        assert lvl == pytest.approx(115.0)

    def test_nearest_opposing_level_ignores_levels_behind(self):
        # weekly_mid below entry must be ignored for an up trade
        lvl = _nearest_opposing_level(100.0, "up", 140.0, 80.0, [])
        assert lvl == pytest.approx(140.0)

    def test_nearest_opposing_level_none_when_all_behind(self):
        assert _nearest_opposing_level(100.0, "up", 90.0, 80.0, []) is None

    def test_headroom_ok_passes_when_no_level_ahead(self):
        # No opposing level ahead -> open road -> always OK regardless of risk.
        assert _headroom_ok(100.0, 90.0, "up", _levels(day_high=95.0, day_low=80.0), []) is True

    def test_headroom_ok_rejects_below_risk(self):
        # headroom 8 (mid at 108) < risk 12 (stop 88) -> reward:risk < 1 -> reject.
        liq = _levels(day_high=116.0, day_low=100.0)  # daily_mid=108
        assert _headroom_ok(100.0, 88.0, "up", liq, []) is False

    def test_headroom_ok_rejects_below_floor(self):
        # headroom 6 (mid 106), risk 3 (stop 97) -> risk < floor, floor(10) binds, 6 < 10 -> reject.
        liq = _levels(day_high=112.0, day_low=100.0)  # daily_mid=106
        assert MIN_HEADROOM_PTS == pytest.approx(10.0)
        assert _headroom_ok(100.0, 97.0, "up", liq, []) is False

    def test_headroom_ok_passes_above_both(self):
        # headroom 25 (mid 125) >= max(risk 12, floor 10) -> OK.
        liq = _levels(day_high=150.0, day_low=100.0)  # daily_mid=125
        assert _headroom_ok(100.0, 88.0, "up", liq, []) is True

    def test_headroom_ok_short_symmetric(self):
        # SHORT: entry 100, daily_mid=92 (headroom 8) < risk 12 (stop 112) -> reject.
        liq = _levels(day_high=100.0, day_low=84.0)  # daily_mid=92
        assert _headroom_ok(100.0, 112.0, "down", liq, []) is False


# ---------------------------------------------------------------------------
# R2 / R3 / R6: headroom gate on entries + conf/gated attribution
# ---------------------------------------------------------------------------

class TestHeadroomGateMarketEntry:
    """Market-entry path: the headroom gate is scoped to o5-only (decision 2026-06-04 after
    backtest showed a general gate over-rejects breakouts). Non-o5 ('normal') market entries
    fire regardless of headroom and carry conf='normal'."""

    def _setup_long_market(self):
        # opp(bearish) body_high=105 ; bar_open=110 > 105 -> approach<0 -> market path.
        # stop = max(opp_low=93, body_low(95)-15)=93 ; bar_mid=111.5 ; risk=18.5
        write_hypothesis(direction="up")
        write_position()
        bar = make_5m_bar(open_=110.0, high=115.0, low=108.0, close=113.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
        return bar, recent

    def test_normal_market_entry_not_gated_low_headroom(self):
        bar, recent = self._setup_long_market()
        # daily_mid=115 -> headroom 3.5 < risk: a GENERAL gate would reject, but the gate is
        # o5-only, so this NORMAL entry still fires.
        write_daily(day_high=130.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-entry"
        assert result["conf"] == "normal"
        assert smt_state.load_position()["active"] != {}

    def test_r3_market_entry_emitted_with_headroom(self):
        bar, recent = self._setup_long_market()
        # daily_mid=150 -> ample headroom -> fires.
        write_daily(day_high=200.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-entry"
        assert result["conf"] == "normal"
        assert smt_state.load_position()["active"] != {}

    def test_normal_short_market_entry_not_gated_low_headroom(self):
        # SHORT mirror: opp(bullish) body_low=95 ; bar_open=90 < 95 -> approach<0 -> market path.
        # stop = min(opp_high=107, body_high(105)+15)=107 ; bar_mid=88.5 ; risk=18.5
        write_hypothesis(direction="down")
        write_position()
        bar = make_5m_bar(open_=90.0, high=92.0, low=85.0, close=87.0)
        recent = make_opp_1m_recent("down", open_=95.0, close_=105.0, high=107.0, low=93.0)
        # daily_mid=85 -> headroom 3.5 < risk, but o5-only gate -> NORMAL entry still fires.
        write_daily(day_high=100.0, day_low=70.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-entry"
        assert smt_state.load_position()["active"] != {}

    def test_r3_no_levels_means_room(self):
        # No liquidities at all -> open road -> entry fires.
        bar, recent = self._setup_long_market()
        # no write_daily -> liquidities empty
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-entry"


class TestHeadroomGateStopEntry:
    """Stop-entry path: headroom gate is o5-only; non-o5 resting stop entries fire regardless."""

    def _setup_long_stop(self):
        # opp body_high=105 ; bar_open=88 -> approach 17 -> stop path.
        # entry_price=max(105, 98)=105 ; stop_loss=max(93, 80)=93 ; risk=12
        # bar_mid=95 (<105) -> resting stop (no market-fill).
        write_hypothesis(direction="up")
        write_position()
        bar = make_5m_bar(open_=88.0, high=100.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
        return bar, recent

    def test_normal_stop_entry_not_gated_low_headroom(self):
        bar, recent = self._setup_long_stop()
        # entry=105 ; daily_mid=110 -> headroom 5 < risk: o5-only gate -> NORMAL entry still fires.
        write_daily(day_high=120.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "new-stop-entry"
        assert result["conf"] == "normal"
        assert smt_state.load_position()["stop_entry"] == pytest.approx(105.0)

    def test_r3_stop_entry_emitted_with_headroom(self):
        bar, recent = self._setup_long_stop()
        # entry=105 ; daily_mid=150 -> ample headroom -> fires.
        write_daily(day_high=200.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "new-stop-entry"
        assert result["conf"] == "normal"
        assert smt_state.load_position()["stop_entry"] == pytest.approx(105.0)


class TestO5FallbackGate:
    """R2: explicit headroom gate on the same-bar o5 pseudo-conf path (conf='o5')."""

    def _setup_o5_long(self):
        # Bullish prior 5m window (open<close) -> _find_last_bar(down) returns None -> o5 fires.
        # window body_high=30510, body_low=30500, high=30512, low=30498 (span 10 <= 25).
        # current 1m bar agrees up (close>open) ; entry_ranges high 30430 -> 30540-30430=110 >100.
        # bar_open=30530 > body_high 30510 -> approach<0 -> o5 MARKET entry (the 03:05 case).
        # bar_mid=30540 ; stop=max(30498, 30485)=30498 ; risk=42.
        write_hypothesis(direction="up",
                         entry_ranges=[{"source": "12hr", "high": 30430.0, "low": 30420.0}])
        write_position()
        bar = make_5m_bar(open_=30530.0, high=30545.0, low=30535.0, close=30540.0)
        recent = make_opp_1m_recent("up", open_=30500.0, close_=30510.0,
                                    high=30512.0, low=30498.0)
        return bar, recent

    def test_r2_o5_fallback_rejected_no_headroom(self):
        bar, recent = self._setup_o5_long()
        # daily_mid=30545 -> headroom 5 < max(risk 42, floor 10) -> gated as o5.
        write_daily(day_high=30560.0, day_low=30530.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "entry-gated"
        assert result["gated"] == "r2-o5-no-headroom"
        assert result["conf"] == "o5"
        assert smt_state.load_position()["active"] == {}

    def test_r2_o5_fallback_fires_with_headroom(self):
        bar, recent = self._setup_o5_long()
        # daily_mid=30590 -> headroom 50 >= 42 -> o5 entry still fires, tagged o5.
        write_daily(day_high=30700.0, day_low=30480.0)
        result = run_strategy(NOW, bar, recent)
        assert result is not None
        assert result["kind"] == "market-entry"
        assert result["conf"] == "o5"
        assert smt_state.load_position()["active"] != {}

    def test_r2_o5_existing_dist_guard_still_respected(self):
        # entry_range only 40 pts behind (< _O5_FALLBACK_DIST 100) -> _o5_fallback returns None
        # -> no confirmation bar -> no signal (existing guard unaffected by the new gate).
        write_hypothesis(direction="up",
                         entry_ranges=[{"source": "12hr", "high": 30500.0, "low": 30490.0}])
        write_position()
        bar = make_5m_bar(open_=30530.0, high=30545.0, low=30535.0, close=30540.0)
        recent = make_opp_1m_recent("up", open_=30500.0, close_=30510.0,
                                    high=30512.0, low=30498.0)
        write_daily(day_high=30700.0, day_low=30480.0)
        result = run_strategy(NOW, bar, recent)
        assert result is None

    def test_r2_o5_body_guard_still_respected(self):
        # o5 window body span 35 > MAX_CONFIRMATION_BODY_PTS(25) -> conf bar rejected -> None.
        write_hypothesis(direction="up",
                         entry_ranges=[{"source": "12hr", "high": 30430.0, "low": 30420.0}])
        write_position()
        bar = make_5m_bar(open_=30560.0, high=30575.0, low=30565.0, close=30570.0)
        recent = make_opp_1m_recent("up", open_=30500.0, close_=30535.0,
                                    high=30540.0, low=30498.0)  # body span 35
        write_daily(day_high=30900.0, day_low=30480.0)
        result = run_strategy(NOW, bar, recent)
        assert result is None


class TestEntryAttribution:
    """R6: conf ('o5'/'normal') on firing entries; gated reason on suppressed entries."""

    def test_r6_market_entry_tagged_normal(self):
        # Non-o5 market entry with room -> conf == 'normal'.
        write_hypothesis(direction="up")
        write_position()
        bar = make_5m_bar(open_=110.0, high=115.0, low=108.0, close=113.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
        write_daily(day_high=200.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result["kind"] == "market-entry"
        assert result["conf"] == "normal"

    def test_r6_o5_entry_tagged_o5(self):
        # o5 pseudo-conf entry with room -> conf == 'o5'.
        write_hypothesis(direction="up",
                         entry_ranges=[{"source": "12hr", "high": 30430.0, "low": 30420.0}])
        write_position()
        bar = make_5m_bar(open_=30530.0, high=30545.0, low=30535.0, close=30540.0)
        recent = make_opp_1m_recent("up", open_=30500.0, close_=30510.0,
                                    high=30512.0, low=30498.0)
        write_daily(day_high=30700.0, day_low=30480.0)
        result = run_strategy(NOW, bar, recent)
        assert result["kind"] == "market-entry"
        assert result["conf"] == "o5"

    def test_r6_gated_entry_emits_entry_gated(self):
        # Headroom-gated o5 entry -> kind 'entry-gated' with reason 'r2-o5-no-headroom',
        # conf 'o5', and no position. (Gate is o5-only, so the gated case is an o5 entry.)
        write_hypothesis(direction="up",
                         entry_ranges=[{"source": "12hr", "high": 30430.0, "low": 30420.0}])
        write_position()
        bar = make_5m_bar(open_=30530.0, high=30545.0, low=30535.0, close=30540.0)
        recent = make_opp_1m_recent("up", open_=30500.0, close_=30510.0,
                                    high=30512.0, low=30498.0)
        write_daily(day_high=30560.0, day_low=30530.0)  # daily_mid=30545 -> headroom 5 << risk
        result = run_strategy(NOW, bar, recent)
        assert result["kind"] == "entry-gated"
        assert result["gated"] == "r2-o5-no-headroom"
        assert result["conf"] == "o5"
        assert smt_state.load_position()["active"] == {}

    def test_r6_stop_entry_tagged_normal(self):
        # Resting stop-entry with room -> conf == 'normal'.
        write_hypothesis(direction="up")
        write_position()
        bar = make_5m_bar(open_=88.0, high=100.0, low=90.0, close=95.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
        write_daily(day_high=200.0, day_low=100.0)
        result = run_strategy(NOW, bar, recent)
        assert result["kind"] == "new-stop-entry"
        assert result["conf"] == "normal"


def test_origin_coil_winner_not_killed():
    """Anti-kill regression: a clear breakout with ample headroom to the next opposing
    level still enters (the gates must not suppress legitimate origin-coil winners)."""
    write_hypothesis(direction="up")
    write_position()
    # Market breakout: bar opened past the conf body_high, wide room above to daily_mid=300.
    bar = make_5m_bar(open_=110.0, high=115.0, low=108.0, close=113.0)
    recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
    write_daily(day_high=400.0, day_low=200.0)  # daily_mid=300, far above -> open road
    result = run_strategy(NOW, bar, recent)
    assert result is not None
    assert result["kind"] == "market-entry"
    assert smt_state.load_position()["active"] != {}
