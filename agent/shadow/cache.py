"""Content-addressed replay cache (GIL-44 Phase 2, Wave 2.4).

Keyed by (trigger_kind, content_hash) → the cached raw decision payload, so re-running
the same regression day replays deterministically with NO API spend and NO re-sampling
(the model shows run-to-run judgment variance — the cache freezes ONE draw per trigger,
caveat C2). A cache MISS on an unchanged trigger is itself a drift signal: the facts
changed, so the hash changed — the engine logs that.

Writes are atomic (temp file in the same dir + os.replace) so two processes writing the
same key never leave a half-written file; a corrupt/partial entry is treated as a miss,
never a crash.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional


def _safe_component(text: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(text))


class ReplayCache:
    def __init__(self, cache_dir):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, trigger_kind: str, content_hash: str) -> Path:
        return self.dir / f"{_safe_component(trigger_kind)}__{_safe_component(content_hash)}.json"

    def get(self, trigger_kind: str, content_hash: str) -> Optional[dict]:
        """Return the cached payload, or None on miss / corrupt entry."""
        path = self._path(trigger_kind, content_hash)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt/partial file → treat as a miss (the caller re-computes + overwrites).
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, trigger_kind: str, content_hash: str, payload: dict) -> None:
        """Atomically write the payload for (trigger_kind, content_hash)."""
        path = self._path(trigger_kind, content_hash)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)          # atomic on POSIX and Windows
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
