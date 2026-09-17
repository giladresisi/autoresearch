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

    def __init__(self, mnq, mes, emit, ai_decisions=None, trade_primary=None,
                 trader=None, **kwargs):
        # `trader_only` arrives ONLY when the agent owns the dispatcher (plan 38 D25);
        # under ACT_TRADER=0 the construction is today's call, argument for argument.
        _FakePipeline.last = {"ai_decisions": ai_decisions,
                              "trade_primary": trade_primary, "trader": trader,
                              "kwargs": dict(kwargs)}
        self.ai_decisions = ai_decisions
        self.trade_primary = trade_primary
        self.trader = trader

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


# --- cycle-1 trader graft (added with the ACT_TRADER wiring) ----------------- #

def test_trader_flag_off_builds_no_trader(monkeypatch, tmp_path, _fake_pipeline):
    """ACT_TRADER=0 is the explicit OPT-OUT — the kill switch. It is the only way to
    get no graft; unset now means ON (every orchestrator session runs the chain)."""
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    monkeypatch.setenv("ACT_TRADER", "0")
    d = main.SmtV2Dispatcher()
    d.on_session_start(_now(), _hist(), _hist())
    assert d._trader is None
    assert _FakePipeline.last["trader"] is None
    # Legacy owns: no `trader_only`, no port — the flag-off construction is unchanged.
    assert _FakePipeline.last["kwargs"] == {} and d._agent_port is None


@pytest.fixture()
def _agent_preconditions(monkeypatch, tmp_path):
    """Plan 38 D16: what a non-refused agent start needs. Everything else about these
    tests is the wiring, so the real trader build is always replaced."""
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(main, "_smtv2_pipeline", "v2")
    for var in ("ACT_TRADER", "ACT_AI_MODE", "FORCE_RESET"):
        monkeypatch.delenv(var, raising=False)


def test_trader_unset_attaches_the_graft_by_default(monkeypatch, _agent_preconditions,
                                                    _fake_pipeline):
    """Unset ⇒ ON. Nothing to configure: every orchestrator session runs the chain —
    and since plan 38 (D25) the chain OWNS the dispatcher, with the legacy engine dark."""
    sentinel = object()
    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_trader",
                        staticmethod(lambda out_dir, sink=None: sentinel))
    d = main.SmtV2Dispatcher()
    d.on_session_start(_now(), _hist(), _hist())
    assert _FakePipeline.last["trader"] is sentinel
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}


def test_trader_flag_on_attaches_the_graft(monkeypatch, tmp_path, _agent_preconditions,
                                           _fake_pipeline):
    sentinel = object()
    captured = {}

    def _fake_build_trader(out_dir, sink=None):
        captured["out_dir"] = out_dir
        captured["sink"] = sink
        return sentinel

    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_trader",
                        staticmethod(_fake_build_trader))
    now = _now()
    d = main.SmtV2Dispatcher()
    d.on_session_start(now, _hist(), _hist())
    assert _FakePipeline.last["trader"] is sentinel
    assert captured["out_dir"] == tmp_path / str(main.cme_session_date(now))
    assert captured["sink"] == d._agent_port.sink, "the trader is built ON the port's sink"


def test_trader_build_failure_is_a_refused_start_not_a_fallback(
        monkeypatch, _agent_preconditions, _fake_pipeline, capsys):
    """A broken trader must never abort the live session — and, since plan 38 (D20), must
    never hand the dispatcher back to legacy either: REFUSED, nobody owns, legacy dark.
    (Was `..._degrades_to_none`: a silent None was harmless only while the trader merely
    observed.)"""
    def _boom(out_dir, sink=None):
        raise RuntimeError("trader build failed")

    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_trader", staticmethod(_boom))
    d = main.SmtV2Dispatcher()
    d.on_session_start(_now(), _hist(), _hist())     # must NOT raise
    assert d._trader is None
    assert _FakePipeline.last["trader"] is None
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}
    out = capsys.readouterr().out
    assert "[AGENT-LIVE] REFUSED: trader build failed: RuntimeError" in out


def test_trader_env_flag_never_imports_the_package_when_off(monkeypatch):
    """The OPT-OUT must leave the live process's import state untouched (review finding
    N2 for the AI-decisions worker; the same rule applies here) — that is what makes it
    a usable kill switch mid-incident."""
    import inspect
    monkeypatch.setenv("ACT_TRADER", "0")
    src = inspect.getsource(main.SmtV2Dispatcher._build_trader)
    env_line = src.index('os.environ.get(')
    assert src.index("from agent.trader.graft import") > env_line
    assert main.SmtV2Dispatcher._build_trader(None) is None
