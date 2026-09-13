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
pytestmark = [pytest.mark.timeout(900), pytest.mark.slow]


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
    # This window is CALIBRATED against the live run's own decisions, so it stays
    # 10:39-11:00 whatever `WINDOW_END_ET` says (13:00 since 2026-09-09). The stub takes
    # `window_end` and ignores it: the signature has to track `replay_window_for`'s, and
    # a stub that silently accepted fewer arguments is what made this a TypeError rather
    # than a fidelity failure.
    monkey.setattr(R, "replay_window_for",
                   lambda d, window_end=None: (day + pd.Timedelta(hours=10, minutes=39),
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


# ── ERA NOTE (2026-08-28, extended 2026-08-29) ─────────────────────────────────
# The live fixture was captured in cycle 1, and it predates TWO rules it cannot be held
# against.
#
# 1. NO FILL MODEL. Its order could never fill, so the Executor kept re-evaluating the
#    same binding and eventually vetoed it on max_distance at 10:43.
#
# 2. NO §5 FRESH-RETRACE PRECONDITION (settled 2026-08-26, implemented this cycle). The
#    fixture's `intended_entry` is a LITERAL-reading entry: the order was placed on
#    ELIGIBILITY ALONE, at a trigger already 40.25 pts crossed (bind price 29237.75 vs
#    trigger 29277.0 on a short). §5 now requires price to retrace INTO the bound gap on
#    a tick after the settle window — and over this fixture's ENTIRE window price never
#    touches [29280.0, 29288.0] at all: the window high is 29268.5, eleven and a half
#    points below the gap's bottom, and falling. There is no retrace to be fresh about.
#
#    That is exactly the pathology the 19-day A/B settled against: "the guard delays the
#    literal entry into a chase rather than blocking it". So the replay declining here is
#    the RULE WORKING, not fidelity lost — and `test_the_gap_is_never_retraced_into`
#    below pins the tape fact so this cannot become an excuse for a real regression.
#
# What still must match, and does: the thesis, the plan, the bound artifact
# (1dfeba17a1c9), and the BIND's trigger/stop (29277 / 29291). Those are the fidelity
# claims. The comparison is made over the COMMON PREFIX for that reason.
LIVE_ERA_TAIL = ("veto", "intended_entry")   # kinds the fixture carries from an older era

GAP_LOW, GAP_HIGH = 29280.0, 29288.0


def _pre_lifecycle(records):
    """The fixture's records up to the point its era diverges from ours."""
    return [r for r in records if r["kind"] not in LIVE_ERA_TAIL]


def test_replay_reproduces_the_same_decision_kinds_in_the_same_order(_replayed):
    """Over the COMMON PREFIX — both sides stripped of the kinds their own era produces
    for reasons the other era cannot have."""
    live = [r["kind"] for r in _pre_lifecycle(_binding_only(_load("trader_decisions.jsonl")))]
    replay = [r["kind"] for r in _pre_lifecycle(_binding_only(_replay_records(_replayed)))]
    assert replay == live, "the common prefix must match; see ERA NOTE for the tail"


def _gap_untouched_in_window():
    """(checked, touched) — whether the tape is present, and whether price ever came
    into the bound gap during the fidelity window."""
    from backtest_smt import _main_dir_for_date
    path = _main_dir_for_date(DATE) / "MNQ_1s.parquet"
    if not path.exists():
        return False, None
    df = pd.read_parquet(path)
    bars = df[(df.index >= pd.Timestamp(f"{DATE} 10:39", tz=TZ))
              & (df.index <= pd.Timestamp(f"{DATE} 11:00", tz=TZ))]
    if not len(bars):
        return False, None
    return True, bool(((bars["High"] >= GAP_LOW) & (bars["Low"] <= GAP_HIGH)).any())


def test_the_replay_declines_the_entry_under_the_fresh_retrace_rule(_replayed):
    """§5's precondition is unmet for the whole window, so nothing may be placed and
    nothing may fill. The fixture's own entry is a LITERAL-era chase at a trigger already
    40.25 pts crossed.

    The TAPE FACT is asserted in the same test, not beside it. Split apart, this
    assertion certifies inertness: finding a future bug that stops §5 placing would make
    it pass, and a machine without the out-of-repo 1s tape would skip the control while
    the weakened gate still went green. Fused, "the rule is working" and "why" stand or
    fall together — and with no tape the whole claim is skipped rather than half-made.
    """
    checked, touched = _gap_untouched_in_window()
    if not checked:
        pytest.skip(f"no 1s tape for {DATE} — the decline claim is unverifiable")
    assert not touched, "price DID re-enter the gap; the era note no longer applies"

    rec = _replay_records(_replayed)
    assert [r for r in rec if r["kind"] == "bind"], "the binding itself must survive"
    assert not [r for r in rec if r["kind"] == "intended_entry"]
    assert not [r for r in rec if r["kind"] in LIFECYCLE_KINDS]
    assert not [r for r in _load("trader_decisions.jsonl")
                if r["kind"] in LIFECYCLE_KINDS],         "the live fixture predates the lifecycle; if it carries one, re-pin this gate"


def test_the_window_high_stays_below_the_gap_by_a_wide_margin():
    """Not a near miss: the window high is 29268.5, eleven and a half points under the
    gap's bottom, and falling. A marginal miss would deserve a different era note."""
    from backtest_smt import _main_dir_for_date
    path = _main_dir_for_date(DATE) / "MNQ_1s.parquet"
    if not path.exists():
        pytest.skip(f"no 1s tape for {DATE}")
    df = pd.read_parquet(path)
    bars = df[(df.index >= pd.Timestamp(f"{DATE} 10:39", tz=TZ))
              & (df.index <= pd.Timestamp(f"{DATE} 11:00", tz=TZ))]
    assert float(bars["High"].max()) <= GAP_LOW - 10.0


def test_replay_binds_the_same_artifact(_replayed):
    live = [r for r in _load("trader_decisions.jsonl") if r["kind"] == "bind"][0]
    rbind = [r for r in _replay_records(_replayed) if r["kind"] == "bind"][0]
    assert rbind["artifact_id"] == live["artifact_id"]
    assert rbind["artifact_label"] == live["artifact_label"]


def test_replay_reproduces_the_trigger_and_stop_the_live_run_computed(_replayed):
    """Read off the BIND, which both eras emit. The prices are the fidelity claim; which
    record kind carries them is an era detail."""
    live = [r for r in _load("trader_decisions.jsonl")
            if r["kind"] == "intended_entry"][0]
    rbind = [r for r in _replay_records(_replayed) if r["kind"] == "bind"][0]
    assert rbind["trigger"] == live["trigger"] == 29277.0
    assert rbind["stop"] == live["stop"] == 29291.0
    assert live["dol"] == 29157.5


def test_the_max_distance_veto_survives_but_for_the_opposite_reason(_replayed):
    """The fixture's 10:43 veto exists because cycle 1 had nothing to fill. The current
    run vetoes too — because §5 declines to place — and the numbers behind it are the
    same tape, so the DETAIL must still name the 60-pt cap."""
    live_veto = [r for r in _load("trader_decisions.jsonl") if r["kind"] == "veto"]
    assert len(live_veto) == 1 and live_veto[0]["reason"] == "max_distance"
    assert live_veto[0]["detail"]["cap"] == 60.0
    rec = _replay_records(_replayed)
    vetoes = [r for r in rec if r["kind"] == "veto"]
    assert vetoes and vetoes[0]["reason"] == "max_distance"
    assert vetoes[0]["detail"]["cap"] == 60.0
    assert vetoes[0]["artifact_id"] == live_veto[0]["artifact_id"]


def test_the_full_decision_identity_matches(_replayed):
    live = [_key(r) for r in _pre_lifecycle(_binding_only(_load("trader_decisions.jsonl")))]
    rep = [_key(r) for r in _pre_lifecycle(_binding_only(_replay_records(_replayed)))]
    assert rep == live, "artifact, trigger and stop must match across the common prefix"


def test_the_arm_env_override_is_restored_after_the_run(_replayed):
    """`build_replay_trader` has to SET ACT_TRADER_ARM_HHMM; leaving it set would re-arm
    every later run in the same process. Divergence fix from the plan."""
    assert os.environ.get("ACT_TRADER_ARM_HHMM") is None
