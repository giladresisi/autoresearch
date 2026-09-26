"""Thesis -> plan. Deterministic; no model call, no I/O.

The plan is what the Executor binds against: a direction, a DOL, the predicates that
kill it, and the set of mechanism classes that are ARMED. Arming is the only real
decision here and it is a two-line rule (l2-mechanisms.md §3–§7):

  - the thesis OPPOSES the last trend  -> `fvg_negation_reversal`   (SUSPENDED, see below)
  - the thesis AGREES with the last trend -> `fvg_return_continuation` (SUSPENDED)
  - `fvg_1m_post_extreme` and `extreme_reject_close` verify their own preconditions
    continuously, so they are ALWAYS armed (§6/§7).

"Last trend" is §3's: of the qualifying legs whose extreme formed <= 60 min ago, the one
with the larger range. A stale leg is not a trend to reverse — but note §3's scope
warning, reproduced in `legs.last_trend`: that recency test scopes the REVERSAL choice
only and must never be used to age out continuation gaps.

Step-8b's near-secondary veto (`hypothesis.py:2129-2138`) is deliberately NOT carried.

**Since 2026-09-16 the two 5m-FVG classes are NOT armed** — `five_min_armed()` is False by
default and `ACT_TRADER_5M=1` restores them. The rule above is otherwise untouched and the
mechanisms are untouched; this is a suspension pending plan 36's D2/D3, not a removal. The
measurement and the known counterexample are on `five_min_armed`.

**`micro_smt_reject` (O3, ADOPTED 2026-09-26, `l2-mechanisms.md` §7a) is armed only when
`micro_smt.MICRO_SMT_ENTRY_ENABLED` is True** — ON by default since adoption, but still a
CONDITIONAL arm (unlike the unconditional self-gating classes above) so a rollback
(`MICRO_SMT_ENTRY_ENABLED = False`) byte-matches pre-adoption plans. See `micro_smt.py`.
"""
from __future__ import annotations

import hashlib
import os

import pandas as pd

from agent.facts.detectors.legs import last_trend
from agent.trader.micro_smt import micro_smt_entry_armed

MECHANISM_CLASSES = (
    "fvg_negation_reversal",
    "fvg_return_continuation",
    "fvg_1m_post_extreme",
    "extreme_reject_close",
    "tmso_reject",
    "fvg_1h_reject",
    "micro_smt_reject",
)

# §6/§7 verify their own preconditions on every bar, so there is nothing for the
# Planner to decide about them.
SELF_GATING_CLASSES = ("fvg_1m_post_extreme", "extreme_reject_close", "tmso_reject",
                       "fvg_1h_reject")

#: The two 5m-FVG entry mechanisms. Kept as a named pair because they are arming
#: ALTERNATIVES — §3's rule picks exactly one of them — and are now suspended together.
FIVE_MIN_CLASSES = ("fvg_negation_reversal", "fvg_return_continuation")

FIVE_MIN_ENV_FLAG = "ACT_TRADER_5M"


