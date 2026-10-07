"""§2's extension veto (operator, 2026-09-29) wired into the Executor.

Same style as `test_executor_micro_smt.py`: the REAL `Executor.on_bar` over synthetic
frames, with the market mechanisms stubbed so a fire can be ordered up at an exact second
and price. What is under test is the Executor's own guard: the anchor (the post-09:30
counter-extreme, in bar time), the strict `> EXTENSION_MAX_PTS` rule, the filter running
BEFORE `pick`, the record, and the flag.

Every bar-close scenario SEEDS one call first (`bar_closed` needs a minute rollover).
The 2026-09-29 prices are the motivating session's own (`l2-mechanisms.md` §2).
"""
import json

import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
import agent.trader.tmso_reject as tmso_reject
from agent.trader import executor as executor_mod
from agent.trader.executor import (EXTENSION_MAX_PTS, EXTENSION_VETO_ENV_FLAG, Executor,
                                   extension_veto_enabled)
from agent.trader.market_mechanisms import post_open_counter_extreme
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-29"
MECHANISMS = ("fvg_1m_post_extreme", "extreme_reject_close", "tmso_reject",
              "fvg_1h_reject", "micro_smt_reject")
HIGH_0929 = ("09:30:02", 30725.0, None)         # the 09:30 bar's top wick


def _ts(hms, date=DATE):
    return pd.Timestamp(f"{date} {hms}", tz=TZ)


def _records(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _vetoes(tmp_path):
    return [r for r in _records(tmp_path)
            if r["kind"] == "veto" and r.get("reason") == "extension"]


def _fills(tmp_path):
    return [r for r in _records(tmp_path) if r["kind"] == "fill"]


def _frames(now, px, marks=(), *, date=DATE, extra=None):
    """Flat 1m bars from 09:00 to the in-progress minute, plus one row per mark
    `(hms, high, low)` carrying that extreme. `extra` is appended as-is (another day)."""
    idx = pd.date_range(_ts("09:00:00", date), now.floor("1min"), freq="1min")
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                       "Volume": 1.0}, index=idx)
    for hms, high, low in marks:
        df.loc[_ts(hms, date)] = [px, high if high is not None else px + 0.5,
                                  low if low is not None else px - 0.5, px, 1.0]
    if extra is not None:
        df = pd.concat([extra, df])
    df = df.sort_index()
    return {"MNQ": df, "MES": df.copy()}


class StubMarket:
    """`MarketMechanisms` with every machine stubbed. `fires[mechanism]` is handed over
    once, the next time that machine is asked on a bar close; `tick_fire` the next time
    §6's tick path is. `pick` takes the first fire it is GIVEN, in the bar loop's own
    order, so a fire filtered out before `pick` can never be the one entered."""

    def __init__(self):
        self.fires = {}
        self.tick_fire = None
        self.picked_from = []

    def _take(self, mechanism):
        return self.fires.pop(mechanism, None)

    def pick(self, fires):
        live = [f for _m, f in fires if f is not None]
        self.picked_from.append([f["mechanism"] for f in live])
        return live[0] if live else None

    def seed_sec7(self, *a, **k): pass
    def sync_episodes(self, *a, **k): pass
    def reset_cycles(self): pass

    def sec6_on_tick(self, *a, **k):
        f, self.tick_fire = self.tick_fire, None
        return f

    def sec6_on_bar_close(self, *a, **k): return self._take("fvg_1m_post_extreme")
    def sec7_on_bar_close(self, *a, **k): return self._take("extreme_reject_close")
    def tmso_on_bar_close(self, *a, **k): return self._take("tmso_reject")
    def fvg1h_on_bar_close(self, *a, **k): return self._take("fvg_1h_reject")
    def nym_on_bar_close(self, *a, **k): return self._take("nym_mid_reject")
    def latch_nym(self): pass

    @staticmethod
    def merge_shared_bar(fires):
        from agent.trader.market_mechanisms import MarketMechanisms
        return MarketMechanisms.merge_shared_bar(fires)

    def micro_smt_entry_on_bar_close(self, *a, **k): return self._take("micro_smt_reject")
    def micro_smt_exit_on_bar_close(self, *a, **k): return None
    # O1 (§7c) inert here: `test_executor_stop_bar_retry.py` covers the retry.
    def stop_bar_retry_enabled(self, mechanism): return False
    def stop_bar_retry_due(self, now): return False
    def drop_stop_bar_retry(self): return None


