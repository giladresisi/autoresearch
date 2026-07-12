"""Session risk gate (spec §9) — deterministic, AI-untouchable.

Config-owned caps the executor enforces regardless of any AI output: daily loss limit,
max trades/session, halt-after-N-consecutive-losers, position-size cap. A breach latches
the gate `breached` for the rest of the session — no AI output (nor any decision input)
can relax it. The daily loss-limit default reuses `loss_limit.DEFAULT_LOSS_LIMIT` (the
same dollar cap the live monitor uses), keeping the backtest gate and the live pause
aligned on one number.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

# Reuse the live loss-limit dollar default without importing the live monitor's file/pause
# machinery (backtest must stay side-effect-free): fall back to -300.0 if unavailable.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
try:
    from loss_limit import DEFAULT_LOSS_LIMIT as _DEFAULT_LOSS_LIMIT
except Exception:                                   # pragma: no cover - defensive
    _DEFAULT_LOSS_LIMIT = -300.0


@dataclass
class RiskGateConfig:
    daily_loss_limit: float = _DEFAULT_LOSS_LIMIT   # halt when realized P&L <= this
    max_trades: Optional[int] = None                # halt after this many closed trades
    max_consecutive_losers: Optional[int] = None    # halt after N losers in a row
    max_position_size: Optional[int] = None         # per-entry contract cap (soft, no halt)


class RiskGate:
    """Tracks realized session P&L / trade count / loser streak and latches HALTED on a
    breach. `can_enter` is the executor's entry interlock; `record_exit` feeds outcomes."""

    def __init__(self, config: Optional[RiskGateConfig] = None):
        self.config = config or RiskGateConfig()
        self.realized_pnl: float = 0.0
        self.trade_count: int = 0
        self.consecutive_losers: int = 0
        self.breached: bool = False
        self.breach_reason: Optional[str] = None

    def record_exit(self, pnl: float) -> None:
        """Record one closed trade's realized P&L, then re-evaluate the caps. Once
        breached the gate stays breached (a subsequent winner cannot un-halt it)."""
        self.realized_pnl += float(pnl)
        self.trade_count += 1
        if pnl < 0:
            self.consecutive_losers += 1
        else:
            self.consecutive_losers = 0
        self._evaluate()

    def _evaluate(self) -> None:
        if self.breached:
            return
        c = self.config
        if self.realized_pnl <= c.daily_loss_limit:
            self._breach(f"daily_loss_limit ({self.realized_pnl:.2f} <= {c.daily_loss_limit:.2f})")
        elif c.max_trades is not None and self.trade_count >= c.max_trades:
            self._breach(f"max_trades ({self.trade_count} >= {c.max_trades})")
        elif (c.max_consecutive_losers is not None
              and self.consecutive_losers >= c.max_consecutive_losers):
            self._breach(f"max_consecutive_losers ({self.consecutive_losers} "
                         f">= {c.max_consecutive_losers})")

    def _breach(self, reason: str) -> None:
        self.breached = True
        self.breach_reason = reason

    def trip(self, reason: str) -> None:
        """Force a breach from an external signal (e.g. the live loss-limit monitor firing
        on unrealized drawdown, mid-position). Idempotent; keeps the first reason latched."""
        if not self.breached:
            self._breach(reason)

    def can_enter(self, size: int = 1) -> bool:
        """True iff a new entry of `size` contracts is permitted right now. A breach hard-
        blocks; the position-size cap blocks the oversized entry WITHOUT latching a halt
        (the executor can retry with a smaller size)."""
        if self.breached:
            return False
        if self.config.max_position_size is not None and size > self.config.max_position_size:
            return False
        return True
