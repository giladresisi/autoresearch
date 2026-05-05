# hypothesis.py
# Every-5m hypothesis module for SMT v2 pipeline.
# Entry: run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> None
# Reads/writes JSON state via smt_state.py. Emits no caller-routable signals.

import copy
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

CAUTIOUS_SECONDARY_MAX_DIST = 150  # pts — secondary (1m confirmation) max distance
CAUTIOUS_INITIAL_MAX_DIST   = 110  # pts — initial (5m confirmation) max distance
CAUTIOUS_MIN_DIST           =  40  # pts — below this secondary distance, skip the entry

LIQUIDITY_APPROACH_DIST    = 100   # pts — Rule 2: "nearly approaching" radius
NEAR_EXTREME_DIST          =  75   # pts — Rule 3a: proximity boost to daily extreme
MOMENTUM_BARS              =   5   # 1m bars — Rule 2: recent momentum window
BOS_SWING_N                =   2   # bars each side for swing high/low detection
BOS_LOOKBACK_1HR           =   8   # 1hr bars — recency window for BOS/CHoCH
BOS_LOOKBACK_4HR           =   3   # 4hr bars — recency window for BOS/CHoCH
DIRECTION_SCORE_THRESHOLD  =  0.35 # combined Rule 3+4 score required to commit
RULE2B_ANCHOR_MAX_AGE_HOURS  =  1.0 # hrs — combined with RULE2B_STALE_RECOVERY_PTS
RULE2B_STALE_RECOVERY_PTS   = 200.0 # pts — anchor is stale only when BOTH age > max_age AND
                                     #       price has recovered this far from the sweep level

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


def _find_last_liquidity(
    mnq_1m: pd.DataFrame,
    liquidities: list,
    extra_bars: pd.DataFrame | None = None,
) -> tuple[str, "pd.Timestamp | None"]:
    """Find the most recently-crossed meaningful liquidity level.

    Scans mnq_1m (session bars) and optionally extra_bars (True Day pre-session bars)
    so overnight/London-session level crosses are captured, not just post-09:20 bars.

    Restricted to: {week_high, week_low, day_high, day_low}.
    A level is "crossed" when the previous bar closed on the near side and the current
    bar's extreme reaches the far side (prev_close < price AND bar.High >= price for
    highs; prev_close > price AND bar.Low <= price for lows).
    Returns (name, cross_timestamp) of the most recently-crossed level, or ("", None).
    """
    meaningful_names = {"week_high", "week_low", "day_high", "day_low", "TDO", "TWO"}

    level_map = {}
    for liq in liquidities:
        if liq.get("name") in meaningful_names and liq.get("kind") == "level":
            level_map[liq["name"]] = liq["price"]

    if not level_map:
        return "", None

    if extra_bars is not None and not extra_bars.empty:
        bars_array = pd.concat([extra_bars, mnq_1m])
        bars_array = bars_array[~bars_array.index.duplicated(keep="last")].sort_index()
    else:
        bars_array = mnq_1m

    high_names = {"week_high", "day_high"}
    best_idx   = -1
    best_name  = ""

    closes = bars_array["Close"].values
    highs  = bars_array["High"].values
    lows   = bars_array["Low"].values

    for name, price in level_map.items():
        if name in high_names:
            # upward cross: prev close below level, current bar reaches above
            crossed = (closes[:-1] < price) & (highs[1:] >= price)
        else:
            # downward cross: prev close above level, current bar reaches below
            crossed = (closes[:-1] > price) & (lows[1:] <= price)
        idxs = np.where(crossed)[0] + 1
        if len(idxs) > 0:
            last_cross = int(idxs[-1])
            if last_cross > best_idx:
                best_idx = last_cross
                best_name = name

    if best_idx < 0:
        return "", None
    return best_name, bars_array.index[best_idx]


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
    """Find the bar nearest to target_ts in combined DataFrame (must be sorted)."""
    if combined.empty:
        return None
    idx = combined.index
    pos = idx.searchsorted(target_ts)
    pos = max(0, min(pos, len(idx) - 1))
    if pos > 0 and abs(idx[pos - 1] - target_ts) <= abs(idx[pos] - target_ts):
        pos -= 1
    bar = combined.iloc[pos]
    return {"Low": float(bar["Low"]), "High": float(bar["High"])}