def _fire(hms, price, *, mechanism="fvg_1m_post_extreme", direction="DOWN", gap_id=None,
          risk=15.0, date=DATE):
    stop = price + risk if direction == "DOWN" else price - risk
    fire = {"mechanism": mechanism, "direction": direction, "price": price, "stop": stop,
            "time": _ts(hms, date)}
    if gap_id is not None:
        fire["gap_id"] = gap_id
    return fire


def make_executor(tmp_path, monkeypatch, *, direction="DOWN", date=DATE, flag=None):
    if flag is None:
        monkeypatch.delenv(EXTENSION_VETO_ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_VETO_ENV_FLAG, flag)
    plan = {"plan_id": "ext", "thesis_id": "t", "direction": direction,
            "dol": {"level": "far", "price": 1.0 if direction == "DOWN" else 99999.0},
            "valid_while": [], "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=_ts("09:21:00", date), ticker="MNQ")
    ex._market = StubMarket()
    monkeypatch.setattr(executor_mod, "select_target", lambda *a, **k: None)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", False)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    return ex


def _step(ex, hms, px, marks=(HIGH_0929,), *, date=DATE, extra=None):
    now = _ts(hms, date)
    ex.on_bar(now, _frames(now, px, marks, date=date, extra=extra))
    return now


def _fire_at(ex, hms, price, *, px=None, marks=(HIGH_0929,), **kw):
    """Seed the previous minute, queue one fire, and close the bar at `hms`."""
    px = price if px is None else px
    prev = (_ts(hms) - pd.Timedelta(minutes=1)).strftime("%H:%M:%S")
    _step(ex, prev, px, marks)
    fire = _fire(hms, price, **kw)
    ex._market.fires[fire["mechanism"]] = fire
    return _step(ex, hms, px, marks)


# --------------------------------------------------------------------------- #
# the knob and the flag                                                         #
# --------------------------------------------------------------------------- #

def test_the_knob_sits_behind_a_named_constant():
    assert EXTENSION_MAX_PTS == 100.0
    assert EXTENSION_VETO_ENV_FLAG == "ACT_EXTENSION_VETO"


@pytest.mark.parametrize("raw,expected", [
    (None, True), ("", True), ("1", True), ("true", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), (" OFF ", False)])
def test_flag_parsing_is_on_by_default_and_off_on_the_four_words(monkeypatch, raw,
                                                                  expected):
    if raw is None:
        monkeypatch.delenv(EXTENSION_VETO_ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_VETO_ENV_FLAG, raw)
    assert extension_veto_enabled() is expected


# --------------------------------------------------------------------------- #
# 1-2. the 2026-09-29 fixture                                                   #
# --------------------------------------------------------------------------- #

def test_down_plan_173_pts_from_the_post_open_high_is_vetoed_with_full_detail(
        tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30552.0, gap_id="9d8e0536d8df")
    assert ex.position() is None
    assert _fills(tmp_path) == []
    vetoes = _vetoes(tmp_path)
    assert len(vetoes) == 1
    v = vetoes[0]
    assert v["mechanism"] == "fvg_1m_post_extreme"
    assert v["artifact_id"] == "9d8e0536d8df"
    assert "artifact_label" in v
    assert v["time"] == "2026-09-29T09:44:00-04:00"
    assert v["detail"] == {"distance": 173.0, "cap": 100.0, "anchor": 30725.0,
                           "anchor_ts": "2026-09-29T09:30:02-04:00", "price": 30552.0}


def test_down_plan_49_25_pts_from_the_post_open_high_is_allowed(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:31:00", 30675.75, mechanism="micro_smt_reject")
    assert ex.position() is not None
    assert ex.position()["direction"] == "DOWN"
    assert [f["price"] for f in _fills(tmp_path)] == [30675.75]
    assert _vetoes(tmp_path) == []


# --------------------------------------------------------------------------- #
# 3. the boundary is STRICT                                                     #
# --------------------------------------------------------------------------- #

def test_exactly_100_pts_is_allowed(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30625.0)
    assert ex.position() is not None
    assert _vetoes(tmp_path) == []


def test_100_25_pts_is_vetoed(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30624.75)
    assert ex.position() is None
    assert [v["detail"]["distance"] for v in _vetoes(tmp_path)] == [100.25]


# --------------------------------------------------------------------------- #
# 4. UP mirror                                                                  #
# --------------------------------------------------------------------------- #

def test_up_plan_measures_from_the_lowest_low_since_0930(tmp_path, monkeypatch):
    low = (("09:30:05", None, 30400.0),)
    ex = make_executor(tmp_path, monkeypatch, direction="UP")
    _fire_at(ex, "09:44:00", 30520.0, direction="UP", marks=low, gap_id="g-far")
    assert ex.position() is None
    v = _vetoes(tmp_path)
    assert len(v) == 1
    assert v[0]["detail"] == {"distance": 120.0, "cap": 100.0, "anchor": 30400.0,
                              "anchor_ts": "2026-09-29T09:30:05-04:00", "price": 30520.0}

    _fire_at(ex, "09:46:00", 30500.0, direction="UP", marks=low, gap_id="g-near")
    assert ex.position() is not None, "exactly 100.0 above the low is allowed"
    assert ex.position()["direction"] == "UP"


# --------------------------------------------------------------------------- #
# 5. inert before any bar at or after 09:30                                     #
# --------------------------------------------------------------------------- #

def test_no_bar_at_or_after_0930_means_no_anchor_and_the_veto_allows(tmp_path,
                                                                      monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    now = _ts("09:29:30")
    mnq = _frames(now, 30900.0, marks=(("09:10:00", 31500.0, None),))["MNQ"]
    assert post_open_counter_extreme(mnq, "DOWN", _ts("09:30:00")) == (None, None)
    assert ex._extension_allows(now, mnq, _fire("09:29:30", 30552.0)) is True
    assert _vetoes(tmp_path) == []


def test_bars_before_0930_never_enter_the_anchor(tmp_path, monkeypatch):
    """A pre-open high 775 pts above the fire is not the anchor: the 09:30 wick is."""
    ex = make_executor(tmp_path, monkeypatch)
    marks = (("09:10:00", 31400.0, None), HIGH_0929)
    _fire_at(ex, "09:44:00", 30630.0, marks=marks)
    assert ex.position() is not None
    assert _vetoes(tmp_path) == []


# --------------------------------------------------------------------------- #
# 6-7. the anchor ratchets; a retrace re-allows                                 #
# --------------------------------------------------------------------------- #

def test_a_later_higher_high_raises_the_anchor_and_the_distance_is_remeasured(
        tmp_path, monkeypatch):
    first = (("09:30:02", 30700.0, None),)
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:36:00", 30595.0, marks=first, gap_id="g1")
    later = first + (("09:40:10", 30740.0, None),)
    # 65 pts from the old anchor (it would pass), 105 from the new one.
    _fire_at(ex, "09:44:00", 30635.0, marks=later, gap_id="g2")
    assert ex.position() is None
    v = _vetoes(tmp_path)
    assert [(r["artifact_id"], r["detail"]["anchor"], r["detail"]["distance"])
            for r in v] == [("g1", 30700.0, 105.0), ("g2", 30740.0, 105.0)]
    assert v[1]["detail"]["anchor_ts"] == "2026-09-29T09:40:10-04:00"


def test_entries_are_re_allowed_once_price_retraces_inside_the_cap(tmp_path,
                                                                   monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30552.0, gap_id="g1")
    assert ex.position() is None
    _fire_at(ex, "09:54:00", 30628.0, gap_id="g2")            # 97.00 from 30725.00
    assert ex.position() is not None
    assert [f["price"] for f in _fills(tmp_path)] == [30628.0]
    assert len(_vetoes(tmp_path)) == 1


# --------------------------------------------------------------------------- #
# 8. each fire is filtered BEFORE pick                                          #
# --------------------------------------------------------------------------- #

def test_a_vetoed_fire_cannot_mask_an_allowed_fire_on_the_same_bar(tmp_path,
                                                                   monkeypatch):
    """`extreme_reject_close` is asked first, so a filter AFTER `pick` would take it,
    veto it, and drop `tmso_reject`'s allowed fire with it."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:43:00", 30650.0)
    ex._market.fires["extreme_reject_close"] = _fire(
        "09:44:00", 30552.0, mechanism="extreme_reject_close")
    ex._market.fires["tmso_reject"] = _fire("09:44:00", 30650.0, mechanism="tmso_reject")
    _step(ex, "09:44:00", 30650.0)
    assert ex._market.picked_from[-1] == ["tmso_reject"]
    assert ex.position() is not None
    fills = _fills(tmp_path)
    assert [(f["mechanism"], f["price"]) for f in fills] == [("tmso_reject", 30650.0)]
    assert [v["mechanism"] for v in _vetoes(tmp_path)] == ["extreme_reject_close"]


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_all_five_market_mechanisms_are_covered(tmp_path, monkeypatch, mechanism):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30552.0, mechanism=mechanism)
    assert ex.position() is None
    v = _vetoes(tmp_path)
    assert [(r["mechanism"], r["artifact_id"]) for r in v] == [(mechanism, mechanism)]


def test_the_tick_path_is_covered_too(tmp_path, monkeypatch):
    """§6's exit-tick / early-runaway fires arrive between bar closes."""
    ex = make_executor(tmp_path, monkeypatch)
    _step(ex, "09:44:00", 30552.0)
    ex._market.tick_fire = _fire("09:44:20", 30552.0, gap_id="g-tick")
    _step(ex, "09:44:20", 30552.0)
    assert ex.position() is None
    assert [v["artifact_id"] for v in _vetoes(tmp_path)] == ["g-tick"]


# --------------------------------------------------------------------------- #
# 9. a veto costs nothing                                                       #
# --------------------------------------------------------------------------- #

def test_a_veto_spends_no_attempt_and_leaves_the_plan_alive(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30552.0)
    assert ex.attempts_used() == 0
    assert ex.bind_state()["plan_alive"] is True
    assert ex.bind_state()["dead_reason"] is None
    assert ex.bind_state().get("market_mech_error") is None
    assert "plan_dead" not in [r["kind"] for r in _records(tmp_path)]


# --------------------------------------------------------------------------- #
# 10. what resets the anchor, and what does not                                 #
# --------------------------------------------------------------------------- #

def test_a_stop_out_and_its_cooldown_do_not_reset_the_anchor(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:31:00", 30675.75, mechanism="micro_smt_reject")
    assert ex.position() is not None
    _step(ex, "09:32:00", 30700.0)                     # short stopped at 30690.75
    assert ex.position() is None
    assert ex.attempts_used() == 1
    _fire_at(ex, "09:44:00", 30552.0, gap_id="g1")
    assert ex.position() is None
    v = _vetoes(tmp_path)
    assert len(v) == 1
    assert v[0]["detail"]["anchor"] == 30725.0
    assert v[0]["detail"]["anchor_ts"] == "2026-09-29T09:30:02-04:00"
    assert ex.attempts_used() == 1


def test_a_new_session_date_starts_a_new_anchor(tmp_path, monkeypatch):
    """Yesterday's 30725.00 is still in the frame; today's plan measures from today's
    own post-09:30 high."""
    nxt = "2026-09-30"
    yesterday = _frames(_ts("12:59:00"), 30600.0, (HIGH_0929,))["MNQ"]
    ex = make_executor(tmp_path, monkeypatch, date=nxt)
    marks = (("09:30:02", 30600.0, None),)
    _step(ex, "09:43:00", 30552.0, marks, date=nxt, extra=yesterday)
    ex._market.fires["fvg_1m_post_extreme"] = _fire("09:44:00", 30552.0, date=nxt)
    _step(ex, "09:44:00", 30552.0, marks, date=nxt, extra=yesterday)
    assert ex.position() is not None, "48.00 from today's 30600.00, not 173.00"
    assert _vetoes(tmp_path) == []


# --------------------------------------------------------------------------- #
# 11. flag off                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_flag_off_the_extended_fire_enters_and_nothing_is_recorded(tmp_path,
                                                                   monkeypatch, raw):
    ex = make_executor(tmp_path, monkeypatch, flag=raw)
    _fire_at(ex, "09:44:00", 30552.0, gap_id="9d8e0536d8df")
    assert ex.position() is not None
    assert [f["price"] for f in _fills(tmp_path)] == [30552.0]
    assert [r for r in _records(tmp_path) if r["kind"] == "veto"] == []


def test_flag_off_does_not_filter_before_pick(tmp_path, monkeypatch):
    """Flag off is the pre-change bar loop: `pick` sees every fire, in order."""
    ex = make_executor(tmp_path, monkeypatch, flag="0")
    _step(ex, "09:43:00", 30650.0)
    ex._market.fires["extreme_reject_close"] = _fire(
        "09:44:00", 30552.0, mechanism="extreme_reject_close")
    ex._market.fires["tmso_reject"] = _fire("09:44:00", 30650.0, mechanism="tmso_reject")
    _step(ex, "09:44:00", 30650.0)
    assert ex._market.picked_from[-1] == ["extreme_reject_close", "tmso_reject"]
    assert [f["mechanism"] for f in _fills(tmp_path)] == ["extreme_reject_close"]


# --------------------------------------------------------------------------- #
# 12. dedupe                                                                    #
# --------------------------------------------------------------------------- #

def test_repeated_vetoes_record_once_per_mechanism_artifact_reason(tmp_path,
                                                                   monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _fire_at(ex, "09:44:00", 30552.0, gap_id="g1")
    _fire_at(ex, "09:46:00", 30560.0, gap_id="g1")             # same gap: no new record
    _fire_at(ex, "09:48:00", 30560.0, gap_id="g2")             # another gap: recorded
    _fire_at(ex, "09:50:00", 30560.0, gap_id="g1", mechanism="tmso_reject")
    assert ex.position() is None
    assert [(v["mechanism"], v["artifact_id"]) for v in _vetoes(tmp_path)] == [
        ("fvg_1m_post_extreme", "g1"), ("fvg_1m_post_extreme", "g2"),
        ("tmso_reject", "g1")]


# --------------------------------------------------------------------------- #
# the real 2026-09-29 tape (skips cleanly if it is missing)                     #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
@pytest.mark.timeout(900)
def test_0929_real_tape_the_0944_short_is_vetoed(tmp_path, monkeypatch):
    """`sec2-0929-extension` end to end: a replay of 2026-09-29 under the thesis the live
    session recorded takes the 09:31:00 short and refuses the 09:44:00 one. Asserted up
    to 09:44:00 only — the tape may still grow past where it ended when this was
    written.

    Re-scoped 2026-09-30 for §7c's stop-bar retry: the veto is proved with BOTH retry
    constants off (the pre-O1 configuration the figures were measured under), and a
    second replay with the retry ON documents the interaction — the 09:31 stop-out's
    retry (09:32:00 @ 30661.25) is still open at 09:44, so the 09:44:00 fire is never
    produced and there is nothing to veto (`sec7c-0929-retry`)."""
    import os
    from agent.trader import named_cases as nc
    from agent.trader.replay import run_replay

    case = nc.by_key("sec2-0929-extension")
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "thesis_cache"))
    monkeypatch.delenv("ACT_TRADER_5M", raising=False)
    monkeypatch.delenv(EXTENSION_VETO_ENV_FLAG, raising=False)

    def _replay(retry):
        monkeypatch.setattr(micro_smt, "STOP_BAR_RETRY", retry)
        monkeypatch.setattr(tmso_reject, "STOP_BAR_RETRY", retry)
        try:
            res = run_replay([case.date], allow_calls=False,
                             thesis=nc.thesis_for(case.key))[case.date]
        except Exception as exc:                    # no 09-29 tape in this environment
            pytest.skip(f"2026-09-29 replay unavailable: {type(exc).__name__}: {exc}")
        rows = [json.loads(line) for line in open(
            os.path.join(res["run_dir"], "trader_decisions.jsonl"), encoding="utf-8")
            if line.strip()]
        return [r for r in rows if r["time"] <= "2026-09-29T09:44:00-04:00"]

    upto = _replay(retry=False)
    fills = [r for r in upto if r.get("kind") == "fill"]
    assert [(f["time"], f["mechanism"], f["price"]) for f in fills] == [
        ("2026-09-29T09:31:00-04:00", "micro_smt_reject", 30675.75)]
    vetoes = [r for r in upto if r.get("kind") == "veto"
              and r.get("reason") == "extension"]
    assert [(v["time"], v["mechanism"]) for v in vetoes] == [
        ("2026-09-29T09:44:00-04:00", case.mechanism)]
    # `anchor_ts` is the label of the bar the Executor holds. Replay hands it completed
    # 1m bars, so the 09:30:02 print is reported as the 09:30 bar.
    assert vetoes[0]["detail"] == {
        "distance": 173.0, "cap": 100.0, "anchor": 30725.0,
        "anchor_ts": "2026-09-29T09:30:00-04:00", "price": 30552.0}

    # With the retry ON (the default): the 09:32:00 retry is open at 09:44, so no fire
    # reaches the veto — no veto record, no fill; only the retry's own initial-target
    # flip lands on that bar.
    upto = _replay(retry=True)
    fills = [r for r in upto if r.get("kind") == "fill"]
    assert [(f["time"], f["price"], "retry_of" in f) for f in fills] == [
        ("2026-09-29T09:31:00-04:00", 30675.75, False),
        ("2026-09-29T09:32:00-04:00", 30661.25, True)]
    assert not [r for r in upto if r.get("kind") == "veto"]
    assert [r["kind"] for r in upto if r["time"].startswith("2026-09-29T09:44")] == [
        "initial_target_reached"]
