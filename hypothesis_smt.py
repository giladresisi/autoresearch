# hypothesis_smt.py
# Session hypothesis system: deterministic ICT/SMT rule engine + Claude API narrative layer.
# ANTHROPIC_API_KEY env var is read automatically by the Anthropic SDK (standard default).
# compute_hypothesis_direction() is safe to call from backtest (no side effects, no API calls).
# HypothesisManager is realtime-only (makes up to 3 Claude API calls per session).
import datetime
import json
from datetime import time
from pathlib import Path
from typing import Optional

import pandas as pd

from strategy_smt import compute_tdo  # only this function — no other strategy imports

# ── Constants ─────────────────────────────────────────────────────────────────
SESSION_DATA_DIR       = Path("data/sessions")
CONTRADICTION_THRESHOLD = 3
EVIDENCE_MOVE_PTS       = 15.0
EVIDENCE_TOUCH_PTS      = 5.0
EXTREME_CONTRADICTION_PTS = 20.0
FVG_LOOKBACK_BARS       = 20

_ET = "America/New_York"


# ── Internal helpers ──────────────────────────────────────────────────────────

# Per-DataFrame caches keyed by id(df). Safe because DataFrames live for the full backtest.
_df_dates_cache: dict = {}
_df_times_cache: dict = {}
_df_two_cache: dict = {}   # id(df) -> {(iso_year, iso_week): tuesday_open}


def _index_dates(df: pd.DataFrame):
    """Return date array for df's index. Cached per DataFrame object."""
    k = id(df)
    if k not in _df_dates_cache:
        idx = df.index
        _df_dates_cache[k] = idx.date if (hasattr(idx, "tz") and idx.tz is not None) else idx.normalize().date
    return _df_dates_cache[k]


def _index_times(df: pd.DataFrame):
    """Return time array for df's index. Cached per DataFrame object."""
    k = id(df)
    if k not in _df_times_cache:
        _df_times_cache[k] = df.index.time
    return _df_times_cache[k]


def _get_two_map(df: pd.DataFrame) -> dict:
    """Return {(iso_year, iso_week): first_tuesday_open} dict. Cached per DataFrame object."""
    k = id(df)
    if k not in _df_two_cache:
        dates = _index_dates(df)
        opens = df["Open"].values
        two_map: dict = {}
        for i, d in enumerate(dates):
            if d.weekday() == 1:   # Tuesday only — cheap filter before isocalendar
                yw = d.isocalendar()[:2]
                if yw not in two_map:
                    two_map[yw] = float(opens[i])
        _df_two_cache[k] = two_map
    return _df_two_cache[k]


def _price_at_900(mnq_1m_df: pd.DataFrame, date: datetime.date) -> Optional[float]:
    """Return the Open of the 09:00 ET 1m bar for date, or None if absent."""
    target = pd.Timestamp(f"{date} 09:00:00", tz=_ET)
    if target in mnq_1m_df.index:
        return float(mnq_1m_df.loc[target, "Open"])
    # Fallback: first bar on that date at or after 09:00
    session_bars = mnq_1m_df[(_index_dates(mnq_1m_df) == date) & (_index_times(mnq_1m_df) >= time(9, 0))]
    return float(session_bars.iloc[0]["Open"]) if not session_bars.empty else None


def _prev_trading_day(date: datetime.date) -> datetime.date:
    """Return the most recent trading day (Mon–Fri) before date."""
    candidate = date - datetime.timedelta(days=1)
    for _ in range(7):
        if candidate.weekday() < 5:
            return candidate
        candidate -= datetime.timedelta(days=1)
    return candidate


def _compute_rule5(mnq_1m_df: pd.DataFrame, date: datetime.date) -> Optional[dict]:
    """Rule 5: TDO / TWO premium-discount zones.

    Returns None when price_at_900 or TDO cannot be determined (insufficient data).
    """
    tdo = compute_tdo(mnq_1m_df, date)
    p900 = _price_at_900(mnq_1m_df, date)
    if tdo is None or p900 is None:
        return None

    # TWO: open of the first 1m bar on Tuesday of the current ISO week
    iso_year, iso_week, _ = date.isocalendar()
    two = _get_two_map(mnq_1m_df).get((iso_year, iso_week))

    day_zone  = ("premium" if p900 > tdo  else "discount") if tdo  is not None else None
    week_zone = ("premium" if p900 > two   else "discount") if two  is not None else None

    return {
        "tdo":           tdo,
        "two":           two,
        "price_at_900":  p900,
        "day_zone":      day_zone,
        "week_zone":     week_zone,
    }


