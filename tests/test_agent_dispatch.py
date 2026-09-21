"""Plan 38 Wave 2: the OUTER half of the agent's order port, and its wiring.

Nothing here can reach a broker. The order module's executor is replaced with a mock in
EVERY test (autouse), live mode is forced off, and all state is redirected into tmp_path
— a previous incident had tests write the real live `global.json`. Where a test needs
the real `dispatch`, it is the real function over a fake executor and a tmp state dir.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import automation.agent_dispatch as ad
import automation.main as main
import paths
import smt_state
from agent.trader import analyzer as analyzer_mod
from agent.trader import executor as executor_mod
from agent.trader.graft import TraderGraft
from automation.agent_dispatch import AgentDispatchPort

# The order module is imported INSIDE the autouse fixture, never here. It builds its
# executor at import from LIVE_TRADING, which the developer's `.env` may set to true, so
# the import has to happen with that variable forced off. It cannot be forced off at
# module level: that leaks into every later test module — and `test_orchestrator_main`
# then no longer skips its pre-session gap-fill, which goes to a REAL IB Gateway if one
# is up (found the hard way while writing this file). `monkeypatch` scopes it to a test.
live_orders = None

TZ = "America/New_York"
DATE = "2026-09-03"
PLAN = {"plan_id": "p38", "thesis_id": "t", "direction": "UP",
        "dol": {"level": "far", "price": 99999.0}, "valid_while": [],
        "armed_classes": [], "attempts_used": 0, "blacklist": [],
        "cooldown_until": None}


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


# --------------------------------------------------------------------------- #
# isolation — autouse, every test                                               #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_broker_no_real_state(tmp_path, monkeypatch):
    global live_orders
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("DISCONNECTED", "false")
    import live_orders as _lo
    live_orders = _lo
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.delenv("ACT_STATE_DIR", raising=False)
    monkeypatch.setattr(paths, "_STATE_DIR", state)
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)
    monkeypatch.setattr(smt_state, "_PATH_CACHE_SD", None, raising=False)
    executor = MagicMock(name="fake-executor")
    executor._entry_is_live = True
    monkeypatch.setattr(live_orders, "_executor", executor)
    monkeypatch.setattr(live_orders, "_LIVE", False)
    monkeypatch.setattr(live_orders, "_DISCONNECTED", False)
    monkeypatch.setattr(live_orders, "_pending_close_after", None)
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path / "sessions")
    live_orders.set_session_date(DATE)
    assert str(tmp_path) in str(smt_state._position_path()), "state is NOT isolated"
    assert str(tmp_path) in str(smt_state.pause_path()), "the pause file is NOT isolated"
    yield executor
    live_orders.set_session_date("")


# --------------------------------------------------------------------------- #
# fakes                                                                         #
# --------------------------------------------------------------------------- #

class FakeBook:
    """`dispatch` and position.json, faked: what the dispatcher would have done."""

    def __init__(self, contracts=1):
        self.signals = []
        self.active = {}
        self.refuse_entries = False
        self.contracts = contracts
        self.raises = False

    def emit(self, sig):
        json.dumps(sig)                       # exactly what the real emit does first
        self.signals.append(sig)
        if self.raises:
            raise RuntimeError("dispatch exploded")
        if sig["kind"] == "market-entry":
            if not self.refuse_entries:
                self.active = {"direction": "long", "contracts": self.contracts,
                               "source": "strategy"}
        elif sig["kind"] == "market-close":
            self.active = {}

    def read(self):
        return dict(self.active)


def _port(tmp_path, book, contracts=1):
    return AgentDispatchPort(book.emit, recorder_path=tmp_path / "agent_dispatch.jsonl",
                             active_reader=book.read, contracts=contracts)


def _lines(tmp_path):
    p = tmp_path / "agent_dispatch.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


def _fill(seq=1, hms="09:40:00", direction="UP", px=29250.0, stop=29235.0):
    return {"kind": "fill", "time": _ts(hms), "price": px, "direction": direction,
            "artifact_id": "extreme_reject_close", "stop": stop, "seq": seq,
            "plan_id": "p38", "mechanism": "extreme_reject_close"}


def _close(kind, seq=2, hms="09:45:00", px=29235.0):
    return {"kind": kind, "time": _ts(hms), "price": px, "direction": "UP",
            "artifact_id": "extreme_reject_close", "entry": 29250.0, "seq": seq,
            "plan_id": "p38", "mechanism": "extreme_reject_close"}


class FakeGraft:
    def __init__(self, position=None, plan=True, error=None, order_error=None):
        self._position = position
        self._plan = dict(PLAN) if plan else None
        self._error, self._order_error = error, order_error
        self.kills, self.disarms = [], []

    def health(self):
        return {"last_error": self._error, "order_error": self._order_error,
                "last_bar": None}

    def position_view(self):
        return dict(self._position) if self._position else None

    def plan(self):
        return self._plan

    def external_kill(self, now, reason, void_position=False):
        self.kills.append((now, reason, void_position))
        if void_position:
            self._position = None

    def disarm(self, now, reason):
        self.disarms.append((now, reason))


HELD = {"direction": "UP", "entry": 29250.0, "stop": 29235.0}


# --------------------------------------------------------------------------- #
# cases 25-28: the sink                                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("direction,expected", [("UP", "up"), ("DOWN", "down")])
def test_a_fill_becomes_one_json_safe_ascii_market_entry(tmp_path, direction, expected):
    """Case 25 (F10)."""
    book = FakeBook()
    ack = _port(tmp_path, book).sink(_fill(direction=direction))
    assert ack == {"ok": True, "reason": ""}
    assert len(book.signals) == 1
    sig = book.signals[0]
    assert sig["kind"] == "market-entry" and sig["direction"] == expected
    assert sig["price"] == 29250.0 and sig["stop"] == 29235.0
    assert sig["flatten_first"] is False and sig["source"] == "agent"
    assert sig["time"] == _ts("09:40:00").isoformat() and isinstance(sig["time"], str)
    json.dumps(sig).encode("ascii")
    assert all(v is None or isinstance(v, (bool, int, float, str)) for v in sig.values())


@pytest.mark.parametrize("kind,reason", [("stop_out", "stop_out"),
                                         ("take_profit", "take_profit"),
                                         ("mark", "window_end")])
def test_every_close_becomes_a_market_close_with_its_reason(tmp_path, kind, reason):
    """Case 26."""
    book = FakeBook()
    port = _port(tmp_path, book)
    port.sink(_fill())
    assert port.sink(_close(kind)) == {"ok": True, "reason": ""}
    sig = book.signals[1]
    assert sig["kind"] == "market-close" and sig["reason"] == reason
    assert sig["skip_recon"] is True and sig["source"] == "agent"
    assert sig["price"] == 29235.0 and isinstance(sig["time"], str)
    json.dumps(sig).encode("ascii")


def test_the_port_source_builds_market_kinds_only():
    """Cases 26 / 2.8 — a SOURCE-level gate (D4, D5, F13). No resting-order kind, no
    legacy stop kind, no cancel kind may even be spelled in this module."""
    src = inspect.getsource(ad)
    for token in ("stop-exit", "stop-entry", "stopped-out", "cancel-"):
        assert token not in src, token
    assert {ad.ENTRY_KIND, ad.CLOSE_KIND} == {"market-entry", "market-close"}


def test_the_same_plan_and_seq_delivered_twice_is_emitted_once(tmp_path):
    """Case 27 (F7): the key is `(plan_id, seq)`, never the artifact id."""
    book = FakeBook()
    port = _port(tmp_path, book)
    first = port.sink(_fill(seq=1))
    assert port.sink(_fill(seq=1)) == first
    assert len(book.signals) == 1
    assert _lines(tmp_path)[-1]["suppressed"] == "duplicate"
    # ...while a SECOND entry with the SAME artifact id is a different event.
    port.sink(_close("stop_out", seq=2))
    port.sink(_fill(seq=3, hms="09:55:00"))
    assert [s["kind"] for s in book.signals] == ["market-entry", "market-close",
                                                 "market-entry"]


def test_an_entry_the_dispatcher_refused_is_acked_not_ok_and_voided(tmp_path):
    """Case 28 (F8): paused / suppress sentinel / window gate all look the same from
    here — `active` is still empty after the dispatch."""
    book = FakeBook()
    book.refuse_entries = True
    ack = _port(tmp_path, book).sink(_fill())
    assert ack == {"ok": False, "reason": "entry_refused", "voided": True}
    line = _lines(tmp_path)[0]
    assert line["ack"] == ack and line["signal"]["kind"] == "market-entry"


def test_the_record_line_has_the_wave_three_contract(tmp_path):
    book = FakeBook()
    _port(tmp_path, book).sink(_fill())
    line = _lines(tmp_path)[0]
    assert set(line) == {"seq", "bar_time", "wall_time", "sim_event", "signal", "ack",
                         "dispatch_ms", "suppressed"}
    assert line["seq"] == 1 and line["bar_time"] == _ts("09:40:00").isoformat()
    assert line["dispatch_ms"] >= 0 and line["suppressed"] is None
    assert line["sim_event"]["kind"] == "fill" and line["ack"]["ok"] is True


def test_a_raising_emit_is_not_ok_and_never_propagates(tmp_path):
    book = FakeBook()
    book.raises = True
    ack = _port(tmp_path, book).sink(_fill())
    assert ack["ok"] is False and ack["reason"].startswith("emit_raised")
    assert "voided" not in ack, "unknown whether it left: an EXTERNAL change, not a void"


def test_a_close_that_leaves_active_set_is_not_ok(tmp_path):
    book = FakeBook()
    port = _port(tmp_path, book)
    port.sink(_fill())
    book.emit = lambda sig: book.signals.append(sig)          # the close does not take
    port._emit = book.emit
    assert port.sink(_close("stop_out"))["reason"] == "close_not_confirmed"
    book.active["source"] = "recon-adopt"
    assert port.sink(_close("stop_out", seq=3))["reason"] == "recon_adopt"


# --------------------------------------------------------------------------- #
# cases 29, 31, 33-36: the watchdog                                             #
# --------------------------------------------------------------------------- #

def test_sim_holds_but_active_is_empty_kills_and_voids_with_no_order(tmp_path):
    """Case 29 (F9): a manual close in another process, or the post-entry verify
    clearing `active`."""
    book = FakeBook()
    graft = FakeGraft(position=HELD)
    _port(tmp_path, book).supervise(_ts("09:50:00"), graft)
    assert graft.kills == [(_ts("09:50:00"), "active_cleared_externally", True)]
    assert book.signals == []
    assert _lines(tmp_path)[-1]["sim_event"]["kind"] == "watchdog_kill"


def test_sim_flat_but_active_set_kills_without_voiding(tmp_path):
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "manual"}
    graft = FakeGraft(position=None)
    _port(tmp_path, book).supervise(_ts("09:50:00"), graft)
    assert graft.kills == [(_ts("09:50:00"), "unexpected_active_position", False)]
    assert book.signals == []


def test_a_recon_adopted_position_kills(tmp_path):
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "recon-adopt"}
    graft = FakeGraft(position=HELD)
    _port(tmp_path, book).supervise(_ts("09:50:00"), graft)
    assert [k[1] for k in graft.kills] == ["recon_adopt"]


def test_a_contracts_mismatch_kills(tmp_path):
    """Case 31 — a genuine anomaly now that D18 records the configured size."""
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 2, "source": "strategy"}
    graft = FakeGraft(position=HELD)
    _port(tmp_path, book, contracts=1).supervise(_ts("09:50:00"), graft)
    assert [k[1] for k in graft.kills] == ["contracts_mismatch"]
    assert book.signals == []


def test_agreement_is_silent_and_a_kill_happens_once(tmp_path):
    book = FakeBook()
    port = _port(tmp_path, book)
    graft = FakeGraft(position=None)
    port.supervise(_ts("09:50:00"), graft)                     # flat / flat
    book.active = {"direction": "long", "contracts": 1, "source": "strategy"}
    graft._position = dict(HELD)
    port.supervise(_ts("09:50:01"), graft)                     # held / held
    assert graft.kills == [] and _lines(tmp_path) == []
    book.active = {}
    for sec in (2, 3, 4):
        port.supervise(_ts(f"09:50:0{sec}"), graft)
    assert len(graft.kills) == 1


def test_no_plan_yet_means_nothing_to_supervise(tmp_path):
    """A manual position held before the agent has armed is not the agent's business
    until there is a plan that could stack onto it."""
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "manual"}
    graft = FakeGraft(position=None, plan=False)
    _port(tmp_path, book).supervise(_ts("08:00:00"), graft)
    assert graft.kills == []


def test_a_trader_error_with_a_position_open_flattens_once_then_disarms(tmp_path):
    """Cases 33 + 35 (D11, D26): exactly ONE market close; no second dispatch."""
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "strategy"}
    graft = FakeGraft(position=HELD, error="RuntimeError: detector exploded")
    port = _port(tmp_path, book)
    for sec in (0, 1, 2):
        port.supervise(_ts(f"09:50:0{sec}"), graft)
    port.supervise(_ts("13:00:00"), graft)
    assert [s["kind"] for s in book.signals] == ["market-close"]
    sig = book.signals[0]
    assert sig["reason"] == "agent_error" and sig["skip_recon"] is True
    assert sig["source"] == "agent"
    json.dumps(sig).encode("ascii")
    assert graft.disarms == [(_ts("09:50:00"), "agent_error")] and graft.kills == []


def test_an_order_book_error_counts_as_a_trader_error(tmp_path):
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "strategy"}
    graft = FakeGraft(position=HELD, order_error="ValueError: book corrupt")
    _port(tmp_path, book).supervise(_ts("09:50:00"), graft)
    assert [s["reason"] for s in book.signals] == ["agent_error"]
    assert len(graft.disarms) == 1


def test_a_trader_error_while_flat_disarms_with_no_order(tmp_path):
    """Case 34."""
    book = FakeBook()
    graft = FakeGraft(position=None, error="RuntimeError: x")
    _port(tmp_path, book).supervise(_ts("09:50:00"), graft)
    assert book.signals == [] and len(graft.disarms) == 1


def test_the_window_end_after_a_kill_on_a_day_the_agent_filled_is_one_close(tmp_path):
    """Case 36 (D22): ONE market close, sent whatever `active` says."""
    book = FakeBook()
    port = _port(tmp_path, book)
    port.sink(_fill())
    book.active = {}                                           # cleared behind the agent
    graft = FakeGraft(position=HELD)
    port.supervise(_ts("09:50:00"), graft)
    assert len(graft.kills) == 1
    for hms in ("12:59:59", "13:00:00", "13:00:01", "13:05:00"):
        port.supervise(_ts(hms), graft)
    closes = [s for s in book.signals if s["kind"] == "market-close"]
    assert len(closes) == 1
    assert closes[0]["reason"] == "window_end" and closes[0]["skip_recon"] is True
    assert closes[0]["time"] == _ts("13:00:00").isoformat()


def test_no_window_end_close_when_the_agent_never_entered(tmp_path):
    """The position belongs to whoever opened it. Flattening it would be the agent
    closing a trade it never had."""
    book = FakeBook()
    book.active = {"direction": "long", "contracts": 1, "source": "manual"}
    graft = FakeGraft(position=None)
    port = _port(tmp_path, book)
    port.supervise(_ts("09:50:00"), graft)
    port.supervise(_ts("13:00:00"), graft)
    assert len(graft.kills) == 1 and book.signals == []


def test_nothing_is_supervised_after_the_window_end(tmp_path):
    """After 13:00 the agent is flat and done; a manual afternoon trade is not a kill,
    and certainly not something to flatten."""
    book = FakeBook()
    port = _port(tmp_path, book)
    port.sink(_fill())
    port.sink(_close("mark", hms="13:00:00"))
    book.active = {"direction": "long", "contracts": 1, "source": "manual"}
    graft = FakeGraft(position=None)
    port.supervise(_ts("14:00:00"), graft)
    assert graft.kills == []
    assert [s["kind"] for s in book.signals] == ["market-entry", "market-close"]


def test_supervise_never_raises(tmp_path):
    class _Broken:
        def health(self):
            raise RuntimeError("no")
    _port(tmp_path, FakeBook()).supervise(_ts("09:50:00"), _Broken())
    assert _lines(tmp_path)[-1]["sim_event"]["kind"] == "supervise_error"


# --------------------------------------------------------------------------- #
# cases 30, 31b, 32, 41: the REAL dispatch, over a fake executor                #
# --------------------------------------------------------------------------- #

def _real_port(tmp_path, emitted=None):
    def _emit(sig):
        json.dumps(sig)
        if emitted is not None:
            emitted.append(sig)
        live_orders.dispatch(sig)
    return AgentDispatchPort(_emit, recorder_path=tmp_path / "agent_dispatch.jsonl",
                             contracts=1)


def _active():
    return live_orders._load_pos().get("active") or {}


def test_an_agent_close_skips_the_reconcile_and_a_legacy_close_does_not(
        tmp_path, monkeypatch, _no_broker_no_real_state):
    """Case 30 (D26). The ONLY change to the order module's dispatch."""
    calls = []
    monkeypatch.setattr(live_orders, "_reconcile_on_close",
                        lambda sig: calls.append(sig) or False)
    monkeypatch.setenv("TRADING_CONTRACTS", "1")
    port = _real_port(tmp_path)
    assert port.sink(_fill())["ok"] is True
    assert port.sink(_close("stop_out"))["ok"] is True
    assert calls == [], "an agent close must not wait on a broker login"
    assert _no_broker_no_real_state.place_close.call_count == 1

    live_orders.dispatch({"kind": "market-close", "price": 29240.0, "reason": "strategy",
                          "time": _ts("09:50:00").isoformat()})
    assert len(calls) == 1, "a close WITHOUT the key reconciles exactly as before"
    live_orders.dispatch({"kind": "market-close", "price": 29240.0, "skip_recon": False,
                          "time": _ts("09:51:00").isoformat()})
    assert len(calls) == 2


