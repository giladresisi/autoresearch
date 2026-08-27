"""Cycle-2 acceptance gates 1, 3, 4, 5 and 6.

These require the cache to have been seeded for DATE (`scripts/replay_session.py --seed`)
and 1s parquet coverage for the window. They are skipped, not failed, when either is
absent -- a missing fixture is an environment problem, not a code defect.

DIVERGENCE from the plan, deliberate and in two places:

1. The plan calls `run_replay` once per test. A replay is a ~90 s 1s backtest, and seven
   of them buys no coverage the two module-scoped runs below do not already give. The two
   runs ARE the determinism gate, so they have to be separate calls; everything else reads
   run A.

2. The plan asserted gate 3 as `pytest.raises` around `run_replay`, and gate 5 as
   "completes without raising". Neither worked: `Analyzer._call` and `TraderGraft.on_bar`
   both swallow every exception, so a refused model call finished the run silently as a
   dark day and gate 5 would have passed against a stone. `run_replay` now surfaces the
   backend's counters and raises on a refusal, and gate 5 asserts `calls == 0` with at
   least one hit -- which is the claim, stated so it can fail.
"""
import json
import os
import shutil

import pytest

from agent.trader.cached_backend import NetworkCallRefused
from agent.trader.replay import replay_window_for, run_replay
from agent.trader.thesis_cache import ThesisCache

DATE = "2026-08-13"

# The project default is `--timeout=60` (pyproject addopts), sized for unit tests. Each
# module-scoped fixture here runs a real 1s backtest (~40-60 s), and that setup time is
# billed to whichever test requests the fixture first -- so under the default these pass
# alone and time out inside a larger session. The assertions are unchanged; only the
# wall-clock budget is raised, and only for the modules that replay a session.
pytestmark = pytest.mark.timeout(900)


def _decisions(run_dir):
    p = os.path.join(run_dir, "trader_decisions.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


@pytest.fixture(scope="module")
def _seeded():
    # Filter by BOUNDARY, not merely "the cache is non-empty". The cache is keyed on
    # (date x code version), so once a second date is seeded a bare non-empty check
    # passes while THIS date is still missing — and the run then fails hard with
    # NetworkCallRefused (allow_calls=False) instead of skipping with a usable message.
    # Same defect the forced-DOL fixture below carried; both are fixed the same way.
    seeded = [f for f in ThesisCache().root.glob("*.json")
              if str((json.loads(f.read_text(encoding="utf-8")) or {}).get("boundary")
                     or "").startswith(DATE)]
    if not seeded:
        pytest.skip(f"thesis cache not seeded for {DATE} -- "
                    f"run scripts/replay_session.py --dates {DATE} --seed")
    return True


@pytest.fixture(scope="module")
def _run_a(_seeded):
    return run_replay([DATE], allow_calls=False)[DATE]


@pytest.fixture(scope="module")
def _run_b(_seeded):
    return run_replay([DATE], allow_calls=False)[DATE]


# -- gate 5 / gate 3 --------------------------------------------------------- #

def test_gate5_a_warm_run_makes_zero_model_calls(_run_a):
    assert _run_a["cache"]["calls"] == 0, "a warm replay must not reach the model"
    assert _run_a["cache"]["refusals"] == 0
    assert _run_a["cache"]["hits"] >= 1, \
        "zero hits means the thesis never came from the recording -- gate 5 is vacuous"


def test_gate3_a_cold_cache_with_calls_disallowed_fails_rather_than_calling(tmp_path,
                                                                            monkeypatch):
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "empty"))
    with pytest.raises(NetworkCallRefused):
        run_replay([DATE], allow_calls=False)


# -- gate 1 ------------------------------------------------------------------ #

def test_gate1_the_two_replays_used_separate_run_directories(_run_a, _run_b):
    """Without this the determinism assertions below could be reading one file twice."""
    assert _run_a["run_dir"] != _run_b["run_dir"]


def test_gate1_two_identical_replays_produce_identical_decisions(_run_a, _run_b):
    assert _decisions(_run_a["run_dir"]) == _decisions(_run_b["run_dir"]), \
        "replay-vs-replay determinism"


def test_gate1_the_replay_actually_produced_decisions(_run_a):
    """A dark day would make every determinism assertion pass against nothing."""
    assert _decisions(_run_a["run_dir"]), \
        "no decisions -- the determinism gate would be vacuous"


def test_gate1_two_identical_replays_produce_identical_plan_stores(_run_a, _run_b):
    pa = json.load(open(os.path.join(_run_a["run_dir"], "plans.json"), encoding="utf-8"))
    pb = json.load(open(os.path.join(_run_b["run_dir"], "plans.json"), encoding="utf-8"))
    assert pa == pb


