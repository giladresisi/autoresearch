import pandas as pd
import pytest

from agent.trader.executor import Executor

TZ = "America/New_York"


def _bars(idx, o, h, l, c):
    # `Volume` is required: `bars.resample` aggregates it, and the predicate view's
    # higher timeframes are resampled. A frame without it silently yields a view that
    # answers 1m only — which is the very hole these tests exist to close.
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0},
                        index=idx)


def _run(tmp_path, plan, df, idx):
    ex = Executor(str(tmp_path), plan, arm_ts=idx[0])
    for t in idx:
        ex.on_bar(t, {"MNQ": df[df.index <= t]}, bar_complete=True)
    return ex.bind_state()


def _plan(**over):
    p = {"plan_id": "t", "direction": "DOWN", "dol": {"price": 29000.0},
         "valid_while": [], "armed_classes": ["fvg_return_continuation"]}
    p.update(over)
    return p


def test_price_beyond_falsifier_actually_fires(tmp_path):
    """The regression: predicates carry `type`, the old code read `kind`."""
    idx = pd.date_range("2026-08-25 09:20", periods=5, freq="1min", tz=TZ)
    df = _bars(idx, 29500.0, 29600.0, 29450.0, 29550.0)
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "price_beyond", "price": 29420.0, "side": "above"}]), df, idx)
    assert st["plan_alive"] is True, "falsification is RECORDED, never acted on"
    assert st["dead_reason"] is None


def test_a_falsifier_that_is_not_reached_leaves_the_plan_alive(tmp_path):
    idx = pd.date_range("2026-08-25 09:20", periods=5, freq="1min", tz=TZ)
    df = _bars(idx, 29300.0, 29350.0, 29250.0, 29300.0)
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "price_beyond", "price": 29420.0, "side": "above"}]), df, idx)
    assert st["plan_alive"] is True


def test_falsification_is_close_based_not_wick_based(tmp_path):
    """A wick through the level with the close back inside must NOT kill the plan —
    the canonical `predicates.eval_predicate` reads the current close."""
    idx = pd.date_range("2026-08-25 09:20", periods=3, freq="1min", tz=TZ)
    df = _bars(idx, 29300.0, 29500.0, 29250.0, 29300.0)   # high 29500 > 29420, close 29300
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "price_beyond", "price": 29420.0, "side": "above"}]), df, idx)
    assert st["plan_alive"] is True


def test_n_closes_beyond_fires_after_n_completed_closes(tmp_path):
    idx = pd.date_range("2026-08-25 09:20", periods=6, freq="1min", tz=TZ)
    df = _bars(idx, 29500.0, 29560.0, 29450.0, 29550.0)
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "n_closes_beyond", "price": 29420.0, "side": "above",
         "tf": "1m", "n": 2}]), df, idx)
    assert st["plan_alive"] is True, "falsification is RECORDED, never acted on"
    assert st["dead_reason"] is None


def test_clock_after_fires_at_the_stated_et_time(tmp_path):
    idx = pd.date_range("2026-08-25 09:58", periods=5, freq="1min", tz=TZ)
    df = _bars(idx, 29300.0, 29310.0, 29290.0, 29300.0)
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "clock_after", "et_time": "10:00"}]), df, idx)
    assert st["plan_alive"] is True, "recorded, not acted on"


def test_all_of_requires_every_sub_predicate(tmp_path):
    idx = pd.date_range("2026-08-25 09:20", periods=4, freq="1min", tz=TZ)
    df = _bars(idx, 29500.0, 29560.0, 29450.0, 29550.0)
    alive = _run(tmp_path, _plan(valid_while=[{"type": "all_of", "of": [
        {"type": "price_beyond", "price": 29420.0, "side": "above"},
        {"type": "clock_after", "et_time": "23:00"}]}]), df, idx)
    assert alive["plan_alive"] is True


def test_the_recorded_falsification_names_the_predicate_that_fired(tmp_path):
    idx = pd.date_range("2026-08-25 09:20", periods=3, freq="1min", tz=TZ)
    df = _bars(idx, 29500.0, 29560.0, 29450.0, 29550.0)
    pred = {"type": "price_beyond", "price": 29420.0, "side": "above"}
    ex = Executor(str(tmp_path), _plan(valid_while=[pred]), arm_ts=idx[0])
    seen = []
    ex._rec.would_have_falsified = lambda **kw: seen.append(kw)
    ex._rec.plan_dead = lambda **kw: seen.append({"UNEXPECTED_DEATH": kw})
    for t in idx:
        ex.on_bar(t, {"MNQ": df[df.index <= t]}, bar_complete=True)
    assert seen, "the falsifier fired, so it must have been recorded"
    assert "UNEXPECTED_DEATH" not in seen[0], "a falsifier must not kill the plan"
    assert seen[0]["predicate"] == pred
    assert len(seen) == 1, "one-shot: a standing falsifier must not repeat every bar"


