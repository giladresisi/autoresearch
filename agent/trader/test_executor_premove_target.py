"""Plan 46 T4 — the leg-mid take-profit on an UNRELATED plan (`Executor._set_target_on_fill`).

Pattern of `test_target_at_fill.py`: `select_target` is monkeypatched to a fixed T2 pick
and `OrderSim` is driven directly. The plan's leg: UP from 29000 to 29400 before 09:20,
so the plan is forced DOWN and the mid at the boundary is 29200.
"""
import json

import pandas as pd
import pytest

import agent.trader.executor as executor_mod
from agent.trader.executor import Executor
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-01"
ARM = pd.Timestamp(f"{DATE} 09:21", tz=TZ)
T2 = {"id": "D1", "level": "london(cur)_low", "price": 29000.0}
PREMOVE = {"status": "UNRELATED", "leg_direction": "UP", "forced_direction": "DOWN",
           "origin": 29000.0, "origin_ts": "2026-08-31T18:00:00-04:00",
           "extreme": 29400.0, "extreme_ts": "2026-09-01T09:00:00-04:00",
           "size": 400.0, "mid_0920": 29200.0,
           "boundary": "2026-09-01T09:20:00-04:00",
           "params": {"mid_min_ahead_pts": 5.0}}


def _t(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _recs(tmp_path, kind=None):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    out = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]
    return [r for r in out if kind is None or r["kind"] == kind]


