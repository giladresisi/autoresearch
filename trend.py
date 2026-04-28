# trend.py
# Per-1m-bar cautious-mode management and trend-invalidation.
# Entry: run_trend(now, mnq_1m_bar, mnq_1m_recent) -> Optional[Signal]
# Reads hypothesis.json, position.json, daily.json.
# Writes hypothesis.json and position.json on state changes.

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from smt_state import (
    load_daily,
    load_hypothesis,
    load_position,
    save_hypothesis,
    save_position,
)

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
Signal = dict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _market_close_signal(now: datetime, price: float, reason: str) -> Signal:
    return {"kind": "market-close", "time": now.isoformat(), "price": price, "reason": reason}


def _clear_position_and_hypothesis(
    position: dict, hypothesis: dict, *, clear_active: bool
) -> None:
    """Mutate position and hypothesis dicts in place — common cleanup for every market-close path."""
    if clear_active:
        position["active"] = {}
    position["limit_entry"] = ""
    position["confirmation_bar"] = {}
    hypothesis["direction"] = "none"


def _last_opposite_bar(
    mnq_1m_recent: pd.DataFrame,
    current_bar_time: str,
    direction: str,
) -> Optional[pd.Series]:
    """Return the most recent bar in mnq_1m_recent (excluding current bar) whose body is
    opposite to direction.  Returns None if no such bar exists.

    direction="up"   → look for bearish bars (close < open)
    direction="down" → look for bullish bars (close > open)
    """
    # Parse current bar timestamp for comparison; also accept direct Timestamp.
    try:
        current_ts = pd.Timestamp(current_bar_time)
    except Exception:
        current_ts = None

    # Iterate in reverse order (most recent first), skipping the current bar.
    for i in range(len(mnq_1m_recent) - 1, -1, -1):
        row = mnq_1m_recent.iloc[i]
        # Exclude the current bar by timestamp equality.
        if current_ts is not None:
            row_ts = mnq_1m_recent.index[i]
            # Make both tz-aware or both tz-naive for comparison.
            if current_ts.tzinfo is not None and row_ts.tzinfo is None:
                row_ts = row_ts.tz_localize(current_ts.tzinfo)
            elif current_ts.tzinfo is None and row_ts.tzinfo is not None:
                current_ts_naive = current_ts.tz_localize(None)
                if row_ts == current_ts_naive:
                    continue
            if row_ts == current_ts:
                continue

        if direction == "up" and row["Close"] < row["Open"]:
            return row
        if direction == "down" and row["Close"] > row["Open"]:
            return row

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_trend(
    now: datetime,
    mnq_1m_bar: dict,
    mnq_1m_recent: pd.DataFrame,
) -> Optional[Signal]:
    """Evaluate cautious-mode arming/rejection/exit and trend invalidation for one 1m bar.

    Parameters
    ----------
    now:
        Wall-clock / bar time for the current bar (used in signal timestamps).
    mnq_1m_bar:
        Dict with keys: "time", "open", "high", "low", "close".
    mnq_1m_recent:
        DataFrame of recent 1m bars (DatetimeIndex, columns Open/High/Low/Close),
        typically the last N bars including the current bar.

    Returns
    -------
    A Signal dict or None.
    """
    # ------------------------------------------------------------------
    # Step 1: load state.
    # ------------------------------------------------------------------
    hypothesis = load_hypothesis()
    position = load_position()
    daily = load_daily()

    direction = hypothesis.get("direction", "none")
    cautious_initial_raw   = hypothesis.get("cautious_price_initial",   "")
    cautious_secondary_raw = hypothesis.get("cautious_price_secondary", "")

    # ------------------------------------------------------------------
    # Step 2: early exit when no active direction.
    # ------------------------------------------------------------------
    if direction == "none":
        return None

    bar_high = float(mnq_1m_bar["high"])
    bar_low = float(mnq_1m_bar["low"])
    bar_close = float(mnq_1m_bar["close"])
    bar_time_str = str(mnq_1m_bar.get("time", now.isoformat()))

    active = position.get("active", {})

    # ------------------------------------------------------------------
    # Step 3: position is open.
    # ------------------------------------------------------------------
    if active:
        cautious_state = active.get("cautious", "no")

        cautious_initial   = float(cautious_initial_raw)   if cautious_initial_raw   != "" else None
        cautious_secondary = float(cautious_secondary_raw) if cautious_secondary_raw != "" else None

        def _surpassed(price: float) -> bool:
            return (bar_high >= price) if direction == "up" else (bar_low <= price)

        def _close_beyond(price: float) -> bool:
            return (bar_close > price) if direction == "up" else (bar_close < price)

        def _reversal(price: float) -> bool:
            return (bar_low <= price) if direction == "up" else (bar_high >= price)

        # ---- 3a: unarmed — check if a cautious level was reached -----------
        if cautious_state == "no":
            if cautious_secondary is None and cautious_initial is None:
                return None

            # Secondary takes priority if surpassed (it's farther, confirms strong move).
            if cautious_secondary is not None and _surpassed(cautious_secondary):
                if _close_beyond(cautious_secondary):
                    position["active"]["cautious"] = "secondary"
                    save_position(position)
                    return {"kind": "cautious-armed", "time": now.isoformat(),
                            "price": bar_close, "level": "secondary"}
                else:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_close, reason="cautious-rejected")

            if cautious_initial is not None and _surpassed(cautious_initial):
                if _close_beyond(cautious_initial):
                    position["active"]["cautious"] = "initial"
                    save_position(position)
                    return {"kind": "cautious-armed", "time": now.isoformat(),
                            "price": bar_close, "level": "initial"}
                else:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_close, reason="cautious-rejected")

            return None

        # ---- 3b: initial cautious — wait for 5m bar opposite body ----------
        if cautious_state == "initial":
            armed_price = cautious_initial if cautious_initial is not None else 0.0

            if _reversal(armed_price):
                _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                save_position(position)
                save_hypothesis(hypothesis)
                return _market_close_signal(now, bar_close, reason="cautious-reversal")

            # Upgrade to secondary if secondary level is now reached.
            if cautious_secondary is not None and _surpassed(cautious_secondary):
                if _close_beyond(cautious_secondary):
                    position["active"]["cautious"] = "secondary"
                    save_position(position)
                    return {"kind": "cautious-armed", "time": now.isoformat(),
                            "price": bar_close, "level": "secondary"}
                else:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_close, reason="cautious-rejected")

            # 5m confirmation: on a 5m boundary, check if the completed 5m bar body
            # is opposite to direction → exit.
            ts = pd.Timestamp(now)
            if ts.minute % 5 == 0:
                five_start = ts - pd.Timedelta(minutes=5)
                five_bars = mnq_1m_recent[mnq_1m_recent.index >= five_start]
                if not five_bars.empty:
                    five_open  = float(five_bars["Open"].iloc[0])
                    five_close = float(five_bars["Close"].iloc[-1])
                    opposite_body = (five_close < five_open) if direction == "up" \
                                    else (five_close > five_open)
                    if opposite_body:
                        _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                        save_position(position)
                        save_hypothesis(hypothesis)
                        return _market_close_signal(now, bar_close, reason="cautious-5m-break")

            return None

        # ---- 3c: secondary cautious — 1m confirmation ----------------------
        if cautious_state in ("secondary", "yes"):
            armed_price = cautious_secondary if cautious_secondary is not None else 0.0

            if _reversal(armed_price):
                _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                save_position(position)
                save_hypothesis(hypothesis)
                return _market_close_signal(now, bar_close, reason="cautious-reversal")

            last_opp = _last_opposite_bar(mnq_1m_recent, bar_time_str, direction)
            if last_opp is not None:
                if direction == "up":
                    broke = bar_low <= float(last_opp["Low"])
                else:
                    broke = bar_high >= float(last_opp["High"])

                if broke:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_close, reason="cautious-1m-break")

        return None

    # ------------------------------------------------------------------
    # Step 4: no open position — scan for opposite-direction liquidity break.
    # ------------------------------------------------------------------
    liquidities = daily.get("liquidities", [])

    for level in liquidities:
        if level.get("kind") != "level":
            continue

        level_price = float(level["price"])

        if direction == "up":
            # Opposite-direction levels are those *below* current price.
            if level_price < bar_close and bar_low <= level_price:
                # A downside support was breached → trend broken.
                hypothesis["direction"] = "none"
                position["confirmation_bar"] = {}
                position["limit_entry"] = ""
                save_position(position)
                save_hypothesis(hypothesis)
                return {
                    "kind":            "trend-broken",
                    "time":            now.isoformat(),
                    "price":           bar_close,
                    "broken_direction": direction,
                    "level_name":      level.get("name", ""),
                    "level_price":     level_price,
                    "bar_low":         bar_low,
                }
        else:  # direction == "down"
            # Opposite-direction levels are those *above* current price.
            if level_price > bar_close and bar_high >= level_price:
                hypothesis["direction"] = "none"
                position["confirmation_bar"] = {}
                position["limit_entry"] = ""
                save_position(position)
                save_hypothesis(hypothesis)
                return {
                    "kind":            "trend-broken",
                    "time":            now.isoformat(),
                    "price":           bar_close,
                    "broken_direction": direction,
                    "level_name":      level.get("name", ""),
                    "level_price":     level_price,
                    "bar_high":        bar_high,
                }

    return None
