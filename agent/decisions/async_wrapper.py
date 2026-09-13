"""Async worker wrapper for the AI decisions engine (observation-only mode).

`prod-agent.md` Phase 3 "respects the contention prerequisite": on the 16 GB live box the
1s loop must NOT block on the ~75 s LLM cycle. The wrapper puts the SAME DecisionEngine
behind a single worker thread — on_hypothesis_trigger/on_checkpoint enqueue a job and
return immediately; the worker computes off the loop.

Staleness guard: when a trigger job completes, if its triggering hypothesis is no longer
standing (a newer new-hypothesis / trend-broken supervened), the record is still written
but FLAGGED discarded (logged, not comparable as live-actionable). Real asynchrony is the
same as the backtest's latency emulation — arrival is when the worker finishes; the
frozen-at-T facts are identical.

Recommendation (D4): land the worker/queue + staleness interface here with fake-clock /
gated-backend unit tests; the supervised live smoke stays deferred behind its own gate
(live API contention on the trading box is a separate operational check).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from records import read_audit_records


def _hyp_id(hyp_event: Optional[dict]) -> str:
    he = hyp_event or {}
    return f"{he.get('time')}|{he.get('direction')}"


def _as_ts(v):
    """Parse a logical time (pd.Timestamp / ISO string) to a comparable Timestamp, or
    None if it isn't a real timestamp (fake-clock unit tests use opaque strings)."""
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return v
    try:
        return pd.Timestamp(v)
    except Exception:
        return None


