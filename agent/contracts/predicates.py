"""Predicate language for AI-trader v2 falsification / exhaustion / recall (spec §6).

A closed, composable JSON vocabulary the executor evaluates on bar close — never free
text. Every `*_if` / `recall.events` field in a thesis or trade plan is a predicate (or a
list of predicates, treated as an implicit any_of by the executor). Two entry points:

  - `validate_predicate(pred) -> list[str]`  syntactic check (closed types, closed enums,
    required params, recursion into all_of/any_of). Empty list == valid.
  - `eval_predicate(pred, market_view) -> bool`  deterministic bar-close evaluation over a
    `MarketView` — a thin adapter exposing current price, completed closes per timeframe,
    level sweep/depletion states, and the clock. Pure: same predicate + same view → same bool.

The vocabulary is intentionally small and additive; an unknown `type` is a hard validation
error (never a best-effort interpretation), mirroring the mechanism-enum policy (spec §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

# --------------------------------------------------------------------------- #
# Closed vocabulary                                                            #
# --------------------------------------------------------------------------- #
SIDES = {"above", "below"}
# Timeframes a MarketView can be asked for; closed so a typo is a validation error.
TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h"}

# type -> required param names (composite kinds handled specially).
_ATOM_PARAMS = {
    "price_beyond": ("price", "side"),
    "n_closes_beyond": ("price", "side", "tf", "n"),
    "level_swept": ("name",),
    "level_depleted": ("name",),
    "time_elapsed": ("minutes",),
    "clock_after": ("et_time",),
}
_COMPOSITE = {"all_of", "any_of"}
PREDICATE_TYPES = set(_ATOM_PARAMS) | _COMPOSITE


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_hhmm(v) -> Optional[int]:
    """'HH:MM' -> minutes since midnight, or None if malformed."""
    if not isinstance(v, str) or v.count(":") != 1:
        return None
    h, m = v.split(":")
    if not (h.isdigit() and m.isdigit()):
        return None
    hi, mi = int(h), int(m)
    if not (0 <= hi <= 23 and 0 <= mi <= 59):
        return None
    return hi * 60 + mi


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #
def validate_predicate(pred, where: str = "predicate") -> list[str]:
    """Return a list of human-readable violations ([] == valid).

    Checks: it is a dict with a known `type`; each atom carries its required params
    with the right kinds and closed-enum values; composites carry a non-empty `of`
    list and recurse. Unknown types and unknown enum values are hard errors.
    """
    errs: list[str] = []
    if not isinstance(pred, dict):
        return [f"{where}: predicate must be an object, got {type(pred).__name__}"]
    ptype = pred.get("type")
    if ptype not in PREDICATE_TYPES:
        return [f"{where}: unknown predicate type {ptype!r} "
                f"(allowed: {sorted(PREDICATE_TYPES)})"]

    if ptype in _COMPOSITE:
        of = pred.get("of")
        if not isinstance(of, list) or not of:
            errs.append(f"{where}.{ptype}: 'of' must be a non-empty list of predicates")
        else:
            for i, sub in enumerate(of):
                errs.extend(validate_predicate(sub, f"{where}.{ptype}[{i}]"))
        return errs

    for p in _ATOM_PARAMS[ptype]:
        if p not in pred:
            errs.append(f"{where}.{ptype}: missing required param '{p}'")
    if errs:
        return errs

    if ptype in ("price_beyond", "n_closes_beyond"):
        if not _is_number(pred.get("price")):
            errs.append(f"{where}.{ptype}: 'price' must be a number")
        if pred.get("side") not in SIDES:
            errs.append(f"{where}.{ptype}: side {pred.get('side')!r} not in {sorted(SIDES)}")
    if ptype == "n_closes_beyond":
        if pred.get("tf") not in TIMEFRAMES:
            errs.append(f"{where}.{ptype}: tf {pred.get('tf')!r} not in {sorted(TIMEFRAMES)}")
        n = pred.get("n")
        if not (isinstance(n, int) and not isinstance(n, bool) and n >= 1):
            errs.append(f"{where}.{ptype}: 'n' must be an integer >= 1")
    if ptype in ("level_swept", "level_depleted"):
        if not isinstance(pred.get("name"), str) or not pred.get("name"):
            errs.append(f"{where}.{ptype}: 'name' must be a non-empty string")
    if ptype == "time_elapsed":
        m = pred.get("minutes")
        if not (_is_number(m) and m > 0):
            errs.append(f"{where}.{ptype}: 'minutes' must be a positive number")
    if ptype == "clock_after":
        if _parse_hhmm(pred.get("et_time")) is None:
            errs.append(f"{where}.{ptype}: et_time {pred.get('et_time')!r} is not 'HH:MM'")
    return errs


def validate_predicate_list(preds, where: str) -> list[str]:
    """A `*_if` field is a LIST of predicates (implicit any_of). Validate each."""
    if preds is None:
        return []
    if not isinstance(preds, list):
        return [f"{where}: must be a list of predicates"]
    errs: list[str] = []
    for i, p in enumerate(preds):
        errs.extend(validate_predicate(p, f"{where}[{i}]"))
    return errs


def referenced_levels(pred) -> set[str]:
    """Every level name a predicate (recursively) references — for the semantic check
    that each name exists in the facts sheet."""
    out: set[str] = set()
    if not isinstance(pred, dict):
        return out
    if pred.get("type") in ("level_swept", "level_depleted"):
        name = pred.get("name")
        if isinstance(name, str) and name:
            out.add(name)
    if pred.get("type") in _COMPOSITE:
        for sub in pred.get("of") or []:
            out |= referenced_levels(sub)
    return out


# --------------------------------------------------------------------------- #
# MarketView — the deterministic evaluation adapter                            #
# --------------------------------------------------------------------------- #
@dataclass
class MarketView:
    """A frozen view of market state at one bar close, all a predicate can read.

    - price          current price (last completed close on the base tf).
    - closes_by_tf   tf -> list of completed closes, oldest first, newest last.
    - swept/depleted level names currently in each spent state.
    - now / since    ET timestamps: `now` for clock_after, `now - since` for time_elapsed
                     (since = the monitored decision's issued_at).
    """

    price: Optional[float] = None
    closes_by_tf: dict = field(default_factory=dict)
    swept: set = field(default_factory=set)
    depleted: set = field(default_factory=set)
    now: Optional[pd.Timestamp] = None
    since: Optional[pd.Timestamp] = None

    def closes(self, tf: str) -> list:
        return list(self.closes_by_tf.get(tf, []))

    def level_swept(self, name: str) -> bool:
        return name in self.swept

    def level_depleted(self, name: str) -> bool:
        return name in self.depleted

    def minutes_elapsed(self) -> Optional[float]:
        if self.now is None or self.since is None:
            return None
        return (self.now - self.since).total_seconds() / 60.0

    def clock_minutes(self) -> Optional[int]:
        if self.now is None:
            return None
        return int(self.now.hour) * 60 + int(self.now.minute)

    @classmethod
    def price_only(cls, price: float, *, fill: int = 64) -> "MarketView":
        """A hypothetical view where price sits AT `price` and every completed close on
        every tf equals `price` — used by the cross-level validator to test whether a
        stop/target price would itself trip a thesis price predicate. Level/time/clock
        atoms are inert (unswept, no elapsed time, no clock) so only the price-based
        atoms can fire."""
        return cls(price=price,
                   closes_by_tf={tf: [price] * fill for tf in TIMEFRAMES})


# --------------------------------------------------------------------------- #
# Evaluation                                                                   #
# --------------------------------------------------------------------------- #
def _beyond(value: float, price: float, side: str) -> bool:
    if side == "above":
        return value > price
    if side == "below":
        return value < price
    return False


def eval_predicate(pred, mv: MarketView) -> bool:
    """Evaluate one predicate against a MarketView. Unknown/malformed → False (the
    executor validates predicates up front; a False here can never manufacture a
    spurious falsification). Missing data (too few closes, no clock) → False."""
    if not isinstance(pred, dict):
        return False
    ptype = pred.get("type")

    if ptype == "all_of":
        subs = pred.get("of") or []
        return bool(subs) and all(eval_predicate(s, mv) for s in subs)
    if ptype == "any_of":
        subs = pred.get("of") or []
        return any(eval_predicate(s, mv) for s in subs)

    if ptype == "price_beyond":
        if mv.price is None or not _is_number(pred.get("price")):
            return False
        return _beyond(mv.price, pred["price"], pred.get("side"))

    if ptype == "n_closes_beyond":
        n = pred.get("n")
        price = pred.get("price")
        if not (isinstance(n, int) and n >= 1) or not _is_number(price):
            return False
        closes = mv.closes(pred.get("tf"))
        if len(closes) < n:
            return False
        side = pred.get("side")
        return all(_beyond(c, price, side) for c in closes[-n:])

    if ptype == "level_swept":
        return mv.level_swept(pred.get("name"))
    if ptype == "level_depleted":
        return mv.level_depleted(pred.get("name"))

    if ptype == "time_elapsed":
        elapsed = mv.minutes_elapsed()
        return elapsed is not None and _is_number(pred.get("minutes")) \
            and elapsed >= pred["minutes"]

    if ptype == "clock_after":
        cm = mv.clock_minutes()
        target = _parse_hhmm(pred.get("et_time"))
        return cm is not None and target is not None and cm >= target

    return False


def eval_any(preds, mv: MarketView) -> bool:
    """A `*_if` list fires if ANY of its predicates fire (implicit any_of)."""
    if not preds:
        return False
    return any(eval_predicate(p, mv) for p in preds)
