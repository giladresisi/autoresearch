# smt_state.py
# JSON load/save for the four SMT v2 state files: global, daily, hypothesis, position.
# Pure IO utility — no business logic. Atomic writes; returns deep-copied defaults on
# missing or schema-mismatched files so callers never mutate the default constants.
#
# In-memory mode: call set_in_memory_mode(True) to skip all disk I/O and keep state
# in a process-level dict. Used by run_backtest_v2 to avoid Windows file-locking issues.

import copy
import json
import os
from pathlib import Path

import paths


def _fast_copy(obj):
    """Deep-copy a JSON-shaped structure ~5-8x faster than copy.deepcopy.

    State here is always JSON-serializable (it is written to disk via json.dumps),
    so we only need to recurse through dict/list/tuple and treat immutable scalars
    as shareable. Unknown types fall back to copy.deepcopy for safety.
    """
    t = type(obj)
    # Scalars are the majority of calls (leaf values) — check them first.
    if t is str or t is int or t is float or t is bool or obj is None:
        return obj
    if t is dict:
        return {k: _fast_copy(v) for k, v in obj.items()}
    if t is list:
        return [_fast_copy(v) for v in obj]
    if t is tuple:
        return tuple(_fast_copy(v) for v in obj)
    return copy.deepcopy(obj)

# The four state files resolve under paths.state_dir() AT CALL TIME, so a mid-run
# paths.set_state_dir(...) takes effect immediately: live points the prefix at the
# session folder, a backtest at its per-run folder. Functions (not constants) are the
# whole point — a captured constant would freeze the prefix at import time.
def _global_path() -> Path:     return paths.state_dir() / "global.json"
def _daily_path() -> Path:      return paths.state_dir() / "daily.json"
def _hypothesis_path() -> Path: return paths.state_dir() / "hypothesis.json"
def _position_path() -> Path:   return paths.state_dir() / "position.json"

# Manual entry-pause sentinel (trade.py pause/resume). Unlike the four state JSONs above,
# this is a manual cross-process control flag (not per-session strategy state), so it
# stays at the legacy worktree-local data/ path rather than moving under state_dir().
# DATA_DIR is retained because is_paused() reconstructs the path from it dynamically.
DATA_DIR        = Path("data")
PAUSE_PATH      = DATA_DIR / "paused"   # manual entry-pause sentinel (trade.py pause/resume)

# Session date locked at startup (ET date, YYYY-MM-DD). Set via set_session_date().
_SESSION_DATE: str = ""


def set_session_date(d: str) -> None:
    global _SESSION_DATE
    _SESSION_DATE = d

DEFAULT_GLOBAL = {"all_time_high": 0.0, "confidence": "medium", "trend": "up"}

DEFAULT_DAILY = {
    "formed_at": "",
    "liquidities": [],
    "estimated_dir": "up",
    "opposite_premove": "no",
}

DEFAULT_HYPOTHESIS = {
    "direction":     "none",
    "weekly_mid":    "",
    "daily_mid":     "",
    "last_liquidity": "",
    "divs":          [],
    "targets":       [],
    "cautious_price": "",
    "entry_ranges":  [],
}

DEFAULT_POSITION = {
    "active": {},
    "stop_entry": "",
    "stop_direction": "",
    "conf_bar_entry": {},
    "conf_bar_exit":  {},
    "pending_stop": None,
    "failed_entries": 0,
    "session_mid_crosses": 0,
}

# ---------------------------------------------------------------------------
# In-memory mode (used by backtests to skip disk I/O)
# ---------------------------------------------------------------------------
_IN_MEMORY = False
_STORE: dict[str, dict] = {}

# Process-local hypothesis cache (invalidated on every save_hypothesis call).
# Not used in _IN_MEMORY mode; not used for position (externally mutated by executor).
_hyp_cache: dict | None = None
_hyp_cache_valid: bool = False


def set_in_memory_mode(enabled: bool) -> None:
    global _IN_MEMORY, _hyp_cache, _hyp_cache_valid
    _IN_MEMORY = enabled
    _hyp_cache = None
    _hyp_cache_valid = False
    _STORE.clear()


def reset_in_memory() -> None:
    """Clear the in-memory store + hypothesis cache without toggling the mode flag.

    Called at the start of each backtest run/date so a fresh state_dir starts from a
    clean slate and never inherits the previous run's _STORE entries.
    """
    global _hyp_cache, _hyp_cache_valid
    _hyp_cache = None
    _hyp_cache_valid = False
    _STORE.clear()


