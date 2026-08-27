"""Gate 2 -- the replay path must reproduce the live path's decisions.

The 2026-08-25 cycle-1 shadow session is the only day both paths have run. Byte identity
is NOT expected: live accumulated bars from ticks while replay reads the 1s parquet. What
must match is the DECISIONS -- same mechanism, same bound artifact, same trigger and stop,
same veto reason.

The live thesis was armed at 10:40 (ACT_TRADER_ARM_HHMM), so this runs a non-standard
window on purpose.

DIVERGENCE from the plan: the replay runs against a PRIVATE cache directory
(ACT_THESIS_CACHE_DIR -> tmp). The plan let it write into `<global>/thesis_cache`, which
would have deposited a synthetic 08-25 recording -- keyed on a stand-in backend, not a
real call -- into the shared cache every worktree reads.
"""
import json
import os

import pandas as pd
import pytest

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "live_20260825")
DATE = "2026-08-25"
TZ = "America/New_York"

# See test_gate_replay.py: the project's 60 s default is sized for unit tests, and the
# module fixture below replays a real session.
pytestmark = pytest.mark.timeout(900)


def _load(name):
    p = os.path.join(FIX, name)
    if not os.path.exists(p):
        pytest.skip(f"fixture {name} absent -- see plan Task 9 Step 1")
    if name.endswith(".jsonl"):
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    return json.load(open(p, encoding="utf-8"))


def _key(rec):
    """The decision identity: what it decided, on what, at what prices."""
    return (rec.get("kind"), rec.get("mechanism"), rec.get("artifact_id"),
            rec.get("trigger"), rec.get("stop"), rec.get("reason"))


def _replay_records(run_dir):
    p = os.path.join(run_dir, "trader_decisions.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


@pytest.fixture(scope="module")
def _replayed(tmp_path_factory):
    from agent.trader.replay import run_replay
    import agent.trader.replay as R

    live_thesis = _load("thesis_state.json").get("thesis")
    if live_thesis is None:
        pytest.skip("live fixture has no thesis")

    # Serve the live thesis directly rather than through a real call: the point is to
    # reproduce the live DECISIONS, and re-calling the model would test the model.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(R, "_real_backend", lambda: (lambda *a, **k: (live_thesis, {})))
    day = pd.Timestamp(DATE, tz=TZ).normalize()
    monkey.setattr(R, "replay_window_for",
                   lambda d: (day + pd.Timedelta(hours=10, minutes=39),
                              day + pd.Timedelta(hours=11, minutes=0)))
    monkey.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path_factory.mktemp("fidelity_cache")))
    try:
        res = run_replay([DATE], allow_calls=True, arrival_latency_sec=40.0,
                         arm_hhmm="10:40")
    finally:
        monkey.undo()
    return res[DATE]["run_dir"]


def test_the_live_fixture_has_the_three_expected_records():
    recs = _load("trader_decisions.jsonl")
    kinds = [r["kind"] for r in recs]
    assert kinds == ["bind", "intended_entry", "veto"]


def test_replay_reproduces_the_same_decision_kinds_in_the_same_order(_replayed):
    live = [r["kind"] for r in _load("trader_decisions.jsonl")]
    replay = [r["kind"] for r in _replay_records(_replayed)]
    assert replay == live


def test_replay_binds_the_same_artifact(_replayed):
    live = [r for r in _load("trader_decisions.jsonl") if r["kind"] == "bind"][0]
    rbind = [r for r in _replay_records(_replayed) if r["kind"] == "bind"][0]
    assert rbind["artifact_id"] == live["artifact_id"]
    assert rbind["artifact_label"] == live["artifact_label"]


def test_replay_reproduces_the_intended_entry_trigger_and_stop(_replayed):
    live = [r for r in _load("trader_decisions.jsonl")
            if r["kind"] == "intended_entry"][0]
    rentry = [r for r in _replay_records(_replayed)
              if r["kind"] == "intended_entry"][0]
    assert rentry["trigger"] == live["trigger"] == 29277.0
    assert rentry["stop"] == live["stop"] == 29291.0
    assert rentry["dol"] == live["dol"] == 29157.5


def test_replay_reproduces_the_max_distance_veto(_replayed):
    live = [r for r in _load("trader_decisions.jsonl") if r["kind"] == "veto"][0]
    rveto = [r for r in _replay_records(_replayed) if r["kind"] == "veto"][0]
    assert rveto["reason"] == live["reason"] == "max_distance"
    assert rveto["detail"]["cap"] == live["detail"]["cap"] == 60.0


def test_the_full_decision_identity_matches(_replayed):
    live = [_key(r) for r in _load("trader_decisions.jsonl")]
    rep = [_key(r) for r in _replay_records(_replayed)]
    assert rep == live


def test_the_arm_env_override_is_restored_after_the_run(_replayed):
    """`build_replay_trader` has to SET ACT_TRADER_ARM_HHMM; leaving it set would re-arm
    every later run in the same process. Divergence fix from the plan."""
    assert os.environ.get("ACT_TRADER_ARM_HHMM") is None
