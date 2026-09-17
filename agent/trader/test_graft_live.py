"""Plan 38 Wave 1: what `TraderGraft` does differently when it is handed an order sink.

Without a sink nothing may change — that is every replay. With one: the Executor is
built on a mirroring port, a restart is refused, and an outside supervisor gets a
surface to poll (`health`, `position_view`) and two levers (`external_kill`, `disarm`).
"""
import json

import pandas as pd
import pytest

from agent.trader import analyzer as analyzer_mod
from agent.trader.graft import TraderGraft
from agent.trader.order_port import MirroringOrderPort
from agent.trader.order_sim import OrderSim
from agent.trader.plan_store import PlanStore
from agent.trader.records import DECISIONS_FILE

TZ = "America/New_York"
DATE = "2026-09-03"
PLAN = {"plan_id": "p38", "thesis_id": "t", "direction": "UP",
        "dol": {"level": "far", "price": 99999.0}, "valid_while": [],
        "armed_classes": [], "attempts_used": 0, "blacklist": [],
        "cooldown_until": None}
THESIS = {"bias": "UP", "dol": {"level": "far", "price": 99999.0}}


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _frame(now, px=29250.0, hi=None, lo=None):
    minute = now.floor("1min")
    idx = pd.date_range(minute - pd.Timedelta(minutes=30), minute, freq="1min")
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                       "Volume": 1.0}, index=idx)
    df.iloc[-1] = [px, hi if hi is not None else px, lo if lo is not None else px, px, 1.0]
    return df


def _recs(tmp_path):
    p = tmp_path / DECISIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


class _FakeAnalyzer:
    """Serves a thesis with no facts assembly and no call: this file is about the graft."""

    def __init__(self, thesis):
        self.thesis = thesis
        self.runs = 0

    def maybe_run(self, now, bars):
        self.runs += 1

    def standing_thesis(self, now=None):
        return self.thesis


def _graft(tmp_path, monkeypatch, *, sink=None, thesis=THESIS):
    g = TraderGraft(tmp_path, backend=lambda *a, **k: None, threaded=False,
                    order_sink=sink)
    g._analyzer = _FakeAnalyzer(thesis)
    monkeypatch.setattr(g, "_derive", lambda thesis, bars, now: dict(PLAN))
    return g


def _bar(g, hms, **kw):
    now = _ts(hms)
    g.on_bar(now, _frame(now, **kw), _frame(now, **kw))
    return now


def _arm(g):
    _bar(g, "09:20:30")
    _bar(g, "09:21:00")                       # the minute rolls: plan derivation runs


# --------------------------------------------------------------------------- #
# cases 18-20: the sink decides the port, and a restart is refused              #
# --------------------------------------------------------------------------- #

def test_no_sink_builds_the_executor_on_a_plain_order_sim(tmp_path, monkeypatch):
    """Case 18 — the replay path."""
    g = _graft(tmp_path, monkeypatch)
    _arm(g)
    assert g.plan() is not None
    assert type(g._executor._sim) is OrderSim
    assert g.disarmed() is None


def test_run_replay_still_builds_the_graft_without_a_sink():
    """`replay._factory` is untouched: it must not grow an `order_sink=`."""
    import inspect
    from agent.trader import replay
    assert "order_sink" not in inspect.getsource(replay)


def test_a_sink_and_an_empty_plan_store_arm_normally(tmp_path, monkeypatch):
    """Case 19."""
    seen = []
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: seen.append(ev) or None)
    _arm(g)
    assert g.plan()["plan_id"] == "p38" and g.plan_alive() is True
    assert isinstance(g._executor._sim, MirroringOrderPort)
    assert PlanStore(tmp_path).all(), "the plan is persisted — it is the restart detector"
    assert seen == [] and g.disarmed() is None

    g._executor._sim.fill_market(_ts("09:40:00"), direction="UP", price=29250.0,
                                 stop=29235.0, artifact_id="x")
    assert seen[0]["plan_id"] == "p38" and seen[0]["seq"] == 1


def test_a_sink_and_a_plan_already_on_disk_never_arm(tmp_path, monkeypatch):
    """Case 20 (F6): a restart would otherwise derive a SECOND plan with a fresh
    attempt budget and no memory of the first one's position."""
    PlanStore(tmp_path).put(dict(PLAN, plan_id="the-first-plan"))
    seen = []
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: seen.append(ev) or None)
    assert g.disarmed() == "restart"
    _arm(g)
    _bar(g, "09:40:00")
    assert g.plan() is None and g._executor is None and seen == []
    assert g._analyzer.runs == 0, "a disarmed session makes no thesis call either"
    disarmed = [r for r in _recs(tmp_path) if r["kind"] == "session_disarmed"]
    assert len(disarmed) == 1 and disarmed[0]["reason"] == "restart"
    assert disarmed[0]["time"] == _ts("09:20:30").isoformat(), "BAR time, first bar"


