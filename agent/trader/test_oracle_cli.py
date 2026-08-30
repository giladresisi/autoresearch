import json
import os
import subprocess
import sys

import pytest

from agent.trader.replay import run_replay

DATE = "2026-08-13"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.timeout(900)

ORACLE = {"bias": "DOWN", "regime": "TREND", "confidence": "HIGH",
          "dol": {"level": "oracle_dol", "price": 29700.0},
          "falsified_if": [{"type": "price_beyond", "price": 30100.0, "side": "above"}]}


@pytest.fixture(scope="module")
def _oracle_run(tmp_path_factory):
    return run_replay([DATE], thesis=ORACLE)[DATE]


def test_an_oracle_run_makes_no_model_call_and_no_cache_hit(_oracle_run):
    assert _oracle_run["cache"] == {"hits": 0, "misses": 0, "calls": 0, "refusals": 0}


def test_the_oracle_thesis_reaches_the_plan(_oracle_run):
    with open(os.path.join(_oracle_run["run_dir"], "plans.json"), encoding="utf-8") as fh:
        plans = json.load(fh)
    assert plans, "the oracle thesis must arm a plan"
    plan = list(plans.values())[0]
    assert plan["direction"] == "DOWN"
    assert plan["dol"]["price"] == 29700.0


def test_provenance_is_stamped_in_the_run_dir(_oracle_run):
    p = os.path.join(_oracle_run["run_dir"], "thesis_source.json")
    with open(p, encoding="utf-8") as fh:
        blob = json.load(fh)
    assert blob["thesis_source"] == "injected"
    assert blob["thesis"]["dol"]["price"] == 29700.0


def test_the_recorded_thesis_is_verbatim(_oracle_run):
    p = os.path.join(_oracle_run["run_dir"], "thesis_source.json")
    with open(p, encoding="utf-8") as fh:
        assert json.load(fh)["thesis"] == ORACLE


def test_thesis_and_seed_are_mutually_exclusive():
    with pytest.raises(ValueError):
        run_replay([DATE], thesis=ORACLE, allow_calls=True)


def test_an_invalid_oracle_is_refused_before_the_run_starts():
    from agent.trader.fixed_backend import OracleThesisError
    bad = dict(ORACLE, falsified_if=[])
    with pytest.raises(OracleThesisError):
        run_replay([DATE], thesis=bad)


def test_the_arrival_gate_can_be_disabled_for_an_oracle_run(_oracle_run):
    ungated = run_replay([DATE], thesis=ORACLE, gate_arrival=False)[DATE]
    assert ungated["run_dir"] != _oracle_run["run_dir"]


def test_the_cli_exposes_thesis_and_thesis_file():
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "replay_session.py"),
                          "--help"], capture_output=True, text=True, cwd=REPO)
    assert "--thesis" in out.stdout
    assert "--thesis-file" in out.stdout
