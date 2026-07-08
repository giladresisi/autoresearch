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
for _p in (HERE, _CALIB_DIR):
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
    """Offline, key-free backend for tests/CI and SHADOW_REAL_API=False.

    With no `responses` it returns a DETERMINISTIC schema-valid NEUTRAL/LOW block
    (the fail-safe shape) for whichever schema it is handed — so the shadow module
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
        fs = failsafe_decision()
        props = schema.get("properties", {})
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
# Validate-and-retry — the trust boundary                                      #
# --------------------------------------------------------------------------- #
def _retry_prompt(violations: list[str]) -> str:
    bullet = "\n".join(f"  - {m}" for m in violations)
    return (
        "Your previous JSON decision failed deterministic validation with these "
        "protocol violations:\n"
        f"{bullet}\n\n"
        "Correct the violations, then RE-DERIVE every field that depends on what you "
        "changed: direction must follow the corrected N against the +/-3 gate; "
        "confidence must respect its ceiling; a directional call must carry a "
        "move_target that exists in the facts, is unswept/undepleted, and sits on the "
        "correct side of current price. Keep unrelated fields identical and return a "
        "fresh, complete JSON decision matching the schema."
    )


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
              max_retries: int = MAX_RETRIES) -> CallOutcome:
    """One decision call with a bounded correction loop.

    validate_block(parsed_block) -> ValidationResult scoped to THIS call. On a
    validation failure we echo the model's own (invalid) JSON back and quote the
    exact violations; after max_retries failures we fall back to the NEUTRAL/LOW
    scripts baseline and record it.
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
        result = validate_block(parsed)
        attempts.append({
            "usage": resp.usage,
            "latency_sec": round(latency, 3),
            "model": resp.model,
            "ok": result.ok,
            "violations": result.messages(),
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

    Returns the CallOutcome (block + audit). The shadow engine calls this at each
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
    )


def decide_next(facts_text: str, context_text: str, facts: dict, standing_daily: dict,
                backend: Backend, *, docs_root: str = DOCS_ROOT) -> CallOutcome:
    """The next-move call in isolation, CONSUMING a standing daily-trend verbatim.

    The shadow engine calls this at each hypothesis trigger with the standing
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
    )


def decide(facts_text: str, context_text: str, facts: dict, backend: Backend, *,
           docs_root: str = DOCS_ROOT) -> dict:
    """The two-call daily-trend→next-move sequence + validate-and-retry, with NO file
    I/O — the importable LLM call-core (GIL-44 Phase 1, Wave 1.2).

    `facts` is the parsed fact-dict (calibration.validate_results.parse_facts output,
    or derive_facts.facts_to_validator_dict) enabling the semantic validator. Returns
    the SAME decision dict `run_cut` assembles, minus the `cut` key (the caller adds
    it): backend/model/protocol_clean/facts_checkpoint/system_prompt_bytes/daily_trend/
    next_move/calls. The shadow module and run_cut both go through here.
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