def test_a_suppressing_reconcile_still_suppresses_a_legacy_close(
        monkeypatch, _no_broker_no_real_state):
    monkeypatch.setattr(live_orders, "_reconcile_on_close", lambda sig: True)
    live_orders.dispatch({"kind": "market-close", "price": 1.0, "reason": "strategy"})
    assert _no_broker_no_real_state.place_close.call_count == 0
    live_orders.dispatch({"kind": "market-close", "price": 1.0, "reason": "stop_out",
                          "skip_recon": True})
    assert _no_broker_no_real_state.place_close.call_count == 1


@pytest.mark.parametrize("contracts", ["1", "2"])
def test_place_market_entry_records_trading_contracts(monkeypatch, contracts):
    """Case 31b (D18)."""
    monkeypatch.setenv("TRADING_CONTRACTS", contracts)
    live_orders.place_market_entry("long", 29250.0, 29235.0)
    assert _active()["contracts"] == int(contracts)


def test_the_downgrade_fill_site_records_trading_contracts_too(monkeypatch):
    monkeypatch.setenv("TRADING_CONTRACTS", "1")
    live_orders._register_downgraded_fill("long", 29250.0, 29235.0, "strategy",
                                          _ts("09:40:00").isoformat(), 29250.5)
    assert _active()["contracts"] == 1


