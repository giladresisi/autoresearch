# execution/protocol.py
# FillRecord dataclass and FillExecutor Protocol — shared contract for all fill implementations.
# All executors (simulated, live) implement FillExecutor so callers are decoupled from fill mechanics.
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from strategy_smt import _BarRow as BarRow


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