def test_gate8_a_warm_run_still_persists_the_recorded_call_meta(_run_a):
    """CODE-REVIEW FINDING. Run A is provably warm (`calls == 0`), so its `call_meta` can
    only have come from the recording. Before the fix it was `{}` and gate 8 passed only
    because seeding directories happened to remain on disk."""
    assert _run_a["cache"]["calls"] == 0
    blob = json.load(open(os.path.join(_run_a["run_dir"], "thesis_state.json"),
                          encoding="utf-8"))
    meta = blob.get("call_meta") or {}
    assert meta.get("latency_sec"), "a warm replay lost the recording's provenance"
    assert (meta.get("usage") or {}).get("input_tokens")


def test_gate1_two_identical_replays_produce_the_same_thesis(_run_a, _run_b):
    ta = json.load(open(os.path.join(_run_a["run_dir"], "thesis_state.json"),
                        encoding="utf-8"))
    tb = json.load(open(os.path.join(_run_b["run_dir"], "thesis_state.json"),
                        encoding="utf-8"))
    assert ta["thesis"] == tb["thesis"]
    assert ta["armed_at"] == tb["armed_at"]


# -- gate 6 ------------------------------------------------------------------ #

def test_gate6_the_first_decision_is_at_or_after_0920(_run_a):
    import pandas as pd
    recs = _decisions(_run_a["run_dir"])
    if not recs:
        pytest.skip("dark day -- no decisions to bound")
    w0, _ = replay_window_for(DATE)
    assert pd.Timestamp(recs[0]["time"]) >= w0


def test_gate6_no_decision_is_recorded_after_1100(_run_a):
    import pandas as pd
    recs = _decisions(_run_a["run_dir"])
    _, w1 = replay_window_for(DATE)
    for rec in recs:
        assert pd.Timestamp(rec["time"]) < w1


def test_gate6_the_arm_is_inside_the_window(_run_a):
    import pandas as pd
    blob = json.load(open(os.path.join(_run_a["run_dir"], "thesis_state.json"),
                          encoding="utf-8"))
    w0, w1 = replay_window_for(DATE)
    assert w0 <= pd.Timestamp(blob["armed_at"]) < w1


def test_gate6_a_dol_touch_is_recorded_with_its_timestamp(_run_a):
    dead = [x for x in _decisions(_run_a["run_dir"])
            if x.get("kind") == "plan_dead" and x.get("reason") == "dol_reached"]
    # 2026-08-13 was chosen precisely so this is NOT skippable: its seeded thesis is UP
    # with DOL prev1_day_high 30001.5, which the window reaches at 09:36:43. A date whose
    # DOL sits outside the window (as 2026-08-12's 30073.25 did) can only skip here, which
    # is why it was rejected as the validation date.
    assert dead, ("no DOL touch recorded -- this date's thesis must name a DOL the "
                  "09:20-11:00 window actually reaches, or the gate proves nothing")
    assert dead[0].get("time")


# -- gate 6, forced deterministically ---------------------------------------- #
#
# The recorded thesis is one sample from a non-deterministic call and MAY name a DOL the
# window never touches (2026-08-12's did, which is why it was rejected as the validation
# date). 2026-08-13's real touch at 09:36:43 exercises the property directly; these forced
# tests keep it provable even if a re-seed produces a different DOL. The SAME recording is
# copied into a private cache under the SAME key with only the DOL price rewritten to a
# level the window does reach, and the real replay path runs against it.

@pytest.fixture(scope="module")
def _forced_dol(tmp_path_factory):
    # Select the recording for THIS date. The cache accumulates one entry per
    # (date x code version), so `glob(...)[0]` picks an arbitrary date's thesis whose key
    # cannot match this date's facts -- the replay then misses and, with calls disallowed,
    # raises NetworkCallRefused instead of running.
    src = [f for f in ThesisCache().root.glob("*.json")
           if str((json.loads(f.read_text(encoding="utf-8")) or {}).get("boundary") or
                  "").startswith(DATE)]
    if not src:
        pytest.skip(f"thesis cache not seeded for {DATE}")
    blob = json.loads(src[0].read_text(encoding="utf-8"))
    thesis = blob.get("thesis") or {}
    if str(thesis.get("bias") or "").upper() != "UP":
        pytest.skip("forced-DOL fixture assumes an UP thesis")

    root = tmp_path_factory.mktemp("forced_cache")
    # 29990 is above the 09:20 price (~29896) and below the window high (30001.5), so it
    # is reached inside the window but not on the arming bar.
    thesis["dol"] = dict(thesis.get("dol") or {}, price=29990.0)
    blob["thesis"] = thesis
    (root / src[0].name).write_text(json.dumps(blob), encoding="utf-8")

    prev = os.environ.get("ACT_THESIS_CACHE_DIR")
    os.environ["ACT_THESIS_CACHE_DIR"] = str(root)
    try:
        res = run_replay([DATE], allow_calls=False)[DATE]
    finally:
        if prev is None:
            os.environ.pop("ACT_THESIS_CACHE_DIR", None)
        else:
            os.environ["ACT_THESIS_CACHE_DIR"] = prev
    return res