def test_an_agent_close_on_an_already_flat_book_sends_one_close(
        tmp_path, _no_broker_no_real_state):
    """Case 32 (D26): the close-on-flat path. No adopt, no kill, `active` stays empty."""
    assert _active() == {}
    emitted = []
    ack = _real_port(tmp_path, emitted).sink(_close("stop_out", seq=1))
    assert ack == {"ok": True, "reason": ""}
    assert _no_broker_no_real_state.place_close.call_count == 1
    assert _no_broker_no_real_state.place_entry.call_count == 0
    assert _active() == {} and len(emitted) == 1


def test_pending_close_after_stays_none_through_an_agent_only_session(
        tmp_path, monkeypatch, _no_broker_no_real_state):
    """Case 41 (F13): only the cancel kind arms it, and the agent never emits one."""
    monkeypatch.setenv("TRADING_CONTRACTS", "1")
    port = _real_port(tmp_path)
    seq = 0
    for hms, px in (("09:40:00", 29250.0), ("09:55:00", 29270.0)):
        seq += 1
        assert port.sink(_fill(seq=seq, hms=hms, px=px, stop=px - 15.0))["ok"]
        assert live_orders._pending_close_after is None
        seq += 1
        assert port.sink(_close("stop_out", seq=seq, px=px - 15.0))["ok"]
        assert live_orders._pending_close_after is None
    assert _no_broker_no_real_state.place_entry.call_count == 2
    assert _no_broker_no_real_state.place_close.call_count == 2


