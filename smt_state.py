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

DATA_DIR        = Path("data")
GLOBAL_PATH     = DATA_DIR / "global.json"
DAILY_PATH      = DATA_DIR / "daily.json"
HYPOTHESIS_PATH = DATA_DIR / "hypothesis.json"
POSITION_PATH   = DATA_DIR / "position.json"

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
    "confirmation_bar": {},
    "failed_entries": 0,
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, payload: dict) -> None:
    if _IN_MEMORY:
        _STORE[str(path)] = copy.deepcopy(payload)
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
            return copy.deepcopy(default)
        return copy.deepcopy(d)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)
    if not default.keys() <= d.keys():
        return copy.deepcopy(default)
    return d


def load_global() -> dict:
    return _load(GLOBAL_PATH, DEFAULT_GLOBAL)


def save_global(d: dict) -> None:
    _atomic_write(GLOBAL_PATH, d)


def load_daily() -> dict:
    return _load(DAILY_PATH, DEFAULT_DAILY)


def save_daily(d: dict) -> None:
    _atomic_write(DAILY_PATH, d)


def load_hypothesis() -> dict:
    global _hyp_cache, _hyp_cache_valid
    if not _IN_MEMORY and _hyp_cache_valid and _hyp_cache is not None:
        return copy.deepcopy(_hyp_cache)
    result = _load(HYPOTHESIS_PATH, DEFAULT_HYPOTHESIS)
    if not _IN_MEMORY:
        _hyp_cache = copy.deepcopy(result)
        _hyp_cache_valid = True
    return result


def save_hypothesis(d: dict) -> None:
    global _hyp_cache, _hyp_cache_valid
    _atomic_write(HYPOTHESIS_PATH, d)
    if not _IN_MEMORY:
        _hyp_cache = copy.deepcopy(d)
        _hyp_cache_valid = True  # write-through: cache the new value immediately


def load_position() -> dict:
    return _load(POSITION_PATH, DEFAULT_POSITION)


def save_position(d: dict) -> None:
    _atomic_write(POSITION_PATH, d)


def bar_state_path(date_str: str | None = None) -> Path:
    import datetime as _dt
    d = date_str or _dt.date.today().isoformat()
    return Path("sessions") / d / "bar_state.json"


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
