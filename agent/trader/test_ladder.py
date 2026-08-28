import pandas as pd

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import NO_MOVE_ZONE_PTS, Executor

TZ = "America/New_York"


def _gap(lo, hi, direction="bull"):
    return Fact(id=f"g{lo}", cls=FactClass.FVG, ticker="MNQ", label=f"g{lo}", name=None,
                reference_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ), price=None,
                price_low=lo, price_high=hi, timeframe="5min", resolution="5min",
                state=FactState.LIVE, state_ts=pd.Timestamp("2026-08-13 09:00", tz=TZ),
                provenance={}, extra={"direction": direction,
                                      "max_anti_excursion": 0.0})


def _ex(tmp_path, direction="UP"):
    return Executor(str(tmp_path), {"plan_id": "t", "direction": direction,
                                    "dol": {"price": 30001.5},
                                    "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-13 09:20", tz=TZ))


def test_the_no_move_zone_is_the_spec_value():
    assert NO_MOVE_ZONE_PTS == 15.0


def test_a_deeper_gap_entered_while_an_order_rests_becomes_the_ladder_target(tmp_path):
    """UP plan: the adverse path is DOWN, so 'deeper' means lower."""
    ex = _ex(tmp_path)
    for g in (_gap(29881.5, 29905.25), _gap(29860.0, 29868.0)):
        ex._store.upsert(g)
    t = ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ), price=29864.0)
    assert t is not None and t.id == "g29860.0"


def test_a_gap_price_has_not_entered_is_not_a_ladder_target(tmp_path):
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29860.0, 29868.0))
    assert ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ),
                             price=29890.0) is None


def test_the_ladder_target_is_min_height_exempt(tmp_path):
    """§2: ladder / deepest-penetration targets are exempt from the minimum."""
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29877.0, 29878.75))       # 1.75 pts
    t = ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ), price=29878.0)
    assert t is not None


def test_the_ladder_target_still_obeys_the_maximum_height(tmp_path):
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29800.0, 29860.0))        # 60 pts
    assert ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ),
                             price=29830.0) is None


def test_the_ladder_never_moves_back_toward_the_original_gap(tmp_path):
    """It moves DEEPER along the adverse path only."""
    ex = _ex(tmp_path)
    shallow, deep = _gap(29881.5, 29905.25), _gap(29860.0, 29868.0)
    for g in (shallow, deep):
        ex._store.upsert(g)
    ex._state["bound_id"] = deep.id
    assert ex._ladder_target(pd.Timestamp("2026-08-13 09:33", tz=TZ),
                             price=29890.0) is None


def test_the_deepest_entered_gap_wins_when_several_are_penetrated(tmp_path):
    ex = _ex(tmp_path)
    for g in (_gap(29880.0, 29890.0), _gap(29860.0, 29868.0), _gap(29840.0, 29848.0)):
        ex._store.upsert(g)
    t = ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ), price=29845.0)
    assert t.id == "g29840.0"


def test_no_rebind_while_price_sits_inside_the_no_move_zone(tmp_path):
    """§8: re-binding within ~15 pts of the resting trigger risks being flat during the
    exact displacement being waited for."""
    ex = _ex(tmp_path)
    ex._store.upsert(_gap(29881.5, 29905.25))
    ex._store.upsert(_gap(29860.0, 29868.0))
    ex._state["trigger"] = 29908.25
    assert ex._in_no_move_zone(price=29900.0) is True
    assert ex._in_no_move_zone(price=29860.0) is False


def test_a_short_plan_ladders_upward(tmp_path):
    """DOWN plan: the adverse path is UP, so 'deeper' means higher."""
    ex = _ex(tmp_path, direction="DOWN")
    for g in (_gap(29880.0, 29890.0, "bear"), _gap(29920.0, 29930.0, "bear")):
        ex._store.upsert(g)
    t = ex._ladder_target(pd.Timestamp("2026-08-13 09:32", tz=TZ), price=29925.0)
    assert t.id == "g29920.0"


# ── binding may only travel along the adverse path (08-13 regressions) ──────────
# added during implementation (not in the plan)

def test_rebinding_never_moves_backward_toward_price(tmp_path):
    """Observed on 08-13: bound trigger 29842 at 09:32, then 29908.25 at 09:33 — a long
    binding moving back UP, toward price. The ladder never does that, and re-selection
    now shares its direction-of-travel rule."""
    ex = _ex(tmp_path, direction="UP")
    deep = _gap(29808.0, 29839.0)          # trigger 29842
    shallow = _gap(29881.5, 29905.25)      # trigger 29908.25
    ex._state["bound_id"] = deep.id
    assert ex._is_deeper_or_same(deep, deep)
    assert not ex._is_deeper_or_same(shallow, deep), (
        "a shallower gap must never be a legal re-selection target")


def test_a_short_travels_upward_not_downward(tmp_path):
    """Mirror: for a short the adverse path is UP, so deeper means a HIGHER gap."""
    ex = _ex(tmp_path, direction="DOWN")
    deep = _gap(29900.0, 29930.0, "bear")
    shallow = _gap(29800.0, 29830.0, "bear")
    assert ex._is_deeper_or_same(deep, shallow)
    assert not ex._is_deeper_or_same(shallow, deep)


def test_a_crossed_trigger_is_STILL_selectable(tmp_path):
    """NOT excluded. §2: a crossed trigger at initial placement executes as a market
    order — "the resting stop-entry is the normal case, market the degenerate case of
    the same mechanism". Filtering them out silenced 2026-08-25 entirely."""
    ex = _ex(tmp_path, direction="UP")
    g = _gap(29808.0, 29839.0)                       # long trigger 29842
    assert ex._trigger_crossed(g, 29870.25), "price above a long's trigger IS crossed"
    assert not ex._trigger_crossed(g, 29800.0)


def test_crossed_is_mirrored_for_a_short(tmp_path):
    ex = _ex(tmp_path, direction="DOWN")
    g = _gap(29808.0, 29839.0, "bear")               # sell stop below -> trigger 29805
    assert ex._trigger_crossed(g, 29800.0)
    assert not ex._trigger_crossed(g, 29870.0)
