# tests/test_fill_executor.py
# Unit tests for SimulatedFillExecutor: verifies fill-price computation for all
# entry/exit order types, slippage modes, and fills_sink callback behaviour.
from unittest.mock import MagicMock
import pytest
import pandas as pd

from execution.simulated import SimulatedFillExecutor
from execution.protocol import FillRecord
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
    ex = SimulatedFillExecutor(entry_slip_ticks=2)
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert rec.fill_price == 20000.0 + 2 * 0.25


def test_market_entry_short_applies_slippage():
    ex = SimulatedFillExecutor(entry_slip_ticks=2)
    rec = ex.place_entry(_signal("short", 20000.0), _bar())
    assert rec.fill_price == 20000.0 - 2 * 0.25


def test_limit_entry_exact_price():
    ex = SimulatedFillExecutor()
    rec = ex.place_entry(_signal("long", 20000.0, limit=True), _bar())
    assert rec.fill_price == 20000.0
    assert rec.order_type == "limit"


def test_human_mode_additive_slippage_long():
    ex = SimulatedFillExecutor(human_mode=True, human_slip_pts=3.0, entry_slip_ticks=2)
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert rec.fill_price == 20000.0 + 2 * 0.25 + 3.0


def test_human_mode_additive_slippage_short():
    ex = SimulatedFillExecutor(human_mode=True, human_slip_pts=3.0, entry_slip_ticks=2)
    rec = ex.place_entry(_signal("short", 20000.0), _bar())
    assert rec.fill_price == 20000.0 - 2 * 0.25 - 3.0


# Exit tests

def test_exit_tp_exact_price():
    ex = SimulatedFillExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_tp", _bar())
    assert rec.fill_price == pos["take_profit"]
    assert rec.order_type == "limit"


def test_exit_secondary_exact_price():
    ex = SimulatedFillExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_secondary", _bar())
    assert rec.fill_price == pos["secondary_target"]
    assert rec.order_type == "limit"


def test_exit_stop_exact_price():
    ex = SimulatedFillExecutor()
    pos = _position("long", 20000.0)
    rec = ex.place_exit(pos, "exit_stop", _bar())
    assert rec.fill_price == pos["stop_price"]
    assert rec.order_type == "stop"


def test_exit_market_bar_mid_no_slip():
    ex = SimulatedFillExecutor(pessimistic=False)
    bar = _bar(high=20010.0, low=19990.0)
    rec = ex.place_exit(_position("long"), "exit_market", bar)
    assert rec.fill_price == (20010.0 + 19990.0) / 2.0


def test_exit_market_pessimistic_long():
    ex = SimulatedFillExecutor(pessimistic=True, market_slip_pts=5.0)
    bar = _bar(high=20010.0, low=19990.0)
    mid = (20010.0 + 19990.0) / 2.0
    rec = ex.place_exit(_position("long"), "exit_market", bar)
    assert rec.fill_price == mid - 5.0


def test_exit_market_pessimistic_short():
    ex = SimulatedFillExecutor(pessimistic=True, market_slip_pts=5.0)
    bar = _bar(high=20010.0, low=19990.0)
    mid = (20010.0 + 19990.0) / 2.0
    rec = ex.place_exit(_position("short"), "exit_market", bar)
    assert rec.fill_price == mid + 5.0


# Callback and field tests

def test_fills_sink_called_on_entry():
    sink = MagicMock()
    ex = SimulatedFillExecutor(fills_sink=sink)
    rec = ex.place_entry(_signal("long"), _bar())
    sink.assert_called_once_with(rec)


def test_fills_sink_called_on_exit():
    sink = MagicMock()
    ex = SimulatedFillExecutor(fills_sink=sink)
    rec = ex.place_exit(_position("long"), "exit_tp", _bar())
    sink.assert_called_once_with(rec)


def test_fill_record_fields_populated():
    ex = SimulatedFillExecutor(symbol="MNQ1!")
    rec = ex.place_entry(_signal("long", 20000.0), _bar())
    assert isinstance(rec, FillRecord)
    assert rec.symbol == "MNQ1!"
    assert rec.direction == "long"
    assert rec.status == "filled"
    assert rec.fill_time is not None
    assert rec.order_id.startswith("sim-")
    assert rec.session_date == "2026-04-30"


def test_start_stop_no_op():
    ex = SimulatedFillExecutor()
    ex.start()
    ex.stop()


def test_unknown_exit_type_raises_value_error():
    ex = SimulatedFillExecutor()
    with pytest.raises(ValueError, match="Unrecognised exit_type"):
        ex.place_exit(_position("long"), "exit_bogus", _bar())
