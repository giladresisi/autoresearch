"""`nym_mid_reject` (`l2-mechanisms.md` §11.9 CANDIDATE): the detector, its switch, the
shared-bar rule with `tmso_reject`, the Executor wiring, and the 2026-10-06 real tape.

Synthetic day: 1m bars from 06:00 ET, flat at PX (H/L +-0.5) before the open, with the
range made by a 07:00 high at PX+40 and an 08:00 low at PX-40, so the NY-morning mid is
PX until a bar extends the range. From 09:30 the tape sits flat at PX+20 (an UP day: the
open is above the mid) or PX-20 (DOWN), so only the scripted bars come near the mid.
"""
import json

import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
import agent.trader.named_cases as nc
import agent.trader.nym_mid_reject as nym
from agent.trader import executor as executor_mod
from agent.trader.executor import Executor
from agent.trader.market_mechanisms import MarketMechanisms
from agent.trader.nym_mid_reject import NymMidReject, nym_mid, precondition
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-29"
PX = 30000.0
MES_PX = 7700.0


def _ts(hms, date=DATE):
    return pd.Timestamp(f"{date} {hms}", tz=TZ)


def _day(end_hm, *, up=True, bars=None, base=None, start="06:00"):
    """1m frame from `start` through the bar labelled `end_hm`."""
    idx = pd.date_range(_ts(start), _ts(end_hm), freq="1min")
    post = PX + 20.0 if up else PX - 20.0
    df = pd.DataFrame({"Open": PX, "High": PX + 0.5, "Low": PX - 0.5, "Close": PX,
                       "Volume": 1.0}, index=idx)
    after = df.index >= _ts("09:30")
    for col, off in (("Open", 0.0), ("High", 0.5), ("Low", -0.5), ("Close", 0.0)):
        df.loc[after, col] = (base if base is not None else post) + off
    overrides = {"07:00": (PX, PX + 40, PX - 0.5, PX), "08:00": (PX, PX + 0.5, PX - 40, PX)}
    overrides.update(bars or {})
    for hm, (o, h, l, c) in overrides.items():
        ts = _ts(hm)
        if ts in df.index:
            df.loc[ts, ["Open", "High", "Low", "Close"]] = [o, h, l, c]
    return df


def _run(machine, frame, *, first="09:30", last="10:40"):
    """Drive the machine bar by bar (each completed at label + 1 min) over `frame`, the
    frame handed in truncated before `now` the way the Executor's is. Returns the fires."""
    fires = []
    for label, row in frame[(frame.index >= _ts(first)) & (frame.index <= _ts(last))].iterrows():
        now = label + pd.Timedelta(minutes=1)
        f = machine.on_bar_close(now, row, frame[frame.index < now])
        if f is not None:
            fires.append(f)
    return fires


# --------------------------------------------------------------------------- #
# the level and the precondition                                              #
# --------------------------------------------------------------------------- #

def test_the_mid_is_the_06_00_range_through_the_bar_being_judged():
    f = _day("09:40")
    day = _ts("00:00")
    assert nym_mid(f, day, _ts("09:29")) == PX
    g = _day("09:40", bars={"09:35": (PX - 8, PX - 2, PX - 60, PX - 5)})
    assert nym_mid(g, day, _ts("09:34")) == PX
    assert nym_mid(g, day, _ts("09:35")) == PX - 10          # (PX+40 + PX-60) / 2


def test_no_mid_when_the_frame_does_not_hold_the_06_00_open():
    f = _day("09:40", start="07:00")
    assert nym_mid(f, _ts("00:00"), _ts("09:35")) is None
    assert precondition(f, _ts("00:00"), short=False)["reason"] == "no_range"


@pytest.mark.parametrize("up,base,ok", [(True, PX + 20, True), (True, PX - 20, False),
                                        (False, PX - 20, True), (False, PX + 20, False),
                                        (True, PX, False), (False, PX, False)])
def test_precondition_open_on_the_favourable_side_of_the_0930_mid(up, base, ok):
    f = _day("09:40", up=up, base=base)
    pre = precondition(f, _ts("00:00"), short=not up)
    assert pre["ok"] is ok and pre["mid"] == PX and pre["open"] == base


# --------------------------------------------------------------------------- #
# the detector                                                                 #
# --------------------------------------------------------------------------- #