def _compute_rule1(mnq_1m_df: pd.DataFrame, date: datetime.date) -> dict:
    """Rule 1: Previous Day Range case detection and directional bias."""
    prev_day = _prev_trading_day(date)
    dates = _index_dates(mnq_1m_df)
    pd_bars = mnq_1m_df[dates == prev_day]

    if pd_bars.empty:
        return {"pdh": None, "pdl": None, "pd_midpoint": None,
                "pd_range_case": None, "pd_range_bias": "neutral", "price_in_pd_range": False}

    pdh = float(pd_bars["High"].max())
    pdl = float(pd_bars["Low"].min())
    pd_midpoint = (pdh + pdl) / 2.0

    p900 = _price_at_900(mnq_1m_df, date)
    if p900 is None:
        return {"pdh": pdh, "pdl": pdl, "pd_midpoint": pd_midpoint,
                "pd_range_case": None, "pd_range_bias": "neutral", "price_in_pd_range": False}

    price_in_pd_range = pdl <= p900 <= pdh

    # Case 1.5: price_at_900 outside [pdl, pdh] — re-analyse on today's overnight range
    if not price_in_pd_range:
        _times = _index_times(mnq_1m_df)
        overnight = mnq_1m_df[(dates == date) & (_times < time(9, 0))]
        if not overnight.empty:
            pdh = float(overnight["High"].max())
            pdl = float(overnight["Low"].min())
            pd_midpoint = (pdh + pdl) / 2.0
            price_in_pd_range = pdl <= p900 <= pdh
            # Recurse logic on the intraday range for case assignment
            case, bias = _assign_case(overnight, pdh, pdl, pd_midpoint, p900)
            return {
                "pdh": pdh, "pdl": pdl, "pd_midpoint": pd_midpoint,
                "pd_range_case": "1.5", "pd_range_bias": bias,
                "price_in_pd_range": price_in_pd_range,
            }
        return {"pdh": pdh, "pdl": pdl, "pd_midpoint": pd_midpoint,
                "pd_range_case": "1.5", "pd_range_bias": "neutral", "price_in_pd_range": False}

    overnight = mnq_1m_df[(dates == date) & (_index_times(mnq_1m_df) < time(9, 0))]
    case, bias = _assign_case(overnight, pdh, pdl, pd_midpoint, p900)
    return {
        "pdh": pdh, "pdl": pdl, "pd_midpoint": pd_midpoint,
        "pd_range_case": case, "pd_range_bias": bias,
        "price_in_pd_range": price_in_pd_range,
    }


def _assign_case(overnight: pd.DataFrame, pdh: float, pdl: float,
                 pd_midpoint: float, p900: float) -> tuple:
    """Determine Rule 1 case and bias from overnight bar data."""
    if overnight.empty:
        price_now_above_mid = p900 > pd_midpoint
        bias = "short" if price_now_above_mid else "long"
        return "1.3", bias

    crossed_mid = bool(((overnight["Low"] <= pd_midpoint) & (overnight["High"] >= pd_midpoint)).any())
    price_now_above_mid = p900 > pd_midpoint
    rng = pdh - pdl

    near_far_extreme = False
    if rng > 0:
        _oh = overnight["High"].values
        _ol = overnight["Low"].values
        _thresh = 0.15 * rng
        for _h, _l in zip(_oh, _ol):
            if _h > pd_midpoint and (pdh - _h) <= _thresh:
                near_far_extreme = True
                break
            if _l < pd_midpoint and (_l - pdl) <= _thresh:
                near_far_extreme = True
                break

    if not crossed_mid:
        # 1.3: never crossed mid → bias toward opposite extreme
        bias = "short" if price_now_above_mid else "long"
        return "1.3", bias

    if near_far_extreme:
        # 1.2: crossed mid and touched far extreme → expect reversal back
        bias = "short" if price_now_above_mid else "long"
        return "1.2", bias

    if price_now_above_mid:
        # 1.4: crossed mid, above mid, not near far → continuation long
        return "1.4", "long"
    # 1.1: crossed mid, below mid, not near far → bearish continuation
    return "1.1", "short"


