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


# The record kinds that EXISTED when the live fixture was captured (cycle 1, before the
# simulated order lifecycle). The Executor now also books `fill` / `stop_out` /
# `take_profit`, which the 2026-08-25 live run could not have written because the
# lifecycle did not exist yet. Comparing the raw record lists therefore compares a
# current replay against a fixture of an older vintage and fails for a reason that has
# nothing to do with fidelity. Both sides are restricted to these kinds; that the new
# ones are genuinely present is asserted separately, below, so the restriction cannot
# quietly absorb a lost decision.
LIVE_RECORD_KINDS = ("bind", "unbind", "intended_entry", "veto", "plan_dead")
LIFECYCLE_KINDS = ("fill", "stop_out", "take_profit")


def _binding_only(recs):
    return [r for r in recs if r.get("kind") in LIVE_RECORD_KINDS]


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


# ── ERA NOTE (2026-08-28) ───────────────────────────────────────────────────────
# The live fixture was captured in cycle 1, which had NO fill model. Its order could
# never fill, so the Executor kept re-evaluating the same binding and eventually vetoed
# it on max_distance at 10:43. Under the Phase-1 order lifecycle the trigger (29277, a
# short) is already crossed at the 10:41 bind — price 29237.75 — so §2's crossed-trigger
# rule fills it, and a filled position correctly stops further binding. The veto is
# therefore a cycle-1 ARTEFACT of having nothing to fill, not a behaviour to preserve.
#
# What still must match, and does: the thesis, the plan, the bound artifact
# (1dfeba17a1c9), and the intended entry's trigger/stop (29277 / 29291). Those are the
# fidelity claims. The comparison is made over the COMMON PREFIX for that reason.
LIVE_ERA_TAIL = ("veto",)   # kinds the fixture carries only because it never filled


def _pre_lifecycle(records):
    """The fixture's records up to the point its era diverges from ours."""
    return [r for r in records if r["kind"] not in LIVE_ERA_TAIL]

def test_replay_reproduces_the_same_decision_kinds_in_the_same_order(_replayed):
    live = [r["kind"] for r in _pre_lifecycle(_binding_only(_load("trader_decisions.jsonl")))]
    replay = [r["kind"] for r in _binding_only(_replay_records(_replayed))]
    assert replay == live, "the common prefix must match; see ERA NOTE for the tail"


def test_the_replay_also_books_the_order_lifecycle_the_live_fixture_predates(_replayed):
    """The counterpart to `LIVE_RECORD_KINDS`: the filtered comparisons above are only
    honest if the excluded records actually exist. 08-25's intended entry rests, and the
    tape reaches its trigger, so the replay must book a fill the fixture cannot carry."""
    assert not [r for r in _load("trader_decisions.jsonl")
                if r["kind"] in LIFECYCLE_KINDS], \
        "the live fixture predates the lifecycle; if it carries one, re-pin this gate"
    booked = [r["kind"] for r in _replay_records(_replayed)
              if r["kind"] in LIFECYCLE_KINDS]
    assert "fill" in booked, \
        "the resting stop-entry never filled -- the lifecycle is not being driven"


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


def test_the_live_veto_is_an_era_artefact_and_the_replay_fills_instead(_replayed):
    """Replaces the old "reproduce the veto" assertion. The fixture's 10:43 veto exists
    ONLY because cycle 1 had no fill model: its order rested unfilled, so the binding was
    re-evaluated until price ran 76.5 pts from the trigger. With the lifecycle the same
    already-crossed trigger fills, and a held position stops re-binding — so no veto can
    occur, and demanding one would pin a defect."""
    live_veto = [r for r in _load("trader_decisions.jsonl") if r["kind"] == "veto"]
    assert len(live_veto) == 1 and live_veto[0]["reason"] == "max_distance"
    assert live_veto[0]["detail"]["cap"] == 60.0
    rec = _replay_records(_replayed)
    assert not [r for r in rec if r["kind"] == "veto"],         "a filled position must not go on to veto its own binding"
    assert [r for r in rec if r["kind"] in LIFECYCLE_KINDS],         "if the replay neither vetoes NOR fills, the binding was lost — a real defect"


def test_the_full_decision_identity_matches(_replayed):
    live = [_key(r) for r in _pre_lifecycle(_binding_only(_load("trader_decisions.jsonl")))]
    rep = [_key(r) for r in _binding_only(_replay_records(_replayed))]
    assert rep == live, "artifact, trigger and stop must match across the common prefix"


def test_the_arm_env_override_is_restored_after_the_run(_replayed):
    """`build_replay_trader` has to SET ACT_TRADER_ARM_HHMM; leaving it set would re-arm
    every later run in the same process. Divergence fix from the plan."""
    assert os.environ.get("ACT_TRADER_ARM_HHMM") is None