class DecisionWorker:
    """Single-worker async front end for a DecisionEngine. The submit_* calls are
    non-blocking; the worker drains the queue off the caller's thread."""

    def __init__(self, engine):
        self.engine = engine
        self._queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._current_hyp_id: Optional[str] = None
        self.records: list = []
        self.discarded: list = []
        self.errors: list = []
        # Deterministic supersede timeline (D-B(4)/D-D): every submit and supersede appends
        # its logical `(now, hyp_id)` boundary here so finalize can recompute staleness from
        # the arrival-vs-supersede timeline independent of wall-clock worker completion order.
        self._submit_log: list = []
        self._supersede_log: list = []
        self._thread = threading.Thread(target=self._run, name="ai-decisions-worker", daemon=True)
        self._thread.start()

    # -- read-through props the backtest/live callers need ------------------ #
    @property
    def events_native(self):
        return self.engine.events_native

    @property
    def audit_path(self):
        return self.engine.audit_path

    # -- non-blocking submit API (called from the 1s loop) ------------------ #
    def submit_hypothesis_trigger(self, now, frames, hyp_event, trigger_kind,
                                  hyp_id: Optional[str] = None) -> None:
        hid = hyp_id or _hyp_id(hyp_event)
        self._current_hyp_id = hid          # this hypothesis is now the standing one
        self._submit_log.append((now, hid))  # its own supersede boundary (D-D)
        self._queue.put(("trigger", now, frames, hyp_event, trigger_kind, hid))

    def submit_checkpoint(self, now, frames) -> None:
        self._queue.put(("checkpoint", now, frames))

    def note_hypothesis_superseded(self, new_hyp_id: Optional[str], now=None) -> None:
        """A newer new-hypothesis/trend-broken supervened; any in-flight job for the old
        hypothesis will be flagged discarded when it completes. `now` (logical time) is
        recorded into the deterministic supersede timeline used by finalize (D-D); the
        live wall-clock `_current_hyp_id` behaviour is unchanged."""
        self._current_hyp_id = new_hyp_id
        self._supersede_log.append((now, new_hyp_id))

    # -- worker ------------------------------------------------------------- #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except Exception as exc:                     # worker MUST survive
                self.errors.append(f"{type(exc).__name__}:{exc}")
            finally:
                self._queue.task_done()

    def _process(self, job) -> None:
        if job[0] == "checkpoint":
            _, now, frames = job
            self.engine.on_checkpoint(now, frames)
            return
        _, now, frames, hyp_event, kind, hid = job
        rec = self.engine.on_hypothesis_trigger(now, frames, hyp_event, kind)
        if rec is None:                      # only fakes return None; keep errors clean
            return
        rec._hyp_id = hid                    # own hypothesis id for the finalize recompute
        # Staleness: a newer hypothesis supervened while this job was in flight.
        if hid != self._current_hyp_id:
            rec.error = ((rec.error + "; ") if rec.error else "") + "discarded-stale"
            self.discarded.append(rec)
        self.records.append(rec)

    # -- lifecycle ---------------------------------------------------------- #
    def drain(self, timeout: Optional[float] = None) -> bool:
        """Block until the queue is empty (all submitted jobs processed). With a
        `timeout`, return once the deadline passes even if a job is still wedged (an LLM
        call can hang on the live box) — returns True if fully drained, else False."""
        if timeout is None:
            self._queue.join()
            return True
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def shutdown(self, timeout: float = 5.0) -> None:
        """Drain (bounded), stop the worker, and join the thread — never blocks past
        ~timeout even if a job is wedged."""
        self.drain(timeout=timeout)
        self._stop.set()
        self._thread.join(timeout=timeout)

    # -- deterministic finalize (both modes) -------------------------------- #
    @staticmethod
    def _strip_stale(err: Optional[str]) -> Optional[str]:
        if not err:
            return err
        parts = [p for p in err.split("; ") if p != "discarded-stale"]
        return "; ".join(parts) if parts else None

    def _recompute_discarded(self) -> None:
        """Recompute each trigger record's `discarded-stale` flag deterministically from the
        arrival-vs-supersede timeline (D-B(4)), overriding the wall-clock flagging done at
        completion. A record is stale iff a boundary (from `_submit_log ∪ _supersede_log`)
        for a *different* hypothesis has logical time strictly after the record's `trigger_ts`
        and at or before its `arrival_ts`. This is the `prod-agent.md:84-85` arrival model and
        makes the backtest and live agree regardless of worker completion order (H1)."""
        boundaries = [(_as_ts(n), h) for n, h in (self._submit_log + self._supersede_log)]
        boundaries = [(ts, h) for ts, h in boundaries if ts is not None]

        discarded: list = []
        for rec in self.records:
            trig = _as_ts(rec.trigger_ts)
            arr = _as_ts(rec.arrival_ts)
            if trig is None or arr is None:
                # Unparseable logical times (fake-clock tests): the recompute can't run —
                # keep the wall-clock verdict instead of silently stripping it (N1).
                if rec.error and "discarded-stale" in rec.error and rec not in discarded:
                    discarded.append(rec)
                continue
            rec.error = self._strip_stale(rec.error)
            rec_hid = getattr(rec, "_hyp_id", None)   # the record's own hypothesis
            stale = any(trig < b_ts <= arr and b_hid != rec_hid
                        for b_ts, b_hid in boundaries)
            if stale:
                rec.error = ((rec.error + "; ") if rec.error else "") + "discarded-stale"
                discarded.append(rec)
        self.discarded = discarded
        # Persisted-artifact key (normalised ts, so the audit's `str(Timestamp)` matches the
        # record's isoformat) so _rewrite_audit_sorted can stamp the flag onto the JSONL.
        self._stale_keys = {(_as_ts(r.trigger_ts), _as_ts(r.arrival_ts), r.trigger_kind)
                            for r in discarded}

    def _sort_events_native(self) -> None:
        """Order the AI events by (arrival, kind). Events carry only `time` (= arrival) and
        `kind` (no trigger_ts on the event schema), so those are the deterministic keys."""
        self.engine.events_native.sort(
            key=lambda e: (str(e.get("time") or ""), str(e.get("kind") or "")))

    def _rewrite_audit_sorted(self) -> None:
        path = Path(self.engine.audit_path)
        if not path.exists():
            return
        stale = getattr(self, "_stale_keys", set())
        recs = read_audit_records(path)
        for r in recs:
            # Stamp the deterministically-recomputed staleness onto the persisted record so
            # the audit JSONL (not just in-memory state) is observable + backtest/live-agreed.
            is_stale = (_as_ts(r.get("trigger_ts")), _as_ts(r.get("arrival_ts")),
                        r.get("trigger_kind")) in stale
            r["discarded"] = is_stale
            if is_stale and "discarded-stale" not in (r.get("error") or ""):
                r["error"] = ((r["error"] + "; ") if r.get("error") else "") + "discarded-stale"
        recs.sort(key=lambda r: (str(r.get("arrival_ts") or ""),
                                 str(r.get("trigger_ts") or ""),
                                 str(r.get("trigger_kind") or "")))
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, default=str) + "\n")
        os.replace(tmp, path)

    def finalize_deterministic(self, session_parquet=None, drain_timeout=None) -> bool:
        """Day/session-end: drain, recompute staleness, sort events + audit, then finalize.

        `drain_timeout` is None (unbounded) for the backtest — every job MUST complete for a
        deterministic result. `close` passes a bounded timeout for live so a wedged real-API
        call can never hang session teardown (H7). Returns whether the queue fully drained;
        the sort/rewrite/recompute are SKIPPED when a job is still wedged so the finalize
        thread never races the still-running worker's audit append / events_native mutation
        (H8). engine.finalize (a no-op for live, session_parquet=None) always runs."""
        drained = self.drain(timeout=drain_timeout)
        if drained:
            self._recompute_discarded()
            self._sort_events_native()
            self._rewrite_audit_sorted()
        self.engine.finalize(session_parquet)
        return drained

    def close(self, timeout: float, session_parquet=None) -> bool:
        """Bounded session-end for live: drain (bounded) → deterministic finalize → shutdown.
        Never raises (errors captured); the worker thread always dies (`finally`)."""
        drained = False
        try:
            drained = self.drain(timeout=timeout)
            # Already waited `timeout` above: unbounded re-drain if drained (instant, empty),
            # else drain_timeout=0 so finalize skips (no second `timeout` wait) — total close
            # is bounded at ~2×timeout worst case.
            self.finalize_deterministic(
                session_parquet=session_parquet,
                drain_timeout=None if drained else 0)
        except Exception as exc:                       # never raise into session teardown
            self.errors.append(f"close:{type(exc).__name__}:{exc}")
        finally:
            self._stop.set()
            self._thread.join(timeout=timeout)
        return drained
