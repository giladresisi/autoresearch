"""Append-only JSONL journal + atomic periodic snapshot for the facts layer.

Facts are RECOMPUTABLE from bars, so durability here is an optimization, not a
correctness requirement (unlike execution state). The journal exists for after-the-fact
explanation: every record carries the content-derived `id` AND the human `label`
together, so a fact can be followed across state changes without decoding a hash.

Writes go to `facts_journal.jsonl` / `facts_snapshot.json`. It must NEVER be
`events.jsonl` — that is the legacy strategy stream the regression diffs line-for-line
against locked baselines.
"""
from __future__ import annotations

import json
import os
import tempfile

import pandas as pd

from agent.facts.records import Fact
from agent.facts.store import FactStore

FACTS_JOURNAL_NAME = "facts_journal.jsonl"
FACTS_SNAPSHOT_NAME = "facts_snapshot.json"


def _iso(ts):
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _atomic_write(path: str, text: str) -> None:
    """temp-file + os.replace, mirroring smt_state._atomic_write including the Windows
    PermissionError fallback (a reader holding the target blocks the rename)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".facts_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows: target held open by a reader. Retry once, then fall back to an
            # in-place rewrite rather than leaving the good snapshot destroyed.
            try:
                os.replace(tmp, path)
            except PermissionError:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.unlink(tmp)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        raise


class Journal:
    """One journal per state directory. All writes are best-effort — a journal failure
    must never propagate into the bar loop."""

    def __init__(self, state_dir) -> None:
        self.state_dir = str(state_dir)

    # -- paths --------------------------------------------------------------- #

    @property
    def journal_path(self) -> str:
        return os.path.join(self.state_dir, FACTS_JOURNAL_NAME)

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.state_dir, FACTS_SNAPSHOT_NAME)

    # -- events -------------------------------------------------------------- #

    def _append(self, record: dict) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    def fact_created(self, fact: Fact) -> None:
        self._append({"event": "fact_created", "id": fact.id, "label": fact.label,
                      "cls": fact.cls.value, "ticker": fact.ticker,
                      "reference_ts": _iso(fact.reference_ts), "state": fact.state.value})

    def fact_state_changed(self, fact: Fact, old) -> None:
        self._append({"event": "fact_state_changed", "id": fact.id, "label": fact.label,
                      "from": getattr(old, "value", old), "to": fact.state.value,
                      "at": _iso(fact.state_ts)})

    def coverage_extended(self, cls, ticker: str, new_from) -> None:
        self._append({"event": "coverage_extended", "id": f"{getattr(cls, 'value', cls)}:{ticker}",
                      "label": f"{ticker} {getattr(cls, 'value', cls)} coverage",
                      "cls": getattr(cls, "value", cls), "ticker": ticker,
                      "covered_from": _iso(new_from)})

    # -- snapshot / restore ---------------------------------------------------- #

    def snapshot(self, store: FactStore) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            _atomic_write(self.snapshot_path,
                          json.dumps(store.to_dict(), default=str, indent=None))
        except Exception:
            pass

    def restore(self) -> "FactStore | None":
        try:
            if not os.path.exists(self.snapshot_path):
                return None
            with open(self.snapshot_path, encoding="utf-8") as fh:
                return FactStore.from_dict(json.load(fh))
        except Exception:
            return None