UP_SELF = {"09:35": (PX + 1, PX + 9, PX - 5, PX + 6)}          # sweeps, closes > mid, green
UP_RED = {"09:35": (PX + 5, PX + 9, PX - 5, PX + 2)}           # sweeps, closes > mid, red
UP_NEXT = {**UP_RED, "09:36": (PX + 2, PX + 8, PX + 1, PX + 7)}


def test_up_sweep_bar_confirms_itself():
    fires = _run(NymMidReject("UP"), _day("10:40", bars=UP_SELF))
    assert len(fires) == 1
    f = fires[0]
    assert f["time"] == _ts("09:36:00") and f["price"] == PX + 6
    assert f["stop"] == PX + 6 - nym.STOP_PTS and f["direction"] == "UP"
    assert f["mechanism"] == "nym_mid_reject" and f["confirm"] == "sweep_bar"
    assert f["level"] == PX


def test_up_next_bar_confirms_a_red_sweep_bar():
    fires = _run(NymMidReject("UP"), _day("10:40", bars=UP_NEXT))
    assert [(f["time"], f["price"], f["confirm"]) for f in fires] == \
        [(_ts("09:37:00"), PX + 7, "next_bar")]


def test_no_confirmation_two_bars_after_the_sweep():
    bars = {**UP_RED, "09:36": (PX + 3, PX + 4, PX + 1, PX + 2),   # red, no sweep
            "09:37": (PX + 2, PX + 9, PX + 1, PX + 8)}               # green, too late
    assert _run(NymMidReject("UP"), _day("10:40", bars=bars)) == []


def test_a_close_under_the_mid_never_fires_even_green():
    bars = {"09:35": (PX - 4, PX + 2, PX - 6, PX - 1)}               # green, close < mid
    assert _run(NymMidReject("UP"), _day("10:40", bars=bars)) == []


def test_the_mid_is_recomputed_with_the_sweep_bars_own_range():
    """09:35 extends the low to PX-60, so its mid is PX-10: closing PX-5 green is above
    it and fires. Under a mid frozen at PX it would not."""
    bars = {"09:35": (PX - 8, PX - 2, PX - 60, PX - 5)}
    fires = _run(NymMidReject("UP"), _day("10:40", bars=bars))
    assert len(fires) == 1 and fires[0]["level"] == PX - 10 and fires[0]["price"] == PX - 5


def test_down_mirror_sweep_bar_and_next_bar():
    self_bar = {"09:35": (PX - 1, PX + 5, PX - 9, PX - 6)}
    fires = _run(NymMidReject("DOWN"), _day("10:40", up=False, bars=self_bar))
    assert len(fires) == 1 and fires[0]["price"] == PX - 6
    assert fires[0]["stop"] == PX - 6 + nym.STOP_PTS and fires[0]["direction"] == "DOWN"
    nxt = {"09:35": (PX - 5, PX + 5, PX - 9, PX - 2), "09:36": (PX - 2, PX - 1, PX - 8, PX - 7)}
    fires = _run(NymMidReject("DOWN"), _day("10:40", up=False, bars=nxt))
    assert [(f["time"], f["confirm"]) for f in fires] == [(_ts("09:37:00"), "next_bar")]


def test_precondition_off_means_no_fire_all_day():
    # UP shape, but the 09:30 open sits under the mid.
    bars = {**UP_SELF, "09:30": (PX - 10, PX + 0.5, PX - 10.5, PX + 20)}
    m = NymMidReject("UP")
    assert _run(m, _day("10:40", bars=bars)) == []
    assert m.state()["precondition"]["reason"] == "open_on_unfavourable_side"
    # The mirror: a DOWN plan on an UP-favourable open.
    assert _run(NymMidReject("DOWN"), _day("10:40", bars=UP_SELF)) == []


def test_a_fire_does_not_latch_but_a_fill_does():
    bars = {**UP_SELF, "09:45": (PX + 1, PX + 9, PX - 5, PX + 6)}
    m = NymMidReject("UP")
    assert [f["time"] for f in _run(m, _day("10:40", bars=bars))] == \
        [_ts("09:36:00"), _ts("09:46:00")]
    m = NymMidReject("UP")
    f = _day("10:40", bars=bars)
    first = _run(m, f, last="09:35")
    assert len(first) == 1
    m.latch()                                   # the Executor: it FILLED
    assert m.filled and _run(m, f, first="09:36") == []


