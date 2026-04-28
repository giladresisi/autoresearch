# hypothesis.py
# Every-5m hypothesis module for SMT v2 pipeline.
# Entry: run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> None
# Reads/writes JSON state via smt_state.py. Emits no caller-routable signals.

import copy
from datetime import datetime, timedelta

import pandas as pd

from smt_state import (
    load_global,
    load_daily,
    load_hypothesis,
    save_hypothesis,
    load_position,
    save_position,
)
from strategy_smt import detect_smt_divergence, detect_smt_fill


def _build_5m_bar(mnq_1m: pd.DataFrame, now: datetime) -> dict | None:
    """Build the current 5m bar using bars from (now - 5min) to now.

    Per spec: "round now down to nearest 5m boundary, then take bars from now-5min to now."
    The bar_start is now rounded down to the nearest 5m boundary (= now - 5min when now
    is exactly on a 5m boundary). We include bars with timestamp in [bar_start, bar_end).

    Returns a dict with Open, High, Low, Close keys, or None if no bars fall in that window.
    """
    ts = pd.Timestamp(now)
    # Round down to nearest 5-minute boundary
    floored_minute = (ts.minute // 5) * 5
    bar_end = ts.replace(minute=floored_minute, second=0, microsecond=0)
    bar_start = bar_end - pd.Timedelta(minutes=5)

    # Filter bars in [bar_start, bar_end)
    window = mnq_1m[(mnq_1m.index >= bar_start) & (mnq_1m.index < bar_end)]
    if window.empty:
        return None

    return {
        "Open":  float(window["Open"].iloc[0]),
        "High":  float(window["High"].max()),
        "Low":   float(window["Low"].min()),
        "Close": float(window["Close"].iloc[-1]),
    }


def _get_liquidity_price(liq: dict) -> float | None:
    """Return the representative price for a liquidity entry."""
    if liq.get("kind") == "level":
        return liq.get("price")
    if liq.get("kind") == "fvg":
        # For FVGs, use the midpoint between top and bottom
        top = liq.get("top")
        bottom = liq.get("bottom")
        if top is not None and bottom is not None:
            return (top + bottom) / 2.0
    return None


def _compute_mid_label(current_close: float, high_price: float, low_price: float) -> str:
    """Classify current_close relative to the midpoint of [low_price, high_price].

    Returns "mid" if within 10 points of the midpoint; "above" or "below" otherwise.
    """
    mid = (high_price + low_price) / 2.0
    diff = current_close - mid
    if abs(diff) <= 10:
        return "mid"
    return "above" if diff > 0 else "below"


def _find_last_liquidity(mnq_1m: pd.DataFrame, liquidities: list) -> str:
    """Find the most recently-touched meaningful liquidity level.

    Restricted to: {week_high, week_low, day_high, day_low}.
    A level is "touched" when bar.High >= price (for highs) or bar.Low <= price (for lows).
    Returns the name of the most recently-touched level (highest bar index), or "" if none.
    """
    meaningful_names = {"week_high", "week_low", "day_high", "day_low"}

    # Build a mapping: name -> price for the meaningful levels
    level_map = {}
    for liq in liquidities:
        if liq.get("name") in meaningful_names and liq.get("kind") == "level":
            level_map[liq["name"]] = liq["price"]

    if not level_map:
        return ""

    # Scan bars backward to find most recently touched level
    # "Highs" (week_high, day_high): touched when bar.High >= price
    # "Lows" (week_low, day_low): touched when bar.Low <= price
    high_names = {"week_high", "day_high"}
    low_names = {"week_low", "day_low"}

    # Track the last (most recent index) touch for each level
    best_idx = -1
    best_name = ""

    bars_array = mnq_1m
    for i in range(len(bars_array) - 1, -1, -1):
        bar = bars_array.iloc[i]
        for name, price in level_map.items():
            touched = False
            if name in high_names and float(bar["High"]) >= price:
                touched = True
            elif name in low_names and float(bar["Low"]) <= price:
                touched = True
            if touched and i > best_idx:
                best_idx = i
                best_name = name
        if best_idx == len(bars_array) - 1:
            # Can't do better — the most recent bar touched something
            break

    return best_name


def _compute_divs(
    mnq_1m: pd.DataFrame,
    mes_1m: pd.DataFrame,
) -> list:
    """Compute SMT divergences by resampling to 15m and 30m bars.

    Calls detect_smt_divergence and detect_smt_fill at each resampled bar.
    Returns a list of dicts: {timeframe, type, side, time}.
    """
    divs = []

    agg = {
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }

    for tf_label, tf_rule in [("15m", "15min"), ("30m", "30min")]:
        # Resample both instruments to the target timeframe
        mnq_rs = mnq_1m.resample(tf_rule, label="left").agg(agg).dropna(subset=["Open"])
        mes_rs = mes_1m.resample(tf_rule, label="left").agg(agg).dropna(subset=["Open"])

        # Align indices: only process bars present in both
        common_idx = mnq_rs.index.intersection(mes_rs.index)
        if len(common_idx) < 2:
            continue

        mnq_aligned = mnq_rs.loc[common_idx].reset_index(drop=False)
        mes_aligned = mes_rs.loc[common_idx].reset_index(drop=False)

        # Rebuild DataFrames with integer index for detect_smt_divergence
        mnq_df = mnq_aligned.set_index(mnq_aligned.columns[0])
        mes_df = mes_aligned.set_index(mes_aligned.columns[0])

        # Only check the most recently completed bar — prevents re-firing old divs
        # on each successive call as the session window grows.
        bar_idx = len(mnq_df) - 1
        if bar_idx < 1:
            continue
        bar_ts = mnq_df.index[bar_idx]

        mnq_pos = mnq_df.iloc[:bar_idx + 1].copy().reset_index(drop=True)
        mes_pos = mes_df.iloc[:bar_idx + 1].copy().reset_index(drop=True)

        result = detect_smt_divergence(
            mes_pos, mnq_pos, bar_idx=bar_idx, session_start_idx=0
        )
        if result is not None:
            direction, _sweep, _miss, smt_type, smt_level = result
            side = "bullish" if direction == "long" else "bearish"
            divs.append({
                "kind":      "smt-div",
                "timeframe": tf_label,
                "type":      smt_type,
                "side":      side,
                "time":      bar_ts.isoformat(),
                "level":     float(smt_level),
            })

        fill_result = detect_smt_fill(mes_pos, mnq_pos, bar_idx=bar_idx)
        if fill_result is not None:
            fill_dir, _fvg_high, _fvg_low = fill_result
            fill_side = "bullish" if fill_dir == "long" else "bearish"
            divs.append({
                "kind":      "smt-div",
                "timeframe": tf_label,
                "type":      "fill",
                "side":      fill_side,
                "time":      bar_ts.isoformat(),
                "level":     None,
            })

    return divs


def _find_nearest_bar(combined: pd.DataFrame, target_ts: pd.Timestamp) -> dict | None:
    """Find the bar nearest to target_ts in combined DataFrame.

    Returns dict with Low and High, or None if DataFrame is empty.
    """
    if combined.empty:
        return None

    # Find nearest index position using searchsorted
    idx_sorted = combined.index.sort_values()
    combined_sorted = combined.loc[idx_sorted]

    pos = idx_sorted.searchsorted(target_ts)
    # Clamp to valid range
    pos = max(0, min(pos, len(idx_sorted) - 1))

    # Check the nearest among pos-1, pos, pos+1
    candidates = []
    for p in [pos - 1, pos]:
        if 0 <= p < len(idx_sorted):
            candidates.append(p)

    if not candidates:
        return None

    # Pick the one with smallest absolute time difference
    best_p = min(candidates, key=lambda p: abs(idx_sorted[p] - target_ts))
    bar = combined_sorted.iloc[best_p]
    return {"Low": float(bar["Low"]), "High": float(bar["High"])}


def run_hypothesis(
    now: datetime,
    mnq_1m: pd.DataFrame,
    mes_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    hist_mes_1m: pd.DataFrame,
) -> list:
    """Run the hypothesis module for the current 5m boundary.

    Reads hypothesis.json; if direction is already set, returns early.
    Otherwise computes all hypothesis fields and writes hypothesis.json.
    Also handles position reset on direction transition from "none".

    Returns a list of smt-div event dicts found this bar (empty list if none).
    """
    # Step 1: Read hypothesis.json; early-exit if direction already set.
    hypothesis = load_hypothesis()
    old_direction = hypothesis["direction"]
    if old_direction != "none":
        return []

    # Step 2: Read global.json ATH; build current 5m bar; check ATH gate.
    global_state = load_global()
    all_time_high = global_state["all_time_high"]

    bar = _build_5m_bar(mnq_1m, now)
    if bar is None:
        return []

    if bar["Low"] > all_time_high and bar["High"] > all_time_high:
        return []  # Both extremes above ATH — no entry opportunity

    current_close = bar["Close"]

    # Step 3: Compute weekly_mid and daily_mid.
    daily = load_daily()
    liquidities = daily.get("liquidities", [])

    week_high_price = None
    week_low_price = None
    day_high_price = None
    day_low_price = None

    for liq in liquidities:
        name = liq.get("name")
        if name == "week_high" and liq.get("kind") == "level":
            week_high_price = liq["price"]
        elif name == "week_low" and liq.get("kind") == "level":
            week_low_price = liq["price"]
        elif name == "day_high" and liq.get("kind") == "level":
            day_high_price = liq["price"]
        elif name == "day_low" and liq.get("kind") == "level":
            day_low_price = liq["price"]

    weekly_mid = ""
    if week_high_price is not None and week_low_price is not None:
        weekly_mid = _compute_mid_label(current_close, week_high_price, week_low_price)

    daily_mid = ""
    if day_high_price is not None and day_low_price is not None:
        daily_mid = _compute_mid_label(current_close, day_high_price, day_low_price)

    # Step 4: last_liquidity — most recently touched meaningful level.
    last_liquidity = _find_last_liquidity(mnq_1m, liquidities)

    # Step 5: divs — SMT divergences at 15m and 30m.
    divs = _compute_divs(mnq_1m, mes_1m)

    # Step 6: direction (TBD hardcoded).
    direction = "up"

    # Step 7: targets — filter liquidities in direction from current close.
    targets = []
    for liq in liquidities:
        kind = liq.get("kind")
        if kind == "level":
            price = liq.get("price")
            if price is None:
                continue
            if direction == "up" and price > current_close:
                targets.append({"name": liq["name"], "price": price})
            elif direction == "down" and price < current_close:
                targets.append({"name": liq["name"], "price": price})
        elif kind == "fvg":
            top = liq.get("top")
            bottom = liq.get("bottom")
            if top is None or bottom is None:
                continue
            if direction == "up" and bottom > current_close:
                targets.append({"name": liq["name"], "price": bottom})
            elif direction == "down" and top < current_close:
                targets.append({"name": liq["name"], "price": top})

    # Step 8: cautious_price (TBD hardcoded).
    cautious_price = ""

    # Step 9: entry_ranges — 12hr ago and 1week ago same time anchors.
    ts_now = pd.Timestamp(now)
    anchor_12hr = ts_now - pd.Timedelta(hours=12)
    anchor_1week = ts_now - pd.Timedelta(weeks=1)

    # Combine historical and current bars for lookup
    combined_mnq = pd.concat([hist_mnq_1m, mnq_1m]).sort_index()
    # Drop duplicates (hist may overlap with current)
    combined_mnq = combined_mnq[~combined_mnq.index.duplicated(keep="last")]

    entry_ranges = []
    bar_12hr = _find_nearest_bar(combined_mnq, anchor_12hr)
    if bar_12hr is not None:
        entry_ranges.append({
            "source": "12hr",
            "low":  bar_12hr["Low"],
            "high": bar_12hr["High"],
        })

    bar_1week = _find_nearest_bar(combined_mnq, anchor_1week)
    if bar_1week is not None:
        entry_ranges.append({
            "source": "1week",
            "low":  bar_1week["Low"],
            "high": bar_1week["High"],
        })

    # Write hypothesis.json
    new_hypothesis = {
        "direction":      direction,
        "weekly_mid":     weekly_mid,
        "daily_mid":      daily_mid,
        "last_liquidity": last_liquidity,
        "divs":           divs,
        "targets":        targets,
        "cautious_price": cautious_price,
        "entry_ranges":   entry_ranges,
    }
    save_hypothesis(new_hypothesis)

    # Step 10: On none -> up/down transition, reset position state.
    if old_direction == "none" and direction != "none":
        position = load_position()
        position["failed_entries"] = 0
        position["confirmation_bar"] = {}
        save_position(position)

    return divs