def test_gate6_forced_dol_is_recorded_as_plan_dead_with_a_timestamp(_forced_dol):
    dead = [x for x in _decisions(_forced_dol["run_dir"])
            if x.get("kind") == "plan_dead" and x.get("reason") == "dol_reached"]
    assert dead, "a DOL inside the window must produce plan_dead/dol_reached"
    assert dead[0].get("time")
    assert dead[0]["detail"]["dol"] == 29990.0


def test_gate6_the_run_does_not_stop_at_the_dol_touch(_forced_dol):
    """The whole point of recording rather than stopping: A/B windows stay comparable.

    Asserted against `last_bar` -- the last bar the GRAFT was handed. Two earlier
    formulations of this test could not fail:

      * the plan's read a `bar_state.json` last-bar stamp. Nothing in the tree writes
        that file in a replay, so the assertion would have skipped forever.
      * the first fix here asserted `facts_snapshot.json` exists. It does -- written
        ONCE per session date (`TraderGraft._maybe_snapshot` short-circuits on
        `_snapshot_date == now.date()`), on the first bar close after the plan arms,
        which is ~9 minutes BEFORE the forced DOL death. It would have passed unchanged
        had the runner aborted at the touch.

    Every on-disk artifact stops when the Executor stops writing. Only the bar counter
    distinguishes "stopped binding" from "stopped running", which is the claim.
    """
    import pandas as pd
    dead = [x for x in _decisions(_forced_dol["run_dir"])
            if x.get("kind") == "plan_dead" and x.get("reason") == "dol_reached"][0]
    _, w1 = replay_window_for(DATE)
    dead_ts = pd.Timestamp(dead["time"])
    assert dead_ts < w1 - pd.Timedelta(minutes=30), \
        "the DOL must die well before the window end for this test to mean anything"
    last_bar = _forced_dol["last_bar"]
    assert last_bar is not None
    assert last_bar > dead_ts, \
        f"the loop stopped at the DOL touch ({dead_ts}); last bar was {last_bar}"


def test_gate6_the_forced_run_still_covers_the_whole_window(_forced_dol):
    """Plan death stops BINDING, not the runner: the loop must reach the window end."""
    import pandas as pd
    _, w1 = replay_window_for(DATE)
    assert _forced_dol["last_bar"] == (w1 - pd.Timedelta(minutes=1)), \
        "the last bar must be the final minute of the window"
    assert _forced_dol["cache"]["calls"] == 0


def test_gate6_an_undying_plan_reaches_the_same_final_bar(_run_a):
    """The control for the test above: a run whose plan never dies ends on the same bar,
    so 'reached 11:00' is a property of the WINDOW, not of the plan's fate."""
    import pandas as pd
    _, w1 = replay_window_for(DATE)
    assert _run_a["last_bar"] == (w1 - pd.Timedelta(minutes=1))


def test_gate6_the_legacy_key_is_not_mistakable_for_executor_output(_run_a):
    """Global-constraint finding: the legacy engine DOES run in replay (the `trader=`
    seam is additive and does not early-return). Its trades must not be reachable under
    a name that reads like the Executor's."""
    assert "result" not in _run_a
    assert "legacy" in _run_a


# -- gate 4 ------------------------------------------------------------------ #

def test_gate4_editing_the_kb_invalidates_the_recording(tmp_path):
    """A doc edit is a strategy change; the next replay must re-record, not reuse."""
    from agent.trader.cached_backend import CachedThesisBackend
    from agent.trader.thesis_cache import ThesisCache as TC
    calls = {"n": 0}

    def _inner(ft, ct, f, *, evidence_magnitude=None):
        calls["n"] += 1
        return {"bias": "DOWN", "dol": {"price": 1.0}}, {}

    c = TC(root=tmp_path)
    v1 = CachedThesisBackend(_inner, cache=c, model_id="m",
                             system_prompt_fn=lambda: "KB v1",
                             task_prompt="T", schema_fn=lambda f: {})
    v2 = CachedThesisBackend(_inner, cache=c, model_id="m",
                             system_prompt_fn=lambda: "KB v2",
                             task_prompt="T", schema_fn=lambda f: {})
    v1("facts", "", {})
    v1("facts", "", {})
    assert calls["n"] == 1
    v2("facts", "", {})
    assert calls["n"] == 2


def test_gate4_the_real_kb_bytes_are_what_the_key_hashes():
    """The gate above uses stand-in strings. This pins that the replay wires the ACTUAL
    concatenated KB in as the system prompt, so a real doc edit really does invalidate."""
    from agent.trader.replay import _prompt_parts
    from agent import run_agent as ra
    sys_fn, task, _schema_fn, _model = _prompt_parts()
    assert sys_fn is ra.build_system_prompt
    assert task and task == ra._TASK_THESIS
