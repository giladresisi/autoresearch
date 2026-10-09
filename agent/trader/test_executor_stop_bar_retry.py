"""O1 (`l2-mechanisms.md` §7c): the stop-bar retry wired into the Executor.

Unlike `test_executor_extension_veto.py` the market mechanisms are NOT stubbed: the REAL
`Executor.on_bar` drives the REAL `MarketMechanisms` (a real `TmsoReject` and a real
`MicroSmt`) over synthetic 1m frames from 07:00, so what fires is what the machines
themselves produce and what is asserted is the Executor's gating around the retry:

  * it is judged at bar B's close -- the first instant after §2's cooldown -- and enters
    through the ordinary fire -> filter -> `pick` -> `_enter_by_market` path;
  * it passes the extension veto, the attempt budget, the spine gates (10:30 cutoff) and,
    for O3, its own windows;
  * it spends an attempt through its own stop-out exactly like any entry, and NEVER
    chains;
  * every fate is recorded: `stop_bar_retry_armed` at the stop-out, then either a `fill`
    stamped `retry_of` or one `stop_bar_retry_skipped` with its reason;
  * the constants False write nothing new and enter nothing new.

Frames: flat at PX from 07:00; TMSO (the Q2 open, 09:22:30) is therefore PX. The O3
pre-open pair's previous extremes over 07:30-09:00 are PX + 0.5 (MNQ) / MES_PX + 0.5.
Bar B's shape is chosen so ONLY the mechanism under test reacts (see each helper).
"""
import json

import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
import agent.trader.tmso_reject as tmso_reject
from agent.trader import executor as executor_mod
from agent.trader.executor import EXTENSION_MAX_PTS, EXTENSION_VETO_ENV_FLAG, Executor
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-29"
PX = 30000.0
MES_PX = 7700.0


@pytest.fixture(autouse=True)
def _strict_tmso_close_back(monkeypatch):
    """The flat tape sits ON TMSO, so the §11.10 close-back tolerance would arm the flat
    09:30 bar a bar early; these tests are about the retry, so they read the strict test."""
    monkeypatch.setattr(tmso_reject, "CLOSE_BACK_TOLERANCE_PTS", 0.0)


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _records(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _kind(tmp_path, *kinds):
    return [r for r in _records(tmp_path) if r["kind"] in kinds]


def _frame(now, px, bars):
    """Flat 1m bars from 07:00 to the in-progress minute, with `bars`
    {"HH:MM": (O, H, L, C)} overriding whole rows. The in-progress row's High/Low are
    what `_drive_orders` tests the stop against at `now`."""
    idx = pd.date_range(_ts("07:00:00"), now.floor("1min"), freq="1min")
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                       "Volume": 1.0}, index=idx)
    for hm, (o, h, l, c) in bars.items():
        ts = _ts(hm + ":00")
        if ts in df.index:
            df.loc[ts] = [o, h, l, c, 1.0]
    return df


