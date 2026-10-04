"""Plan 38 Wave 1: the Executor's order-port seam and §8's temporary spine gates.

Everything here runs the REAL `Executor.on_bar` over synthetic 1s-cadence frames; only
the market mechanisms are stubbed, so a fire can be ordered up at an exact second. The
rules under test live in the Executor, in BAR time:

  * no NEW entry at or after 10:30:00; an open position is still managed after it;
  * no NEW entry after a profitable close;
  * at the first bar >= 13:00:00 any open position is MARKED and the plan dies;
  * a port that reports an external change kills the plan.

`test_the_five_before_dates_replay_byte_identical` is the expensive one (`-m slow`): it
replays the five sessions captured BEFORE this change and compares the streams byte for
byte. That is what makes "the default port changes nothing" a measurement.
"""
import json
import os
from types import SimpleNamespace

import pandas as pd
import pytest

from agent.trader import executor as executor_mod
from agent.trader.executor import (ENTRY_CUTOFF_ET, NO_ENTRY_AFTER_POSITIVE,
                                   WINDOW_END_ET, Executor)
from agent.trader.order_port import MirroringOrderPort
from agent.trader.order_sim import OrderSim
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-03"
ARM = pd.Timestamp(f"{DATE} 09:21", tz=TZ)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                   "plan38_before")
BEFORE_DATES = ("2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _recs(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _kinds(tmp_path):
    return [r["kind"] for r in _recs(tmp_path)]


def _frames(now, px, *, hi=None, lo=None):
    """30 flat 1m bars, the last one the in-progress minute — replay's frame shape."""
    minute = now.floor("1min")
    idx = pd.date_range(minute - pd.Timedelta(minutes=30), minute, freq="1min")
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                       "Volume": 1.0}, index=idx)
    df.iloc[-1] = [px, hi if hi is not None else px, lo if lo is not None else px, px, 1.0]
    return {"MNQ": df, "MES": df.copy()}


class StubMarket:
    """`MarketMechanisms` with the machines removed: `fire` is handed to the Executor the
    next time it ASKS, and only then — a blocked bar never reaches `pick`."""

    def __init__(self):
        self.fire = None
        self.asked = 0
        self.arbiter = SimpleNamespace(spend=lambda mechanism: None)

    def pick(self, fires):
        self.asked += 1
        fire, self.fire = self.fire, None
        return fire

    def seed_sec7(self, *a, **k): pass
    def sync_episodes(self, *a, **k): pass
    def reset_cycles(self): pass
    def sec6_on_tick(self, *a, **k): return None
    def sec6_on_bar_close(self, *a, **k): return None
    def sec7_on_bar_close(self, *a, **k): return None
    def tmso_on_bar_close(self, *a, **k): return None
    def fvg1h_on_bar_close(self, *a, **k): return None
    # O1 (§7c) inert here: `test_executor_stop_bar_retry.py` covers the retry.
    def stop_bar_retry_enabled(self, mechanism): return False
    def stop_bar_retry_due(self, now): return False
    def drop_stop_bar_retry(self): return None


def _fire(now, px, *, direction="UP", risk=15.0, mechanism="extreme_reject_close"):
    stop = px - risk if direction == "UP" else px + risk
    return {"mechanism": mechanism, "direction": direction, "price": px, "stop": stop,
            "time": now}


