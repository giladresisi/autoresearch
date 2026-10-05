"""The follow-up entry wired into the Executor (§11.8 CANDIDATE, plan 50).

A scripted long: entered by a stubbed fire, its stop moved by the MES-sweep stage on a
synthetic MES sweep, touched in profit — and then the follow-up's own triggers, window,
budget and deferred death, over the REAL `Executor.on_bar`. Only the regular market
mechanisms are stubbed (`StubMarket`), exactly as in `test_executor_live_rules.py`.
"""
import pandas as pd
import pytest

from agent.trader import followup, mes_sweep
from agent.trader.test_executor_live_rules import (DATE, TZ, _fire, _kinds, _recs, _ts,
                                                   make_executor)

T2 = 29400.0
PICK = {"id": "D1", "level": "far", "price": T2}
LEVEL = 7800.0


class Tape:
    """1m bars from 09:30; `put` overrides one minute, everything else is flat. `frames`
    hands the Executor the completed bars plus an in-progress one, as replay does."""

    def __init__(self, mnq_px=29250.0, mes_px=7790.0, start="09:30", end="11:30"):
        idx = pd.date_range(_ts(f"{start}:00"), _ts(f"{end}:00"), freq="1min")
        self.mnq = pd.DataFrame({"Open": mnq_px, "High": mnq_px + 0.5,
                                 "Low": mnq_px - 0.5, "Close": mnq_px, "Volume": 1.0},
                                index=idx)
        self.mes = pd.DataFrame({"Open": mes_px, "High": mes_px + 0.5,
                                 "Low": mes_px - 0.5, "Close": mes_px, "Volume": 1.0},
                                index=idx)

    def put(self, hms, mnq=None, mes=None):
        ts = _ts(f"{hms}:00")
        for frame, row in ((self.mnq, mnq), (self.mes, mes)):
            if row is not None:
                frame.loc[ts, ["Open", "High", "Low", "Close"]] = row
        return self

    def frames(self, now, partial=None):
        minute = now.floor("1min")
        out = {}
        for key, frame in (("MNQ", self.mnq), ("MES", self.mes)):
            done = frame[frame.index < minute].copy()
            o = float(frame.loc[minute, "Open"])
            row = partial if (partial is not None and key == "MNQ") else (o, o, o, o)
            done.loc[minute] = [row[0], row[1], row[2], row[3], 1.0]
            out[key] = done
        return out


def go(ex, tape, hms, partial=None, fire=None):
    now = _ts(hms)
    if fire is not None:
        ex._market.fire = _fire(now, fire)
    ex.on_bar(now, tape.frames(now, partial))
    return now


def minutes(ex, tape, start, end):
    """Step every minute :00 from `start` to `end` inclusive."""
    for ts in pd.date_range(_ts(f"{start}:00"), _ts(f"{end}:00"), freq="1min"):
        go(ex, tape, ts.strftime("%H:%M:%S"))


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setattr(followup, "FOLLOWUP_ENABLED", True)
    monkeypatch.setattr(mes_sweep, "MES_SWEEP_ENABLED", True)
    # the synthetic frames do not reach back to 18:00: hand the stage its level directly
    monkeypatch.setattr(mes_sweep, "day_extreme", lambda mes, d, at: (LEVEL, at))


def _entered_tape(entry_min="10:20", sweep_min="10:25"):
    """Long from 29250 at `entry_min`; MES sweeps 7800 in `sweep_min` and closes back
    under it while MNQ closes that bar at 29340, so the sweep stop is 29335."""
    t = Tape()
    after = t.mnq.index > _ts(f"{sweep_min}:00")
    t.mnq.loc[after, ["Open", "High", "Low", "Close"]] = [29340.0, 29340.5, 29339.5, 29340.0]
    t.put(sweep_min, mnq=(29330.0, 29345.0, 29328.0, 29340.0),
          mes=(7799.0, 7802.0, 7798.0, 7799.5))
    return t


def _to_exit(tmp_path, monkeypatch, tape=None, pick=PICK):
    """Entry 10:20, sweep stop set 10:26:00, touched 10:26:30. Returns (ex, tape)."""
    ex = make_executor(tmp_path, monkeypatch, pick=pick)
    tape = tape or _entered_tape()
    minutes(ex, tape, "10:15", "10:19")
    go(ex, tape, "10:20:00", fire=29250.0)
    minutes(ex, tape, "10:21", "10:25")
    go(ex, tape, "10:26:00")
    assert ex.position()["stop"] == 29335.0
    go(ex, tape, "10:26:30", partial=(29340.0, 29341.0, 29334.0, 29334.5))
    return ex, tape


