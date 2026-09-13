"""Live async wrapper tests (Phase 5) — fake engine + gated backend, no real LLM.

Plan 8 additions (W1-Tt1..Tt5) exercise the deterministic finalize (sort audit +
events_native, arrival-vs-supersede staleness recompute, bounded close) against BOTH a
lightweight attr-fake engine and a real stub-backed DecisionEngine."""

import os
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from async_wrapper import DecisionWorker
from records import DecisionRecord, read_audit_records

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _rec(kind="new-hypothesis"):
    return DecisionRecord(trigger_ts="t", arrival_ts="t2", trigger_kind=kind,
                        facts_content_hash="h", decision={}, paired_diff={},
                        verdict="clean", fallback=False)


def _real_worker(tmp_path, latency=79.0, tag="o"):
    """A DecisionWorker over a real stub-backed DecisionEngine (minimal frames ⇒ degraded
    snapshot ⇒ deterministic failsafe records; enough to exercise finalize/staleness). `tag`
    isolates the out_dir so repeated workers never share an accumulating audit file."""
    from engine import DecisionEngine
    from decisions_config import DecisionsConfig
    from run_agent import StubBackend, DOCS_ROOT
    cfg = DecisionsConfig(real_api=False, latency_sec=latency)
    eng = DecisionEngine(cfg, StubBackend(), DOCS_ROOT, out_dir=str(tmp_path / tag))
    return DecisionWorker(eng)


class _AttrEngine:
    """Fake engine exposing the attrs finalize_deterministic touches (events_native,
    records, audit_path, finalize), with an optional gate and raise-on-Nth-call."""

    def __init__(self, tmp_path, gate=None, raise_on=None):
        self.gate = gate
        self.events_native = []
        self.records = []
        self.audit_path = Path(tmp_path) / "audit.jsonl"
        self.calls = []
        self._n = 0
        self._raise_on = raise_on

    def on_hypothesis_trigger(self, now, frames, hyp_event, kind):
        self._n += 1
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self._raise_on and self._n == self._raise_on:
            raise RuntimeError("boom")
        rec = _rec(kind)
        self.records.append(rec)
        return rec

    def on_checkpoint(self, now, frames):
        self.calls.append("checkpoint")

    def finalize(self, session_parquet=None):
        self.calls.append("finalize")


def _T(offset_sec=0):
    return pd.Timestamp("2026-05-19 09:30:00", tz="America/New_York") + pd.Timedelta(
        seconds=offset_sec)


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


# ── Plan 8: deterministic finalize + lifecycle (W1-Tt1..Tt5) ────────────────── #

def test_finalize_sorts_audit_and_events(tmp_path):
    """W1-Tt1 happy: audit JSONL + events_native sorted by (arrival, trigger) at finalize,
    regardless of the (FIFO) order the jobs were submitted/written in."""
    w = _real_worker(tmp_path)
    try:
        # Submit later-then-earlier so completion order is NOT sorted order.
        w.submit_hypothesis_trigger(_T(600), {}, {"time": _T(600).isoformat(),
                                    "direction": "up"}, "new-hypothesis", hyp_id="H2")
        w.submit_hypothesis_trigger(_T(0), {}, {"time": _T(0).isoformat(),
                                    "direction": "down"}, "new-hypothesis", hyp_id="H1")
        w.submit_checkpoint(_T(300), {})       # cadence may not fire — must not crash
        w.finalize_deterministic()
        arrivals = [r["arrival_ts"] for r in read_audit_records(w.audit_path)]
        assert arrivals == sorted(arrivals)    # audit rewritten in arrival order
        ev_times = [e.get("time") for e in w.events_native]
        assert ev_times == sorted(ev_times)    # events_native sorted likewise
    finally:
        w.shutdown()