def _compute_rule2(hist_mnq_df: pd.DataFrame, date: datetime.date) -> dict:
    """Rule 2: Multi-day trend from 5m historical data."""
    if hist_mnq_df.empty:
        return {"trend_direction": "neutral", "trend_strength": "weak", "days_analyzed": 0}

    dates = _index_dates(hist_mnq_df)
    prior_dates = sorted({d for d in dates if d < date})[-5:]

    if len(prior_dates) < 2:
        return {"trend_direction": "neutral", "trend_strength": "weak", "days_analyzed": len(prior_dates)}

    daily = []
    for d in prior_dates:
        day_bars = hist_mnq_df[dates == d]
        if day_bars.empty:
            continue
        daily.append((float(day_bars["High"].max()), float(day_bars["Low"].min())))

    if len(daily) < 2:
        return {"trend_direction": "neutral", "trend_strength": "weak", "days_analyzed": len(daily)}

    bullish_days = 0
    bearish_days = 0
    for i in range(1, len(daily)):
        hh = daily[i][0] > daily[i - 1][0]
        hl = daily[i][1] > daily[i - 1][1]
        lh = daily[i][0] < daily[i - 1][0]
        ll = daily[i][1] < daily[i - 1][1]
        if hh or hl:
            bullish_days += 1
        if lh or ll:
            bearish_days += 1

    n = len(daily) - 1
    if bullish_days >= 3 and bearish_days == 0:
        direction, strength = "bullish", "strong"
    elif bearish_days >= 3 and bullish_days == 0:
        direction, strength = "bearish", "strong"
    elif bullish_days >= 2 and bearish_days == 0:
        direction, strength = "bullish", "moderate"
    elif bearish_days >= 2 and bullish_days == 0:
        direction, strength = "bearish", "moderate"
    else:
        direction, strength = "neutral", "weak"

    return {"trend_direction": direction, "trend_strength": strength, "days_analyzed": n}


def _compute_rule3(mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame,
                   date: datetime.date, two: Optional[float],
                   trend_direction: str) -> list:
    """Rule 3: Deceptions — stop-hunt wicks confirming the primary trend."""
    if trend_direction == "neutral" or hist_mnq_df.empty:
        return []

    hist_dates = _index_dates(hist_mnq_df)
    prior_dates = sorted({d for d in hist_dates if d < date})[-5:]
    result = []

    for d in prior_dates:
        day_bars = hist_mnq_df[hist_dates == d]
        if len(day_bars) < 2:
            continue

        # Asia session for this day: 20:00-23:59 of prior calendar day
        prior_cal = d - datetime.timedelta(days=1)
        prior_dates_1m = _index_dates(mnq_1m_df) if not mnq_1m_df.empty else []
        if not mnq_1m_df.empty:
            asia_mask = (
                (_index_dates(mnq_1m_df) == prior_cal) &
                (mnq_1m_df.index.time >= time(20, 0))
            )
            asia_bars = mnq_1m_df[asia_mask]
            asia_high = float(asia_bars["High"].max()) if not asia_bars.empty else None
            asia_low  = float(asia_bars["Low"].min())  if not asia_bars.empty else None
        else:
            asia_high = asia_low = None

        _d_highs = day_bars["High"].values
        _d_lows  = day_bars["Low"].values
        for i in range(1, len(_d_highs)):
            if trend_direction == "bullish":
                wick_level = float(_d_lows[i])
                is_wick = wick_level < float(_d_lows[i - 1])
                direction_label = "bearish"
            else:
                wick_level = float(_d_highs[i])
                is_wick = wick_level > float(_d_highs[i - 1])
                direction_label = "bullish"

            if not is_wick:
                continue

            # Check if wick touches TWO or Asia H/L within EVIDENCE_TOUCH_PTS
            touched = None
            if two is not None and abs(wick_level - two) <= EVIDENCE_TOUCH_PTS:
                touched = "two"
            elif asia_high is not None and abs(wick_level - asia_high) <= EVIDENCE_TOUCH_PTS:
                touched = "asia_high"
            elif asia_low is not None and abs(wick_level - asia_low) <= EVIDENCE_TOUCH_PTS:
                touched = "asia_low"

            if touched:
                result.append({
                    "date":           str(d),
                    "direction":      direction_label,
                    "touched_level":  touched,
                    "confirms_trend": True,
                })

    return result