def test_window_bars_before_0930_and_fires_at_or_after_1030_are_ignored():
    early = {"09:29": (PX + 1, PX + 9, PX - 5, PX + 6)}
    assert _run(NymMidReject("UP"), _day("10:40", bars=early), first="09:20") == []
    late_ok = {"10:28": (PX + 1, PX + 9, PX - 5, PX + 6)}
    fires = _run(NymMidReject("UP"), _day("10:40", bars=late_ok))
    assert [f["time"] for f in fires] == [_ts("10:29:00")]
    at_cutoff = {"10:29": (PX + 1, PX + 9, PX - 5, PX + 6)}
    assert _run(NymMidReject("UP"), _day("10:40", bars=at_cutoff)) == []
    # A 10:28 red sweep would confirm on the 10:29 bar, i.e. at 10:30:00: refused.
    nxt = {"10:28": (PX + 5, PX + 9, PX - 5, PX + 2), "10:29": (PX + 2, PX + 8, PX + 1, PX + 7)}
    assert _run(NymMidReject("UP"), _day("10:40", bars=nxt)) == []


def test_the_0930_opening_bar_cannot_sweep_and_self_confirm():
    """Operator 2026-10-07 (motivated by 09-18): the 09:30 opening bar is excluded."""
    bars = {"09:30": (PX + 1, PX + 9, PX - 5, PX + 6)}
    assert _run(NymMidReject("UP"), _day("10:40", bars=bars)) == []


def test_a_0930_sweep_cannot_arm_a_0931_confirmation():
    bars = {"09:30": (PX + 5, PX + 9, PX - 5, PX + 2),               # red sweep
            "09:31": (PX + 2, PX + 8, PX + 1, PX + 7)}               # green, would confirm
    assert _run(NymMidReject("UP"), _day("10:40", bars=bars)) == []


def test_a_0931_sweep_still_fires():
    bars = {"09:31": (PX + 1, PX + 9, PX - 5, PX + 6)}
    fires = _run(NymMidReject("UP"), _day("10:40", bars=bars))
    assert [(f["time"], f["confirm"]) for f in fires] == [(_ts("09:32:00"), "sweep_bar")]


def test_the_precondition_still_reads_the_0930_open():
    """The 09:30 bar is excluded from sweeping but its OPEN still decides the day."""
    sweep = {"09:31": (PX + 1, PX + 9, PX - 5, PX + 6)}
    m = NymMidReject("UP")
    assert len(_run(m, _day("10:40", bars=sweep))) == 1
    assert m.state()["precondition"]["open"] == PX + 20
    off = {**sweep, "09:30": (PX - 10, PX + 20.5, PX - 10.5, PX + 20)}
    m = NymMidReject("UP")
    assert _run(m, _day("10:40", bars=off)) == []
    assert m.state()["precondition"]["open"] == PX - 10
    assert m.state()["precondition"]["reason"] == "open_on_unfavourable_side"


# --------------------------------------------------------------------------- #
# the switch                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,want", [(None, nym.DEFAULT_ENABLED), ("", nym.DEFAULT_ENABLED),
                                      ("0", False), ("off", False), ("No", False),
                                      ("false", False), ("1", True), ("on", True),
                                      ("TRUE", True), ("yes", True)])
