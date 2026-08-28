import json
import pandas as pd
import pytest
from agent.trader.executor import Executor, SETTLE_UNTIL_SECONDS
from agent.trader.records import DECISIONS_FILE

PLAN = {"plan_id": "p1", "thesis_id": "t1", "direction": "DOWN",
        "dol": {"level": "prev1_week_low", "price": 29533.5},
        "valid_while": [], "armed_classes": ["fvg_return_continuation"],
        "attempts_used": 0, "blacklist": [], "cooldown_until": None}


def _bars(start="2026-08-13 09:00", n=120, base=29800.0):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    s = pd.Series(range(n), index=idx).astype(float) * -0.5 + base
    return pd.DataFrame({"Open": s, "High": s + 4, "Low": s - 4, "Close": s,
                         "Volume": 1.0}, index=idx)


BARS = {"MNQ": _bars(), "MES": _bars()}
ARM = pd.Timestamp("2026-08-13 09:20", tz="America/New_York")


def _kinds(tmp_path):
    path = tmp_path / DECISIONS_FILE
    if not path.exists():
        return []
    return [json.loads(l)["kind"]
            for l in path.read_text(encoding="utf-8").strip().split("\n") if l]


def _recs(tmp_path):
    path = tmp_path / DECISIONS_FILE
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").strip().split("\n") if l]


def test_executor_never_imports_live_orders():
    import inspect
    import agent.trader.executor as mod
    src = inspect.getsource(mod)
    assert "live_orders" not in src


def test_runs_from_the_arm_not_from_0930(tmp_path):
    """l2 §2: in-window penetrations count for eligibility tracking."""
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:22", tz="America/New_York"), BARS)
    assert ex.bind_state()["tracking"] is True


