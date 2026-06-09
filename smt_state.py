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

# daily/hypothesis/position resolve under paths.state_dir() AT CALL TIME, so a mid-run
# paths.set_state_dir(...) takes effect immediately: live points the prefix at the
# session folder, a backtest at its per-run folder. Functions (not constants) are the
# whole point — a captured constant would freeze the prefix at import time.
def _daily_path() -> Path:      return paths.state_dir() / "daily.json"
def _hypothesis_path() -> Path: return paths.state_dir() / "hypothesis.json"
def _position_path() -> Path:   return paths.state_dir() / "position.json"
def _smts_path() -> Path:       return paths.state_dir() / "smts.json"


def _global_path() -> Path:
    """global.json is the exception to the per-session state_dir rule.

    It carries the dynamic all_time_high, which must persist ACROSS sessions. In LIVE it
    therefore lives in the stable general live folder (paths.general_live_dir()), not the
    per-session state_dir — so the ATH is simply read back each session with no
    prior-session seeding needed. In BACKTEST (in-memory) it stays under state_dir() so
    each per-run/per-date folder is isolated and final_snapshot() captures it (and the
    in-memory store stays keyed per run). _IN_MEMORY is the live-vs-backtest discriminator."""
    if _IN_MEMORY:
        return paths.state_dir() / "global.json"
    return paths.general_live_dir() / "global.json"

def _session_folder_date() -> str:
    """ET session date naming the per-session folder (sessions/<date>) for files that may
    be written by separate processes (bar_state.json, the pause sentinel). ET — not naive
    local — so a standalone trade.py on a clock running ahead of ET doesn't pick tomorrow's
    folder once the local date rolls but the ET session date has not. _SESSION_DATE wins."""
    import datetime as _dt
    import zoneinfo as _zi
    return _SESSION_DATE or _dt.datetime.now(_zi.ZoneInfo("America/New_York")).date().isoformat()


# Manual entry-pause sentinel (trade.py pause/resume). Lives in the general live folder
# (<global>/general/live) alongside global.json, NOT under state_dir() or a per-session
# folder: it is a manual cross-process flag (trade.py writes it, the orchestrator reads
# it), so a single fixed location makes both processes agree by construction, without
# either having to set state_dir() or compute the session date. Because it is no longer
# per-session, a pause now PERSISTS across sessions/restarts until explicitly resumed.
def pause_path() -> Path:
    return paths.general_live_dir() / "paused"

# Session date locked at startup (ET date, YYYY-MM-DD). Set via set_session_date().
_SESSION_DATE: str = ""


def set_session_date(d: str) -> None:
    global _SESSION_DATE
    _SESSION_DATE = d


def ensure_live_state_dir() -> None:
    """Point state_dir at the live session folder when no caller has set it.

    position/daily/hypothesis are CROSS-PROCESS files in live: the orchestrator pipeline
    sets state_dir to sessions/<date> explicitly (session_pipeline.on_session_start), but
    a standalone process (trade.py, an ad-hoc `import live_orders` REPL) that never called
    paths.set_state_dir silently read/wrote the legacy worktree-local data/ — a DIFFERENT
    position.json than the one the orchestrator manages. Incident 2026-06-05 04:21: a
    manual close cleared stop_entry in the wrong file, so the session copy kept a stale
    stop_entry with no broker counterpart until bar-based fill-detection confirmed it
    into a phantom position.

    Resolution mirrors session_pipeline: ACT_STATE_DIR env wins (the orchestrator hands
    it to its subprocess), else sessions/<CME trade date> — the same folder naming the
    orchestrator uses (_SESSION_DATE when locked, else session_times.session_date_str).
    No-op in in-memory (backtest) mode or once state_dir points anywhere non-default,
    so explicit callers (backtests, the pipeline) keep full control."""
    if _IN_MEMORY or not paths.state_dir_is_default():
        return
    env = os.environ.get("ACT_STATE_DIR")
    if env:
        paths.set_state_dir(env)
        return
    from session_times import session_date_str
    paths.set_state_dir(paths.sessions_dir() / (_SESSION_DATE or session_date_str()))

DEFAULT_GLOBAL = {"all_time_high": 0.0, "confidence": "medium", "trend": "up"}

DEFAULT_DAILY = {
    "formed_at": "",
    "liquidities": [],
    # Additive MES counterpart of `liquidities` (SMT V2). Same structure
    # (session/day/week levels + 1hr FVGs). The MNQ `liquidities` key is unchanged.
    "liquidities_mes": [],
    "estimated_dir": "up",
    "opposite_premove": "no",
}

# SMT V2 detection store: per-target edge/re-arm state + the reference consumer's
# retained set. Mirrors the daily/hypothesis/position load/save + _IN_MEMORY pattern.
DEFAULT_SMTS = {
    "detect_state": {},
    "watch": {"retained": []},
}

DEFAULT_HYPOTHESIS = {
    "direction":     "none",
    # GIL-8 manual direction lock (trade.py set-direction): while True, the automatic
    # hypothesis resets (level sweep, ATH cross, mid-cross / global-trend trend-broken)
    # are suspended. Invariant: manual=True implies direction != "none" — every path
    # that clears direction also clears this flag.
    "manual":        False,
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
    "cautious_dist_shrinks": 0,
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
    for name in ("global.json", "daily.json", "hypothesis.json", "position.json", "smts.json"):
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


def load_smts() -> dict:
    return _load(_smts_path(), DEFAULT_SMTS)


def save_smts(d: dict) -> None:
    _atomic_write(_smts_path(), d)


def is_paused() -> bool:
    """True if a manual entry pause is in effect (the session-folder `paused` sentinel exists).

    Always False in in-memory (backtest) mode — pause is a live-execution control and must
    never affect backtests, which never create the sentinel anyway.
    """
    if _IN_MEMORY:
        return False
    return pause_path().exists()


def bar_state_path(date_str: str | None = None) -> Path:
    # ET session date (see _session_folder_date) — matches the orchestrator's session folder
    # and the pause sentinel; a naive-local date could pick the wrong day's folder for a
    # standalone process (e.g. trade.py) once the local date rolls but the ET session has not.
    d = date_str or _session_folder_date()
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
