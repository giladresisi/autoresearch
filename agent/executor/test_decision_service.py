"""Phase-3 DecisionService tests: coalescing, supersede staleness, audit linkage,
and a stub end-to-end thesis→plan→bus round-trip (plan §Phase 3)."""

import os
import sys

import pandas as pd

from bus import DecisionBus
from decision_service import DecisionService, L2
from records import read_audit_records

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_AGENT, os.path.join(_AGENT, "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _Outcome:
    def __init__(self, block):
        self.block = block
        self.attempts = []
        self.latency_total = 0.0
        self.usage_total = {}
        self.verdict = "clean"
        self.fallback = False


class _Sink:
    def __init__(self):
        self.theses = []
        self.plans = []

    def on_thesis_arrived(self, block, ts=None):
        self.theses.append(block)

    def on_plan_arrived(self, block, ts=None):
        self.plans.append(block)


def _fr(hash_, ts="2026-05-19 18:30", trigger="t"):
    return {"facts_hash": hash_, "trigger_ts": pd.Timestamp(ts, tz="America/New_York"),
            "trigger": trigger}


# --------------------------------------------------------------------------- #
# Coalescing                                                                   #
# --------------------------------------------------------------------------- #
def test_two_queued_l2_coalesce_to_one_call_newest_facts():
    seen = []
    svc = DecisionService(run_l1=lambda fr: _Outcome({"bias": "UP"}),
                          run_l2=lambda fr, th: (seen.append(fr["facts_hash"])
                                                 or _Outcome({"verdict": "WAIT"})))
    svc.request_plan(_fr("old"), {"thesis_id": "th_1"})
    svc.request_plan(_fr("new"), {"thesis_id": "th_1"})   # supersedes the queued one
    made = svc.pump()
    assert made == 1                          # coalesced to a single call
    assert seen == ["new"]                    # with the newest facts
    assert svc.stats["coalesced"] == 1


# --------------------------------------------------------------------------- #
# Supersede-in-flight → stale (not delivered)                                 #
# --------------------------------------------------------------------------- #
def test_supersede_in_flight_marks_stale():
    sink = _Sink()

    calls = {"n": 0}

    def run_l2(fr, th):
        calls["n"] += 1
        if calls["n"] == 1:
            # A newer trigger for L2 arrives WHILE this first call is running.
            svc.request_plan(_fr("newer"), {"thesis_id": "th_1"})
        return _Outcome({"verdict": "WAIT", "_facts": fr["facts_hash"]})

    svc = DecisionService(run_l1=lambda fr: _Outcome({"bias": "UP"}), run_l2=run_l2,
                          sink=sink)
    svc.request_plan(_fr("first"), {"thesis_id": "th_1"})
    svc.pump()
    assert svc.stats["stale"] == 1            # the first result was superseded
    assert len(svc.stale) == 1
    # Only the newest (non-stale) result is delivered to the executor.
    assert len(sink.plans) == 1
    assert sink.plans[0]["_facts"] == "newer"


# --------------------------------------------------------------------------- #
# Audit record: facts_hash + thesis linkage                                    #
# --------------------------------------------------------------------------- #
def test_delivery_gated_by_arrival_time():
    # A decision is delivered to the sink only once the clock reaches trigger + latency
    # (models call latency identically in backtest + live; fixes zero-latency delivery).
    sink = _Sink()
    svc = DecisionService(run_l1=lambda fr: _Outcome({"bias": "UP"}),
                          run_l2=lambda fr, th: _Outcome({"verdict": "WAIT"}),
                          sink=sink, latency_sec=60.0)
    trig = pd.Timestamp("2026-05-19 18:00", tz="America/New_York")
    svc.request_thesis({"facts_hash": "h", "trigger_ts": trig, "trigger": "open"})
    svc.process()                                       # call made; delivery queued
    svc.deliver_ready(trig + pd.Timedelta(seconds=30))  # before arrival → not delivered
    assert sink.theses == []
    svc.deliver_ready(trig + pd.Timedelta(seconds=90))  # after arrival → delivered
    assert len(sink.theses) == 1


def test_worker_thread_never_calls_sink_directly():
    # In threaded mode the worker only PROCESSES; the sink is applied by deliver_ready on
    # the caller's thread (no cross-thread director mutation).
    sink = _Sink()
    svc = DecisionService(run_l1=lambda fr: _Outcome({"bias": "UP"}),
                          run_l2=lambda fr, th: _Outcome({"verdict": "WAIT"}),
                          sink=sink, threaded=True, latency_sec=0.0)
    try:
        svc.request_thesis({"facts_hash": "h", "trigger_ts": None, "trigger": "open"})
        import time
        for _ in range(100):                            # let the worker process
            if svc.records:
                break
            time.sleep(0.02)
        assert sink.theses == []                        # worker did NOT deliver
        svc.deliver_ready(None)                          # consumer delivers
        assert len(sink.theses) == 1
    finally:
        svc.close()


def test_audit_record_has_facts_hash_and_thesis_linkage(tmp_path):
    bus = DecisionBus(tmp_path, date="2026-05-19")
    audit = tmp_path / "ai_decisions_audit.jsonl"
    svc = DecisionService(run_l1=lambda fr: _Outcome({"bias": "UP"}),
                          run_l2=lambda fr, th: _Outcome({"verdict": "SETUP"}),
                          bus=bus, audit_path=audit)
    svc.request_thesis(_fr("hA"))
    svc.pump()
    th_id = bus.read_thesis()["thesis_id"]
    svc.request_plan(_fr("hB"), bus.read_thesis())
    svc.pump()

    recs = read_audit_records(audit)
    assert [r["call_type"] for r in recs] == ["decide_thesis", "decide_plan"]
    assert recs[0]["facts_hash"] == "hA"
    assert recs[1]["facts_hash"] == "hB"
    assert recs[1]["thesis_id"] == th_id       # plan audit links to its parent thesis


# --------------------------------------------------------------------------- #
# Stub end-to-end thesis → plan → bus round-trip                              #
# --------------------------------------------------------------------------- #
def test_stub_end_to_end_thesis_plan_bus(tmp_path):
    from run_agent import decide_plan, decide_thesis, make_backend
    backend = make_backend("stub")

    def run_l1(fr):
        return decide_thesis(fr["facts_text"], "", fr["facts"], backend)

    def run_l2(fr, thesis):
        return decide_plan(fr["facts_text"], "", fr["facts"], thesis, backend)

    bus = DecisionBus(tmp_path, date="2026-05-19")
    sink = _Sink()
    svc = DecisionService(run_l1=run_l1, run_l2=run_l2, bus=bus,
                          audit_path=tmp_path / "audit.jsonl", sink=sink)

    fr = {"facts_text": "", "facts": {}, "facts_hash": "h0",
          "trigger_ts": pd.Timestamp("2026-05-19 18:00", tz="America/New_York"),
          "trigger": "session_open"}
    svc.request_thesis(fr)
    svc.pump()
    thesis = bus.read_thesis()
    assert thesis is not None and thesis["bias"] == "NEUTRAL"   # stub fail-safe thesis
    assert sink.theses and sink.theses[0]["thesis_id"] == thesis["thesis_id"]

    svc.request_plan({**fr, "trigger": "awaiting_setup"}, thesis)
    svc.pump()
    plan = bus.read_plan()
    assert plan is not None and plan["verdict"] == "WAIT"       # stub fail-safe plan
    assert plan["thesis_id"] == thesis["thesis_id"]            # linkage persisted
