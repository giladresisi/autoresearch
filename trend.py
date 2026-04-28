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
    cautious_price_raw = hypothesis.get("cautious_price", "")

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

        # ---- 3a: attempt cautious arming -----------------------------------
        if cautious_state == "no":
            # Only attempt arming if cautious_price is set.
            if cautious_price_raw == "":
                return None  # No cautious_price configured; nothing to do.

            cautious_price = float(cautious_price_raw)

            # Check whether the bar surpassed cautious_price.
            if direction == "up":
                surpassed = bar_high >= cautious_price
            else:  # direction == "down"
                surpassed = bar_low <= cautious_price

            if not surpassed:
                return None  # Bar did not reach the threshold.

            # Check whether close went *beyond* cautious_price.
            if direction == "up":
                close_beyond = bar_close > cautious_price
            else:
                close_beyond = bar_close < cautious_price

            if close_beyond:
                # Arm cautious mode.
                position["active"]["cautious"] = "yes"
                save_position(position)
                return {
                    "kind": "cautious-armed",
                    "time": now.isoformat(),
                    "price": bar_close,
                }
            else:
                # Touched but did not close beyond → reject and exit.
                _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                save_position(position)
                save_hypothesis(hypothesis)
                return _market_close_signal(now, bar_close, reason="cautious-rejected")

        # ---- 3b: cautious already armed ------------------------------------
        if cautious_state == "yes":
            # cautious_price must be set if cautious is "yes"; parse it safely.
            cautious_price = float(cautious_price_raw) if cautious_price_raw != "" else 0.0

            # Reversal check: price crossed back to the pre-cautious side.
            if direction == "up":
                reversal = bar_low <= cautious_price
            else:
                reversal = bar_high >= cautious_price

            if reversal:
                _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                save_position(position)
                save_hypothesis(hypothesis)
                return _market_close_signal(now, bar_close, reason="cautious-reversal")

            # 1m-break check: current bar broke below/above the last opposite 1m bar.
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
                    "kind": "trend-broken",
                    "time": now.isoformat(),
                    "price": bar_close,
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
                    "kind": "trend-broken",
                    "time": now.isoformat(),
                    "price": bar_close,
                }

    return None