def _compute_rule4(mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame,
                   date: datetime.date) -> dict:
    """Rule 4: Session extremes and last-visited extreme."""
    prior_cal = date - datetime.timedelta(days=1)
    dates = _index_dates(mnq_1m_df)
    hist_dates = _index_dates(hist_mnq_df) if not hist_mnq_df.empty else []

    def _hi_lo(mask):
        bars = mnq_1m_df[mask]
        if bars.empty:
            return None, None
        return float(bars["High"].max()), float(bars["Low"].min())

    asia_mask = (dates == prior_cal) & (mnq_1m_df.index.time >= time(20, 0))
    asia_high, asia_low = _hi_lo(asia_mask)

    london_mask = (dates == date) & (mnq_1m_df.index.time >= time(2, 0)) & (mnq_1m_df.index.time < time(5, 0))
    london_high, london_low = _hi_lo(london_mask)

    pm_mask = (dates == date) & (mnq_1m_df.index.time >= time(7, 0)) & (mnq_1m_df.index.time < time(9, 0))
    ny_premarket_high, ny_premarket_low = _hi_lo(pm_mask)

    # Prior week H/L from hist_mnq_df
    prior_week_high = prior_week_low = None
    if not hist_mnq_df.empty:
        iso_year, iso_week, _ = date.isocalendar()
        pw_year, pw_week = (iso_year, iso_week - 1) if iso_week > 1 else (iso_year - 1, 52)
        pw_bars_list = []
        for d in set(hist_dates):
            dy, dw, _ = d.isocalendar()
            if dy == pw_year and dw == pw_week:
                pw_bars_list.append(d)
        if pw_bars_list:
            pw_mask = [d in pw_bars_list for d in hist_dates]
            pw_bars = hist_mnq_df[pw_mask]
            prior_week_high = float(pw_bars["High"].max())
            prior_week_low  = float(pw_bars["Low"].min())

    # Overnight bars for last_extreme_visited
    overnight_mask = (dates == date) & (mnq_1m_df.index.time < time(9, 0))
    overnight_bars = mnq_1m_df[overnight_mask]

    extremes = {
        "asia_high":         asia_high,
        "asia_low":          asia_low,
        "london_high":       london_high,
        "london_low":        london_low,
        "ny_premarket_high": ny_premarket_high,
        "ny_premarket_low":  ny_premarket_low,
        "prior_week_high":   prior_week_high,
        "prior_week_low":    prior_week_low,
    }

    last_extreme_visited = None
    last_extreme_ts = None
    p900 = _price_at_900(mnq_1m_df, date)

    if not overnight_bars.empty:
        for row in overnight_bars.itertuples():
            close = float(row.Close)
            ts = row.Index
            for level_name, level_val in extremes.items():
                if level_val is None:
                    continue
                if abs(close - level_val) <= 10.0:
                    if last_extreme_ts is None or ts > last_extreme_ts:
                        last_extreme_visited = level_name
                        last_extreme_ts = ts

    # next_extreme_candidate: most recent unvisited extreme on opposite side of last visited
    next_extreme_candidate = None
    if last_extreme_visited is not None and p900 is not None:
        visited_val = extremes[last_extreme_visited]
        if visited_val is not None:
            above_price = visited_val > p900
            # Look for the closest unvisited extreme on the opposite side
            candidates = []
            for name, val in extremes.items():
                if name == last_extreme_visited or val is None:
                    continue
                if above_price and val < p900:
                    candidates.append((name, val))
                elif not above_price and val > p900:
                    candidates.append((name, val))
            if candidates:
                # Most recently touched = closest to price
                candidates.sort(key=lambda x: abs(x[1] - p900))
                next_extreme_candidate = candidates[0][0]

    return {
        **extremes,
        "last_extreme_visited":  last_extreme_visited,
        "next_extreme_candidate": next_extreme_candidate,
    }


def _compute_overnight(mnq_1m_df: pd.DataFrame, date: datetime.date) -> dict:
    """Overnight high/low: bars where index.date == date and time < 09:00."""
    dates = _index_dates(mnq_1m_df)
    mask = (dates == date) & (mnq_1m_df.index.time < time(9, 0))
    bars = mnq_1m_df[mask]
    if bars.empty:
        return {"overnight_high": None, "overnight_low": None}
    return {
        "overnight_high": float(bars["High"].max()),
        "overnight_low":  float(bars["Low"].min()),
    }


def _compute_fvgs(mnq_1m_df: pd.DataFrame, date: datetime.date,
                  price_at_900: float, tdo: float) -> list:
    """Find Fair Value Gaps in the last FVG_LOOKBACK_BARS before 09:00 that lie between price and TDO."""
    if price_at_900 is None or tdo is None:
        return []

    dates = _index_dates(mnq_1m_df)
    pre_session = mnq_1m_df[(dates == date) & (mnq_1m_df.index.time < time(9, 0))]
    bars = pre_session.tail(FVG_LOOKBACK_BARS + 2).reset_index(drop=True)

    if len(bars) < 3:
        return []

    result = []
    for i in range(1, len(bars) - 1):
        prev_high = float(bars.iloc[i - 1]["High"])
        prev_low  = float(bars.iloc[i - 1]["Low"])
        next_high = float(bars.iloc[i + 1]["High"])
        next_low  = float(bars.iloc[i + 1]["Low"])

        # Bullish FVG: gap between prev_high and next_low (next_low > prev_high)
        if next_low > prev_high:
            fvg_low, fvg_high = prev_high, next_low
            if tdo > price_at_900 and fvg_low >= price_at_900 and fvg_high <= tdo:
                result.append({"low": fvg_low, "high": fvg_high})
        # Bearish FVG: gap between next_high and prev_low (next_high < prev_low)
        if next_high < prev_low:
            fvg_low, fvg_high = next_high, prev_low
            if tdo < price_at_900 and fvg_high <= price_at_900 and fvg_low >= tdo:
                result.append({"low": fvg_low, "high": fvg_high})

    return result