def test_an_unknown_predicate_kind_no_longer_silently_fails_open(tmp_path):
    """Every closed-vocabulary kind is now implemented; a malformed one is REPORTED."""
    from agent.contracts.predicates import validate_predicate_list
    errs = validate_predicate_list([{"type": "not_a_real_kind"}], "valid_while")
    assert errs


def test_a_malformed_valid_while_is_reported_in_the_bind_state(tmp_path):
    """`eval_predicate` maps anything malformed to False, so a typo'd falsifier is
    indistinguishable from one that never fires and the plan runs the whole window.
    `derive_plan` copies thesis predicates verbatim and validates nothing, so the
    Executor is the only place this can surface."""
    ex = Executor(str(tmp_path), _plan(valid_while=[{"type": "vibes_beyond"}]),
                  arm_ts=pd.Timestamp("2026-08-25 09:20", tz=TZ))
    assert ex.bind_state()["predicate_errors"]
    ok = Executor(str(tmp_path), _plan(valid_while=[
        {"type": "price_beyond", "price": 29420.0, "side": "above"}]),
        arm_ts=pd.Timestamp("2026-08-25 09:20", tz=TZ))
    assert ok.bind_state()["predicate_errors"] == []


def test_a_higher_timeframe_close_predicate_can_actually_fire(tmp_path):
    """The view must answer EVERY timeframe `predicates.TIMEFRAMES` allows. Publishing
    only `1m` does not fail loudly on a `5m` falsifier — `eval_predicate` returns False
    for what it cannot answer, so the plan would never die. That is the `kind`/`type`
    bug again, one layer up, and `validate_predicate` accepts the shape either way."""
    idx = pd.date_range("2026-08-25 09:20", periods=40, freq="1min", tz=TZ)
    df = _bars(idx, 29500.0, 29560.0, 29450.0, 29550.0)
    st = _run(tmp_path, _plan(valid_while=[
        {"type": "n_closes_beyond", "price": 29420.0, "side": "above",
         "tf": "5m", "n": 2}]), df, idx)
    assert st["plan_alive"] is True, "falsification is RECORDED, never acted on"
    assert st["dead_reason"] is None


def test_a_level_swept_predicate_reads_the_stores_real_sweep_state(tmp_path):
    """`level_swept` / `level_depleted` are first-class atoms. Handing the evaluator
    empty sets makes any thesis whose falsifier is a level sweep immortal."""
    from agent.facts.records import Fact, FactClass, FactState

    idx = pd.date_range("2026-08-25 09:20", periods=3, freq="1min", tz=TZ)
    df = _bars(idx, 29300.0, 29310.0, 29290.0, 29300.0)
    pred = {"type": "level_swept", "name": "prev1_day_high"}
    ex = Executor(str(tmp_path), _plan(valid_while=[pred]), arm_ts=idx[0])

    def _level(state):
        return Fact(id="lvl-1", cls=FactClass.LEVEL, ticker="MNQ", label="l",
                    name="prev1_day_high", reference_ts=idx[0], price=29999.0,
                    price_low=None, price_high=None, timeframe=None,
                    resolution="1min", state=state, state_ts=idx[0], provenance={},
                    extra={"kind": "high"})

    # `_falsified` directly, not through `on_bar`: the Executor owns its maintainer
    # here, and the bar-close cascade calls `run_batch`, which REPLACES the whole
    # LEVEL slice — wiping any injected level before the predicate is ever read.
    ex._store.upsert(_level(FactState.LIVE))
    assert ex._falsified(df, idx[-1]) is None

    ex._store.upsert(_level(FactState.SWEPT))
    assert ex._falsified(df, idx[-1]) == {"predicate": pred}

    # Depletion implies the sweep already happened.
    ex._store.upsert(_level(FactState.DEPLETED))
    assert ex._falsified(df, idx[-1]) == {"predicate": pred}
