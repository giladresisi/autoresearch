"""GIL-44 Phase-3 AI decisions engine configuration + master flag.

Flag idiom (repo standard, GIL-34/39): a module-level constant defaulting OFF with
an ACT_* env override. Default OFF ⇒ the pipeline never constructs the engine ⇒ zero
code path, zero spend, byte-identical regressions. Nothing here has side effects.

Secrets: no keys or guessable values live here — API keys come from `.env` via
run_agent.load_env_file. Env overrides are booleans/strings only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# AI-trader v2 operating mode (spec §3): off | shadow | primary, default off.
#   off      — byte-identical to the hypothesis-driven system (regression gate).
#   shadow   — the v1 observation-only decisions engine runs as today (no trades from AI).
#   primary  — the v2 loop (TradeDirector) drives entries/management.
# ACT_AI_MODE takes precedence; the legacy ACT_AI_DECISIONS=1 flag aliases to `shadow`
# so existing shadow runs keep working unchanged.
_AI_MODES = ("off", "shadow", "primary")


def resolve_ai_mode() -> str:
    m = os.environ.get("ACT_AI_MODE")
    if m is not None:
        ml = m.strip().lower()
        if ml in _AI_MODES:
            return ml
        if ml:                              # set but unrecognized (e.g. a typo "primry"):
            import warnings                 # warn (not stdout) so a typo never silently
            warnings.warn(                  # disables primary — then fall through to legacy/off.
                f"ACT_AI_MODE={m!r} is not one of {_AI_MODES}; treating as 'off'/legacy. "
                "Check for a typo (e.g. 'primary').", RuntimeWarning, stacklevel=2)
    if _env_flag("ACT_AI_DECISIONS", False):
        return "shadow"                     # legacy alias
    return "off"


AI_MODE: str = resolve_ai_mode()

# Master flag: True while the v1 observation engine (shadow) should be constructed. Kept
# for backward compatibility with existing call sites (default OFF ⇒ byte-identical).
AI_DECISIONS_ENABLED: bool = (AI_MODE == "shadow")
# The v2 primary loop construction gate.
AI_PRIMARY_ENABLED: bool = (AI_MODE == "primary")

# Phase-0 measured mean of the two-call cycle (min 66.1 / mean 78.7 / max 106.8 s over
# the 5 calibration decision.json audits) → rounded to 79. arrival = trigger + this.
DECISION_LATENCY_SEC: float = 79.0

# Compare mode (False) records a datapoint at EVERY trigger; production-faithful (True)
# suppresses a re-decision when no new fresh event arrived since the last call.
DECISIONS_CHURN_GUARD: bool = _env_flag("ACT_AI_DECISIONS_CHURN_GUARD", False)

# Daily-trend checkpoint cadence, ET (decisions/daily-trend.md §1). Each fires once per
# session the first time it is crossed.
DECISIONS_CHECKPOINTS_ET = ("06:00", "09:20", "13:00", "18:00")

# None → run_agent auto-selects the backend from .env (OpenRouter if OPENROUTER_API_KEY,
# else Anthropic). Model None → backend default (claude-haiku-4.5).
DECISIONS_BACKEND = os.environ.get("ACT_AI_DECISIONS_BACKEND") or None
DECISIONS_MODEL = os.environ.get("ACT_AI_DECISIONS_MODEL") or None

# True → real API; False → offline stub backend (tests/CI, no keys, no spend). Env
# ACT_AI_DECISIONS_REAL_API=0 forces stub for a whole run without a code change.
DECISIONS_REAL_API: bool = _env_flag("ACT_AI_DECISIONS_REAL_API", True)

# Snapshots larger than this many chars are stored by reference (file+hash) in the audit
# rather than inlined (records.py).
SNAPSHOT_INLINE_MAX_CHARS = 4000

# Bounded session-end teardown budget (seconds): the live dispatcher's worker.close() drains
# at most this long before abandoning a wedged in-flight LLM call, so session close (position
# / limit cleanup) is never delayed by the AI worker. Numeric env override only, no secrets.
DECISIONS_SHUTDOWN_TIMEOUT: float = float(
    os.environ.get("ACT_AI_DECISIONS_SHUTDOWN_TIMEOUT", "8.0"))


@dataclass
class DecisionsConfig:
    """A snapshot of the module-level knobs, so an engine instance is self-contained
    and a test can override a field without mutating global module state."""

    enabled: bool = AI_DECISIONS_ENABLED
    mode: str = AI_MODE
    latency_sec: float = DECISION_LATENCY_SEC
    churn_guard: bool = DECISIONS_CHURN_GUARD
    checkpoints_et: tuple = DECISIONS_CHECKPOINTS_ET
    backend: object = DECISIONS_BACKEND
    model: object = DECISIONS_MODEL
    real_api: bool = DECISIONS_REAL_API
    snapshot_inline_max_chars: int = SNAPSHOT_INLINE_MAX_CHARS
    shutdown_timeout: float = DECISIONS_SHUTDOWN_TIMEOUT


def default_config() -> DecisionsConfig:
    """Build a DecisionsConfig from the current module-level values (re-reads env each
    call would require a reload; construct explicitly in tests for overrides)."""
    return DecisionsConfig()
