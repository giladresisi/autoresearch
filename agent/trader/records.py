"""Structured decision records — "where I would have entered", written to disk.

Double duty: realtime visibility while cycle 1 places no orders, and the artifact that
lets cycle-2 regression cases be DATA FILES (an expected `trader_decisions.jsonl`
sequence) instead of bespoke test code.

Every record carries `kind`, `time` (BAR time, ISO), `plan_id`, `mechanism`, and —
whenever an artifact is involved — `artifact_id` AND `artifact_label` together. The
content-derived id alone is unreadable six weeks later; the label alone is not an
identifier. They always travel as a pair.

The file is `trader_decisions.jsonl`. Never `events.jsonl` — that is the legacy stream
the regression diffs line-for-line against locked baselines.
"""
from __future__ import annotations

import json
import os

DECISIONS_FILE = "trader_decisions.jsonl"


def _iso(ts):
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def announce(record: dict) -> None:
    """Realtime stdout line for a decision.

    Cycle 1 places no orders, so this print IS the "where I would have entered"
    notification. The ProcessManager relay captures stdout into `signals.log`, so
    trader decisions land there time-ordered ALONGSIDE the legacy engine's own emit
    lines -- which is the entire point of running both in shadow.

    Prefixed `[TRADER]` and deliberately NOT bare JSON, so nothing that parses the
    legacy bare-JSON emit lines is disturbed. Best-effort: a broken stdout must never
    reach the bar loop.
    """
    try:
        bits = ["[TRADER] %s %s" % (record.get("time"), record.get("kind"))]
        for key in ("mechanism", "artifact_label", "trigger", "stop", "dol",
                    "reason", "detail", "plan_id"):
            val = record.get(key)
            if val not in (None, ""):
                bits.append("%s=%s" % (key, val))
        print(" ".join(bits), flush=True)
    except Exception:
        pass


class DecisionRecorder:
    """Append-only, one JSON object per line. Every write is best-effort: a recorder
    failure must never propagate into the bar loop."""

    def __init__(self, state_dir) -> None:
        self.state_dir = str(state_dir)

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, DECISIONS_FILE)

    def _write(self, record: dict) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass
        announce(record)

    def _base(self, kind: str, now, plan_id, mechanism) -> dict:
        return {"kind": kind, "time": _iso(now), "plan_id": plan_id,
                "mechanism": mechanism}

    # -- record kinds ---------------------------------------------------------- #

    def intended_entry(self, *, now, plan_id, mechanism, artifact_id, artifact_label,
                       trigger, stop, dol, **extra) -> None:
        rec = self._base("intended_entry", now, plan_id, mechanism)
        rec.update({"artifact_id": artifact_id, "artifact_label": artifact_label,
                    "trigger": trigger, "stop": stop, "dol": dol})
        rec.update(extra)
        self._write(rec)

    def veto(self, *, now, plan_id, mechanism, reason, detail=None, artifact_id=None,
             artifact_label=None) -> None:
        rec = self._base("veto", now, plan_id, mechanism)
        rec.update({"reason": reason, "detail": detail or {},
                    "artifact_id": artifact_id, "artifact_label": artifact_label})
        self._write(rec)

    def bind(self, *, now, plan_id, mechanism, artifact_id, artifact_label, **extra) -> None:
        rec = self._base("bind", now, plan_id, mechanism)
        rec.update({"artifact_id": artifact_id, "artifact_label": artifact_label})
        rec.update(extra)
        self._write(rec)

    def unbind(self, *, now, plan_id, mechanism, artifact_id=None, artifact_label=None,
               reason=None) -> None:
        rec = self._base("unbind", now, plan_id, mechanism)
        rec.update({"artifact_id": artifact_id, "artifact_label": artifact_label,
                    "reason": reason})
        self._write(rec)

    def plan_dead(self, *, now, plan_id, reason, detail=None) -> None:
        rec = self._base("plan_dead", now, plan_id, None)
        rec.update({"reason": reason, "detail": detail or {}})
        self._write(rec)