# 7 ------------------------------------------------------------------------- #

def test_the_sweep_stop_touched_in_profit_opens_the_followup_and_defers_the_death(
        tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch)
    assert ex.position() is None
    recs = _recs(tmp_path)
    exit_rec = [r for r in recs if r["kind"] == "stop_out_initial"][0]
    assert exit_rec["price"] == 29335.0
    (opened,) = [r for r in recs if r["kind"] == "followup_opened"]
    assert opened["exit_px"] == 29335.0 and opened["exit_kind"] == "stop_out_initial"
    assert opened["t2"] == T2 and opened["t2_remaining"] == 65.0
    assert opened["first_entry"] == 29250.0
    assert opened["window_end"].startswith(f"{DATE}T10:46:30")
    assert "plan_dead" not in _kinds(tmp_path)
    go(ex, tape, "10:27:00")
    st = ex.bind_state()
    assert st["plan_alive"] is True and st["followup"]["open"] is True
    # the regular entry paths stay shut; the follow-up's own are the only way in
    assert ex._entry_block(_ts("10:27:00")) == "followup_only"
    assert ex._positive_close_death() == (None, None)


# 8 ------------------------------------------------------------------------- #

def _reject_tape():
    t = _entered_tape()
    t.put("10:26", mnq=(29338.0, 29340.0, 29320.0, 29322.0))        # sets the low
    t.put("10:27", mnq=(29322.0, 29330.0, 29321.0, 29328.0))        # quiet 1
    t.put("10:28", mnq=(29328.0, 29334.0, 29322.0, 29326.0))        # quiet 2
    t.put("10:29", mnq=(29326.0, 29331.0, 29321.5, 29330.0))        # quiet 3: armed
    t.put("10:30", mnq=(29330.0, 29336.0, 29322.0, 29334.0))
    t.put("10:31", mnq=(29330.0, 29334.0, 29322.0, 29333.0))
    t.put("10:32", mnq=(29312.0, 29327.0, 29310.0, 29324.0))        # new low, closes up
    t.put("10:33", mnq=(29324.0, 29326.0, 29322.0, 29325.0))
    return t


def test_reject_close_fires_past_the_cutoff_and_reuses_the_first_t2(tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch, tape=_reject_tape())
    minutes(ex, tape, "10:27", "10:32")
    assert ex.position() is None                 # the 10:32 bar completes at 10:33:00
    go(ex, tape, "10:33:00")
    pos = ex.position()
    assert pos is not None and pos["entry"] == 29324.0 and pos["stop"] == 29310.0
    recs = _recs(tmp_path)
    fills = [r for r in recs if r["kind"] == "fill"]
    assert [f["mechanism"] for f in fills] == ["extreme_reject_close",
                                               "followup_reject_close"]
    assert fills[1]["followup"] is True and fills[1]["time"].startswith(f"{DATE}T10:33:00")
    tgt = [r for r in recs if r["kind"] == "target_selected"][-1]
    assert tgt["pick"]["price"] == T2 and "reused_from" in tgt["first_pick"]
    assert ex._sim.target == T2
    assert "plan_dead" not in _kinds(tmp_path)
    assert ex._entry_block(_ts("10:33:00")) == "followup_only"


# 9 ------------------------------------------------------------------------- #

def _two_attempt_tape():
    t = _reject_tape()
    t.put("10:33", mnq=(29324.0, 29325.0, 29300.0, 29303.0))
    t.put("10:34", mnq=(29305.0, 29310.0, 29304.0, 29309.0))        # first bar of a gap
    t.put("10:35", mnq=(29309.0, 29330.0, 29308.0, 29328.0))
    t.put("10:36", mnq=(29328.0, 29335.0, 29315.0, 29333.0))        # low > 29310: gap
    return t


