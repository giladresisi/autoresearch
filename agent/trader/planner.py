"""Thesis -> plan. Deterministic; no model call, no I/O.

The plan is what the Executor binds against: a direction, a DOL, the predicates that
kill it, and the set of mechanism classes that are ARMED. Arming is the only real
decision here and it is a two-line rule (l2-mechanisms.md §3–§7):

  - the thesis OPPOSES the last trend  -> `fvg_negation_reversal`
  - the thesis AGREES with the last trend -> `fvg_return_continuation`
  - `fvg_1m_post_extreme` and `extreme_reject_close` verify their own preconditions
    continuously, so they are ALWAYS armed (§6/§7).

"Last trend" is §3's: of the qualifying legs whose extreme formed <= 60 min ago, the one
with the larger range. A stale leg is not a trend to reverse — but note §3's scope
warning, reproduced in `legs.last_trend`: that recency test scopes the REVERSAL choice
only and must never be used to age out continuation gaps.

Step-8b's near-secondary veto (`hypothesis.py:2129-2138`) is deliberately NOT carried.
"""
from __future__ import annotations

import hashlib

import pandas as pd

from agent.facts.detectors.legs import last_trend

MECHANISM_CLASSES = (
    "fvg_negation_reversal",
    "fvg_return_continuation",
    "fvg_1m_post_extreme",
    "extreme_reject_close",
)

# §6/§7 verify their own preconditions on every bar, so there is nothing for the
# Planner to decide about them.
SELF_GATING_CLASSES = ("fvg_1m_post_extreme", "extreme_reject_close")

_BIAS_TO_LEG = {"DOWN": "down", "SHORT": "down", "UP": "up", "LONG": "up"}


def _plan_id(thesis: dict, now) -> str:
    parts = [str((thesis or {}).get("thesis_id") or ""),
             str((thesis or {}).get("bias") or ""),
             str((((thesis or {}).get("dol") or {}) or {}).get("price")),
             str(now)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def derive_plan(thesis: dict, legs, now: pd.Timestamp) -> "dict | None":
    """Thesis + legs -> plan dict. Returns None only for a non-dict thesis."""
    if not isinstance(thesis, dict):
        return None

    bias = str(thesis.get("bias") or "").upper()
    want_leg = _BIAS_TO_LEG.get(bias)

    armed = list(SELF_GATING_CLASSES)
    trend = last_trend(list(legs or ()), now)
    trend_dir = (trend.extra.get("direction") if trend is not None else None)
    if want_leg is not None and trend_dir is not None:
        if trend_dir != want_leg:
            armed.insert(0, "fvg_negation_reversal")       # thesis reverses the trend
        else:
            armed.insert(0, "fvg_return_continuation")     # thesis rides it

    valid_while = list(thesis.get("falsified_if") or []) + list(thesis.get("exhausted_if") or [])

    return {
        "plan_id": _plan_id(thesis, now),
        "thesis_id": thesis.get("thesis_id"),
        "direction": bias or None,
        "dol": thesis.get("dol"),
        "valid_while": valid_while,
        "armed_classes": armed,
        "attempts_used": 0,
        "blacklist": [],
        "cooldown_until": None,
        "created_at": str(now) if now is not None else None,
        "last_trend": ({"id": trend.id, "direction": trend_dir,
                        "range": trend.extra.get("range"),
                        "extreme_ts": str(trend.reference_ts)} if trend is not None
                       else None),
    }
