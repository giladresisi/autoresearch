"""Plan 16: the target is picked at the FILL, and the DOL / `dol_floor` are inert.

The `select_target` cases run against the real parquets (the menu is production's own
`build_menus`); the Executor cases drive `OrderSim` directly so they pin the wiring
without needing a whole session's bars.
"""
import json

import pandas as pd
import pytest

from agent.trader.executor import Executor, DOL_FLOOR_PTS
from agent.trader.order_sim import OrderSim, RestingOrder
from agent.trader.records import DECISIONS_FILE, DecisionRecorder
from agent.trader.target import select_target

TZ = "America/New_York"
DATE = "2026-09-03"
FILL_TS = pd.Timestamp(f"{DATE} 09:32:11", tz=TZ)


def _recs(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _of_kind(tmp_path, kind):
    return [r for r in _recs(tmp_path) if r["kind"] == kind]


@pytest.fixture(scope="module")
def session_bars():
    from backtest_smt import _main_dir_for_date
    mid = _main_dir_for_date(DATE)
    out = {}
    for tk in ("MNQ", "MES"):
        df = pd.read_parquet(mid / f"{tk}_1m.parquet")
        out[tk] = df.loc[FILL_TS - pd.Timedelta(days=20):FILL_TS]
    return out


# --------------------------------------------------------------------------- #
# select_target                                                                 #
# --------------------------------------------------------------------------- #

def test_select_target_returns_the_d1_menu_row(session_bars):
    """Case 1: a non-empty menu yields D1. 2026-09-03 09:32:11 UP is projection_up."""
    pick = select_target(session_bars, FILL_TS, "UP")
    assert pick is not None
    assert pick["id"] == "D1"
    assert pick["level"] == "projection_up"
    assert pick["price"] == pytest.approx(29367.0)


def test_select_target_is_none_on_degraded_bars_and_does_not_raise():
    """Case 2: it is called from inside the bar loop; it may never raise."""
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert select_target({"MNQ": empty, "MES": empty}, FILL_TS, "UP") is None
    assert select_target(None, FILL_TS, "UP") is None
    assert select_target({}, FILL_TS, "DOWN") is None


def test_select_target_rejects_a_direction_that_is_not_a_menu_key(session_bars):
    """Case 3 (partner): NEUTRAL / junk is not a menu, and must not be guessed at."""
    assert select_target(session_bars, FILL_TS, "NEUTRAL") is None
    assert select_target(session_bars, FILL_TS, "") is None


# --------------------------------------------------------------------------- #
# the wiring: a fill sets the target                                            #
# --------------------------------------------------------------------------- #

def _executor(tmp_path, monkeypatch, pick, direction="UP"):
    plan = {"plan_id": "p16", "thesis_id": "t", "direction": direction,
            "dol": {"level": "london(cur)_high", "price": 29293.0},
            "valid_while": [], "armed_classes": ["fvg_return_continuation"],
            "attempts_used": 0, "blacklist": [], "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=pd.Timestamp(f"{DATE} 09:21", tz=TZ))
    monkeypatch.setattr("agent.trader.executor.select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    return ex


def _bar(px, lo=None, hi=None):
    return pd.Series({"Open": px, "High": hi if hi is not None else px,
                      "Low": lo if lo is not None else px, "Close": px, "Volume": 1.0})


def test_resting_fill_sets_the_target_and_records_it(tmp_path, monkeypatch):
    """Case 4."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    ex._sim.place(RestingOrder("UP", 29241.0, 29216.0, "gapA",
                               pd.Timestamp(f"{DATE} 09:31", tz=TZ)))
    frame = pd.DataFrame([_bar(29245.0, lo=29238.0, hi=29247.0)],
                         index=[pd.Timestamp(f"{DATE} 09:32:11", tz=TZ)])
    ex._drive_orders(pd.Timestamp(f"{DATE} 09:32:11", tz=TZ), frame)

    assert ex._sim.target == pytest.approx(29367.0)
    sel = _of_kind(tmp_path, "target_selected")
    assert len(sel) == 1
    assert sel[0]["target"] == pytest.approx(29367.0)
    assert sel[0]["level"] == "projection_up"


def test_market_fill_sets_the_target_too(tmp_path, monkeypatch):
    """Case 5: the crossed-trigger path never goes through `_drive_orders`."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="gapB")
    ex._set_target_on_fill(now)
    assert ex._sim.target == pytest.approx(29367.0)
    assert len(_of_kind(tmp_path, "target_selected")) == 1


def test_an_empty_menu_leaves_no_target_and_is_still_recorded(tmp_path, monkeypatch):
    """Case 6: `None` is a real outcome — no target, position rides to stop or mark."""
    ex = _executor(tmp_path, monkeypatch, None)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="gapC")
    ex._set_target_on_fill(now)

    assert ex._sim.target is None
    sel = _of_kind(tmp_path, "target_selected")
    assert len(sel) == 1 and sel[0]["target"] is None and sel[0]["pick"] is None

    # a far bar must NOT book a take-profit: there is no target to hit
    evs = ex._sim.on_bar(pd.Timestamp(f"{DATE} 09:40", tz=TZ), _bar(29500.0, hi=29500.0))
    assert [e["kind"] for e in evs] == []
    assert ex._sim.position is not None


def test_take_profit_fires_at_the_t2_target_not_the_0920_dol(tmp_path, monkeypatch):
    """Case 9: the plan's DOL is 29293; the T2 pick is 29367. 29300 must NOT exit."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="gapD")
    ex._set_target_on_fill(now)

    through_old_dol = ex._sim.on_bar(pd.Timestamp(f"{DATE} 09:39", tz=TZ),
                                     _bar(29300.0, hi=29300.0))
    assert [e["kind"] for e in through_old_dol] == []      # 29293 is inert

    at_target = ex._sim.on_bar(pd.Timestamp(f"{DATE} 09:49", tz=TZ),
                               _bar(29370.0, hi=29370.0))
    assert [e["kind"] for e in at_target] == ["take_profit"]
    assert at_target[0]["price"] == pytest.approx(29367.0)


# --------------------------------------------------------------------------- #
# the DOL is inert                                                              #
# --------------------------------------------------------------------------- #

def test_dol_floor_records_but_no_longer_vetoes(tmp_path, monkeypatch):
    """Case 7: `would_have_vetoed` once, no `veto`, and the caller falls through."""
    ex = _executor(tmp_path, monkeypatch, None)
    plan_dol = 29293.0
    trigger = plan_dol - (DOL_FLOOR_PTS / 2.0)      # inside the floor by construction

    class _Gap:
        id, label = "gapE", "MNQ 5min bull FVG"

    now = pd.Timestamp(f"{DATE} 09:31", tz=TZ)
    ex._would_have_vetoed_once(now, "fvg_return_continuation", _Gap(), "dol_floor",
                               {"remaining": plan_dol - trigger, "floor": DOL_FLOOR_PTS,
                                "trigger": trigger, "dol": plan_dol})
    ex._would_have_vetoed_once(now, "fvg_return_continuation", _Gap(), "dol_floor",
                               {"remaining": plan_dol - trigger, "floor": DOL_FLOOR_PTS,
                                "trigger": trigger, "dol": plan_dol})

    assert len(_of_kind(tmp_path, "would_have_vetoed")) == 1     # deduped
    assert _of_kind(tmp_path, "veto") == []                      # never a real veto
    assert _of_kind(tmp_path, "would_have_vetoed")[0]["reason"] == "dol_floor"


def test_dol_reached_records_once_and_does_not_kill_the_plan(tmp_path, monkeypatch):
    """Case 8: plan stays alive through the 09:20 DOL, recorded exactly once."""
    ex = _executor(tmp_path, monkeypatch, None)
    now = pd.Timestamp(f"{DATE} 09:39", tz=TZ)
    # UP plan, DOL 29293 — a frame whose high is through it, on three separate bars.
    through = pd.DataFrame(
        [_bar(29300.0, hi=29300.0)],
        index=[pd.Timestamp(f"{DATE} 09:39", tz=TZ)])
    for _ in range(3):
        reason, _detail = ex._death(through, now, True)
        assert reason != "dol_reached"

    killed = _of_kind(tmp_path, "would_have_killed")
    assert len(killed) == 1
    assert killed[0]["reason"] == "dol_reached"
    assert killed[0]["detail"]["dol"] == pytest.approx(29293.0)


def test_attempts_exhausted_still_kills(tmp_path, monkeypatch):
    """Guard: removing the DOL death must not disarm the attempt budget as well."""
    ex = _executor(tmp_path, monkeypatch, None)
    ex._plan["attempts_used"] = 3
    reason, _ = ex._death(pd.DataFrame(), pd.Timestamp(f"{DATE} 09:45", tz=TZ), True)
    assert reason == "attempts_exhausted"


def test_a_closed_position_does_not_leave_its_target_behind():
    """REGRESSION, 08-18. `on_bar` fills a resting order and tests the take-profit in the
    SAME call, so a target left over from the previous position is live in exactly that
    window. On 08-18 it fired: a DOWN fill at 29564.5 booked `take_profit` at 29625.0 —
    the prior position's target, 60.5 pts the WRONG WAY, recorded as a win."""
    sim = OrderSim(dol=None)
    sim.set_target(29625.0)
    sim.fill_market(pd.Timestamp(f"{DATE} 09:40", tz=TZ), direction="DOWN",
                    price=29763.25, stop=29788.25, artifact_id="a")
    evs = sim.on_bar(pd.Timestamp(f"{DATE} 09:55", tz=TZ),
                     _bar(29620.0, lo=29620.0, hi=29630.0))
    assert [e["kind"] for e in evs] == ["take_profit"]
    assert sim.target is None, "the target must not outlive its position"

    # A NEW resting order filling on a bar that also spans the OLD target must not exit.
    sim.place(RestingOrder("DOWN", 29564.5, 29589.5, "b",
                           pd.Timestamp(f"{DATE} 11:04", tz=TZ)))
    # High stays under the 29589.5 stop, so the ONLY thing that could exit here is the
    # stale 29625.0: for a short the take-profit test is `Low <= target`, and 29625 sits
    # ABOVE the entry, so it would fire instantly and book 60.5 pts the wrong way.
    evs = sim.on_bar(pd.Timestamp(f"{DATE} 11:04:40", tz=TZ),
                     _bar(29570.0, lo=29560.0, hi=29575.0))
    assert [e["kind"] for e in evs] == ["fill"], "a stale target must not book an exit"
    assert sim.position is not None


# --------------------------------------------------------------------------- #
# the T2 target is what now kills the plan                                      #
# --------------------------------------------------------------------------- #

def _frame(rows):
    idx = [pd.Timestamp(f"{DATE} {t}", tz=TZ) for t, _ in rows]
    return pd.DataFrame([{"Open": p, "High": p + 2, "Low": p - 2, "Close": p,
                          "Volume": 1.0} for _, p in rows], index=idx)


def test_reaching_the_t2_target_kills_the_plan(tmp_path, monkeypatch):
    """Replaces `dol_reached` in the role it used to hold: one plan, one draw."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="g")
    ex._set_target_on_fill(now)

    below = _frame([("09:34", 29300.0), ("09:35", 29320.0)])
    assert ex._death(below, pd.Timestamp(f"{DATE} 09:35", tz=TZ), True)[0] is None

    through = _frame([("09:34", 29300.0), ("09:49", 29370.0)])
    reason, detail = ex._death(through, pd.Timestamp(f"{DATE} 09:49", tz=TZ), True)
    assert reason == "target_reached"
    assert detail["target"] == pytest.approx(29367.0)
    assert detail["level"] == "projection_up"


def test_the_target_kills_even_after_the_attempt_that_chose_it_stopped_out(tmp_path,
                                                                           monkeypatch):
    """The objective belongs to the PLAN, not to one fill. `OrderSim` drops its copy on
    close (the stale-target fix); the Executor's must outlive it."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="g")
    ex._set_target_on_fill(now)
    ex._sim.on_bar(pd.Timestamp(f"{DATE} 09:35", tz=TZ), _bar(29224.0, lo=29220.0))
    assert ex._sim.position is None and ex._sim.target is None      # stopped out

    through = _frame([("09:40", 29300.0), ("09:49", 29370.0)])
    assert ex._death(through, pd.Timestamp(f"{DATE} 09:49", tz=TZ), True)[0] ==         "target_reached"


def test_a_target_is_not_reached_by_bars_that_predate_the_pick(tmp_path, monkeypatch):
    """Same discipline as `_since_arm`: price traded through the level freely before it
    was ever chosen, and judging the plan by that kills it on the bar it picks."""
    pick = {"id": "D1", "level": "projection_up", "price": 29367.0}
    ex = _executor(tmp_path, monkeypatch, pick)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="g")
    ex._set_target_on_fill(now)
    earlier = _frame([("09:25", 29400.0), ("09:34", 29300.0)])
    assert ex._death(earlier, pd.Timestamp(f"{DATE} 09:34", tz=TZ), True)[0] is None


def test_no_target_means_no_target_death(tmp_path, monkeypatch):
    """An empty menu leaves the plan with no objective; it must not die on one."""
    ex = _executor(tmp_path, monkeypatch, None)
    ex._bars = {"MNQ": None, "MES": None}
    now = pd.Timestamp(f"{DATE} 09:33", tz=TZ)
    ex._sim.fill_market(now, direction="UP", price=29250.0, stop=29225.0,
                        artifact_id="g")
    ex._set_target_on_fill(now)
    far = _frame([("09:40", 29900.0)])
    assert ex._death(far, pd.Timestamp(f"{DATE} 09:40", tz=TZ), True)[0] is None
