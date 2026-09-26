"""O3 (`micro_smt_reject`) / O4 (`micro_smt_exit`) wired into the Executor.

Mirrors `test_executor_live_rules.py`'s style: the market mechanisms are stubbed so a
fire can be ordered up at an exact second, and what is under test is the Executor's OWN
gating around them (the flags, O3's own 12:30 cutoff exempting it from the shared 10:30
one, the shared attempt budget and cooldown, and O4's positive-close / attempt-spend
rules). The detector's own logic (the divergence, the confirmation, no-lookahead, the
real 2026-09-24 reproduction) is `test_micro_smt.py`'s job.

Every scenario SEEDS one call before the one under test: `bar_closed` only reports a
completed bar once the MINUTE has rolled since a previous call (see `executor.py`), and
both `micro_smt_reject` and `micro_smt_exit` are bar-close-only mechanisms, so a bare
single call can never reach them regardless of the flags — that would make a broken gate
pass silently.

Flag-off byte-identity for the REST of the default suite is covered by every other
existing Executor test passing unmodified with both flags at their False default —
nothing here changes unless a test explicitly monkeypatches a flag to True.
"""
import json

import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
from agent.trader import executor as executor_mod
from agent.trader.executor import Executor
from agent.trader.order_port import MirroringOrderPort
from agent.trader.order_sim import OrderSim
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-24"
ARM = pd.Timestamp(f"{DATE} 09:21", tz=TZ)


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _records(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def _kinds(tmp_path):
    return [r["kind"] for r in _records(tmp_path)]


def _frames(now, mnq_px, mes_px):
    """30 flat 1m bars per ticker, the last one the in-progress minute."""
    minute = now.floor("1min")
    idx = pd.date_range(minute - pd.Timedelta(minutes=30), minute, freq="1min")

    def _df(px):
        return pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                             "Volume": 1.0}, index=idx)
    return {"MNQ": _df(mnq_px), "MES": _df(mes_px)}


class _StubArbiter:
    def __init__(self):
        self.spent = []

    def spend(self, mechanism):
        self.spent.append(mechanism)


class StubMicroMarket:
    """`MarketMechanisms` with every machine stubbed. `entry_fire` / `exit_fire` are
    consumed once, and every call is COUNTED, so this stub -- unlike
    `test_executor_live_rules.StubMarket` -- actually reflects whether the Executor
    reached the detector at all, which is what O3's cutoff exemption and O4's flag
    gating need to prove."""

    def __init__(self):
        self.entry_fire = None
        self.exit_fire = None
        self.entry_calls = 0
        self.exit_calls = 0
        self.arbiter = _StubArbiter()

    def pick(self, fires):
        for _mech, fire in fires:
            if fire is not None:
                return fire
        return None

    def seed_sec7(self, *a, **k): pass
    def sync_episodes(self, *a, **k): pass
    def reset_cycles(self): pass
    def sec6_on_tick(self, *a, **k): return None
    def sec6_on_bar_close(self, *a, **k): return None
    def sec7_on_bar_close(self, *a, **k): return None
    def tmso_on_bar_close(self, *a, **k): return None
    def fvg1h_on_bar_close(self, *a, **k): return None

    def micro_smt_entry_on_bar_close(self, *a, **k):
        self.entry_calls += 1
        f, self.entry_fire = self.entry_fire, None
        return f

    def micro_smt_exit_on_bar_close(self, *a, **k):
        self.exit_calls += 1
        f, self.exit_fire = self.exit_fire, None
        return f


def make_executor(tmp_path, monkeypatch, *, pick=None, order_port=None):
    plan = {"plan_id": "microsmt", "thesis_id": "t", "direction": "DOWN",
            "dol": {"level": "far", "price": 1.0}, "valid_while": [],
            "armed_classes": [], "attempts_used": 0, "blacklist": [],
            "cooldown_until": None}
    ex = Executor(tmp_path, plan=plan, arm_ts=ARM, ticker="MNQ", order_port=order_port)
    ex._market = StubMicroMarket()
    monkeypatch.setattr(executor_mod, "select_target",
                        lambda *a, **k: (dict(pick) if pick else None))
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", False)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", False)
    return ex


def _step(ex, hms, mnq_px, mes_px=None):
    now = _ts(hms)
    ex.on_bar(now, _frames(now, mnq_px, mes_px if mes_px is not None else mnq_px))
    return now