def _weighted_vote(rule1_bias: Optional[str], week_zone: Optional[str],
                   day_zone: Optional[str], trend_direction: Optional[str]) -> str:
    """Weighted directional vote across four signals."""
    long_score = short_score = 0

    if rule1_bias == "long":
        long_score += 3
    elif rule1_bias == "short":
        short_score += 3

    if week_zone == "discount":
        long_score += 2
    elif week_zone == "premium":
        short_score += 2

    if day_zone == "discount":
        long_score += 1
    elif day_zone == "premium":
        short_score += 1

    if trend_direction == "bullish":
        long_score += 1
    elif trend_direction == "bearish":
        short_score += 1

    if long_score >= 2 and long_score > short_score:
        return "long"
    if short_score >= 2 and short_score > long_score:
        return "short"
    return "neutral"


# ── Public API ────────────────────────────────────────────────────────────────

def compute_hypothesis_direction(mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame,
                                  date: datetime.date) -> Optional[str]:
    """Deterministic rule engine. No side effects. Safe to call from backtest."""
    try:
        r5 = _compute_rule5(mnq_1m_df, date)
        if r5 is None:
            return None
        r1 = _compute_rule1(mnq_1m_df, date)
        r2 = _compute_rule2(hist_mnq_df, date)
        return _weighted_vote(
            r1["pd_range_bias"],
            r5["week_zone"],
            r5["day_zone"],
            r2["trend_direction"],
        )
    except Exception:
        return None  # insufficient data → caller treats as no hypothesis


def _count_aligned_rules(r1_bias, w_zone, d_zone, trend, direction):
    """Count rule votes aligned with direction (0-4). 0 when direction is None/neutral."""
    if not direction or direction == "neutral":
        return 0
    score = 0
    if r1_bias == direction:
        score += 1
    if (w_zone == "discount" and direction == "long") or (w_zone == "premium" and direction == "short"):
        score += 1
    if (d_zone == "discount" and direction == "long") or (d_zone == "premium" and direction == "short"):
        score += 1
    if (trend == "bullish" and direction == "long") or (trend == "bearish" and direction == "short"):
        score += 1
    return score


def compute_hypothesis_context(
    mnq_1m_df: pd.DataFrame,
    hist_mnq_df: pd.DataFrame,
    date: datetime.date,
) -> Optional[dict]:
    """Return per-rule breakdown + final direction + score. No side effects. Safe for backtest."""
    try:
        r5 = _compute_rule5(mnq_1m_df, date)
        if r5 is None:
            return None
        r1 = _compute_rule1(mnq_1m_df, date)
        r2 = _compute_rule2(hist_mnq_df, date)
        direction = _weighted_vote(
            r1["pd_range_bias"],
            r5["week_zone"],
            r5["day_zone"],
            r2["trend_direction"],
        )
        r1_bias       = r1.get("pd_range_bias", "neutral")
        w_zone        = r5.get("week_zone")
        d_zone        = r5.get("day_zone")
        trend         = r2.get("trend_direction", "neutral")
        return {
            "direction":        direction,
            "pd_range_case":    r1.get("pd_range_case"),
            "pd_range_bias":    r1_bias,
            "week_zone":        w_zone,
            "day_zone":         d_zone,
            "trend_direction":  trend,
            "hypothesis_score": _count_aligned_rules(r1_bias, w_zone, d_zone, trend, direction),
        }
    except Exception:
        return None


def _build_session_context(mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame,
                            date: datetime.date) -> Optional[dict]:
    """Assemble ALL rule outputs into the session_context dict for Claude."""
    r5 = _compute_rule5(mnq_1m_df, date)
    if r5 is None:
        return None
    r1 = _compute_rule1(mnq_1m_df, date)
    r2 = _compute_rule2(hist_mnq_df, date)
    r3 = _compute_rule3(mnq_1m_df, hist_mnq_df, date, r5.get("two"), r2["trend_direction"])
    r4 = _compute_rule4(mnq_1m_df, hist_mnq_df, date)
    overnight = _compute_overnight(mnq_1m_df, date)
    fvgs = _compute_fvgs(mnq_1m_df, date, r5["price_at_900"], r5["tdo"])
    return {**r5, **r1, **r2, "deceptions": r3, **r4, **overnight, "fvgs_toward_tdo": fvgs}


def _extract_response_text(msg) -> str:
    """Return the first non-empty text block from a Claude Messages response.

    Raises RuntimeError with diagnostic info if no text block is present —
    e.g. when extended thinking consumes all tokens and no TextBlock is emitted.
    """
    for block in msg.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    block_types = [getattr(b, "type", type(b).__name__) for b in msg.content]
    stop_reason = getattr(msg, "stop_reason", "unknown")
    raise RuntimeError(
        f"Claude response had no text block; stop_reason={stop_reason} blocks={block_types}"
    )


