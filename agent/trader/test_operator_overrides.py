"""Plan 41: the operator's direction and target overrides.

The control file is the seam: `trade.py` appends, the graft drains on a bar close and
applies, and every verdict lands in `trader_decisions.jsonl` with BAR time. These cases
drive the two halves directly — the Executor's target surface against `OrderSim`, the
graft's drain against a stub Executor — so nothing here needs a whole session's bars.
"""
import json

import pandas as pd
import pytest

from agent.trader.executor import Executor
from agent.trader.operator_control import (KIND_RESET_TARGET, KIND_SET_DIRECTION,
                                           KIND_SET_TARGET, OperatorControl)
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-03"
NOW = pd.Timestamp(f"{DATE} 09:35", tz=TZ)


def _recs(tmp_path, kind=None):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    out = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]
    return [r for r in out if kind is None or r["kind"] == kind]


def _executor(tmp_path, monkeypatch, pick, direction="UP"):
    plan = {"plan_id": "p41", "direction": direction, "valid_while": [],
            "armed_classes": ["extreme_reject_close"], "attempts_used": 1,
            "blacklist": [], "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=pd.Timestamp(f"{DATE} 09:21", tz=TZ))
    monkeypatch.setattr("agent.trader.executor.select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    monkeypatch.setattr("agent.trader.executor.target_menu",
                        lambda *a, **k: ([dict(pick)] if pick else []))
    ex._bars = {"MNQ": None, "MES": None}
    return ex


def _filled(tmp_path, monkeypatch, pick, direction="UP", entry=29250.0):
    ex = _executor(tmp_path, monkeypatch, pick, direction=direction)
    ex._sim.fill_market(NOW, direction=direction, price=entry,
                        stop=entry - 25.0 if direction == "UP" else entry + 25.0,
                        artifact_id="gap41")
    ex._set_target_on_fill(NOW)
    return ex


# --------------------------------------------------------------------------- #
# the control file                                                              #
# --------------------------------------------------------------------------- #

def test_control_file_append_and_drain_is_monotonic(tmp_path):
    ctl = OperatorControl(tmp_path)
    a = ctl.append({"kind": KIND_SET_DIRECTION, "direction": "UP"})
    b = ctl.append({"kind": KIND_RESET_TARGET})
    assert (a["seq"], b["seq"]) == (1, 2)
    assert [r["seq"] for r in ctl.pending(0)] == [1, 2]
    assert [r["seq"] for r in ctl.pending(1)] == [2]
    assert ctl.pending(2) == []


def test_a_malformed_line_does_not_break_the_drain(tmp_path):
    ctl = OperatorControl(tmp_path)
    ctl.append({"kind": KIND_RESET_TARGET})
    with open(ctl.path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "set_targ')          # a half-written tail
    assert [r["kind"] for r in ctl.pending(0)] == [KIND_RESET_TARGET]


# --------------------------------------------------------------------------- #
# Executor: the target surface                                                  #
# --------------------------------------------------------------------------- #

def test_target_override_sets_the_sim_target_and_rearms_initial(tmp_path, monkeypatch):
    ex = _filled(tmp_path, monkeypatch, {"id": "D1", "level": "projection_up",
                                         "price": 29367.0})
    before = len(_recs(tmp_path, "initial_target_selected"))
    res = ex.set_target_override(NOW, 29500.0, level="htf_month_high_202608")
    assert res["accepted"] is True
    assert ex._sim.target == pytest.approx(29500.0)
    assert ex._target_level == "htf_month_high_202608"
    sel = _recs(tmp_path, "target_selected")[-1]
    assert sel["target"] == pytest.approx(29500.0)
    # the stage is re-derived from the NEW target, not left on the old one
    assert len(_recs(tmp_path, "initial_target_selected")) == before + 1


def test_target_override_refused_before_a_fill(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, {"id": "D1", "level": "x", "price": 29367.0})
    assert ex.set_target_override(NOW, 29500.0) == {"accepted": False,
                                                    "reason": "no_position"}
    assert ex._sim.target is None


def test_target_override_refused_when_the_fill_selected_no_default(tmp_path, monkeypatch):
    """An empty menu at the fill: there is nothing to override or to reset to."""
    ex = _filled(tmp_path, monkeypatch, None)
    assert ex.set_target_override(NOW, 29500.0)["reason"] == "no_default_target"


def test_target_override_refused_on_the_wrong_side_of_the_entry(tmp_path, monkeypatch):
    ex = _filled(tmp_path, monkeypatch, {"id": "D1", "level": "p", "price": 29367.0},
                 entry=29250.0)
    res = ex.set_target_override(NOW, 29200.0)          # below a LONG entry
    assert res["accepted"] is False and res["reason"] == "wrong_side_of_entry"
    assert ex._sim.target == pytest.approx(29367.0)     # unchanged

    short = _filled(tmp_path, monkeypatch, {"id": "D1", "level": "p", "price": 29100.0},
                    direction="DOWN", entry=29250.0)
    assert short.set_target_override(NOW, 29300.0)["reason"] == "wrong_side_of_entry"


def test_target_reset_restores_the_fill_default(tmp_path, monkeypatch):
    ex = _filled(tmp_path, monkeypatch, {"id": "D1", "level": "projection_up",
                                         "price": 29367.0})
    ex.set_target_override(NOW, 29500.0, level="operator")
    res = ex.reset_target(NOW)
    assert res["accepted"] is True
    assert ex._sim.target == pytest.approx(29367.0)
    assert ex._target_level == "projection_up"


def test_the_menu_file_is_written_at_the_fill(tmp_path, monkeypatch):
    ex = _filled(tmp_path, monkeypatch, {"id": "D1", "level": "projection_up",
                                         "price": 29367.0})
    menu = json.loads((tmp_path / Executor.MENU_FILE).read_text(encoding="utf-8"))
    assert menu["default"]["price"] == pytest.approx(29367.0)
    assert menu["rows"] and menu["rows"][0]["level"] == "projection_up"
    assert menu["position"] is True


# --------------------------------------------------------------------------- #
# the graft's drain                                                             #
# --------------------------------------------------------------------------- #

class _StubExecutor:
    def __init__(self, *, position=False, attempts=2):
        self._position, self._attempts = position, attempts
        self.killed = None
        self.set_calls, self.reset_calls = [], 0

    def has_position(self):
        return self._position

    def attempts_used(self):
        return self._attempts

    def kill_plan(self, now, reason, detail=None):
        self.killed = (now, reason, detail)

    def set_target_override(self, now, price, level=None):
        self.set_calls.append((price, level))
        return {"accepted": True, "detail": {"price": price, "level": level}}

    def reset_target(self, now):
        self.reset_calls += 1
        return {"accepted": True}

    def refresh_menu(self, now):
        return [{"id": "D2", "level": "htf_month_high_202608", "price": 30639.5}]

    def on_bar(self, *a, **k):
        pass


def _graft(tmp_path, plan=None, executor=None):
    from agent.trader.graft import TraderGraft
    g = TraderGraft(tmp_path, backend=None)
    g._plan = plan if plan is not None else {"plan_id": "p1", "direction": "DOWN"}
    g._executor = executor
    return g


def test_direction_override_kills_the_plan_and_rederives_the_other_way(tmp_path, monkeypatch):
    ex = _StubExecutor(attempts=2)
    g = _graft(tmp_path, executor=ex)
    monkeypatch.setattr(g, "_derive",
                        lambda thesis, bars, now: {"plan_id": "p2",
                                                   "direction": thesis["bias"],
                                                   "attempts_used": 0})
    OperatorControl(tmp_path).append({"kind": KIND_SET_DIRECTION, "direction": "UP"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})

    assert ex.killed is not None and ex.killed[1] == "operator_direction_change"
    assert g._plan["direction"] == "UP" and g._plan["plan_id"] == "p2"
    assert g._plan["replaces_plan_id"] == "p1"
    rec = _recs(tmp_path, "operator_override")[-1]
    assert rec["accepted"] is True and rec["command"] == KIND_SET_DIRECTION
    assert rec["time"].startswith(f"{DATE}T09:35")          # BAR time, not wall clock


def test_direction_override_carries_remaining_attempts(tmp_path, monkeypatch):
    ex = _StubExecutor(attempts=2)
    g = _graft(tmp_path, executor=ex)
    monkeypatch.setattr(g, "_derive", lambda t, b, n: {"plan_id": "p2",
                                                       "direction": t["bias"]})
    OperatorControl(tmp_path).append({"kind": KIND_SET_DIRECTION, "direction": "UP"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert g._plan["attempts_used"] == 2                    # carried, not reset


def test_direction_override_reset_attempts_flag_starts_a_fresh_budget(tmp_path, monkeypatch):
    ex = _StubExecutor(attempts=2)
    g = _graft(tmp_path, executor=ex)
    monkeypatch.setattr(g, "_derive", lambda t, b, n: {"plan_id": "p2",
                                                       "direction": t["bias"]})
    OperatorControl(tmp_path).append({"kind": KIND_SET_DIRECTION, "direction": "UP",
                                      "reset_attempts": True})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert g._plan["attempts_used"] == 0


def test_direction_override_refused_with_an_open_position(tmp_path, monkeypatch):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_SET_DIRECTION, "direction": "UP"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.killed is None and g._plan["direction"] == "DOWN"
    rec = _recs(tmp_path, "operator_override")[-1]
    assert rec["accepted"] is False and rec["reason"] == "open_position"


def test_direction_override_refused_on_a_dark_day(tmp_path):
    g = _graft(tmp_path, plan=None, executor=None)
    OperatorControl(tmp_path).append({"kind": KIND_SET_DIRECTION, "direction": "UP"})
    g._drain_operator(NOW, {}, None)
    assert _recs(tmp_path, "operator_override")[-1]["reason"] == "no_plan"


def test_target_override_by_menu_row_resolves_the_price(tmp_path):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_SET_TARGET, "level": "D2"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.set_calls == [(30639.5, "htf_month_high_202608")]


def test_target_override_with_a_level_outside_the_menu_is_refused(tmp_path):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_SET_TARGET, "level": "nope"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.set_calls == []
    assert _recs(tmp_path, "operator_override")[-1]["reason"] == "level_not_in_menu"


def test_reset_target_command_reaches_the_executor(tmp_path):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_RESET_TARGET})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.reset_calls == 1


def test_an_unknown_command_is_recorded_not_ignored(tmp_path):
    g = _graft(tmp_path, executor=_StubExecutor())
    OperatorControl(tmp_path).append({"kind": "detonate"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    rec = _recs(tmp_path, "operator_override")[-1]
    assert rec["accepted"] is False and rec["reason"].startswith("unknown_command")


def test_each_record_is_applied_exactly_once(tmp_path):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_RESET_TARGET})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    g._drain_operator(NOW + pd.Timedelta(minutes=1), {}, {"bias": "DOWN"})
    assert ex.reset_calls == 1


def test_a_record_is_held_until_its_created_at_bar(tmp_path):
    """Replay fidelity: an override issued at 09:40 must not land on the 09:35 bar."""
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append(
        {"kind": KIND_RESET_TARGET, "created_at": f"{DATE} 09:40:12-04:00"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})                   # 09:35 — too early
    assert ex.reset_calls == 0 and _recs(tmp_path, "operator_override") == []
    g._drain_operator(pd.Timestamp(f"{DATE} 09:41", tz=TZ), {}, {"bias": "DOWN"})
    assert ex.reset_calls == 1


def test_an_undue_record_does_not_block_being_applied_later(tmp_path):
    """The drain stops at the first undue record and resumes from it, in order."""
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    ctl = OperatorControl(tmp_path)
    ctl.append({"kind": KIND_RESET_TARGET, "created_at": f"{DATE} 09:30:00-04:00"})
    ctl.append({"kind": KIND_RESET_TARGET, "created_at": f"{DATE} 09:50:00-04:00"})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.reset_calls == 1
    g._drain_operator(pd.Timestamp(f"{DATE} 09:51", tz=TZ), {}, {"bias": "DOWN"})
    assert ex.reset_calls == 2


def test_a_record_without_a_stamp_is_due_immediately(tmp_path):
    ex = _StubExecutor(position=True)
    g = _graft(tmp_path, executor=ex)
    OperatorControl(tmp_path).append({"kind": KIND_RESET_TARGET})
    g._drain_operator(NOW, {}, {"bias": "DOWN"})
    assert ex.reset_calls == 1
