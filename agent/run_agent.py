"""GIL-44 Phase-2 decision-agent runner (backend-pluggable production LLMClient).

Makes the two LLM decision calls per cut — `daily_trend` at the checkpoint, then
`next_move` as of now consuming the standing daily-trend — enforces schema-shaped
structured output, and runs each call through the deterministic validator with a
bounded validate-and-retry loop (fail-safe NEUTRAL/LOW on repeated failure).

Prompt assembly is byte-stable so the knowledge base caches as a single cached
system prefix (the 90% cache-read discount depends on those bytes never moving):
the system prompt is the KB concatenated VERBATIM in a fixed order, with one
prompt-cache breakpoint after it. The per-cut facts sheet + context + a short task
instruction are the (uncached) user message.

Two backends sit behind one interface:
  - OpenRouterBackend (default) — plain httpx to the chat-completions API, Anthropic
    provider pinned so prompt caching is consistent.
  - AnthropicBackend — the official SDK, structured output via `output_config.format`.

CLI:
    python agent/run_agent.py --cut calibration/cuts/<id> [--backend ...] [--model ...]

Writes one self-contained `decision.json` into the cut folder (the canonical
decision, per-call reasoning, validator verdicts, retry counts, fallback flags,
token usage incl. cache read/write, and per-call latency). Library paths are
silent; only the CLI prints a one-line summary.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Path wiring — make the sibling validator + the calibration facts parser      #
# importable both as a CLI (python agent/run_agent.py) and under pytest.       #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
_CALIB_DIR = os.path.join(REPO_ROOT, "calibration")
_CONTRACTS_DIR = os.path.join(HERE, "contracts")
for _p in (HERE, _CALIB_DIR, _CONTRACTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from validator import ValidationResult, Violation, failsafe_decision, validate  # noqa: E402
from validate_results import parse_facts  # noqa: E402  (calibration/validate_results.py)

# --------------------------------------------------------------------------- #
# Knowledge base — VERBATIM, in this FIXED order (never glob order). The system #
# prompt bytes must be identical every run for the prompt cache to hit.        #
# --------------------------------------------------------------------------- #
DOCS_ROOT = os.path.join(REPO_ROOT, "agent-docs", "strategy")
KB_FILES = (
    "smt.md",
    "liquidity-levels.md",
    "equilibrium.md",
    "session-structure.md",
    "entry-confirmation.md",
    "decisions/daily-trend.md",
    "decisions/next-move.md",
)
_KB_SEP = "\n\n---\n\n"

DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-haiku-4.5",
    "anthropic": "claude-haiku-4-5",
}
MAX_TOKENS = 8192
MAX_RETRIES = 2


def build_system_prompt(docs_root: str = DOCS_ROOT) -> str:
    """Concatenate the KB VERBATIM in KB_FILES order → byte-stable system string.

    Reads each file as utf-8 with newline translation disabled so the emitted
    bytes are exactly the on-disk bytes; identical inputs always yield the
    byte-identical system string (the prompt-cache invariant).
    """
    blocks = []
    for rel in KB_FILES:
        path = os.path.join(docs_root, rel.replace("/", os.sep))
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        blocks.append(f"# FILE: {rel}\n\n{text}")
    return _KB_SEP.join(blocks)


# --------------------------------------------------------------------------- #
# Structured-output JSON schemas — mirror the validator's canonical shape       #
# (the dict failsafe_decision() returns). All objects are closed and fully      #
# required (strict structured outputs); nullables use anyOf.                    #
# --------------------------------------------------------------------------- #
def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_ENUM_DIR = {"enum": ["up", "down", "neutral"]}
_ENUM_CONF = {"enum": ["high", "medium", "low"]}

_DRIVER_ITEM = {
    "type": "object",
    "properties": {
        "id": {"enum": ["D1", "D2", "D3", "D4", "D5", "D6"]},
        "vote": {"type": "integer", "enum": [-1, 0, 1]},
        "weight": {"type": "number"},
        "contribution": {"type": "number"},
        "events": {
            "type": "array", "items": {"type": "string"},
            "description": "Short stable ids of the physical events/data this "
                           "driver's vote rests on (e.g. "
                           "'overnight_high_sweep_complex'). Drivers resting on the "
                           "SAME event must use the identical id — the correlation "
                           "audit checks for double-counting across drivers.",
        },
        "discounted_events": {
            "type": "array", "items": {"type": "string"},
            "description": "Subset of this driver's events on which it already took "
                           "the x0.5 second-appearance correlation discount in its "
                           "contribution.",
        },
    },
    "required": ["id", "vote", "weight", "contribution", "events",
                 "discounted_events"],
    "additionalProperties": False,
}

DAILY_TREND_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": _ENUM_DIR,
        "confidence": _ENUM_CONF,
        "regime": {"enum": ["trend", "range", "hybrid"]},
        "day_dol": _nullable({"type": "string"}),
        "weakens_to_neutral_if": {"type": "string"},
        "flips_if": {"type": "string"},
        "drivers": {"type": "array", "items": _DRIVER_ITEM},
        "S": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "direction", "confidence", "regime", "day_dol", "weakens_to_neutral_if",
        "flips_if", "drivers", "S", "reasoning",
    ],
    "additionalProperties": False,
}

_LEDGER_ITEM = {
    "type": "object",
    "properties": {
        "type": {
            "enum": [
                "smt_divergence", "sweep", "failed_reclaim_daily_mid",
                "displacement_mss", "mid_rejection", "sustained_acceptance",
                "laggard_fail",
            ]
        },
        "tier": {"type": "number"},
        "session_side": {"type": "number"},
        "alignment": {"type": "number"},
        "freshness": {"type": "number"},
        "whipsaw": {
            "type": "number",
            "description": "Declared whipsaw multiplier: 0.5 ONLY for structure items "
                           "inside the 09:15-11:30 ET whipsaw window, else 1.0. If it "
                           "applies, declare it HERE - never fold an undeclared 0.5 "
                           "into score.",
        },
        "score": {
            "type": "number",
            "description": "MUST equal tier * session_side * alignment * freshness * "
                           "whipsaw using exactly the values declared in THIS item.",
        },
        "note": {"type": "string"},
    },
    "required": [
        "type", "tier", "session_side", "alignment", "freshness", "whipsaw",
        "score", "note",
    ],
    "additionalProperties": False,
}

_VETO_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "triggered": {"type": "boolean"},
        "effect": _nullable({"enum": ["cap_to_low", "cap_to_medium"]}),
    },
    "required": ["name", "triggered", "effect"],
    "additionalProperties": False,
}

_MOVE_TARGET = _nullable({
    "type": "object",
    "properties": {
        "level": {"type": "string"},
        "price": _nullable({"type": "number"}),
    },
    "required": ["level", "price"],
    "additionalProperties": False,
})

_RES_SIDE = {
    "type": "object",
    "properties": {
        "condition": {"type": "string"},
        "price": _nullable({"type": "number"}),
        "target": _nullable({"type": "string"}),
    },
    "required": ["condition", "price", "target"],
    "additionalProperties": False,
}

NEXT_MOVE_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": _ENUM_DIR,
        "confidence": _ENUM_CONF,
        "move_target": _MOVE_TARGET,
        "flip_trigger": {"type": "string"},
        "flipped_target": _nullable({"type": "string"}),
        "arm_entry_confirmation": {"enum": ["yes", "no"]},
        "resolution": {
            "type": "object",
            "properties": {"long_if": _RES_SIDE, "short_if": _RES_SIDE},
            "required": ["long_if", "short_if"],
            "additionalProperties": False,
        },
        "bull_ledger": {"type": "array", "items": _LEDGER_ITEM},
        "bear_ledger": {"type": "array", "items": _LEDGER_ITEM},
        "N": {
            "type": "number",
            "description": "MUST equal sum of bull_ledger scores minus sum of "
                           "bear_ledger scores, exactly as declared. Direction must "
                           "then follow N against the +/-3 gate (up: N>=3, down: "
                           "N<=-3, else neutral).",
        },
        "vetoes": {"type": "array", "items": _VETO_ITEM},
        "reasoning": {"type": "string"},
    },
    "required": [
        "direction", "confidence", "move_target", "flip_trigger", "flipped_target",
        "arm_entry_confirmation", "resolution", "bull_ledger", "bear_ledger", "N",
        "vetoes", "reasoning",
    ],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Task instructions (the tail of each user message)                            #
# --------------------------------------------------------------------------- #
_TASK_DAILY = (
    "TASK — daily-trend decision.\n"
    "Make the daily-trend decision exactly per decisions/daily-trend.md, evaluated "
    "AT the checkpoint printed in S0 ('daily-trend checkpoint (most recent at/before "
    "now)'), using ONLY data at or before that checkpoint. Score the six slow drivers "
    "with integer votes in {-1, 0, +1} at their fixed doc weights, sum to S, and set "
    "direction/confidence/regime per the doc thresholds. Declare each driver's "
    "underlying physical events in its `events` list (same event -> identical id "
    "across drivers); when a driver takes the x0.5 second-appearance correlation "
    "discount on an event, list that id in its `discounted_events`. Return JSON "
    "matching the schema."
)
_TASK_NEXT = (
    "TASK — next-move decision.\n"
    "Make the next-move decision exactly per decisions/next-move.md, evaluated as of "
    "'now' (the last S0 timestamp). CONSUME the standing daily-trend decision provided "
    "below (do not recompute it). Build the bull/bear ledgers with the closed item "
    "types and multiplier sets, net to N, apply vetoes, and set direction/confidence/"
    "targets/resolution per the doc. Return JSON matching the schema.\n"
    "ITEM CARDS: where an S3b candidate item card exists for an item, COPY tier and "
    "freshness (and their base product) from the card — never recompute the freshness "
    "exponent yourself; only session_side, alignment and whipsaw are yours to judge.\n"
    "ARITHMETIC CONTRACT (validated deterministically): each ledger item's score MUST "
    "equal the product of its five declared multiplier fields (tier * session_side * "
    "alignment * freshness * whipsaw) - if a multiplier applies, declare it in its "
    "field, never fold it silently into score. N MUST equal sum(bull scores) - "
    "sum(bear scores). Direction MUST follow the declared N against the +/-3 gate. "
    "Recompute these three checks before returning.\n"
    "TARGET CONTRACT: a directional call MUST carry a move_target, and it must be a "
    "level present in the facts, on the correct side of current price, and NOT "
    "already swept/depleted (a spent pool is never a target).\n\n"
    "STANDING DAILY-TREND DECISION (verbatim):\n"
)


# --------------------------------------------------------------------------- #
# Backend interface + normalized response                                      #
# --------------------------------------------------------------------------- #
@dataclass
class CallResponse:
    parsed: dict
    raw_text: str
    usage: dict
    model: str


class Backend:
    """One call = one structured-output completion. Subclasses do the transport."""

    name: str = "backend"
    model: str = ""

    def complete(self, *, system: str, messages: list[dict], schema: dict,
                 max_tokens: int = MAX_TOKENS) -> CallResponse:
        raise NotImplementedError


class OpenRouterBackend(Backend):
    """OpenRouter chat-completions, Anthropic provider pinned (cache consistency)."""

    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: Optional[str] = None):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the openrouter backend")
        self._key = api_key
        self.model = model or DEFAULT_MODELS["openrouter"]

    def complete(self, *, system: str, messages: list[dict], schema: dict,
                 max_tokens: int = MAX_TOKENS) -> CallResponse:
        import httpx

        sys_msg = {
            "role": "system",
            "content": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [sys_msg, *messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "decision", "strict": True, "schema": schema},
            },
            "provider": {"order": ["Anthropic"], "allow_fallbacks": False},
            "usage": {"include": True},
        }
        resp = httpx.post(
            self.URL,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        usage = {
            "input_tokens": u.get("prompt_tokens"),
            "output_tokens": u.get("completion_tokens"),
            "cache_read_input_tokens": (
                details.get("cached_tokens")
                if details.get("cached_tokens") is not None
                else u.get("cache_read_input_tokens", 0)
            ),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "raw": u,
        }
        return CallResponse(parsed=json.loads(text), raw_text=text, usage=usage,
                            model=data.get("model", self.model))


class AnthropicBackend(Backend):
    """Official anthropic SDK; structured output via output_config.format."""

    name = "anthropic"

    def __init__(self, api_key: str, model: Optional[str] = None):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the anthropic backend")
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or DEFAULT_MODELS["anthropic"]

    def complete(self, *, system: str, messages: list[dict], schema: dict,
                 max_tokens: int = MAX_TOKENS) -> CallResponse:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return CallResponse(parsed=json.loads(text), raw_text=text, usage=usage,
                            model=resp.model)


class StubBackend(Backend):
    """Offline, key-free backend for tests/CI and DECISIONS_REAL_API=False.

    With no `responses` it returns a DETERMINISTIC schema-valid NEUTRAL/LOW block
    (the fail-safe shape) for whichever schema it is handed — so the decisions engine
    can run end-to-end with no network and no API spend. An optional `responses`
    queue lets a test inject a scripted sequence of parsed decision dicts (e.g. a
    protocol-violating block to exercise the validate-and-retry / failsafe path).
    """

    name = "stub"

    def __init__(self, responses: Optional[list] = None, model: str = "stub-model"):
        self.model = model
        self._queue = ([copy.deepcopy(r) for r in responses]
                       if responses is not None else None)

    def _canned(self, schema: dict) -> dict:
        props = schema.get("properties", {})
        # v2 thesis / trade-plan schemas (spec §2) — first-class stub paths.
        if "bias" in props:
            from schemas import failsafe_thesis
            out = copy.deepcopy(failsafe_thesis())
            out["reasoning"] = "stub deterministic neutral/low thesis (offline backend)"
            return out
        if "verdict" in props:
            from schemas import failsafe_plan
            out = copy.deepcopy(failsafe_plan())
            out["reasoning"] = "stub deterministic WAIT plan (offline backend)"
            return out
        fs = failsafe_decision()
        block = fs["daily_trend"] if "drivers" in props else fs["next_move"]
        out = copy.deepcopy(block)
        out["reasoning"] = "stub deterministic neutral/low decision (offline backend)"
        return out

    def complete(self, *, system: str, messages: list[dict], schema: dict,
                 max_tokens: int = MAX_TOKENS) -> CallResponse:
        parsed = self._queue.pop(0) if self._queue is not None else self._canned(schema)
        return CallResponse(
            parsed=parsed,
            raw_text=json.dumps(parsed),
            usage={"input_tokens": 0, "output_tokens": 0,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            model=self.model,
        )


# --------------------------------------------------------------------------- #
# .env loading — shell-set vars WIN (setdefault semantics), key never logged.  #
# --------------------------------------------------------------------------- #
def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            if key:
                os.environ.setdefault(key, val)  # shell-set value takes precedence


def make_backend(backend: Optional[str] = None, model: Optional[str] = None,
                 env_path: Optional[str] = None) -> Backend:
    """Build a backend, auto-selecting by key availability when not overridden.

    Loads the worktree .env first (shell-set vars win). When `backend` is None
    the selection is automatic: OPENROUTER_API_KEY present -> OpenRouter; else
    ANTHROPIC_API_KEY present -> Anthropic; else a clear error. An explicit
    `backend` (the --backend flag) overrides the auto-selection.
    """
    if backend == "stub":
        return StubBackend(model=model)          # offline; no .env / key needed
    load_env_file(env_path or os.path.join(REPO_ROOT, ".env"))
    if backend is None:
        if os.environ.get("OPENROUTER_API_KEY"):
            backend = "openrouter"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            backend = "anthropic"
        else:
            raise RuntimeError(
                "No API key found — set OPENROUTER_API_KEY (preferred) or "
                "ANTHROPIC_API_KEY in the environment or the worktree .env"
            )
    if backend == "openrouter":
        return OpenRouterBackend(os.environ.get("OPENROUTER_API_KEY", ""), model)
    if backend == "anthropic":
        return AnthropicBackend(os.environ.get("ANTHROPIC_API_KEY", ""), model)
    raise ValueError(f"unknown backend {backend!r} (expected openrouter|anthropic)")


# --------------------------------------------------------------------------- #
# Code-derived arithmetic — the model judges, code computes                     #
# --------------------------------------------------------------------------- #
# The model's judgment lives in item selection and the multiplier CHOICES
# (session_side / alignment / whipsaw, votes, vetoes); the arithmetic over them
# (item score products, N/S sums, the confidence ceiling) is pure computation and
# was the dominant retry/failsafe cause (ARI_N_MISMATCH + dependents — a failsafe
# DISCARDS correctly-gathered evidence, observed 2026-06-25 08:30). The model still
# emits its own arithmetic as a self-check; code overrides it and records every
# disagreement in the attempt audit ("arith_overrides"). Direction is deliberately
# NOT overridden: a declared direction inconsistent with the computed net score is
# a judgment/form error (targets and resolution follow direction), so it stays a
# validator retry — now quoted against the CORRECT net score.
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _min_conf(a: str, b: str) -> str:
    return a if _CONF_RANK.get(a, 0) <= _CONF_RANK.get(b, 0) else b


def _derive_next_arithmetic(block: dict) -> tuple[dict, list]:
    """Compute item scores, N, and the confidence ceiling from the model's declared
    judgments (next-move.md §2-§4). Returns (block, override_notes)."""
    notes: list = []
    totals = {}
    for side in ("bull_ledger", "bear_ledger"):
        total = 0.0
        for i, item in enumerate(block.get(side) or []):
            try:
                computed = (float(item["tier"]) * float(item["session_side"])
                            * float(item["alignment"]) * float(item["freshness"])
                            * float(item["whipsaw"]))
            except (KeyError, TypeError, ValueError):
                continue
            declared = item.get("score")
            if not isinstance(declared, (int, float)) or abs(declared - computed) > 1e-9:
                notes.append(f"{side}[{i}].score {declared} -> {computed:.6g}")
                item["score"] = computed
            total += computed
        totals[side] = total
    n = totals.get("bull_ledger", 0.0) - totals.get("bear_ledger", 0.0)
    if not isinstance(block.get("N"), (int, float)) or abs(block["N"] - n) > 1e-9:
        notes.append(f"N {block.get('N')} -> {n:.6g}")
        block["N"] = n

    # Confidence ceiling (§3) + veto caps (§4) — mechanical given the declared items.
    win = max(totals.values(), default=0.0)
    lose = min(totals.values(), default=0.0)
    if abs(n) >= 6 and (win <= 0 or lose <= 0.5 * win):
        ceiling = "high"
    elif abs(n) >= 3:
        ceiling = "medium"
    else:
        ceiling = "low"
    for veto in block.get("vetoes") or []:
        if veto.get("triggered") and veto.get("effect") == "cap_to_low":
            ceiling = "low"
        elif veto.get("triggered") and veto.get("effect") == "cap_to_medium":
            ceiling = _min_conf(ceiling, "medium")
    declared_conf = block.get("confidence")
    final = _min_conf(str(declared_conf), ceiling)
    if final != declared_conf:
        notes.append(f"confidence {declared_conf} -> {final} (ceiling {ceiling})")
        block["confidence"] = final
    return block, notes


def _derive_thesis_arithmetic(block: dict, magnitude=None, dol_available=None,
                              suppressed_p1_levels=None, suppressed_p2_sites=None,
                              level_htf_close_status=None, level_tiers=None,
                              smt_candidates=None, week_extremes=None,
                              now_price=None, fvg_zone_meta=None) -> tuple[dict, list]:
    """Compute per-item points, net score, and the confidence ceiling from the model's
    declared P1/P2 evidence ledger (decisions/thesis.md §2.1/§4/§6). Mirrors
    _derive_daily_arithmetic/_derive_next_arithmetic: confidence is silently corrected
    here (pure arithmetic); bias is deliberately NOT overridden — a declared bias
    inconsistent with the computed net score is a judgment error and stays a validator
    retry (validate_contracts.validate_thesis's ARI_THESIS_BIAS check), quoted against
    the CORRECT net score on retry. An empty/absent ledger (e.g. the NEUTRAL failsafe,
    or a thin P3/P4-only call not yet covered by this ledger — thesis.md §8 gap) is a
    no-op.

    `dol_available` ({"UP": bool, "DOWN": bool}, thesis.md §8 no-liquidity rule) folds the
    same no-eligible-DOL-for-this-direction override into the confidence ceiling here as
    validate_thesis applies to expected_bias — so a no-liquidity call is clamped to LOW
    confidence even before the bias-consistency retry loop, not just at the validator.

    `level_htf_close_status`/`level_tiers`/`smt_candidates`/`week_extremes`/`now_price`/
    `fvg_zone_meta` (2026-08-02) are threaded straight through to score_thesis_evidence's
    P1/P2/P3 auto-derivation, §2.1e tier promotion, extremity-based dominance resolution,
    and P5 same-move dedup — code-injected evidence is reflected here too, so the
    ceiling/net-score computed BEFORE the validator retry loop already accounts for it,
    not just validate_thesis's own re-scoring."""
    from validate_contracts import score_thesis_evidence
    notes: list = []
    evidence = block.get("evidence") or []
    if not evidence and not level_htf_close_status and not level_tiers:
        return block, notes
    scoring = score_thesis_evidence(evidence, magnitude=magnitude, dol_available=dol_available,
                                    suppressed_p1_levels=suppressed_p1_levels,
                                    suppressed_p2_sites=suppressed_p2_sites,
                                    level_htf_close_status=level_htf_close_status,
                                    level_tiers=level_tiers, smt_candidates=smt_candidates,
                                    week_extremes=week_extremes, now_price=now_price,
                                    fvg_zone_meta=fvg_zone_meta)
    # Audit-annotate each item with its computed points/side in place (mirrors
    # _derive_next_arithmetic writing item["score"] back onto the ledger).
    block["evidence"] = scoring["scored_evidence"]
    ceiling = scoring["confidence_ceiling"]
    declared_conf = block.get("confidence")
    final = _min_conf(str(declared_conf).lower(), ceiling.lower())
    if final.upper() != declared_conf:
        notes.append(f"confidence {declared_conf} -> {final.upper()} "
                     f"(ceiling {ceiling}, net_score {scoring['net_score']}, "
                     f"contradiction {scoring['contradiction']})")
        block["confidence"] = final.upper()
    return block, notes


def _derive_daily_arithmetic(block: dict) -> tuple[dict, list]:
    """Compute S and the confidence ceiling from the declared driver contributions
    (daily-trend.md §3). Contributions themselves stay model-owned — they encode the
    D1 halving and correlation-discount judgments the validator checks separately."""
    notes: list = []
    drivers = block.get("drivers") or []
    contribs = [d.get("contribution") for d in drivers
                if isinstance(d.get("contribution"), (int, float))]
    s = float(sum(contribs))
    if not isinstance(block.get("S"), (int, float)) or abs(block["S"] - s) > 1e-9:
        notes.append(f"S {block.get('S')} -> {s:.6g}")
        block["S"] = s

    opposer = any(isinstance(d.get("weight"), (int, float)) and d["weight"] >= 2
                  and isinstance(d.get("vote"), (int, float)) and d["vote"] != 0
                  and (d["vote"] > 0) != (s > 0)
                  for d in drivers) if s != 0 else False
    if abs(s) >= 6 and not opposer:
        ceiling = "high"
    elif abs(s) >= 3:
        ceiling = "medium"
    else:
        ceiling = "low"
    declared_conf = block.get("confidence")
    final = _min_conf(str(declared_conf), ceiling)
    if final != declared_conf:
        notes.append(f"confidence {declared_conf} -> {final} (ceiling {ceiling})")
        block["confidence"] = final
    return block, notes


# --------------------------------------------------------------------------- #
# Validate-and-retry — the trust boundary                                      #
# --------------------------------------------------------------------------- #
def _retry_prompt(violations: list[str]) -> str:
    bullet = "\n".join(f"  - {m}" for m in violations)
    body = (
        "Your previous JSON decision failed deterministic validation with these "
        "protocol violations:\n"
        f"{bullet}\n\n"
        "Correct the violations, then RE-DERIVE every OTHER field that depends on what "
        "you changed so the whole decision is mutually consistent — do not just relabel "
        "one field to silence a violation while leaving the evidence/ledger that produced "
        "it untouched. In particular: a declared direction/bias must match the "
        "RECOMPUTED net score of your own ledger/evidence items (if a violation names an "
        "expected value, that expected value is correct — either change the label to "
        "match it, or if you believe your evidence was right, change your evidence items "
        "instead and let the label follow); confidence must respect its computed "
        "ceiling; any directional target/DOL must exist in the facts, be unswept/"
        "undepleted, and sit on the correct side of current price; a falsified_if/"
        "exhausted_if predicate must not already be true at the current price. Keep "
        "unrelated fields identical and return a fresh, complete JSON decision matching "
        "the schema."
    )
    # plan 15 Task 8 (cheaper lever, per the Task-1b spike): an ARI_THESIS_BIAS mismatch is
    # the recurring "model re-tallies its own evidence and re-diverges on retry" failure — the
    # code's net score is authoritative and the per-item points/sign are code-derived, so the
    # fix is to relabel `bias`, NOT to re-pick or re-tally the evidence ledger. Spelling this
    # out stops the observed churn where each retry rewrote the ledger and produced a new
    # inconsistency (2026-07-14 01:00 → 3 attempts → failsafe).
    if any("ARI_THESIS_BIAS" in m for m in violations):
        body += (
            "\n\nON THE BIAS/NET-SCORE MISMATCH SPECIFICALLY: the expected bias quoted above "
            "is the sign of code's authoritative recomputation of YOUR OWN evidence ledger — "
            "you do not tally the points yourself (tier x tf x clearance and the UP/DOWN sign "
            "are all code-derived per item). Do NOT re-pick or re-tally your evidence items to "
            "chase a different number. KEEP your evidence ledger exactly as-is and set `bias` "
            "to the quoted expected value — UNLESS a specific evidence item is factually wrong "
            "(wrong level, wrong accept/reject, wrong mature/tier/tf), in which case fix that "
            "ONE item and let bias follow. Changing the whole ledger on this retry is what "
            "causes it to fail again."
        )
    return body


@dataclass
class CallOutcome:
    block: dict
    reasoning: Optional[str]
    retries: int
    fallback: bool
    verdict: str                       # "clean" | "failsafe"
    attempts: list = field(default_factory=list)
    latency_total: float = 0.0
    usage_total: dict = field(default_factory=dict)


def _sum_usage(attempts: list) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    out = {k: 0 for k in keys}
    for a in attempts:
        for k in keys:
            v = a["usage"].get(k)
            if isinstance(v, (int, float)):
                out[k] += v
    return out


def _run_call(backend: Backend, system: str, base_user: str, schema: dict,
              validate_block, failsafe_block: dict,
              max_retries: int = MAX_RETRIES, derive_block=None) -> CallOutcome:
    """One decision call with a bounded correction loop.

    derive_block(parsed) -> (parsed, override_notes) runs FIRST (code-derived
    arithmetic overriding the model's self-check numbers). validate_block(parsed_block)
    -> ValidationResult scoped to THIS call. On a validation failure we echo the
    model's own (invalid) JSON back and quote the exact violations; after max_retries
    failures we fall back to the NEUTRAL/LOW scripts baseline and record it.
    """
    messages = [{"role": "user", "content": base_user}]
    attempts: list = []
    reasoning: Optional[str] = None
    retries = 0

    for attempt in range(max_retries + 1):
        t0 = time.perf_counter()
        resp = backend.complete(system=system, messages=messages, schema=schema)
        latency = time.perf_counter() - t0

        parsed = dict(resp.parsed)
        reasoning = parsed.pop("reasoning", None)     # audit-only; never validated
        overrides: list = []
        if derive_block is not None:
            parsed, overrides = derive_block(parsed)
        result = validate_block(parsed)
        attempts.append({
            "usage": resp.usage,
            "latency_sec": round(latency, 3),
            "model": resp.model,
            "ok": result.ok,
            "violations": result.messages(),
            "arith_overrides": overrides,
            # Full per-attempt parsed block — including FAILED attempts, which were
            # previously discarded once the next retry started. Needed to diagnose *why*
            # an attempt failed (bad tally vs. sound reasoning + a slip choosing bias, a
            # bad predicate pick, etc.), not just that it failed. Post-analysis only,
            # never validated — this dict is not read by any code path.
            "bias": parsed.get("bias"),
            "regime": parsed.get("regime"),
            "dol_rationale": parsed.get("dol_rationale"),
            "dol": parsed.get("dol"),
            "falsified_if_rationale": parsed.get("falsified_if_rationale"),
            "falsified_if": parsed.get("falsified_if"),
            "exhausted_if_rationale": parsed.get("exhausted_if_rationale"),
            "exhausted_if": parsed.get("exhausted_if"),
            "confidence": parsed.get("confidence"),
            "recall": parsed.get("recall"),
            "evidence": parsed.get("evidence"),
            "reasoning": reasoning,
        })

        if result.ok:
            return CallOutcome(
                block=parsed, reasoning=reasoning, retries=retries, fallback=False,
                verdict="clean", attempts=attempts,
                latency_total=round(sum(a["latency_sec"] for a in attempts), 3),
                usage_total=_sum_usage(attempts),
            )

        if attempt < max_retries:
            retries += 1
            messages.append({"role": "assistant", "content": resp.raw_text})
            messages.append({"role": "user", "content": _retry_prompt(result.messages())})

    return CallOutcome(
        block=dict(failsafe_block), reasoning=reasoning, retries=retries, fallback=True,
        verdict="failsafe", attempts=attempts,
        latency_total=round(sum(a["latency_sec"] for a in attempts), 3),
        usage_total=_sum_usage(attempts),
    )


def _next_only(result: ValidationResult) -> ValidationResult:
    """Keep only next_move-scoped violations (daily_trend is already clean)."""
    r = ValidationResult()
    r.violations = [v for v in result.violations if v.where.startswith("next_move")]
    return r


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _facts_context(facts_text: str, context_text: str) -> str:
    return (
        f"## FACT SHEET (deterministic)\n\n{facts_text}\n\n"
        f"## CONTEXT AT CUT\n\n{context_text}\n\n"
    )


def decide_daily(facts_text: str, context_text: str, facts: dict, backend: Backend, *,
                 docs_root: str = DOCS_ROOT) -> CallOutcome:
    """The daily-trend call in isolation (validate-and-retry, failsafe on repeat).

    Returns the CallOutcome (block + audit). The decisions engine calls this at each
    checkpoint to (re)compute the STANDING daily-trend; run_cut/decide compose it
    with decide_next. A neutral failsafe next_move keeps validation scoped to daily.
    """
    system = build_system_prompt(docs_root)
    fs = failsafe_decision()
    daily_user = _facts_context(facts_text, context_text) + _TASK_DAILY
    return _run_call(
        backend, system, daily_user, DAILY_TREND_SCHEMA,
        validate_block=lambda d: validate(
            {"daily_trend": d, "next_move": fs["next_move"]}, facts=facts),
        failsafe_block=fs["daily_trend"],
        derive_block=_derive_daily_arithmetic,
    )


def decide_next(facts_text: str, context_text: str, facts: dict, standing_daily: dict,
                backend: Backend, *, docs_root: str = DOCS_ROOT) -> CallOutcome:
    """The next-move call in isolation, CONSUMING a standing daily-trend verbatim.

    The decisions engine calls this at each hypothesis trigger with the standing
    daily-trend from the most recent checkpoint (or a neutral default before the
    first checkpoint)."""
    system = build_system_prompt(docs_root)
    fs = failsafe_decision()
    standing = json.dumps(standing_daily, indent=2, sort_keys=True)
    next_user = _facts_context(facts_text, context_text) + _TASK_NEXT + standing
    return _run_call(
        backend, system, next_user, NEXT_MOVE_SCHEMA,
        validate_block=lambda n: _next_only(
            validate({"daily_trend": standing_daily, "next_move": n}, facts=facts)),
        failsafe_block=fs["next_move"],
        derive_block=_derive_next_arithmetic,
    )


# --------------------------------------------------------------------------- #
# AI-trader v2 call types (spec §2) — thesis (L1) + trade plan (L2).           #
# These reuse the SAME backend / caching / validate-retry machinery; the KB    #
# system prompt is unchanged (KB restructuring is out of scope). The task tail #
# is a minimal skeleton — level internals (prompt content, calibration) are    #
# deferred; StubBackend paths are first-class (offline determinism).           #
# --------------------------------------------------------------------------- #
_TASK_THESIS = (
    "TASK — L1 thesis decision (AI-trader v2).\n"
    "Decide where the market is going and what would prove you wrong, as of 'now' (the "
    "last S0 timestamp). Return a thesis JSON matching the schema. The schema requests "
    "fields in this order: evidence, then reasoning, then bias/regime/dol_rationale/dol/"
    "falsified_if_rationale/falsified_if/exhausted_if_rationale/exhausted_if/confidence/"
    "recall — DELIBERATELY evidence-and-reasoning-first, so you enumerate and think "
    "through your evidence before committing to bias (UP/DOWN/NEUTRAL), regime, a DOL "
    "(draw-on-liquidity) when directional, and structured falsified_if/exhausted_if/"
    "recall predicates.\n"
    "\nRATIONALE FIELDS (dol_rationale, falsified_if_rationale, exhausted_if_rationale). "
    "Your main `reasoning` field closes BEFORE these three values are generated, so nothing "
    "in `reasoning` can justify them after the fact — each rationale is your ONLY chance to "
    "derive the field it immediately precedes, in 1-2 sentences, citing the specific facts "
    "that support it. dol_rationale: name the alternative pools you considered from the S8 "
    "DOL menu for your bias and why this one wins (nearest eligible, cleanest structure, "
    "etc.) — not a restatement of the DOL menu, an actual comparison. falsified_if_rationale: "
    "state why THIS threshold/level, not a different one — if you copied it from the S8 "
    "predicate menu, say which menu entry and why that one over the others offered. "
    "exhausted_if_rationale: same, for the exhaustion condition. If a rationale would just "
    "restate the value with no real derivation, that is a sign you have not actually decided "
    "why — reconsider the pick, do not paper over it with filler text. When bias is NEUTRAL "
    "and dol/falsified_if/exhausted_if are all null/empty (no-liquidity case below), each "
    "rationale should say so briefly (e.g. 'none — NEUTRAL, no DOL menu eligible').\n"
    "\nPREDICATE VOCABULARY (closed; the schema enforces it). Each predicate is one of the "
    "six atoms — price_beyond(price, side), n_closes_beyond(price, side, tf, n), "
    "level_swept(name), level_depleted(name), time_elapsed(minutes), clock_after(et_time) "
    "— or a single all_of/any_of over atoms (composition depth 1: a composite may NOT "
    "contain another composite). No other predicate type exists.\n"
    "\nMENUS (facts section S8). S8 pre-computes, for each direction, an eligible DOL menu "
    "(unswept/undepleted pools on the correct side of price, with IDs D1, D2, …) and a "
    "predicate menu (falsification F*, exhaustion X*, recall R*) with concrete params. "
    "SELECT BY COPYING a menu entry's params EXACTLY — pick your DOL from the DOL menu for "
    "your bias, your exhausted_if from the X entries (the draw is reached), your "
    "falsified_if from the F entries, and your recall.events from the R entries. Escape "
    "hatch: you MAY emit an off-menu predicate, but it must be schema-valid and every level "
    "it names must exist in the facts (unswept/undepleted where the menu requires). Prefer "
    "menu entries.\n"
    "\nEvery level named in a predicate or the DOL must exist in the facts. "
    "\n\nEVIDENCE LEDGER (P1/P2 only — thesis.md §2.1). As of 2026-08-02, every real, "
    "non-suppressed P1/P2 reading that already has a qualifying HTF close is injected by "
    "code directly — you do NOT need to declare it for it to score. Your remaining job "
    "here is narrower: (1) if you judge an auto-scored item stale/played-out, veto it by "
    "declaring that SAME {criterion, asset, level} with exhausted: true; (2) declare any "
    "IMMATURE (mature: false) item yourself if you want its pending_resolution logged — "
    "code never auto-injects an immature reading. For any item you DO declare, one per "
    "{criterion: P1|P2, asset: MNQ|MES, level: <name in facts>, "
    "tier: session|day|week, tf: 1h|4h (the qualifying HTF close), direction: "
    "accept|reject, mature: bool (has the §3 maturity gate — >=1 qualifying HTF close "
    "since the sweep — actually passed for this item?)}. Do NOT declare which way (UP/"
    "DOWN) an item leans — code derives that mechanically from the level's own high/low "
    "identity plus accept/reject, so leave that judgment to the validator. THE TWO SIDES "
    "MIRROR, THEY DO NOT MATCH — a recurring error is treating 'accept' as always-bullish "
    "on both sides: accepting BEYOND A HIGH is bullish continuation (price kept pushing "
    "up through resistance) — but accepting BEYOND A LOW is BEARISH continuation (price "
    "kept pushing down through support), not bullish, precisely because it is the same "
    "kind of continuation in the OPPOSITE direction. Symmetrically, rejecting a low "
    "(bouncing back above it) is bullish reversal; rejecting a high (failing above it, "
    "closing back under) is bearish reversal. Do not reason 'accept = bullish' or 'reject "
    "= bearish' as a blanket rule — always re-derive per item from which side (_high vs "
    "_low) it is. "
    "\nONE EVENT, ONE CITATION. If the SAME level/asset/direction qualifies on BOTH 1h and "
    "4h (both have a qualifying close since the sweep), code keeps only the 4h reading and "
    "zeroes an accompanying 1h one automatically — citing both does not add weight, so "
    "prefer declaring just the 4h item when both are mature. If only 1h has closed, declare "
    "that (4h's eventual read is still unknown). This does NOT apply across genuinely "
    "different timeframe READS of the same level (e.g. 1h rejects while 4h accepts) — that "
    "is a real, distinct contradiction worth declaring as two items, not a duplicate. "
    "\nTHIS SAME POLARITY RULE GOVERNS P2. A P2 item is the LAGGER (the asset whose own HTF "
    "close you read — §5) rejecting the liquidity it just swept, so the reversal runs in the "
    "OPPOSITE direction of the sweep: rejecting a swept LOW (the lagger swept a _low then "
    "closed back ABOVE it) is BULLISH; rejecting a swept HIGH (swept a _high then closed back "
    "UNDER it) is BEARISH. It is mechanically IDENTICAL to P1's own reject polarity, just "
    "applied to the lagger's side of an SMT pair rather than a plain single-asset sweep — a "
    "recurring error is narrating a correctly-scored bullish P2 low-reject as 'adds weight to "
    "the DOWN thesis' because the word 'swept a low' feels bearish. It does not: the REJECT "
    "flips it. Code derives the P2 sign the same way (level _high/_low polarity + reject), so "
    "describe it in prose the way code will score it, not backwards. Each swept "
    "level's HTF close in the facts sheet is tagged [clearance: WEAK|NORMAL|STRONG] — code "
    "scales that item's points by this already-visible factor (weak clearances count for "
    "less, strong for more) before tallying the net score. Weigh this when forming your own "
    "bias lean; do not compute the multiplier yourself, just account for which side's "
    "evidence is visibly stronger. Your declared "
    "bias must match the sign of the resulting net score, or the call is rejected and "
    "retried against the correct score — so tally your own items before committing to "
    "bias. confidence is your self-report; code clamps it to a ceiling computed from the "
    "same ledger (net score magnitude, capped under a live cross-asset P1 contradiction "
    "per §6) — audit-only beyond that clamp. "
    "\nP5 (FVG-FILL — thesis.md §2.1). The facts S9 'FVG-FILL CANDIDATES' block lists each "
    "VISITED fair-value-gap zone with an id (e.g. 'MES 1hr 2026-07-14 00:00:00-04:00 bull'). "
    "A visited zone is a fill event — usable P5 evidence. To score it, add an evidence item "
    "{criterion: P5, asset, level: <copy the zone id VERBATIM, including the bull/bear word>, "
    "tier: session|day|week, tf: 1h|4h, direction: accept|reject, mature}. direction=accept "
    "means the zone HELD its own bias (a bull zone held as support / a bear zone held as "
    "resistance); reject means it was violated (closed through). Do NOT declare UP/DOWN — code "
    "derives the sign from the bull/bear kind in the id plus accept/reject (bull-accept and "
    "bear-reject are bullish; bear-accept and bull-reject are bearish), the SAME mirrored "
    "polarity as P1. Only cite a VISITED zone; an unvisited gap is a pending draw, not a fill. "
    "Each candidate may also carry a code-computed 'stretch_since_fill=Nx avg_1h' and, past "
    "shelf life, a '[SUGGESTED EXHAUSTED]' tag (same meaning/override as the SMT one below — "
    "set exhausted: true on the item if you judge it stale, or disagree and score it "
    "normally). ADJACENT same-asset/same-kind/same-tf zones (consecutive bars — one "
    "continuous move that kept creating new gaps as it went) are auto-collapsed by code to "
    "the freshest one — citing several from one continuous move does not add weight, so "
    "prefer the single freshest zone from a run rather than listing every one. "
    "\nSMT EXHAUSTION (thesis.md §2.1c). Each S9 SMT candidate may carry a code-computed "
    "'stretch_since_fire=Nx avg_1h' and, past its tier-relative shelf life (session 2x, day "
    "4x, week 8x), a '[SUGGESTED EXHAUSTED]' tag — the divergence has already played out (price "
    "ran far from where it fired), so it is no longer live continuation evidence. This is a "
    "SUGGESTION, not automatic: if you judge such a P2 item stale you may set exhausted: true on "
    "that evidence item, which zeroes its contribution (like an immature item). You may also "
    "DISAGREE and score it normally (omit exhausted) — the suggestion is a hint from stretch, "
    "not a gate. Do not set exhausted on a fresh, un-flagged SMT. "
    "\nP3/P4 (EQUILIBRIUM & RECLAIM — thesis.md §2.1). "
    "\n- P3 (position vs. equilibrium) is FULLY AUTOMATIC as of 2026-08-02 — do NOT declare "
    "P3 items yourself. Code reads daily_mid/weekly_mid's own HTF-close verdict directly from "
    "the facts and injects the correct evidence item (asset, tier, tf, accept/reject) for you, "
    "for both assets, whenever a mature reading exists — this is a plain fact lookup, not a "
    "judgment call, so your input adds nothing and any P3 item you declare is discarded and "
    "replaced. Spend your judgment on P1/P2/P4/P5 instead. "
    "\n- P4 (reclaim / failed reclaim of a mid, HTF-confirmed) is still yours to declare — "
    "{criterion: P4, asset, level: "
    "daily_mid_high | daily_mid_low | weekly_mid_high | weekly_mid_low, tier, tf, direction: "
    "accept | reject, mature} — the _high/_low encodes the reclaim DIRECTION and accept "
    "(reclaimed and held) / reject (failed reclaim) works exactly like P1's accept/reject on a "
    "high/low. Use the HTF-close-confirmed reclaim, not a bare mid touch/cross (proven noise). "
    "P4 reuses the P1 tier/tf multipliers and the §3 maturity gate — code computes the sign and "
    "points, you only tag the reclaim. "
    "Return JSON matching the schema."
    "\n\nS9 EVIDENCE FACTS (plan 14 — read before deciding). The facts now carry an S9 block "
    "with stretch/distance, session-maturity, and cross-family confluence lines: "
    "\n- Recall: when current evidence is thin/immature, prefer a clock_after at the next "
    "sub-session boundary (menu R*) over an arbitrary minutes-elapsed count (thesis.md §1). "
    "\n- Session maturity: a thin ledger early in the session (see S9 session_elapsed_frac / "
    "mature-item count) is expected data-scarcity, not market ambiguity — let it inform "
    "confidence, do not read it as contradiction (thesis.md §2.2). "
    "\n- Pending resolution: for any IMMATURE (mature: false) day/week evidence item you cite, "
    "populate pending_resolution {resolves_tf: 1h|4h, resolves_at: <copy the matching 'next 1h "
    "close'/'next 4h close' timestamp from the S9 PENDING RESOLUTION line — do NOT compute it "
    "yourself>, implied_direction_if_confirmed: UP|DOWN (which way it leans IF that close "
    "confirms)}. It is informational (unscored), so an execution layer can read when/how the "
    "item will resolve. "
    "\n- Stretch: before declaring regime TREND, check the S9 stretch line — an undigested "
    "high-stretch TREND is an unverifiable load-bearing input (one confidence tier down, "
    "thesis.md §2.1c / §2.2 veto category). "
    "\n- Sparse structure: when the nearest-level distance is large, prefer a closer existing "
    "predicate (smaller-n daily-mid close, a nearer anti-pool) over a far default (existing "
    "predicate types only). "
    "\n- No liquidity: check the S8 DOL menu for the direction your evidence leans BEFORE "
    "declaring bias. If that direction's menu is '(none eligible)' — every named pool on "
    "that side is already swept, e.g. deep into a sustained trend that has taken out all "
    "nearby lows/highs — there is nothing left to draw to, the same situation as price "
    "beyond the all-time high with no resistance above it. Do NOT invent an off-menu DOL "
    "or reuse an already-swept level. Declare bias NEUTRAL, confidence LOW, dol null, "
    "falsified_if/exhausted_if empty (thesis.md §8) — code enforces this regardless of "
    "your evidence ledger's net score, so declaring NEUTRAL here is correct, not a hedge. "
    "\n- Nested / duplicate levels (thesis.md §2.1b/§2.1d): a swept level tagged "
    "'[nested/duplicate ...]' in the S9 close-status block is EITHER an older prevN level "
    "already superseded by a more-recent, deeper same-family level (day_low/day_high/"
    "week_low/week_high), OR a duplicate restatement of one physical sweep that another "
    "named level already covers at the identical timestamp. Do NOT declare a fresh P1 item "
    "there — code zeroes it regardless. If that same level is ALSO listed under SMT "
    "candidates, check its tag there: 'grandfathered' means the divergence fired BEFORE the "
    "level became nested (a newer level superseded it only afterward) — still valid P2 "
    "evidence, score it as P2, never as P1. 'P2-SUPPRESSED' means the level was ALREADY "
    "nested when the divergence itself fired — no evidence at all here, not P1 and not P2; "
    "code zeroes it regardless of what you declare."
    "\n- Equilibrium-stale P1 items (thesis.md §2.1c): a close-status line tagged "
    "'[SUGGESTED STALE: price has since reached equilibrium]' means price has, since this "
    "level's own sweep, already traveled all the way to (and tested) the relevant daily or "
    "weekly mid — more recent, more meaningful behavior than the original sweep. This is a "
    "suggestion, not a hard rule: you may set exhausted:true on that P1 item (same field "
    "P2 uses for a played-out SMT) if you judge the sweep no longer meaningful, or leave it "
    "scoring normally if you judge it still relevant."
    "\n- Near-maturity pre-confirmation (thesis.md §3a, a BOUNDED exception to the §3 "
    "maturity gate): the S9 'NEAR-MATURITY PRE-CONFIRMATION CANDIDATES' block lists any day/"
    "week-tier item due to close within a few minutes. You may declare mature=true for that "
    "item NOW, using its implied_direction, ONLY if its preconfirm_eligible flag is True — "
    "this already requires BOTH the current price to sit comfortably clear of the level "
    "(distance_safe) AND a second independent agreeing source with no contradiction present "
    "(corroborated: cross-asset, cross-tier, or a live P2 SMT). If preconfirm_eligible is "
    "False (even if a candidate is listed), treat the item as immature as usual — do NOT "
    "declare mature=true on it."
)
_TASK_PLAN = (
    "TASK — L2 trade-plan decision (AI-trader v2).\n"
    "Given the standing thesis below, decide whether and how to engage: return SETUP or "
    "WAIT matching the schema. A SETUP carries entry mechanisms (closed enum kinds), a "
    "direction, a stop, an exit target (a level in the facts, unswept/undepleted, on the "
    "correct side of price and BEFORE the thesis DOL), management mechanisms, structured "
    "setup_falsified_if / setup_exhausted_if predicates, and a MANDATORY on_dol_falsified "
    "action. The stop must NOT sit where a thesis falsified_if predicate would fire. A WAIT "
    "carries a recall (events + max_age_min). Return JSON matching the schema.\n\n"
    "STANDING THESIS (verbatim):\n"
)


def decide_thesis(facts_text: str, context_text: str, facts: dict, backend: Backend, *,
                  docs_root: str = DOCS_ROOT, evidence_magnitude=None) -> CallOutcome:
    """The L1 thesis call in isolation (validate-and-retry, failsafe on repeat).

    Validation is the deterministic contract validator (schemas + predicate vocabulary +
    semantic level checks + the evidence-ledger arithmetic below). The thesis carries a
    P1/P2 evidence ledger (decisions/thesis.md §2.1); code derives each item's points/sign
    and the confidence ceiling (_derive_thesis_arithmetic), and validate_thesis rejects a
    declared bias inconsistent with the computed net score (retry, not silent override —
    same split as daily-trend/next-move). P3 (daily_mid/weekly_mid position) and every
    real, non-suppressed named-level P1/P2 reading are fully code-derived from the facts,
    not model-declared (2026-08-02) — the model's remaining evidence-ledger role is
    relevance judgment on the small residual (which candidates are worth citing beyond
    what code already injects) plus an `exhausted: true` veto. P4 and the standing-
    thesis-recall confidence escalation (spec §8) remain reasoning-only / not yet
    code-derived (thesis.md §8 gap)."""
    from schemas import build_thesis_schema, failsafe_thesis
    from validate_contracts import validate_thesis
    system = build_system_prompt(docs_root)
    user = _facts_context(facts_text, context_text) + _TASK_THESIS
    valid_levels = list((facts.get("levels") or {}).keys())
    schema = build_thesis_schema(valid_levels, extra_evidence_levels=facts.get("fvg_zones"))
    menus = facts.get("menus")
    dol_available = None
    if menus is not None:
        dol_menu = menus.get("dol") or {}
        dol_available = {"UP": bool(dol_menu.get("UP")), "DOWN": bool(dol_menu.get("DOWN"))}
    suppressed_p1_levels = facts.get("suppressed_p1_levels")
    suppressed_p2_sites = facts.get("suppressed_p2_sites")
    level_htf_close_status = facts.get("level_htf_close_status")
    level_tiers = facts.get("level_tiers")
    smt_candidates = facts.get("smt_candidates")
    week_extremes = facts.get("week_extremes")
    now_price = facts.get("now_price")
    fvg_zone_meta = facts.get("fvg_zone_meta")
    return _run_call(
        backend, system, user, schema,
        validate_block=lambda d: validate_thesis(d, facts),
        failsafe_block=failsafe_thesis(),
        derive_block=lambda d: _derive_thesis_arithmetic(
            d, magnitude=evidence_magnitude, dol_available=dol_available,
            suppressed_p1_levels=suppressed_p1_levels, suppressed_p2_sites=suppressed_p2_sites,
            level_htf_close_status=level_htf_close_status, level_tiers=level_tiers,
            smt_candidates=smt_candidates, week_extremes=week_extremes, now_price=now_price,
            fvg_zone_meta=fvg_zone_meta),
    )


def decide_plan(facts_text: str, context_text: str, facts: dict, standing_thesis: dict,
                backend: Backend, *, docs_root: str = DOCS_ROOT) -> CallOutcome:
    """The L2 trade-plan call in isolation, CONSUMING a standing thesis verbatim. Validated
    against the full contract (syntactic + semantic + cross-level vs the thesis)."""
    from schemas import TRADE_PLAN_SCHEMA, failsafe_plan
    from validate_contracts import validate_trade_plan
    system = build_system_prompt(docs_root)
    standing = json.dumps(standing_thesis, indent=2, sort_keys=True, default=str)
    user = _facts_context(facts_text, context_text) + _TASK_PLAN + standing
    return _run_call(
        backend, system, user, TRADE_PLAN_SCHEMA,
        validate_block=lambda p: validate_trade_plan(p, thesis=standing_thesis, facts=facts),
        failsafe_block=failsafe_plan(),
    )


def decide(facts_text: str, context_text: str, facts: dict, backend: Backend, *,
           docs_root: str = DOCS_ROOT) -> dict:
    """The two-call daily-trend→next-move sequence + validate-and-retry, with NO file
    I/O — the importable LLM call-core (GIL-44 Phase 1, Wave 1.2).

    `facts` is the parsed fact-dict (calibration.validate_results.parse_facts output,
    or derive_facts.facts_to_validator_dict) enabling the semantic validator. Returns
    the SAME decision dict `run_cut` assembles, minus the `cut` key (the caller adds
    it): backend/model/protocol_clean/facts_checkpoint/system_prompt_bytes/daily_trend/
    next_move/calls. The decisions engine and run_cut both go through here.
    """
    daily = decide_daily(facts_text, context_text, facts, backend, docs_root=docs_root)
    nxt = decide_next(facts_text, context_text, facts, daily.block, backend,
                      docs_root=docs_root)

    protocol_clean = not daily.fallback and not nxt.fallback
    return {
        "backend": backend.name,
        "model": backend.model,
        "protocol_clean": protocol_clean,
        "facts_checkpoint": facts.get("checkpoint"),
        "system_prompt_bytes": len(build_system_prompt(docs_root).encode("utf-8")),
        "daily_trend": daily.block,
        "next_move": nxt.block,
        "calls": {
            "daily_trend": _call_audit(daily),
            "next_move": _call_audit(nxt),
        },
    }


def run_cut(cut_dir: str, backend: Backend, *, docs_root: str = DOCS_ROOT,
            write: bool = True) -> dict:
    """Run both decision calls for one cut, validate, and assemble decision.json.

    A thin cut-coupled wrapper over `decide`: reads facts.txt/context-at-cut.md,
    parses facts, delegates the two calls to `decide`, prepends the `cut` label, and
    writes decision.json.
    """
    with open(os.path.join(cut_dir, "facts.txt"), encoding="utf-8") as fh:
        facts_text = fh.read()
    context_path = os.path.join(cut_dir, "context-at-cut.md")
    context_text = ""
    if os.path.exists(context_path):
        with open(context_path, encoding="utf-8") as fh:
            context_text = fh.read()

    facts = parse_facts(facts_text)
    core = decide(facts_text, context_text, facts, backend, docs_root=docs_root)
    decision = {"cut": os.path.basename(os.path.normpath(cut_dir)), **core}

    if write:
        with open(os.path.join(cut_dir, "decision.json"), "w", encoding="utf-8") as fh:
            json.dump(decision, fh, indent=2)
    return decision


def _call_audit(o: CallOutcome) -> dict:
    return {
        "verdict": o.verdict,
        "fallback": o.fallback,
        "retries": o.retries,
        "reasoning": o.reasoning,
        "latency_total_sec": o.latency_total,
        "usage_total": o.usage_total,
        "attempts": o.attempts,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GIL-44 Phase-2 decision-agent runner")
    ap.add_argument("--cut", required=True, help="path to a calibration/cuts/<id> folder")
    ap.add_argument("--backend", choices=["openrouter", "anthropic"], default=None,
                    help="override auto-selection (default: OpenRouter if "
                         "OPENROUTER_API_KEY is set, else Anthropic)")
    ap.add_argument("--model", default=None, help="override the backend's default model")
    args = ap.parse_args(argv)

    cut_dir = os.path.abspath(args.cut)
    if not os.path.isdir(cut_dir):
        ap.error(f"--cut folder not found: {cut_dir}")

    backend = make_backend(args.backend, args.model)
    decision = run_cut(cut_dir, backend)

    d, n = decision["daily_trend"], decision["next_move"]
    cache_read = sum(
        decision["calls"][c]["usage_total"].get("cache_read_input_tokens", 0) or 0
        for c in ("daily_trend", "next_move")
    )
    print(
        f"{decision['cut']} [{decision['backend']}:{decision['model']}] "
        f"daily={d.get('direction')}/{d.get('confidence')} "
        f"next={n.get('direction')}/{n.get('confidence')} "
        f"protocol_clean={decision['protocol_clean']} "
        f"cache_read={cache_read}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
