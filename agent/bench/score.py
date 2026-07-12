"""Per-lifecycle scoring + per-day metrics (plan 10 Phase 3).

Enriches each lifecycle record with the two quality signals the internals effort will
watch move:

  false_kill        — after a NON-completion death (falsified / safety_net / ttl), did
                      price still touch the dead thesis's DOL within the lookahead horizon
                      H (=4h, annotate.py convention)? A True means the kill was premature.
  late_kill_adverse — for a falsified directional thesis, the give-back in points from the
                      lifecycle's peak-favorable point to the falsification fire.

Then rolls the day up into the scorecard metrics (completion rate, false-kill count,
median late-kill adverse, churn, coverage, failsafe rate, API cost, latency). Pure over
the logged summary + the bar source; `rescore` re-runs this identically.
"""

from __future__ import annotations

import statistics
from typing import Optional

import pandas as pd

# Anthropic price table (USD per 1M tokens) — Haiku 4.5 default; used only to estimate
# the run's API cost for the scorecard. Structure-reserved; values are list-price.
_PRICES = {
    "default": {"in": 1.0, "out": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "cache_read": 0.10, "cache_write": 1.25},
}


def estimate_cost(usage: dict, model: str = "default") -> float:
    u = usage or {}
    p = _PRICES.get(model) or _PRICES["default"]
    return (
        (u.get("input_tokens") or 0) * p["in"]
        + (u.get("output_tokens") or 0) * p["out"]
        + (u.get("cache_read_input_tokens") or 0) * p["cache_read"]
        + (u.get("cache_creation_input_tokens") or 0) * p["cache_write"]
    ) / 1_000_000.0


def _ts(v) -> Optional[pd.Timestamp]:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t


def _false_kill(lc: dict, bars: pd.DataFrame, horizon: pd.Timedelta) -> Optional[bool]:
    """True iff a non-completion death's DOL was still touched within H after death."""
    if lc.get("cause") not in ("falsified", "safety_net", "ttl"):
        return None
    bias, dol = lc.get("bias"), lc.get("dol_price")
    died = _ts(lc.get("died_ts"))
    if bias not in ("UP", "DOWN") or not isinstance(dol, (int, float)) or died is None:
        return None
    window = bars[(bars.index > died) & (bars.index <= died + horizon)]
    if len(window) == 0:
        return False
    if bias == "UP":
        return bool((window["high"] >= dol).any())
    return bool((window["low"] <= dol).any())


def _lookahead_truncated(lc: dict, bars: pd.DataFrame, horizon: pd.Timedelta) -> Optional[bool]:
    """For a false_kill-eligible death, whether the H-window ran past the last available
    bar (session end) — i.e. a `false_kill == False` might be a truncated verdict, not a
    confirmed one. None when false_kill is not applicable."""
    if lc.get("cause") not in ("falsified", "safety_net", "ttl"):
        return None
    if lc.get("bias") not in ("UP", "DOWN") or not isinstance(lc.get("dol_price"), (int, float)):
        return None
    died = _ts(lc.get("died_ts"))
    if died is None or len(bars) == 0:
        return None
    return bool(died + horizon > bars.index[-1])


def _late_kill_adverse(lc: dict, bars: pd.DataFrame) -> Optional[float]:
    """Give-back (pts) from the peak-favorable point to the falsification fire, for a
    falsified directional thesis."""
    if lc.get("cause") != "falsified":
        return None
    bias, ref = lc.get("bias"), lc.get("arrival_price")
    died = _ts(lc.get("died_ts"))
    mfe = lc.get("mfe")
    if bias not in ("UP", "DOWN") or not isinstance(ref, (int, float)) or died is None:
        return None
    row = bars[bars.index <= died]
    if len(row) == 0:
        return None
    bar = row.iloc[-1]
    fav_at_death = (float(bar["high"]) - ref) if bias == "UP" else (ref - float(bar["low"]))
    give_back = (mfe or 0.0) - fav_at_death
    return round(max(0.0, give_back), 2)


def score_date(summary: dict, source, cfg) -> dict:
    """Enrich a run_date/rescore_date summary in place-ish (returns a new dict) with
    per-lifecycle scoring + the per-day metrics block."""
    date = summary["date"]
    bars = source.session_bars(date)
    open_ts, end_ts = source.session_window(date)
    horizon = cfg.lookahead_h

    lifecycles = [dict(lc) for lc in summary["lifecycles"]]
    for lc in lifecycles:
        lc["false_kill"] = _false_kill(lc, bars, horizon)
        lc["late_kill_adverse"] = _late_kill_adverse(lc, bars)
        lc["lookahead_truncated"] = _lookahead_truncated(lc, bars, horizon)

    stood = [lc for lc in lifecycles if lc.get("stood")]
    completed = [lc for lc in stood if lc.get("cause") == "completed"]
    falsified = [lc for lc in lifecycles if lc.get("cause") == "falsified"]
    late_vals = [lc["late_kill_adverse"] for lc in falsified
                 if isinstance(lc.get("late_kill_adverse"), (int, float))]

    # Coverage: standing minutes / session minutes.
    session_min = max(1.0, (end_ts - open_ts).total_seconds() / 60.0)
    standing_min = 0.0
    for lc in stood:
        a, d = _ts(lc.get("arrival_ts")), _ts(lc.get("died_ts"))
        if a is not None and d is not None:
            standing_min += max(0.0, (d - a).total_seconds() / 60.0)
    coverage_pct = round(min(100.0, standing_min / session_min * 100.0), 1)

    # API cost + tokens + latency (over the decisions).
    decisions = summary.get("decisions") or []
    cost = 0.0
    tin = tout = cread = 0
    latencies = []
    for rec in decisions:
        u = rec.get("usage_total") or {}
        cost += estimate_cost(u)
        tin += u.get("input_tokens") or 0
        tout += u.get("output_tokens") or 0
        cread += u.get("cache_read_input_tokens") or 0
        lt = rec.get("latency_total_sec")
        if isinstance(lt, (int, float)) and lt > 0:
            latencies.append(lt)

    metrics = {
        "n_lifecycles": len(lifecycles),
        "n_directional": len(stood),
        "n_completed": len(completed),
        "completion_rate": round(len(completed) / len(stood), 3) if stood else None,
        "false_kill_count": sum(1 for lc in lifecycles if lc.get("false_kill") is True),
        "median_late_kill_adverse": round(statistics.median(late_vals), 2) if late_vals else None,
        "churn": summary.get("n_calls", 0),
        "coverage_pct": coverage_pct,
        "failsafe_rate": round(
            sum(1 for lc in lifecycles if lc.get("cause") == "failsafe") / len(lifecycles), 3)
        if lifecycles else None,
        "api_cost_usd": round(cost, 6),
        "tokens_in": tin, "tokens_out": tout, "cache_read": cread,
        "latency_mean": round(statistics.mean(latencies), 2) if latencies else None,
        "latency_median": round(statistics.median(latencies), 2) if latencies else None,
    }

    return {
        "date": date, "regime": cfg.regime_for(date),
        "day_outcome": summary.get("day_outcome", "ok"),
        "session_open": open_ts.isoformat(), "session_end": end_ts.isoformat(),
        "session_minutes": round(session_min, 1),
        "lifecycles": lifecycles, "metrics": metrics,
        "decisions": decisions,
    }
