import pandas as pd

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import MAX_ATTEMPTS, Executor
from agent.trader.order_sim import RestingOrder

TZ = "America/New_York"


def _gap(lo, hi, direction="bull"):
    return Fact(id=f"g{lo}", cls=FactClass.FVG, ticker="MNQ", label=f"g{lo}", name=None,
                reference_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ), price=None,
                price_low=lo, price_high=hi, timeframe="5min", resolution="5min",
                state=FactState.LIVE, state_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ),
                provenance={}, extra={"direction": direction, "max_anti_excursion": 0.0})


def _ex(tmp_path):
    return Executor(str(tmp_path), {"plan_id": "t", "direction": "UP",
                                    "dol": {"price": 30001.5}, "blacklist": [],
                                    "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-13 09:20", tz=TZ))


def _stop(ex, artifact_id, price, now):
    ex._on_stop_out({"kind": "stop_out", "time": now, "price": price,
                     "artifact_id": artifact_id})


def test_the_attempt_cap_is_the_spec_value():
    assert MAX_ATTEMPTS == 3


def test_a_stop_out_consumes_an_attempt(tmp_path):
    ex = _ex(tmp_path)
    _stop(ex, "g1", 29880.0, pd.Timestamp("2026-08-13 09:33", tz=TZ))
    assert ex._plan["attempts_used"] == 1


def test_three_stop_outs_exhaust_the_plan(tmp_path):
    ex = _ex(tmp_path)
    for i in range(3):
        _stop(ex, f"g{i}", 29880.0,
              pd.Timestamp(f"2026-08-13 09:3{3 + i}", tz=TZ))
    reason, _ = ex._death(pd.DataFrame(), pd.Timestamp("2026-08-13 09:40", tz=TZ))
    assert reason == "attempts_exhausted"


def test_the_counter_is_per_plan_not_per_gap(tmp_path):
    """§8: 'regardless of which FVG each attempt bound'."""
    ex = _ex(tmp_path)
    _stop(ex, "gA", 29880.0, pd.Timestamp("2026-08-13 09:33", tz=TZ))
    _stop(ex, "gB", 29870.0, pd.Timestamp("2026-08-13 09:35", tz=TZ))
    assert ex._plan["attempts_used"] == 2


def test_no_deeper_gap_penetrated_means_no_blacklist(tmp_path):
    """§8: an unconditional blacklist would break 07-23's validated same-gap re-entry."""
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29880.0, 29900.0))
    ex._state["now_price"] = 29890.0
    _stop(ex, "g29880.0", 29880.0, pd.Timestamp("2026-08-13 09:33", tz=TZ))
    assert ex._plan["blacklist"] == []


def test_a_deeper_gap_penetrated_blacklists_the_failed_gap(tmp_path):
    ex = _ex(tmp_path)
    for g in (_gap(29880.0, 29900.0), _gap(29850.0, 29860.0)):
        ex._store.upsert(g)
    ex._state["now_price"] = 29855.0          # inside the deeper gap
    _stop(ex, "g29880.0", 29880.0, pd.Timestamp("2026-08-13 09:33", tz=TZ))
    assert "g29880.0" in ex._plan["blacklist"]


def test_a_blacklisted_gap_is_never_bound_again(tmp_path):
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29880.0, 29900.0))
    ex._plan["blacklist"] = ["g29880.0"]
    ids = {g.id for g in ex._eligible_gaps(pd.Timestamp("2026-08-13 09:40", tz=TZ))}
    assert "g29880.0" not in ids


def test_the_blacklist_survives_price_returning_to_the_failed_gap(tmp_path):
    """'ignored even if price later returns to it; a stop-run through its edge is a
    falsified edge'."""
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29880.0, 29900.0))
    ex._plan["blacklist"] = ["g29880.0"]
    ex._state["now_price"] = 29890.0
    ids = {g.id for g in ex._eligible_gaps(pd.Timestamp("2026-08-13 09:50", tz=TZ))}
    assert "g29880.0" not in ids


def test_the_stop_out_is_recorded_with_its_artifact(tmp_path):
    """Driven through `_drive_orders`, not by calling `_on_stop_out` directly: the
    recording happens in the sim-event loop, so a direct call records NOTHING and an
    `assert not seen or ...` would pass against an empty list forever."""
    ex = _ex(tmp_path)
    gap = _gap(29880.0, 29900.0)
    ex._store.upsert(gap)
    seen = []
    ex._rec.order_event = lambda **kw: seen.append(kw)

    ex._sim.place(RestingOrder(direction="UP", trigger=29905.0, stop=29880.0,
                               artifact_id=gap.id,
                               placed_at=pd.Timestamp("2026-08-13 09:32", tz=TZ)))
    frame = pd.DataFrame(
        {"Open": [29900.0], "High": [29910.0], "Low": [29875.0], "Close": [29878.0]},
        index=[pd.Timestamp("2026-08-13 09:33", tz=TZ)])
    ex._drive_orders(pd.Timestamp("2026-08-13 09:33", tz=TZ), frame)

    assert ex._state["order_error"] is None, ex._state["order_error"]
    assert [r["kind"] for r in seen] == ["fill", "stop_out"]
    assert all(r.get("artifact_id") == gap.id for r in seen)
    assert all(r.get("artifact_label") == gap.label for r in seen), \
        "records.py: the id and the label always travel as a pair"
    assert ex._plan["attempts_used"] == 1