def test_a_plan_on_disk_does_not_stop_a_sinkless_graft(tmp_path, monkeypatch):
    """The refusal is a LIVE rule. Replay run dirs are fresh, but nothing here may
    depend on that."""
    PlanStore(tmp_path).put(dict(PLAN, plan_id="older"))
    g = _graft(tmp_path, monkeypatch)
    _arm(g)
    assert g.plan() is not None and g.disarmed() is None


# --------------------------------------------------------------------------- #
# case 21: health                                                               #
# --------------------------------------------------------------------------- #

def test_a_raising_run_lands_in_health_and_on_bar_does_not_raise(tmp_path, monkeypatch):
    """Case 21 (F3)."""
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: None)
    assert g.health() == {"last_error": None, "order_error": None, "last_bar": None}

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")
    monkeypatch.setattr(g, "_run", _boom)
    now = _bar(g, "09:40:00")
    h = g.health()
    assert h["last_error"] == "RuntimeError: detector exploded" and h["last_bar"] == now


def test_the_executors_swallowed_order_error_is_visible_in_health(tmp_path, monkeypatch):
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: None)
    _arm(g)

    def _boom(now, bar):
        raise ValueError("book corrupt")
    monkeypatch.setattr(g._executor._sim, "on_bar", _boom)
    _bar(g, "09:40:00")
    assert g.health()["order_error"] == "ValueError: book corrupt"
    assert g.health()["last_error"] is None


# --------------------------------------------------------------------------- #
# case 22: external_kill                                                        #
# --------------------------------------------------------------------------- #

def test_external_kill_kills_the_plan_before_voiding_and_always_records(tmp_path,
                                                                         monkeypatch):
    """Case 22. Order: plan dead, THEN void, THEN record — and the record is attempted
    even when the void raises."""
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: None)
    _arm(g)
    g._executor._sim.fill_market(_ts("09:40:00"), direction="UP", price=29250.0,
                                 stop=29235.0, artifact_id="x")
    order = []
    real_kill = g._executor.kill_plan

    def _kill(*a, **k):
        order.append(("kill", g.position_view() is not None))
        real_kill(*a, **k)

    def _void():
        order.append(("void", g.plan_alive()))
        raise RuntimeError("void failed")

    monkeypatch.setattr(g._executor, "kill_plan", _kill)
    monkeypatch.setattr(g._executor, "void_position", _void)
    g.external_kill(_ts("09:41:00"), "active_cleared", void_position=True)   # no raise

    assert order == [("kill", True), ("void", False)], \
        "the plan must already be dead when the position is voided"
    kinds = [r["kind"] for r in _recs(tmp_path)]
    assert kinds.index("plan_dead") < kinds.index("external_kill")
    rec = [r for r in _recs(tmp_path) if r["kind"] == "external_kill"][0]
    assert rec["reason"] == "active_cleared" and rec["void_position"] is True
    assert "void failed" in rec["detail"]["error"]
    assert "void failed" in g.health()["last_error"]


def test_external_kill_voids_the_position_without_booking_an_exit(tmp_path,
                                                                   monkeypatch):
    seen = []
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: seen.append(ev) or None)
    _arm(g)
    g._executor._sim.fill_market(_ts("09:40:00"), direction="UP", price=29250.0,
                                 stop=29235.0, artifact_id="x")
    g.external_kill(_ts("09:41:00"), "active_cleared", void_position=True)
    assert g.position_view() is None and g.plan_alive() is False
    assert [e["kind"] for e in seen] == ["fill"], "a void sends NOTHING outward"
    dead = [r for r in _recs(tmp_path) if r["kind"] == "plan_dead"][0]
    assert dead["reason"] == "external_position_change"
    _bar(g, "09:45:00")
    assert g.plan()["plan_id"] == "p38", "a dead plan is never re-derived"


def test_external_kill_without_void_leaves_the_model_alone(tmp_path, monkeypatch):
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: None)
    _arm(g)
    g.external_kill(_ts("09:41:00"), "unexpected_active", void_position=False)
    assert g.plan_alive() is False and g.position_view() is None
    assert [r["kind"] for r in _recs(tmp_path)] == ["plan_dead", "external_kill"]


def test_disarm_stops_all_further_work_and_records_once(tmp_path, monkeypatch):
    g = _graft(tmp_path, monkeypatch, sink=lambda ev: None)
    _arm(g)
    runs = g._analyzer.runs
    g.disarm(_ts("09:41:00"), "agent_error")
    g.disarm(_ts("09:41:01"), "again")
    _bar(g, "09:42:00")
    assert g._analyzer.runs == runs and g.disarmed() == "agent_error"
    assert [r["reason"] for r in _recs(tmp_path)
            if r["kind"] == "session_disarmed"] == ["agent_error"]


