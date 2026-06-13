# tests/test_smt_decouple_active.py
# SMT-v2 Phase 1 — Decouple active-position management from the live hypothesis.
#
# Covers: freeze-at-fill (strategy + live_orders paths), trend.py managing off the
# FROZEN snapshot after a hypothesis flip/none, frozen-ladder immutability, the kept
# pending-stop-entry cancel, the removed automatic mismatch close, and back-compat.
# All state files are redirected to tmp_path; no broker/IB/network.

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    load_position,
    load_hypothesis,
)

NOW = datetime(2026, 4, 27, 10, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def redirect_paths(tmp_path, monkeypatch):
    """Redirect all smt_state path constants to tmp_path so tests are isolated."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    # global.json + bar_state resolve under ACT_GLOBAL_DIR — isolate it too so strategy's
    # load_global reads the same global we write here (mirrors test_smt_strategy_v2).
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_hyp_cache_valid", False)
    # trend.py imported these names at import-time; patch them too.
    import trend
    monkeypatch.setattr(trend, "load_global", smt_state.load_global)
    monkeypatch.setattr(trend, "load_hypothesis", smt_state.load_hypothesis)
    monkeypatch.setattr(trend, "save_hypothesis", smt_state.save_hypothesis)
    monkeypatch.setattr(trend, "load_position", smt_state.load_position)
    monkeypatch.setattr(trend, "save_position", smt_state.save_position)
    monkeypatch.setattr(trend, "load_daily", smt_state.load_daily)
    save_global({"all_time_high": 0.0, "confidence": "medium", "trend": "up"})


def _bar(open_=100.0, high=110.0, low=90.0, close=105.0,
         time_str="2026-04-27T10:01:00-04:00") -> dict:
    return {"time": time_str, "open": open_, "high": high, "low": low, "close": close}


def _recent(closes, opens, highs=None, lows=None,
            start="2026-04-27 09:30:00", tz="America/New_York") -> pd.DataFrame:
    if highs is None:
        highs = [max(o, c) + 2 for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) - 2 for o, c in zip(opens, closes)]
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz=tz)
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes}, index=idx)


def _hyp(direction="up", **kw):
    h = copy.deepcopy(DEFAULT_HYPOTHESIS)
    h["direction"] = direction
    h.update(kw)
    save_hypothesis(h)
    return h


def _daily(levels=None):
    d = copy.deepcopy(DEFAULT_DAILY)
    d["liquidities"] = levels or []
    save_daily(d)
    return d


def _frozen_active(direction="up", cautious="no", initial="", initial_lv="",
                   secondary="", secondary_lv="", fill_price=100.0, stop=95.0,
                   cautious_break_price=None, source="strategy"):
    """Build an active sub-dict carrying the frozen snapshot (mgmt_direction up/down)."""
    mgmt = "up" if direction in ("up", "long") else "down"
    tier = "week" if str(secondary_lv).startswith("week") else "day"
    a = {
        "time": "2026-04-27T10:00:00-04:00",
        "fill_price": fill_price,
        "direction": direction,
        "stop": stop,
        "contracts": 2,
        "cautious": cautious,
        "source": source,
        "mgmt_direction": mgmt,
        "cautious_initial": initial,
        "cautious_initial_level": initial_lv,
        "cautious_secondary": secondary,
        "cautious_secondary_level": secondary_lv,
        "backing_tier": tier,
    }
    if cautious_break_price is not None:
        a["cautious_break_price"] = cautious_break_price
    return a


# ===========================================================================
# Freeze at fill — strategy paths
# ===========================================================================

class TestFreezeAtFillStrategy:
    def test_stop_entry_fill_freezes_all_six_fields(self):
        from strategy import run_strategy
        _hyp(direction="up",
             cautious_price_initial="160", cautious_price_initial_level="day_high",
             cautious_price_secondary="200", cautious_price_secondary_level="week_high")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["stop_entry"] = 100.0
        pos["conf_bar_entry"] = {"time": "2026-04-27T09:55:00-04:00",
                                 "high": 105.0, "low": 95.0,
                                 "body_high": 103.0, "body_low": 94.0}
        save_position(pos)
        bar = {"time": "2026-04-27T10:01:00-04:00", "open": 99.0, "high": 102.0,
               "low": 98.0, "close": 101.0, "body_high": 100.5, "body_low": 99.5}
        result = run_strategy(NOW, bar, _recent([100, 101], [99, 100]))
        assert result["kind"] == "stop-entry-filled"
        a = load_position()["active"]
        h = load_hypothesis()
        for f in ("mgmt_direction", "cautious_initial", "cautious_initial_level",
                  "cautious_secondary", "cautious_secondary_level", "backing_tier"):
            assert f in a, f
        assert a["mgmt_direction"] == "up"
        # frozen ladder == fill-anchored (post-recompute) hypothesis ladder
        assert a["cautious_initial"] == h["cautious_price_initial"]
        assert a["cautious_secondary_level"] == h["cautious_price_secondary_level"]

    def test_market_entry_fill_freezes_all_six_fields(self):
        from tests.test_smt_strategy_v2 import (
            make_5m_bar, make_opp_1m_recent, write_daily, write_hypothesis,
            write_position, NOW as NOW_5M,
        )
        from strategy import run_strategy
        write_hypothesis(direction="up",
                         cautious_price_initial="160", cautious_price_initial_level="day_high",
                         cautious_price_secondary="200", cautious_price_secondary_level="week_high")
        write_position()
        write_daily(day_high=200.0, day_low=100.0)
        bar = make_5m_bar(open_=110.0, high=115.0, low=108.0, close=113.0)
        recent = make_opp_1m_recent("up", open_=105.0, close_=95.0, high=107.0, low=93.0)
        result = run_strategy(NOW_5M, bar, recent)
        assert result["kind"] == "market-entry"
        a = load_position()["active"]
        assert a["mgmt_direction"] == "up"
        assert "cautious_initial" in a and "backing_tier" in a


# ===========================================================================
# Freeze at fill — live_orders paths
# ===========================================================================

class TestFreezeAtFillLiveOrders:
    def test_downgrade_fill_freezes_fields(self, tmp_path, monkeypatch):
        import live_orders
        _hyp(direction="up",
             cautious_price_initial="160", cautious_price_initial_level="day_high",
             cautious_price_secondary="200", cautious_price_secondary_level="week_high")
        _daily([])
        save_position(copy.deepcopy(DEFAULT_POSITION))
        monkeypatch.setattr(live_orders, "_load_pos", smt_state.load_position)
        monkeypatch.setattr(live_orders, "_save_pos", smt_state.save_position)
        monkeypatch.setattr(live_orders, "_log", lambda *a, **k: None)
        live_orders._register_downgraded_fill(
            direction="long", entry_price=100.0, stop_price=95.0,
            source="strategy", now="2026-04-27T10:01:00-04:00", fill_price=100.0)
        a = load_position()["active"]
        assert a["mgmt_direction"] == "up"
        assert a["source"] == "strategy"
        for f in ("cautious_initial", "cautious_secondary_level", "backing_tier"):
            assert f in a, f

    def test_place_market_entry_freezes_fields(self, tmp_path, monkeypatch):
        import live_orders
        _hyp(direction="down",
             cautious_price_initial="60", cautious_price_initial_level="day_low",
             cautious_price_secondary="20", cautious_price_secondary_level="week_low")
        _daily([])
        save_position(copy.deepcopy(DEFAULT_POSITION))
        monkeypatch.setattr(live_orders, "_load_pos", smt_state.load_position)
        monkeypatch.setattr(live_orders, "_save_pos", smt_state.save_position)
        monkeypatch.setattr(live_orders, "_log", lambda *a, **k: None)
        mock_exec = MagicMock()
        mock_exec._entry_is_live = True
        monkeypatch.setattr(live_orders, "_executor", mock_exec)
        live_orders.place_market_entry("short", 19950.0, 19980.0, source="manual")
        a = load_position()["active"]
        assert a["mgmt_direction"] == "down"
        assert a["source"] == "manual"
        assert a["cautious_secondary_level"] == "week_low"
        assert a["backing_tier"] == "week"

    def test_backing_tier_derivation(self):
        for lv, tier in (("week_high", "week"), ("day_high", "day"), ("", "day")):
            a = {}
            smt_state.freeze_active_mgmt(a, "up", {"cautious_price_secondary_level": lv})
            assert a["backing_tier"] == tier, lv


# ===========================================================================
# trend.py manages off the frozen snapshot
# ===========================================================================

class TestTrendManagesOffFrozen:
    def test_trend_manages_when_hypothesis_flipped_opposite(self):
        """Frozen mgmt_direction=up; live hypothesis flipped to down. An UP arm scenario
        still fires using the frozen up-side math, not the down side."""
        from trend import run_trend
        # Live hypothesis is the OPPOSITE direction with a wiped ladder.
        _hyp(direction="down", cautious_price_initial="", cautious_price_secondary="")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="up", cautious="no",
                                       initial="160", initial_lv="day_high")
        save_position(pos)
        bar = _bar(open_=100, high=162, low=98, close=161)
        result = run_trend(NOW, bar, _recent([100, 161], [99, 100]))
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial"
        assert load_position()["active"]["cautious"] == "initial"

    def test_trend_manages_when_hypothesis_none(self):
        """Frozen mgmt_direction=down; live hypothesis none. The none early-return is
        skipped and the DOWN position is managed (secondary break fires on the down side)."""
        from trend import run_trend
        _hyp(direction="none", cautious_price_secondary="")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="down", cautious="secondary",
                                       secondary="90", secondary_lv="day_low",
                                       fill_price=120.0, stop=130.0,
                                       cautious_break_price=110.0)
        save_position(pos)
        # down secondary exit uses bar CLOSE: close=111 > break 110 -> stop-exit
        bar = _bar(open_=108, high=112, low=107, close=111)
        result = run_trend(NOW, bar, _recent([108, 111], [109, 108]))
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-secondary-break"

    def test_trend_break_check_correct_side_after_flip(self):
        """Initial-cautious break uses the frozen direction's comparator even when the
        live hypothesis is flipped. Frozen down trade: break fires on bar_high > break."""
        from trend import run_trend
        _hyp(direction="up")  # live flipped opposite the frozen down trade
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="down", cautious="initial",
                                       initial="60", initial_lv="day_low",
                                       fill_price=100.0, stop=110.0,
                                       cautious_break_price=105.0)
        save_position(pos)
        # down: _broke = bar_high > break(105). high=106 -> break.
        bar = _bar(open_=104, high=106, low=103, close=104)
        result = run_trend(NOW, bar, _recent([104, 104], [105, 104]))
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-initial-break"

    def test_global_trend_reset_skipped_when_active(self):
        """confidence=high + opposing live direction would fire trend-broken when flat;
        with an active frozen position it is SKIPPED and the position keeps managing."""
        from trend import run_trend
        save_global({"all_time_high": 0.0, "confidence": "high", "trend": "up"})
        _hyp(direction="down")  # opposes global trend "up"
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="down", cautious="no",
                                       secondary="", initial="")
        save_position(pos)
        bar = _bar(open_=100, high=105, low=95, close=102)
        result = run_trend(NOW, bar, _recent([100, 102], [99, 100]))
        # trend-broken did NOT fire; the live hypothesis direction is untouched.
        assert result is None or result.get("kind") != "trend-broken"
        assert load_hypothesis()["direction"] == "down"
        assert load_position()["active"] != {}

    def test_ath_secondary_uses_frozen_lv2(self):
        """Frozen secondary level week_high at ATH while the live hypothesis is flipped →
        the ATH-secondary break-even path keys off the frozen lv2 (break-even at fill)."""
        from trend import run_trend
        save_global({"all_time_high": 0.0, "session_ath": 100.0,
                     "confidence": "medium", "trend": "up"})
        _hyp(direction="down", cautious_price_secondary_level="")  # live flipped/wiped
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        # frozen secondary at/above session_ath(100) with level week_high → f_ath_secondary
        pos["active"] = _frozen_active(direction="up", cautious="secondary",
                                       secondary="150", secondary_lv="week_high",
                                       fill_price=120.0, stop=110.0,
                                       cautious_break_price=120.0)
        save_position(pos)
        # up ATH-secondary break-even: _be_broken = bar_low < cbp(120). low=119 -> break.
        bar = _bar(open_=121, high=125, low=119, close=122)
        result = run_trend(NOW, bar, _recent([121, 122], [120, 121]))
        assert result is not None
        assert result["kind"] == "stop-exit"
        assert result["reason"] == "cautious-secondary-break"


# ===========================================================================
# Immutability
# ===========================================================================

class TestFrozenImmutability:
    def test_frozen_ladder_not_overwritten_by_recompute(self):
        import hypothesis as hyp_mod
        h = _hyp(direction="up",
                 cautious_price_initial="160", cautious_price_initial_level="day_high",
                 cautious_price_secondary="200", cautious_price_secondary_level="week_high")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="up", initial="160", initial_lv="day_high",
                                       secondary="200", secondary_lv="week_high")
        save_position(pos)
        frozen_before = dict(load_position()["active"])
        # A later recompute mutates ONLY the hypothesis ladder.
        hyp_mod.recompute_cautious_for_fill(h, 175.0, [], 99999.0, 0)
        save_hypothesis(h)
        a = load_position()["active"]
        assert a["cautious_initial"] == frozen_before["cautious_initial"]
        assert a["cautious_secondary"] == frozen_before["cautious_secondary"]
        assert a["cautious_secondary_level"] == frozen_before["cautious_secondary_level"]

    def test_frozen_ladder_survives_hypothesis_direction_change(self):
        from trend import run_trend
        _hyp(direction="up",
             cautious_price_initial="160", cautious_price_initial_level="day_high")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["active"] = _frozen_active(direction="up", initial="160", initial_lv="day_high")
        save_position(pos)
        # Flip live hypothesis + rewrite the live ladder entirely.
        _hyp(direction="down",
             cautious_price_initial="50", cautious_price_initial_level="day_low",
             cautious_price_secondary="10", cautious_price_secondary_level="week_low")
        a = load_position()["active"]
        assert a["mgmt_direction"] == "up"
        assert a["cautious_initial"] == "160"
        # trend.py still manages the up trade off the frozen ladder.
        bar = _bar(open_=100, high=162, low=98, close=161)
        result = run_trend(NOW, bar, _recent([100, 161], [99, 100]))
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial"


# ===========================================================================
# Pending-stop-entry cancel preserved
# ===========================================================================

class TestPendingCancelPreserved:
    def test_pending_stop_entry_cancel_on_direction_change(self):
        from tests.test_smt_strategy_v2 import (
            make_5m_bar, make_empty_1m_recent, write_hypothesis, write_position,
        )
        from strategy import run_strategy
        write_hypothesis(direction="down")  # flipped vs resting up stop
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["stop_entry"] = 100.0
        pos["stop_direction"] = "up"
        save_position(pos)
        bar = make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())
        assert result is not None
        assert result["kind"] == "cancel-stop-entry"
        assert result["reason"] == "direction-changed"
        assert load_position()["stop_entry"] == ""

    def test_pending_stop_entry_cancel_on_direction_none(self):
        from tests.test_smt_strategy_v2 import (
            make_5m_bar, make_empty_1m_recent, write_hypothesis, write_position,
        )
        from strategy import run_strategy
        write_hypothesis(direction="none")
        pos = copy.deepcopy(DEFAULT_POSITION)
        pos["stop_entry"] = 100.0
        pos["stop_direction"] = "up"
        save_position(pos)
        bar = make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0)
        result = run_strategy(NOW, bar, make_empty_1m_recent())
        assert result is not None
        assert result["kind"] == "cancel-stop-entry"
        assert result["reason"] == "direction-none"
        assert load_position()["stop_entry"] == ""


# ===========================================================================
# No force-close on mismatch (strategy)
# ===========================================================================

class TestNoForceClose:
    def test_automatic_position_preserved_on_mismatch(self):
        from tests.test_smt_strategy_v2 import (
            make_5m_bar, make_empty_1m_recent, write_hypothesis, write_position,
        )
        from strategy import run_strategy
        write_hypothesis(direction="down")
        active = {"time": NOW.isoformat(), "fill_price": 100.0, "direction": "up",
                  "stop": 95.0, "contracts": 2, "cautious": "no"}
        write_position(active=dict(active))
        result = run_strategy(NOW, make_5m_bar(open_=101.0, high=105.0, low=98.0, close=103.0),
                              make_empty_1m_recent())
        assert result is None
        assert load_position()["active"] == active


# ===========================================================================
# Back-compat
# ===========================================================================

class TestBackCompat:
    def test_legacy_active_without_frozen_fields_managed_via_fallback(self):
        """An active dict lacking the frozen fields (only legacy direction) is managed via
        the fallback: mgmt_direction from direction, ladder from live hypothesis."""
        from trend import run_trend
        _hyp(direction="up",
             cautious_price_initial="160", cautious_price_initial_level="day_high")
        _daily([])
        pos = copy.deepcopy(DEFAULT_POSITION)
        # legacy active — NO mgmt_direction / frozen ladder fields
        pos["active"] = {
            "time": "2026-04-27T10:00:00-04:00", "fill_price": 100.0,
            "direction": "up", "stop": 95.0, "contracts": 2, "cautious": "no",
        }
        save_position(pos)
        bar = _bar(open_=100, high=162, low=98, close=161)
        result = run_trend(NOW, bar, _recent([100, 161], [99, 100]))
        assert result is not None
        assert result["kind"] == "new-stop-exit"
        assert result["level"] == "initial"
        assert load_position()["active"]["cautious"] == "initial"
