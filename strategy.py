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

def _body_opposite_to(direction: str, bar: dict) -> bool:
    """Return True if the bar's body is in the direction opposite to the hypothesis.

    For an up hypothesis the opposite bar is bearish (close < open).
    For a down hypothesis the opposite bar is bullish (close > open).
    """
    if direction == "up":
        return bar["close"] < bar["open"]
    elif direction == "down":
        return bar["close"] > bar["open"]
    return False


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
    mnq_5m_bar: dict,
    mnq_1m_recent: pd.DataFrame,
) -> Optional[dict]:
    """Process a just-completed 5m bar and return an Optional Signal.

    Args:
        now:            Timestamp of the bar boundary (end of the completing bar).
        mnq_5m_bar:     Dict with keys time, open, high, low, close, body_high, body_low.
        mnq_1m_recent:  Recent 1m bars (for context; not currently used in strategy logic).

    Returns:
        A Signal dict or None.
    """
    hypothesis = smt_state.load_hypothesis()
    position   = smt_state.load_position()

    direction = hypothesis.get("direction", "none")

    # ------------------------------------------------------------------ #
    # Section 2: No active position                                        #
    # ------------------------------------------------------------------ #
    if not position["active"]:
        # 2.1 Early-exit conditions
        if direction == "none":
            return None
        if position["failed_entries"] > 2:
            return None

        # 2.3 PRIORITY: same-bar new-opposite-confirmation overrides fill
        if _body_opposite_to(direction, mnq_5m_bar):
            # Update confirmation bar
            position["confirmation_bar"] = {
                "time":      mnq_5m_bar["time"],
                "high":      mnq_5m_bar["high"],
                "low":       mnq_5m_bar["low"],
                "body_high": mnq_5m_bar["body_high"],
                "body_low":  mnq_5m_bar["body_low"],
            }

            # body_end_price: entry price for the limit order
            # For longs (direction=up): enter at body_high of the bearish bar
            # For shorts (direction=down): enter at body_low of the bullish bar
            if direction == "up":
                body_end_price = mnq_5m_bar["body_high"]
            else:
                body_end_price = mnq_5m_bar["body_low"]

            if position["limit_entry"] == "":
                kind = "new-limit-entry"
            else:
                kind = "move-limit-entry"

            position["limit_entry"] = body_end_price
            smt_state.save_position(position)

            return _make_signal(kind, now, body_end_price)

        # 2.4 No new opposite bar — check fill
        limit_entry = position["limit_entry"]
        if limit_entry != "" and _bar_crosses(mnq_5m_bar, float(limit_entry)):
            fill_price  = float(limit_entry)
            conf_bar    = position["confirmation_bar"]

            # Stop: confirmation_bar.low for long, confirmation_bar.high for short
            if direction == "up":
                stop = conf_bar["low"]
            else:
                stop = conf_bar["high"]

            position["active"] = {
                "time":       mnq_5m_bar["time"],
                "fill_price": fill_price,
                "direction":  direction,
                "stop":       stop,
                "contracts":  2,
                "cautious":   "no",
            }
            position["limit_entry"]      = ""
            position["confirmation_bar"] = {}
            smt_state.save_position(position)

            return _make_signal("limit-entry-filled", now, fill_price, direction=direction)

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
        return _make_signal("market-close", now, mnq_5m_bar["close"], reason="direction-mismatch")

    # 3.2 Stop crossed
    stop = active["stop"]
    active_dir = active["direction"]
    stopped = False
    if active_dir == "up" and mnq_5m_bar["low"] <= stop:
        stopped = True
    elif active_dir == "down" and mnq_5m_bar["high"] >= stop:
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
