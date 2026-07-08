"""Live async wrapper tests (Phase 5) — fake engine + gated backend, no real LLM."""

import threading
import time

import pytest

from async_wrapper import DecisionWorker
from records import DecisionRecord


def _rec(kind="new-hypothesis"):
    return DecisionRecord(trigger_ts="t", arrival_ts="t2", trigger_kind=kind,
                        facts_content_hash="h", decision={}, paired_diff={},
                        verdict="clean", fallback=False)


class _FakeEngine:
    def __init__(self, gate=None):
        self.gate = gate
        self.calls = []

    def on_hypothesis_trigger(self, now, frames, hyp_event, kind):
        if self.gate is not None:
            self.gate.wait(timeout=5)
        self.calls.append(kind)
        return _rec(kind)

    def on_checkpoint(self, now, frames):
        self.calls.append("checkpoint")


def test_job_computed_off_thread_loop_returns_immediately():
    gate = threading.Event()
    eng = _FakeEngine(gate=gate)
    w = DecisionWorker(eng)
    try:
        w.submit_hypothesis_trigger("t", {}, {"time": "t", "direction": "up"},
                                    "new-hypothesis", hyp_id="H1")
        time.sleep(0.1)                       # worker has picked up the job and is blocked
        assert w.records == []                # the submit call did NOT block on the work
        gate.set()
        w.drain()
        assert len(w.records) == 1
    finally:
        gate.set()
        w.shutdown()


def test_staleness_flags_discarded():
    gate = threading.Event()
    eng = _FakeEngine(gate=gate)
    w = DecisionWorker(eng)
    try:
        w.submit_hypothesis_trigger("t", {}, {"time": "t1", "direction": "up"},
                                    "new-hypothesis", hyp_id="H1")
        w.note_hypothesis_superseded("H2")    # a newer hypothesis supervened in flight
        gate.set()
        w.drain()
        assert len(w.discarded) == 1
        assert "discarded-stale" in w.discarded[0].error
    finally:
        gate.set()
        w.shutdown()


def test_worker_survives_exception():
    class _Raising:
        def __init__(self):
            self.n = 0

        def on_hypothesis_trigger(self, now, frames, hyp_event, kind):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            return _rec(kind)

        def on_checkpoint(self, now, frames):
            pass

    w = DecisionWorker(_Raising())
    try:
        w.submit_hypothesis_trigger("t", {}, {"time": "t"}, "new-hypothesis", hyp_id="H1")
        w.drain()
        assert w.errors and "RuntimeError" in w.errors[0]   # caught, logged
        w.submit_hypothesis_trigger("t", {}, {"time": "t"}, "new-hypothesis", hyp_id="H2")
        w.drain()
        assert len(w.records) == 1                          # worker survived, still works
    finally:
        w.shutdown()


def test_shutdown_drains_queue():
    eng = _FakeEngine()
    w = DecisionWorker(eng)
    for _ in range(5):
        w.submit_checkpoint("t", {})
    w.shutdown()
    assert not w._thread.is_alive()
    assert eng.calls.count("checkpoint") == 5               # all drained before stop
