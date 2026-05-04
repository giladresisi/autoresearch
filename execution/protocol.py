# execution/protocol.py
# FillRecord dataclass, assumed_fill_price utility, and FillExecutor Protocol.
# All executors (simulated, live) implement FillExecutor so callers are decoupled from fill mechanics.
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from strategy_smt import _BarRow as BarRow


def assumed_fill_price(
    direction: str,
    order_type: str,
    reference_price: float,
    slip_ticks: int = 2,
    tick_size: float = 0.25,
) -> float:
    """Estimate fill price with tick-based entry slippage for market orders.

    Limit and stop orders fill at the reference price by definition.
    Long market entries pay slip; short market entries receive less.
    """
    if order_type != "market":
        return reference_price
    slip = slip_ticks * tick_size
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
    def modify_limit_entry(self, old_signal: dict, new_signal: dict, bar: BarRow) -> None: ...
