"""The NEUTRAL tie-break: turn a NEUTRAL thesis into a directional one so every armed day
trades (`ACT_THESIS_TIEBREAK`, default ON; `0`/`false` restores the dark NEUTRAL day).

Applied by the Analyzer AFTER the thesis backend (so after the thesis cache): a recorded
NEUTRAL is tie-broken on replay without a re-seed, and this rule can change without
invalidating any recording. It never touches a thesis that already stands.

Rules, first match wins. A rule only decides when its side has a DOL menu row. An SMT is
never read on its own: what decides is what the LAGGER (the swept asset) did at the level
afterwards on its HTF closes.

1. **Accepted week-tier SMT -> continuation.** The freshest week-tier SMT swept in the
   current session (since the prior 18:00 ET) whose lagger then CLOSED BEYOND the level on
   both 1h and 4h. The divergence was ignored: the move continues through it. A bearish
   SMT at a week high that was accepted -> UP; a bullish one at a week low -> DOWN. Nested
   /suppressed SMTs count here -- suppression is a P2 scoring rule, and being ignored by
   the lagger is exactly the information this rule reads. 2026-08-18: bullish SMT at the
   08-11 low (prev1_week_low), MES closed the 01:00 1h bar below it -> DOWN (the day kept
   falling). 2026-09-21 and 2026-08-03: bearish SMTs at prior week highs accepted by the
   lagger inside a multi-week uptrend -> UP.
2. **Rejected day-extreme SMT -> the SMT's direction.** The freshest non-exhausted SMT at
   the running day extreme on both assets (`promoted_from` set) whose lagger CLOSED BACK
   inside on the 1h: above -> DOWN, below -> UP. 2026-07-15: MES swept the london high
   (the day high) at 08:43, MNQ failed to confirm, and the 09:00 1h bar closed MES back
   below -> DOWN (the day fell 374 pts; rules 3 and 4 both call UP there). 07-15's own
   week-tier acceptance (prev2_week_high) is from the PREVIOUS session, so rule 1 skips it.
   Deliberately limited to the day extreme: on 2026-07-02 a rejected week-LOW SMT (not a
   day extreme) pointed UP and the day fell 614 pts.
3. **Net-score sign** over the thesis's own evidence plus code-injected items (the number
   the validator judges bias against). Inside the +/-1.0 dead band this is the call the
   model declined to make.
4. **Nearer draw.** The side whose menu D1 sits closer (`dist_ratio`), i.e. the nearest
   liquidity. Only reached on an exact 0 net with no qualifying SMT.

The result carries `thesis_source: "tiebreak"`, `tiebreak_rule` and the model's own
`model_bias`, so trades taken on a tie-broken day can be separated in any study.
"""
from __future__ import annotations

import os

import pandas as pd

_OFF = ("0", "false", "no", "off")


def tiebreak_enabled() -> bool:
    return os.environ.get("ACT_THESIS_TIEBREAK", "").strip().lower() not in _OFF


def _ts(value):
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(ts) else ts


def session_start(now) -> "pd.Timestamp | None":
    """The 18:00 ET session open the facts' `now` belongs to."""
    now = _ts(now)
    if now is None:
        return None
    start = now.normalize() + pd.Timedelta(hours=18)
    return start if start <= now else start - pd.Timedelta(days=1)


def _lagger_close(close_status, c, tf):
    """The swept asset's HTF verdict at the SMT's level: True accept, False reject, None."""
    return (((close_status or {}).get(c.get("swept_ticker")) or {}).get(c.get("level"))
            or {}).get(tf)


def _swept_since(c, start) -> bool:
    ts = _ts(c.get("swept_at"))
    return ts is not None and ts >= start


def _freshest(cands):
    best, best_ts = None, None
    for c in cands:
        ts = _ts(c.get("swept_at"))
        if ts is not None and (best_ts is None or ts > best_ts):
            best, best_ts = c, ts
    return best