# --------------------------------------------------------------------------- #
# the graft + the port, end to end (cases 40e, 40f, 40g)                        #
# --------------------------------------------------------------------------- #

class StubMarket:
    def __init__(self):
        self.fire, self.asked = None, 0
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


class _FakeAnalyzer:
    def maybe_run(self, now, bars): pass
    def standing_thesis(self, now=None): return {"bias": "UP", "dol": {"price": 1.0}}


def _live_frame(now, px, minute_hi, minute_lo):
    """The LIVE shape: the last row is the partial minute with CUMULATIVE extremes."""
    minute = now.floor("1min")
    idx = pd.date_range(minute - pd.Timedelta(minutes=30), minute, freq="1min")
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px,
                       "Volume": 1.0}, index=idx)
    df.iloc[-1] = [px, minute_hi, minute_lo, px, 1.0]
    return df


def _armed_graft(tmp_path, monkeypatch, port):
    monkeypatch.setattr(executor_mod, "select_target", lambda *a, **k: None)
    g = TraderGraft(tmp_path / "trader", backend=lambda *a, **k: None, threaded=False,
                    order_sink=port.sink)
    g._analyzer = _FakeAnalyzer()
    monkeypatch.setattr(g, "_derive", lambda thesis, bars, now: dict(PLAN))
    for hms in ("09:20:30", "09:21:00"):
        now = _ts(hms)
        g.on_bar(now, _live_frame(now, 29250.0, 29250.0, 29250.0),
                 _live_frame(now, 29250.0, 29250.0, 29250.0))
    g._executor._market = StubMarket()
    return g