# --------------------------------------------------------------------------- #
# case 23: no thesis, no trading                                                #
# --------------------------------------------------------------------------- #

def _raises(*a, **k):
    raise RuntimeError("backend down")


@pytest.mark.parametrize("backend", [
    _raises,
    lambda *a, **k: (None, {"verdict": "refused"}),
    lambda *a, **k: ("not a thesis", {}),
    lambda *a, **k: ({"bias": "NEUTRAL", "dol": None}, {}),
    lambda *a, **k: ({"bias": "UP"}, {}),                       # directional, no DOL
], ids=["raises", "refuses", "malformed", "neutral", "no-dol"])
def test_no_standing_thesis_means_no_plan_and_no_sink_call(tmp_path, monkeypatch,
                                                           backend):
    """Case 23 (D10). The REAL Analyzer, with facts assembly stubbed so the backend is
    actually reached."""
    monkeypatch.setattr(analyzer_mod, "assemble_facts",
                        lambda store, bars, now: ("facts", "ctx", {}, None))
    monkeypatch.delenv("ACT_TRADER_ARM_HHMM", raising=False)
    seen, calls = [], []

    def _counted(*a, **k):
        calls.append(1)
        return backend(*a, **k)

    g = TraderGraft(tmp_path, _counted, threaded=False,
                    order_sink=lambda ev: seen.append(ev) or None)
    for hms in ("09:19:59", "09:20:00", "09:21:00", "09:31:00", "10:00:00", "12:59:00"):
        _bar(g, hms)
    assert calls == [1], "the backend must be REACHED, once, or this proves nothing"
    assert g.plan() is None and g._executor is None and seen == []
    assert g.position_view() is None and g.plan_alive() is False
    assert not PlanStore(tmp_path).all()


# --------------------------------------------------------------------------- #
# case 24: the raw-second overlay                                               #
# --------------------------------------------------------------------------- #

def test_the_raw_second_replaces_the_last_mnq_rows_high_and_low_only(tmp_path):
    """Case 24 (F1)."""
    g = TraderGraft(tmp_path, backend=None)
    now = _ts("09:44:01")
    mnq, mes = _frame(now, hi=29290.0, lo=29210.0), _frame(now, hi=29290.0, lo=29210.0)
    raw = {"open": 29251.0, "high": 29252.0, "low": 29249.5, "close": 29250.0,
           "volume": 3, "second_ts": now}
    out = g._frames(now, mnq, mes, None, None, raw_mnq=raw)

    last = out["MNQ"].iloc[-1]
    assert (last["High"], last["Low"]) == (29252.0, 29249.5)
    assert (last["Open"], last["Close"]) == (29250.0, 29250.0), "only High/Low move"
    assert out["MNQ"].iloc[:-1].equals(mnq.iloc[:-1])
    assert out["MES"] is mes and mes.iloc[-1]["High"] == 29290.0, "MES untouched"
    assert mnq.iloc[-1]["High"] == 29290.0, "the caller's frame is never mutated"


def test_the_overlay_is_a_no_op_when_absent_or_stale(tmp_path):
    g = TraderGraft(tmp_path, backend=None)
    now = _ts("09:44:01")
    mnq, mes = _frame(now, hi=29290.0), _frame(now)
    assert g._frames(now, mnq, mes, None, None)["MNQ"] is mnq
    assert g._frames(now, mnq, mes, None, None, raw_mnq=None)["MNQ"] is mnq
    stale = {"high": 1.0, "low": 0.5, "second_ts": _ts("09:44:00")}
    assert g._frames(now, mnq, mes, None, None, raw_mnq=stale)["MNQ"] is mnq
    assert g._frames(now, mnq, mes, None, None, raw_mnq={"high": 1.0})["MNQ"] is mnq


def test_the_overlay_never_mutates_the_cached_history(tmp_path):
    g = TraderGraft(tmp_path, backend=None)
    now = _ts("09:44:01")
    hist = _frame(_ts("09:00:00"))
    raw = {"high": 1.0, "low": 0.5, "second_ts": now}
    out = g._frames(now, None, None, hist, hist, raw_mnq=raw)
    assert out["MNQ"].iloc[-1]["High"] == 1.0
    assert g._hist["MNQ"].iloc[-1]["High"] != 1.0


def test_set_raw_second_is_consumed_by_exactly_one_bar(tmp_path, monkeypatch):
    g = _graft(tmp_path, monkeypatch)
    seen = []
    real = g._frames

    def _spy(*a, raw_mnq=None, **k):
        seen.append(raw_mnq)
        return real(*a, raw_mnq=raw_mnq, **k)
    monkeypatch.setattr(g, "_frames", _spy)
    raw = {"high": 29251.0, "low": 29249.0, "second_ts": _ts("09:40:00")}
    g.set_raw_second(raw)
    _bar(g, "09:40:00")
    _bar(g, "09:40:01")
    assert seen == [raw, None]