# ---------------------------------------------------------------------------
# Direction-determination helpers (Rules 1-5)
# ---------------------------------------------------------------------------

def _named_price(liquidities: list, name: str) -> float | None:
    for liq in liquidities:
        if liq.get("name") == name and liq.get("kind") == "level":
            return float(liq["price"])
    return None


def _detect_fvg_1hr(
    hist_mnq_1m: pd.DataFrame,
    session_mnq_1m: pd.DataFrame,
    *,
    hist_1hr: "pd.DataFrame | None" = None,
) -> list:
    _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if hist_1hr is not None and not hist_1hr.empty:
        if not session_mnq_1m.empty:
            sess_1hr = session_mnq_1m.resample("1h").agg(_agg).dropna(subset=["Open"])
            bars = pd.concat([hist_1hr, sess_1hr])
            bars = bars[~bars.index.duplicated(keep="last")].sort_index()
        else:
            bars = hist_1hr
    else:
        combined = pd.concat([hist_mnq_1m, session_mnq_1m])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        bars = combined.resample("1h").agg(_agg).dropna(subset=["Open"])
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
    now: "datetime",
    *,
    hist_1hr: "pd.DataFrame | None" = None,
    hist_4hr: "pd.DataFrame | None" = None,
) -> tuple:
    fvg_1hr = _detect_fvg_1hr(hist_mnq_1m, mnq_1m, hist_1hr=hist_1hr)
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

    # Pre-session True Day bars (18:00 prior calendar day to session open).
    # Used by Rule 2b so overnight/London level touches are visible at NY open.
    _pre_session: pd.DataFrame | None = None
    if not mnq_1m.empty and not hist_mnq_1m.empty:
        _ts = mnq_1m.index[0]
        if _ts.tzinfo is None:
            _ts = _ts.tz_localize("America/New_York")
        else:
            _ts = _ts.tz_convert("America/New_York")
        _prior = _ts.date() - timedelta(days=1)
        _true_day_start = pd.Timestamp(
            datetime(_prior.year, _prior.month, _prior.day, 18, 0, 0),
            tz="America/New_York",
        )
        _pre_session = hist_mnq_1m[
            (hist_mnq_1m.index >= _true_day_start) & (hist_mnq_1m.index < _ts)
        ]

    # Rule 1: fresh sweep of a meaningful level — decisive state-change event.
    r1 = _check_fresh_touch(current_bar, prior, levels)
    if r1:
        _conf, smt_aln = _co_evaluate_with_smt(r1["direction"], r1["base_conf"], smt_sc)
        reason["rule"]              = "rule1"
        reason["fresh_touch_level"] = r1["touched_level"]["name"]
        reason["smt_alignment"]     = smt_aln
        return r1["direction"], reason

    # Rule 2b: last high-priority sweep + daily-mid position.
    # Scans the full True Day (pre-session + session) so overnight/London level touches are
    # visible at NY open.
    #
    # Same-side cases (last=low+below_mid, last=high+above_mid) use a two-layer check:
    #   Layer 1 (post-sweep): did price cross the mid after the sweep, then fail back?
    #     A failed attempt is the strongest signal — override everything else.
    #   Layer 2 (pre-sweep fallback): was there a committed directional mid-cross before
    #     the sweep, with no opposite-level revisit?  That marks a continuation sweep.
    #     If neither layer fires, treat as a liquidity grab → expect reversal.
    #
    #   last=low  + above mid                                                  => up  (bounce confirmed)
    #   last=low  + below mid + upward cross AFTER sweep (failed bullish)      => down
    #   last=low  + below mid + downward cross BEFORE sweep + high not hit     => down (continuation)
    #   last=low  + below mid + else                                           => up  (low grab → bounce)
    #   last=high + below mid                                                  => down (drop confirmed)
    #   last=high + above mid + downward cross AFTER sweep (failed bearish)    => up
    #   last=high + above mid + upward cross BEFORE sweep + low not hit        => up  (continuation)
    #   last=high + above mid + else                                           => down (high grab → drop)
    _low_names  = {"day_low", "week_low", "TDO", "TWO"}
    _high_names = {"day_high", "week_high"}
    _last_liq, _last_liq_ts = _find_last_liquidity(mnq_1m, liquidities, extra_bars=_pre_session)
    if _last_liq and day_high is not None and day_low is not None:
        _daily_mid = (day_high + day_low) / 2.0
        _above_mid = current_close > _daily_mid
        _liq_price_map = {l["name"]: float(l["price"]) for l in liquidities if l.get("kind") == "level"}

        # Gate: stale anchor — sweep is old AND price has structurally recovered from it.
        # Requiring both conditions prevents blocking trending days where price is simply
        # pulling back toward the sweep level (small recovery).
        # Pre-session touches use session open as the age reference so they're always fresh
        # at the session start regardless of when overnight they happened.
        _anchor_age_ok = True
        if _last_liq_ts is not None:
            _now_tz = pd.Timestamp(now)
            if _now_tz.tzinfo is None:
                _now_tz = _now_tz.tz_localize("America/New_York")
            _session_open = mnq_1m.index[0] if not mnq_1m.empty else _last_liq_ts
            _ref_ts = max(_last_liq_ts, _session_open)
            _age_h = (_now_tz - _ref_ts).total_seconds() / 3600.0
            _sweep_price = _liq_price_map.get(_last_liq) if _liq_price_map else None
            if _sweep_price is not None:
                _recovery = abs(current_close - _sweep_price)
                _anchor_age_ok = not (_age_h > RULE2B_ANCHOR_MAX_AGE_HOURS and _recovery > RULE2B_STALE_RECOVERY_PTS)
            else:
                _anchor_age_ok = _age_h <= RULE2B_ANCHOR_MAX_AGE_HOURS

        if _pre_session is not None and not _pre_session.empty:
            _true_day_bars = pd.concat([_pre_session, mnq_1m])
            _true_day_bars = _true_day_bars[~_true_day_bars.index.duplicated(keep="last")].sort_index()
        else:
            _true_day_bars = mnq_1m

        def _last_mid_cross_after(after_ts: "pd.Timestamp | None", upward: bool) -> "pd.Timestamp | None":
            _bars = _true_day_bars[_true_day_bars.index > after_ts] if after_ts is not None else _true_day_bars
            result = None
            for i in range(len(_bars)):
                if upward     and float(_bars["High"].iloc[i]) > _daily_mid:
                    result = _bars.index[i]
                if not upward and float(_bars["Low"].iloc[i])  < _daily_mid:
                    result = _bars.index[i]
            return result

        def _first_mid_cross_before(before_ts: "pd.Timestamp | None", upward: bool) -> "pd.Timestamp | None":
            _bars = _true_day_bars[_true_day_bars.index <= before_ts] if before_ts is not None else _true_day_bars
            for i in range(1, len(_bars)):
                c_now  = float(_bars["Close"].iloc[i])
                c_prev = float(_bars["Close"].iloc[i - 1])
                if upward     and c_now > _daily_mid and c_prev <= _daily_mid:
                    return _bars.index[i]
                if not upward and c_now < _daily_mid and c_prev >= _daily_mid:
                    return _bars.index[i]
            return None

        def _opp_level_touched(from_ts: "pd.Timestamp | None", to_ts: "pd.Timestamp | None",
                               names: set, check_high: bool) -> bool:
            _w = _true_day_bars
            if from_ts is not None:
                _w = _w[_w.index > from_ts]
            if to_ts is not None:
                _w = _w[_w.index <= to_ts]
            if _w.empty:
                return False
            for _n in names:
                _lp = _liq_price_map.get(_n)
                if _lp is None:
                    continue
                if check_high and (_w["High"] >= _lp).any():
                    return True
                if not check_high and (_w["Low"] <= _lp).any():
                    return True
            return False

        r2b_dir = None
        if _anchor_age_ok:
            if _last_liq in _low_names:
                if _above_mid:
                    r2b_dir = "up"
                else:
                    # Layer 1: post-sweep upward cross that subsequently failed → bearish
                    _last_up_ts = _last_mid_cross_after(_last_liq_ts, upward=True)
                    if _last_up_ts is not None:
                        r2b_dir = "down"
                    else:
                        # Layer 2: pre-sweep committed bearish cross (continuation sweep)
                        _pre_cross_ts = _first_mid_cross_before(_last_liq_ts, upward=False)
                        if _pre_cross_ts is not None and not _opp_level_touched(
                                _pre_cross_ts, _last_liq_ts, _high_names, check_high=True):
                            r2b_dir = "down"
                        else:
                            r2b_dir = "up"  # liquidity grab → expect bounce
            elif _last_liq in _high_names:
                if not _above_mid:
                    r2b_dir = "down"
                else:
                    # Layer 1: post-sweep downward cross that subsequently failed → bullish
                    _last_down_ts = _last_mid_cross_after(_last_liq_ts, upward=False)
                    if _last_down_ts is not None:
                        r2b_dir = "up"
                    else:
                        # Layer 2: pre-sweep committed bullish cross (continuation sweep)
                        _pre_cross_ts = _first_mid_cross_before(_last_liq_ts, upward=True)
                        if _pre_cross_ts is not None and not _opp_level_touched(
                                _pre_cross_ts, _last_liq_ts, _low_names, check_high=False):
                            r2b_dir = "up"
                        else:
                            r2b_dir = "down"  # liquidity grab → expect drop
        if r2b_dir is not None:
            reason["rule"]             = "rule2b"
            reason["last_swept_level"] = _last_liq
            return r2b_dir, reason

    # Rule 2: trending toward an unvisited level with momentum — decisive continuation.
    r2 = _check_approaching(current_bar, prior, levels, mnq_1m)
    if r2:
        reason["rule"]              = "rule2"
        reason["approaching_level"] = r2["approaching_level"]["name"]
        reason["approaching_dist"]  = round(r2["dist"], 1)
        return r2["direction"], reason

    # Rules 3+4: multi-TF premium/discount bias + BOS/CHoCH + SMT scoring layer.
    pd_sc = _compute_pd_score(current_close, week_high, week_low, day_high, day_low)
    _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if hist_1hr is not None and hist_4hr is not None:
        _sess_1hr = mnq_1m.resample("1h").agg(_agg).dropna(subset=["Open"])
        mnq_1hr = pd.concat([hist_1hr, _sess_1hr])
        mnq_1hr = mnq_1hr[~mnq_1hr.index.duplicated(keep="last")].sort_index()
        _sess_4hr = mnq_1m.resample("4h").agg(_agg).dropna(subset=["Open"])
        mnq_4hr = pd.concat([hist_4hr, _sess_4hr])
        mnq_4hr = mnq_4hr[~mnq_4hr.index.duplicated(keep="last")].sort_index()
    else:
        combined_bars = pd.concat([hist_mnq_1m, mnq_1m])
        combined_bars = combined_bars[~combined_bars.index.duplicated(keep="last")].sort_index()
        mnq_1hr = combined_bars.resample("1h").agg(_agg).dropna(subset=["Open"])
        mnq_4hr = combined_bars.resample("4h").agg(_agg).dropna(subset=["Open"])
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


