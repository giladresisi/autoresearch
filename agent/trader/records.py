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
                    # The order lifecycle's own numbers. Without them a `fill` /
                    # `stop_out` / `take_profit` / `mark` line reaches stdout with no
                    # price on it, and the session's P&L is unreadable from the log
                    # alone -- recoverable only by parsing the JSONL afterwards.
                    "direction", "entry", "price",
                    "reason", "predicate", "detail", "plan_id"):
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

    def order_event(self, *, now, plan_id, mechanism, kind, **extra) -> None:
        """A simulated order-lifecycle event: `fill`, `stop_out`, `take_profit`.

        The event's own `time` is dropped in favour of `now`: they are the same bar
        instant, and `_base` already renders it in the ISO form every other record uses.
        """
        rec = self._base(str(kind), now, plan_id, mechanism)
        extra.pop("time", None)
        rec.update({k: (_iso(v) if hasattr(v, "isoformat") else v)
                    for k, v in extra.items()})
        self._write(rec)

    def would_have_falsified(self, *, now, plan_id, predicate, detail=None) -> None:
        """The thesis falsifier fired. Recorded, NOT acted on — the plan lives.

        Its only purpose is to answer a question nobody can answer today: what would
        killing the plan here have cost or saved? Because the future rule under
        consideration is kill-the-plan, the FIRE TIME alone reconstructs the
        counterfactual — everything the plan did afterwards is what it would have
        forgone. That is why this is a timestamp and a predicate rather than a flag.

        Emitted once per plan (Executor holds a one-shot latch): a falsifier that stays
        true would otherwise repeat on every bar close for the rest of the session.
        """
        self._write({**self._base("would_have_falsified", now, plan_id, None),
                     "predicate": predicate, "detail": detail})

    def target_selected(self, *, now, plan_id, mechanism, pick, **extra) -> None:
        """The T2 target chosen at a fill — `build_menus`'s D1, re-anchored at that
        instant (`agent/trader/target.py`).

        Written on EVERY fill, including when the menu came back empty (`pick=None`).
        That case is a real outcome, not an error — §5 of `l2-target-selection.md`
        measures an empty direction menu on 6.0% of sessions — and a fill that silently
        carried no target would otherwise be indistinguishable in the artifact from one
        whose target simply never filled.
        """
        rec = self._base("target_selected", now, plan_id, mechanism)
        rec.update({"pick": pick,
                    "target": (pick or {}).get("price") if isinstance(pick, dict) else None,
                    "level": (pick or {}).get("level") if isinstance(pick, dict) else None})
        rec.update(extra)
        self._write(rec)

    def would_have_vetoed(self, *, now, plan_id, mechanism, reason, detail=None,
                          artifact_id=None, artifact_label=None) -> None:
        """A veto that no longer vetoes. Recorded, NOT acted on.

        Same standing as `would_have_falsified`: plan 16 made the DOL inert, so
        `dol_floor` keeps being COMPUTED and recorded — the counterfactual is only
        recoverable if the fire time is on disk — while the entry proceeds. Kept
        distinct from `veto` so nothing downstream (`report_replay_pnl.summarize` counts
        `veto` records) reads an inert observation as a refusal.
        """
        rec = self._base("would_have_vetoed", now, plan_id, mechanism)
        rec.update({"reason": reason, "detail": detail or {},
                    "artifact_id": artifact_id, "artifact_label": artifact_label})
        self._write(rec)

    def would_have_killed(self, *, now, plan_id, reason, detail=None) -> None:
        """A plan-death condition that no longer kills. Recorded, NOT acted on.

        `dol_reached` used to end the session the moment price touched the 09:20 DOL.
        Plan 16 removed that: the DOL is no longer the target, so dying on it was both
        an entry effect and incoherent. Emitted ONCE per plan — the condition stays true
        for the rest of the session, so re-recording it every bar would bury the file.
        """
        rec = self._base("would_have_killed", now, plan_id, None)
        rec.update({"reason": reason, "detail": detail or {}})
        self._write(rec)

    def plan_dead(self, *, now, plan_id, reason, detail=None) -> None:
        rec = self._base("plan_dead", now, plan_id, None)
        rec.update({"reason": reason, "detail": detail or {}})
        self._write(rec)

    def external_kill(self, *, now, plan_id, reason, void_position, detail=None) -> None:
        """Something OUTSIDE the chain changed the position, and the chain stood down.

        Live only. Written by `TraderGraft.external_kill` AFTER the plan is dead and the
        modelled position voided, and attempted even if the void raised — the record is
        the only evidence of why the rest of the session is empty."""
        rec = self._base("external_kill", now, plan_id, None)
        rec.update({"reason": reason, "void_position": bool(void_position),
                    "detail": detail or {}})
        self._write(rec)

    def session_disarmed(self, *, now, reason, plan_id=None, detail=None) -> None:
        """The chain will do nothing more this session. Live only: a restart that found
        a plan already on disk, or a disarm ordered from outside after a trader error."""
        rec = self._base("session_disarmed", now, plan_id, None)
        rec.update({"reason": reason, "detail": detail or {}})
        self._write(rec)