def make_executor(tmp_path, monkeypatch, *, veto_flag=None):
    if veto_flag is None:
        monkeypatch.delenv(EXTENSION_VETO_ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_VETO_ENV_FLAG, veto_flag)
    plan = {"plan_id": "o1", "thesis_id": "t", "direction": "DOWN",
            "dol": {"level": "far", "price": 1.0}, "valid_while": [],
            "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=_ts("09:21:00"), ticker="MNQ")
    monkeypatch.setattr(executor_mod, "select_target", lambda *a, **k: None)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", False)
    return ex


class Tape:
    """A scripted day: the MNQ / MES bar overrides accumulate, and every `step` hands
    the Executor both frames truncated at `now`."""

    def __init__(self, ex, mnq_bars=None, mes_bars=None):
        self.ex = ex
        self.mnq = dict(mnq_bars or {})
        self.mes = dict(mes_bars or {})

    def step(self, hms):
        now = _ts(hms)
        self.ex.on_bar(now, {"MNQ": _frame(now, PX, self.mnq),
                             "MES": _frame(now, MES_PX, self.mes)})
        return now


# --------------------------------------------------------------------------- #
# tmso_reject                                                                  #
# --------------------------------------------------------------------------- #
# 09:31 sweeps TMSO (H PX+5), closes back under it (C PX-5) and red -> fires at 09:32:00
# @ PX-5, stop min(PX+5, PX+10) = PX+5. The 09:33 row's High PX+6 takes that stop at
# the 09:33:14 call. O3 stays inert: MES mirrors MNQ, so both assets break and the
# divergence is cancelled.

TMSO_SWEEP = {"09:31": (PX + 2, PX + 5, PX - 10, PX - 5)}
B_FAV = (PX + 1, PX + 6, PX - 8, PX - 6)               # red: D1 holds
B_ADV = (PX - 4, PX + 6, PX - 8, PX + 3)               # green: D1 fails


def _tmso_day(tmp_path, monkeypatch, b_bar, **kw):
    ex = make_executor(tmp_path, monkeypatch, **kw)
    tape = Tape(ex, TMSO_SWEEP, {"09:31": (MES_PX + 2, MES_PX + 5, MES_PX - 10, MES_PX - 5)})
    tape.step("09:30:00")
    tape.step("09:31:00")
    tape.step("09:32:00")                                # the sweep bar completes: fire
    fills = _kind(tmp_path, "fill")
    assert len(fills) == 1 and fills[0]["mechanism"] == "tmso_reject"
    assert fills[0]["time"] == _ts("09:32:00").isoformat() and fills[0]["price"] == PX - 5
    tape.mnq["09:33"] = b_bar
    tape.mes["09:33"] = (MES_PX + 1, MES_PX + 6, MES_PX - 8, MES_PX - 6)
    tape.step("09:33:14")                                # stop-out at PX+5
    so = _kind(tmp_path, "stop_out")
    assert len(so) == 1 and so[0]["time"] == _ts("09:33:14").isoformat()
    assert so[0]["price"] == PX + 5
    return ex, tape


def test_tmso_retry_fires_at_bar_bs_close_and_is_stamped_retry_of(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, B_FAV)
    armed = _kind(tmp_path, "stop_bar_retry_armed")
    assert len(armed) == 1 and armed[0]["mechanism"] == "tmso_reject"
    assert armed[0]["time"] == _ts("09:33:14").isoformat()
    assert armed[0]["detail"] == {"stop_out_time": _ts("09:33:14").isoformat(),
                                  "entry": PX - 5, "bar": _ts("09:33:00").isoformat(),
                                  "retry_at": _ts("09:34:00").isoformat()}
    tape.step("09:34:00")                                # bar B completes: the retry
    fills = _kind(tmp_path, "fill")
    assert len(fills) == 2
    retry = fills[1]
    assert retry["mechanism"] == "tmso_reject" and retry["direction"] == "DOWN"
    assert retry["time"] == _ts("09:34:00").isoformat() and retry["price"] == PX - 6
    assert retry["retry_of"] == {"stop_out_time": _ts("09:33:14").isoformat(),
                                 "entry": PX - 5, "bar": _ts("09:33:00").isoformat()}
    assert "retry_of" not in fills[0]
    # tmso's own stop rule: the running swept extreme max(PX+5, PX+6) capped at 15 from
    # the retry's entry -> min(PX+6, PX+9) = PX+6.
    assert ex.position()["stop"] == PX + 6
    assert ex.position()["entry"] == PX - 6
    assert _kind(tmp_path, "stop_bar_retry_skipped") == []
    # The normal fill path ran: a target was picked for the retry too.
    assert len(_kind(tmp_path, "target_selected")) == 2
    # The budget counts stop-outs: one so far, the retry is open.
    assert ex.attempts_used() == 1


def test_tmso_no_retry_on_an_adverse_close(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, B_ADV)
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "adverse_close"
    assert skipped[0]["time"] == _ts("09:34:00").isoformat()
    assert skipped[0]["detail"]["bar"] == _ts("09:33:00").isoformat()


def test_tmso_retry_spends_an_attempt_and_never_chains(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, B_FAV)
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 2 and ex.attempts_used() == 1
    # The retry is stopped (PX+6) inside a bar that then closes red -- a favourable
    # close, which for an ORIGINAL entry would arm a retry. Not for a retry.
    tape.mnq["09:35"] = (PX + 1, PX + 8, PX - 9, PX - 7)
    tape.mes["09:35"] = (MES_PX + 1, MES_PX + 8, MES_PX - 9, MES_PX - 7)
    tape.step("09:35:20")
    so = _kind(tmp_path, "stop_out")
    assert len(so) == 2 and so[1]["time"] == _ts("09:35:20").isoformat()
    assert ex.attempts_used() == 2, "the retry spent an attempt through its own stop-out"
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "no_chain"
    assert skipped[0]["time"] == _ts("09:35:20").isoformat()
    assert len(_kind(tmp_path, "stop_bar_retry_armed")) == 1
    tape.step("09:36:00")
    tape.step("09:37:00")
    assert len(_kind(tmp_path, "fill")) == 2, "a stopped retry does not retry again"
    assert ex.position() is None


def test_tmso_retry_goes_through_the_extension_veto(tmp_path, monkeypatch):
    """Bar B's own spike puts the post-09:30 counter-extreme PX+120 above the retry's
    price PX-6: 126 > the 100-pt cap. Vetoed, recorded as a veto AND as the retry's
    fate; no attempt spent; the plan lives."""
    ex, tape = _tmso_day(tmp_path, monkeypatch, (PX + 1, PX + 120, PX - 8, PX - 6))
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    vetoes = [r for r in _kind(tmp_path, "veto") if r["reason"] == "extension"]
    assert len(vetoes) == 1 and vetoes[0]["mechanism"] == "tmso_reject"
    assert vetoes[0]["detail"]["distance"] == 126.0
    assert vetoes[0]["detail"]["cap"] == EXTENSION_MAX_PTS
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "vetoed"
    assert ex.attempts_used() == 1 and ex.bind_state()["plan_alive"]


def test_tmso_retry_with_the_veto_off_is_taken(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, (PX + 1, PX + 120, PX - 8, PX - 6),
                         veto_flag="0")
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 2 and ex.position() is not None
    # Cap binds: min(max(PX+5, PX+120), PX-6+15) = PX+9.
    assert ex.position()["stop"] == PX + 9


def test_tmso_no_retry_at_or_after_the_1030_cutoff(tmp_path, monkeypatch):
    """The sweep at 10:28 fires 10:29:00; the 10:29 bar takes the stop at 10:29:40 and
    closes red at 10:30:00 -- tmso_reject's own cutoff
    (`executor.MECHANISM_CUTOFF_ET`; the shared one is 11:00 since 2026-10-08). No retry, and the reason says so."""
    ex = make_executor(tmp_path, monkeypatch)
    tape = Tape(ex, {"10:28": (PX + 2, PX + 5, PX - 10, PX - 5)},
                {"10:28": (MES_PX + 2, MES_PX + 5, MES_PX - 10, MES_PX - 5)})
    for hms in ("10:26:00", "10:27:00", "10:28:00", "10:29:00"):
        tape.step(hms)
    assert len(_kind(tmp_path, "fill")) == 1
    tape.mnq["10:29"] = B_FAV
    tape.mes["10:29"] = (MES_PX + 1, MES_PX + 6, MES_PX - 8, MES_PX - 6)
    tape.step("10:29:40")
    assert len(_kind(tmp_path, "stop_out")) == 1
    assert len(_kind(tmp_path, "stop_bar_retry_armed")) == 1
    tape.step("10:30:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "entry_cutoff"


def test_tmso_no_retry_when_the_budget_is_spent(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, B_FAV)
    # Pretend two earlier attempts were spent: this stop-out is the third.
    assert ex.attempts_used() == 1
    ex._plan["attempts_used"] = 3
    # Re-run the arming decision as `_on_stop_out` would with the budget gone.
    ex._market.drop_stop_bar_retry()
    ex._arm_stop_bar_retry(_kind(tmp_path, "stop_out")[0] | {"time": _ts("09:33:14")})
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert skipped and skipped[-1]["reason"] == "attempts_exhausted"
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1


def test_tmso_constant_false_restores_todays_stream(tmp_path, monkeypatch):
    """With the constant off: one fill, one stop-out, NO retry record of any kind, and
    the stream is exactly the constant-on stream up to the stop-out. The A/B over real
    tape (§7c) is the byte-for-byte check against the pre-O1 recordings."""
    on_dir, off_dir = tmp_path / "on", tmp_path / "off"
    on_dir.mkdir(); off_dir.mkdir()
    _, tape = _tmso_day(on_dir, monkeypatch, B_FAV)
    tape.step("09:34:00")
    monkeypatch.setattr(tmso_reject, "STOP_BAR_RETRY", False)
    ex, tape = _tmso_day(off_dir, monkeypatch, B_FAV)
    tape.step("09:34:00")
    tape.step("09:35:00")
    assert len(_kind(off_dir, "fill")) == 1 and ex.position() is None
    assert not [r for r in _records(off_dir) if r["kind"].startswith("stop_bar_retry")]
    assert not any("retry_of" in r for r in _records(off_dir))
    on = [r for r in _records(on_dir) if not r["kind"].startswith("stop_bar_retry")]
    off = _records(off_dir)
    assert off == on[:len(off)]


def test_tmso_a_plan_dying_with_a_retry_armed_records_it(tmp_path, monkeypatch):
    ex, tape = _tmso_day(tmp_path, monkeypatch, B_FAV)
    ex.kill_plan(_ts("09:33:30"), "operator")
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "plan_dead"
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1


# --------------------------------------------------------------------------- #
# micro_smt_reject (O3, the pre-open pair)                                     #
# --------------------------------------------------------------------------- #
# 09:31: MNQ breaks its 07:30-09:00 high (H PX+5 > PX+0.5) while MES holds (H MES_PX+0.5,
# not strictly above); both close red -> O3 fires at 09:32:00 @ PX+1, stop
# min(PX+5+2, PX+1+15) = PX+7. The bar does NOT close back under TMSO (C PX+1 > PX), so
# tmso_reject never arms. The 09:33 row's High PX+8 takes the stop at 09:33:14.

O3_SWEEP_MNQ = {"09:31": (PX + 3, PX + 5, PX - 2, PX + 1)}
O3_SWEEP_MES = {"09:31": (MES_PX + 0.4, MES_PX + 0.5, MES_PX - 2, MES_PX - 1)}
O3_B_MNQ = (PX + 2, PX + 8, PX - 1, PX + 1)            # red, still closes over TMSO
O3_B_MES = (MES_PX + 0.2, MES_PX + 0.5, MES_PX - 3, MES_PX - 2)   # red, holds


def _o3_day(tmp_path, monkeypatch, mnq_b, mes_b, *, during=None):
    ex = make_executor(tmp_path, monkeypatch)
    tape = Tape(ex, O3_SWEEP_MNQ, O3_SWEEP_MES)
    tape.step("09:30:00")
    tape.step("09:31:00")
    tape.step("09:32:00")
    fills = _kind(tmp_path, "fill")
    assert len(fills) == 1 and fills[0]["mechanism"] == "micro_smt_reject"
    assert fills[0]["price"] == PX + 1 and ex.position()["stop"] == PX + 7
    if during:
        tape.mes["09:32"] = during
    tape.mnq["09:33"] = mnq_b
    tape.mes["09:33"] = mes_b
    tape.step("09:33:14")
    so = _kind(tmp_path, "stop_out")
    assert len(so) == 1 and so[0]["price"] == PX + 7
    return ex, tape


def test_o3_retry_fires_when_both_assets_close_favourable(tmp_path, monkeypatch):
    ex, tape = _o3_day(tmp_path, monkeypatch, O3_B_MNQ, O3_B_MES)
    armed = _kind(tmp_path, "stop_bar_retry_armed")
    assert len(armed) == 1 and armed[0]["mechanism"] == "micro_smt_reject"
    tape.step("09:34:00")
    fills = _kind(tmp_path, "fill")
    assert len(fills) == 2 and fills[1]["mechanism"] == "micro_smt_reject"
    assert fills[1]["time"] == _ts("09:34:00").isoformat() and fills[1]["price"] == PX + 1
    assert fills[1]["retry_of"]["entry"] == PX + 1
    # O3's own rule: MNQ's running micro-session extreme max(PX+5, PX+8) + 2 = PX+10,
    # capped at 15 from the entry: min(PX+10, PX+16) = PX+10.
    assert ex.position()["stop"] == PX + 10
    assert ex.attempts_used() == 1


def test_o3_no_retry_when_mes_closed_adverse(tmp_path, monkeypatch):
    ex, tape = _o3_day(tmp_path, monkeypatch, O3_B_MNQ,
                       (MES_PX - 3, MES_PX + 0.5, MES_PX - 3, MES_PX - 1))   # green
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "mes_adverse_close"


def test_o3_no_retry_when_the_divergence_was_cancelled_while_open(tmp_path, monkeypatch):
    """MES breaks its 07:30-09:00 high on the 09:32 bar, while the position is open and
    the machine is not being asked (§7a.1's whole-session reading)."""
    ex, tape = _o3_day(tmp_path, monkeypatch, O3_B_MNQ, O3_B_MES,
                       during=(MES_PX, MES_PX + 2, MES_PX - 1, MES_PX - 0.5))
    tape.step("09:34:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "divergence_cancelled"


def test_o3_retry_at_1030_is_the_next_micro_session_and_does_not_happen(tmp_path,
                                                                        monkeypatch):
    """O3 is exempt from the shared cutoff and 10:30:00 is inside its ordinary window,
    but the fire's micro-session (09:00-10:30) is over: no retry."""
    ex = make_executor(tmp_path, monkeypatch)
    tape = Tape(ex, {"10:28": (PX + 3, PX + 5, PX - 2, PX + 1)},
                {"10:28": (MES_PX + 0.4, MES_PX + 0.5, MES_PX - 2, MES_PX - 1)})
    for hms in ("10:26:00", "10:27:00", "10:28:00", "10:29:00"):
        tape.step(hms)
    assert len(_kind(tmp_path, "fill")) == 1
    tape.mnq["10:29"] = O3_B_MNQ
    tape.mes["10:29"] = O3_B_MES
    tape.step("10:29:40")
    assert len(_kind(tmp_path, "stop_out")) == 1
    tape.step("10:30:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    skipped = _kind(tmp_path, "stop_bar_retry_skipped")
    assert len(skipped) == 1 and skipped[0]["reason"] == "micro_session_ended"


def test_o3_constant_false_restores_todays_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(micro_smt, "STOP_BAR_RETRY", False)
    ex, tape = _o3_day(tmp_path, monkeypatch, O3_B_MNQ, O3_B_MES)
    tape.step("09:34:00")
    tape.step("09:35:00")
    assert len(_kind(tmp_path, "fill")) == 1 and ex.position() is None
    assert not [r for r in _records(tmp_path) if r["kind"].startswith("stop_bar_retry")]


def test_the_executor_still_contains_no_wall_clock():
    import inspect
    src = inspect.getsource(executor_mod)
    assert "get_et_now" not in src and "datetime.now" not in src


# --------------------------------------------------------------------------- #
# the real 2026-09-29 tape (skips cleanly if it is missing)                     #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
@pytest.mark.timeout(900)
def test_0929_real_tape_the_retry_is_taken_and_stopped_at_102557(tmp_path, monkeypatch):
    """`sec7c-0929-retry` end to end under the thesis the live session recorded: the
    09:31:00 short is stopped 09:31:14, the 09:31 bar closes red on both assets, the
    retry fires 09:32:00 @ 30661.25 with stop 30676.25 (one tick over the 09:32 high)
    and is stopped 10:25:57. Asserted up to 10:30 only -- the tape ended 10:34 when this
    was written and may still be back-filled."""
    import os
    from agent.trader import named_cases as nc
    from agent.trader.replay import run_replay

    case = nc.by_key("sec7c-0929-retry")
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "thesis_cache"))
    monkeypatch.delenv("ACT_TRADER_5M", raising=False)
    monkeypatch.delenv(EXTENSION_VETO_ENV_FLAG, raising=False)
    monkeypatch.setattr(micro_smt, "STOP_BAR_RETRY", True)
    monkeypatch.setattr(tmso_reject, "STOP_BAR_RETRY", True)
    try:
        res = run_replay([case.date], allow_calls=False,
                         thesis=nc.thesis_for(case.key))[case.date]
    except Exception as exc:                        # no 09-29 tape in this environment
        pytest.skip(f"2026-09-29 replay unavailable: {type(exc).__name__}: {exc}")
    rows = [json.loads(line) for line in open(
        os.path.join(res["run_dir"], "trader_decisions.jsonl"), encoding="utf-8")
        if line.strip()]
    upto = [r for r in rows if r["time"] <= "2026-09-29T10:30:00-04:00"]
    fills = [r for r in upto if r["kind"] == "fill"]
    assert [(f["time"], f["mechanism"], f["price"]) for f in fills] == [
        ("2026-09-29T09:31:00-04:00", case.mechanism, 30675.75),
        ("2026-09-29T09:32:00-04:00", case.mechanism, case.entry_price)]
    assert "retry_of" not in fills[0]
    assert fills[1]["retry_of"] == {"stop_out_time": "2026-09-29T09:31:14-04:00",
                                    "entry": 30675.75, "bar": "2026-09-29T09:31:00-04:00"}
    stops = [r for r in upto if r["kind"] == "stop_out"]
    assert [(s["time"], s["price"], s["entry"]) for s in stops] == [
        ("2026-09-29T09:31:14-04:00", 30690.75, 30675.75),
        ("2026-09-29T10:25:57-04:00", case.exit_price, case.entry_price)]
    assert case.stop == case.exit_price and case.pnl == -15.00
    fates = [(r["kind"], r["time"][11:19], r.get("reason")) for r in upto
             if r["kind"].startswith("stop_bar_retry")]
    assert fates == [("stop_bar_retry_armed", "09:31:14", None),
                     ("stop_bar_retry_skipped", "10:25:57", "no_chain")]
    # The 09:44:00 fire sec2-0929-extension records as VETOED is never produced: a
    # position is open. No veto, no fill, at 09:44 -- only the retry's own initial
    # target flip (`initial_target_reached`) lands on that bar.
    at_0944 = [r["kind"] for r in upto if r["time"].startswith("2026-09-29T09:44")]
    assert at_0944 == ["initial_target_reached"], at_0944
    # Attempts asserted, never scored (`test_arbiter_forward`'s convention: a fill).
    assert len(fills) == case.attempts_used
