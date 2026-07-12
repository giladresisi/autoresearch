"""Phase-4 PrimaryRunner integration tests (plan §Phase 4).

Drives the whole v2 primary stack (bus + DecisionService + TradeDirector + mechanism
adapter) over the committed golden facts frames with the StubBackend — no LLM, no broker.
Asserts the structure: a NEUTRAL/LOW stub thesis parks in THESIS_LOW_CONF and arms zero
mechanisms (no entry while low-conf / WAIT), and that the stub run is deterministic."""

import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "contracts"),
           os.path.join(_AGENT, "decisions")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from primary_runner import PrimaryRunner            # noqa: E402
from trade_director import State                    # noqa: E402

FIX = os.path.join(_AGENT, "fixtures", "derive_facts_golden")


def _frames(now=None):
    mnq = pd.read_parquet(os.path.join(FIX, "MNQ_1s_slice.parquet"))
    mes = pd.read_parquet(os.path.join(FIX, "MES_1s_slice.parquet"))
    if now is None:
        now = min(mnq.index[-1], mes.index[-1])
    return {"mnq_today": mnq, "mes_today": mes, "hist_mnq": None, "hist_mes": None,
            "hist_1hr": None, "hist_4hr": None, "ath_mnq": 30077.0, "ath_mes": 7602.5,
            "now": now}


def _stub_backend():
    from run_agent import make_backend
    return make_backend("stub")


def _fixtures_present():
    return os.path.exists(os.path.join(FIX, "MNQ_1s_slice.parquet"))


def test_primary_stub_low_conf_arms_nothing(tmp_path):
    if not _fixtures_present():
        pytest.skip("golden fixtures not present")
    runner = PrimaryRunner(tmp_path, _stub_backend(), date="2026-05-19", threaded=False)
    frames = _frames()
    open_ts = frames["now"]
    runner.on_session_open(open_ts, frames)
    # The thesis is delivered with modelled latency (trigger + ~79s), so it has NOT arrived
    # at the open — the director is still NO_THESIS until a bar past the arrival time.
    assert runner.director.thesis is None
    # Advance a bar well past the latency budget → the thesis matures and is delivered.
    runner.on_bar(open_ts + pd.Timedelta(minutes=3), _frames())

    # Stub → NEUTRAL/LOW thesis → parked in THESIS_LOW_CONF, no plan call, nothing armed.
    assert runner.director.state == State.THESIS_LOW_CONF
    assert runner.director.thesis.bias == "NEUTRAL"
    assert runner.mechanism.armed == []                 # zero entries (no SETUP arms)
    assert runner.director.position_open is False

    # The standing thesis was published to the bus with an id; no plan file (never confident).
    thesis = runner.bus.read_thesis()
    assert thesis is not None and thesis["thesis_id"].startswith("th_2026-05-19_")
    assert runner.bus.read_plan() is None
    # An audit record was written for the L1 call.
    assert runner.audit_path.exists()


def test_primary_stub_deterministic_across_two_runs(tmp_path):
    if not _fixtures_present():
        pytest.skip("golden fixtures not present")

    def run(dirpath):
        runner = PrimaryRunner(dirpath, _stub_backend(), date="2026-05-19", threaded=False)
        frames = _frames()
        open_ts = frames["now"]
        runner.on_session_open(open_ts, frames)
        # a few quiet bars — thesis matures with latency, no predicates fire, nothing arms
        for i in range(5):
            runner.on_bar(open_ts + pd.Timedelta(minutes=i + 1), _frames())
        return (runner.director.state, [k for k, _ in runner.mechanism.armed],
                runner.bus.read_thesis()["bias"])

    a = run(tmp_path / "run_a")
    b = run(tmp_path / "run_b")
    assert a == b                                        # deterministic
    assert a[1] == []                                    # zero arms both runs