# ── HypothesisManager ─────────────────────────────────────────────────────────

class HypothesisManager:
    """Realtime hypothesis lifecycle manager. Not safe for backtest use (makes API calls)."""

    GENERATION_SYSTEM_PROMPT = """
You are an ICT/SMT market analyst. You receive a session_context dict containing
computed values for 5 rules: Rule 1 (Previous Day Range), Rule 2 (Multi-day Trend),
Rule 3 (Deceptions/Liquidity Sweeps), Rule 4 (Session Extremes), Rule 5 (Premium/Discount vs TDO/TWO).

Your task: produce a directional hypothesis for the NY session (9:30am ET open).

Rules:
- Rule 1 (pd_range_case + pd_range_bias): PRIMARY signal, weight 3. Mandatory.
- Rule 5 week_zone: weight 2. Discount = expect long; Premium = expect short.
- Rule 5 day_zone: weight 1. Yields to week_zone on conflict.
- Rule 2 trend: weight 1. Weakest; yields to Rules 1+5 on conflict.
- Rule 3 deceptions: informational. Trend-confirming sweep history.
- Rule 4 session extremes: supportive hint. Not decisive alone.

The overnight_high / overnight_low are the most likely 9:30am liquidity grab targets.
Always describe the expected sweep direction in expected_scenario before the real move.

Respond ONLY with valid JSON matching this schema exactly:
{
  "direction_bias": "long | short | neutral",
  "confidence": "low | medium | high",
  "narrative": "...",
  "primary_reason": "...",
  "supporting_factors": ["..."],
  "contradicting_factors": ["..."],
  "key_levels_to_watch": [<floats>],
  "expected_scenario": "..."
}
""".strip()

    REVISION_SYSTEM_PROMPT = """
You are reviewing a session hypothesis that has accumulated contradicting evidence.
Given the original hypothesis, evidence log, and current market context, revise the hypothesis.
Respond with valid JSON matching this schema exactly:
{
  "direction_bias": "long | short | neutral",
  "confidence": "low | medium | high",
  "narrative": "...",
  "primary_reason": "...",
  "supporting_factors": ["..."],
  "contradicting_factors": ["..."],
  "key_levels_to_watch": [<floats>],
  "expected_scenario": "...",
  "revision_reason": "...",
  "replaces_hypothesis": true
}
""".strip()

    SUMMARY_SYSTEM_PROMPT = """
You are summarizing a completed trading session hypothesis vs actual outcome.
Given the full hypothesis JSON (including evidence_log, any revisions, signals_fired),
produce a concise session summary.
Respond with valid JSON matching this schema exactly:
{
  "summary": "...",
  "hypothesis_accuracy": "correct | incorrect | partial | no_signal",
  "learnings": ["...", "..."]
}
""".strip()

    def __init__(self, mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame, date: datetime.date):
        self._mnq_1m_df    = mnq_1m_df
        self._hist_mnq_df  = hist_mnq_df
        self._date         = date
        self._direction_bias: Optional[str] = None
        self._session_data: Optional[dict]  = None
        self._hypothesis_file: Optional[Path] = None
        self._contradiction_count = 0
        self._revision_triggered  = False
        self._call_count          = 0  # max 3 per session
        self._key_levels: list = []

    @property
    def direction_bias(self) -> Optional[str]:
        return self._direction_bias

    def generate(self) -> None:
        """Run rule engine + Claude Call 1. Writes hypothesis file. Prints to terminal."""
        import anthropic  # lazy import — not needed in backtest path

        context = _build_session_context(self._mnq_1m_df, self._hist_mnq_df, self._date)
        if context is None:
            print("[HYPOTHESIS] Insufficient data — skipping hypothesis generation.", flush=True)
            return

        direction = _weighted_vote(
            context.get("pd_range_bias", "neutral"),
            context.get("week_zone", "neutral"),
            context.get("day_zone", "neutral"),
            context.get("trend_direction", "neutral"),
        )
        self._direction_bias = direction

        hypothesis_json = None
        if self._call_count < 3:
            try:
                client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
                msg = client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=4096,
                    system=self.GENERATION_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": json.dumps(context, default=str)}],
                )
                self._call_count += 1
                raw = _extract_response_text(msg)
                hypothesis_json = json.loads(raw)
                self._direction_bias = hypothesis_json.get("direction_bias", direction)
                self._key_levels = hypothesis_json.get("key_levels_to_watch", [])
            except Exception as e:
                # API failure is non-fatal — deterministic direction still used
                print(f"[HYPOTHESIS] Claude call failed: {e}", flush=True)
                hypothesis_json = {
                    "direction_bias":       direction,
                    "confidence":           "low",
                    "narrative":            f"Rule engine only (API error: {e})",
                    "primary_reason":       context.get("pd_range_case", "unknown"),
                    "supporting_factors":   [],
                    "contradicting_factors": [],
                    "key_levels_to_watch":  [],
                    "expected_scenario":    "",
                }

        # Ensure hypothesis_json always has a valid dict before writing
        if hypothesis_json is None:
            hypothesis_json = {
                "direction_bias":        direction,
                "confidence":            "low",
                "narrative":             "Rule engine only (max calls reached at generation)",
                "primary_reason":        context.get("pd_range_case", "unknown"),
                "supporting_factors":    [],
                "contradicting_factors": [],
                "key_levels_to_watch":   [],
                "expected_scenario":     "",
            }

        now_et = pd.Timestamp.now(tz=_ET)
        self._session_data = {
            "date":            str(self._date),
            "generated_at":    now_et.isoformat(),
            "session_context": context,
            "hypothesis":      hypothesis_json,
            "contradiction_count": 0,
            "evidence_log":    [],
            "revisions":       [],
            "signals_fired":   [],
            "summary":         None,
        }

        session_dir = SESSION_DATA_DIR / str(self._date)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._hypothesis_file = session_dir / "hypothesis.json"
        self._write_file()
        self._print_hypothesis(now_et, hypothesis_json)

    def evaluate_bar(self, bar) -> None:
        """Per 1m bar evidence check. No LLM. May trigger revision if threshold reached."""
        if self._session_data is None or self._direction_bias is None:
            return

        bar_ts = pd.Timestamp(bar.date if hasattr(bar, "date") else bar.name)
        if bar_ts.tz is None:
            bar_ts = bar_ts.tz_localize(_ET)
        close = float(bar.close if hasattr(bar, "close") else bar["Close"])
        high  = float(bar.high  if hasattr(bar, "high")  else bar["High"])
        low   = float(bar.low   if hasattr(bar, "low")   else bar["Low"])

        tdo   = self._session_data["session_context"].get("tdo")
        bias  = self._direction_bias
        findings: list = []

        # Check 1: key level touch (within EVIDENCE_TOUCH_PTS)
        for level in self._key_levels:
            if (abs(close - level) <= EVIDENCE_TOUCH_PTS
                    or abs(low - level) <= EVIDENCE_TOUCH_PTS
                    or abs(high - level) <= EVIDENCE_TOUCH_PTS):
                findings.append({
                    "time":           bar_ts.isoformat(),
                    "event":          f"Price touched key level {level:.2f}",
                    "classification": "neutral",
                    "level_touched":  str(level),
                    "bar_close":      close,
                })

        # Check 2: >15 pts move in or against hypothesis direction
        prev_close = (self._session_data["evidence_log"][-1]["bar_close"]
                      if self._session_data["evidence_log"] else None)
        if prev_close is not None:
            move = (close - prev_close) if bias == "long" else (prev_close - close)
            if move > EVIDENCE_MOVE_PTS:
                findings.append({
                    "time":           bar_ts.isoformat(),
                    "event":          f"Price moved {move:.1f} pts in hypothesis direction",
                    "classification": "supports",
                    "level_touched":  None,
                    "bar_close":      close,
                })
            elif -move > EVIDENCE_MOVE_PTS:
                findings.append({
                    "time":           bar_ts.isoformat(),
                    "event":          f"Price moved {-move:.1f} pts against hypothesis",
                    "classification": "contradicts",
                    "level_touched":  None,
                    "bar_close":      close,
                })

        # Check 5: extreme contradiction — close >20 pts beyond TDO in wrong direction
        if tdo is not None:
            if bias == "long" and close < tdo - EXTREME_CONTRADICTION_PTS:
                findings.append({
                    "time":           bar_ts.isoformat(),
                    "event":          f"EXTREME: closed {close:.2f}, {tdo - close:.1f} pts below TDO ({tdo:.2f})",
                    "classification": "contradicts",
                    "level_touched":  "tdo",
                    "bar_close":      close,
                    "extreme":        True,
                })
                self._contradiction_count = CONTRADICTION_THRESHOLD  # instant trigger
            elif bias == "short" and close > tdo + EXTREME_CONTRADICTION_PTS:
                findings.append({
                    "time":           bar_ts.isoformat(),
                    "event":          f"EXTREME: closed {close:.2f}, {close - tdo:.1f} pts above TDO ({tdo:.2f})",
                    "classification": "contradicts",
                    "level_touched":  "tdo",
                    "bar_close":      close,
                    "extreme":        True,
                })
                self._contradiction_count = CONTRADICTION_THRESHOLD

        for f in findings:
            if f["classification"] == "contradicts" and not f.get("extreme"):
                self._contradiction_count += 1
            self._session_data["evidence_log"].append(f)
            self._session_data["contradiction_count"] = self._contradiction_count
            self._print_evidence(bar_ts, f)

        self._write_file()

        # Trigger revision if threshold reached and not already revised
        if self._contradiction_count >= CONTRADICTION_THRESHOLD and not self._revision_triggered:
            self._revision_triggered = True
            self._revise(bar_ts)

    def _revise(self, bar_ts: pd.Timestamp) -> None:
        """Claude Call 2 — revision on contradiction threshold."""
        import anthropic
        if self._call_count >= 3:
            return
        try:
            client = anthropic.Anthropic()
            payload = {
                "original_hypothesis": self._session_data["hypothesis"],
                "evidence_log":        self._session_data["evidence_log"],
                "session_context":     self._session_data["session_context"],
            }
            msg = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=self.REVISION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
            self._call_count += 1
            raw = _extract_response_text(msg)
            revision = json.loads(raw)
            self._direction_bias = revision.get("direction_bias", self._direction_bias)
            self._key_levels = revision.get("key_levels_to_watch", self._key_levels)
            self._session_data["revisions"].append(revision)
            self._write_file()
            self._print_revision(bar_ts, revision)
        except Exception as e:
            print(f"[HYPOTHESIS] Revision call failed: {e}", flush=True)

    def finalize(self, signal: Optional[dict], exit_result: Optional[dict]) -> None:
        """Claude Call 3 — session summary. Called at session end."""
        import anthropic
        if self._session_data is None:
            return

        if signal is not None:
            fired_entry = {
                "direction":          signal.get("direction"),
                "entry_time":         str(signal.get("entry_time", "")),
                "entry_price":        signal.get("entry_price"),
                "matches_hypothesis": signal.get("matches_hypothesis"),
                "exit_type":          exit_result.get("exit_type") if exit_result else None,
                "pnl":                exit_result.get("pnl") if exit_result else None,
            }
            self._session_data["signals_fired"].append(fired_entry)
            self._write_file()

        if self._call_count >= 3:
            print("[HYPOTHESIS] Max API calls reached — skipping summary.", flush=True)
            return

        now_et = pd.Timestamp.now(tz=_ET)
        try:
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=self.SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(self._session_data, default=str)}],
            )
            self._call_count += 1
            raw = _extract_response_text(msg)
            summary_json = json.loads(raw)
            self._session_data["summary"] = summary_json
            self._write_file()
            print(f"[{now_et.strftime('%H:%M:%S')}] SUMMARY     {summary_json.get('summary', '')}", flush=True)
        except Exception as e:
            print(f"[HYPOTHESIS] Summary call failed: {e}", flush=True)

    def _write_file(self) -> None:
        """Atomic file write — write to .tmp then rename."""
        if self._hypothesis_file is None or self._session_data is None:
            return
        tmp = self._hypothesis_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._session_data, indent=2, default=str))
        tmp.replace(self._hypothesis_file)

    def _print_hypothesis(self, ts: pd.Timestamp, h: dict) -> None:
        bias       = h.get("direction_bias", "?")
        confidence = h.get("confidence", "?")
        narrative  = h.get("narrative", "")
        expected   = h.get("expected_scenario", "")
        levels     = h.get("key_levels_to_watch", [])
        contradict = h.get("contradicting_factors", [])
        print(f"[{ts.strftime('%H:%M:%S')}] HYPOTHESIS  {bias} ({confidence} confidence)", flush=True)
        if narrative:
            for line in narrative.split("\n"):
                print(f"           {line}", flush=True)
        if contradict:
            print(f"           Contradicts: {'; '.join(contradict)}", flush=True)
        if expected:
            print(f"           Expected: {expected}", flush=True)
        if levels:
            print(f"           Levels: {' -> '.join(str(l) for l in levels)}", flush=True)

    def _print_evidence(self, ts: pd.Timestamp, finding: dict) -> None:
        cls = finding["classification"]
        count_str = f"({self._contradiction_count})" if cls == "contradicts" else ""
        print(
            f"[{ts.strftime('%H:%M:%S')}] EVIDENCE    {cls}{count_str:<15} | {finding['event']}",
            flush=True,
        )

    def _print_revision(self, ts: pd.Timestamp, revision: dict) -> None:
        bias   = revision.get("direction_bias", "?")
        conf   = revision.get("confidence", "?")
        reason = revision.get("revision_reason", "")
        print(f"[{ts.strftime('%H:%M:%S')}] HYPOTHESIS REVISED -> {bias} ({conf} confidence)", flush=True)
        if reason:
            print(f"           {reason}", flush=True)