def test_no_entry_record_during_the_settle_window(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    for m in range(20, 31):
        ex.on_bar(pd.Timestamp(f"2026-08-13 09:{m:02d}", tz="America/New_York"), BARS)
    assert "intended_entry" not in _kinds(tmp_path)


def test_settle_window_ends_at_093030(tmp_path):
    assert SETTLE_UNTIL_SECONDS == 30


def test_dol_floor_veto_is_recorded_with_remaining_distance(tmp_path):
    """l2 §2: >= 60 pts must remain between trigger and DOL."""
    near = dict(PLAN, dol={"level": "x", "price": 29560.0})
    ex = Executor(tmp_path, plan=near, arm_ts=ARM)
    for m in range(31, 60):
        ex.on_bar(pd.Timestamp(f"2026-08-13 09:{m:02d}", tz="America/New_York"), BARS)
    for r in _recs(tmp_path):
        if r.get("reason") == "dol_floor":
            assert r["kind"] != "intended_entry"


def test_max_distance_guard_blocks_a_far_trigger(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35", tz="America/New_York"), BARS)
    assert ex.bind_state() is not None


def test_plan_death_is_evaluated_with_nothing_open(tmp_path):
    """l2 §7 scope lesson: unscoped, it fires hours after the plan completed."""
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    hit = _bars(base=29533.0)
    ex.on_bar(pd.Timestamp("2026-08-13 10:00", tz="America/New_York"),
              {"MNQ": hit, "MES": hit})
    assert ex.bind_state()["plan_alive"] is False


def test_executor_goes_dark_when_the_plan_dies(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    hit = _bars(base=29533.0)
    ex.on_bar(pd.Timestamp("2026-08-13 10:00", tz="America/New_York"), {"MNQ": hit, "MES": hit})
    before = ex.bind_state()
    ex.on_bar(pd.Timestamp("2026-08-13 10:05", tz="America/New_York"), {"MNQ": hit, "MES": hit})
    assert ex.bind_state()["plan_alive"] is False and before["plan_alive"] is False


def test_per_second_path_does_no_fvg_detection(tmp_path):
    """Per-second work stays O(small); detection runs on bar-close cascades."""
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35:14", tz="America/New_York"), BARS, bar_complete=False)
    assert ex.bind_state()["last_cascade"] is None


def test_executor_writes_no_legacy_state_file(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35", tz="America/New_York"), BARS)
    for forbidden in ("events.jsonl", "daily.json", "hypothesis.json", "position.json", "smts.json"):
        assert not (tmp_path / forbidden).exists()


def test_executor_swallows_detector_exceptions(tmp_path, monkeypatch):
    import agent.facts.incremental as inc
    monkeypatch.setattr(inc, "run_incremental", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35", tz="America/New_York"), BARS)


# --- added during implementation (not in the plan) --------------------------- #
# The plan's fixture is a smooth ramp, which contains NO 5m FVGs at all, so every
# binding/guard assertion above passes vacuously. These use a tape that actually
# produces a bear gap.

def _gappy(start="2026-08-13 09:00", n=180, base=29800.0):
    """A staircase down with displacement bars, so 5m bear FVGs really form."""
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    px, cur = [], base
    for i in range(n):
        cur -= 12.0 if (i % 15) in (5, 6, 7) else 0.2
        px.append(cur)
    s = pd.Series(px, index=idx)
    return pd.DataFrame({"Open": s, "High": s + 1.5, "Low": s - 1.5, "Close": s,
                         "Volume": 1.0}, index=idx)


GAPPY = {"MNQ": _gappy(), "MES": _gappy()}


def _drive(tmp_path, plan, first="09:20", last="10:30"):
    ex = Executor(tmp_path, plan=plan, arm_ts=ARM)
    idx = GAPPY["MNQ"].index
    lo = pd.Timestamp(f"2026-08-13 {first}", tz="America/New_York")
    hi = pd.Timestamp(f"2026-08-13 {last}", tz="America/New_York")
    for ts in idx[(idx >= lo) & (idx <= hi)]:
        # Live cadence: no flag. The minute rollover drives the cascade, which is what
        # automation/main.py actually produces.
        ex.on_bar(ts, {tk: df[df.index <= ts] for tk, df in GAPPY.items()})
    return ex


def test_a_real_bear_gap_produces_an_intended_entry_record(tmp_path):
    far_dol = dict(PLAN, dol={"level": "x", "price": 28000.0})
    _drive(tmp_path, far_dol)
    recs = [r for r in _recs(tmp_path) if r["kind"] == "intended_entry"]
    assert recs, f"expected an intended entry; got kinds {set(_kinds(tmp_path))}"
    r = recs[0]
    assert r["trigger"] < r["stop"], "a SHORT enters below and stops above"
    assert r["artifact_id"] and r["artifact_label"]


def test_stop_is_capped_at_25_points_from_the_trigger(tmp_path):
    far_dol = dict(PLAN, dol={"level": "x", "price": 28000.0})
    _drive(tmp_path, far_dol)
    for r in _recs(tmp_path):
        if r["kind"] == "intended_entry":
            assert r["stop"] - r["trigger"] <= 25.0 + 1e-9


def test_dol_floor_veto_fires_and_suppresses_the_entry(tmp_path):
    """A DOL sitting right under price leaves < 60 pts and must veto every entry."""
    near = dict(PLAN, dol={"level": "x", "price": 29700.0})
    _drive(tmp_path, near, last="09:45")
    recs = _recs(tmp_path)
    vetoes = [r for r in recs if r.get("reason") == "dol_floor"]
    assert vetoes, f"expected a dol_floor veto; got {[r['kind'] for r in recs]}"
    assert "remaining" in vetoes[0]["detail"]
    assert not [r for r in recs if r["kind"] == "intended_entry"]


def test_no_entry_before_the_settle_window_ends_even_with_a_live_gap(tmp_path):
    far_dol = dict(PLAN, dol={"level": "x", "price": 28000.0})
    ex = _drive(tmp_path, far_dol, first="09:20", last="09:30")
    assert "intended_entry" not in _kinds(tmp_path)
    assert ex.bind_state()["in_settle"] is True


def test_settle_window_is_open_at_0930_and_closed_at_0931(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:30:00", tz="America/New_York"), BARS, bar_complete=False)
    assert ex.bind_state()["in_settle"] is True
    ex.on_bar(pd.Timestamp("2026-08-13 09:31:00", tz="America/New_York"), BARS, bar_complete=False)
    assert ex.bind_state()["in_settle"] is False


def test_plan_death_is_recorded_exactly_once(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    hit = _bars(base=29533.0)
    for m in range(0, 10):
        ex.on_bar(pd.Timestamp(f"2026-08-13 10:{m:02d}", tz="America/New_York"),
                  {"MNQ": hit, "MES": hit})
    assert _kinds(tmp_path).count("plan_dead") == 1


def test_a_dead_plan_emits_nothing_further(tmp_path):
    ex = Executor(tmp_path, plan=dict(PLAN, dol={"level": "x", "price": 29799.0}),
                  arm_ts=ARM)
    for ts in GAPPY["MNQ"].index[:60]:
        ex.on_bar(ts, {tk: df[df.index <= ts] for tk, df in GAPPY.items()})
    assert "intended_entry" not in _kinds(tmp_path)
    assert ex.bind_state()["plan_alive"] is False


def test_blacklisted_gap_is_never_bound(tmp_path):
    far_dol = dict(PLAN, dol={"level": "x", "price": 28000.0})
    ex = _drive(tmp_path, far_dol)
    bound = ex.bind_state()["bound_id"]
    assert bound
    ex2 = _drive(tmp_path / "b", dict(far_dol, blacklist=[bound]))
    assert ex2.bind_state()["bound_id"] != bound


def test_falsified_price_predicate_kills_the_plan(tmp_path):
    plan = dict(PLAN, dol={"level": "x", "price": 28000.0},
                valid_while=[{"type": "price_beyond", "side": "below", "price": 29790.0}])
    ex = _drive(tmp_path, plan, last="09:40")
    assert ex.bind_state()["plan_alive"] is False
    assert ex.bind_state()["dead_reason"] == "falsified"


def test_unknown_predicate_kinds_do_not_kill_the_plan(tmp_path):
    plan = dict(PLAN, dol={"level": "x", "price": 28000.0},
                valid_while=[{"type": "some_future_predicate", "whatever": 1}])
    ex = _drive(tmp_path, plan, last="09:40")
    assert ex.bind_state()["plan_alive"] is True


# --- regression tests for the code-review findings --------------------------- #

def test_pre_arm_dol_traversal_does_not_kill_the_plan(tmp_path):
    """The frame reaches back to the 18:00 session open. Overnight traversal of a level
    the plan later names as its DOL is routine — judging a plan by price action that
    PREDATES it killed almost every plan on its first bar."""
    idx = pd.date_range("2026-08-13 09:00", periods=180, freq="1min", tz="America/New_York")
    # Pre-arm the tape dips through the DOL (29700); post-arm it stays well above it.
    px = [29710.0] * 30 + [29800.0] * 150
    s = pd.Series(px, index=idx)
    bars = pd.DataFrame({"Open": s, "High": s + 2, "Low": s - 12, "Close": s,
                         "Volume": 1.0}, index=idx)
    assert bars["Low"].min() <= 29700.0 <= bars[bars.index >= "2026-08-13 10:30"]["Low"].min()
    plan = dict(PLAN, dol={"level": "x", "price": 29700.0})
    arm = pd.Timestamp("2026-08-13 10:30", tz="America/New_York")
    ex = Executor(tmp_path, plan=plan, arm_ts=arm)
    ex.on_bar(arm, {"MNQ": bars, "MES": bars})
    assert ex.bind_state()["plan_alive"] is True, (
        "pre-arm price touched the DOL; post-arm price never did")


def test_post_arm_dol_touch_still_kills_the_plan(tmp_path):
    """The counterpart: the arm slice must not make the death check blind."""
    hit = _bars(base=29533.0)
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 10:00", tz="America/New_York"),
              {"MNQ": hit, "MES": hit})
    assert ex.bind_state()["plan_alive"] is False


def test_bar_close_is_derived_from_a_minute_rollover(tmp_path):
    """The live driver hard-codes bar_complete=False on every tick, so the Executor has
    to notice the rollover itself or it never cascades at all."""
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    base = pd.Timestamp("2026-08-13 09:35:00", tz="America/New_York")
    ex.on_bar(base, BARS, bar_complete=False)                  # seeds the minute
    assert ex.bind_state()["last_cascade"] is None
    ex.on_bar(base + pd.Timedelta(seconds=30), BARS, bar_complete=False)
    assert ex.bind_state()["last_cascade"] is None, "same minute — still a partial"
    ex.on_bar(base + pd.Timedelta(minutes=1), BARS, bar_complete=False)
    assert ex.bind_state()["last_cascade"] is not None, "the minute rolled — cascade"


def test_bar_complete_none_behaves_like_the_auto_detect_it_names(tmp_path):
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    base = pd.Timestamp("2026-08-13 09:35:00", tz="America/New_York")
    ex.on_bar(base, BARS)
    ex.on_bar(base + pd.Timedelta(minutes=1), BARS)
    assert ex.bind_state()["last_cascade"] is not None


def test_explicit_bar_complete_true_still_cascades_immediately(tmp_path):
    """The 1m regression driver (backtest_smt.py:1505) does pass True."""
    ex = Executor(tmp_path, plan=PLAN, arm_ts=ARM)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35", tz="America/New_York"), BARS,
              bar_complete=True)
    assert ex.bind_state()["last_cascade"] is not None


def test_coverage_watermark_reports_what_the_bars_actually_hold(tmp_path):
    """A 14-day request over a 2-hour slab covers 2 hours. Claiming 14 days makes
    health() and the coverage precondition lie."""
    from agent.facts.records import FactClass
    ex = Executor(tmp_path, plan=dict(PLAN, dol={"level": "x", "price": 28000.0}),
                  arm_ts=ARM)
    # Live cadence: the first call only seeds the minute, the second rolls it.
    ex.on_bar(pd.Timestamp("2026-08-13 09:34", tz="America/New_York"), BARS)
    ex.on_bar(pd.Timestamp("2026-08-13 09:35", tz="America/New_York"), BARS)
    covered = ex._store.covered_from(FactClass.LEVEL, "MNQ")
    assert covered is not None
    assert covered >= BARS["MNQ"].index[0], (
        f"watermark {covered} claims history before the first bar "
        f"{BARS['MNQ'].index[0]}")
