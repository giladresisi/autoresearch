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
    assert _kinds(tmp_path) == ["fill", "target_selected", "stop_out"]

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
    would hide this guard. The Executor's own copy of the target is cleared after the
    fill, so the plan SURVIVES the winner and the guard is what blocks."""
    ex = make_executor(tmp_path, monkeypatch,
                       pick={"id": "D1", "level": "x", "price": 29300.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    ex._target_price = None                       # `target_reached` can no longer fire
    _step(ex, "09:50:00", 29295.0, hi=29301.0)
    assert _kinds(tmp_path)[-1] == "take_profit"
    assert ex.bind_state()["plan_alive"] is True

    asked = ex._market.asked
    _step(ex, "09:52:00", 29290.0, fire=True)
    assert ex.position() is None and ex._market.asked == asked
    assert ex.bind_state()["entry_block"] == "after_positive_trade"
    assert _kinds(tmp_path).count("fill") == 1


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
    assert _kinds(tmp_path) == ["fill", "target_selected"]
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
    assert _kinds(tmp_path) == ["fill_voided", "fill", "target_selected"]


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
    `trader_decisions.jsonl` replayed at 879032b, BEFORE the order port existed, against
    the warm thesis cache (`ACT_TRADER_BACKEND=anthropic`, no model call).

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
