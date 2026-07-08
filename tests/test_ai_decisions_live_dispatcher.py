"""GIL-44 Phase-3 (slice 2) live-dispatcher wiring tests — offline, no IB, no keys, no LLM.

Prove the live `SmtV2Dispatcher` worker lifecycle deterministically: flag-OFF builds NO
worker (byte-identical live path), flag-ON attaches one at the correct per-session out_dir,
the per-second session-end teardown is idempotent + bounded, and an engine-build failure
degrades to no-worker without ever aborting the live session.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types

import pandas as pd
import pytest

import automation.main as main

_AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)


class _FakePipeline:
    """Captures the ai_decisions arg the dispatcher passes; no daily/data needed."""

    last = {}

    def __init__(self, mnq, mes, emit, ai_decisions=None):
        _FakePipeline.last = {"ai_decisions": ai_decisions}
        self.ai_decisions = ai_decisions

    def on_session_start(self, now, today_at_open, force_reset=False):
        pass

    def on_1m_bar(self, *a, **kw):
        return []


class _FakeWorker:
    def __init__(self):
        self.closed = 0
        self.close_timeout = None
        self.events_native = [{"kind": "new-hypothesis", "source": "ai-decisions",
                               "time": "2026-05-19T09:30:00-04:00"}]

    def close(self, timeout, session_parquet=None):
        self.closed += 1
        self.close_timeout = timeout
        return True


def _hist():
    idx = pd.date_range("2026-05-19 09:15", periods=6, freq="1min", tz="America/New_York")
    return pd.DataFrame({"Open": 21000.0, "High": 21010.0, "Low": 20990.0,
                         "Close": 21002.0, "Volume": 100.0}, index=idx)


@pytest.fixture()
def _fake_pipeline(monkeypatch):
    import session_pipeline
    monkeypatch.setattr(session_pipeline, "SessionPipeline", _FakePipeline)


def _now():
    return pd.Timestamp("2026-05-19 09:20", tz="America/New_York")


# ── W2-Tt1: flag OFF ⇒ no worker, no thread ─────────────────────────────────── #

def test_flag_off_builds_no_worker(monkeypatch, tmp_path, _fake_pipeline):
    import decisions_config
    monkeypatch.setattr(decisions_config, "AI_DECISIONS_ENABLED", False, raising=False)
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    monkeypatch.delenv("ACT_AI_DECISIONS", raising=False)

    before = {t.name for t in threading.enumerate()}
    d = main.SmtV2Dispatcher()
    d.on_session_start(_now(), _hist(), _hist())

    assert d._worker is None
    assert _FakePipeline.last["ai_decisions"] is None          # byte-identical live path
    after = {t.name for t in threading.enumerate()}
    assert "ai-decisions-worker" not in (after - before)       # no worker thread spawned


# ── W2-Tt2: flag ON ⇒ worker attached at the per-session out_dir ────────────── #

def test_flag_on_constructs_worker_at_session_dir(monkeypatch, tmp_path, _fake_pipeline):
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    fake = _FakeWorker()
    captured = {}

    def _fake_build_worker(out_dir):
        captured["out_dir"] = out_dir
        return fake

    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_worker",
                        staticmethod(_fake_build_worker))

    now = _now()
    d = main.SmtV2Dispatcher()
    d.on_session_start(now, _hist(), _hist())

    assert d._worker is fake
    assert _FakePipeline.last["ai_decisions"] is fake
    assert captured["out_dir"] == tmp_path / str(main.cme_session_date(now))


# ── W2-Tt3: session-end teardown is idempotent (per-second closed branch) ───── #

def test_session_end_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    fake = _FakeWorker()
    now = _now()
    d = main.SmtV2Dispatcher()
    d._worker = fake
    d._session_date = main.cme_session_date(now)
    d._session_closed = False

    for _ in range(3):                          # the closed branch fires every second
        d.on_session_end(now)

    assert fake.closed == 1                      # close invoked exactly once
    ai_events = tmp_path / str(d._session_date) / "ai_events.jsonl"
    assert ai_events.exists()
    assert len(ai_events.read_text(encoding="utf-8").splitlines()) == 1   # written once


# ── W2-Tt4: wedged in-flight call ⇒ bounded teardown, thread eventually exits ─ #

def test_session_end_bounded_when_worker_wedged(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    import decisions_config
    # Force a small teardown budget regardless of the module-import-time default.
    monkeypatch.setattr(decisions_config, "DecisionsConfig",
                        lambda: types.SimpleNamespace(shutdown_timeout=0.2))

    from decisions.async_wrapper import DecisionWorker

    class _Blocking:
        def __init__(self):
            self.gate = threading.Event()
            self.events_native = []
            self.records = []
            self.audit_path = tmp_path / "audit.jsonl"

        def on_hypothesis_trigger(self, now, frames, hyp_event, kind):
            self.gate.wait(timeout=5)
            return None

        def on_checkpoint(self, now, frames):
            pass

        def finalize(self, session_parquet=None):
            pass

    eng = _Blocking()
    worker = DecisionWorker(eng)
    worker.submit_hypothesis_trigger(_now(), {}, {"time": "t"}, "new-hypothesis", hyp_id="H1")
    time.sleep(0.05)                             # worker picked up the job, blocked on gate

    d = main.SmtV2Dispatcher()
    d._worker = worker
    d._session_date = main.cme_session_date(_now())
    d._session_closed = False

    t0 = time.monotonic()
    d.on_session_end(_now())
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0                         # bounded — did NOT wait out the 5 s gate

    eng.gate.set()                               # release so the daemon thread can exit
    worker._thread.join(timeout=2.0)
    assert not worker._thread.is_alive()


# ── W2-Tt5: engine-build failure ⇒ degrade to no-worker, live never aborts ──── #

def test_engine_build_failure_degrades(monkeypatch, tmp_path, _fake_pipeline):
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    import decisions.live_factory as lf
    monkeypatch.setattr(lf, "ai_decisions_enabled", lambda: True)

    def _boom(out_dir):
        raise RuntimeError("engine build failed")

    monkeypatch.setattr(lf, "build_decision_worker", _boom)

    now = _now()
    d = main.SmtV2Dispatcher()
    d.on_session_start(now, _hist(), _hist())     # must NOT raise

    assert d._worker is None
    assert _FakePipeline.last["ai_decisions"] is None
    # live keeps running: on_1m_bar still works (no raise) with no worker attached
    d.on_1m_bar(now, pd.Series(dtype=float), pd.Series(dtype=float), _hist(), _hist())
