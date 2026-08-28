"""The `SessionPipeline` hook. Additive, exception-swallowing, flag-gated.

Follows the ADDITIVE `_ai_decisions_on_bar` template, NOT the `trade_primary` hook:
the primary hook early-returns and bypasses the legacy engine entirely (one-brain),
and cycle 1 needs the legacy engine and this chain running side by side so their
intents can be compared.

Sequencing owned here:

  1. `Analyzer.maybe_run` — fires once at its own arm minute, on a background thread.
  2. `FactsMaintainer.on_bar` — the store, the cascade and coverage. Driven from the
     first bar when `FACTS_ALL_SESSION` is set, otherwise from the moment a plan arms.
     The graft drives it either way, so the Executor does not (its ownership rule) and
     it runs exactly once per bar.
  3. On a STANDING thesis with no plan yet: extend coverage, segment 5m legs, then
     `derive_plan`. The Planner needs legs, and the newest 5m bin may not have closed
     at the instant the thesis lands, so plan derivation is retried on later bars
     rather than forced immediately.
  4. `Executor.on_bar` every bar thereafter — binding and guards only.

No standing thesis ⇒ the whole chain stays dark (l2-mechanisms.md §2, decided
2026-08-17): no fallback DOL, no self-armed thesis.

Two things this hook must get right that the bar loop does NOT hand it:

  * **History.** `today_mnq` / `today_mes` are the CURRENT CME session only (~15 h).
    The Analyzer's window is 17 days and the Executor's level window is 14, so the
    pipeline's `hist_*` frames are passed alongside and spliced on. Without them
    `detect_levels` finds no prior session at all and the thesis is decided against an
    empty level universe — silently, because the view is still structurally valid.
  * **Bar close.** The live driver hard-codes `bar_complete=False` on every tick. The
    cascade trigger is therefore derived from a minute rollover, with the flag as an
    override. See `Executor.bar_closed`.
"""
from __future__ import annotations

import os

import pandas as pd

from agent.facts.bars import resample
from agent.facts.detectors.legs import segment_legs
from agent.facts.journal import Journal
from agent.facts.maintainer import FactsMaintainer
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.facts.store import ensure_coverage
from agent.trader.analyzer import Analyzer
from agent.trader.executor import Executor
from agent.trader.plan_store import PlanStore
from agent.trader.planner import derive_plan

TRADER_ENV_FLAG = "ACT_TRADER"

# Drive facts maintenance from the session's FIRST BAR rather than from the moment a
# plan arms.
#
# False (cycle-1 default) reproduces today's behaviour exactly: the store starts
# filling when the Analyzer produces a standing thesis and a plan is derived.
# True is the all-session shape — the store accumulates in parallel with the Analyzer's
# run and on dark days, and ONLY the order-placing half waits for the plan. Flipping
# this constant is the entire change; nothing else needs editing, which is what makes
# the eventual switch a decision rather than a refactor.
FACTS_ALL_SESSION = False
PLAN_TICKER = "MNQ"
# How much history the Executor is given. Its widest declared window is 14 days
# (LEVEL); a couple of days of slack absorbs weekends and holidays.
EXECUTOR_HISTORY = pd.Timedelta(days=17)


