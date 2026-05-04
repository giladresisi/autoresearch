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

DEFAULT_GLOBAL = {"all_time_high": 0.0, "trend": "up"}

DEFAULT_DAILY = {
    "date": "",
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
    "limit_entry": "",
    "confirmation_bar": {},
    "failed_entries": 0,
}

# ---------------------------------------------------------------------------
# In-memory mode (used by backtests to skip disk I/O)
# ---------------------------------------------------------------------------
_IN_MEMORY = False
_STORE: dict[str, dict] = {}


def set_in_memory_mode(enabled: bool) -> None:
    global _IN_MEMORY
    _IN_MEMORY = enabled
    if not enabled:
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
    return _load(HYPOTHESIS_PATH, DEFAULT_HYPOTHESIS)


def save_hypothesis(d: dict) -> None:
    _atomic_write(HYPOTHESIS_PATH, d)


def load_position() -> dict:
    return _load(POSITION_PATH, DEFAULT_POSITION)


def save_position(d: dict) -> None:
    _atomic_write(POSITION_PATH, d)