def test_env_flag(monkeypatch, raw, want):
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", None)
    if raw is None:
        monkeypatch.delenv(nym.ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(nym.ENV_FLAG, raw)
    assert nym.enabled() is want


def test_module_override_beats_the_environment(monkeypatch):
    monkeypatch.setenv(nym.ENV_FLAG, "1")
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", False)
    assert nym.enabled() is False
    monkeypatch.setenv(nym.ENV_FLAG, "0")
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", True)
    assert nym.enabled() is True


def test_default_is_on_for_now():
    """The operator confirms the default at commit time; flip `DEFAULT_ENABLED`."""
    assert nym.DEFAULT_ENABLED is True


def test_planner_arms_it_only_when_enabled(monkeypatch):
    from agent.trader.planner import derive_plan
    thesis = {"thesis_id": "t", "bias": "UP", "dol": {"level": "x", "price": 1e5},
              "falsified_if": []}
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", True)
    assert "nym_mid_reject" in derive_plan(thesis, [], _ts("09:21"))["armed_classes"]
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", False)
    assert "nym_mid_reject" not in derive_plan(thesis, [], _ts("09:21"))["armed_classes"]


# --------------------------------------------------------------------------- #
# the shared-bar rule                                                          #
# --------------------------------------------------------------------------- #

def test_shared_bar_merges_into_one_nym_fire_with_the_bigger_stop():
    t = _ts("09:36:00")
    nymf = {"time": t, "price": PX + 6, "stop": PX - 14, "direction": "UP",
            "level": PX, "mechanism": "nym_mid_reject"}
    tmso = {"time": t, "price": PX + 6, "stop": PX - 5, "direction": "UP",
            "level": PX + 1, "mechanism": "tmso_reject"}
    out = MarketMechanisms.merge_shared_bar(
        [("extreme_reject_close", None), ("tmso_reject", tmso), ("nym_mid_reject", nymf)])
    assert dict(out)["tmso_reject"] is None
    merged = dict(out)["nym_mid_reject"]
    assert merged["stop"] == PX - 14 and merged["mechanism"] == "nym_mid_reject"
    assert merged["also_fired"] == [{"mechanism": "tmso_reject", "price": PX + 6,
                                     "stop": PX - 5, "level": PX + 1}]
    # tmso's stop farther (a hypothetical wider tmso): the bigger one still wins.
    wide = dict(tmso, stop=PX - 30)
    out = MarketMechanisms.merge_shared_bar([("tmso_reject", wide), ("nym_mid_reject", nymf)])
    assert dict(out)["nym_mid_reject"]["stop"] == PX - 30
    # One side alone, or a tmso RETRY, is left as it is.
    alone = [("tmso_reject", None), ("nym_mid_reject", nymf)]
    assert MarketMechanisms.merge_shared_bar(alone) == alone
    retry = [("tmso_reject", dict(tmso, retry_of={})), ("nym_mid_reject", nymf)]
    assert MarketMechanisms.merge_shared_bar(retry) == retry


# --------------------------------------------------------------------------- #
# the Executor                                                                 #
# --------------------------------------------------------------------------- #

def _records(tmp_path, *kinds):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]
    return [r for r in rows if r["kind"] in kinds]


def _executor(tmp_path, monkeypatch, *, on=True, direction="UP"):
    monkeypatch.setattr(nym, "NYM_MID_REJECT_ENABLED", on)
    plan = {"plan_id": "o7", "thesis_id": "t", "direction": direction,
            "dol": {"level": "far", "price": 1e5 if direction == "UP" else 1.0},
            "valid_while": [], "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=_ts("09:21:00"), ticker="MNQ")
    monkeypatch.setattr(executor_mod, "select_target", lambda *a, **k: None)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", False)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", False)
    return ex


def _drive(ex, bars, steps):
    """Hand the Executor the MNQ/MES frames truncated at each `now` (the in-progress
    minute included, as the bar loop does). MES is flat, so the SMT-wait block and the
    MES sweep stop stay inert."""
    for hms in steps:
        now = _ts(hms)
        mnq = _day(now.floor("1min").strftime("%H:%M"), bars=bars)
        mes = _day(now.floor("1min").strftime("%H:%M"), base=MES_PX,
                   bars={"07:00": (MES_PX,) * 4, "08:00": (MES_PX,) * 4})
        mes[["Open", "High", "Low", "Close"]] = MES_PX
        ex.on_bar(now, {"MNQ": mnq, "MES": mes})


# TMSO (the 09:22:30 Q2 open = the 09:23 bar's open on a 1m frame) is set to PX+40, above
# every post-open print, so an UP `tmso_reject` (needs a close back ABOVE it) cannot fire
# in the single-mechanism tests.
TMSO_HIGH = {"09:23": (PX + 40, PX + 40, PX + 39.5, PX + 39.5)}
STEPS = [f"09:{m:02d}:00" for m in range(30, 40)]