def _live_bar(g, hms, px, *, fire=False, raw_hi=None, raw_lo=None,
              minute_hi=None, minute_lo=None):
    now = _ts(hms)
    if fire:
        g._executor._market.fire = {"mechanism": "extreme_reject_close",
                                    "direction": "UP", "price": px, "stop": px - 15.0,
                                    "time": now}
    g.set_raw_second({"high": raw_hi if raw_hi is not None else px,
                      "low": raw_lo if raw_lo is not None else px, "second_ts": now})
    frame = _live_frame(now, px, minute_hi if minute_hi is not None else px,
                        minute_lo if minute_lo is not None else px)
    g.on_bar(now, frame, frame.copy())
    return now


def _decisions(tmp_path):
    p = tmp_path / "trader" / "trader_decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


def test_a_paused_entry_is_voided_the_plan_lives_and_a_close_still_goes_out(
        tmp_path, monkeypatch, _no_broker_no_real_state):
    """Case 40e (D23). The pause is the REAL one: a file, checked by the real dispatch."""
    monkeypatch.setenv("TRADING_CONTRACTS", "1")
    port = _real_port(tmp_path)
    g = _armed_graft(tmp_path, monkeypatch, port)
    executor = _no_broker_no_real_state

    assert live_orders.pause() is True
    _live_bar(g, "09:40:00", 29250.0, fire=True)
    assert executor.place_entry.call_count == 0, "paused: nothing reached the broker"
    assert g.position_view() is None and g.plan_alive() is True
    assert g._executor._plan["attempts_used"] == 0
    assert [r["kind"] for r in _decisions(tmp_path)] == ["fill_voided"]
    # The latch: the fire was CONSUMED. Nothing re-offers it on the next second —
    # the machines never learn how a fill ended, so a void cannot re-arm one.
    asked = g._executor._market.asked
    _live_bar(g, "09:40:01", 29250.0)
    assert g._executor._market.fire is None and g._executor._market.asked == asked + 1
    assert executor.place_entry.call_count == 0
    port.supervise(_ts("09:40:01"), g)
    assert g.plan_alive() is True, "a voided entry is not an external change"

    # Resumed: the plan is alive and enters. Paused again: its CLOSE still goes out.
    assert live_orders.resume() is True
    _live_bar(g, "09:45:00", 29260.0, fire=True)
    assert executor.place_entry.call_count == 1 and g.position_view() is not None
    assert _active()["contracts"] == 1
    live_orders.pause()
    _live_bar(g, "09:45:30", 29240.0, raw_lo=29230.0, minute_lo=29230.0)
    assert executor.place_close.call_count == 1 and _active() == {}
    assert g.position_view() is None
    assert [r["kind"] for r in _decisions(tmp_path)] == [
        "fill_voided", "fill", "target_selected",
                       "initial_target_selected", "stop_out"]
    assert g._executor._plan["attempts_used"] == 1
    live_orders.resume()


