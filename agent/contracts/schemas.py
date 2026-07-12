"""Thesis / trade-plan contracts (spec §2.1, §2.2, §7).

Dataclasses + structured-output JSON schemas for the two AI decision levels. The
dataclasses are thin typed views over the JSON blocks the model returns (parsed via
`Thesis.from_dict` / `TradePlan.from_dict`); the JSON schemas mirror them for the
backend's strict structured-output mode. Level INTERNALS (how the model reasons, KB
content, calibration) are out of scope — this is the field-level shape only.

The mechanism enums (spec §7) are the closed executor toolbox the plan may arm. An
unknown `kind` is a validation reject, never a best-effort interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Enums (closed per release)                                                    #
# --------------------------------------------------------------------------- #
BIASES = {"UP", "DOWN", "NEUTRAL"}
DAILY_REGIMES = {"TREND", "RANGE", "HYBRID"}
CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}
VERDICTS = {"SETUP", "WAIT"}
DIRECTIONS = {"LONG", "SHORT"}

# spec §7 — entry mechanisms (map to existing code paths via the Phase-4 adapter).
ENTRY_MECHANISMS = {
    "confirmation_bar",
    "fvg_retrace",
    "level_break_stop_entry",
    "market_on_condition",
}
# spec §7 — management / exit mechanisms.
MGMT_MECHANISMS = {
    "take_profit",
    "move_stop",
    "raise_to_breakeven",
    "trail",
    "market_close_on",
}
# spec §2.2 — mandatory on_dol_falsified actions.
DOL_FALSIFIED_ACTIONS = {"MARKET_CLOSE", "TIGHTEN_STOP"}

# plan 11 Phase 4 — failsafe recall max-age. A failsafed L1 call must schedule its own
# retry (event-driven design: a failsafe leaves the system blind until it re-asks). The
# 07-02 bench run showed a failsafed 18:00 call with max_age_min=0 stayed blind all
# session. Both the bench engine (_run_waiting) and the production TradeDirector
# (_ttl_expired / _maybe_recall_l1) already honor recall.max_age_min, so this is a
# data-only change in the failsafe factory.
FAILSAFE_RECALL_MAX_AGE_MIN = 60

# plan 12 Fix 1 — code-enforced default recall max-age for EVERY non-standing (valid
# low-conf / NEUTRAL / waiting) thesis, not just the failsafe. Spec §2.1 gives the model a
# `recall.max_age_min`, but a VALID low-conf thesis whose model-authored max_age is 0/absent
# and whose recall events never fire otherwise waits forever (07-02 diag: th_04 waited 20.5h
# to session end). The default (same value as the failsafe) is the cap: a missing / zero /
# absurdly-large declared max_age is clamped DOWN to it, while a smaller declared value is
# respected (the model may ask to be re-called sooner). Enforced in code, never trusted from
# the model output.
DEFAULT_LOWCONF_RECALL_MAX_AGE_MIN = 60


def effective_recall_max_age(declared, default: float = DEFAULT_LOWCONF_RECALL_MAX_AGE_MIN):
    """The code-enforced effective recall max-age (minutes) for a non-standing thesis:
    `min(declared, default)` when `declared` is a positive number, else `default`. A
    missing / zero / negative / non-numeric declared value falls back to the default; a
    declared value larger than the default is clamped down to it; a smaller declared value
    is respected. `default` doubles as the cap (spec §2.1; plan 12 Fix 1)."""
    if not isinstance(declared, (int, float)) or isinstance(declared, bool) or declared <= 0:
        return default
    return min(declared, default)


# --------------------------------------------------------------------------- #
# Dataclasses                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Thesis:
    """Level-1 standing decision (spec §2.1). `confidence` here is the model's
    self-report — audit-only; the executor gates on the code-derived confidence
    (spec §8)."""

    thesis_id: Optional[str] = None
    issued_at: Optional[str] = None
    facts_hash: Optional[str] = None
    bias: Optional[str] = None
    regime: Optional[str] = None
    dol: Optional[dict] = None                       # {"level": str, "price": float}
    falsified_if: list = field(default_factory=list)  # [predicate]
    exhausted_if: list = field(default_factory=list)  # [predicate]
    confidence: Optional[str] = None
    recall: Optional[dict] = None                    # {"events": [pred], "max_age_min": int}
    reasoning: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Thesis":
        d = d or {}
        return cls(
            thesis_id=d.get("thesis_id"), issued_at=d.get("issued_at"),
            facts_hash=d.get("facts_hash"), bias=d.get("bias"), regime=d.get("regime"),
            dol=d.get("dol"), falsified_if=list(d.get("falsified_if") or []),
            exhausted_if=list(d.get("exhausted_if") or []),
            confidence=d.get("confidence"), recall=d.get("recall"),
            reasoning=d.get("reasoning"))

    def to_dict(self) -> dict:
        return {
            "thesis_id": self.thesis_id, "issued_at": self.issued_at,
            "facts_hash": self.facts_hash, "bias": self.bias, "regime": self.regime,
            "dol": self.dol, "falsified_if": self.falsified_if,
            "exhausted_if": self.exhausted_if, "confidence": self.confidence,
            "recall": self.recall, "reasoning": self.reasoning,
        }

    def is_directional(self) -> bool:
        return self.bias in ("UP", "DOWN")


@dataclass
class TradePlan:
    """Level-2 decision (spec §2.2): a SETUP or a WAIT, parented to a thesis."""

    plan_id: Optional[str] = None
    thesis_id: Optional[str] = None
    verdict: Optional[str] = None
    entry: Optional[dict] = None                     # {"mechanisms": [...], "direction": ...}
    stop: Optional[dict] = None                      # {"price": float}
    breakeven: Optional[dict] = None
    exit: Optional[dict] = None                      # {"target": {...}, "management": [...]}
    setup_falsified_if: list = field(default_factory=list)
    setup_exhausted_if: list = field(default_factory=list)
    on_dol_falsified: Optional[dict] = None          # mandatory on SETUP
    recall: Optional[dict] = None                    # WAIT only
    reasoning: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "TradePlan":
        d = d or {}
        return cls(
            plan_id=d.get("plan_id"), thesis_id=d.get("thesis_id"),
            verdict=d.get("verdict"), entry=d.get("entry"), stop=d.get("stop"),
            breakeven=d.get("breakeven"), exit=d.get("exit"),
            setup_falsified_if=list(d.get("setup_falsified_if") or []),
            setup_exhausted_if=list(d.get("setup_exhausted_if") or []),
            on_dol_falsified=d.get("on_dol_falsified"), recall=d.get("recall"),
            reasoning=d.get("reasoning"))

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "thesis_id": self.thesis_id,
            "verdict": self.verdict, "entry": self.entry, "stop": self.stop,
            "breakeven": self.breakeven, "exit": self.exit,
            "setup_falsified_if": self.setup_falsified_if,
            "setup_exhausted_if": self.setup_exhausted_if,
            "on_dol_falsified": self.on_dol_falsified, "recall": self.recall,
            "reasoning": self.reasoning,
        }

    def is_setup(self) -> bool:
        return self.verdict == "SETUP"

    def direction(self) -> Optional[str]:
        return (self.entry or {}).get("direction")


# --------------------------------------------------------------------------- #
# Structured-output JSON schemas (mirror the validator's canonical shape)       #
# --------------------------------------------------------------------------- #
def failsafe_thesis() -> dict:
    """The L1 fail-safe: a NEUTRAL / LOW standing thesis (no directional commitment, no
    entries follow). Itself a valid thesis (validate_thesis(failsafe_thesis()).ok)."""
    return {
        "bias": "NEUTRAL", "regime": "RANGE", "dol": None,
        "falsified_if": [], "exhausted_if": [], "confidence": "LOW",
        "recall": {"events": [], "max_age_min": FAILSAFE_RECALL_MAX_AGE_MIN},
        "reasoning": "fail-safe neutral/low thesis (offline/failsafe)",
    }


def failsafe_plan() -> dict:
    """The L2 fail-safe: a WAIT (no setup armed). Its recall re-asks after a short TTL."""
    return {
        "verdict": "WAIT", "entry": None, "stop": None, "breakeven": None, "exit": None,
        "setup_falsified_if": [], "setup_exhausted_if": [], "on_dol_falsified": None,
        "recall": {"events": [{"type": "time_elapsed", "minutes": 30}], "max_age_min": 30},
        "reasoning": "fail-safe WAIT (offline/failsafe)",
    }


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


# --------------------------------------------------------------------------- #
# Strict predicate schema fragment (spec §6; plan 11 Phase 1)                   #
# --------------------------------------------------------------------------- #
# The predicate vocabulary is enforced AT GENERATION TIME: each `*_if` / recall.events
# field is an `anyOf` of the six atoms (each with `type` as a const + exactly its
# required params) plus the two composites (`all_of` / `any_of`) whose items are ATOMS
# ONLY. Composition depth is capped at 1 (a composite may not nest a composite) — this
# avoids the recursive schemas that structured-output backends handle poorly. The
# contracts evaluator (`predicates.eval_predicate`) still supports arbitrary nesting, so
# depth-1 is a SCHEMA restriction only (documented in agent-optimizations.md §6). An
# unknown predicate `type` is now impossible to generate, killing the SYN_BAD_PREDICATE
# → failsafe class the bench saw on every date.
_SIDE = {"enum": ["above", "below"]}
_TF = {"enum": ["1m", "5m", "15m", "1h", "4h"]}


def _atom(ptype: str, props: dict) -> dict:
    """One atom shape: `type` const + exactly `props` (all required, no extras)."""
    properties = {"type": {"const": ptype}, **props}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


# The six atoms (mirror predicates._ATOM_PARAMS exactly). NOTE: numeric bound keywords
# (minimum / exclusiveMinimum) and array minItems are deliberately OMITTED — the Anthropic
# structured-output backend rejects `minimum` on integers, and these bounds are enforced by
# the deterministic predicate validator anyway (`predicates.validate_predicate`: n >= 1,
# minutes > 0, non-empty `of`) — "constraints live in code, not the schema" (spec §5).
#
# The atoms live in `$defs` and every predicate field references `#/$defs/predicate` — WITHOUT
# $defs the union is inlined into each of falsified_if / exhausted_if / recall.events (and the
# composites re-inline all six atoms), and the Anthropic constrained-decoding grammar compiler
# rejects the result as "grammar too large". $ref keeps the compiled grammar small.
_ATOM_PROPS = {
    "price_beyond": {"price": {"type": "number"}, "side": _SIDE},
    "n_closes_beyond": {"price": {"type": "number"}, "side": _SIDE, "tf": _TF,
                        "n": {"type": "integer"}},
    "level_swept": {"name": {"type": "string"}},
    "level_depleted": {"name": {"type": "string"}},
    "time_elapsed": {"minutes": {"type": "number"}},
    "clock_after": {"et_time": {"type": "string"}},
}
_ATOM_DEF_NAMES = {t: f"pred_{t}" for t in _ATOM_PROPS}
_ATOM_DEFS = {_ATOM_DEF_NAMES[t]: _atom(t, props) for t, props in _ATOM_PROPS.items()}
_ATOM_REFS = [{"$ref": f"#/$defs/{_ATOM_DEF_NAMES[t]}"} for t in _ATOM_PROPS]


def _composite_def(ptype: str) -> dict:
    """A depth-1 composite: `type` const + `of` array of ATOMS ONLY (atom $refs — no nested
    composite)."""
    return {
        "type": "object",
        "properties": {
            "type": {"const": ptype},
            "of": {"type": "array", "items": {"anyOf": list(_ATOM_REFS)}},
        },
        "required": ["type", "of"],
        "additionalProperties": False,
    }


# The predicate def: any atom, or a depth-1 all_of/any_of over atoms.
_PREDICATE_DEF = {"anyOf": [*_ATOM_REFS, _composite_def("all_of"), _composite_def("any_of")]}
# All predicate $defs to embed at each top-level schema's root ($ref resolves against it).
_PRED_DEFS = {**_ATOM_DEFS, "predicate": _PREDICATE_DEF}

# In a FIELD position: reference the shared def (keeps the compiled grammar small).
_PREDICATE = {"$ref": "#/$defs/predicate"}
_PRED_LIST = {"type": "array", "items": _PREDICATE}

# A SELF-CONTAINED predicate schema (carries its own $defs) for standalone jsonschema
# validation + tests — `validate(inst, predicate_schema())`.
def predicate_schema() -> dict:
    return {"$defs": dict(_PRED_DEFS), **_PREDICATE_DEF}

_DOL = _nullable({
    "type": "object",
    "properties": {"level": {"type": "string"}, "price": {"type": "number"}},
    "required": ["level", "price"],
    "additionalProperties": False,
})

_RECALL = {
    "type": "object",
    "properties": {"events": _PRED_LIST, "max_age_min": {"type": "number"}},
    "required": ["events", "max_age_min"],
    "additionalProperties": False,
}

THESIS_SCHEMA = {
    "type": "object",
    "$defs": dict(_PRED_DEFS),
    "properties": {
        "bias": {"enum": sorted(BIASES)},
        "regime": {"enum": sorted(DAILY_REGIMES)},
        "dol": _DOL,
        "falsified_if": _PRED_LIST,
        "exhausted_if": _PRED_LIST,
        "confidence": {"enum": sorted(CONFIDENCES)},
        "recall": _RECALL,
        "reasoning": {"type": "string"},
    },
    "required": ["bias", "regime", "dol", "falsified_if", "exhausted_if",
                 "confidence", "recall", "reasoning"],
    "additionalProperties": False,
}

_MECHANISM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "params": {"type": "object"},
        "valid_while": _PRED_LIST,
    },
    "required": ["kind", "params", "valid_while"],
    "additionalProperties": False,
}

_MGMT = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "params": {"type": "object"},
        "when": _PRED_LIST,
    },
    "required": ["kind", "params", "when"],
    "additionalProperties": False,
}

TRADE_PLAN_SCHEMA = {
    "type": "object",
    "$defs": dict(_PRED_DEFS),
    "properties": {
        "verdict": {"enum": sorted(VERDICTS)},
        "entry": _nullable({
            "type": "object",
            "properties": {
                "mechanisms": {"type": "array", "items": _MECHANISM},
                "direction": {"enum": sorted(DIRECTIONS)},
            },
            "required": ["mechanisms", "direction"],
            "additionalProperties": False,
        }),
        "stop": _nullable({
            "type": "object",
            "properties": {"price": {"type": "number"}},
            "required": ["price"], "additionalProperties": False,
        }),
        "breakeven": _nullable({
            "type": "object",
            "properties": {"raise_to_be_if": _PRED_LIST},
            "required": ["raise_to_be_if"], "additionalProperties": False,
        }),
        "exit": _nullable({
            "type": "object",
            "properties": {
                "target": _nullable({
                    "type": "object",
                    "properties": {"level": {"type": "string"}, "price": {"type": "number"}},
                    "required": ["level", "price"], "additionalProperties": False,
                }),
                "management": {"type": "array", "items": _MGMT},
            },
            "required": ["target", "management"], "additionalProperties": False,
        }),
        "setup_falsified_if": _PRED_LIST,
        "setup_exhausted_if": _PRED_LIST,
        "on_dol_falsified": _nullable({
            "type": "object",
            "properties": {"action": {"enum": sorted(DOL_FALSIFIED_ACTIONS)},
                           "params": {"type": "object"}},
            "required": ["action", "params"], "additionalProperties": False,
        }),
        "recall": _nullable(_RECALL),
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "entry", "stop", "breakeven", "exit",
                 "setup_falsified_if", "setup_exhausted_if", "on_dol_falsified",
                 "recall", "reasoning"],
    "additionalProperties": False,
}
