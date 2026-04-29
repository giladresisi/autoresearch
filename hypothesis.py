# hypothesis.py
# Every-5m hypothesis module for SMT v2 pipeline.
# Entry: run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> None
# Reads/writes JSON state via smt_state.py. Emits no caller-routable signals.

import copy
from datetime import datetime, timedelta

import pandas as pd

CAUTIOUS_SECONDARY_MAX_DIST = 150  # pts — secondary (1m confirmation) max distance
CAUTIOUS_INITIAL_MAX_DIST   = 100  # pts — initial (5m confirmation) max distance
CAUTIOUS_MIN_DIST           =  40  # pts — below this secondary distance, skip the entry

LIQUIDITY_APPROACH_DIST    = 100   # pts — Rule 2: "nearly approaching" radius
NEAR_EXTREME_DIST          =  75   # pts — Rule 3a: proximity boost to daily extreme
MOMENTUM_BARS              =   5   # 1m bars — Rule 2: recent momentum window
BOS_SWING_N                =   2   # bars each side for swing high/low detection
BOS_LOOKBACK_1HR           =   8   # 1hr bars — recency window for BOS/CHoCH
BOS_LOOKBACK_4HR           =   3   # 4hr bars — recency window for BOS/CHoCH
DIRECTION_SCORE_THRESHOLD  =  0.35 # combined Rule 3+4 score required to commit

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
        bar_close = float(mnq_df.iloc[bar_idx]["Close"])

        if result is not None:
            direction, _sweep, _miss, smt_type, smt_level = result
            side = "bullish" if direction == "long" else "bearish"

            # Compute both MNQ and MES session extremes involved in the divergence,
            # independent of which instrument leads. smt_level alone is ambiguous
            # (it's MNQ for normal, MES for symmetric).
            _sess = slice(0, bar_idx)
            if "body" in smt_type:
                _mnq_hi = float(mnq_pos["Close"].iloc[_sess].max())
                _mnq_lo = float(mnq_pos["Close"].iloc[_sess].min())
                _mes_hi = float(mes_pos["Close"].iloc[_sess].max())
                _mes_lo = float(mes_pos["Close"].iloc[_sess].min())
            else:
                _mnq_hi = float(mnq_pos["High"].iloc[_sess].max())
                _mnq_lo = float(mnq_pos["Low"].iloc[_sess].min())
                _mes_hi = float(mes_pos["High"].iloc[_sess].max())
                _mes_lo = float(mes_pos["Low"].iloc[_sess].min())
            if direction == "short":  # bearish — a session high was swept
                mnq_div_price = _mnq_hi
                mes_div_price = _mes_hi
            else:                     # bullish — a session low was swept
                mnq_div_price = _mnq_lo
                mes_div_price = _mes_lo

            divs.append({
                "kind":          "smt-div",
                "timeframe":     tf_label,
                "type":          smt_type,
                "side":          side,
                "time":          bar_ts.isoformat(),
                "price":         bar_close,
                "mnq_div_price": mnq_div_price,
                "mes_div_price": mes_div_price,
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
                "price":     bar_close,
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


# ---------------------------------------------------------------------------
# Direction-determination helpers (Rules 1-5)
# ---------------------------------------------------------------------------

def _named_price(liquidities: list, name: str) -> float | None:
    for liq in liquidities:
        if liq.get("name") == name and liq.get("kind") == "level":
            return float(liq["price"])
    return None


def _detect_fvg_1hr(hist_mnq_1m: pd.DataFrame, session_mnq_1m: pd.DataFrame) -> list:
    combined = pd.concat([hist_mnq_1m, session_mnq_1m])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    bars = combined.resample("1h").agg(agg).dropna(subset=["Open"])
    bars = bars.iloc[-(BOS_LOOKBACK_1HR + 5):]
    n = len(bars)
    fvgs = []
    for i in range(1, n - 1):
        hi_prev = float(bars["High"].iloc[i - 1])
        lo_prev = float(bars["Low"].iloc[i - 1])
        hi_next = float(bars["High"].iloc[i + 1])
        lo_next = float(bars["Low"].iloc[i + 1])
        if lo_next > hi_prev:
            fvgs.append({"kind": "bullish", "bottom": hi_prev, "top": lo_next,
                         "bar_time": bars.index[i], "bar_pos": i})
        elif hi_next < lo_prev:
            fvgs.append({"kind": "bearish", "bottom": hi_next, "top": lo_prev,
                         "bar_time": bars.index[i], "bar_pos": i})
    min_pos = n - 1 - 5
    return [f for f in fvgs if f["bar_pos"] >= min_pos]


def _build_meaningful_levels(liquidities: list, fvg_1hr: list) -> list:
    levels = []
    for liq in liquidities:
        name = liq.get("name")
        if liq.get("kind") != "level":
            continue
        if name == "week_high":
            levels.append({"price": float(liq["price"]), "kind": "high", "name": "week_high", "priority": 1})
        elif name == "week_low":
            levels.append({"price": float(liq["price"]), "kind": "low",  "name": "week_low",  "priority": 1})
        elif name == "day_high":
            levels.append({"price": float(liq["price"]), "kind": "high", "name": "day_high",  "priority": 2})
        elif name == "day_low":
            levels.append({"price": float(liq["price"]), "kind": "low",  "name": "day_low",   "priority": 2})
    for fvg in fvg_1hr:
        if fvg["kind"] == "bullish":
            levels.append({"price": fvg["bottom"], "kind": "low",  "name": "fvg_1hr_bull_bottom", "priority": 3})
        else:
            levels.append({"price": fvg["top"],    "kind": "high", "name": "fvg_1hr_bear_top",    "priority": 3})
    return sorted(levels, key=lambda lv: lv["priority"])


def _was_previously_touched(level: dict, prior_bars: pd.DataFrame) -> bool:
    if prior_bars.empty:
        return False
    if level["kind"] == "high":
        return bool((prior_bars["High"] >= level["price"]).any())
    return bool((prior_bars["Low"] <= level["price"]).any())


def _check_fresh_touch(current_bar: dict, prior_bars: pd.DataFrame, levels: list) -> dict | None:
    for level in levels:
        if level["kind"] == "high":
            touched_now = float(current_bar["High"]) >= level["price"]
        else:
            touched_now = float(current_bar["Low"]) <= level["price"]
        if not touched_now:
            continue
        if _was_previously_touched(level, prior_bars):
            continue
        direction = "down" if level["kind"] == "high" else "up"
        return {"direction": direction, "touched_level": level, "base_conf": 1.0}
    return None


def _co_evaluate_with_smt(direction: str, base_conf: float, smt_score: float) -> tuple:
    smt_sign = 1 if direction == "up" else -1
    aligned = (smt_sign * smt_score) > 0
    if aligned and abs(smt_score) >= 0.30:
        smt_alignment = "confirmed"
    elif not aligned and abs(smt_score) >= 0.60:
        smt_alignment = "contradicted"
    else:
        smt_alignment = "neutral"
    return base_conf, smt_alignment


def _check_approaching(
    current_bar: dict,
    prior_bars: pd.DataFrame,
    levels: list,
    mnq_1m: pd.DataFrame,
) -> dict | None:
    current_close = float(current_bar["Close"])
    for level in levels:
        if _was_previously_touched(level, prior_bars):
            continue
        dist = abs(level["price"] - current_close)
        if dist > LIQUIDITY_APPROACH_DIST:
            continue
        if level["kind"] == "high":
            approaching = current_close < level["price"]
        else:
            approaching = current_close > level["price"]
        if not approaching:
            continue
        recent = mnq_1m["Close"].iloc[-MOMENTUM_BARS:]
        if len(recent) < 2:
            continue
        if level["kind"] == "high":
            momentum_ok = float(recent.iloc[-1]) > float(recent.iloc[0])
        else:
            momentum_ok = float(recent.iloc[-1]) < float(recent.iloc[0])
        if not momentum_ok:
            continue
        direction = "up" if level["kind"] == "high" else "down"
        return {"direction": direction, "approaching_level": level, "dist": dist, "conf": 0.75}
    return None


def _compute_pd_score(
    close: float,
    week_h: float | None,
    week_l: float | None,
    day_h: float | None,
    day_l: float | None,
) -> float:
    if week_h is None or week_l is None or day_h is None or day_l is None:
        return 0.0
    week_mid = (week_h + week_l) / 2.0
    day_mid  = (day_h  + day_l)  / 2.0
    weekly_premium = close > week_mid
    daily_premium  = close > day_mid
    if weekly_premium and daily_premium:
        pd_score = -0.70
    elif not weekly_premium and not daily_premium:
        pd_score = +0.70
    elif weekly_premium and not daily_premium:
        pd_score = +0.30
    else:
        pd_score = -0.30
    if (day_h - close) < NEAR_EXTREME_DIST:
        pd_score -= 0.15
    if (close - day_l) < NEAR_EXTREME_DIST:
        pd_score += 0.15
    return max(-1.0, min(1.0, pd_score))


def _compute_bos_choch_score(bars_df: pd.DataFrame, swing_n: int, lookback: int) -> float:
    n = len(bars_df)
    if n < 2 * swing_n + 1:
        return 0.0
    highs  = bars_df["High"].values
    lows   = bars_df["Low"].values
    closes = bars_df["Close"].values
    shs = [i for i in range(swing_n, n - swing_n)
           if highs[i] == highs[i - swing_n: i + swing_n + 1].max()]
    sls = [i for i in range(swing_n, n - swing_n)
           if lows[i]  == lows[i  - swing_n: i + swing_n + 1].min()]
    last_idx = n - 1
    current_close = float(closes[-1])
    score = 0.0
    recent_shs = [i for i in shs if last_idx - i <= lookback]
    if recent_shs:
        latest_sh = max(recent_shs)
        if current_close > float(highs[latest_sh]):
            recency = 1.0 - (last_idx - latest_sh) / lookback
            score += recency
    recent_sls = [i for i in sls if last_idx - i <= lookback]
    if recent_sls:
        latest_sl = max(recent_sls)
        if current_close < float(lows[latest_sl]):
            recency = 1.0 - (last_idx - latest_sl) / lookback
            score -= recency
    return max(-1.0, min(1.0, score))


def _closest_level_name(price: float | None, liquidities: list) -> str | None:
    if price is None:
        return None
    best_name = None
    best_dist = float("inf")
    for liq in liquidities:
        if liq.get("kind") == "level":
            p = liq.get("price")
        elif liq.get("kind") == "fvg":
            top = liq.get("top")
            bottom = liq.get("bottom")
            if top is None or bottom is None:
                continue
            p = (top + bottom) / 2.0
        else:
            continue
        if p is None:
            continue
        d = abs(p - price)
        if d < best_dist:
            best_dist = d
            best_name = liq.get("name")
    return best_name if best_dist <= 10 else None


def _compute_smt_score(divs: list, liquidities: list) -> float:
    LEVEL_WEIGHT = {
        "week_high": 3, "week_low": 3,
        "day_high":  2, "day_low":  2,
        "ny_morning_high": 1, "ny_morning_low": 1,
        "london_high": 1,     "london_low": 1,
    }
    TF_WEIGHT   = {"30m": 2.0, "15m": 1.0}
    TYPE_WEIGHT = {"wick": 2.0, "wick_sym": 2.0, "body": 1.5, "body_sym": 1.5, "fill": 1.0}
    score = 0.0
    max_possible = 0.0
    for div in divs:
        level_name = _closest_level_name(div.get("mnq_div_price"), liquidities)
        lw = LEVEL_WEIGHT.get(level_name, 1) if level_name else 1
        tw = TF_WEIGHT.get(div.get("timeframe", ""), 1.0)
        yw = TYPE_WEIGHT.get(div.get("type", ""), 1.0)
        w  = lw * tw * yw
        sign = 1 if div.get("side") == "bullish" else -1
        score        += sign * w
        max_possible += w
    if max_possible == 0:
        return 0.0
    return max(-1.0, min(1.0, score / max_possible))


def _determine_direction(
    current_bar: dict,
    mnq_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    liquidities: list,
    global_state: dict,
    divs: list,
) -> tuple:
    fvg_1hr = _detect_fvg_1hr(hist_mnq_1m, mnq_1m)
    levels  = _build_meaningful_levels(liquidities, fvg_1hr)
    prior   = mnq_1m.iloc[:-1]
    smt_sc  = _compute_smt_score(divs, liquidities)

    week_high = _named_price(liquidities, "week_high")
    week_low  = _named_price(liquidities, "week_low")
    day_high  = _named_price(liquidities, "day_high")
    day_low   = _named_price(liquidities, "day_low")
    current_close = float(current_bar["Close"])

    def _zone(close, hi, lo):
        if hi is None or lo is None:
            return "unknown"
        return "premium" if close > (hi + lo) / 2.0 else "discount"

    reason = {
        "rule":              None,
        "weekly_zone":       _zone(current_close, week_high, week_low),
        "daily_zone":        _zone(current_close, day_high,  day_low),
        "smt_score":         round(smt_sc, 3),
        "pd_score":          None,
        "bos_score_1hr":     None,
        "bos_score_4hr":     None,
        "rule3_score":       None,
        "combined_score":    None,
        "fresh_touch_level": None,
        "smt_alignment":     None,
        "approaching_level": None,
        "approaching_dist":  None,
        "last_swept_level":  None,
    }

    # Rule 1: fresh sweep of a meaningful level — decisive state-change event.
    r1 = _check_fresh_touch(current_bar, prior, levels)
    if r1:
        _conf, smt_aln = _co_evaluate_with_smt(r1["direction"], r1["base_conf"], smt_sc)
        reason["rule"]              = "rule1"
        reason["fresh_touch_level"] = r1["touched_level"]["name"]
        reason["smt_alignment"]     = smt_aln
        return r1["direction"], reason

    # Rule 2: trending toward an unvisited level with momentum — decisive continuation.
    r2 = _check_approaching(current_bar, prior, levels, mnq_1m)
    if r2:
        reason["rule"]              = "rule2"
        reason["approaching_level"] = r2["approaching_level"]["name"]
        reason["approaching_dist"]  = round(r2["dist"], 1)
        return r2["direction"], reason

    # Rule 2b: last high-priority sweep + daily-mid position (failed reversal / confirmation).
    # A high-priority level (day/week extreme) being the most recent touch creates a
    # directional expectation that is confirmed or cancelled by position vs daily_mid:
    #   last swept = low → above mid ⇒ up (bounce confirmed)  | below mid ⇒ down (bounce failed)
    #   last swept = high → below mid ⇒ down (drop confirmed) | above mid ⇒ up (drop failed)
    _low_names  = {"day_low", "week_low"}
    _high_names = {"day_high", "week_high"}
    _last_liq   = _find_last_liquidity(mnq_1m, liquidities)
    if _last_liq and day_high is not None and day_low is not None:
        _daily_mid  = (day_high + day_low) / 2.0
        _above_mid  = current_close > _daily_mid
        if _last_liq in _low_names:
            r2b_dir = "up" if _above_mid else "down"
        elif _last_liq in _high_names:
            r2b_dir = "down" if not _above_mid else "up"
        else:
            r2b_dir = None
        if r2b_dir is not None:
            reason["rule"]            = "rule2b"
            reason["last_swept_level"] = _last_liq
            return r2b_dir, reason

    # Rules 3+4: multi-TF premium/discount bias + BOS/CHoCH + SMT scoring layer.
    pd_sc = _compute_pd_score(current_close, week_high, week_low, day_high, day_low)
    combined_bars = pd.concat([hist_mnq_1m, mnq_1m])
    combined_bars = combined_bars[~combined_bars.index.duplicated(keep="last")].sort_index()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    mnq_1hr = combined_bars.resample("1h").agg(agg).dropna(subset=["Open"])
    mnq_4hr = combined_bars.resample("4h").agg(agg).dropna(subset=["Open"])
    b1hr = _compute_bos_choch_score(mnq_1hr, BOS_SWING_N, BOS_LOOKBACK_1HR)
    b4hr = _compute_bos_choch_score(mnq_4hr, BOS_SWING_N, BOS_LOOKBACK_4HR)
    bos_sc   = 0.35 * b1hr + 0.65 * b4hr
    r3_sc    = 0.55 * pd_sc + 0.45 * bos_sc
    combined = 0.65 * r3_sc + 0.35 * smt_sc

    reason["pd_score"]       = round(pd_sc,    3)
    reason["bos_score_1hr"]  = round(b1hr,     3)
    reason["bos_score_4hr"]  = round(b4hr,     3)
    reason["rule3_score"]    = round(r3_sc,    3)
    reason["combined_score"] = round(combined, 3)

    if abs(combined) >= DIRECTION_SCORE_THRESHOLD:
        reason["rule"] = "rule3_4"
        return ("up" if combined > 0 else "down"), reason

    # Rule 5: global trend fallback when no rule commits.
    reason["rule"] = "rule5_trend"
    return global_state.get("trend", "up"), reason


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

    # Step 6: direction — determined by ICT rules (see direction.md).
    direction, direction_reason = _determine_direction(
        current_bar  = bar,
        mnq_1m       = mnq_1m,
        hist_mnq_1m  = hist_mnq_1m,
        liquidities  = liquidities,
        global_state = global_state,
        divs         = divs,
    )

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

    # Step 8: two-tier cautious prices.
    # Secondary (1m confirmation): furthest in-direction level within CAUTIOUS_SECONDARY_MAX_DIST.
    # Initial  (5m confirmation): furthest in-direction level within CAUTIOUS_INITIAL_MAX_DIST
    #   that is closer to current price than the secondary (intermediate target).
    _cautious_all = []  # list of (price, name) within secondary range
    for liq in liquidities:
        liq_kind = liq.get("kind")
        if liq_kind == "level":
            p = liq.get("price")
        elif liq_kind == "fvg":
            p = liq.get("bottom") if direction == "up" else liq.get("top")
        else:
            continue
        if p is None:
            continue
        if direction == "up" and current_close < p <= current_close + CAUTIOUS_SECONDARY_MAX_DIST:
            _cautious_all.append((p, liq.get("name", "")))
        elif direction == "down" and current_close - CAUTIOUS_SECONDARY_MAX_DIST <= p < current_close:
            _cautious_all.append((p, liq.get("name", "")))
    ath = global_state["all_time_high"]
    if direction == "up" and current_close < ath <= current_close + CAUTIOUS_SECONDARY_MAX_DIST:
        _cautious_all.append((ath, "ATH"))

    if _cautious_all:
        _sec = max(_cautious_all, key=lambda x: x[0]) if direction == "up" \
               else min(_cautious_all, key=lambda x: x[0])
        cautious_price_secondary       = _sec[0]
        cautious_price_secondary_level = _sec[1]

        # Initial: furthest within CAUTIOUS_INITIAL_MAX_DIST that is strictly closer than secondary
        if direction == "up":
            _init_candidates = [(p, n) for p, n in _cautious_all
                                if p < cautious_price_secondary and p <= current_close + CAUTIOUS_INITIAL_MAX_DIST]
        else:
            _init_candidates = [(p, n) for p, n in _cautious_all
                                if p > cautious_price_secondary and p >= current_close - CAUTIOUS_INITIAL_MAX_DIST]

        if _init_candidates:
            _ini = max(_init_candidates, key=lambda x: x[0]) if direction == "up" \
                   else min(_init_candidates, key=lambda x: x[0])
            if abs(_ini[0] - current_close) >= CAUTIOUS_MIN_DIST:
                cautious_price_initial       = _ini[0]
                cautious_price_initial_level = _ini[1]
            else:
                cautious_price_initial       = ""
                cautious_price_initial_level = ""
        else:
            cautious_price_initial       = ""
            cautious_price_initial_level = ""
    else:
        cautious_price_secondary       = ""
        cautious_price_secondary_level = ""
        cautious_price_initial         = ""
        cautious_price_initial_level   = ""

    # Step 8b: veto direction when entry conditions are unfavourable.
    # (1) Secondary cautious price is too close — not enough room to run.
    # (2) Up direction but we are already at or above the recorded ATH — price in uncharted territory.
    if direction != "none":
        sec_dist = (abs(float(cautious_price_secondary) - current_close)
                    if cautious_price_secondary != "" else 0)
        if cautious_price_secondary != "" and sec_dist < CAUTIOUS_MIN_DIST:
            direction = "none"
        elif direction == "up" and current_close >= ath:
            direction = "none"

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
        "direction":                     direction,
        "weekly_mid":                    weekly_mid,
        "daily_mid":                     daily_mid,
        "last_liquidity":                last_liquidity,
        "divs":                          divs,
        "targets":                       targets,
        "cautious_price_initial":        cautious_price_initial,
        "cautious_price_initial_level":  cautious_price_initial_level,
        "cautious_price_secondary":      cautious_price_secondary,
        "cautious_price_secondary_level": cautious_price_secondary_level,
        "entry_ranges":                  entry_ranges,
    }
    save_hypothesis(new_hypothesis)

    # Step 10: On none -> up/down transition, reset position state.
    if old_direction == "none" and direction != "none":
        position = load_position()
        position["failed_entries"] = 0
        position["confirmation_bar"] = {}
        save_position(position)

    hyp_event = {
        "kind":          "new-hypothesis",
        "time":          pd.Timestamp(now).isoformat(),
        "price":         current_close,
        "direction":     direction,
        "weekly_mid":    weekly_mid,
        "daily_mid":     daily_mid,
        "last_liquidity": last_liquidity,
        "targets":       targets,
        "cautious_price_initial":        cautious_price_initial,
        "cautious_price_initial_level":  cautious_price_initial_level,
        "cautious_price_secondary":      cautious_price_secondary,
        "cautious_price_secondary_level": cautious_price_secondary_level,
        "entry_ranges":                  entry_ranges,
        "direction_reason":              direction_reason,
    }
    if direction == "none":
        return divs
    return [hyp_event] + divs