def test_a_process_whose_first_bar_is_0921_never_arms(tmp_path, monkeypatch):
    """Case 40f: the arm is an EXACT-MINUTE test. A late start is a dark day, by design."""
    monkeypatch.delenv("ACT_TRADER_ARM_HHMM", raising=False)
    monkeypatch.setattr(analyzer_mod, "assemble_facts",
                        lambda store, bars, now: ("facts", "ctx", {}, None))
    calls, seen = [], []

    def _backend(*a, **k):
        calls.append(1)
        return {"bias": "UP", "dol": {"level": "x", "price": 99999.0}}, {}

    g = TraderGraft(tmp_path / "trader", _backend, threaded=False,
                    order_sink=lambda ev: seen.append(ev) or None)
    for hms in ("09:21:00", "09:22:00", "09:31:00", "10:00:00", "12:59:59"):
        now = _ts(hms)
        frame = _live_frame(now, 29250.0, 29250.0, 29250.0)
        g.on_bar(now, frame, frame.copy())
    assert calls == [] and g.plan() is None and seen == []

    # Control: the same graft shape DOES arm when it sees the 09:20 minute.
    g2 = TraderGraft(tmp_path / "trader2", _backend, threaded=False,
                     order_sink=lambda ev: None)
    now = _ts("09:20:00")
    frame = _live_frame(now, 29250.0, 29250.0, 29250.0)
    g2.on_bar(now, frame, frame.copy())
    assert calls == [1]


def test_nothing_fires_at_093029_and_a_mechanism_may_at_093030(tmp_path, monkeypatch):
    """Case 40g, on the LIVE frame shape: a cumulative minute row plus the raw second."""
    book = FakeBook()
    g = _armed_graft(tmp_path, monkeypatch, _port(tmp_path, book))
    _live_bar(g, "09:30:29", 29250.0, fire=True, minute_hi=29290.0, minute_lo=29210.0)
    assert book.signals == [] and g._executor._market.asked == 0
    assert g._executor._market.fire is not None, "still in the settle window: not asked"

    _live_bar(g, "09:30:30", 29250.0, fire=True, minute_hi=29290.0, minute_lo=29210.0)
    assert [s["kind"] for s in book.signals] == ["market-entry"]
    assert book.signals[0]["time"] == _ts("09:30:30").isoformat()


def test_the_raw_second_not_the_cumulative_minute_decides_a_stop(tmp_path, monkeypatch):
    """F1, end to end. The minute's Low was printed BEFORE the fill; a stop test against
    it would book a stop-out the replay never sees."""
    book = FakeBook()
    g = _armed_graft(tmp_path, monkeypatch, _port(tmp_path, book))
    _live_bar(g, "09:44:01", 29250.0, fire=True, minute_lo=29200.0)   # low is pre-fill
    assert g.position_view() is not None
    _live_bar(g, "09:44:02", 29251.0, minute_lo=29200.0)             # raw second: quiet
    assert g.position_view() is not None
    assert [s["kind"] for s in book.signals] == ["market-entry"]
    _live_bar(g, "09:44:03", 29240.0, raw_lo=29230.0, minute_lo=29200.0)
    assert [s["kind"] for s in book.signals] == ["market-entry", "market-close"]


# --------------------------------------------------------------------------- #
# cases 37-40d: automation/main.py wiring                                       #
# --------------------------------------------------------------------------- #

class _FakePipeline:
    last: dict = {}

    def __init__(self, mnq, mes, emit, ai_decisions=None, trade_primary=None,
                 trader=None, **kwargs):
        _FakePipeline.last = {"emit": emit, "trader": trader, "kwargs": dict(kwargs)}

    def on_session_start(self, now, today_at_open, force_reset=False):
        pass

    def on_1m_bar(self, *a, **kw):
        return []


def _hist():
    idx = pd.date_range("2026-09-03 09:15", periods=6, freq="1min", tz=TZ)
    return pd.DataFrame({"Open": 29250.0, "High": 29260.0, "Low": 29240.0,
                         "Close": 29252.0, "Volume": 100.0}, index=idx)


@pytest.fixture()
def _wired(monkeypatch):
    import session_pipeline
    monkeypatch.setattr(session_pipeline, "SessionPipeline", _FakePipeline)
    monkeypatch.setattr(main, "_smtv2_pipeline", "v2")
    for var in ("ACT_TRADER", "ACT_AI_MODE", "FORCE_RESET", "ACT_TRADER_BACKEND",
                "ACT_TRADER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    # The other two session-start builders are not under test, and the decisions worker
    # builds a REAL backend — which loads the developer's `.env` — whenever that env
    # says it is enabled. Neither may run here.
    monkeypatch.setenv("ACT_AI_DECISIONS", "0")
    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_worker",
                        staticmethod(lambda out_dir: None))
    monkeypatch.setattr(main.SmtV2Dispatcher, "_build_primary",
                        staticmethod(lambda out_dir, date: None))
    dispatched = []
    monkeypatch.setattr(live_orders, "dispatch", dispatched.append)
    monkeypatch.setattr(main, "_emit_v2_signal", lambda sig: None)
    return dispatched


