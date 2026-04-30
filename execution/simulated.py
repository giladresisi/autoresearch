# execution/simulated.py
# SimulatedFillExecutor: fills orders synchronously using bar OHLCV data.
# Extracts fill-price logic from backtest_smt._open_position() and _build_trade_record()
# so both the backtest harness and signal_smt share identical fill semantics.
from __future__ import annotations
import datetime
import uuid

from execution.protocol import FillRecord, BarRow

# All exit_type values that produce a market fill (bar mid ± slippage).
# Any value not in this set or the four exact-price types raises ValueError.
_MARKET_EXIT_TYPES = frozenset({
    "exit_time", "session_close", "exit_market", "end_of_backtest",
    "exit_session_end",
    "exit_invalidation_mss", "exit_invalidation_cisd",
    "exit_invalidation_smt", "exit_invalidation_opposing_disp",
})


class SimulatedFillExecutor:
    """Synchronous fill executor for backtests and live signal display.

    Computes fill prices from bar data without any async delay.
    market_slip_pts and pessimistic affect exits only; entry slippage is tick-based.
    """

    def __init__(
        self,
        *,
        pessimistic: bool = False,
        market_slip_pts: float = 5.0,
        v2_market_slip_pts: float = 2.0,
        human_mode: bool = False,
        human_slip_pts: float = 0.0,
        entry_slip_ticks: int = 2,
        symbol: str = "MNQ1!",
        fills_sink=None,
    ) -> None:
        self._pessimistic        = pessimistic
        self._market_slip_pts    = market_slip_pts
        self._v2_market_slip_pts = v2_market_slip_pts
        self._human_mode         = human_mode
        self._human_slip_pts     = human_slip_pts
        self._entry_slip_ticks   = entry_slip_ticks
        self._symbol             = symbol
        self._fills_sink         = fills_sink

    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord:
        is_limit = signal.get("limit_fill_bars") is not None
        if is_limit:
            fill_price = float(signal["entry_price"])
            order_type = "limit"
        else:
            slip = self._entry_slip_ticks * 0.25
            if signal["direction"] == "long":
                fill_price = float(signal["entry_price"]) + slip
            else:
                fill_price = float(signal["entry_price"]) - slip
            order_type = "market"
        # Human-mode: additive slippage on top of tick slippage (long pays more, short receives less)
        if self._human_mode and self._human_slip_pts > 0:
            if signal["direction"] == "long":
                fill_price += self._human_slip_pts
            else:
                fill_price -= self._human_slip_pts
        rec = FillRecord(
            order_id=f"sim-{uuid.uuid4().hex[:8]}",
            symbol=self._symbol,
            direction=signal["direction"],
            order_type=order_type,
            requested_price=float(signal["entry_price"]),
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=signal.get("contracts", 1),
            status="filled",
            session_date=str(bar.name.date()) if bar.name is not None and hasattr(bar.name, "date") else "",
        )
        if self._fills_sink is not None:
            self._fills_sink(rec)
        return rec

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> FillRecord:
        direction_sign = 1 if position["direction"] == "long" else -1
        if exit_type == "exit_tp":
            fill_price = float(position["take_profit"])
            order_type = "limit"
        elif exit_type == "exit_secondary":
            fill_price = float(position["secondary_target"])
            order_type = "limit"
        elif exit_type == "exit_stop":
            fill_price = float(position["stop_price"])
            order_type = "stop"
        elif exit_type == "partial_exit":
            partial = position.get("partial_price")
            fill_price = float(partial) if partial is not None else float(bar.Close)
            order_type = "limit"
        elif exit_type in _MARKET_EXIT_TYPES:
            mid = (float(bar.High) + float(bar.Low)) / 2.0
            if self._pessimistic:
                fill_price = mid - direction_sign * self._market_slip_pts
            else:
                fill_price = mid
            order_type = "market"
        else:
            raise ValueError(f"Unrecognised exit_type: {exit_type!r}")
        rec = FillRecord(
            order_id=f"sim-{uuid.uuid4().hex[:8]}",
            symbol=self._symbol,
            direction=position["direction"],
            order_type=order_type,
            requested_price=fill_price,
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=position.get("contracts", 1),
            status="filled",
            session_date=str(bar.name.date()) if bar.name is not None and hasattr(bar.name, "date") else "",
        )
        if self._fills_sink is not None:
            self._fills_sink(rec)
        return rec

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
