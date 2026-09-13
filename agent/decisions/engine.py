"""Decision core + standing daily-trend mini-orchestrator (GIL-44 Phase 2, Wave 2.3).

The DecisionEngine runs the two-call state machine ALONGSIDE the hypothesis engine:
  - on_checkpoint  → the daily-trend call over checkpoint-truncated facts; stores the
                     STANDING daily-trend (fires once per configured ET checkpoint).
  - on_hypothesis_trigger → the next-move call consuming the standing daily-trend, at the
                     same trigger a `new-hypothesis` fired; returns a DecisionRecord with the
                     paired diff vs the hypothesis event.

It never mutates strategy state and never raises into the pipeline: any exception /
guard-kill / repeated-invalid yields a NEUTRAL/LOW failsafe record with the reason.
"""

from __future__ import annotations

import datetime
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from run_agent import decide_daily, decide_next, failsafe_decision  # noqa: E402
from derive_facts import TZ, trade_date  # noqa: E402

from facts_adapter import Snapshot, build_snapshot  # noqa: E402
from guard import LookaheadError, assert_no_lookahead  # noqa: E402
from records import (  # noqa: E402
    DecisionRecord,
    build_ai_daily_trend_event,
    build_ai_hypothesis_event,
    build_paired_diff,
    snapshot_ref,
    write_audit_record,
)


@dataclass
class _Decision:
    """A CallOutcome flattened to a serialisable form (audit payload)."""

    block: dict
    verdict: str
    fallback: bool
    retries: int = 0
    reasoning: Optional[str] = None
    attempts: list = field(default_factory=list)
    latency_total_sec: float = 0.0
    usage_total: dict = field(default_factory=dict)

    @classmethod
    def from_outcome(cls, o) -> "_Decision":
        return cls(block=o.block, verdict=o.verdict, fallback=o.fallback,
                   retries=o.retries, reasoning=o.reasoning, attempts=o.attempts,
                   latency_total_sec=o.latency_total, usage_total=o.usage_total)


