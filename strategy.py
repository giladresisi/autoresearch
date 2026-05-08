# strategy.py
# Per-5m-bar entry / stop / direction-mismatch logic for SMT v2 pipeline.
# Pure compute: reads hypothesis.json and position.json; updates position.json;
# returns an Optional[Signal] dict. No parquet loading.

from __future__ import annotations

import copy
import json
from datetime import datetime, time as _time
from typing import Optional

import pandas as pd

import smt_state

_DIR_UP            = "up"
_DIR_DOWN          = "down"
_CONF_BAR_MINS     = 5   # default confirmation bar window
_CONF_BAR_MINS_ATH = 15  # confirmation bar window when above session ATH

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_last_bar(
    mnq_1m_recent: pd.DataFrame,
    now: datetime,
    bar_direction: str,
    minutes: int,
    not_before: str,
) -> Optional[dict]:
    """Return the most recently completed `minutes`-bar that moved in bar_direction.

    bar_direction='down' → bearish (close < open); 'up' → bullish (close > open).
    not_before: ISO timestamp — skip if last period started more than `minutes` before this.
    """
    if mnq_1m_recent is None or mnq_1m_recent.empty:
        return None

    delta                = pd.Timedelta(minutes=minutes)
    current_period_start = pd.Timestamp(now).floor(f"{minutes}min")
    last_period_start    = current_period_start - delta

    if not_before:
        if last_period_start < pd.Timestamp(not_before) - delta:
            return None

    _idx = mnq_1m_recent.index
    _sp  = _idx.searchsorted(last_period_start,    side="left")
    _ep  = _idx.searchsorted(current_period_start, side="left")
    window = mnq_1m_recent.iloc[_sp:_ep]
    if window.empty:
        return None

    o = float(window.iloc[0]["Open"])
    c = float(window.iloc[-1]["Close"])
    if bar_direction == _DIR_DOWN and c < o:
        return {
            "time":      last_period_start.isoformat(),
            "high":      float(window["High"].max()),
            "low":       float(window["Low"].min()),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    if bar_direction == _DIR_UP and c > o:
        return {
            "time":      last_period_start.isoformat(),
            "high":      float(window["High"].max()),
            "low":       float(window["Low"].min()),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    return None


def _bar_crosses(bar: dict, price: float) -> bool:
    """Return True if the bar's H/L range spans *price* (inclusive)."""
    return bar["low"] <= price <= bar["high"]


# Minimum fraction of the bar range that must be on the favourable side of the
# close before an entry is accepted.  A long entry on a bar that closed in the
# bottom 40 % of its range (shooting-star shape) is likely a wick-triggered
# false signal; the same logic applies symmetrically for shorts.
_CPR_MIN = 0.40

def _entry_bar_cpr_ok(bar: dict, direction: str) -> bool:
    """Return True if the entry bar's close position meets the quality threshold."""
    rng = bar["high"] - bar["low"]
    if rng < 0.25:
        return True  # near-doji — no meaningful signal either way
    if direction == _DIR_UP:
        return (bar["close"] - bar["low"]) / rng >= _CPR_MIN
    return (bar["high"] - bar["close"]) / rng >= _CPR_MIN


def _make_signal(kind: str, now: datetime, price: float, **kwargs) -> dict:
    """Build a JSON-serialisable signal dict."""
    sig: dict = {
        "kind":  kind,
        "time":  now.isoformat(),
        "price": float(price),
    }
    sig.update(kwargs)
    return sig


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_strategy(
    now: datetime,
    mnq_bar: dict,
    mnq_1m_recent: pd.DataFrame,
    fill_check_only: bool = False,
) -> Optional[dict]:
    """Process a completed 1m bar and return an Optional Signal.

    Args:
        now:           Timestamp of the bar boundary.
        mnq_bar:       Dict with keys time, open, high, low, close, body_high, body_low.
        mnq_1m_recent: Recent 1m bars (for context; not currently used in strategy logic).

    Returns:
        A Signal dict or None.
    """
    hypothesis = smt_state.load_hypothesis()
    position   = smt_state.load_position()

    direction    = hypothesis.get("direction", "none")
    formed_at    = hypothesis.get("formed_at", "")

    # ------------------------------------------------------------------ #
    # Section 2: No active position                                        #
    # ------------------------------------------------------------------ #
    if not position["active"]:
        # Block entries before 9:30 ET — pre-open bars are hypothesis-formation only.
        if now.time() < _time(9, 30):
            return None

        # confidence=high: global conviction active — no automatic entries (limit or market).
        _global = smt_state.load_global()
        if _global.get("confidence") == "high":
            return None

        # Block long entries when price is at or above the session ATH (fixed 09:20 high).
        # Short entries above session ATH are valid — that's precisely the expected direction.
        _session_ath = _global.get("session_ath")
        _above_session_ath = (
            _session_ath is not None and float(mnq_bar["high"]) >= float(_session_ath)
        )
        if direction == _DIR_UP and _above_session_ath:
            return None

        # 2.1 Early-exit conditions — cancel any pending limit if direction changed.
        stop_entry = position["stop_entry"]
        stop_direction = position.get("stop_direction", "")
        if stop_entry != "" and stop_direction != "" and (
            direction == "none" or direction != stop_direction
        ):
            _cancel_price = float(stop_entry)
            position["stop_entry"]      = ""
            position["stop_direction"]  = ""
            position["confirmation_bar"] = {}
            smt_state.save_position(position)
            _reason = "direction-none" if direction == "none" else "direction-changed"
            return _make_signal("cancel-stop-entry", now, _cancel_price, reason=_reason)

        if direction == "none":
            return None
        if position["failed_entries"] > 2:
            return None

        _MARKET_ENTRY_THRESHOLD = 15.0  # pts: switch to market if price is this close
        MIN_STOP_DISTANCE = 5.0
        MAX_CONFIRMATION_BODY_PTS = 25.0  # reject momentum/reversal bars as confirmation

        # 2.4 Fill check runs FIRST so a limit that fills on the same bar as a new
        # confirmation bar is detected rather than overwritten by a move-limit signal.
        # Long fills when bar_high >= limit; short fills when bar_low <= limit.
        stop_entry = position["stop_entry"]
        _entry_f = float(stop_entry) if stop_entry != "" else None
        _entry_reached = _entry_f is not None and (
            (direction == _DIR_UP   and float(mnq_bar["high"]) >= _entry_f) or
            (direction == _DIR_DOWN and float(mnq_bar["low"])  <= _entry_f)
        )
        if stop_entry != "" and _entry_reached:
            fill_price = float(stop_entry)
            conf_bar   = position["confirmation_bar"]
            _STOP_WICK_CAP = 10.0
            if direction == _DIR_UP:
                stop = max(float(conf_bar["low"]), float(conf_bar["body_low"]) - _STOP_WICK_CAP)
            else:
                stop = min(float(conf_bar["high"]), float(conf_bar["body_high"]) + _STOP_WICK_CAP)
            # Fill as soon as price reaches the limit — bar close quality is irrelevant
            # for fill confirmation (the broker fills a limit order the instant price
            # touches it, regardless of where the bar closes).
            if abs(fill_price - float(stop)) >= MIN_STOP_DISTANCE:
                position["active"] = {
                    "time":       mnq_bar["time"],
                    "fill_price": fill_price,
                    "direction":  direction,
                    "stop":       stop,
                    "contracts":  2,
                    "cautious":   "no",
                }
                position["stop_entry"]      = ""
                position["stop_direction"]  = ""
                position["confirmation_bar"] = {}
                smt_state.save_position(position)
                return _make_signal("stop-entry-filled", now, fill_price, direction=direction, stop=stop)

        if fill_check_only:
            return None

        # 2.3 Find the most recent completed opposite bar and set/update limit.
        # Above session ATH, require a 15m confirmation bar (more restrictive).
        # Only emits a signal when the reference bar changes.
        _opp_dir  = _DIR_UP if direction == _DIR_DOWN else _DIR_DOWN
        _bar_mins = _CONF_BAR_MINS_ATH if _above_session_ath else _CONF_BAR_MINS
        opp_5m = _find_last_bar(mnq_1m_recent, now, _opp_dir, _bar_mins, formed_at)
        if opp_5m is not None and (opp_5m["body_high"] - opp_5m["body_low"]) <= MAX_CONFIRMATION_BODY_PTS:
            body_end_price = opp_5m["body_high"] if direction == _DIR_UP else opp_5m["body_low"]
            current_conf_time = position.get("confirmation_bar", {}).get("time", "")
            if opp_5m["time"] != current_conf_time:
                conf_bar_snap = {
                    "time":      opp_5m["time"],
                    "high":      opp_5m["high"],
                    "low":       opp_5m["low"],
                    "body_high": opp_5m["body_high"],
                    "body_low":  opp_5m["body_low"],
                }

                bar_open = float(mnq_bar["open"])
                if direction == _DIR_UP:
                    approach = body_end_price - bar_open
                else:
                    approach = bar_open - body_end_price

                if approach < _MARKET_ENTRY_THRESHOLD:
                    bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                    _STOP_WICK_CAP = 10.0
                    if direction == _DIR_UP:
                        stop = max(float(opp_5m["low"]), float(opp_5m["body_low"]) - _STOP_WICK_CAP)
                    else:
                        stop = min(float(opp_5m["high"]), float(opp_5m["body_high"]) + _STOP_WICK_CAP)
                    # Directional stop check: stop must be on the protective side of entry.
                    # abs() alone passes when the bar moved past the conf bar during the candle.
                    if direction == _DIR_UP   and (bar_mid - float(stop)) < MIN_STOP_DISTANCE:
                        return None
                    if direction == _DIR_DOWN and (float(stop) - bar_mid) < MIN_STOP_DISTANCE:
                        return None
                    if not _entry_bar_cpr_ok(mnq_bar, direction):
                        return None
                    position["active"] = {
                        "time":       mnq_bar["time"],
                        "fill_price": bar_mid,
                        "direction":  direction,
                        "stop":       stop,
                        "contracts":  2,
                        "cautious":   "no",
                    }
                    position["confirmation_bar"] = conf_bar_snap
                    position["stop_entry"]      = ""
                    position["stop_direction"]  = ""
                    smt_state.save_position(position)
                    return _make_signal("market-entry", now, bar_mid, direction=direction, stop=stop)

                position["confirmation_bar"] = conf_bar_snap
                kind = "new-stop-entry" if position["stop_entry"] == "" else "move-stop-entry"
                position["stop_entry"]     = body_end_price
                position["stop_direction"] = direction
                smt_state.save_position(position)
                return _make_signal(kind, now, body_end_price)

        # Nothing triggered
        return None

    # ------------------------------------------------------------------ #
    # Section 3: Active position                                           #
    # ------------------------------------------------------------------ #
    active = position["active"]

    # 3.1 Direction mismatch (includes direction == "none")
    if direction == "none" or direction != active.get("direction"):
        position["active"]            = {}
        position["stop_entry"]       = ""
        position["confirmation_bar"]  = {}
        smt_state.save_position(position)
        _bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
        return _make_signal("market-close", now, _bar_mid, reason="direction-mismatch", close_reason="trend-broken")

    # 3.2 Stop crossed
    stop = active["stop"]
    active_dir = active["direction"]
    stopped = False
    if active_dir == _DIR_UP and mnq_bar["low"] <= stop:
        stopped = True
    elif active_dir == _DIR_DOWN and mnq_bar["high"] >= stop:
        stopped = True

    if stopped:
        exit_price = stop
        position["active"]            = {}
        position["stop_entry"]       = ""
        # confirmation_bar intentionally preserved: prevents immediate re-entry on the
        # same bar before the next 5m hypothesis re-evaluation can run.
        position["failed_entries"]    = position.get("failed_entries", 0) + 1

        # Flag for re-evaluation when the stop crossed the daily or weekly mid —
        # structural signals that the directional thesis has genuinely inverted.
        # Stops on the same side of both mids are noise and skip the re-run.
        _daily = smt_state.load_daily()
        _liq_map = {l["name"]: l["price"] for l in _daily.get("liquidities", [])
                    if l.get("kind") == "level"}
        _dh = _liq_map.get("day_high")
        _dl = _liq_map.get("day_low")
        _daily_mid = (_dh + _dl) / 2.0 if _dh is not None and _dl is not None else None
        _wh = _liq_map.get("week_high")
        _wl = _liq_map.get("week_low")
        _weekly_mid = (_wh + _wl) / 2.0 if _wh is not None and _wl is not None else None
        _stop_crossed_daily = _daily_mid is not None and (
            (active_dir == _DIR_UP   and float(exit_price) < _daily_mid) or
            (active_dir == _DIR_DOWN and float(exit_price) > _daily_mid)
        )
        _stop_crossed_weekly = _weekly_mid is not None and (
            (active_dir == _DIR_UP   and float(exit_price) < _weekly_mid) or
            (active_dir == _DIR_DOWN and float(exit_price) > _weekly_mid)
        )
        if _stop_crossed_daily or _stop_crossed_weekly:
            position["reeval_after_stop"] = True

        smt_state.save_position(position)
        return _make_signal("stopped-out", now, exit_price)

    # 3.3 Above-ATH close: short position still above session ATH exits when a 15m
    # down bar (that stayed above ATH) forms and a 1m up bar breaks above its body_high.
    # Uses bar["low"] for both checks: if price drops below ATH on any bar the rule is off;
    # likewise the reference 15m bar must not have dipped below ATH during its period.
    if active_dir == _DIR_DOWN:
        _global = smt_state.load_global()
        _session_ath = _global.get("session_ath")
        if _session_ath is not None and float(mnq_bar["low"]) >= float(_session_ath):
            _last_15m_down = _find_last_bar(mnq_1m_recent, now, _DIR_DOWN, _CONF_BAR_MINS_ATH, active.get("time", ""))
            if (
                _last_15m_down is not None
                and float(_last_15m_down["low"]) >= float(_session_ath)
                and float(mnq_bar["close"]) > float(mnq_bar["open"])
                and float(mnq_bar["high"]) > float(_last_15m_down["body_high"])
            ):
                position["active"]           = {}
                position["stop_entry"]      = ""
                position["confirmation_bar"] = {}
                smt_state.save_position(position)
                _close_price = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                return _make_signal(
                    "market-close", now, _close_price,
                    reason="above-ath-15m-reversal",
                    close_reason="above-ath-reversal",
                )

    # 3.4 Position active, no event
    return None


# ---------------------------------------------------------------------------
# Position reset helpers — called by daily.py and hypothesis.py so that all
# position.json writes go through the strategy module, not around it.
# ---------------------------------------------------------------------------

def reset_position_for_session() -> None:
    """Clear all active-trade and pending-limit fields at session open.

    Called by daily.run_daily once per session (09:20 ET).
    """
    pos = smt_state.load_position()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    pos["failed_entries"] = 0
    smt_state.save_position(pos)


def reset_position_for_new_hypothesis() -> None:
    """Clear entry-state fields on a none→up/down hypothesis transition.

    Called by hypothesis.run_hypothesis when a directional bias is established.
    Leaves 'active' untouched: a filled trade persists across hypothesis changes.
    """
    pos = smt_state.load_position()
    pos["failed_entries"] = 0
    pos["confirmation_bar"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    smt_state.save_position(pos)