def _seed(ex, hms, mnq_px, mes_px=None):
    """A bar-close cascade needs a PRIOR call to establish `_last_minute` before the
    next one can report a rolled-over, completed bar. This call asserts nothing."""
    _step(ex, hms, mnq_px, mes_px)


# --------------------------------------------------------------------------- #
# O3: the flag and its own cutoff                                              #
# --------------------------------------------------------------------------- #

def test_flag_off_micro_smt_entry_is_never_asked_past_the_shared_cutoff(tmp_path,
                                                                          monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is None
    assert ex._market.entry_calls == 0, "the flag being off must short-circuit BEFORE " \
        "the detector is even asked, exactly like every other flag in this codebase"


def test_flag_on_micro_smt_entry_fires_past_the_shared_1030_cutoff(tmp_path, monkeypatch):
    """O3's whole point: exempt from `ENTRY_CUTOFF_ET`, governed by its OWN window."""
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    assert ex.position()["direction"] == "DOWN"
    assert ex._market.entry_calls == 1
    assert "fill" in _kinds(tmp_path)


def test_flag_on_micro_smt_entry_blocked_before_the_window_opens(tmp_path, monkeypatch):
    """No O3 entry before 10:30 -- the window's own lower bound, not just the shared
    cutoff exemption. `_micro_smt_entry_block` must gate BEFORE the detector runs."""
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:28:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:29:00")}
    _step(ex, "10:29:00", 30600.0)
    assert ex.position() is None
    assert ex._market.entry_calls == 0, "before 10:30 O3 must not be evaluated at all"


def test_flag_on_micro_smt_entry_blocked_at_and_after_1100(tmp_path, monkeypatch):
    """The operator's window: 10:30 <= entry < 11:00 ET. 11:00:00 itself is OUT."""
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:59:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("11:00:00")}
    _step(ex, "11:00:00", 30600.0)
    assert ex.position() is None
    assert ex._market.entry_calls == 0, "the window's own upper bound must gate " \
        "BEFORE the detector, exactly like the lower bound"


def test_flag_on_micro_smt_entry_allowed_one_second_before_the_window_closes(tmp_path,
                                                                              monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:58:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:59:59")}
    _step(ex, "10:59:59", 30600.0)
    assert ex.position() is not None


def test_the_other_four_mechanisms_are_still_blocked_past_1030_when_O3_is_on(tmp_path,
                                                                              monkeypatch):
    """Enabling O3 must not silently re-open the 10:30 cutoff for tmso/§6/§7/fvg_1h."""
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    calls = []
    ex._market.tmso_on_bar_close = lambda *a, **k: (calls.append(1), None)[1]
    _seed(ex, "10:30:00", 30600.0)
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is None
    assert calls == [], "tmso_reject must not even be asked once the shared cutoff hits"


def test_micro_smt_entry_shares_the_attempt_budget_and_cooldown(tmp_path, monkeypatch):
    """Same spine as every other market mechanism: cooldown after a stop-out, and the
    shared 3-attempt cap, both still gate O3 exactly as they gate the rest."""
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    _step(ex, "10:32:00", 30620.0)                      # stops out (short, price up)
    assert ex.position() is None
    assert ex.attempts_used() == 1
    # Still inside the same-minute cooldown: must not be re-asked.
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30615.0, "time": _ts("10:32:30")}
    _step(ex, "10:32:30", 30600.0)
    assert ex.position() is None


# --------------------------------------------------------------------------- #
# O4: the flag, the positive-close latch, and the attempt spend on a loser      #
# --------------------------------------------------------------------------- #

