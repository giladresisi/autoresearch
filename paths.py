# paths.py
# Single source of truth for the project's runtime file locations.
#
# Why this exists: production parquets, live-session logs, strategy state JSONs and
# regression outputs used to be hardcoded relative to the worktree CWD. That made many
# agents running backtests in parallel worktrees collide with each other and with the
# live orchestrator's file writes (Windows [WinError 5] rename-over-open failures).
# Every base dir here is env-overridable (mirroring the existing FUTURES_CACHE_DIR
# pattern) so each context — live, backtest, per-run — gets an isolated location.
#
# Import-cheap by design: only os / pathlib / datetime / zoneinfo. Do NOT import heavy
# project modules here; many cheap modules import paths at load time.

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# TH (Asia/Bangkok) is the project's session-naming timezone — reused from session_times.
_TH = ZoneInfo("Asia/Bangkok")
# Naive datetimes are interpreted as ET (the repo's wall-clock convention) before TH conversion.
_ET = ZoneInfo("America/New_York")


def _ensure(p: Path) -> Path:
    """mkdir -p the directory and return it. Every getter funnels through here so a
    freshly-overridden env var or state dir is materialized on first use."""
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Global root + its children (production data + live sessions live machine-global)
# ---------------------------------------------------------------------------

def global_root() -> Path:
    """Machine-global root holding production data and live sessions, shared across all
    worktrees. Env `ACT_GLOBAL_DIR`, default `~/projects/auto-co-trader/global`."""
    root = os.environ.get("ACT_GLOBAL_DIR")
    p = Path(root).expanduser() if root else Path("~/projects/auto-co-trader/global").expanduser()
    return _ensure(p)


def general_live_dir() -> Path:
    """Live orchestrator's general persistent folder (`<global>/general/live`).

    Holds the live parquet append target AND the cross-session live artifacts that must
    persist outside any single session folder: `global.json` (dynamic all_time_high) and
    the manual-pause sentinel. The live writer and backtest readers never share a file —
    this is the writer's side. (Formerly `<global>/data/live`.)"""
    return _ensure(global_root() / "general" / "live")


def general_main_dir() -> Path:
    """Backtest parquet read source (`<global>/general/main`). `parquet-check` promotes
    validated parquets live -> main after a successful post-session run.
    (Formerly `<global>/data/main`.)"""
    return _ensure(global_root() / "general" / "main")


def sessions_dir() -> Path:
    """Live-session log root (`<global>/sessions`), visible to every worktree."""
    return _ensure(global_root() / "sessions")


# ---------------------------------------------------------------------------
# Regression outputs — worktree-local (gitignored), per-run subfolders
# ---------------------------------------------------------------------------

def regression_dir() -> Path:
    """Worktree-root regression root. Env `ACT_REGRESSION_DIR`, default `<cwd>/regression`.
    Worktree-local on purpose so parallel agents' runs don't mix. The root holds tracked
    tooling (e.g. plot_regression.py); the machine-local per-date run folders live under
    regression_sessions_dir() (gitignored)."""
    root = os.environ.get("ACT_REGRESSION_DIR")
    p = Path(root).expanduser() if root else Path.cwd() / "regression"
    return _ensure(p)


def regression_sessions_dir() -> Path:
    """Per-date regression run folders live under `<regression>/sessions/` so the
    regression/ root can keep committed tooling while these date folders stay gitignored."""
    return _ensure(regression_dir() / "sessions")


def regression_run_dir(date: str, started: datetime) -> Path:
    """Per-run output folder `<regression>/sessions/<date>/<HH-MM-SS TH>`.

    `started` is converted to TH (Asia/Bangkok) and formatted HH-MM-SS — the same
    timezone the rest of the project names sessions by (session_times.cme_session_date).
    A naive `started` is interpreted as ET to match the repo's wall-clock convention.
    """
    if started.tzinfo is None:
        started = started.replace(tzinfo=_ET)
    stamp = started.astimezone(_TH).strftime("%H-%M-%S")
    return _ensure(regression_sessions_dir() / date / stamp)


# ---------------------------------------------------------------------------
# State-dir prefix — the per-context location for the four strategy state JSONs.
# Settable (mirroring smt_state.set_session_date): live points it at the session
# folder, backtest at the per-run folder. Defaults to legacy `data/` so behavior is
# unchanged until callers opt in (Wave 4).
# ---------------------------------------------------------------------------

_STATE_DIR: Path = Path("data")


def set_state_dir(path) -> None:
    """Set the prefix under which strategy state JSONs resolve. Takes effect on the next
    state read/write (smt_state resolves paths under state_dir() at call time)."""
    global _STATE_DIR
    _STATE_DIR = Path(path)


def state_dir() -> Path:
    """Current state-JSON prefix (default legacy `data/`). Directory is ensured so a
    freshly-set session/run dir exists before the first state write."""
    return _ensure(_STATE_DIR)
