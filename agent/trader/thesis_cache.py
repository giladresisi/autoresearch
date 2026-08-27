"""Content-addressed cache of L1 theses.

**Why this exists.** A real L1 call is not deterministic — five identical calls at one
boundary produced three directional theses and two NEUTRALs, with two different DOLs. A
replay that calls the model therefore cannot be replay-vs-replay deterministic, and an
A/B across two Executor variants would be comparing model noise. Freezing the thesis
makes the Executor the only variable: the cache is an EXPERIMENTAL CONTROL, not a cost
optimisation.

**Why the key hashes the prompt.** Everything that determines a thesis — the KB docs, the
task prompt, the JSON schema, the facts, the boundary, the model — is exactly the bytes
that would be sent. Hashing those captures all of it and cannot drift out of sync with
what actually happens, unlike enumerating source files to hash.

**Where it lives.** `<global>/thesis_cache/`, never the worktree: it survives worktree
deletion, and sharing it across worktrees is safe precisely because the content key
already separates code versions.

**Known precedent.** Plan 7 built a replay cache keyed on (trigger, facts-hash) and
commit bb493e1 REMOVED it because "docs will change often — every run re-samples". That
is expected here too and is fine: frequent invalidation is the correct behaviour when the
Analyzer changes. Entries are never overwritten in place, so a KB change produces a new
key and the old recording stays readable for comparison.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

CACHE_ENV = "ACT_THESIS_CACHE_DIR"
KEY_LEN = 16


def cache_root() -> Path:
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override).expanduser()
    from paths import global_root
    return global_root() / "thesis_cache"


def thesis_key(*, boundary, facts_text, context_text, system_prompt,
               task_prompt, schema, model_id) -> str:
    """16-hex content hash over everything that determines the thesis.

    Fields are length-prefixed before joining so that moving bytes across a boundary
    (a shorter facts_text plus a longer task_prompt) cannot collide.
    """
    parts = [
        str(boundary or ""),
        str(facts_text or ""),
        str(context_text or ""),
        str(system_prompt or ""),
        str(task_prompt or ""),
        json.dumps(schema or {}, sort_keys=True, default=str),
        str(model_id or ""),
    ]
    h = hashlib.sha256()
    for p in parts:
        b = p.encode("utf-8")
        h.update(str(len(b)).encode("ascii"))
        h.update(b"\x00")
        h.update(b)
    return h.hexdigest()[:KEY_LEN]


class ThesisCache:
    """One JSON file per key. Reads degrade to a miss; writes are atomic."""

    def __init__(self, root=None) -> None:
        self.root = Path(root) if root is not None else cache_root()

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str):
        record = self.get_record(key)
        return record.get("thesis") if record else None

    def get_record(self, key: str):
        """The whole stored blob (thesis + meta + boundary), or None on a miss.

        `get` alone made the recorded meta WRITE-ONLY: a warm replay served the thesis but
        reported `call_meta = {}`, so the "latency and tokens are recorded on both paths"
        claim held only for the seeding run. The provenance of a served recording is the
        provenance of the call that produced it, so the hit path replays it.
        """
        try:
            p = self.path_for(key)
            if not p.exists():
                return None
            blob = json.loads(p.read_text(encoding="utf-8"))
            thesis = blob.get("thesis")
            return blob if isinstance(thesis, dict) else None
        except Exception:
            return None                      # corrupt entry => miss => the run re-calls

    def put(self, key: str, thesis: dict, *, meta=None, boundary=None) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            blob = {"key": key, "boundary": str(boundary) if boundary else None,
                    "thesis": thesis, "meta": meta or {}}
            fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".thesis_",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(blob, fh, default=str)
                os.replace(tmp, self.path_for(key))
            except Exception:
                # A serialisation failure would otherwise strand the temp file in the
                # shared cache root forever, once per failed put.
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception:
            pass

    def clear(self) -> int:
        n = 0
        try:
            for p in self.root.glob("*.json"):
                p.unlink()
                n += 1
        except Exception:
            pass
        return n