def _start(monkeypatch, build=None):
    if build is not None:
        monkeypatch.setattr(main.SmtV2Dispatcher, "_build_trader", staticmethod(build))
    d = main.SmtV2Dispatcher()
    d.on_session_start(_ts("09:20:00"), _hist(), _hist())
    return d


LEGACY_ORDER = {"kind": "new-stop-entry", "direction": "up", "price": 1.0, "stop": 0.5}
AGENT_ENTRY = {"kind": "market-entry", "direction": "up", "price": 1.0, "stop": 0.5,
               "source": "agent"}


def test_the_agent_owns_the_dispatcher_by_default_and_a_legacy_order_is_dropped(
        tmp_path, monkeypatch, _wired):
    """Cases 37 + 40b — the default-on consequence of D25, pinned."""
    sentinel = FakeGraft()
    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: sentinel)
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}
    assert _FakePipeline.last["trader"] is sentinel and d._agent_port is not None
    assert d._agent_refused is None

    _FakePipeline.last["emit"](dict(LEGACY_ORDER))
    assert _wired == [], "a legacy order kind while the agent owns is DROPPED"
    rec = (tmp_path / "sessions" / DATE / "agent_dispatch.jsonl").read_text("utf-8")
    assert json.loads(rec.splitlines()[-1])["suppressed"] == "legacy_order_kind"

    d._emit(dict(AGENT_ENTRY))
    d._emit({"kind": "new-hypothesis", "direction": "up"})     # log-only: passes
    assert [s["kind"] for s in _wired] == ["market-entry", "new-hypothesis"]


def test_the_sink_is_the_ports_and_reaches_the_dispatcher(monkeypatch, _wired):
    captured = {}

    def _build(out_dir, sink=None, date=None):
        captured["sink"] = sink
        return FakeGraft()

    d = _start(monkeypatch, _build)
    assert captured["sink"] == d._agent_port.sink
    d._agent_port._read_active = lambda: {"contracts": 1}
    captured["sink"](_fill())
    assert [s["kind"] for s in _wired] == ["market-entry"]


def test_supervise_and_the_raw_second_reach_the_trader(monkeypatch, _wired):
    graft = FakeGraft(position=HELD)
    graft.raw = []
    graft.set_raw_second = graft.raw.append
    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: graft)
    d._agent_port._read_active = lambda: {}
    d.forward_raw_second(SimpleNamespace(last_mnq_second={"high": 2.0, "low": 1.0}))
    d.supervise(_ts("09:50:00"))
    assert graft.raw == [{"high": 2.0, "low": 1.0}]
    assert [k[1] for k in graft.kills] == ["active_cleared_externally"]


def test_the_live_bar_loop_calls_supervise_even_when_the_pipeline_raises(monkeypatch):
    """The per-second path calls the pipeline directly, bypassing the dispatcher's own
    wrapper — so that is where the hook has to be, in a `finally`."""
    src = inspect.getsource(main._on_bar)
    call = src.index("_smtv2_dispatcher._pipeline.on_1m_bar(")
    assert src.index("try:", call - 40) < call < src.index("finally:", call)
    assert src.index("_smtv2_dispatcher.supervise(_bar_ts)") > src.index("finally:", call)
    assert src.index("forward_raw_second(_ib_source)") < call


@pytest.mark.parametrize("env,needle", [
    ({"ACT_AI_MODE": "primary"}, "ACT_AI_MODE=primary"),
    ({"ACT_AI_MODE": " PRIMARY "}, "ACT_AI_MODE=primary"),
    ({"FORCE_RESET": "true"}, "FORCE_RESET=true"),
])
def test_a_config_refusal_prints_one_line_installs_no_sink_and_emits_nothing(
        monkeypatch, _wired, capsys, env, needle):
    """Cases 38 + 39 + 40c."""
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    built = []
    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: built.append(1) or FakeGraft())
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("[AGENT-LIVE]")]
    assert len(lines) == 1 and lines[0].startswith("[AGENT-LIVE] REFUSED: ")
    assert needle in lines[0]
    lines[0].encode("ascii")
    assert built == [] and d._trader is None, "no trader, so no sink installed"
    assert _FakePipeline.last["trader"] is None
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}, "legacy STAYS dark"
    _FakePipeline.last["emit"](dict(LEGACY_ORDER))
    d.supervise(_ts("09:50:00"))
    assert _wired == [], "a refused start emits nothing and never falls back"


def test_smt_pipeline_v1_is_a_refusal(monkeypatch, _wired, capsys):
    """Case 39."""
    monkeypatch.setattr(main, "_smtv2_pipeline", "v1")
    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: FakeGraft())
    assert "SMT_PIPELINE is not v2" in capsys.readouterr().out
    assert d._trader is None
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}


def test_under_v1_a_refused_process_keeps_the_v1_brain_dark(monkeypatch):
    processed = []
    monkeypatch.setattr(main, "_process", processed.append)
    monkeypatch.setattr(main, "_smtv2_pipeline", "v1")
    monkeypatch.setattr(main, "_agent_refused_v1", "SMT_PIPELINE is not v2")
    main._on_bar(object(), None)
    assert processed == []
    monkeypatch.setattr(main, "_agent_refused_v1", None)      # ACT_TRADER=0: as before
    main._on_bar("bar", None)
    assert processed == ["bar"]