def five_min_armed() -> bool:
    """OFF by default since 2026-09-16. `ACT_TRADER_5M=1` (or true/yes/on) re-arms them.

    **Suspended, not deleted.** Every line of §4 and §5 stays exactly where it is: the
    Executor's resting-stop path, the arbiter's `RESTING_MECHANISMS`, the named cases and
    their tests. Only the Planner stops putting the names in `armed_classes`, which is the
    single gate `Executor._entry_mechanism` reads. Flipping this back is one variable.

    **Why.** A three-arm engine replay over 2026-08-31..09-04 (plan 36, "the budget finding
    that unblocks it"):

        A  as shipped                 -45.75 pts
        B  these two unarmed         +199.25 pts     <- this
        C  B, and 5m gaps invisible   +83.50 pts

    B wins four of the five days and produces the only two `target_reached` deaths in the
    set. The failure mode is specific: these mechanisms RE-ARM on the same artifact at the
    same trigger and the same stop after being stopped out — 09-01 spends attempts 1 and 2
    on one zone 53 seconds apart, and is dead at 09:40:24, 72 seconds before the low that
    launched a 277-point move. With them unarmed that day still has two attempts left at
    09:43.

    **What is NOT done here, deliberately.** `Arbiter.sec6_armed` still stands §6 down while
    a usable 5m gap exists. That is arm C, and C is REJECTED: releasing §6 on 09-02 fires it
    at 09:44:01 and 09:47:16 and kills the plan at 09:49:40, seven minutes before the
    09:56:00 entry that arm B takes to take-profit for +140.50. The 5m gaps keep doing their
    one useful job — suppressing §6 — without being traded.

    **The counterexample, stated so it is not lost.** 08-31 is the one day where the 5m
    continuation entry was right (+21.50 marked, MFE +107.25); B replaces it with three
    losers, -53.25. Plan 36's D2 is specified to take that day's real entry at 09:33:00
    instead, and until it exists this is a measured trade-off, not a free win. n=5, on the
    five days this cycle was fitted to — the 29-session engine scan is still owed.
    """
    raw = str(os.environ.get(FIVE_MIN_ENV_FLAG, "")).strip().lower()
    return raw in ("1", "true", "yes", "on")

_BIAS_TO_LEG = {"DOWN": "down", "SHORT": "down", "UP": "up", "LONG": "up"}


def _plan_id(thesis: dict, now) -> str:
    parts = [str((thesis or {}).get("thesis_id") or ""),
             str((thesis or {}).get("bias") or ""),
             str((((thesis or {}).get("dol") or {}) or {}).get("price")),
             str(now)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def derive_plan(thesis: dict, legs, now: pd.Timestamp,
                *, five_min: "bool | None" = None) -> "dict | None":
    """Thesis + legs -> plan dict. Returns None only for a non-dict thesis.

    `five_min` overrides the §4/§5 suspension for ONE call: None reads `five_min_armed()`,
    which is what production does. It exists for `agent/study/entries.py`, whose subject IS
    the 5m entry path — forcing it there keeps the study measuring what it was written to
    measure instead of silently reporting NO_MECHANISM on every date.
    """
    if not isinstance(thesis, dict):
        return None

    bias = str(thesis.get("bias") or "").upper()
    want_leg = _BIAS_TO_LEG.get(bias)

    armed = list(SELF_GATING_CLASSES)
    if micro_smt_entry_armed():
        # O3, ADOPTED (`l2-mechanisms.md` §7a), flag-gated ON by default — see
        # `micro_smt.py`. Unlike the unconditional self-gating classes above, this one
        # is not armed at all when its flag is False, so a rollback byte-matches
        # pre-adoption plans.
        armed.append("micro_smt_reject")
    trend = last_trend(list(legs or ()), now)
    trend_dir = (trend.extra.get("direction") if trend is not None else None)
    # `last_trend` is still computed and still reported in the plan when the 5m classes are
    # suspended: it is the §3 record of what the session was doing at the arm, and reading
    # a plan without it would be strictly worse.
    arm_5m = five_min_armed() if five_min is None else bool(five_min)
    if want_leg is not None and trend_dir is not None and arm_5m:
        if trend_dir != want_leg:
            armed.insert(0, "fvg_negation_reversal")       # thesis reverses the trend
        else:
            armed.insert(0, "fvg_return_continuation")     # thesis rides it

    # `valid_while` carries the FALSIFIERS only, and nothing acts on them (see
    # Executor._death). EXHAUSTION IS DROPPED ENTIRELY: reaching the DOL *is* the
    # exhaustion, and L1 says so itself — every recorded thesis sets `exhausted_if` to
    # exactly the DOL price, 08-25's rationale spelling it out as "london(cur)_low, the
    # DOL itself — reaching it exhausts the DOWN thesis by delivering the immediate
    # draw". Carrying it as a second predicate only duplicated the DOL touch.
    #
    # Dropped HERE and not in L1's schema on purpose: `thesis_key()` hashes the schema,
    # so changing the contract would invalidate every recorded thesis and force a
    # re-seed of every date. Ignoring the field downstream costs nothing.
    valid_while = list(thesis.get("falsified_if") or [])

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