def _executor(tmp_path, monkeypatch, *, premove=PREMOVE, direction="DOWN", pick=T2,
              mid_target=True):
    # The mid target is its own switch, OFF by default (operator decision 2026-09-30: the
    # path overrides the direction only). These tests pin the hook, so they turn it on.
    monkeypatch.setattr(executor_mod.premove_context, "MID_TARGET_ENABLED", mid_target)
    plan = {"plan_id": "p46", "thesis_id": None, "direction": direction,
            "dol": {"level": "premove_leg_mid", "price": 29200.0}, "valid_while": [],
            "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    if premove is not None:
        plan["premove"] = dict(premove)
    ex = Executor(tmp_path, plan=plan, arm_ts=ARM)
    monkeypatch.setattr(executor_mod, "select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    ex._bars = {"MNQ": None, "MES": None}
    return ex


def _mnq(rows):
    """rows: [(hms, high, low)] -> a 1m frame (Open = Close = mid)."""
    idx = [_t(h) for h, _, _ in rows]
    return pd.DataFrame({"Open": [(h + l) / 2 for _, h, l in rows],
                         "High": [h for _, h, _ in rows], "Low": [l for _, _, l in rows],
                         "Close": [(h + l) / 2 for _, h, l in rows], "Volume": 1.0},
                        index=pd.DatetimeIndex(idx))


def _fill(ex, hms, price, stop=29450.0):
    now = _t(hms)
    ex._sim.fill_market(now, direction=ex._plan["direction"], price=price, stop=stop,
                        artifact_id="gapX")
    ex._set_target_on_fill(now)
    return now


# --------------------------------------------------------------------------- #
# happy path                                                                    #
# --------------------------------------------------------------------------- #

def test_mid_uses_the_extension_up_to_the_fill(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    # 09:19 is before the boundary and must not count; 09:22's 29480 extends the leg.
    ex._track_premove_extreme(_t("09:25:10"), _mnq([("09:19", 29600.0, 29390.0),
                                                     ("09:22", 29480.0, 29380.0),
                                                     ("09:25", 29350.0, 29300.0)]))
    assert ex._pm_ext == 29480.0
    _fill(ex, "09:31:00", 29350.0)
    mid = (29000.0 + 29480.0) / 2
    assert ex._sim.target == pytest.approx(mid)
    assert ex._target_level == "premove_leg_mid"
    sel = _recs(tmp_path, "target_selected")
    assert len(sel) == 1 and sel[0]["level"] == "premove_leg_mid"
    assert sel[0]["pick"] == {"level": "premove_leg_mid", "price": mid, "premove": True}
    pmid = sel[0]["premove_mid"]
    assert pmid["mid"] == mid and pmid["extreme"] == 29480.0 and pmid["entry"] == 29350.0
    assert pmid["origin"] == 29000.0 and pmid["displaced"] == T2
    menu = json.loads((tmp_path / "target_menu.json").read_text(encoding="utf-8"))
    assert menu["default"]["level"] == "premove_leg_mid"
    assert menu["active"] == {"price": mid, "level": "premove_leg_mid"}


def test_an_intra_minute_tick_extension_counts(tmp_path, monkeypatch):
    """Tick-level: the LAST row is re-read on every call, so a second inside the
    minute that trades beyond the extreme extends it even if the minute closes back."""
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:05"), _mnq([("09:30", 29390.0, 29380.0)]))
    assert ex._pm_ext == 29400.0
    ex._track_premove_extreme(_t("09:30:06"), _mnq([("09:30", 29432.25, 29380.0)]))
    ex._track_premove_extreme(_t("09:30:07"), _mnq([("09:30", 29395.0, 29380.0)]))
    assert ex._pm_ext == 29432.25
    _fill(ex, "09:30:40", 29380.0)
    assert ex._sim.target == pytest.approx((29000.0 + 29432.25) / 2)


def test_on_bar_drives_the_tracker(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(ex, "_track_premove_extreme",
                        lambda now, mnq: calls.append((now, len(mnq))))
    idx = pd.date_range(_t("09:00"), _t("09:22"), freq="1min")
    frame = pd.DataFrame({"Open": 29390.0, "High": 29391.0, "Low": 29389.0,
                          "Close": 29390.0, "Volume": 1.0}, index=idx)
    ex.on_bar(_t("09:22:05"), {"MNQ": frame, "MES": frame})
    assert calls and calls[0][0] == _t("09:22:05")


def test_target_is_fixed_after_the_fill(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:30:10", 29380.0)
    assert ex._sim.target == pytest.approx(29200.0)
    ex._track_premove_extreme(_t("09:31:00"), _mnq([("09:31", 29445.0, 29380.0)]))
    assert ex._pm_ext == 29445.0
    assert ex._sim.target == pytest.approx(29200.0) and ex._target_price == 29200.0


def test_second_fill_recomputes_with_the_newer_extension(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:30:10", 29380.0, stop=29420.0)
    evs = ex._sim.on_bar(_t("09:31:00"), _mnq([("09:31", 29440.0, 29400.0)]).iloc[-1])
    assert [e["kind"] for e in evs] == ["stop_out"]
    ex._track_premove_extreme(_t("09:31:00"), _mnq([("09:31", 29440.0, 29400.0)]))
    _fill(ex, "09:35:00", 29410.0)
    assert ex._sim.target == pytest.approx((29000.0 + 29440.0) / 2)
    sel = _recs(tmp_path, "target_selected")
    assert [s["target"] for s in sel] == [29200.0, 29220.0]


def test_reaching_the_mid_kills_the_plan_target_reached_and_no_later_entry(tmp_path,
                                                                           monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:30:10", 29380.0)
    evs = ex._sim.on_bar(_t("09:50:00"), _mnq([("09:50", 29210.0, 29195.0)]).iloc[-1])
    assert [e["kind"] for e in evs] == ["take_profit"]
    assert evs[0]["price"] == pytest.approx(29200.0)
    reason, detail = ex._death(_mnq([("09:50", 29210.0, 29195.0)]), _t("09:50:00"))
    assert reason == "target_reached"
    assert detail == {"target": 29200.0, "level": "premove_leg_mid"}
    ex._kill_plan(_t("09:50:00"), reason, detail)
    assert ex.bind_state()["plan_alive"] is False
    dead = _recs(tmp_path, "plan_dead")
    assert dead and dead[0]["reason"] == "target_reached"


# --------------------------------------------------------------------------- #
# fallback                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("entry", [29204.0, 29200.0, 29190.0])
def test_mid_behind_or_within_5pts_of_entry_falls_back_to_T2_and_records_why(
        tmp_path, monkeypatch, entry):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:40:00", entry, stop=entry + 25.0)
    assert ex._sim.target == pytest.approx(29000.0) and ex._target_level == T2["level"]
    sel = _recs(tmp_path, "target_selected")[0]
    assert sel["pick"] == T2
    assert sel["premove_mid"] == {"skipped": "behind_entry", "mid": 29200.0,
                                  "entry": entry, "min_ahead_pts": 5.0}


def test_exactly_5pts_ahead_takes_the_mid(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:40:00", 29205.0, stop=29230.0)
    assert ex._target_level == "premove_leg_mid"


def test_operator_direction_flip_uses_T2(tmp_path, monkeypatch):
    t2_up = {"id": "D1", "level": "london(cur)_high", "price": 29600.0}
    ex = _executor(tmp_path, monkeypatch, direction="UP", pick=t2_up)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:40:00", 29390.0, stop=29365.0)
    assert ex._sim.target == pytest.approx(29600.0)
    sel = _recs(tmp_path, "target_selected")[0]
    assert sel["pick"] == t2_up
    assert sel["premove_mid"]["skipped"] == "direction_changed"


# --------------------------------------------------------------------------- #
# unchanged                                                                     #
# --------------------------------------------------------------------------- #

def test_mid_target_is_off_by_default():
    """Direction only: turning the path on must not change any take-profit."""
    from agent.trader import premove_context
    assert premove_context.MID_TARGET_ENABLED is False


def test_with_the_mid_target_off_a_forced_plan_takes_T2_exactly_like_any_plan(tmp_path,
                                                                              monkeypatch):
    """The default. An UNRELATED plan still carries its `premove` block (the direction was
    forced), but the Executor ignores it: no tracking, the T2 pick, no extra record keys."""
    ex = _executor(tmp_path, monkeypatch, mid_target=False)
    assert ex._pm is None
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29500.0, 29380.0)]))
    assert ex._pm_ext is None
    _fill(ex, "09:40:00", 29350.0)
    sel = _recs(tmp_path, "target_selected")[0]
    assert set(sel) == {"kind", "time", "plan_id", "mechanism", "pick", "target", "level"}
    assert sel["pick"] == T2 and ex._sim.target == pytest.approx(29000.0)
    assert ex._target_level != "premove_leg_mid"


def test_plan_without_premove_records_exactly_what_it_did_before(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, premove=None)
    assert ex._pm is None
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29500.0, 29380.0)]))
    assert ex._pm_ext is None
    _fill(ex, "09:40:00", 29350.0)
    sel = _recs(tmp_path, "target_selected")[0]
    assert set(sel) == {"kind", "time", "plan_id", "mechanism", "pick", "target", "level"}
    assert sel["pick"] == T2 and ex._sim.target == pytest.approx(29000.0)


@pytest.mark.parametrize("status", ["PART", "NOT_BIG", None])
def test_a_non_unrelated_block_is_ignored(tmp_path, monkeypatch, status):
    ex = _executor(tmp_path, monkeypatch, premove=dict(PREMOVE, status=status))
    assert ex._pm is None
    _fill(ex, "09:40:00", 29350.0)
    assert "premove_mid" not in _recs(tmp_path, "target_selected")[0]


def test_operator_agent_target_overrides_and_reset_restores_the_mid(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex._track_premove_extreme(_t("09:30:00"), _mnq([("09:30", 29400.0, 29380.0)]))
    _fill(ex, "09:40:00", 29350.0)
    got = ex.set_target_override(_t("09:41:00"), 29100.0)
    assert got["accepted"] is True and ex._sim.target == pytest.approx(29100.0)
    back = ex.reset_target(_t("09:42:00"))
    assert back == {"accepted": True, "detail": {"price": 29200.0,
                                                 "level": "premove_leg_mid"}}
    assert ex._sim.target == pytest.approx(29200.0)