def seed_global_from_prior() -> None:
    """Live only: carry all_time_high forward across the now per-session state folders.

    Each live session's global.json starts fresh, so without this the dynamic ATH would
    reset every session. Scans the most recent prior session's global.json under
    paths.sessions_dir() and seeds the current session's ATH if higher. No-op in
    in-memory (backtest) mode — backtests must stay deterministic/isolated. Never raises.
    """
    if _IN_MEMORY:
        return
    try:
        cur = paths.state_dir().resolve()
        root = paths.sessions_dir()
        best = 0.0
        if root.exists():
            for child in root.iterdir():
                if not child.is_dir() or child.resolve() == cur:
                    continue
                gp = child / "global.json"
                if not gp.exists():
                    continue
                try:
                    ath = float(json.loads(gp.read_text(encoding="utf-8")).get("all_time_high", 0.0) or 0.0)
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    continue
                best = max(best, ath)
        if best > 0.0:
            g = load_global()
            if best > float(g.get("all_time_high", 0.0) or 0.0):
                g["all_time_high"] = best
                save_global(g)
    except Exception:
        return


def final_snapshot() -> None:
    """Dump the four state files for the current state_dir() to disk as real JSON.

    Used by backtests (which run in-memory) to leave one inspectable snapshot of the
    final state in the per-run folder. No-op for files never written this run.
    """
    target = paths.state_dir()
    for name in ("global.json", "daily.json", "hypothesis.json", "position.json"):
        data = _STORE.get(str(target / name))
        if data is None:
            continue
        (target / name).write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, payload: dict) -> None:
    if _IN_MEMORY:
        _STORE[str(path)] = _fast_copy(payload)
        return
    text = json.dumps(payload, indent=2, sort_keys=True)
    tmp = path.with_suffix(".writing")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except (PermissionError, FileNotFoundError):
        path.write_text(text, encoding="utf-8")


def _load(path: Path, default: dict) -> dict:
    if _IN_MEMORY:
        d = _STORE.get(str(path))
        if d is None:
            return _fast_copy(default)
        return _fast_copy(d)
    if not path.exists():
        return _fast_copy(default)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _fast_copy(default)
    # Completely wrong schema (no recognized keys) → return default.
    if not (default.keys() & d.keys()):
        return _fast_copy(default)
    # Forward-compatible merge: new default keys get their default value;
    # extra keys in the file (e.g. cooldowns) are preserved.
    merged = _fast_copy(default)
    merged.update(d)
    return merged


def load_global() -> dict:
    return _load(_global_path(), DEFAULT_GLOBAL)


def save_global(d: dict) -> None:
    _atomic_write(_global_path(), d)


def load_daily() -> dict:
    return _load(_daily_path(), DEFAULT_DAILY)


def save_daily(d: dict) -> None:
    _atomic_write(_daily_path(), d)


def load_hypothesis() -> dict:
    global _hyp_cache, _hyp_cache_valid
    if not _IN_MEMORY and _hyp_cache_valid and _hyp_cache is not None:
        return _fast_copy(_hyp_cache)
    result = _load(_hypothesis_path(), DEFAULT_HYPOTHESIS)
    if not _IN_MEMORY:
        _hyp_cache = _fast_copy(result)
        _hyp_cache_valid = True
    return result


def save_hypothesis(d: dict) -> None:
    global _hyp_cache, _hyp_cache_valid
    _atomic_write(_hypothesis_path(), d)
    if not _IN_MEMORY:
        _hyp_cache = _fast_copy(d)
        _hyp_cache_valid = True  # write-through: cache the new value immediately


def load_position() -> dict:
    return _load(_position_path(), DEFAULT_POSITION)


def save_position(d: dict) -> None:
    _atomic_write(_position_path(), d)


def is_paused() -> bool:
    """True if a manual entry pause is in effect (the data/paused sentinel exists).

    Always False in in-memory (backtest) mode — pause is a live-execution control and must
    never affect backtests, which never create the sentinel anyway.
    """
    if _IN_MEMORY:
        return False
    # Resolve from DATA_DIR dynamically so test fixtures that redirect DATA_DIR to a tmp dir
    # (and never create the sentinel) automatically see "not paused".
    return (DATA_DIR / "paused").exists()


def bar_state_path(date_str: str | None = None) -> Path:
    import datetime as _dt
    import zoneinfo as _zi
    # Resolve to the ET date (matching the ET-named session folders and live_orders),
    # NOT the naive local date. A standalone process (e.g. trade.py) on a machine whose
    # local clock runs ahead of ET would otherwise pick the wrong day's folder once the
    # local date has rolled over but the ET session date has not (after ~13:00 ET for a
    # UTC+7 clock) — which made an ad-hoc close read no bar_state and log price 0.0.
    d = date_str or _SESSION_DATE or _dt.datetime.now(_zi.ZoneInfo("America/New_York")).date().isoformat()
    return paths.sessions_dir() / d / "bar_state.json"


def save_bar_state(data: dict, date_str: str | None = None) -> None:
    path = bar_state_path(date_str)
    if not _IN_MEMORY:
        path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)


def load_bar_state(date_str: str | None = None) -> dict | None:
    path = bar_state_path(date_str)
    if _IN_MEMORY:
        return _STORE.get(str(path))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