def test_executor_fires_fills_records_and_latches(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    _drive(ex, {**TMSO_HIGH, **UP_SELF}, STEPS)
    fills = _records(tmp_path, "fill")
    assert len(fills) == 1
    f = fills[0]
    assert f["mechanism"] == "nym_mid_reject" and f["time"] == _ts("09:36:00").isoformat()
    assert f["price"] == PX + 6 and f["stop"] == PX + 6 - nym.STOP_PTS
    assert f["nym_mid"] == {"level": PX, "confirm": "sweep_bar",
                            "bar": _ts("09:35").isoformat()}
    assert "also_fired" not in f
    assert ex._market.state()["nym_mid"]["filled"] is True


def test_executor_flag_off_is_inert(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, on=False)
    _drive(ex, {**TMSO_HIGH, **UP_SELF}, STEPS)
    assert _records(tmp_path, "fill") == []
    assert ex._market.state()["nym_mid"]["filled"] is False


def test_executor_no_refire_after_its_stop_out_and_the_attempt_is_spent(tmp_path, monkeypatch):
    stop_bar = {"09:37": (PX + 6, PX + 7, PX - 20, PX - 18)}         # takes PX-14
    again = {"09:45": (PX + 1, PX + 9, PX - 5, PX + 6)}              # a second valid sweep
    ex = _executor(tmp_path, monkeypatch)
    steps = STEPS[:7] + ["09:37:30"] + [f"09:{m:02d}:00" for m in range(38, 50)]
    _drive(ex, {**TMSO_HIGH, **UP_SELF, **stop_bar, **again}, steps)
    assert [r["mechanism"] for r in _records(tmp_path, "fill")] == ["nym_mid_reject"]
    so = _records(tmp_path, "stop_out")
    assert len(so) == 1 and so[0]["price"] == PX - 14
    assert int(ex._plan.get("attempts_used") or 0) == 1


def test_shared_bar_with_tmso_one_fill_bigger_stop_both_latches(tmp_path, monkeypatch):
    """TMSO = PX (the 09:22 bar opens at PX). 09:35 sweeps it and the mid and closes back
    green: `tmso_reject` fires with stop max(wick PX-5, PX+6-15) = PX-5, `nym_mid_reject`
    with PX-14. One fill, nym's, stop PX-14, tmso recorded as also fired."""
    ex = _executor(tmp_path, monkeypatch)
    _drive(ex, UP_SELF, STEPS)
    fills = _records(tmp_path, "fill")
    assert len(fills) == 1
    f = fills[0]
    assert f["mechanism"] == "nym_mid_reject" and f["stop"] == PX - 14
    assert f["also_fired"] == [{"mechanism": "tmso_reject", "price": PX + 6,
                                "stop": PX - 5, "level": PX}]
    st = ex._market.state()
    assert st["nym_mid"]["filled"] is True
    assert st["tmso"]["fired_sessions"] == [str(_ts("09:00"))]


def test_without_nym_the_same_bar_is_tmsos_own_fill(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch, on=False)
    _drive(ex, UP_SELF, STEPS)
    fills = _records(tmp_path, "fill")
    assert [(r["mechanism"], r["stop"]) for r in fills] == [("tmso_reject", PX - 5)]


# --------------------------------------------------------------------------- #
# the motivating day, over the real tape                                       #
# --------------------------------------------------------------------------- #

def test_1006_real_tape_fires_long_0937_at_31489_50():
    """§11.9's motivating day: 09:35 wicks to 31478.50 under the mid 31480.00, 09:36
    L 31457.75 C 31489.50 > O 31482.50 and > mid -> long 09:37:00 @ 31489.50, stop
    31469.50. Figures read from registry key `o7-1006`."""
    case = nc.by_key("o7-1006")
    try:
        from backtest_smt import _main_dir_for_date
        mnq = pd.read_parquet(_main_dir_for_date(case.date) / "MNQ_1m.parquet")
        day = mnq.loc[case.date]
    except Exception:
        pytest.skip("2026-10-06 main 1m parquet not available in this environment")
    if not len(day) or day.index.min() > _ts("06:00", case.date) \
            or day.index.max() < _ts("09:40", case.date):
        pytest.skip("2026-10-06 1m bars do not cover 06:00-09:40 in this environment")
    m = NymMidReject("UP")
    fire = None
    for label, row in day.between_time("09:30", "10:29").iterrows():
        now = label + pd.Timedelta(minutes=1)
        fire = m.on_bar_close(now, row, mnq[mnq.index < now])
        if fire is not None:
            break
    assert fire is not None
    assert fire["time"] == _ts(case.entry_time, case.date)
    assert fire["price"] == pytest.approx(case.entry_price)
    assert fire["stop"] == pytest.approx(case.stop)
    assert fire["level"] == pytest.approx(31480.00) and fire["confirm"] == "next_bar"
