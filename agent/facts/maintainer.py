"""Session-scoped facts maintenance: the store, the cascade, and coverage.

**Why this is not inside the Executor.** Facts are SESSION state; a plan is not. The
store must be able to keep accumulating when no plan is armed (before the Analyzer has
run, on a dark day, and after a plan has died), because the next thing that needs it
should not start from an empty store. Keeping the store inside a plan-scoped object
made all three of those cases freeze it.

The split is the one the design settled on, one layer down: the PLAN layer changes only
on thesis events, the BINDING layer churns freely, and FACTS underlie both.

`FACTS_ALL_SESSION` (in `agent.trader.graft`) decides whether the graft drives this from
the session's first bar or only once a plan arms. This class does not care — it is
driven by whoever owns it, and is correct either way.

`run_incremental` is reached through its MODULE so a monkeypatched detector is actually
exercised, and so a raising detector is demonstrably swallowed rather than taking the
bar loop down — same reason as the Executor's original call site.
"""
from __future__ import annotations

import pandas as pd

import agent.facts.incremental as _incremental
from agent.facts.bars import resample
from agent.facts.detectors._common import normalize, truncate
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.facts.store import FactStore, ensure_coverage

# Enough 1m bars to contain 20 completed 1h bars plus slack for the maintenance break
# and a partial trailing hour.
ATR_TAIL = pd.Timedelta(hours=30)
ATR_BARS = 20


class FactsMaintainer:
    """Owns the FactStore, the incremental detector state, and the ATR proxy.

    `on_bar` is total: it never raises, because it sits on the live per-second callback
    that also drives the legacy engine's order execution.
    """

    def __init__(self, *, store=None, requirement=EXECUTOR_REQUIREMENT,
                 ticker: str = "MNQ") -> None:
        self._store = store if store is not None else FactStore()
        self._req = requirement
        self._ticker = ticker
        self._inc_state: dict = {}
        self._avg_range_1h = None
        self._last_cascade = None
        self._now_price = None

    # -- accessors ------------------------------------------------------------- #

    @property
    def store(self) -> FactStore:
        return self._store

    @property
    def avg_range_1h(self):
        return self._avg_range_1h

    @property
    def last_cascade(self):
        return self._last_cascade

    @property
    def now_price(self):
        return self._now_price

    def state(self) -> dict:
        return {"last_cascade": self._last_cascade,
                "avg_range_1h": self._avg_range_1h,
                "now_price": self._now_price}

    # -- the driver ------------------------------------------------------------ #

    def on_bar(self, now: pd.Timestamp, bars: dict, bar_complete: bool) -> None:
        """One maintenance pass: cascade, then (on bar close) ATR + coverage.

        `bar_complete` here is the ALREADY-DERIVED bar-close verdict, not the raw flag
        from the driver — the caller owns rollover detection so the Executor and the
        maintainer cannot disagree about whether a minute closed.
        """
        if now is None:
            return

        mnq = truncate(normalize((bars or {}).get(self._ticker)), now)
        if len(mnq):
            self._now_price = float(mnq["Close"].iloc[-1])

        # The cascade. Swallowed: a detector must never take the bar loop down.
        try:
            self._inc_state = _incremental.run_incremental(
                self._store, self._inc_state, bars or {}, now, bool(bar_complete))
        except Exception:
            pass

        if not bar_complete:
            return                                    # per-second path ends here

        self._last_cascade = str(now)

        try:
            self._refresh_avg_range(mnq)
            ensure_coverage(self._store, bars or {}, self._req, now,
                            price=self._now_price or 0.0,
                            avg_range_1h=self._avg_range_1h)
        except Exception:
            pass

    # -- internals -------------------------------------------------------------- #

    def _refresh_avg_range(self, mnq: pd.DataFrame) -> None:
        """Mean true range of the last 20 completed 1h bars — the coverage envelope's
        ATR proxy. Recomputed on bar close only.

        Resamples a 30-HOUR TAIL, not the whole frame. The frame carries session history
        (~24k rows) and this needs 20 bars off the end of it; resampling the lot cost
        hundreds of ms on every bar close.
        """
        try:
            if len(mnq) == 0:
                return
            tail_bars = mnq[mnq.index >= mnq.index[-1] - ATR_TAIL]
            h1 = resample(tail_bars, "1h")
            if len(h1) == 0:
                return
            tail = h1.tail(ATR_BARS)
            self._avg_range_1h = float((tail["High"] - tail["Low"]).mean())
        except Exception:
            pass
