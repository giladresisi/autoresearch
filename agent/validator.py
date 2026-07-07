"""Deterministic decision validator (GIL-44 Phase 1).

Machine-verifies every LLM decision (the daily-trend + next-move outputs the
decision docs define) before anything consumes it. Three deterministic layers:

  1. Syntactic  — schema completeness, enums, required fields per form.
  2. Arithmetic — integer votes, contribution = weight x vote (D1 halving only),
                  S = sum(contribution); ledger item score = product of its
                  multipliers; N = sum(bull) - sum(bear); direction consistent
                  with the +/-3 thresholds; confidence tier not over-claimed;
                  item types in the closed list; multipliers in their closed sets.
  3. Semantic   — the stated target is a level present in the facts, un-swept /
                  un-depleted, on the correct side of price; resolution triggers
                  on the correct side; checkpoint matches the facts snapshot.

This module is the permanent trust boundary: used post-hoc by the calibration
sweep (calibration/validate_results.py), as validate-and-retry in the Phase-2
bench runner, and as the safety interlock in the Phase-3 orchestrator. It reads
a *structured* decision (dict) and optional *facts* (dict) — never raw markdown;
report parsing lives in validate_results.py.

Failure policy is the caller's:
  - sweep mode      -> flag the run protocol_clean=false (see ValidationResult).
  - runner/prod     -> re-prompt quoting result.messages(); on repeated failure
                       fall back to failsafe_decision() (NEUTRAL / LOW).

All numeric constants here MIRROR the decision docs (single source of truth). A
change to a weight/threshold in the docs is a strategy change and must be
reflected here (and re-swept) — the docs render from these or fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Constants mirrored from the decision docs                                    #
# --------------------------------------------------------------------------- #
DIRECTIONS = {"up", "down", "neutral"}
CONFIDENCES = {"high", "medium", "low"}
REGIMES = {"trend", "range", "hybrid"}
ARM_VALUES = {"yes", "no"}

# daily-trend.md §2 — six slow drivers, fixed weights.
DRIVER_WEIGHTS = {"D1": 3, "D2": 2, "D3": 1, "D4": 2, "D5": 1, "D6": 1}
DRIVER_IDS = list(DRIVER_WEIGHTS)

# next-move.md §2 — the closed list of eligible ledger item types.
ITEM_TYPES = {
    "smt_divergence",
    "sweep",
    "failed_reclaim_daily_mid",
    "displacement_mss",
    "mid_rejection",
    "sustained_acceptance",
    "laggard_fail",
}

# next-move.md §2 — closed multiplier sets.
TIER_WEIGHTS = {3.0, 2.0, 1.5, 1.0}                     # ATH/week, day, fill, session
SESSION_SIDES = {1.5, 1.3, 1.0, 0.7, 0.5, 0.4}          # incl. ticker-scope 0.5 haircut
ALIGNMENTS = {1.0, 0.6, 0.3}                            # with / neutral-both / counter
WHIPSAWS = {1.0, 0.5, 0.0}                              # none / structure / open-window mid

# Direction thresholds (both decisions share the +/-3 gate).
DIR_THRESHOLD = 3.0

# Float tolerances. Reports round to ~3 decimals; votes/contributions are exact.
EPS_EXACT = 1e-6
EPS_SCORE = 0.02


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Violation:
    layer: str        # "syntactic" | "arithmetic" | "semantic"
    code: str         # stable machine code, e.g. "ARI_FRACTIONAL_VOTE"
    message: str      # human-readable, quotable in a re-prompt
    where: str = ""   # locus, e.g. "daily_trend.drivers[D1]"

    def __str__(self) -> str:
        loc = f" ({self.where})" if self.where else ""
        return f"[{self.code}]{loc} {self.message}"


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def protocol_clean(self) -> bool:
        """Sweep-mode flag: True when the decision violated no protocol rule."""
        return self.ok

    def codes(self) -> set[str]:
        return {v.code for v in self.violations}

    def messages(self) -> list[str]:
        return [str(v) for v in self.violations]

    def by_layer(self, layer: str) -> list[Violation]:
        return [v for v in self.violations if v.layer == layer]

    def add(self, layer: str, code: str, message: str, where: str = "") -> None:
        self.violations.append(Violation(layer, code, message, where))


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def validate(decision: dict, facts: Optional[dict] = None) -> ValidationResult:
    """Run the three deterministic layers over a structured decision.

    `decision` is the canonical dict with `daily_trend` and `next_move` blocks.
    `facts` (optional) enables the semantic layer; when absent it is skipped so
    a missing fact sheet can never manufacture a false positive.
    """
    r = ValidationResult()
    if not isinstance(decision, dict):
        r.add("syntactic", "SYN_MISSING_FIELD", "decision must be a dict", "<root>")
        return r

    daily = decision.get("daily_trend")
    nxt = decision.get("next_move")

    _check_syntactic(daily, nxt, r)
    _check_arithmetic(daily, nxt, r)
    if facts is not None:
        _check_semantic(daily, nxt, facts, r)
    return r


def failsafe_decision() -> dict:
    """The runner/production fail-safe: NEUTRAL / LOW, scripts baseline unchanged.

    Emitted when validation cannot be satisfied after the allotted retries. It is
    itself a valid decision (validate(failsafe_decision()).ok is True).
    """
    return {
        "daily_trend": {
            "direction": "neutral",
            "confidence": "low",
            "regime": "range",
            "day_dol": None,
            "weakens_to_neutral_if": "n/a (fail-safe)",
            "flips_if": "n/a (fail-safe)",
            "drivers": [
                {"id": did, "vote": 0, "weight": w, "contribution": 0.0}
                for did, w in DRIVER_WEIGHTS.items()
            ],
            "S": 0.0,
        },
        "next_move": {
            "direction": "neutral",
            "confidence": "low",
            "move_target": None,
            "flip_trigger": "n/a (fail-safe)",
            "flipped_target": None,
            "arm_entry_confirmation": "no",
            "resolution": {
                "long_if": {"condition": "n/a", "price": None, "target": None},
                "short_if": {"condition": "n/a", "price": None, "target": None},
            },
            "bull_ledger": [],
            "bear_ledger": [],
            "N": 0.0,
            "vetoes": [],
        },
    }


# --------------------------------------------------------------------------- #
# Layer 1 — Syntactic                                                          #
# --------------------------------------------------------------------------- #
def _check_syntactic(daily: Any, nxt: Any, r: ValidationResult) -> None:
    if not isinstance(daily, dict):
        r.add("syntactic", "SYN_MISSING_FIELD", "missing daily_trend block", "daily_trend")
    else:
        _syn_daily(daily, r)
    if not isinstance(nxt, dict):
        r.add("syntactic", "SYN_MISSING_FIELD", "missing next_move block", "next_move")
    else:
        _syn_next(nxt, r)


def _syn_daily(daily: dict, r: ValidationResult) -> None:
    for f in ("direction", "confidence", "regime", "weakens_to_neutral_if", "flips_if",
              "drivers"):
        if daily.get(f) in (None, ""):
            r.add("syntactic", "SYN_MISSING_FIELD", f"daily_trend missing '{f}'",
                  "daily_trend")

    _enum(daily.get("direction"), DIRECTIONS, "daily_trend.direction", r)
    _enum(daily.get("confidence"), CONFIDENCES, "daily_trend.confidence", r)
    _enum(daily.get("regime"), REGIMES, "daily_trend.regime", r)

    # Directional daily-trend requires a day draw (day_dol).
    if daily.get("direction") in ("up", "down") and not daily.get("day_dol"):
        r.add("syntactic", "SYN_DIRECTIONAL_MISSING_TARGET",
              "directional daily_trend requires day_dol", "daily_trend.day_dol")


def _syn_next(nxt: dict, r: ValidationResult) -> None:
    for f in ("direction", "confidence", "arm_entry_confirmation"):
        if nxt.get(f) in (None, ""):
            r.add("syntactic", "SYN_MISSING_FIELD", f"next_move missing '{f}'", "next_move")

    _enum(nxt.get("direction"), DIRECTIONS, "next_move.direction", r)
    _enum(nxt.get("confidence"), CONFIDENCES, "next_move.confidence", r)
    _enum(nxt.get("arm_entry_confirmation"), ARM_VALUES,
          "next_move.arm_entry_confirmation", r)

    direction = nxt.get("direction")
    if direction == "neutral":
        res = nxt.get("resolution")
        if not isinstance(res, dict) or not res.get("long_if") or not res.get("short_if"):
            r.add("syntactic", "SYN_NEUTRAL_MISSING_RESOLUTION",
                  "neutral next_move requires both long_if and short_if resolution triggers",
                  "next_move.resolution")
    elif direction in ("up", "down"):
        if not nxt.get("move_target"):
            r.add("syntactic", "SYN_DIRECTIONAL_MISSING_TARGET",
                  "directional next_move requires move_target", "next_move.move_target")


def _enum(value: Any, allowed: set[str], where: str, r: ValidationResult) -> None:
    if value is not None and value not in allowed:
        r.add("syntactic", "SYN_BAD_ENUM",
              f"'{value}' not in {sorted(allowed)}", where)


# --------------------------------------------------------------------------- #
# Layer 2 — Arithmetic / protocol                                             #
# --------------------------------------------------------------------------- #
def _check_arithmetic(daily: Any, nxt: Any, r: ValidationResult) -> None:
    if isinstance(daily, dict):
        _ari_daily(daily, r)
    if isinstance(nxt, dict):
        _ari_next(nxt, r)


def _ari_daily(daily: dict, r: ValidationResult) -> None:
    drivers = daily.get("drivers")
    if not isinstance(drivers, list) or not drivers:
        return  # syntactic layer already flagged the missing table

    total = 0.0
    opposers_up = opposers_down = 0
    for drv in drivers:
        did = drv.get("id", "?")
        where = f"daily_trend.drivers[{did}]"
        vote = drv.get("vote")
        weight = drv.get("weight")
        contribution = drv.get("contribution")

        # Fixed weight per driver (mirror the doc).
        if did in DRIVER_WEIGHTS and weight != DRIVER_WEIGHTS[did]:
            r.add("arithmetic", "ARI_BAD_CONTRIBUTION",
                  f"{did} weight {weight} != doc weight {DRIVER_WEIGHTS[did]}", where)
            weight = DRIVER_WEIGHTS[did]

        # Votes are integers in {-1, 0, +1}. The only sanctioned fraction is
        # D1's halved *contribution* — never a fractional vote.
        if not _is_int_vote(vote):
            r.add("arithmetic", "ARI_FRACTIONAL_VOTE",
                  f"{did} vote {vote!r} is not an integer in {{-1, 0, +1}}", where)
        elif isinstance(contribution, (int, float)) and isinstance(weight, (int, float)):
            base = weight * vote
            halved_ok = (did == "D1")
            if not (_close(contribution, base, EPS_EXACT)
                    or (halved_ok and _close(contribution, base * 0.5, EPS_EXACT))):
                allowed = f"{base}" + (f" or {base * 0.5} (D1 halving)" if halved_ok else "")
                r.add("arithmetic", "ARI_BAD_CONTRIBUTION",
                      f"{did} contribution {contribution} != weight x vote ({allowed})", where)

        if isinstance(contribution, (int, float)):
            total += contribution
        # Opposition tracking uses the sign of the vote for weight->=2 drivers.
        if isinstance(vote, (int, float)) and isinstance(weight, (int, float)) and weight >= 2:
            if vote > 0:
                opposers_down += 1   # a +vote opposes a DOWN call
            elif vote < 0:
                opposers_up += 1     # a -vote opposes an UP call

    _check_correlation_audit(drivers, r)

    # S = sum of contributions.
    declared_S = daily.get("S")
    if isinstance(declared_S, (int, float)) and not _close(declared_S, total, EPS_SCORE):
        r.add("arithmetic", "ARI_S_MISMATCH",
              f"declared S={declared_S} != sum(contributions)={round(total, 3)}",
              "daily_trend.S")
    S = declared_S if isinstance(declared_S, (int, float)) else total

    # Direction consistent with the +/-3 gate.
    expected = _direction_from_score(S)
    if daily.get("direction") in DIRECTIONS and daily["direction"] != expected:
        r.add("arithmetic", "ARI_DAILY_DIRECTION",
              f"direction '{daily['direction']}' inconsistent with S={round(S, 3)} "
              f"(expected '{expected}')", "daily_trend.direction")

    # Confidence must not be over-claimed (down-tiering is always allowed).
    opposers = opposers_up if daily.get("direction") == "up" else \
        opposers_down if daily.get("direction") == "down" else 0
    ceiling = _daily_confidence_ceiling(S, opposers)
    _check_conf_overclaim(daily.get("confidence"), ceiling,
                          "ARI_DAILY_CONFIDENCE", "daily_trend.confidence", S, r)


def _check_correlation_audit(drivers: list, r: ValidationResult) -> None:
    """Mechanical pattern-vs-event double-count check (daily-trend.md §2).

    An event supports ONE driver at full weight; a later appearance must be x0.5.
    Fires only when the same event id is claimed at FULL weight (non-zero
    contribution, not marked discounted) by two or more drivers. Runs only when
    drivers explicitly declare their `events` — absent that, it is skipped (never
    a false positive on prose-only reports).
    """
    claimants: dict[str, list[str]] = {}
    for drv in drivers:
        events = drv.get("events")
        if not events:
            continue
        contribution = drv.get("contribution")
        if isinstance(contribution, (int, float)) and abs(contribution) <= EPS_EXACT:
            continue  # a driver voting 0 consumes nothing
        discounted = set(drv.get("discounted_events") or [])
        did = drv.get("id", "?")
        for ev in events:
            if ev in discounted:
                continue  # this appearance already took its second-appearance haircut
            claimants.setdefault(ev, []).append(did)

    for ev, ids in claimants.items():
        if len(ids) >= 2:
            r.add("arithmetic", "ARI_PATTERN_EVENT_DOUBLE_COUNT",
                  f"event '{ev}' claimed at full weight by {ids} — the second "
                  f"appearance must be discounted x0.5 (correlation audit)",
                  "daily_trend.drivers")


def _ari_next(nxt: dict, r: ValidationResult) -> None:
    bull = nxt.get("bull_ledger") or []
    bear = nxt.get("bear_ledger") or []
    have_ledgers = bool(bull) or bool(bear)

    bull_sum = _score_ledger(bull, "bull", r)
    bear_sum = _score_ledger(bear, "bear", r)

    # N-mismatch is only checkable when the ledger components were supplied.
    declared_N = nxt.get("N")
    if have_ledgers and isinstance(declared_N, (int, float)):
        real_N = bull_sum - bear_sum
        if not _close(declared_N, real_N, EPS_SCORE):
            r.add("arithmetic", "ARI_N_MISMATCH",
                  f"declared N={declared_N} != sum(bull)-sum(bear)={round(real_N, 3)}",
                  "next_move.N")

    # Determine N for direction/confidence: declared wins; else recompute from
    # ledgers; else (no data) skip these checks — never a false positive.
    if isinstance(declared_N, (int, float)):
        N = declared_N
    elif have_ledgers:
        N = bull_sum - bear_sum
    else:
        return

    expected = _direction_from_score(N)
    if nxt.get("direction") in DIRECTIONS and nxt["direction"] != expected:
        r.add("arithmetic", "ARI_NEXT_DIRECTION",
              f"direction '{nxt['direction']}' inconsistent with N={round(N, 3)} "
              f"(expected '{expected}')", "next_move.direction")

    ceiling = _next_confidence_ceiling(N, bull_sum, bear_sum, nxt.get("vetoes"))
    _check_conf_overclaim(nxt.get("confidence"), ceiling,
                          "ARI_NEXT_CONFIDENCE", "next_move.confidence", N, r)


def _score_ledger(items: list, side: str, r: ValidationResult) -> float:
    total = 0.0
    for i, item in enumerate(items):
        where = f"next_move.{side}_ledger[{i}]"
        itype = item.get("type")
        if itype is not None and itype not in ITEM_TYPES:
            r.add("arithmetic", "ARI_INVENTED_ITEM_TYPE",
                  f"item type '{itype}' not in the closed list {sorted(ITEM_TYPES)}", where)

        tier = item.get("tier")
        sess = item.get("session_side")
        align = item.get("alignment")
        fresh = item.get("freshness")
        whip = item.get("whipsaw", 1.0)
        score = item.get("score")

        _check_multiplier(tier, TIER_WEIGHTS, "tier", where, r)
        _check_multiplier(sess, SESSION_SIDES, "session_side", where, r)
        _check_multiplier(align, ALIGNMENTS, "alignment", where, r)
        _check_multiplier(whip, WHIPSAWS, "whipsaw", where, r)
        if isinstance(fresh, (int, float)) and not (0.0 < fresh <= 1.0 + EPS_EXACT):
            r.add("arithmetic", "ARI_BAD_MULTIPLIER",
                  f"freshness {fresh} outside (0, 1]", where)

        mults = [tier, sess, align, fresh, whip]
        if all(isinstance(m, (int, float)) for m in mults) and isinstance(score, (int, float)):
            product = 1.0
            for m in mults:
                product *= m
            if not _close(score, product, EPS_SCORE):
                r.add("arithmetic", "ARI_ITEM_SCORE",
                      f"score {score} != product of multipliers {round(product, 4)}", where)
        if isinstance(score, (int, float)):
            total += score
    return total


def _check_multiplier(value: Any, allowed: set, name: str, where: str,
                      r: ValidationResult) -> None:
    if isinstance(value, (int, float)) and not any(_close(value, a, EPS_EXACT) for a in allowed):
        r.add("arithmetic", "ARI_BAD_MULTIPLIER",
              f"{name} {value} not in closed set {sorted(allowed)}", where)


# --------------------------------------------------------------------------- #
# Layer 3 — Semantic sanity                                                    #
# --------------------------------------------------------------------------- #
def _check_semantic(daily: Any, nxt: Any, facts: dict, r: ValidationResult) -> None:
    now_price = facts.get("now_price")
    levels = facts.get("levels") or {}

    # Checkpoint used must match the facts snapshot (S0).
    if isinstance(daily, dict):
        cp_decision = daily.get("checkpoint")
        cp_facts = facts.get("checkpoint")
        if cp_decision and cp_facts and cp_decision.strip() != cp_facts.strip():
            r.add("semantic", "SEM_CHECKPOINT_MISMATCH",
                  f"daily_trend checkpoint '{cp_decision}' != facts checkpoint '{cp_facts}'",
                  "daily_trend.checkpoint")

    if not isinstance(nxt, dict):
        return

    direction = nxt.get("direction")
    target = nxt.get("move_target")
    if direction in ("up", "down") and isinstance(target, dict):
        _check_target(target, direction, now_price, levels, r,
                      where="next_move.move_target")

    # Neutral resolution triggers: long above price, short below price.
    if direction == "neutral" and isinstance(now_price, (int, float)):
        res = nxt.get("resolution") or {}
        long_p = (res.get("long_if") or {}).get("price")
        short_p = (res.get("short_if") or {}).get("price")
        if isinstance(long_p, (int, float)) and long_p < now_price - EPS_EXACT:
            r.add("semantic", "SEM_RESOLUTION_WRONG_SIDE",
                  f"long_if price {long_p} is below current price {now_price}",
                  "next_move.resolution.long_if")
        if isinstance(short_p, (int, float)) and short_p > now_price + EPS_EXACT:
            r.add("semantic", "SEM_RESOLUTION_WRONG_SIDE",
                  f"short_if price {short_p} is above current price {now_price}",
                  "next_move.resolution.short_if")


def _check_target(target: dict, direction: str, now_price: Any, levels: dict,
                  r: ValidationResult, where: str) -> None:
    name = target.get("level")
    price = target.get("price")

    lvl = levels.get(name) if name is not None else None
    if name is not None and lvl is None:
        r.add("semantic", "SEM_TARGET_NOT_IN_FACTS",
              f"target level '{name}' is not present in the facts", where)

    if isinstance(now_price, (int, float)) and isinstance(price, (int, float)):
        if direction == "down" and price > now_price + EPS_EXACT:
            r.add("semantic", "SEM_TARGET_WRONG_SIDE",
                  f"down move but target {price} is above current price {now_price}", where)
        elif direction == "up" and price < now_price - EPS_EXACT:
            r.add("semantic", "SEM_TARGET_WRONG_SIDE",
                  f"up move but target {price} is below current price {now_price}", where)

    if isinstance(lvl, dict) and (lvl.get("swept") and lvl.get("depleted")):
        r.add("semantic", "SEM_TARGET_SWEPT_DEPLETED",
              f"target level '{name}' is already swept and depleted (spent pool)", where)


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #
def _is_int_vote(vote: Any) -> bool:
    if not isinstance(vote, (int, float)) or isinstance(vote, bool):
        return False
    return _close(vote, round(vote), EPS_EXACT) and int(round(vote)) in (-1, 0, 1)


def _close(a: float, b: float, eps: float) -> bool:
    return abs(a - b) <= eps


def _direction_from_score(score: float) -> str:
    if score >= DIR_THRESHOLD - EPS_EXACT:
        return "up"
    if score <= -DIR_THRESHOLD + EPS_EXACT:
        return "down"
    return "neutral"


def _daily_confidence_ceiling(S: float, opposers: int) -> str:
    absS = abs(S)
    if absS >= 6 - EPS_EXACT and opposers == 0:
        return "high"
    if absS >= 3 - EPS_EXACT and opposers <= 1:
        return "medium"
    return "low"


def _next_confidence_ceiling(N: float, bull: float, bear: float,
                             vetoes: Optional[list]) -> str:
    absN = abs(N)
    winning, losing = (bull, bear) if bull >= bear else (bear, bull)

    cap_low = cap_medium = False
    for v in vetoes or []:
        if v.get("triggered"):
            eff = v.get("effect")
            if eff == "cap_to_low":
                cap_low = True
            elif eff == "cap_to_medium":
                cap_medium = True

    if absN >= 6 - EPS_EXACT:
        ceiling = "high"
    elif absN >= 3 - EPS_EXACT:
        ceiling = "medium"
    else:
        ceiling = "low"

    # high additionally requires the losing ledger <= half the winning.
    if ceiling == "high" and losing > 0.5 * winning + EPS_EXACT:
        ceiling = "medium"
    if cap_low:
        ceiling = "low"
    if cap_medium and ceiling == "high":
        ceiling = "medium"
    return ceiling


def _check_conf_overclaim(claimed: Any, ceiling: str, code: str, where: str,
                          score: float, r: ValidationResult) -> None:
    if claimed in CONFIDENCES and _CONF_RANK[claimed] > _CONF_RANK[ceiling]:
        r.add("arithmetic", code,
              f"confidence '{claimed}' over-claims the ceiling '{ceiling}' "
              f"(score={round(score, 3)})", where)
