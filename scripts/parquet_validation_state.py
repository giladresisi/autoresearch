# scripts/parquet_validation_state.py
# Persist + interpret the parquet validation watermark (a sidecar JSON store).
# Exists so check_session_parquets can validate only the appended tail instead of
# rescanning the entire multi-hundred-thousand-row main parquet body every run (GIL-15).
from __future__ import annotations

import json
import os
from pathlib import Path

# Bump whenever validation rules change semantics, to force a one-time full
# re-scan of every parquet (a version mismatch invalidates existing watermarks).
VALIDATOR_VERSION: int = 1


def load_state(state_path: Path) -> dict:
    """Read the sidecar JSON; return {} if missing OR unreadable/corrupt.

    Fail-safe: any read/parse error is treated as "no state" so the caller falls
    back to a full validation (more validation, never less). Never raises.
    """
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_watermark(state: dict, parquet_name: str) -> dict | None:
    """Return the per-parquet entry dict, or None if not present."""
    entry = state.get(parquet_name)
    return entry if isinstance(entry, dict) else None


def set_watermark(
    state_path: Path,
    parquet_name: str,
    *,
    validated_through: str,
    validated_rows: int,
    first_bar: str,
) -> None:
    """Update one parquet's watermark entry and write the sidecar back atomically.

    Loads existing state first so other parquets' entries are preserved, stamps
    the current VALIDATOR_VERSION, then writes to a .tmp sibling and os.replace()s
    it into place (atomic — no partially-written sidecar is ever observed).
    """
    state = load_state(state_path)
    state[parquet_name] = {
        "validator_version": VALIDATOR_VERSION,
        "validated_through": validated_through,
        "validated_rows": validated_rows,
        "first_bar": first_bar,
    }
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path)


def needs_full_validation(
    entry: dict | None,
    *,
    first_bar: str | None,
    row_count: int,
) -> tuple[bool, str]:
    """Pure decision (no I/O): does this parquet need a full re-validation?

    Returns (True, reason) for any condition that breaks the append-only
    assumption, else (False, "") when a tail-only incremental check is safe.
    """
    if entry is None:
        return True, "no-watermark"
    if entry["validator_version"] != VALIDATOR_VERSION:
        return True, "version-bump"
    if first_bar is None or entry["first_bar"] != first_bar:
        return True, "body-rewritten"
    if row_count < entry["validated_rows"]:
        return True, "truncation"
    return False, ""
