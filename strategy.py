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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_last_opposite_5m_bar(
    mnq_1m_recent: pd.DataFrame,
    now: datetime,
    direction: str,
    hypothesis_formed_at: str,
) -> Optional[dict]:
    """Return the most recently completed 5m bar that is opposite to direction,
    but only if it completed at or after the hypothesis was formed.

    direction='up'   → look for bearish 5m bar (close < open)
    direction='down' → look for bullish 5m bar (close > open)
    """
    if mnq_1m_recent is None or mnq_1m_recent.empty:
        return None

    current_5m_start = pd.Timestamp(now).floor("5min")
    last_5m_start = current_5m_start - pd.Timedelta(minutes=5)

    # Apply hypothesis timing constraint before doing any data work.
    if hypothesis_formed_at:
        formed_ts = pd.Timestamp(hypothesis_formed_at)
        cutoff = formed_ts - pd.Timedelta(minutes=5)
        if last_5m_start < cutoff:
            return None

    # Only the 1m bars of the last completed 5m period are needed.
    _idx = mnq_1m_recent.index
    _sp = _idx.searchsorted(last_5m_start,    side="left")
    _ep = _idx.searchsorted(current_5m_start, side="left")
    window = mnq_1m_recent.iloc[_sp:_ep]
    if window.empty:
        return None

    o = float(window.iloc[0]["Open"])
    c = float(window.iloc[-1]["Close"])
    if direction == "up" and c < o:
        return {
            "time":      last_5m_start.isoformat(),
            "high":      float(window["High"].max()),
            "low":       float(window["Low"].min()),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    if direction == "down" and c > o:
        return {
            "time":      last_5m_start.isoformat(),
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
    if direction == "up":
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

        # 2.1 Early-exit conditions
        if direction == "none":
            return None
        if position["failed_entries"] > 2:
            return None

        _MARKET_ENTRY_THRESHOLD = 5.0  # pts: switch to market if price is this close
        MIN_STOP_DISTANCE = 5.0
        MAX_CONFIRMATION_BODY_PTS = 25.0  # reject momentum/reversal bars as confirmation

        # 2.4 Fill check runs FIRST so a limit that fills on the same bar as a new
        # confirmation bar is detected rather than overwritten by a move-limit signal.
        # Long fills when bar_high >= limit; short fills when bar_low <= limit.
        limit_entry = position["limit_entry"]
        _limit_f = float(limit_entry) if limit_entry != "" else None
        _limit_reached = _limit_f is not None and (
            (direction == "up"   and float(mnq_bar["high"]) >= _limit_f) or
            (direction == "down" and float(mnq_bar["low"])  <= _limit_f)
        )
        if limit_entry != "" and _limit_reached:
            fill_price = float(limit_entry)
            conf_bar   = position["confirmation_bar"]
            if direction == "up":
                stop = conf_bar["body_low"]
            else:
                stop = conf_bar["body_high"]
            # Only fill if stop distance and bar quality are acceptable.
            # If not, fall through to section 2.3: a new confirmation bar may offer
            # a better entry price and stop, so the limit should be moved rather than
            # filled at the stale price.
            if abs(fill_price - float(stop)) >= MIN_STOP_DISTANCE and _entry_bar_cpr_ok(mnq_bar, direction):
                position["active"] = {
                    "time":       mnq_bar["time"],
                    "fill_price": fill_price,
                    "direction":  direction,
                    "stop":       stop,
                    "contracts":  2,
                    "cautious":   "no",
                }
                position["limit_entry"]      = ""
                position["confirmation_bar"] = {}
                smt_state.save_position(position)
                return _make_signal("limit-entry-filled", now, fill_price, direction=direction, stop=stop)

        if fill_check_only:
            return None

        # 2.3 Find the most recent completed opposite 5m bar and set/update limit.
        # Only emits a signal when the reference bar changes.
        opp_5m = _find_last_opposite_5m_bar(mnq_1m_recent, now, direction, formed_at)
        if opp_5m is not None and (opp_5m["body_high"] - opp_5m["body_low"]) <= MAX_CONFIRMATION_BODY_PTS:
            body_end_price = opp_5m["body_high"] if direction == "up" else opp_5m["body_low"]
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
                if direction == "up":
                    approach = body_end_price - bar_open
                else:
                    approach = bar_open - body_end_price

                if approach < _MARKET_ENTRY_THRESHOLD:
                    bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                    stop = opp_5m["body_low"] if direction == "up" else opp_5m["body_high"]
                    if abs(bar_mid - float(stop)) < MIN_STOP_DISTANCE:
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
                    position["limit_entry"]      = ""
                    smt_state.save_position(position)
                    return _make_signal("market-entry", now, bar_mid, direction=direction, stop=stop)

                position["confirmation_bar"] = conf_bar_snap
                kind = "new-limit-entry" if position["limit_entry"] == "" else "move-limit-entry"
                position["limit_entry"] = body_end_price
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
        position["limit_entry"]       = ""
        position["confirmation_bar"]  = {}
        smt_state.save_position(position)
        _bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
        return _make_signal("market-close", now, _bar_mid, reason="direction-mismatch", close_reason="trend-broken")

    # 3.2 Stop crossed
    stop = active["stop"]
    active_dir = active["direction"]
    stopped = False
    if active_dir == "up" and mnq_bar["low"] <= stop:
        stopped = True
    elif active_dir == "down" and mnq_bar["high"] >= stop:
        stopped = True

    if stopped:
        exit_price = stop
        position["active"]            = {}
        position["limit_entry"]       = ""
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
            (active_dir == "up"   and float(exit_price) < _daily_mid) or
            (active_dir == "down" and float(exit_price) > _daily_mid)
        )
        _stop_crossed_weekly = _weekly_mid is not None and (
            (active_dir == "up"   and float(exit_price) < _weekly_mid) or
            (active_dir == "down" and float(exit_price) > _weekly_mid)
        )
        if _stop_crossed_daily or _stop_crossed_weekly:
            position["reeval_after_stop"] = True

        smt_state.save_position(position)
        return _make_signal("stopped-out", now, exit_price)

    # 3.3 Position active, no event
    return None