def test_followup_stop_outs_spend_the_followup_budget_not_the_plans(tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch, tape=_two_attempt_tape())
    minutes(ex, tape, "10:27", "10:33")
    assert ex.position()["entry"] == 29324.0
    go(ex, tape, "10:33:30", partial=(29324.0, 29325.0, 29309.0, 29309.5))   # stop 29310
    assert ex.position() is None
    assert ex._plan["attempts_used"] == 0
    assert ex.bind_state()["followup"]["attempts"] == 1
    assert ex.bind_state()["followup"]["open"] is True
    assert "plan_dead" not in _kinds(tmp_path)
    minutes(ex, tape, "10:34", "10:37")
    pos = ex.position()
    assert pos is not None and pos["entry"] == 29333.0
    mechs = [r["mechanism"] for r in _recs(tmp_path) if r["kind"] == "fill"]
    assert mechs[-1] == "followup_fvg_1m"
    assert pos["stop"] == max(29304.0 - 0.25, 29333.0 - 25.0)
    go(ex, tape, "10:37:30", partial=(29333.0, 29334.0, 29307.0, 29307.5))
    assert ex.position() is None and ex._plan["attempts_used"] == 0
    recs = _recs(tmp_path)
    kinds = [r["kind"] for r in recs]
    assert kinds.index("followup_closed") < kinds.index("plan_dead")
    closed = [r for r in recs if r["kind"] == "followup_closed"][0]
    assert closed["reason"] == "attempts_exhausted" and closed["attempts"] == 2
    dead = [r for r in recs if r["kind"] == "plan_dead"][0]
    assert dead["reason"] == "positive_close"
    assert dead["detail"]["followup"] == {"reason": "attempts_exhausted", "attempts": 2}
    assert ex._plan["attempts_used"] == 0


# 10 ------------------------------------------------------------------------ #