def compute_live_hl_mid(
    combined_1m: pd.DataFrame,
    now: "pd.Timestamp",
) -> dict:
    """Compute live day / week high, low, mid from bar data.

    Returns a dict containing day_high, day_low, day_mid, week_high, week_low, week_mid.
    Only keys where sufficient bar data exists are included.

    Parameters
    ----------
    combined_1m : deduplicated, sorted 1m bars covering the current futures day and week
    now         : current tz-aware ET timestamp
    """
    today  = now.date()
    result: dict = {}

    # Day: prior calendar day 18:00 ET → now
    _prior_cal = today - timedelta(days=1)
    _day_start = pd.Timestamp(
        datetime(_prior_cal.year, _prior_cal.month, _prior_cal.day, 18, 0, 0),
        tz="America/New_York",
    )
    _day_bars = combined_1m[combined_1m.index >= _day_start]
    if not _day_bars.empty:
        dh = float(_day_bars["High"].max())
        dl = float(_day_bars["Low"].min())
        result["day_high"] = dh
        result["day_low"]  = dl
        result["day_mid"]  = (dh + dl) / 2.0

    # Week: Sunday 18:00 ET (futures week open)
    _today_wd  = now.isocalendar().weekday          # 1=Mon … 7=Sun
    _monday    = today - timedelta(days=_today_wd - 1)
    _sunday    = today if _today_wd == 7 else _monday - timedelta(days=1)
    _week_start = pd.Timestamp(
        datetime(_sunday.year, _sunday.month, _sunday.day, 18, 0, 0),
        tz="America/New_York",
    )
    _week_bars = combined_1m[combined_1m.index >= _week_start]
    if not _week_bars.empty:
        wh = float(_week_bars["High"].max())
        wl = float(_week_bars["Low"].min())
        result["week_high"] = wh
        result["week_low"]  = wl
        result["week_mid"]  = (wh + wl) / 2.0

    return result