class DecisionEngine:
    def __init__(self, config, backend, docs_root, out_dir):
        self.config = config
        self.backend = backend
        self.docs_root = docs_root
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.out_dir / "ai_decisions_audit.jsonl"
        self.snapshots_dir = self.out_dir / "snapshots"

        self.standing_daily_trend: Optional[dict] = None
        self._standing_meta: dict = {}
        self._fired_checkpoints: set = set()
        self._session_first_ts: Optional[pd.Timestamp] = None
        self._last_now: Optional[pd.Timestamp] = None
        self._last_trigger_hash: Optional[str] = None

        self.records: list = []
        self.events_native: list = []
        self.stats = {"api_calls": 0, "guard_kills": 0,
                      "failsafes": 0, "checkpoints": 0, "triggers": 0}

    # ------------------------------------------------------------------ #
    # Checkpoint cadence                                                   #
    # ------------------------------------------------------------------ #
    def _checkpoint_timestamps(self, now: pd.Timestamp):
        d = trade_date(now)
        out = []
        for hm in self.config.checkpoints_et:
            h, m = (int(x) for x in hm.split(":"))
            base = d - datetime.timedelta(days=1) if h >= 18 else d
            ts = pd.Timestamp(datetime.datetime(base.year, base.month, base.day, h, m),
                              tz=TZ)
            out.append((hm, ts))
        return out

    def _session_first(self, frames) -> Optional[pd.Timestamp]:
        f = frames.get("mnq_today")
        if f is not None and len(f):
            return f.index[0]
        return None

    def on_checkpoint(self, now: pd.Timestamp, frames: dict) -> None:
        """Called each 1m bar; fires the daily-trend decision the first time each
        configured checkpoint is crossed by real session data (idempotent per
        checkpoint). A checkpoint that predates the session's first bar is marked
        fired-skipped (data started after it — never crossed)."""
        if self._session_first_ts is None:
            self._session_first_ts = self._session_first(frames)
        prev = self._last_now
        self._last_now = now
        for hm, ts in self._checkpoint_timestamps(now):
            if hm in self._fired_checkpoints or ts > now:
                continue
            crossed = prev is None or prev < ts
            if not crossed:
                continue
            sess_first = self._session_first_ts
            if sess_first is not None and ts < sess_first:
                self._fired_checkpoints.add(hm)      # missed — data started after it
                continue
            self._fired_checkpoints.add(hm)
            self._run_checkpoint(now, frames, hm, ts)

    def _safe_snapshot(self, frames, checkpoint=None) -> Snapshot:
        """build_snapshot but guaranteed not to raise — a snapshot-build failure becomes
        a degraded snapshot (→ failsafe record), honouring the engine's never-raise
        contract even on the live-wrapper path."""
        try:
            return build_snapshot(frames, checkpoint=checkpoint)
        except Exception as exc:
            now = checkpoint if checkpoint is not None else frames.get("now")
            return Snapshot(text="", validator_dict={}, content_hash="<snapshot-error>",
                            max_ts=now, now=now, checkpoint=checkpoint, degraded=True,
                            error=f"snapshot:{type(exc).__name__}:{exc}")

    def _run_checkpoint(self, now, frames, hm, ts) -> None:
        self.stats["checkpoints"] += 1
        trigger_kind = f"checkpoint:{hm}"
        fs = failsafe_decision()

        error = None
        try:
            snap = self._safe_snapshot(frames, checkpoint=ts)
            if snap.degraded:
                dec = _Decision(block=fs["daily_trend"], verdict="failsafe", fallback=True)
                error = snap.error
            else:
                assert_no_lookahead(snap, ts)
                dec = self._daily_decision(snap)
        except LookaheadError:
            self.stats["guard_kills"] += 1
            dec = _Decision(block=fs["daily_trend"], verdict="guard-kill", fallback=True)
            error = "lookahead"
        except Exception as exc:                          # never raise into the pipeline
            dec = _Decision(block=fs["daily_trend"], verdict="failsafe", fallback=True)
            error = f"{type(exc).__name__}:{exc}"

        if dec.fallback:
            self.stats["failsafes"] += 1
        self.standing_daily_trend = dec.block
        self._standing_meta = {"checkpoint": str(ts), "content_hash": snap.content_hash}

        arrival = ts + pd.Timedelta(seconds=self.config.latency_sec)
        event = build_ai_daily_trend_event(dec.block, time_iso=arrival.isoformat())
        self.events_native.append(event)
        audit = self._audit(trigger_ts=ts, arrival_ts=arrival, trigger_kind=trigger_kind,
                            snap=snap, daily=dec, nxt=None, hyp_event=None,
                            paired_diff=None, error=error)
        write_audit_record(self.audit_path, audit)

    # ------------------------------------------------------------------ #
    # Hypothesis trigger                                                   #
    # ------------------------------------------------------------------ #
    def on_hypothesis_trigger(self, now: pd.Timestamp, frames: dict, hyp_event: dict,
                              trigger_kind: str) -> DecisionRecord:
        self.stats["triggers"] += 1
        fs = failsafe_decision()
        snap = self._safe_snapshot(frames)          # never raises → contract honoured
        standing = self.standing_daily_trend or fs["daily_trend"]  # neutral default

        # Churn guard (production-faithful): no fresh facts since the last trigger →
        # return the standing decision, no new API call.
        if (self.config.churn_guard and self._last_trigger_hash == snap.content_hash
                and self.records):
            prev = self.records[-1]
            suppressed = DecisionRecord(
                trigger_ts=now.isoformat(),
                arrival_ts=(now + pd.Timedelta(seconds=self.config.latency_sec)).isoformat(),
                trigger_kind=trigger_kind, facts_content_hash=snap.content_hash,
                decision=prev.decision, paired_diff=prev.paired_diff,
                verdict="churn-suppressed", fallback=False,
                standing_daily_trend=standing, audit={})
            self.records.append(suppressed)
            return suppressed
        self._last_trigger_hash = snap.content_hash

        error = None
        try:
            if snap.degraded:
                dec = _Decision(block=fs["next_move"], verdict="failsafe", fallback=True)
                error = snap.error
            else:
                assert_no_lookahead(snap, now)
                dec = self._next_decision(snap, standing)
        except LookaheadError:
            self.stats["guard_kills"] += 1
            dec = _Decision(block=fs["next_move"], verdict="guard-kill", fallback=True)
            error = "lookahead"
        except Exception as exc:                          # never raise into the pipeline
            dec = _Decision(block=fs["next_move"], verdict="failsafe", fallback=True)
            error = f"{type(exc).__name__}:{exc}"

        if dec.fallback:
            self.stats["failsafes"] += 1

        arrival = now + pd.Timedelta(seconds=self.config.latency_sec)
        paired = build_paired_diff(hyp_event, dec.block, standing)
        now_price = (snap.validator_dict or {}).get("now_price")
        self.events_native.append(build_ai_hypothesis_event(
            dec.block, time_iso=arrival.isoformat(), hyp_event=hyp_event,
            now_price=now_price))
        audit = self._audit(trigger_ts=now, arrival_ts=arrival, trigger_kind=trigger_kind,
                            snap=snap, daily=None, nxt=dec, hyp_event=hyp_event,
                            paired_diff=paired, error=error, standing=standing)
        write_audit_record(self.audit_path, audit)

        record = DecisionRecord(
            trigger_ts=now.isoformat(), arrival_ts=arrival.isoformat(),
            trigger_kind=trigger_kind, facts_content_hash=snap.content_hash,
            decision={"daily_trend": standing, "next_move": dec.block},
            paired_diff=paired, verdict=dec.verdict, fallback=dec.fallback, error=error,
            standing_daily_trend=standing, audit=audit)
        self.records.append(record)
        return record

    # ------------------------------------------------------------------ #
    # Decision calls                                                       #
    # ------------------------------------------------------------------ #
    def _daily_decision(self, snap) -> _Decision:
        outcome = decide_daily(snap.text, "", snap.validator_dict, self.backend,
                               docs_root=self.docs_root)
        self.stats["api_calls"] += 1
        return _Decision.from_outcome(outcome)

    def _next_decision(self, snap, standing) -> _Decision:
        outcome = decide_next(snap.text, "", snap.validator_dict, standing, self.backend,
                              docs_root=self.docs_root)
        self.stats["api_calls"] += 1
        return _Decision.from_outcome(outcome)

    # ------------------------------------------------------------------ #
    # Audit assembly                                                       #
    # ------------------------------------------------------------------ #
    def _audit(self, *, trigger_ts, arrival_ts, trigger_kind, snap, daily, nxt,
               hyp_event, paired_diff, error, standing=None) -> dict:
        primary = nxt if nxt is not None else daily
        decision = {}
        if daily is not None:
            decision["daily_trend"] = daily.block
        if standing is not None and daily is None:
            decision["daily_trend"] = standing
        if nxt is not None:
            decision["next_move"] = nxt.block
        return {
            "trigger_ts": str(trigger_ts),
            "arrival_ts": str(arrival_ts),
            "trigger_kind": trigger_kind,
            "facts_content_hash": snap.content_hash,
            "facts_snapshot_ref": snapshot_ref(
                snap.text, snap.content_hash, self.snapshots_dir,
                self.config.snapshot_inline_max_chars),
            "standing_daily_trend_superseded": self._standing_meta or None,
            "attempts": primary.attempts if primary else [],
            "latency_total_sec": primary.latency_total_sec if primary else 0.0,
            "usage_total": primary.usage_total if primary else {},
            "reasoning": primary.reasoning if primary else None,
            "decision": decision,
            "paired_diff": paired_diff,
            "verdict": primary.verdict if primary else "failsafe",
            "fallback": primary.fallback if primary else True,
            "error": error,
            "outcome": None,          # filled by the Phase-4 annotation job
        }

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #
    def flush(self) -> None:
        """Records are written eagerly per call; nothing buffered. Present for the
        interface contract (and future batched writers)."""
        return None

    def finalize(self, session_parquet=None) -> None:
        """Day-end hook. Phase-4 annotation (annotate.py) fills each record's outcome
        from post-arrival bars when a session parquet is provided; absent that, this is
        a clean no-op so a short/interrupted day flushes without dangling records."""
        if session_parquet is None:
            return
        try:
            from annotate import annotate_session  # noqa: E402  (Phase-4 module)
        except ImportError:
            return
        annotate_session(self.audit_path, session_parquet)