def test_window_end_with_no_fire_closes_it_then_the_plan_dies(tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch)
    minutes(ex, tape, "10:27", "10:46")
    assert "followup_closed" not in _kinds(tmp_path)
    assert ex.bind_state()["followup"]["open"] is True
    go(ex, tape, "10:46:30")
    kinds = _kinds(tmp_path)
    assert kinds.index("followup_closed") < kinds.index("plan_dead")
    closed = [r for r in _recs(tmp_path) if r["kind"] == "followup_closed"][0]
    assert closed["reason"] == "window_end"
    dead = [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"][0]
    assert dead["reason"] == "positive_close"
    assert dead["detail"]["followup"]["reason"] == "window_end"
    assert ex.bind_state()["plan_alive"] is False


# 11 ------------------------------------------------------------------------ #

def test_flag_off_the_exit_ends_the_plan_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(followup, "FOLLOWUP_ENABLED", False)
    ex, tape = _to_exit(tmp_path, monkeypatch)
    kinds = _kinds(tmp_path)
    assert "plan_dead" in kinds
    assert not any(k.startswith("followup_") for k in kinds)
    assert [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"][0][
        "reason"] == "positive_close"
    assert ex._entry_block(_ts("10:27:00")) == "after_positive_trade"
    assert ex.bind_state().get("followup") is None


# 12 ------------------------------------------------------------------------ #

def test_a_t2_too_close_skips_and_the_plan_dies_as_today(tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch, pick={"id": "D1", "level": "x",
                                                     "price": 29360.0})
    recs = _recs(tmp_path)
    (skipped,) = [r for r in recs if r["kind"] == "followup_skipped"]
    assert skipped["reason"] == "t2_too_close"
    kinds = [r["kind"] for r in recs]
    assert "followup_opened" not in kinds and "plan_dead" in kinds
    assert ex.bind_state().get("followup") is None


# 13 ------------------------------------------------------------------------ #

def test_a_followup_position_is_exempt_from_the_sweep_stop(tmp_path, monkeypatch):
    """Operator, 2026-10-04 (09-04: the stage rebuilt at the follow-up's fill swept it out
    again six minutes later for +5). MES sweeping again while the FOLLOW-UP position is
    open moves nothing: its stop is its own, and no second follow-up can arise."""
    monkeypatch.setattr(mes_sweep, "MES_SWEEP_CUTOFF_ET", None)   # not the reason it stays
    tape = _reject_tape()
    # the follow-up position (29324 from 10:33): MES sweeps again in 10:34 and closes
    # inside; MNQ closes that bar at 29345 — the first version moved the stop to 29340
    tape.put("10:34", mnq=(29330.0, 29346.0, 29328.0, 29345.0),
             mes=(7799.0, 7801.0, 7798.0, 7799.5))
    tape.put("10:35", mnq=(29345.0, 29346.0, 29344.0, 29345.0))
    ex, tape = _to_exit(tmp_path, monkeypatch, tape=tape)
    minutes(ex, tape, "10:27", "10:33")
    assert ex.position()["entry"] == 29324.0
    stop_before = ex.position()["stop"]
    minutes(ex, tape, "10:34", "10:35")
    assert ex.position()["stop"] == stop_before
    go(ex, tape, "10:35:30", partial=(29345.0, 29346.0, 29339.0, 29339.5))
    assert ex.position() is not None, "the 10:35 dip would have touched the swept stop"
    assert [r for r in _recs(tmp_path) if r["kind"] == "stop_moved"
            and r.get("reason") == "mes_sweep" and r.get("entry") == 29324.0] == []
    assert _kinds(tmp_path).count("followup_opened") == 1
    assert ex._mes_sweep is None


# extras: the vetoes ------------------------------------------------------------ #

def test_a_tick_beyond_the_legs_origin_closes_it_structure_broken(tmp_path, monkeypatch):
    ex, tape = _to_exit(tmp_path, monkeypatch)
    go(ex, tape, "10:27:00")
    go(ex, tape, "10:27:30", partial=(29330.0, 29331.0, 29249.0, 29250.0))   # < 29249.5
    closed = [r for r in _recs(tmp_path) if r["kind"] == "followup_closed"]
    assert [c["reason"] for c in closed] == ["structure_broken"]
    assert "plan_dead" in _kinds(tmp_path)


def test_a_falsified_thesis_does_not_block_the_followup(tmp_path, monkeypatch):
    """Operator, 2026-10-04: on 09-18 the falsifier veto blocked a follow-up the tape then
    rewarded. Plans no longer die on falsification; the follow-up does not either."""
    ex = make_executor(tmp_path, monkeypatch, pick=PICK)
    ex._falsify_recorded = True                  # recorded earlier in the plan
    tape = _entered_tape()
    minutes(ex, tape, "10:15", "10:19")
    go(ex, tape, "10:20:00", fire=29250.0)
    minutes(ex, tape, "10:21", "10:26")
    go(ex, tape, "10:26:30", partial=(29340.0, 29341.0, 29334.0, 29334.5))
    kinds = _kinds(tmp_path)
    assert "followup_opened" in kinds and "followup_skipped" not in kinds
    assert "plan_dead" not in kinds


def test_a_non_sweep_profitable_exit_opens_no_followup(tmp_path, monkeypatch):
    """The break-even / any other stop touched in profit is today's behaviour."""
    ex = make_executor(tmp_path, monkeypatch, pick=PICK)
    tape = Tape()
    minutes(ex, tape, "10:15", "10:19")
    go(ex, tape, "10:20:00", fire=29250.0)
    go(ex, tape, "10:21:00")
    ex.set_stop_override(_ts("10:21:00"), 29260.0)
    go(ex, tape, "10:21:30", partial=(29260.0, 29262.0, 29259.0, 29261.0))
    kinds = _kinds(tmp_path)
    assert "stop_out_initial" in kinds and "plan_dead" in kinds
    assert not any(k.startswith("followup_") for k in kinds)


def test_a_followup_position_is_exempt_from_breakeven_at_fifty_percent(tmp_path, monkeypatch):
    """Operator, 2026-10-04: on the forced-entry rig break-even at 50% scratched four
    follow-ups that had run 29-93 pts. A follow-up position that covers half the way to
    T2 keeps its own stop."""
    tape = _reject_tape()
    mid = 29324.0 + 0.5 * (T2 - 29324.0)
    tape.put("10:34", mnq=(29325.0, mid + 2.0, 29324.0, mid + 1.0))     # past the mid
    ex, tape = _to_exit(tmp_path, monkeypatch, tape=tape)
    minutes(ex, tape, "10:27", "10:33")
    assert ex.position()["entry"] == 29324.0
    stop_before = ex.position()["stop"]
    minutes(ex, tape, "10:34", "10:35")
    assert ex.position() is not None and ex.position()["stop"] == stop_before
    assert ex._trail is None
    assert not [r for r in _recs(tmp_path) if r["kind"] in ("trail_armed", "stop_moved")
                and r.get("entry") == 29324.0]
