import pandas as pd

from agent.trader.order_sim import OrderSim, RestingOrder

TZ = "America/New_York"


def _bar(o, h, l, c):
    return {"Open": o, "High": h, "Low": l, "Close": c}


def _t(hhmm):
    return pd.Timestamp(f"2026-08-13 {hhmm}", tz=TZ)


def _long():
    return RestingOrder(direction="UP", trigger=29900.0, stop=29880.0,
                        artifact_id="g1", placed_at=_t("09:31"))


def _short():
    return RestingOrder(direction="DOWN", trigger=29800.0, stop=29820.0,
                        artifact_id="g2", placed_at=_t("09:31"))


def test_a_resting_long_does_not_fill_below_its_trigger():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    assert s.on_bar(_t("09:32"), _bar(29890.0, 29895.0, 29885.0, 29890.0)) == []
    assert s.position is None
    assert s.resting is not None


def test_a_resting_long_fills_at_its_trigger_when_the_tape_reaches_it():
    """§11's calibrated convention: a resting stop fills AT its price."""
    s = OrderSim(dol=30001.5)
    s.place(_long())
    ev = s.on_bar(_t("09:32"), _bar(29890.0, 29910.0, 29885.0, 29905.0))
    assert [e["kind"] for e in ev] == ["fill"]
    assert ev[0]["price"] == 29900.0
    assert s.position["direction"] == "UP"


def test_a_resting_short_fills_at_its_trigger():
    s = OrderSim(dol=29000.0)
    s.place(_short())
    ev = s.on_bar(_t("09:32"), _bar(29810.0, 29815.0, 29790.0, 29795.0))
    assert ev[0]["kind"] == "fill" and ev[0]["price"] == 29800.0


def test_a_filled_long_stops_out_at_its_stop():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    s.on_bar(_t("09:32"), _bar(29890.0, 29910.0, 29885.0, 29905.0))
    ev = s.on_bar(_t("09:33"), _bar(29905.0, 29906.0, 29875.0, 29878.0))
    assert [e["kind"] for e in ev] == ["stop_out"]
    assert ev[0]["price"] == 29880.0
    assert s.position is None


def test_a_filled_long_takes_profit_at_the_dol():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    s.on_bar(_t("09:32"), _bar(29890.0, 29910.0, 29885.0, 29905.0))
    ev = s.on_bar(_t("09:36"), _bar(29950.0, 30010.0, 29940.0, 30005.0))
    assert [e["kind"] for e in ev] == ["take_profit"]
    assert ev[0]["price"] == 30001.5


def test_a_bar_that_touches_both_stop_and_dol_resolves_as_the_stop():
    """Same-bar ambiguity resolves adversely — never book the better of the two."""
    s = OrderSim(dol=30001.5)
    s.place(_long())
    s.on_bar(_t("09:32"), _bar(29890.0, 29910.0, 29885.0, 29905.0))
    ev = s.on_bar(_t("09:33"), _bar(29905.0, 30010.0, 29875.0, 29900.0))
    assert [e["kind"] for e in ev] == ["stop_out"]


def test_a_bar_that_fills_and_stops_in_the_same_bar_emits_both_adversely():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    ev = s.on_bar(_t("09:32"), _bar(29890.0, 29915.0, 29870.0, 29875.0))
    assert [e["kind"] for e in ev] == ["fill", "stop_out"]


def test_cancel_removes_a_resting_order_without_filling_it():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    s.cancel()
    assert s.resting is None
    assert s.on_bar(_t("09:32"), _bar(29890.0, 29999.0, 29885.0, 29990.0)) == []


def test_placing_a_second_order_replaces_the_first():
    """§2's single stop-entry policy: only one resting order exists at a time."""
    s = OrderSim(dol=30001.5)
    s.place(_long())
    second = RestingOrder(direction="UP", trigger=29950.0, stop=29930.0,
                          artifact_id="g3", placed_at=_t("09:33"))
    s.place(second)
    assert s.resting.artifact_id == "g3"


def test_a_market_fill_uses_the_supplied_price():
    """Crossed-trigger execution fills at the 1s mid at placement, not at a trigger."""
    s = OrderSim(dol=30001.5)
    ev = s.fill_market(_t("09:32"), direction="UP", price=29907.5, stop=29885.0,
                       artifact_id="g4")
    assert ev["kind"] == "fill" and ev["price"] == 29907.5
    assert s.position["stop"] == 29885.0


def test_the_last_stop_out_is_retained_for_the_cooldown():
    s = OrderSim(dol=30001.5)
    s.place(_long())
    s.on_bar(_t("09:32"), _bar(29890.0, 29910.0, 29885.0, 29905.0))
    s.on_bar(_t("09:33"), _bar(29905.0, 29906.0, 29875.0, 29878.0))
    assert s.last_stop_out["time"] == _t("09:33")
    assert s.last_stop_out["artifact_id"] == "g1"


def test_no_events_are_emitted_while_flat_with_nothing_resting():
    s = OrderSim(dol=30001.5)
    assert s.on_bar(_t("09:32"), _bar(29890.0, 29999.0, 29800.0, 29900.0)) == []