def make_executor(tmp_path, monkeypatch, *, order_port=None, pick=None):
    plan = {"plan_id": "p38", "thesis_id": "t", "direction": "UP",
            "dol": {"level": "far", "price": 99999.0}, "valid_while": [],
            "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=ARM, order_port=order_port)
    ex._market = StubMarket()
    monkeypatch.setattr(executor_mod, "select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    return ex


def _step(ex, hms, px, *, fire=False, hi=None, lo=None, **fire_kw):
    now = _ts(hms)
    if fire:
        ex._market.fire = _fire(now, px, **fire_kw)
    ex.on_bar(now, _frames(now, px, hi=hi, lo=lo))
    return now


# --------------------------------------------------------------------------- #
# the constants are the rule                                                    #
# --------------------------------------------------------------------------- #

def test_the_rules_sit_behind_named_constants():
    assert ENTRY_CUTOFF_ET == (10, 30)
    assert NO_ENTRY_AFTER_POSITIVE is True
    assert WINDOW_END_ET == (13, 0)


def test_the_window_end_has_one_source():
    from agent.trader import replay
    assert replay.WINDOW_END_ET is executor_mod.WINDOW_END_ET


# --------------------------------------------------------------------------- #
# case 9: an injected port decides nothing                                      #
# --------------------------------------------------------------------------- #

def _scripted_session(ex):
    _step(ex, "09:31:00", 29250.0)
    _step(ex, "09:40:00", 29250.0, fire=True)                   # entry 1
    _step(ex, "09:40:30", 29240.0, lo=29230.0)                  # stop-out
    _step(ex, "09:45:00", 29260.0, fire=True)                   # entry 2
    _step(ex, "09:50:00", 29290.0)
    _step(ex, "09:55:00", 29330.0, hi=29345.0)                  # take-profit
    _step(ex, "09:56:00", 29340.0, fire=True)                   # must not enter


def test_an_injected_port_with_a_recording_sink_decides_identically(tmp_path,
                                                                     monkeypatch):
    """Case 9."""
    pick = {"id": "D1", "level": "projection_up", "price": 29340.0}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _scripted_session(make_executor(tmp_path / "a", monkeypatch, pick=pick))

    seen = []
    port = MirroringOrderPort(OrderSim(dol=None), lambda ev: seen.append(ev) or None)
    ex = make_executor(tmp_path / "b", monkeypatch, order_port=port, pick=pick)
    port._context = ex.order_context
    _scripted_session(ex)

    default = (tmp_path / "a" / DECISIONS_FILE).read_bytes()
    assert default == (tmp_path / "b" / DECISIONS_FILE).read_bytes()
    assert b'"fill"' in default and b'"take_profit"' in default
    assert [e["kind"] for e in seen] == ["fill", "stop_out", "fill", "take_profit"]
    assert [e["seq"] for e in seen] == [1, 2, 3, 4]
    assert {e["plan_id"] for e in seen} == {"p38"}
    assert all(e["mechanism"] == "extreme_reject_close" for e in seen)


# --------------------------------------------------------------------------- #
# cases 10, 11: the 10:30 cutoff                                                #
# --------------------------------------------------------------------------- #

def test_no_entry_at_or_after_1030_but_an_open_position_is_still_managed(tmp_path,
                                                                          monkeypatch):
    """Case 10."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:20:00", 29250.0, fire=True)
    assert ex.position() is not None
    _step(ex, "10:30:00", 29255.0)
    assert ex.bind_state()["entry_block"] == "entry_cutoff"
    _step(ex, "10:45:00", 29240.0, lo=29230.0)                 # the stop is still live
    assert ex.position() is None
    assert _kinds(tmp_path) == ["fill", "target_selected", "initial_target_selected",
                                "stop_out"]

    asked = ex._market.asked
    _step(ex, "10:46:00", 29250.0, fire=True)
    assert ex.position() is None and ex._market.asked == asked
    assert _kinds(tmp_path).count("fill") == 1
    assert ex.bind_state()["plan_alive"] is True, "the cutoff blocks entries, not the plan"


def test_exactly_1030_00_is_blocked(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:30:00", 29250.0, fire=True)
    assert ex.position() is None and "fill" not in _kinds(tmp_path)


def test_a_1029_59_entry_is_allowed(tmp_path, monkeypatch):
    """Case 11 — the boundary."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:29:59", 29250.0, fire=True)
    assert ex.position() is not None
    assert _kinds(tmp_path)[:1] == ["fill"]


def test_a_resting_order_still_unfilled_at_the_cutoff_is_withdrawn(tmp_path,
                                                                    monkeypatch):
    from agent.trader.order_sim import RestingOrder
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:29:00", 29250.0)
    ex._sim.place(RestingOrder("UP", 29260.0, 29240.0, "gapA", _ts("10:29:00")))
    _step(ex, "10:30:00", 29262.0, hi=29265.0)                 # would have filled it
    assert ex._sim.resting is None and ex.position() is None
    assert "fill" not in _kinds(tmp_path)


def test_the_cutoff_is_measured_on_the_arm_date_not_the_bar_date(tmp_path, monkeypatch):
    """The live bar loop runs the whole CME session. 09:40 the morning AFTER a 20:00
    arm is not 'after 10:30' merely because 20:00 was."""
    ex = make_executor(tmp_path, monkeypatch)
    assert ex._entry_block(_ts("09:40:00")) is None
    assert ex._entry_block(_ts("10:30:00")) == "entry_cutoff"
    assert ex._at_window_end(_ts("12:59:59")) is False
    assert ex._at_window_end(_ts("13:00:00")) is True


# --------------------------------------------------------------------------- #
# case 12: no entry after a positive close                                      #
# --------------------------------------------------------------------------- #

def test_no_entry_after_a_positive_close_with_target_death_disabled(tmp_path,
                                                                     monkeypatch):
    """Case 12 (F16). The take-profit also kills the plan via `target_reached`, which
    would hide this rule. The Executor's own copy of the target is cleared after the
    fill, so `target_reached` cannot fire and the positive close is what ends the plan
    (2026-09-30: a plan death, `positive_close`, on the exit's own bar)."""
    ex = make_executor(tmp_path, monkeypatch,
                       pick={"id": "D1", "level": "x", "price": 29300.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex._target_price = None                       # `target_reached` can no longer fire
    _step(ex, "09:50:00", 29295.0, hi=29301.0)
    _ORDER_KINDS = ("fill", "fill_voided", "stop_out", "take_profit", "mark",
                   "initial_opp_close")
    assert [k for k in _kinds(tmp_path) if k in _ORDER_KINDS][-1] == "take_profit"
    dead = [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"]
    assert [d["reason"] for d in dead] == ["positive_close"]
    assert dead[0]["time"] == _ts("09:50:00").isoformat()
    assert dead[0]["detail"] == {"exit": "take_profit", "entry": 29250.0,
                                 "price": 29300.0}
    assert ex.bind_state()["plan_alive"] is False

    asked = ex._market.asked
    _step(ex, "09:52:00", 29290.0, fire=True)
    assert ex.position() is None and ex._market.asked == asked
    assert ex.bind_state()["entry_block"] == "after_positive_trade"
    assert _kinds(tmp_path).count("fill") == 1
    assert _kinds(tmp_path).count("plan_dead") == 1


def test_a_take_profit_still_dies_as_target_reached(tmp_path, monkeypatch):
    """`positive_close` is evaluated LAST: a target touch keeps the reason it always
    recorded, so no stream with a take-profit in it moves."""
    ex = make_executor(tmp_path, monkeypatch,
                       pick={"id": "D1", "level": "x", "price": 29300.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    _step(ex, "09:50:00", 29295.0, hi=29301.0)
    dead = [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"]
    assert [d["reason"] for d in dead] == ["target_reached"]


def test_a_profitable_operator_stop_exit_kills_the_plan(tmp_path, monkeypatch):
    """The 2026-09-30 session: the exit was the operator's trailed stop, the target was
    never reached, and the plan stayed alive with all three attempts in hand."""
    ex = make_executor(tmp_path, monkeypatch,
                       pick={"id": "D1", "level": "x", "price": 29400.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex.set_stop_override(_ts("09:50:00"), 29280.0)
    _step(ex, "09:51:10", 29281.0, lo=29279.5)
    dead = [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"]
    assert [d["reason"] for d in dead] == ["positive_close"]
    assert dead[0]["detail"]["exit"] == "stop_out_initial"
    assert ex.bind_state()["plan_alive"] is False and ex._plan["attempts_used"] == 0


def test_a_breakeven_stop_exit_is_not_a_positive_close(tmp_path, monkeypatch):
    """Stop moved to the entry and hit: nothing was made, so the plan lives (and no
    attempt is spent — `stop_out_initial` is not a failed attempt either)."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex.set_stop_override(_ts("09:50:00"), 29250.0)
    _step(ex, "09:51:10", 29251.0, lo=29249.0)
    assert ex.position() is None and "stop_out_initial" in _kinds(tmp_path)
    assert ex.bind_state()["plan_alive"] is True and ex._plan["attempts_used"] == 0


def test_a_losing_close_does_not_block_the_next_entry(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    _step(ex, "09:40:30", 29240.0, lo=29230.0)
    _step(ex, "09:45:00", 29260.0, fire=True)
    assert _kinds(tmp_path).count("fill") == 2


def test_the_positive_trade_rule_comes_out_with_its_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_mod, "NO_ENTRY_AFTER_POSITIVE", False)
    ex = make_executor(tmp_path, monkeypatch,
                       pick={"id": "D1", "level": "x", "price": 29300.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex._target_price = None
    _step(ex, "09:50:00", 29295.0, hi=29301.0)
    _step(ex, "09:52:00", 29290.0, fire=True)
    assert _kinds(tmp_path).count("fill") == 2


# --------------------------------------------------------------------------- #
# cases 13, 14: the window end                                                  #
# --------------------------------------------------------------------------- #

def test_the_window_end_marks_an_open_position_then_kills_the_plan(tmp_path,
                                                                    monkeypatch):
    """Case 13."""
    seen = []
    port = MirroringOrderPort(OrderSim(dol=None), lambda ev: seen.append(ev) or None)
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    port._context = ex.order_context
    _step(ex, "10:20:00", 29250.0, fire=True)
    _step(ex, "12:59:59", 29270.0)
    assert ex.position() is not None and ex.bind_state()["plan_alive"] is True

    _step(ex, "13:00:00", 29275.0)
    recs = _recs(tmp_path)
    assert [r["kind"] for r in recs][-2:] == ["mark", "plan_dead"]
    assert recs[-2]["price"] == 29275.0 and recs[-2]["entry"] == 29250.0
    assert recs[-1]["reason"] == "window_end"
    assert ex.position() is None
    assert [e["kind"] for e in seen] == ["fill", "mark"]

    _step(ex, "13:00:01", 29280.0)                              # fires exactly once
    assert _kinds(tmp_path).count("mark") == 1
    assert _kinds(tmp_path).count("plan_dead") == 1


def test_the_window_end_with_nothing_open_still_kills_the_plan(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "13:00:00", 29250.0)
    assert _kinds(tmp_path) == ["plan_dead"]
    assert _recs(tmp_path)[0]["reason"] == "window_end"


def test_a_stop_on_the_window_end_bar_books_the_stop_not_the_mark(tmp_path, monkeypatch):
    """Adverse resolution, the simulation's standing rule."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:20:00", 29250.0, fire=True)
    _step(ex, "13:00:00", 29240.0, lo=29230.0)
    assert "stop_out" in _kinds(tmp_path) and "mark" not in _kinds(tmp_path)


def test_a_window_ending_at_125959_never_reaches_the_rule(tmp_path, monkeypatch):
    """Case 14: replay's last bar. The run's OWN mark still books the runner."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "10:20:00", 29250.0, fire=True)
    _step(ex, "12:59:59", 29270.0)
    assert _kinds(tmp_path) == ["fill", "target_selected", "initial_target_selected"]
    assert ex.bind_state()["plan_alive"] is True
    ev = ex.mark_open_position()                   # what `run_replay` does after the loop
    assert ev["kind"] == "mark" and _kinds(tmp_path)[-1] == "mark"
    assert "plan_dead" not in _kinds(tmp_path)


# --------------------------------------------------------------------------- #
# case 15: an external change kills the plan                                    #
# --------------------------------------------------------------------------- #

def test_a_port_reporting_an_external_change_kills_the_plan(tmp_path, monkeypatch):
    """Case 15."""
    acks = [{"ok": False, "reason": "dispatch_failed"}]
    port = MirroringOrderPort(OrderSim(dol=None),
                              lambda ev: acks.pop(0) if acks else None)
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    _step(ex, "09:40:00", 29250.0, fire=True)
    assert ex.position() is None
    assert _kinds(tmp_path) == ["fill_voided"], "no target is picked for a voided fill"

    _step(ex, "09:40:01", 29250.0, fire=True)
    recs = _recs(tmp_path)
    assert recs[-1]["kind"] == "plan_dead"
    assert recs[-1]["reason"] == "external_position_change"
    assert recs[-1]["detail"]["reason"] == "dispatch_failed"
    assert ex.position() is None and _kinds(tmp_path).count("fill") == 0

    _step(ex, "09:41:00", 29250.0, fire=True)                   # no further binding
    assert _kinds(tmp_path) == ["fill_voided", "plan_dead"]


def test_a_voided_entry_keeps_the_plan_and_spends_no_attempt(tmp_path, monkeypatch):
    """D23, the Executor's half: `voided` is not an external change."""
    acks = [{"ok": False, "reason": "entry_refused", "voided": True}]
    port = MirroringOrderPort(OrderSim(dol=None),
                              lambda ev: acks.pop(0) if acks else None)
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    _step(ex, "09:40:00", 29250.0, fire=True)
    _step(ex, "09:41:00", 29250.0)
    assert ex.bind_state()["plan_alive"] is True
    assert ex._plan["attempts_used"] == 0 and ex._target_price is None
    _step(ex, "09:45:00", 29260.0, fire=True)                   # and it can enter again
    assert _kinds(tmp_path) == ["fill_voided", "fill", "target_selected",
                                "initial_target_selected"]


def test_kill_plan_and_void_position_from_outside(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex.kill_plan(_ts("09:41:00"), "external_position_change", {"reason": "manual"})
    ex.kill_plan(_ts("09:41:01"), "external_position_change", {"reason": "again"})
    assert _kinds(tmp_path).count("plan_dead") == 1
    ex.void_position()
    assert ex.position() is None and ex._sim.target is None
    assert "stop_out" not in _kinds(tmp_path) and ex._plan["attempts_used"] == 0


# --------------------------------------------------------------------------- #
# plan 47 D1: a stop the OPERATOR moved at the broker is adopted by the model    #
# --------------------------------------------------------------------------- #

def test_set_stop_moves_the_sim_stop_and_records_it(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    res = ex.set_stop_override(_ts("09:50:00"), 29280.0)
    assert res == {"accepted": True, "detail": {"stop": 29280.0, "prev_stop": 29235.0}}
    assert ex.position()["stop"] == 29280.0
    moved = [r for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    assert len(moved) == 1 and moved[0]["reason"] == "operator"
    assert moved[0]["price"] == 29280.0 and moved[0]["prev_stop"] == 29235.0


def test_set_stop_refused_with_no_position(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    assert ex.set_stop_override(_ts("09:50:00"), 29280.0) == {
        "accepted": False, "reason": "no_position"}
    assert "stop_moved" not in _kinds(tmp_path)


@pytest.mark.parametrize("price", ["abc", None, 0.0, -5.0, float("nan")])
def test_set_stop_refused_with_a_bad_price(tmp_path, monkeypatch, price):
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    assert ex.set_stop_override(_ts("09:50:00"), price)["reason"] == "bad_price"
    assert ex.position()["stop"] == 29235.0


def test_a_touch_of_an_operator_stop_books_stop_out_initial_and_latches_positive(
        tmp_path, monkeypatch):
    """The 2026-09-30 shape: the operator trails the broker stop into profit and it is
    hit. The model books the exit itself, at the moved stop, on the tick that touches it
    — not a failed attempt, and a positive close."""
    seen = []
    port = MirroringOrderPort(OrderSim(dol=None), lambda ev: seen.append(ev) or None)
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    port._context = ex.order_context
    _step(ex, "09:40:00", 29250.0, fire=True)
    assert ex.set_stop_override(_ts("09:50:00"), 29280.0)["accepted"] is True
    assert [e["kind"] for e in seen] == ["fill"], "adopting a stop sends nothing"

    _step(ex, "09:50:01", 29290.0, lo=29282.0)                 # above it: still open
    assert ex.position() is not None
    _step(ex, "09:51:10", 29281.0, lo=29279.5)                 # through it
    assert ex.position() is None
    out = [r for r in _recs(tmp_path) if r["kind"] == "stop_out_initial"]
    assert len(out) == 1 and out[0]["price"] == 29280.0
    assert [e["kind"] for e in seen] == ["fill", "stop_out_initial"]
    assert ex._plan["attempts_used"] == 0 and "stop_out" not in _kinds(tmp_path)
    assert ex._positive_close is True

    asked = ex._market.asked
    _step(ex, "09:53:00", 29290.0, fire=True)
    assert ex.position() is None and ex._market.asked == asked
    assert _kinds(tmp_path).count("fill") == 1


def test_an_operator_stop_on_the_losing_side_still_books_a_stop_out(tmp_path,
                                                                     monkeypatch):
    """Tightened but still below the entry: a touch is a LOSS, so it is a `stop_out` —
    an attempt spent — exactly like the mechanism's own stop."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:40:00", 29250.0, fire=True)
    assert ex.set_stop_override(_ts("09:41:00"), 29244.0)["accepted"] is True
    _step(ex, "09:41:30", 29246.0, lo=29243.0)
    assert ex.position() is None
    kinds = _kinds(tmp_path)
    assert "stop_out" in kinds and "stop_out_initial" not in kinds
    assert [r for r in _recs(tmp_path) if r["kind"] == "stop_out"][0]["price"] == 29244.0
    assert ex._plan["attempts_used"] == 1 and ex._positive_close is False


# --------------------------------------------------------------------------- #
# cases 16, 17: the standing gates still hold                                   #
# --------------------------------------------------------------------------- #

def test_the_executor_source_still_contains_no_wall_clock():
    """Case 16."""
    import inspect
    src = inspect.getsource(executor_mod)
    assert "get_et_now" not in src and "datetime.now" not in src
    assert "Timestamp.now" not in src and "live_" + "orders" not in src


def test_the_import_ban_gates_cover_the_new_module():
    """Case 17."""
    from agent.trader import test_gate_mechanisms as gm
    from agent.trader import test_gate_no_legacy_writes as gw
    assert "agent.trader.order_port" in gm.NEW_MODULES
    assert any(p.endswith("order_port.py") for p in gw._modules())
    gm.test_no_new_module_imports_a_legacy_writer("agent.trader.order_port")
    gm.test_no_new_module_reaches_a_forbidden_call("agent.trader.order_port")
    gm.test_no_new_module_writes_a_legacy_state_file("agent.trader.order_port")


# --------------------------------------------------------------------------- #
# case 8: the five BEFORE streams                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
@pytest.mark.timeout(1800)
@pytest.mark.parametrize("date", BEFORE_DATES)
def test_the_five_before_dates_replay_byte_identical(date, monkeypatch):
    """Case 8. `fixtures/plan38_before/<date>.jsonl` is that session's
    `trader_decisions.jsonl` replayed against the warm thesis cache
    (`ACT_TRADER_BACKEND=anthropic`, no model call). First captured at 879032b, BEFORE the
    order port existed; RE-CAPTURED 2026-09-27 at cb083e2 (plan 42). The first commit that
    moves them is f73c20f (`tmso_reject`: the sweep bar may confirm itself, bisected);
    later deliberate commits in 879032b..cb083e2 (e.g. 8b90ee7's far-excursion veto) are
    in range but were not bisected individually. Moves: 08-31 -53.25 -> -38.25, 09-03
    +106.25 -> -32.50, 09-01/09-02 same totals with earlier `tmso_reject` entries, 09-04
    re-seeded 2026-09-27 (+65.75). 09-03 ALONE re-captured 2026-09-30 for §2's extension
    veto: its 10:18:00 `fvg_1m_post_extreme` long @ 29326.5 (-17.50) is vetoed at 127.25
    pts, -32.50 -> -15.00.

    2026-09-30 (plan 44 adoption): 08-31, 09-02, 09-03 and 09-04 re-seeded under
    per-TF level status (thesis.md §2.1g re-keys every recording; 09-01 is a
    stretch-override day and makes no call). Each re-seeded thesis kept its old bias and
    DOL, so only 08-31 was re-captured, and its move predates the re-seed: a375c83
    (PR #108, O3 on the 07:30-09:00 -> 09:00 micro pair) opened a pre-open
    `micro_smt_reject` short at 09:33:00 @ 29467.75, closed by `micro_smt_exit` at 12:07:00
    @ 29371.50 (+96.25), replacing the two `fvg_1m_post_extreme` stop-outs (-8.25, -30.00).
    The re-seeded replay is byte-identical to the old recording's replay after #108.

    2026-09-30 (§7c, the stop-bar retry): 09-01, 09-02, 09-03 and 09-04 re-captured.
    Each `tmso_reject` stop-out now writes `stop_bar_retry_armed` and, its bar having
    closed AGAINST the thesis, `stop_bar_retry_skipped adverse_close` (09-01 09:36:58,
    09-02 09:45:56, 09-04 09:31:33) — records only, every trade unchanged. 09-03's
    10:08:27 stop-out bar closed WITH the thesis: the retry fills 10:09:00 @ 29256.5
    (stop 29241.5), reaches its initial target 11:30 and is closed by `micro_smt_exit`
    12:59:00 @ 29532.0 (+275.50); the 10:18:00 extension veto is no longer produced
    (position open): -15.00 -> +260.50, 1 -> 2 attempts. 08-31 untouched (no covered
    stop-out; byte-identical).

    2026-09-30 (plan 47 O2, §8: a positive trade ends the plan): 08-31 and 09-03
    re-captured. Each gains exactly ONE record, `plan_dead reason=positive_close`, on the
    bar of its profitable `micro_smt_exit` (08-31 12:07:00, 09-03 12:59:00) — no trade,
    price or attempt moves. 09-01, 09-02 and 09-04 byte-identical (no positive close
    that was not already a plan death).

    2026-09-30 (plan 47 O4, the initial-target touch record): 09-03 ALONE re-captured.
    It gains one `initial_target_touched` record — the 11:27 bar touched the retry's
    initial (29523.825) and closed back, two bars before the 11:29 bar reached it. A
    record only; the other four have no touch that was not already the reach bar.

    2026-10-02 (§8 break-even at 50%, ON): 08-31, 09-02, 09-03 and 09-04 re-captured.
    Each position that reached half the way to T2 gains `trail_armed` + `stop_moved
    reason=breakeven` on that tick (09-02 10:07:15, 09-03 11:03:16, 09-04 10:12:00 —
    exits unchanged). 08-31 MOVES: the 09:33 `micro_smt_reject` short armed 09:35:59,
    came back to the entry and scratched (`stop_out_initial` @ 29467.75 at 09:47:47)
    instead of riding to the 12:07 micro-SMT exit (+96.25 -> 0.00); the plan lived on
    and recorded one extension veto at 10:07. 09-01 byte-identical (no position reached
    the mid). This is the rig's 08-31 result (o3-trail-study.md Part 4) reproduced on
    the recorded thesis.

    2026-10-03: all five re-captured, for two reasons at once. (1) Every `fill` record
    now carries `stop` (`order_sim._open`; the entry-criterion scorer reads it). (2) The
    first capture since PRs #116 (O1: §6.1 clauses 5-6; O6: §7c first-T2 reuse) and #117
    (O2: §11.6) were merged WITHOUT re-capture: 09-02 loses its 09:55:04 exit-tick
    entry (-4.75; O1), two trades instead of three; 09-04 loses its 10:03:05 exit-tick
    entry (-9.50; O1) and the 10:06:00 fill binds the first fill's pick asia(cur)_low
    29481.5 instead of TDO 29571.0 (O6), so break-even arms 10:26:38 and the take-profit
    lands 11:30:54 @ 29481.5 (+179.75 instead of +90.25 at 10:26:39); 09-01's plan_id
    moved (faf69eb4ae1d -> 1755712af6f6) with every trade unchanged; 08-31 and 09-03 are
    field-only. None of these moves was bisected to a single commit.

    A DELIBERATE mechanism change moves these streams; re-capture them then, exactly as
    the change protocol's step 3 says. A cold cache skips — it proves nothing either way.
    """
    from agent.trader.cached_backend import NetworkCallRefused
    from agent.trader.replay import run_replay

    monkeypatch.setenv("ACT_TRADER_BACKEND", "anthropic")
    monkeypatch.delenv("ACT_TRADER_MODEL", raising=False)
    monkeypatch.delenv("ACT_TRADER_5M", raising=False)
    try:
        res = run_replay([date], allow_calls=False)
    except NetworkCallRefused:
        pytest.skip(f"{date}: thesis cache is cold; BEFORE unavailable")
    with open(os.path.join(res[date]["run_dir"], DECISIONS_FILE), "rb") as fh:
        after = fh.read()
    with open(os.path.join(FIX, f"{date}.jsonl"), "rb") as fh:
        before = fh.read()
    assert after.replace(b"\r\n", b"\n") == before.replace(b"\r\n", b"\n")
