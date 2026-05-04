# tests/test_fill_executor.py
# Unit tests for SimulatedBrokerExecutor: verifies fill-price computation for all
# entry/exit order types, slippage modes, and fills_sink callback behaviour.
from unittest.mock import MagicMock
import pytest
import pandas as pd

from execution.simulated import SimulatedBrokerExecutor
from execution.protocol import FillRecord, assumed_fill_price
from strategy_smt import _BarRow


def _bar(high=20005.0, low=19995.0, close=20000.0, volume=100.0) -> _BarRow:
    ts = pd.Timestamp("2026-04-30 10:00:00", tz="America/New_York")
    return _BarRow(20000.0, high, low, close, volume, ts)


def _signal(direction="long", price=20000.0, limit=False):
    s = {
        "direction": direction,
        "entry_price": price,
        "take_profit": 20100.0,
        "stop_price": 19950.0,
        "secondary_target": 20080.0,
        "contracts": 1,
    }
    if limit:
        s["limit_fill_bars"] = 3
    return s


def _position(direction="long", entry=20000.0):
    return {
        "direction": direction,
        "entry_price": entry,
        "assumed_entry": entry,
        "take_profit": 20100.0,
        "stop_price": 19950.0,
        "secondary_target": 20080.0,
        "partial_price": None,
        "contracts": 1,
    }


# Entry tests

def test_market_entry_long_applies_slippage():
    ex = SimulatedBrokerExecutor(entry_slip_ticks=2)
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert rec.fill_price == 20000.0 + 2 * 0.25


def test_market_entry_short_applies_slippage():
    ex = SimulatedBrokerExecutor(entry_slip_ticks=2)
    rec = ex.place_entry(_signal("short", 20000.0), _bar())
    assert rec.fill_price == 20000.0 - 2 * 0.25


def test_limit_entry_exact_price():
    ex = SimulatedBrokerExecutor()
    rec = ex.place_entry(_signal("long", 20000.0, limit=True), _bar())
    assert rec.fill_price == 20000.0
    assert rec.order_type == "limit"


def test_human_mode_additive_slippage_long():
    ex = SimulatedBrokerExecutor(human_mode=True, human_slip_pts=3.0, entry_slip_ticks=2)
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert rec.fill_price == 20000.0 + 2 * 0.25 + 3.0


def test_human_mode_additive_slippage_short():
    ex = SimulatedBrokerExecutor(human_mode=True, human_slip_pts=3.0, entry_slip_ticks=2)
    rec = ex.place_entry(_signal("short", 20000.0), _bar())
    assert rec.fill_price == 20000.0 - 2 * 0.25 - 3.0


# Exit tests

def test_exit_tp_exact_price():
    ex = SimulatedBrokerExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_tp", _bar())
    assert rec.fill_price == pos["take_profit"]
    assert rec.order_type == "limit"


def test_exit_secondary_exact_price():
    ex = SimulatedBrokerExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_secondary", _bar())
    assert rec.fill_price == pos["secondary_target"]
    assert rec.order_type == "limit"


def test_exit_stop_exact_price():
    ex = SimulatedBrokerExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_stop", _bar())
    assert rec.fill_price == pos["stop_price"]
    assert rec.order_type == "stop"


def test_exit_market_bar_mid_no_slip():
    ex = SimulatedBrokerExecutor(pessimistic=False)
    bar = _bar(high=20010.0, low=19990.0)
    rec = ex.place_exit(_position("long"), "exit_market", bar)
    assert rec.fill_price == (20010.0 + 19990.0) / 2.0


def test_exit_market_pessimistic_long():
    ex = SimulatedBrokerExecutor(pessimistic=True, market_slip_pts=5.0)
    bar = _bar(high=20010.0, low=19990.0)
    mid = (20010.0 + 19990.0) / 2.0
    rec = ex.place_exit(_position("long"), "exit_market", bar)
    assert rec.fill_price == mid - 5.0


def test_exit_market_pessimistic_short():
    ex = SimulatedBrokerExecutor(pessimistic=True, market_slip_pts=5.0)
    bar = _bar(high=20010.0, low=19990.0)
    mid = (20010.0 + 19990.0) / 2.0
    rec = ex.place_exit(_position("short"), "exit_market", bar)
    assert rec.fill_price == mid + 5.0


# Callback and field tests

def test_fills_sink_called_on_entry():
    sink = MagicMock()
    ex = SimulatedBrokerExecutor(fills_sink=sink)
    rec = ex.place_entry(_signal("long"), _bar())
    sink.assert_called_once_with(rec)


def test_fills_sink_called_on_exit():
    sink = MagicMock()
    ex = SimulatedBrokerExecutor(fills_sink=sink)
    rec = ex.place_exit(_position("long"), "exit_tp", _bar())
    sink.assert_called_once_with(rec)


def test_fill_record_fields_populated():
    ex = SimulatedBrokerExecutor(symbol="MNQ1!")
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert isinstance(rec, FillRecord)
    assert rec.symbol == "MNQ1!"
    assert rec.direction == "long"
    assert rec.status == "filled"
    assert rec.fill_time is not None
    assert rec.order_id.startswith("sim-")
    assert rec.session_date == "2026-04-30"


def test_start_stop_no_op():
    ex = SimulatedBrokerExecutor()
    ex.start()
    ex.stop()


def test_unknown_exit_type_raises_value_error():
    ex = SimulatedBrokerExecutor()
    with pytest.raises(ValueError, match="Unrecognised exit_type"):
        ex.place_exit(_position("long"), "exit_bogus", _bar())


# assumed_fill_price utility

def test_assumed_fill_price_market_long():
    assert assumed_fill_price("long", "market", 20000.0, slip_ticks=2, tick_size=0.25) == pytest.approx(20000.5)


def test_assumed_fill_price_market_short():
    assert assumed_fill_price("short", "market", 20000.0, slip_ticks=2, tick_size=0.25) == pytest.approx(19999.5)


def test_assumed_fill_price_limit_no_slip():
    assert assumed_fill_price("long", "limit", 20000.0) == pytest.approx(20000.0)


def test_assumed_fill_price_stop_no_slip():
    assert assumed_fill_price("long", "stop", 20000.0) == pytest.approx(20000.0)


def test_assumed_fill_price_custom_tick_size():
    assert assumed_fill_price("long", "market", 20000.0, slip_ticks=3, tick_size=0.5) == pytest.approx(20001.5)


def test_assumed_fill_price_zero_slip():
    assert assumed_fill_price("long", "market", 20000.0, slip_ticks=0) == pytest.approx(20000.0)


def test_simulated_entry_uses_assumed_fill_price():
    # SimulatedBrokerExecutor.place_entry must agree with assumed_fill_price for market orders
    ex = SimulatedBrokerExecutor(entry_slip_ticks=3)
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert rec.fill_price == pytest.approx(assumed_fill_price("long", "market", 20000.0, slip_ticks=3))
