"""Shared AI-decisions flag + worker factory (GIL-44 Phase-3, slice 2).

The single construction site for the decisions engine/worker, imported by BOTH the
backtest (`backtest_smt.py`) and the live dispatcher (`automation/main.py`), so the live
process never imports the heavy backtest module (OD-5/H5). Default OFF ⇒ callers never
call these ⇒ zero worker thread, zero spend, byte-identical backtest + live.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)


def _ensure_path() -> None:
    for _p in (_HERE, _AGENT):
        if _p not in sys.path:
            sys.path.insert(0, _p)


def ai_decisions_enabled() -> bool:
    """Read the AI-decisions master flag. decisions_config only imports os/dataclasses, so
    this is cheap and side-effect-free; default OFF keeps regressions byte-identical."""
    try:
        _ensure_path()
        import decisions_config as _sc
        return bool(_sc.AI_DECISIONS_ENABLED)
    except Exception:
        # Fall back to the raw env read so a decisions_config import problem can never
        # flip the flag on (or break a flag-OFF regression).
        val = os.environ.get("ACT_AI_DECISIONS")
        return val is not None and val.strip().lower() in ("1", "true", "yes", "on")


def build_decision_engine(out_dir):
    """Construct a DecisionEngine for one run, using the stub backend unless real_api is on.
    Only called when the flag is ON, so the AI-decisions stack is imported lazily. Raises on
    failure — CALLERS degrade to None (the trading run is never aborted by an AI-decisions
    init problem)."""
    _ensure_path()
    import decisions_config as _sc
    from decisions.engine import DecisionEngine
    from run_agent import make_backend, DOCS_ROOT

    config = _sc.DecisionsConfig()
    backend = (make_backend("stub") if not config.real_api
               else make_backend(config.backend, config.model))
    return DecisionEngine(config, backend, DOCS_ROOT, out_dir=str(out_dir))


def build_decision_worker(out_dir):
    """Wrap a fresh DecisionEngine in the async DecisionWorker (THE execution model in both
    modes). Raises on engine-build failure — CALLERS catch and degrade to None."""
    _ensure_path()          # async_wrapper's own imports need agent/decisions on sys.path
    from decisions.async_wrapper import DecisionWorker
    return DecisionWorker(build_decision_engine(out_dir))


def ai_primary_enabled() -> bool:
    """Read the v2 primary-mode gate (ACT_AI_MODE=primary). Default OFF ⇒ callers never
    build the v2 stack ⇒ zero code path, byte-identical backtest + live."""
    try:
        _ensure_path()
        import decisions_config as _sc
        return bool(_sc.AI_PRIMARY_ENABLED)
    except Exception:
        return os.environ.get("ACT_AI_MODE", "").strip().lower() == "primary"


def build_primary_runner(out_dir, *, date: str = "", threaded: bool = False,
                         confidence_fn=None):
    """Construct the v2 PrimaryRunner (bus + async DecisionService + TradeDirector +
    mechanism adapter). Stub backend unless real_api is on. Only called when ACT_AI_MODE=
    primary, so the v2 stack is imported lazily. Raises on failure — CALLERS degrade to None
    (the trading run is never aborted by an AI init problem)."""
    _ensure_path()
    for _p in (_AGENT, os.path.join(_AGENT, "executor")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    import decisions_config as _sc
    from run_agent import make_backend
    from executor.primary_runner import PrimaryRunner

    config = _sc.DecisionsConfig()
    backend = (make_backend("stub") if not config.real_api
               else make_backend(config.backend, config.model))
    if confidence_fn is None:
        try:
            from confidence import confidence as confidence_fn  # Phase-5 gate (if present)
        except Exception:
            confidence_fn = None
    return PrimaryRunner(out_dir, backend, date=date, confidence_fn=confidence_fn,
                         latency_sec=config.latency_sec, threaded=threaded)
