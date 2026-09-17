"""Plan 38 Wave 1: `OrderPort` and `MirroringOrderPort`.

The mirror is the only thing standing between the brain's position model and a real
order, so what is pinned here is what it SENDS, what it does when the far side says no,
and that it cannot be talked into naming the far side.
"""
import inspect

import pandas as pd
import pytest

from agent.trader import order_port
from agent.trader.order_port import VOIDED_KIND, MirroringOrderPort, OrderPort
from agent.trader.order_sim import OrderSim, RestingOrder

TZ = "America/New_York"


def _ts(hms):
    return pd.Timestamp(f"2026-09-03 {hms}", tz=TZ)


def _bar(px, lo=None, hi=None):
    return pd.Series({"Open": px, "High": hi if hi is not None else px,
                      "Low": lo if lo is not None else px, "Close": px, "Volume": 1.0})


class _Sink:
    def __init__(self, acks=None, raises=False):
        self.events = []
        self._acks = list(acks or [])
        self._raises = raises

    def __call__(self, event):
        self.events.append(event)
        if self._raises:
            raise RuntimeError("far side down")
        return self._acks.pop(0) if self._acks else None


def _port(sink, mechanism="extreme_reject_close"):
    ctx = {"plan_id": "p38", "mechanism": mechanism}
    return MirroringOrderPort(OrderSim(dol=None), sink, context=lambda: dict(ctx)), ctx


def test_order_sim_satisfies_the_port_unchanged():
    assert isinstance(OrderSim(dol=None), OrderPort)
    port, _ = _port(_Sink())
    assert isinstance(port, OrderPort)


def test_a_fill_reaches_the_sink_once_with_stop_plan_mechanism_and_seq():
    """Case 1."""
    sink = _Sink()
    port, _ = _port(sink)
    ev = port.fill_market(_ts("09:40:00"), direction="UP", price=29250.0, stop=29235.0,
                          artifact_id="extreme_reject_close")
    assert len(sink.events) == 1
    sent = sink.events[0]
    assert sent["kind"] == "fill" and sent["stop"] == 29235.0
    assert sent["plan_id"] == "p38" and sent["mechanism"] == "extreme_reject_close"
    assert sent["seq"] == 1 and sent["direction"] == "UP" and sent["price"] == 29250.0
    assert sent["time"] == _ts("09:40:00")
    # The Executor gets the simulation's OWN event back — nothing the mirror added may
    # leak into the decision stream.
    assert ev["kind"] == "fill" and "seq" not in ev and "stop" not in ev
    assert port.position is not None and port.external is None


def test_a_resting_fill_carries_the_resting_orders_stop():
    sink = _Sink()
    port, _ = _port(sink, mechanism="fvg_return_continuation")
    port.place(RestingOrder("UP", 29241.0, 29216.0, "gapA", _ts("09:31:00")))
    evs = port.on_bar(_ts("09:32:11"), _bar(29245.0, lo=29238.0, hi=29247.0))
    assert [e["kind"] for e in evs] == ["fill"]
    assert sink.events[0]["stop"] == 29216.0 and sink.events[0]["artifact_id"] == "gapA"


@pytest.mark.parametrize("kind", ["stop_out", "take_profit", "mark"])
def test_each_close_reaches_the_sink_once_with_the_entry(kind):
    """Case 2."""
    sink = _Sink()
    port, ctx = _port(sink)
    port.fill_market(_ts("09:40:00"), direction="UP", price=29250.0, stop=29235.0,
                     artifact_id="x")
    # The Executor clears its mechanism when the plan dies with the position still open;
    # the close must still be attributed to the mechanism that opened it.
    ctx["mechanism"] = None
    if kind == "stop_out":
        evs = port.on_bar(_ts("09:41:00"), _bar(29240.0, lo=29230.0))
    elif kind == "take_profit":
        port.set_target(29300.0)
        evs = port.on_bar(_ts("09:41:00"), _bar(29290.0, hi=29301.0))
    else:
        evs = [port.mark_open(_ts("13:00:00"), 29260.0)]
    assert [e["kind"] for e in evs] == [kind]
    assert [e["kind"] for e in sink.events] == ["fill", kind]
    close = sink.events[1]
    assert close["entry"] == 29250.0 and close["seq"] == 2
    assert close["mechanism"] == "extreme_reject_close" and close["plan_id"] == "p38"
    assert port.position is None