def test_staleness_recompute_is_deterministic(tmp_path):
    """W1-Tt2: H1@T superseded by H2 at T+30s (< arrival T+79s) ⇒ discarded-stale; a
    supersede at T+120s (> arrival) does NOT flag it. Identical across 5 repeated runs."""
    results = []
    audits = []
    for i in range(5):
        w = _real_worker(tmp_path, latency=79.0, tag=f"o{i}")
        try:
            w.submit_hypothesis_trigger(_T(0), {}, {"time": _T(0).isoformat(),
                                        "direction": "up"}, "new-hypothesis", hyp_id="H1")
            w.note_hypothesis_superseded("H2", now=_T(30))
            w.finalize_deterministic()
            results.append([("discarded-stale" in (r.error or "")) for r in w.records])
            # The recomputed staleness is PERSISTED to the audit JSONL, deterministically.
            arecs = read_audit_records(w.audit_path)
            audits.append([(r.get("discarded"), r.get("error")) for r in arecs])
        finally:
            w.shutdown()
    assert all(r == [True] for r in results)   # deterministic: H1 stale every run
    assert all(a == audits[0] for a in audits)  # audit persistence byte-stable run-to-run
    assert any(d is True and "discarded-stale" in (e or "")
               for rec in audits for (d, e) in rec)   # flag actually written to disk

    w2 = _real_worker(tmp_path, latency=79.0, tag="neg")
    try:
        w2.submit_hypothesis_trigger(_T(0), {}, {"time": _T(0).isoformat(),
                                     "direction": "up"}, "new-hypothesis", hyp_id="H1")
        w2.note_hypothesis_superseded("H2", now=_T(120))   # after arrival → not stale
        w2.finalize_deterministic()
        assert [("discarded-stale" in (r.error or "")) for r in w2.records] == [False]
        assert w2.discarded == []
    finally:
        w2.shutdown()


def test_finalize_survives_engine_error(tmp_path):
    """W1-Tt3 error: engine raises on job 1 → worker survives (errors populated), job 2
    records; finalize_deterministic still sorts/rewrites without crashing."""
    eng = _AttrEngine(tmp_path, raise_on=1)
    w = DecisionWorker(eng)
    try:
        w.submit_hypothesis_trigger(_T(0), {}, {"time": "t"}, "new-hypothesis", hyp_id="H1")
        w.drain()
        assert w.errors and "RuntimeError" in w.errors[0]
        w.submit_hypothesis_trigger(_T(60), {}, {"time": "t"}, "new-hypothesis", hyp_id="H2")
        w.drain()
        w.finalize_deterministic()             # no audit file yet → no crash
        assert len(w.records) == 1
        assert "finalize" in eng.calls
    finally:
        w.shutdown()


def test_close_is_bounded_when_job_wedged(tmp_path):
    """W1-Tt4 interruption: close(timeout=0.2) while a gated job is wedged returns promptly
    (does NOT wait out the 5 s gate) and never raises; the thread exits once released."""
    gate = threading.Event()
    eng = _AttrEngine(tmp_path, gate=gate)
    w = DecisionWorker(eng)
    w.submit_hypothesis_trigger(_T(0), {}, {"time": "t"}, "new-hypothesis", hyp_id="H1")
    time.sleep(0.05)                           # worker picked up the job, blocked on gate
    t0 = time.monotonic()
    drained = w.close(timeout=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0                       # bounded — did not hang on the wedged call
    assert drained is False                    # the wedged job was abandoned
    gate.set()                                 # release so the daemon thread can exit
    w._thread.join(timeout=2.0)
    assert not w._thread.is_alive()


def test_concurrency_discarded_is_deterministic(tmp_path):
    """W1-Tt5 concurrency: 50 rapid submits interleaved with supersede notes → after
    finalize, len(records) == submits and the discarded set is a deterministic function of
    the timeline (identical across 3 runs)."""
    def _run(tag):
        w = _real_worker(tmp_path, latency=79.0, tag=tag)
        try:
            for i in range(50):
                t = _T(i * 10)                 # 10 s apart
                w.submit_hypothesis_trigger(t, {}, {"time": t.isoformat(),
                                            "direction": "up"}, "new-hypothesis",
                                            hyp_id=f"H{i}")
                if i % 3 == 0:                 # supersede every 3rd, 5 s after the submit
                    w.note_hypothesis_superseded(f"H{i}b", now=t + pd.Timedelta(seconds=5))
            w.finalize_deterministic()
            n = len(w.records)
            stale = sorted(r.trigger_ts for r in w.records
                           if "discarded-stale" in (r.error or ""))
            return n, stale
        finally:
            w.shutdown()

    runs = [_run(f"c{i}") for i in range(3)]
    assert all(n == 50 for n, _ in runs)       # one record per submit
    assert runs[0][1] == runs[1][1] == runs[2][1]   # discarded set deterministic
