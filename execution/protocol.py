# execution/protocol.py
# FillRecord dataclass, assumed_fill_price utility, and FillExecutor Protocol.
# All executors (simulated, live) implement FillExecutor so callers are decoupled from fill mechanics.
from __future__ import annotations
import datetime
from dataclasses import dataclass
from typing import Protocol

from strategy_smt import _BarRow as BarRow


def assumed_fill_price(
    direction: str,
    order_type: str,
    reference_price: float,
    slip_ticks: int = 2,
    tick_size: float = 0.25,
    bar_time: "datetime.time | None" = None,
) -> float:
    """Estimate fill price with tick-based entry slippage.

    Market orders: 3 ticks (calibrated from live PMT relay observations).
    Stop orders: time-based slippage — 4 ticks before 11:00 ET (higher volatility),
        1 tick at or after 11:00 ET.  bar_time=None uses the pessimistic 4-tick default.
    Limit and all other order types: fill at reference_price unchanged.
    Long direction: slippage is adverse (adds to price).
    Short direction: slippage is adverse (subtracts from price).
    """
    if order_type == "market":
        effective_ticks = 3
    elif order_type == "stop":
        cutoff = datetime.time(11, 0)
        if bar_time is None or bar_time < cutoff:
            effective_ticks = 4
        else:
            effective_ticks = 1
    else:
        return reference_price
    slip = effective_ticks * tick_size
    return reference_price + slip if direction == "long" else reference_price - slip


@dataclass
class FillRecord:
    order_id:        str
    symbol:          str
    direction:       str           # "long" | "short"
    order_type:      str           # "market" | "limit" | "stop"
    requested_price: float
    fill_price:      float | None  # None = pending (async executors only)
    fill_time:       str | None    # ISO-8601 string
    contracts:       int
    status:          str           # "pending" | "filled" | "rejected"
    session_date:    str           # "YYYY-MM-DD"


class FillExecutor(Protocol):
    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord | None: ...
    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> FillRecord | None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def place_close(self, label: str = "close") -> None: ...
    def modify_stop_entry(self, old_signal: dict, new_signal: dict, bar: BarRow) -> None: ...