def accepted_week_smt_direction(smt_candidates, close_status, now) -> "str | None":
    """Rule 1: continuation through the freshest current-session week-tier SMT whose
    lagger accepted the level on both 1h and 4h."""
    start = session_start(now)
    if start is None:
        return None
    cands = [c for c in smt_candidates or ()
             if isinstance(c, dict) and c.get("tier") == "week"
             and c.get("side") in ("above", "below")
             and _swept_since(c, start)
             and _lagger_close(close_status, c, "1h") is True
             and _lagger_close(close_status, c, "4h") is True]
    best = _freshest(cands)
    if best is None:
        return None
    return "UP" if best["side"] == "above" else "DOWN"


def rejected_day_extreme_smt_direction(smt_candidates, close_status) -> "str | None":
    """Rule 2: the freshest non-exhausted running-extreme SMT whose lagger's 1h closed
    back inside the level."""
    cands = [c for c in smt_candidates or ()
             if isinstance(c, dict) and c.get("meaningful") and c.get("promoted_from")
             and not c.get("suggested_exhausted") and c.get("side") in ("above", "below")
             and _lagger_close(close_status, c, "1h") is False]
    best = _freshest(cands)
    if best is None:
        return None
    return "DOWN" if best["side"] == "above" else "UP"


def _score_kwargs(facts: dict, magnitude) -> dict:
    dol = ((facts.get("menus") or {}).get("dol") or {})
    kw = {k: facts.get(k) for k in (
        "suppressed_p1_levels", "suppressed_p2_sites", "level_htf_close_status",
        "level_tiers", "smt_candidates", "week_extremes", "now_price", "fvg_zone_meta",
        "mid_reclaim", "htf_reversal", "mid_position", "p1_stale_levels")}
    kw["magnitude"] = magnitude
    kw["dol_available"] = {"UP": bool(dol.get("UP")), "DOWN": bool(dol.get("DOWN"))}
    return kw


def _net_score(thesis: dict, facts: dict, magnitude):
    from agent import run_agent  # noqa: F401  (puts agent/contracts on sys.path)
    from validate_contracts import score_thesis_evidence
    return score_thesis_evidence(thesis.get("evidence") or [],
                                 **_score_kwargs(facts, magnitude))


def tiebreak_thesis(thesis, facts, magnitude=None) -> "dict | None":
    """A directional copy of a NEUTRAL `thesis`, or None to leave it as it is."""
    if not isinstance(thesis, dict) or not isinstance(facts, dict):
        return None
    if str(thesis.get("bias") or "").upper() not in ("", "NEUTRAL"):
        return None
    menus = facts.get("menus") or {}
    dol_menu = menus.get("dol") or {}

    def _d1(direction):
        rows = dol_menu.get(direction) or []
        return rows[0] if rows and rows[0].get("price") is not None else None

    rule, direction, net = None, None, None
    smts, closes = facts.get("smt_candidates"), facts.get("level_htf_close_status")
    for name, pick in (
            ("accepted_week_smt", lambda: accepted_week_smt_direction(smts, closes,
                                                                      facts.get("now"))),
            ("rejected_day_extreme_smt", lambda: rejected_day_extreme_smt_direction(smts,
                                                                                   closes))):
        side = pick()
        if side and _d1(side):
            rule, direction = name, side
            break
    if rule is None:
        scoring = _net_score(thesis, facts, magnitude)
        net = scoring["net_score"]
        side = "UP" if net > 0 else "DOWN" if net < 0 else None
        if side and _d1(side):
            rule, direction = "net_sign", side
    if rule is None:
        near = [(float(_d1(d).get("dist_ratio") or float("inf")), d)
                for d in ("UP", "DOWN") if _d1(d)]
        if near:
            rule, direction = "nearer_dol", min(near)[1]
    if rule is None:
        return None

    d1 = _d1(direction)
    falsifier = next((e["predicate"] for e in (menus.get("predicates") or {}).get(direction) or []
                      if e.get("family") == "falsification" and e.get("recommended")), None)
    out = dict(thesis)
    out.update({
        "bias": direction, "confidence": "LOW",
        "dol": {"level": d1.get("level"), "price": float(d1["price"])},
        "dol_rationale": f"tie-break ({rule}): menu D1 on the {direction} side",
        "falsified_if": [falsifier] if falsifier else [],
        "falsified_if_rationale": "tie-break: the menu's RECOMMENDED falsifier",
        "thesis_source": "tiebreak", "tiebreak_rule": rule,
        "model_bias": thesis.get("bias"), "tiebreak_net_score": net,
    })
    return out