def test_flag_off_micro_smt_exit_is_never_asked(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    ex._market.exit_fire = {"price": 30500.0, "time": _ts("10:33:00")}
    _step(ex, "10:33:00", 30550.0)
    assert ex.position() is not None, "flag off: the position must be untouched"
    assert ex._market.exit_calls == 0


def test_flag_on_profitable_micro_smt_exit_closes_and_latches_no_entry_after_positive(
        tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    before = ex.attempts_used()
    ex._market.exit_fire = {"price": 30500.0, "time": _ts("10:33:00")}     # short, +100
    _step(ex, "10:33:00", 30550.0)
    assert ex.position() is None
    assert "micro_smt_exit" in _kinds(tmp_path)
    assert ex.attempts_used() == before, "a WINNING O4 exit must not spend an attempt"
    assert ex._positive_close is True, "a WINNING O4 exit must latch " \
        "NO_ENTRY_AFTER_POSITIVE exactly like a T2 touch does"

    # And NO further entry, of any mechanism, for the rest of the plan -- the NEXT
    # call is the first one whose CACHED `entry_block` reflects the latch above (it is
    # computed once per `on_bar` call, at the top, before this exit runs).
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30400.0, "stop": 30415.0, "time": _ts("10:40:00")}
    _step(ex, "10:40:00", 30400.0)
    assert ex.position() is None
    assert ex.bind_state()["entry_block"] == "after_positive_trade"


def test_flag_on_losing_micro_smt_exit_spends_the_shared_attempt(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    before = ex.attempts_used()
    ex._market.exit_fire = {"price": 30650.0, "time": _ts("10:33:00")}    # short, -50: a loser
    _step(ex, "10:33:00", 30620.0)
    assert ex.position() is None
    assert "micro_smt_exit" in _kinds(tmp_path)
    assert ex.attempts_used() == before + 1, "a LOSING O4 exit spends the shared " \
        "attempt budget exactly like a stop-out would"
    assert ex._market.arbiter.spent == ["micro_smt_reject"]
    # NOT `entry_block is None`: by 10:33 the shared 10:30 cutoff independently blocks
    # any new entry regardless of this latch -- the invariant under test is the LATCH
    # itself, which only `_positive_close` isolates from that unrelated time gate.
    assert ex._positive_close is False, "a loser does not latch NO_ENTRY_AFTER_POSITIVE"


def test_flag_on_micro_smt_exit_refuses_on_a_live_mirroring_port(tmp_path, monkeypatch):
    """`MirroringOrderPort` has no `flatten` (only `OrderSim` does) -- the same gap
    `_initial_opp_close` (plan 35 action B) already refuses on. O4 must refuse the same
    way, not raise into a swallowed `market_mech_error` or silently no-op forever."""
    sunk = []
    port = MirroringOrderPort(OrderSim(dol=None), lambda ev: sunk.append(ev) or
                              {"ok": True, "reason": ""})
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    assert ex.position() is not None
    ex._market.exit_fire = {"price": 30500.0, "time": _ts("10:33:00")}
    _step(ex, "10:33:00", 30550.0)
    assert ex.position() is not None, "refused, not crashed and not silently closed"
    assert ex.bind_state().get("micro_smt_exit_unwired") is True
    assert "micro_smt_exit" not in [e.get("kind") for e in sunk]
    vetoes = [r for r in _records(tmp_path) if r.get("reason") == "micro_smt_exit_unwired"]
    assert len(vetoes) == 1, "the refusal must be VISIBLE in trader_decisions.jsonl " \
        "so a live session shows when O4 would have exited"

    # A SECOND fire on a LATER bar of the SAME position must not record again -- one
    # deduped observation per position, not one per bar.
    ex._market.exit_fire = {"price": 30510.0, "time": _ts("10:34:00")}
    _step(ex, "10:34:00", 30550.0)
    assert len([r for r in _records(tmp_path)
               if r.get("reason") == "micro_smt_exit_unwired"]) == 1


def test_micro_smt_exit_unwired_record_resets_per_position(tmp_path, monkeypatch):
    """A NEW position that hits the same live-port gap is recorded again -- the latch is
    per-position (reset at every fill), not a one-shot for the whole plan."""
    sunk = []
    port = MirroringOrderPort(OrderSim(dol=None), lambda ev: sunk.append(ev) or
                              {"ok": True, "reason": ""})
    ex = make_executor(tmp_path, monkeypatch, order_port=port)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", True)
    _seed(ex, "10:30:00", 30600.0)
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:31:00")}
    _step(ex, "10:31:00", 30600.0)
    ex._market.exit_fire = {"price": 30500.0, "time": _ts("10:33:00")}
    _step(ex, "10:33:00", 30550.0)
    assert len([r for r in _records(tmp_path)
               if r.get("reason") == "micro_smt_exit_unwired"]) == 1

    # Stop it out (spends the cooldown) and re-enter -- a fresh position.
    _step(ex, "10:34:00", 30720.0)
    assert ex.position() is None
    ex._market.entry_fire = {"mechanism": "micro_smt_reject", "direction": "DOWN",
                             "price": 30600.0, "stop": 30700.0, "time": _ts("10:36:00")}
    _step(ex, "10:36:00", 30600.0)
    assert ex.position() is not None
    ex._market.exit_fire = {"price": 30500.0, "time": _ts("10:38:00")}
    _step(ex, "10:38:00", 30550.0)
    assert len([r for r in _records(tmp_path)
               if r.get("reason") == "micro_smt_exit_unwired"]) == 2
