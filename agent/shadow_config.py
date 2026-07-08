"""GIL-44 Phase-3 AI shadow-decisions configuration + master flag.

Flag idiom (repo standard, GIL-34/39): a module-level constant defaulting OFF with
an ACT_* env override. Default OFF ⇒ the pipeline never constructs the shadow ⇒ zero
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


# Master flag: env ACT_AI_SHADOW=1 overrides the default-OFF constant.
AI_SHADOW_ENABLED: bool = _env_flag("ACT_AI_SHADOW", False)

# Phase-0 measured mean of the two-call cycle (min 66.1 / mean 78.7 / max 106.8 s over
# the 5 calibration decision.json audits) → rounded to 79. arrival = trigger + this.
SHADOW_DECISION_LATENCY_SEC: float = 79.0

# Compare mode (False) records a datapoint at EVERY trigger; production-faithful (True)
# suppresses a re-decision when no new fresh event arrived since the last call.
SHADOW_CHURN_GUARD: bool = _env_flag("ACT_AI_SHADOW_CHURN_GUARD", False)

# Daily-trend checkpoint cadence, ET (decisions/daily-trend.md §1). Each fires once per
# session the first time it is crossed.
SHADOW_CHECKPOINTS_ET = ("06:00", "09:20", "13:00", "18:00")

# None → run_agent auto-selects the backend from .env (OpenRouter if OPENROUTER_API_KEY,
# else Anthropic). Model None → backend default (claude-haiku-4.5).
SHADOW_BACKEND = os.environ.get("ACT_AI_SHADOW_BACKEND") or None
SHADOW_MODEL = os.environ.get("ACT_AI_SHADOW_MODEL") or None

# Content-addressed replay cache, shared across runs (deterministic re-replays, no spend).
SHADOW_REPLAY_CACHE_DIR = os.path.join(_HERE, "shadow_cache")

# True → real API; False → offline stub backend (tests/CI, no keys, no spend). Env
# ACT_AI_SHADOW_REAL_API=0 forces stub for a whole run without a code change.
SHADOW_REAL_API: bool = _env_flag("ACT_AI_SHADOW_REAL_API", True)

# Snapshots larger than this many chars are stored by reference (file+hash) in the audit
# rather than inlined (records.py).
SNAPSHOT_INLINE_MAX_CHARS = 4000


@dataclass
class ShadowConfig:
    """A snapshot of the module-level knobs, so an engine instance is self-contained
    and a test can override a field without mutating global module state."""

    enabled: bool = AI_SHADOW_ENABLED
    latency_sec: float = SHADOW_DECISION_LATENCY_SEC
    churn_guard: bool = SHADOW_CHURN_GUARD
    checkpoints_et: tuple = SHADOW_CHECKPOINTS_ET
    backend: object = SHADOW_BACKEND
    model: object = SHADOW_MODEL
    cache_dir: str = SHADOW_REPLAY_CACHE_DIR
    real_api: bool = SHADOW_REAL_API
    snapshot_inline_max_chars: int = SNAPSHOT_INLINE_MAX_CHARS


def default_config() -> ShadowConfig:
    """Build a ShadowConfig from the current module-level values (re-reads env each
    call would require a reload; construct explicitly in tests for overrides)."""
    return ShadowConfig()