def trader_enabled() -> bool:
    """ON by default. `ACT_TRADER=0` (or false/no/off) is an explicit OPT-OUT.

    Every orchestrator session runs the chain — there is nothing to configure. The
    switch survives only as a kill switch: if the chain misbehaves mid-session (a
    hung model call, a detector throwing past its guard, a cascade over the cadence)
    it can be disabled with one environment variable and a restart, instead of a code
    change or a revert while the market is open. It is also how a regression run is
    kept free of a live model call once cycle 2 wires replay.
    """
    raw = str(os.environ.get(TRADER_ENV_FLAG, "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


class TraderGraft:
    def __init__(self, state_dir, backend, *, requirement=EXECUTOR_REQUIREMENT,
                 threaded: bool = True, arrival_latency_sec: float = 0.0) -> None:
        """`arrival_latency_sec` withholds the thesis until `armed_at + latency` in BAR
        time. 0.0 (the default, and what live constructs) is cycle-1 behaviour exactly;
        cycle-2 replay sets it to reproduce, deterministically, the wall-clock delay live
        gets for free from running the call on a thread."""
        self.state_dir = str(state_dir)
        self._req = requirement
        self._journal = Journal(state_dir)
        self._analyzer = (Analyzer(state_dir, backend, threaded=threaded,
                                   arrival_latency_sec=arrival_latency_sec)
                          if backend is not None else None)
        self._snapshot_date = None
        self._plans = PlanStore(state_dir)
        # Session-scoped, constructed up front. Whether it is DRIVEN before a plan arms
        # is FACTS_ALL_SESSION's call; it exists either way so the Executor always gets
        # the same object and there is exactly one store per session.
        self._maint = FactsMaintainer(requirement=requirement, ticker=PLAN_TICKER)
        self._executor = None
        self._plan = None
        self._last_minute = None
        self._hist: dict = {}

    # -- the hook ------------------------------------------------------------- #

    def on_bar(self, now, today_mnq, today_mes, bar_complete=None,
               hist_mnq=None, hist_mes=None) -> None:
        """Never raises. The bar loop's own try/except is a backstop, not the guard."""
        try:
            self._run(now, today_mnq, today_mes, bar_complete, hist_mnq, hist_mes)
        except Exception:
            pass

    # -- internals ------------------------------------------------------------ #

    def _bar_closed(self, now, bar_complete) -> bool:
        """Derived from a minute rollover; `bar_complete` is an override, not the sole
        source. The live driver never sets it. First call only seeds the minute."""
        minute = now.floor("1min")
        rolled = self._last_minute is not None and minute != self._last_minute
        self._last_minute = minute
        return bool(bar_complete) or rolled

    def _frames(self, now, today_mnq, today_mes, hist_mnq, hist_mes) -> dict:
        """today_* spliced onto the pipeline's history, bounded to what the Executor
        needs. The history slice is cached — it does not change within a session."""
        out = {}
        for tk, today, hist in (("MNQ", today_mnq, hist_mnq),
                                ("MES", today_mes, hist_mes)):
            if tk not in self._hist and hist is not None and len(hist):
                self._hist[tk] = hist[hist.index >= now - EXECUTOR_HISTORY]
            h = self._hist.get(tk)
            if h is None or not len(h):
                out[tk] = today
                continue
            if today is None or not len(today):
                out[tk] = h
                continue
            merged = pd.concat([h[h.index < today.index[0]], today])
            out[tk] = merged
        return out

    def _run(self, now, today_mnq, today_mes, bar_complete, hist_mnq, hist_mes) -> None:
        if now is None or self._analyzer is None:
            return
        closed = self._bar_closed(now, bar_complete)
        bars = self._frames(now, today_mnq, today_mes, hist_mnq, hist_mes)

        self._analyzer.maybe_run(now, bars)

        # Facts maintenance. Under FACTS_ALL_SESSION it runs from the first bar —
        # accumulating in parallel with the Analyzer's own (threaded) run and on dark
        # days. Otherwise it starts only once an Executor exists, which is cycle-1's
        # documented behaviour. Either way the GRAFT drives it, so the Executor knows
        # not to (its ownership rule), and it is driven exactly once per bar.
        if FACTS_ALL_SESSION or self._executor is not None:
            self._maint.on_bar(now, bars, closed)

        thesis = self._analyzer.standing_thesis(now=now)
        if thesis is None:
            return                                     # dark day — nothing to arm

        if self._plan is None:
            if not closed:
                return                                 # plan derivation is bar-close work
            self._plan = self._derive(thesis, bars, now)
            if self._plan is None:
                return
            self._plans.put(self._plan)
            self._executor = Executor(self.state_dir, self._plan, arm_ts=now,
                                      maintainer=self._maint, requirement=self._req)
            # The arming bar itself must still get one maintenance pass: under the
            # default switch the block above ran before the Executor existed, so this
            # bar would otherwise be skipped entirely.
            if not FACTS_ALL_SESSION:
                self._maint.on_bar(now, bars, closed)

        if self._executor is not None:
            self._executor.on_bar(now, bars, bar_complete=closed)

        # Last, so the arming bar is covered too: under the default switch the
        # maintenance block above runs BEFORE the Executor exists, so a snapshot taken
        # there would skip the very bar the plan armed on.
        if FACTS_ALL_SESSION or self._executor is not None:
            self._maybe_snapshot(now, closed)

    def _maybe_snapshot(self, now, closed) -> None:
        """One store snapshot per session date, on a bar close.

        Snapshots the SESSION store — the one the Executor actually binds against.
        (It used to snapshot a throwaway store the Analyzer built and then ignored,
        which described nothing anyone used.)
        """
        if not closed or self._snapshot_date == now.date():
            return
        self._snapshot_date = now.date()
        try:
            self._journal.snapshot(self._maint.store)
        except Exception:
            pass

    def _derive(self, thesis: dict, bars: dict, now) -> "dict | None":
        # Per-ticker prices. The maintainer's own dict is authoritative once it has run;
        # on the ARMING bar it has not (under FACTS_ALL_SESSION=False maintenance is
        # gated on an Executor existing), so the frames are read directly for whatever
        # it is still missing rather than judging every class against a missing price.
        prices = dict(self._maint.prices)
        for tkr, frame in (bars or {}).items():
            if prices.get(tkr) is None:
                px = _last_close(frame)
                if px:
                    prices[tkr] = px
        ensure_coverage(self._maint.store, bars, self._req, now,
                        prices=prices, atrs=self._maint.atrs)
        legs = segment_legs(resample(_as_frame(bars.get(PLAN_TICKER)), "5min"),
                            now, PLAN_TICKER)
        return derive_plan(thesis, legs, now)

    # -- introspection (tests / the comparison artifact) ---------------------- #

    def plan(self):
        return self._plan

    def last_bar_minute(self):
        """The floored minute of the LAST bar this graft was handed, or None.

        Set by `_bar_closed` at the top of `_run`, before any early return, so it tracks
        every bar the runner delivered — including bars after the plan died, which is
        exactly the distinction cycle-2's "the run continues past the DOL touch" gate
        needs to make. Every on-disk artifact stops earlier than the loop does.
        """
        return self._last_minute

    def bind_state(self):
        return self._executor.bind_state() if self._executor is not None else None

    def coverage_report(self) -> dict:
        """Per (class, ticker) coverage as of the last bar — the run artifact that made
        the 08-13 units bug visible. Never raises: it is diagnostic output."""
        try:
            return self._maint.coverage_report()
        except Exception:
            return {}

    @property
    def store(self):
        """The session store — readable before a plan arms, which is the point."""
        return self._maint.store

    @property
    def maintainer(self) -> FactsMaintainer:
        return self._maint


def _as_frame(df):
    return df if df is not None else pd.DataFrame()


def _last_close(df) -> float:
    try:
        cols = {str(c).lower(): c for c in df.columns}
        return float(df[cols["close"]].iloc[-1])
    except Exception:
        return 0.0