def test_two_fills_sharing_one_artifact_id_both_reach_the_sink():
    """Case 3 (F7): a market mechanism with no gap reuses its own name as the id."""
    sink = _Sink()
    port, _ = _port(sink)
    for hms, px in (("09:40:00", 29250.0), ("09:55:00", 29270.0)):
        port.fill_market(_ts(hms), direction="UP", price=px, stop=px - 15.0,
                         artifact_id="extreme_reject_close")
        port.on_bar(_ts(hms) + pd.Timedelta(minutes=1), _bar(px - 20.0, lo=px - 20.0))
    assert [e["kind"] for e in sink.events] == ["fill", "stop_out", "fill", "stop_out"]
    assert {e["artifact_id"] for e in sink.events} == {"extreme_reject_close"}
    assert [e["seq"] for e in sink.events] == [1, 2, 3, 4]
    assert len({(e["plan_id"], e["seq"]) for e in sink.events}) == 4


def test_a_refused_fill_voids_the_position_and_sets_external():
    """Case 4."""
    sink = _Sink(acks=[{"ok": False, "reason": "dispatch_failed"}])
    port, _ = _port(sink)
    ev = port.fill_market(_ts("09:40:00"), direction="UP", price=29250.0, stop=29235.0,
                          artifact_id="x")
    assert port.position is None and port.target is None
    assert port.external == {"reason": "dispatch_failed", "kind": "fill"}
    assert ev["kind"] == VOIDED_KIND and ev["reason"] == "dispatch_failed"
    assert port.last_fill is None


def test_a_voided_ack_voids_the_fill_but_is_not_an_external_change():
    """D23: entries paused on the far side. The plan lives."""
    sink = _Sink(acks=[{"ok": False, "reason": "entry_refused", "voided": True}])
    port, _ = _port(sink)
    ev = port.fill_market(_ts("09:40:00"), direction="UP", price=29250.0, stop=29235.0,
                          artifact_id="x")
    assert ev["kind"] == VOIDED_KIND
    assert port.position is None and port.external is None


def test_a_voided_resting_fill_drops_its_same_bar_stop_out_and_starts_no_cooldown():
    sink = _Sink(acks=[{"ok": False, "reason": "entry_refused", "voided": True}])
    port, _ = _port(sink, mechanism="fvg_return_continuation")
    port.place(RestingOrder("UP", 29241.0, 29216.0, "gapA", _ts("09:31:00")))
    # One bar reaches the trigger AND the stop: the simulation books both.
    evs = port.on_bar(_ts("09:32:11"), _bar(29230.0, lo=29210.0, hi=29247.0))
    assert [e["kind"] for e in evs] == [VOIDED_KIND]
    assert [e["kind"] for e in sink.events] == ["fill"]
    assert port.last_stop_out is None and port.position is None


def test_a_raising_sink_is_a_refusal_and_nothing_propagates():
    """Case 5."""
    port, _ = _port(_Sink(raises=True))
    ev = port.fill_market(_ts("09:40:00"), direction="DOWN", price=29250.0,
                          stop=29265.0, artifact_id="x")
    assert ev["kind"] == VOIDED_KIND
    assert port.position is None
    assert port.external["reason"].startswith("sink_raised")


def test_a_failed_close_ack_does_not_resurrect_the_position():
    """Case 6."""
    sink = _Sink(acks=[None, {"ok": False, "reason": "close_not_confirmed"}])
    port, _ = _port(sink)
    port.fill_market(_ts("09:40:00"), direction="UP", price=29250.0, stop=29235.0,
                     artifact_id="x")
    evs = port.on_bar(_ts("09:41:00"), _bar(29240.0, lo=29230.0))
    assert [e["kind"] for e in evs] == ["stop_out"]
    assert port.position is None
    assert port.external == {"reason": "close_not_confirmed", "kind": "stop_out"}
    assert port.last_stop_out is not None, "the stop-out happened; the cooldown stands"


def test_the_module_names_no_legacy_module_docstrings_included():
    """Case 7. Raw source, not the AST: a docstring mention is already too close."""
    src = inspect.getsource(order_port)
    for token in ("live_" + "orders", "smt_state", "session_pipeline", "pickmytrade",
                  "get_et_now", "datetime.now"):
        assert token not in src, token


def test_the_module_imports_only_the_standard_library():
    import ast
    tree = ast.parse(inspect.getsource(order_port))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {"__future__", "typing"}, mods
