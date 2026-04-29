# strategy.py
# Per-5m-bar entry / stop / direction-mismatch logic for SMT v2 pipeline.
# Pure compute: reads hypothesis.json and position.json; updates position.json;
# returns an Optional[Signal] dict. No parquet loading.

from __future__ import annotations

import copy
import json
from datetime import datetime
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

    five_m = mnq_1m_recent.resample("5min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna(subset=["Open", "Close"])

    if five_m.empty:
        return None

    # Exclude the currently-forming 5m period.
    current_5m_start = pd.Timestamp(now).floor("5min")
    completed = five_m[five_m.index < current_5m_start]

    # Only consider 5m bars that completed at or after the hypothesis was formed.
    # A bar at index T completes at T + 5min, so we keep bars where T >= formed_at - 5min.
    if hypothesis_formed_at:
        formed_ts = pd.Timestamp(hypothesis_formed_at)
        cutoff = formed_ts - pd.Timedelta(minutes=5)
        completed = completed[completed.index >= cutoff]

    if completed.empty:
        return None

    # Only check the single most-recently-completed bar.
    row = completed.iloc[-1]
    o, c = float(row["Open"]), float(row["Close"])
    if direction == "up" and c < o:
        return {
            "time":      completed.index[-1].isoformat(),
            "high":      float(row["High"]),
            "low":       float(row["Low"]),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    if direction == "down" and c > o:
        return {
            "time":      completed.index[-1].isoformat(),
            "high":      float(row["High"]),
            "low":       float(row["Low"]),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    return None


def _bar_crosses(bar: dict, price: float) -> bool:
    """Return True if the bar's H/L range spans *price* (inclusive)."""
    return bar["low"] <= price <= bar["high"]


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
        # 2.1 Early-exit conditions
        if direction == "none":
            return None
        if position["failed_entries"] > 2:
            return None

        # 2.3 Find the most recent completed opposite 5m bar and set/update limit.
        # Only emits a signal when the reference bar changes; otherwise falls
        # through to 2.4 to check fill on the existing limit.
        _MARKET_ENTRY_THRESHOLD = 5.0  # pts: switch to market if price is this close
        MIN_STOP_DISTANCE = 5.0

        opp_5m = _find_last_opposite_5m_bar(mnq_1m_recent, now, direction, formed_at)
        if opp_5m is not None:
            body_end_price = opp_5m["body_high"] if direction == "up" else opp_5m["body_low"]
            current_conf_time = position.get("confirmation_bar", {}).get("time", "")
            if opp_5m["time"] != current_conf_time or position["limit_entry"] == "":
                conf_bar_snap = {
                    "time":      opp_5m["time"],
                    "high":      opp_5m["high"],
                    "low":       opp_5m["low"],
                    "body_high": opp_5m["body_high"],
                    "body_low":  opp_5m["body_low"],
                }

                # Approach distance: how far (in pts) price must still travel to reach entry.
                # Negative means the bar's open already blew past the entry price.
                bar_open = float(mnq_bar["open"])
                if direction == "up":
                    approach = body_end_price - bar_open
                else:
                    approach = bar_open - body_end_price

                if approach < _MARKET_ENTRY_THRESHOLD:
                    # Price is at or within threshold of the entry — fill immediately at bar mid.
                    bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                    stop = opp_5m["low"] if direction == "up" else opp_5m["high"]
                    if abs(bar_mid - float(stop)) < MIN_STOP_DISTANCE:
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

        # 2.4 No updated 5m reference bar — check fill on existing limit.
        # Long fills when bar_high >= limit (bar reached or gapped above it).
        # Short fills when bar_low  <= limit (bar reached or gapped below it).
        limit_entry = position["limit_entry"]
        _limit_f = float(limit_entry) if limit_entry != "" else None
        _limit_reached = _limit_f is not None and (
            (direction == "up"   and float(mnq_bar["high"]) >= _limit_f) or
            (direction == "down" and float(mnq_bar["low"])  <= _limit_f)
        )
        if limit_entry != "" and _limit_reached:
            fill_price  = float(limit_entry)
            conf_bar    = position["confirmation_bar"]

            # Stop: confirmation_bar.low for long, confirmation_bar.high for short
            if direction == "up":
                stop = conf_bar["low"]
            else:
                stop = conf_bar["high"]

            if abs(fill_price - float(stop)) < MIN_STOP_DISTANCE:
                return None

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
        position["failed_entries"]    = position.get("failed_entries", 0) + 1
        smt_state.save_position(position)
        return _make_signal("stopped-out", now, exit_price)

    # 3.3 Position active, no event
    return None