def test_a_trader_that_fails_to_build_or_is_not_built_is_a_refusal(monkeypatch, _wired,
                                                                   capsys):
    """Case 39 — FAILED is not OFF: the reason is printed, legacy stays dark."""
    def _boom(out_dir, sink=None, date=None):
        raise RuntimeError("No API key found")

    d = _start(monkeypatch, _boom)
    assert "[AGENT-LIVE] REFUSED: trader build failed: RuntimeError: No API key found" \
        in capsys.readouterr().out
    assert d._trader is None and d._agent_owns is True
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}

    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: None)
    assert "REFUSED: the trader was not built" in capsys.readouterr().out
    assert _FakePipeline.last["kwargs"] == {"trader_only": True}


def test_a_manual_market_entry_still_reaches_the_broker_after_a_refusal(
        monkeypatch, _wired, _no_broker_no_real_state):
    """Case 40c (D20): `trade.py` runs in ANOTHER process and calls the order module
    directly. A refused agent start must leave that path, and the pause file, alone."""
    monkeypatch.setenv("FORCE_RESET", "true")
    monkeypatch.setenv("TRADING_CONTRACTS", "1")
    d = _start(monkeypatch, lambda out_dir, sink=None, date=None: FakeGraft())
    assert d._agent_refused is not None
    assert live_orders.is_paused() is False, "a refusal must not engage the pause"
    live_orders.place_market_entry("long", 29250.0, 29235.0, source="manual")
    assert _no_broker_no_real_state.place_entry.call_count == 1
    assert _active()["source"] == "manual"


def test_act_trader_off_is_todays_flag_off_process(monkeypatch, _wired, capsys):
    """Case 40: legacy owns. No `trader_only`, no port, no sink, nothing dropped."""
    monkeypatch.setenv("ACT_TRADER", "0")
    d = _start(monkeypatch)                                     # the REAL build: it is OFF
    assert _FakePipeline.last["kwargs"] == {} and _FakePipeline.last["trader"] is None
    assert d._agent_port is None and d._agent_owns is False and d._trader is None
    assert "[AGENT-LIVE]" not in capsys.readouterr().out
    _FakePipeline.last["emit"](dict(LEGACY_ORDER))
    assert [s["kind"] for s in _wired] == ["new-stop-entry"], "legacy orders go out"
    d.supervise(_ts("09:50:00"))                               # a no-op
    d.forward_raw_second(SimpleNamespace(last_mnq_second={"high": 1.0}))


def _real_build(monkeypatch):
    """The REAL `_build_trader`, with the backends replaced by markers and the worktree
    `.env` loader disabled — this test must not read it."""
    import agent.run_agent as ra
    monkeypatch.setattr(ra, "load_env_file", lambda path: None)
    monkeypatch.setattr(ra, "OpenRouterBackend", lambda key, model: ("openrouter", model))
    monkeypatch.setattr(ra, "AnthropicBackend", lambda key, model: ("anthropic", model))
    seen = []
    monkeypatch.setattr(analyzer_mod, "thesis_via_decide_thesis",
                        lambda backend, **k: seen.append(backend) or (lambda *a, **kw: None))
    return seen


def test_backend_unset_auto_selects_by_whichever_key_exists(tmp_path, monkeypatch,
                                                            _wired):
    """Case 40d (D21)."""
    seen = _real_build(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-key")
    graft = main.SmtV2Dispatcher._build_trader(tmp_path / "t", sink=lambda ev: None,
                                               date="2026-09-03")
    assert seen == [("anthropic", None)] and isinstance(graft, TraderGraft)
    assert graft._order_sink is not None

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-not-a-key")
    main.SmtV2Dispatcher._build_trader(tmp_path / "t2", sink=lambda ev: None,
                                      date="2026-09-03")
    assert seen[-1] == ("openrouter", None), "with both keys, OpenRouter is preferred"


def test_no_model_key_at_all_is_a_refusal_with_the_reason(monkeypatch, _wired, capsys):
    """Case 40d (D21/F12): not a silent None."""
    _real_build(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = _start(monkeypatch)
    out = capsys.readouterr().out
    assert "[AGENT-LIVE] REFUSED: trader build failed: RuntimeError: No API key found" \
        in out
    [line] = [l for l in out.splitlines() if l.startswith("[AGENT-LIVE]")]
    line.encode("ascii")
    assert d._trader is None and _FakePipeline.last["kwargs"] == {"trader_only": True}


def test_refusal_reason_is_none_for_a_clean_v2_environment():
    assert ad.refusal_reason({"SMT_PIPELINE": "v2"}) is None
    assert ad.refusal_reason({"SMT_PIPELINE": "v2", "FORCE_RESET": "false"}) is None
    assert ad.refusal_reason({}) is not None, "SMT_PIPELINE unset means v1"
    assert ad.refused_line("a—b  c") == "[AGENT-LIVE] REFUSED: a?b c"
