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
        """SHORT: bar_mid within proximity above the sell entry -> stop = bar_mid + risk,
        entry = bar_mid. Intended risk = |stop_loss - entry_price| = |202 - 190| = 12."""
        write_hypothesis(direction="down")
        write_position()
        # bar_open=200; opp(bullish) body_low=194 -> entry_price=min(194,190)=190
        # opp_high=202 -> stop_loss=min(202, 198+15)=202 ; risk=12
        # bar high=200 low=185 -> bar_mid=192.5, within 5 of entry 190 -> will market-fill
        bar = make_5m_bar(open_=200.0, high=200.0, low=185.0, close=190.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(192.5), "entry re-anchored to bar_mid"
        assert result["stop"] == pytest.approx(204.5), "stop = bar_mid(192.5) + risk(12)"
        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(192.5)
        assert pos["pending_stop"] == pytest.approx(204.5)

    def test_fix1_long_anchors_stop_to_bar_mid_minus_risk(self):
        """LONG mirror: bar_mid within proximity below the buy entry -> stop = bar_mid - risk,
        entry = bar_mid. risk = |stop_loss - entry_price| = |198 - 210| = 12."""
        write_hypothesis(direction="up")
        write_position()
        # bar_open=200; opp(bearish) body_high=206 -> entry_price=max(206,210)=210
        # opp_low=198 -> stop_loss=max(198, 202-15)=198 ; risk=12
        # bar high=215 low=200 -> bar_mid=207.5, within 5 of entry 210 -> will market-fill
        bar = make_5m_bar(open_=200.0, high=215.0, low=200.0, close=210.0)
        recent = make_opp_1m_recent("up", open_=206.0, close_=202.0, high=210.0, low=198.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(207.5), "entry re-anchored to bar_mid"
        assert result["stop"] == pytest.approx(195.5), "stop = bar_mid(207.5) - risk(12)"
        pos = smt_state.load_position()
        assert pos["stop_entry"] == pytest.approx(207.5)
        assert pos["pending_stop"] == pytest.approx(195.5)

    # --- Regression: far resting stop entries are untouched ------------------

    def test_far_resting_stop_entry_unchanged(self):
        """SHORT, bar_mid well outside proximity -> entry/stop unchanged vs current behavior."""
        write_hypothesis(direction="down")
        write_position()
        # entry_price=min(194,190)=190 ; stop_loss=min(202,213)=202 (as Fix1 case)
        # bar high=220 low=200 -> bar_mid=210 ; 210 <= 190+5? no -> no re-anchor, no chase-skip
        bar = make_5m_bar(open_=200.0, high=220.0, low=200.0, close=205.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=198.0, high=202.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is not None
        assert result["kind"] in ("new-stop-entry", "move-stop-entry")
        assert result["price"] == pytest.approx(190.0), "far entry unchanged"
        assert result["stop"] == pytest.approx(202.0), "far stop unchanged"

    def test_proximity_entry_still_rejected_when_anchored_stop_too_close(self):
        """Non-proximity entry whose natural stop is < MIN_STOP_DISTANCE returns None
        (the existing relative-to-entry check at lines 409/411 still fires)."""
        write_hypothesis(direction="down")
        write_position()
        # opp bar range tiny: body_low=194, body_high=196, high=197 -> stop_loss=197
        # bar_open=210 -> entry_price=min(194,200)=194 ; stop_loss-entry=197-194=3 < 5
        # bar high=215 low=205 -> bar_mid=210 ; 210<=194+5? no -> no re-anchor -> reject
        bar = make_5m_bar(open_=210.0, high=215.0, low=205.0, close=208.0)
        recent = make_opp_1m_recent("down", open_=194.0, close_=196.0, high=197.0, low=190.0)
        result = run_strategy(NOW, bar, recent)

        assert result is None, "stop within MIN_STOP_DISTANCE of entry must reject"
