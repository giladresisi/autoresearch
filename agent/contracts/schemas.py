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

import copy
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

# decisions/thesis.md §2.1 evidence-ledger item shape. P1 (HTF close beyond/before at ANY
# sweep) and P2 (meaningful SMT + HTF rejection) share the accept/reject-of-a-sweep shape;
# P5 (plan 15 Task 4 — FVG-fill) reuses it with the sign derived from the fill zone's
# bull/bear kind; P3 (equilibrium position) and P4 (reclaim/failed-reclaim) are the plan-15
# Task 6 minimal code-derivation (P3 above/below mid, P4 reclaim accept/reject). The model
# declares WHICH criterion/asset/level/tier/tf fired and whether it is accept or reject; code
# (validate_contracts.score_thesis_evidence) derives the UP/DOWN sign — never model-declared —
# which is what makes a P1 sign misclassification structurally impossible (2026-07-02 08:00 bug).
EVIDENCE_CRITERIA = {"P1", "P2", "P3", "P4", "P5"}
EVIDENCE_ASSETS = {"MNQ", "MES"}
EVIDENCE_TIERS = {"session", "day", "week"}
EVIDENCE_TFS = {"1h", "4h"}
EVIDENCE_DIRECTIONS = {"accept", "reject"}

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
    evidence: list = field(default_factory=list)      # [P1/P2 evidence item] — §2.1
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
            evidence=list(d.get("evidence") or []),
            reasoning=d.get("reasoning"))

    def to_dict(self) -> dict:
        return {
            "thesis_id": self.thesis_id, "issued_at": self.issued_at,
            "facts_hash": self.facts_hash, "bias": self.bias, "regime": self.regime,
            "dol": self.dol, "falsified_if": self.falsified_if,
            "exhausted_if": self.exhausted_if, "confidence": self.confidence,
            "recall": self.recall, "evidence": self.evidence,
            "reasoning": self.reasoning,
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
        "evidence": [],
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

# decisions/thesis.md §2.1 evidence-ledger item — P1/P2 only (see EVIDENCE_* comment above).
# `points`/`side` are NOT in this schema: they are code-derived (validate_contracts.
# score_thesis_evidence), never model-declared — the model selects which criterion/asset/
# level/tier/tf/direction fired; code does the sign + arithmetic.
_EVIDENCE_ITEM = {
    "type": "object",
    "properties": {
        "criterion": {"enum": sorted(EVIDENCE_CRITERIA)},
        "asset": {"enum": sorted(EVIDENCE_ASSETS)},
        "level": {"type": "string"},
        "tier": {"enum": sorted(EVIDENCE_TIERS)},
        "tf": {"enum": sorted(EVIDENCE_TFS)},
        "direction": {"enum": sorted(EVIDENCE_DIRECTIONS)},
        "mature": {"type": "boolean"},
        # plan 15 Task 5: optional model override — set True on a P2 SMT item the model judges
        # stale (played out past its tier-relative shelf life, see S9 suggested_exhausted) to
        # exclude it from continuation scoring. Omitted = not exhausted. LLM-judged, not
        # hard-gated: code zeroes an exhausted item's points, it never sets this itself.
        "exhausted": {"type": "boolean"},
        # plan 15 Task 7: optional structured pending-resolution for an IMMATURE (mature:False)
        # day/week item — when/how it will resolve, so an execution layer can read it. Only
        # meaningful when mature is False; informational (zero points either way). resolves_at
        # is the code-computed next-HTF-close timestamp the model COPIES from the S9 "PENDING
        # RESOLUTION" line (not model-computed); it is optional (omit -> relative timing in
        # prose). implied_direction_if_confirmed is the model's read of which way the item
        # leans IF the pending close confirms — audit metadata, never fed to score_thesis_evidence.
        "pending_resolution": {
            "type": "object",
            "properties": {
                "resolves_tf": {"enum": sorted(EVIDENCE_TFS)},
                "resolves_at": {"type": "string"},
                "implied_direction_if_confirmed": {"enum": ["UP", "DOWN"]},
            },
            "required": ["resolves_tf", "implied_direction_if_confirmed"],
            "additionalProperties": False,
        },
    },
    "required": ["criterion", "asset", "level", "tier", "tf", "direction", "mature"],
    "additionalProperties": False,
}
_EVIDENCE_LIST = {"type": "array", "items": _EVIDENCE_ITEM}

THESIS_SCHEMA = {
    "type": "object",
    "$defs": dict(_PRED_DEFS),
    # Property order is deliberate: both backends use STRICT json-schema-constrained
    # output (agent/run_agent.py's OpenRouterBackend/AnthropicBackend), which generates
    # fields in this declared order. `evidence` and `reasoning` come BEFORE `bias` so the
    # model enumerates its evidence and narrates its tally as real generated tokens it can
    # condition on, before committing to bias — previously `bias` was first, forcing the
    # model to commit to a bias token sequence before generating a single evidence item or
    # word of reasoning, which is exactly backwards (no chain-of-thought was structurally
    # possible before the answer). Code still independently re-derives sign/net-score from
    # `evidence` and never trusts this ordering or the reasoning text itself — this is a
    # generation-order fix for the model's own consistency, not a new validated input.
    "properties": {
        "evidence": _EVIDENCE_LIST,
        "reasoning": {"type": "string"},
        "bias": {"enum": sorted(BIASES)},
        "regime": {"enum": sorted(DAILY_REGIMES)},
        "dol": _DOL,
        "falsified_if": _PRED_LIST,
        "exhausted_if": _PRED_LIST,
        "confidence": {"enum": sorted(CONFIDENCES)},
        "recall": _RECALL,
    },
    "required": ["evidence", "reasoning", "bias", "regime", "dol", "falsified_if",
                 "exhausted_if", "confidence", "recall"],
    "additionalProperties": False,
}


# plan 15 Tasks 4/6: synthetic equilibrium-mid level names a P3/P4 evidence item may
# reference (not real named price levels — the daily/weekly mid position and its
# HTF-confirmed reclaim). Allowed in the evidence `level` enum ONLY (never the DOL enum,
# which must stay a real drawable pool). P3 uses the bare mid; P4 the _high/_low reclaim
# form (the reclaim direction _evidence_side derives its sign from).
EVIDENCE_MID_LEVELS = (
    "daily_mid", "weekly_mid",
    "daily_mid_high", "daily_mid_low", "weekly_mid_high", "weekly_mid_low",
)


def build_thesis_schema(valid_levels=None, extra_evidence_levels=None) -> dict:
    """THESIS_SCHEMA with the `level` fields on evidence items and DOL constrained to an
    ENUM of the level names actually present in THIS call's facts, when `valid_levels` is
    given. Under strict schema-constrained decoding (both backends use this), an enum
    makes it STRUCTURALLY IMPOSSIBLE for the model to emit a plausible-but-malformed level
    name (e.g. 'asia_cur_high' instead of the facts sheet's 'asia(cur)_high') — a
    recurring SEM_LEVEL_NOT_IN_FACTS failure seen across repeated manual runs, previously
    only caught after the fact by the semantic validator and sent back for a retry.
    `valid_levels=None`/empty falls back to the original unconstrained string type —
    byte-identical to plain THESIS_SCHEMA for any caller that has no facts (tests, the
    static THESIS_SCHEMA export itself).

    `extra_evidence_levels` (plan 15 Tasks 4/6): additional strings allowed for the
    EVIDENCE `level` only, not the DOL — the P5 FVG-zone ids and the P3/P4 synthetic mid
    names (EVIDENCE_MID_LEVELS). These are real facts-grounded identifiers that are not
    named price levels, so they belong in the evidence enum but must never be offered as a
    DOL draw. Ignored (byte-identical) when `valid_levels` is falsy."""
    schema = copy.deepcopy(THESIS_SCHEMA)
    if valid_levels:
        dol_enum = {"enum": sorted(set(valid_levels))}
        ev_names = set(valid_levels) | set(extra_evidence_levels or []) | set(EVIDENCE_MID_LEVELS)
        level_enum = {"enum": sorted(ev_names)}
        schema["properties"]["evidence"]["items"]["properties"]["level"] = level_enum
        schema["properties"]["dol"]["anyOf"][0]["properties"]["level"] = dol_enum
        # level_swept/level_depleted predicates (falsified_if/exhausted_if/recall.events)
        # reference a level by `name`, not `level` — same malformed-name risk, same fix.
        # These atom defs live in $defs (shared via $ref), so patch them there too. They must
        # name a REAL price level (dol_enum), never a synthetic mid / FVG-zone id.
        for def_name in ("pred_level_swept", "pred_level_depleted"):
            if def_name in schema["$defs"]:
                schema["$defs"][def_name]["properties"]["name"] = dol_enum
    return schema


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
