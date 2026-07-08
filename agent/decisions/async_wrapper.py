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

import queue
import threading
import time
from typing import Optional


def _hyp_id(hyp_event: Optional[dict]) -> str:
    he = hyp_event or {}
    return f"{he.get('time')}|{he.get('direction')}"


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
        self._thread = threading.Thread(target=self._run, name="ai-decisions-worker", daemon=True)
        self._thread.start()

    # -- non-blocking submit API (called from the 1s loop) ------------------ #
    def submit_hypothesis_trigger(self, now, frames, hyp_event, trigger_kind,
                                  hyp_id: Optional[str] = None) -> None:
        hid = hyp_id or _hyp_id(hyp_event)
        self._current_hyp_id = hid          # this hypothesis is now the standing one
        self._queue.put(("trigger", now, frames, hyp_event, trigger_kind, hid))

    def submit_checkpoint(self, now, frames) -> None:
        self._queue.put(("checkpoint", now, frames))

    def note_hypothesis_superseded(self, new_hyp_id: Optional[str]) -> None:
        """A newer new-hypothesis/trend-broken supervened; any in-flight job for the old
        hypothesis will be flagged discarded when it completes."""
        self._current_hyp_id = new_hyp_id

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