def run_hypothesis(
    now: datetime,
    mnq_1m: pd.DataFrame,
    mes_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    hist_mes_1m: pd.DataFrame,
    *,
    hist_1hr: "pd.DataFrame | None" = None,
    hist_4hr: "pd.DataFrame | None" = None,
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

    # Resolve _ts_now early — needed for live bar recomputation below.
    _ts_now = pd.Timestamp(now)
    if _ts_now.tzinfo is None:
        _ts_now = _ts_now.tz_localize("America/New_York")
    else:
        _ts_now = _ts_now.tz_convert("America/New_York")

    # Step 3: Compute weekly_mid and daily_mid.
    daily = load_daily()
    liquidities = daily.get("liquidities", [])

    # Refresh day/week high, low, mid from live bars.
    # daily.json is frozen at 09:20; the NY-session open sets new extremes that
    # must be reflected in the mid calculation used for hypothesis direction.
    _combined_live = pd.concat([hist_mnq_1m, mnq_1m])
    _combined_live = _combined_live[~_combined_live.index.duplicated(keep="last")].sort_index()
    _live_hl = compute_live_hl_mid(_combined_live, _ts_now)
    for _liq in liquidities:
        if _liq.get("kind") == "level" and _liq["name"] in _live_hl:
            _liq["price"] = _live_hl[_liq["name"]]

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

    # Step 4: last_liquidity — most recently touched meaningful level (True Day scope).
    _prior_cal = _ts_now.date() - timedelta(days=1)
    _true_day_start_hyp = pd.Timestamp(
        datetime(_prior_cal.year, _prior_cal.month, _prior_cal.day, 18, 0, 0),
        tz="America/New_York",
    )
    _hyp_pre_session = hist_mnq_1m[
        (hist_mnq_1m.index >= _true_day_start_hyp)
        & (hist_mnq_1m.index < (mnq_1m.index[0] if not mnq_1m.empty else _ts_now))
    ]
    last_liquidity, _ = _find_last_liquidity(mnq_1m, liquidities, extra_bars=_hyp_pre_session)

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
        now          = now,
        hist_1hr     = hist_1hr,
        hist_4hr     = hist_4hr,
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
    # (3) No targets exist in this direction — nothing to trade toward.
    if direction != "none":
        sec_dist = (abs(float(cautious_price_secondary) - current_close)
                    if cautious_price_secondary != "" else 0)
        if cautious_price_secondary != "" and sec_dist < CAUTIOUS_MIN_DIST:
            direction = "none"
        elif direction == "up" and current_close >= ath:
            direction = "none"
        elif not targets:
            direction = "none"

    # Step 9: entry_ranges — 12hr ago and 1week ago same time anchors.
    # Both anchors fall within hist_mnq_1m (session runs 09:20-17:00 ET; even
    # at 17:00, 12h prior = 05:00 ET which is pre-session).
    ts_now = pd.Timestamp(now)
    anchor_12hr = ts_now - pd.Timedelta(hours=12)
    anchor_1week = ts_now - pd.Timedelta(weeks=1)

    entry_ranges = []
    bar_12hr = _find_nearest_bar(hist_mnq_1m, anchor_12hr)
    if bar_12hr is not None:
        entry_ranges.append({
            "source": "12hr",
            "low":  bar_12hr["Low"],
            "high": bar_12hr["High"],
        })

    bar_1week = _find_nearest_bar(hist_mnq_1m, anchor_1week)
    if bar_1week is not None:
        entry_ranges.append({
            "source": "1week",
            "low":  bar_1week["Low"],
            "high": bar_1week["High"],
        })

    # Write hypothesis.json
    if direction != "none" and direction != old_direction:
        formed_at = pd.Timestamp(now).isoformat()
    elif direction != "none":
        formed_at = hypothesis.get("formed_at", pd.Timestamp(now).isoformat())
    else:
        formed_at = ""

    new_hypothesis = {
        "direction":                     direction,
        "formed_at":                     formed_at,
        "weekly_mid":                    weekly_mid,
        "daily_mid":                     daily_mid,
        "last_liquidity":                last_liquidity,
        "divs":                          divs,
        "targets":                       targets,
        "cautious_price":                "",
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
        position["limit_entry"] = ""
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
