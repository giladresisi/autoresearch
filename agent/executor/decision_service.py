"""Async decision service — the TradeDirector's decision provider (spec §10, P2→P3).

Realizes the provider interface (`request_thesis(facts_ref)`, `request_plan(facts_ref,
thesis)`) as new call kinds over the existing run_agent call core. Adds the two things the
batch flagged: **per-level coalescing** (at most one pending call per level; a newer trigger
supersedes the queued one — targets the 40% stale-discard rate) and **supersede-in-flight
staleness** (a result whose level got a newer trigger while it ran is recorded but not
delivered). On completion a result is published to the JSON bus, an audit record is written,
and the parsed block is delivered to the executor (`sink.on_thesis_arrived` / `on_plan_arrived`).

Two run modes share one coalescing core:
  - `threaded=True` (live): a worker thread drains submissions off the caller's loop.
  - `threaded=False` (backtest): submissions queue; the caller `pump()`s them deterministically
    (arrival = trigger_ts + latency), so backtest and live agree with no wall-clock dependence.

The actual L1/L2 calls are injected (`run_l1`, `run_l2`) so this is testable with no LLM.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "decisions")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from records import (  # noqa: E402
    build_plan_audit_record, build_thesis_audit_record, write_audit_record,
)

L1, L2 = "L1", "L2"


def _arrival(trigger_ts, latency_sec):
    if trigger_ts is None:
        return None
    try:
        import pandas as pd
        return trigger_ts + pd.Timedelta(seconds=latency_sec)
    except Exception:
        return trigger_ts


class DecisionService:
    def __init__(self, run_l1: Callable, run_l2: Callable, *, bus=None,
                 audit_path=None, sink=None, latency_sec: float = 79.0,
                 threaded: bool = False):
        self._run_l1 = run_l1                 # run_l1(facts_ref) -> CallOutcome-like
        self._run_l2 = run_l2                 # run_l2(facts_ref, thesis) -> CallOutcome-like
        self.bus = bus
        self.audit_path = audit_path
        self.sink = sink                      # TradeDirector (on_thesis_arrived/on_plan_arrived)
        self.latency_sec = latency_sec
        self.threaded = threaded

        self._lock = threading.Lock()
        self._pending: dict = {}              # level -> (seq, facts_ref, thesis)
        self._latest_seq: dict = {L1: 0, L2: 0}
        self._seq = 0
        self.records: list = []               # in-memory audit records (also written)
        self.stale: list = []
        self.stats = {"calls": 0, "coalesced": 0, "stale": 0, "delivered": 0}
        # Deliveries are marshalled through this queue and applied to the sink (the
        # TradeDirector) ONLY by `deliver_ready`, which the consumer calls on its own thread
        # (the pipeline/bar-loop thread) — the worker thread never touches director state
        # (fixes the live threaded data race). Each item is (arrival_ts, level, block); it is
        # delivered when the bar clock reaches arrival_ts (trigger + latency), so the backtest
        # models the same call latency as live and the two agree (spec §10 parity).
        self._deliveries: "list" = []

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="v2-decision-worker",
                                            daemon=True)
            self._thread.start()

    # -- provider interface (called by the TradeDirector) ------------------- #
    def request_thesis(self, facts_ref) -> None:
        self._enqueue(L1, facts_ref, None)

    def request_plan(self, facts_ref, thesis) -> None:
        self._enqueue(L2, facts_ref, thesis)

    def _enqueue(self, level, facts_ref, thesis) -> None:
        with self._lock:
            self._seq += 1
            self._latest_seq[level] = self._seq
            if level in self._pending:
                self.stats["coalesced"] += 1        # a queued call for this level is dropped
            self._pending[level] = (self._seq, facts_ref, thesis)
        if self.threaded:
            self._wake.set()

    # -- draining ----------------------------------------------------------- #
    def process(self) -> int:
        """Process all currently-pending levels once (make the calls, enqueue deliveries).
        Runs on the worker thread (live) or the caller's thread (backtest via pump). Never
        touches the sink — delivery is `deliver_ready`'s job, on the consumer thread."""
        made = 0
        while True:
            with self._lock:
                if not self._pending:
                    break
                # Fixed level order (L1 before L2) for deterministic processing.
                level = L1 if L1 in self._pending else L2
                seq, facts_ref, thesis = self._pending.pop(level)
            self._process(level, seq, facts_ref, thesis)
            made += 1
        return made

    def pump(self, now=None) -> int:
        """Synchronous convenience (backtest/tests): process pending calls, then deliver any
        matured decisions (arrival_ts <= now; all when now is None). Same-thread, so no race."""
        made = self.process()
        self.deliver_ready(now)
        return made

    def deliver_ready(self, now=None) -> int:
        """Apply matured deliveries to the sink on the CALLER's thread. A delivery matures
        when the bar clock `now` reaches its arrival_ts (trigger + latency); `now=None`
        delivers everything (tests / session teardown). Deterministic: delivered in
        (arrival_ts, level) order."""
        ready = []
        with self._lock:
            keep = []
            for arrival_ts, level, block in self._deliveries:
                if now is None or arrival_ts is None or self._reached(arrival_ts, now):
                    ready.append((arrival_ts, level, block))
                else:
                    keep.append((arrival_ts, level, block))
            self._deliveries = keep
        ready.sort(key=lambda x: (str(x[0]), x[1]))
        for arrival_ts, level, block in ready:
            self.stats["delivered"] += 1
            self._apply(level, block, arrival_ts)
        return len(ready)

    @staticmethod
    def _reached(arrival_ts, now) -> bool:
        try:
            return now >= arrival_ts
        except TypeError:
            return True                               # incomparable clocks (tests) → deliver

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=0.05):
                continue
            self._wake.clear()
            try:
                self.process()                        # process only; consumer delivers
            except Exception:                         # worker must survive
                pass

    def _process(self, level, seq, facts_ref, thesis) -> None:
        self.stats["calls"] += 1
        # A facts_ref may be a thunk (callable) so the caller can defer the costly facts
        # snapshot until a call actually fires (most bars make no call).
        if callable(facts_ref):
            facts_ref = facts_ref()
        outcome = self._run_l1(facts_ref) if level == L1 else self._run_l2(facts_ref, thesis)
        block = getattr(outcome, "block", None) or {}
        trigger = self._trigger_of(facts_ref)
        trigger_ts = self._ts_of(facts_ref)
        arrival_ts = _arrival(trigger_ts, self.latency_sec)
        facts_hash = self._hash_of(facts_ref)

        # Supersede-in-flight: a newer trigger for this level arrived while we ran.
        with self._lock:
            stale = self._latest_seq.get(level, 0) > seq
            if stale:
                self.stats["stale"] += 1

        if level == L1:
            published = block
            if self.bus is not None and not stale:
                published = self.bus.publish_thesis(block, issued_at=str(arrival_ts),
                                                    facts_hash=facts_hash)
            rec = build_thesis_audit_record(
                block=published, trigger=trigger, trigger_ts=trigger_ts,
                arrival_ts=arrival_ts, facts_hash=facts_hash,
                attempts=getattr(outcome, "attempts", None),
                latency_total_sec=getattr(outcome, "latency_total", 0.0),
                usage_total=getattr(outcome, "usage_total", None),
                verdict=getattr(outcome, "verdict", "clean"),
                fallback=getattr(outcome, "fallback", False), stale=stale)
        else:
            thesis_id = (thesis or {}).get("thesis_id") if isinstance(thesis, dict) \
                else getattr(thesis, "thesis_id", None)
            published = block
            if self.bus is not None and not stale:
                published = self.bus.publish_plan(block, thesis_id=thesis_id,
                                                  issued_at=str(arrival_ts),
                                                  facts_hash=facts_hash)
            rec = build_plan_audit_record(
                block=published, trigger=trigger, trigger_ts=trigger_ts,
                arrival_ts=arrival_ts, facts_hash=facts_hash, thesis_id=thesis_id,
                attempts=getattr(outcome, "attempts", None),
                latency_total_sec=getattr(outcome, "latency_total", 0.0),
                usage_total=getattr(outcome, "usage_total", None),
                verdict=getattr(outcome, "verdict", "clean"),
                fallback=getattr(outcome, "fallback", False), stale=stale)

        with self._lock:
            self.records.append(rec)
            if self.audit_path is not None:
                write_audit_record(self.audit_path, rec)
            if stale:
                self.stale.append(rec)
                return                                # not delivered — newer call supersedes
            # Marshal the delivery; the consumer thread applies it at/after arrival_ts.
            self._deliveries.append((arrival_ts, level, published))

    def _apply(self, level, block, arrival_ts) -> None:
        """Apply one matured delivery to the sink. Runs ONLY on the consumer's thread
        (deliver_ready), so the sink (TradeDirector) is never mutated from the worker."""
        if self.sink is None:
            return
        if level == L1:
            self.sink.on_thesis_arrived(block, ts=arrival_ts)
        else:
            self.sink.on_plan_arrived(block, ts=arrival_ts)

    # -- facts_ref accessors (a facts_ref is a dict or an object) ----------- #
    @staticmethod
    def _get(facts_ref, key, default=None):
        if isinstance(facts_ref, dict):
            return facts_ref.get(key, default)
        return getattr(facts_ref, key, default)

    def _hash_of(self, facts_ref):
        return self._get(facts_ref, "facts_hash") or self._get(facts_ref, "content_hash")

    def _ts_of(self, facts_ref):
        return self._get(facts_ref, "trigger_ts") or self._get(facts_ref, "now")

    def _trigger_of(self, facts_ref):
        return self._get(facts_ref, "trigger", "unknown")

    # -- lifecycle ---------------------------------------------------------- #
    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        try:
            self.process()                            # drain any pending the worker missed
        except Exception:
            pass
