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
from agent.trader.operator_control import (KIND_RESET_TARGET, KIND_SET_DIRECTION,
                                           KIND_SET_TARGET, OperatorControl)
from agent.trader.order_port import MirroringOrderPort
from agent.trader.order_sim import OrderSim
from agent.trader.plan_store import PlanStore
from agent.trader.planner import derive_plan
from agent.trader.records import DecisionRecorder

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
                 threaded: bool = True, arrival_latency_sec: float = 0.0,
                 order_sink=None, htf_extremes=None) -> None:
        """`arrival_latency_sec` withholds the thesis until `armed_at + latency` in BAR
        time. 0.0 (the default, and what live constructs) is cycle-1 behaviour exactly;
        cycle-2 replay sets it to reproduce, deterministically, the wall-clock delay live
        gets for free from running the call on a thread.

        `order_sink` is what makes a session LIVE. With one, the Executor is built on a
        `MirroringOrderPort` that reports every simulated order event to it, and a
        restart is refused (see `_disarmed`). Without one — every replay, and every
        caller that predates plan 38 — nothing below changes: the Executor gets its
        default `OrderSim` and no plan on disk can stop the chain arming.

        `htf_extremes` (plan 40) is `htf_source.load_session_extremes`'s result, computed
        by the caller from the 1m parquet at the session open. It is written to the run
        folder once, here, and the plan ticker's part is handed to every Executor."""
        self.state_dir = str(state_dir)
        self._htf_ctx = None
        if htf_extremes is not None:
            from agent.facts.htf_source import executor_context, write_artifact
            # Neither may cost the session its trader (a live REFUSED start): the
            # artifact is a record, and a context that cannot be built means T2 selects
            # exactly as it did before plan 40.
            try:
                write_artifact(state_dir, htf_extremes)
            except Exception:
                pass
            try:
                self._htf_ctx = executor_context(htf_extremes, PLAN_TICKER)
            except Exception:
                self._htf_ctx = None
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
        self._order_sink = order_sink
        self._rec = DecisionRecorder(state_dir)
        # The last finalized RAW second, handed over by the live driver just before the
        # bar it belongs to (see `set_raw_second`). Consumed once.
        self._raw_second = None
        # What an outside supervisor polls. `on_bar` swallows every exception and the
        # Executor swallows order-book failures into its own state, so without this a
        # broken chain is indistinguishable from a quiet one.
        self._health = {"last_error": None, "last_bar": None}
        # Why this session will never (again) act, or None. A LIVE restart is the case
        # that sets it at construction: the Analyzer restores its thesis from disk and a
        # plan id hashes its own arm instant, so the next bar close would silently derive
        # a SECOND plan with a fresh attempt budget and no memory of any position the
        # first one opened. A plan already on disk is the evidence that one existed.
        self._disarmed = None
        self._disarm_recorded = False
        # Plan 41: the operator's control channel. `_control_seq` is the last record
        # applied, so a re-read cannot apply one twice and a command written while the
        # loop was busy is picked up on the next bar close rather than lost.
        self._control = OperatorControl(state_dir)
        self._control_seq = 0
        if order_sink is not None and self._plans.all():
            self._disarmed = "restart"

    # -- the hook ------------------------------------------------------------- #

    def on_bar(self, now, today_mnq, today_mes, bar_complete=None,
               hist_mnq=None, hist_mes=None) -> None:
        """Never raises. The bar loop's own try/except is a backstop, not the guard.

        What it swallows is kept in `health()` — nothing outside this method can see it
        any other way."""
        try:
            self._health["last_bar"] = now
            self._run(now, today_mnq, today_mes, bar_complete, hist_mnq, hist_mes)
        except Exception as exc:
            self._health["last_error"] = f"{type(exc).__name__}: {exc}"

    # -- internals ------------------------------------------------------------ #

    def _bar_closed(self, now, bar_complete) -> bool:
        """Derived from a minute rollover; `bar_complete` is an override, not the sole
        source. The live driver never sets it. First call only seeds the minute."""
        minute = now.floor("1min")
        rolled = self._last_minute is not None and minute != self._last_minute
        self._last_minute = minute
        return bool(bar_complete) or rolled

    def set_raw_second(self, row) -> None:
        """Hand over the last finalized RAW second of the plan ticker, for the NEXT
        `on_bar` only. A mapping with `high` / `low` and, when known, `second_ts`.

        The live driver's partial-minute row is CUMULATIVE for the minute; replay's
        carries the raw per-second High/Low, and that is the basis the fill model, the
        stop test and the market-fill mid were calibrated against. See `_frames`."""
        self._raw_second = row

    def _frames(self, now, today_mnq, today_mes, hist_mnq, hist_mes,
                raw_mnq=None) -> dict:
        """today_* spliced onto the pipeline's history, bounded to what the Executor
        needs. The history slice is cached — it does not change within a session.

        `raw_mnq` overlays the LAST plan-ticker row's High/Low with one raw second's, so
        a live frame has replay's shape. No-op when absent, and when it names a second
        other than `now` — a stale second must never repaint a later bar."""
        out = self._spliced(now, today_mnq, today_mes, hist_mnq, hist_mes)
        if raw_mnq is not None:
            frame = _overlay_last_row(out.get(PLAN_TICKER), raw_mnq, now)
            if frame is not None:
                out[PLAN_TICKER] = frame
        return out

    def _spliced(self, now, today_mnq, today_mes, hist_mnq, hist_mes) -> dict:
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
        raw, self._raw_second = self._raw_second, None
        if now is None or self._analyzer is None:
            return
        closed = self._bar_closed(now, bar_complete)
        if self._disarmed is not None:
            # Recorded on the first bar rather than at construction, so the record
            # carries BAR time like every other one.
            if not self._disarm_recorded:
                self._disarm_recorded = True
                self._rec.session_disarmed(
                    now=now, reason=self._disarmed,
                    plan_id=(self._plan or {}).get("plan_id"))
            return
        bars = self._frames(now, today_mnq, today_mes, hist_mnq, hist_mes, raw_mnq=raw)

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
                                      maintainer=self._maint, requirement=self._req,
                                      order_port=self._order_port(),
                                      htf_extremes=self._htf_ctx)
            # The arming bar itself must still get one maintenance pass: under the
            # default switch the block above ran before the Executor existed, so this
            # bar would otherwise be skipped entirely.
            if not FACTS_ALL_SESSION:
                self._maint.on_bar(now, bars, closed)

        # Plan 41: the operator's commands are applied HERE — after the plan exists and
        # before the Executor runs, so a direction change takes effect on this bar rather
        # than one bar late, and only on a bar close (no wall clock, and never mid-bar).
        if closed:
            self._drain_operator(now, bars, thesis)

        if self._executor is not None:
            self._executor.on_bar(now, bars, bar_complete=closed)

        # Last, so the arming bar is covered too: under the default switch the
        # maintenance block above runs BEFORE the Executor exists, so a snapshot taken
        # there would skip the very bar the plan armed on.
        if FACTS_ALL_SESSION or self._executor is not None:
            self._maybe_snapshot(now, closed)

    # -- plan 41: operator overrides ------------------------------------------ #

    def _drain_operator(self, now, bars, thesis) -> None:
        """Apply every control record written since the last drain. Never raises.

        Each record is recorded with BAR time and its verdict, accepted or not: a refused
        command is the operator believing the session is in a state it is not, and they
        have to be able to see that in the artifact (and in the log line the recorder
        announces) rather than wait for a change that never came."""
        try:
            pending = self._control.pending(self._control_seq)
        except Exception:
            return
        for rec in pending:
            # NOT YET DUE: a record carries the wall instant the CLI wrote it, and it is
            # applied on the first bar at or after that instant. Live this is a no-op
            # (bar time tracks the clock, so the next bar close is due immediately); in a
            # REPLAY of the recorded file it is the whole point — without it every
            # override would land on the replay's first bar close instead of the bar it
            # was issued on, and the replay would not reproduce the session. Records are
            # in seq order, which is time order, so the first undue one stops the drain
            # and leaves itself and its successors pending.
            if not _due(rec, now):
                break
            seq = int(rec.get("seq") or 0)
            self._control_seq = max(self._control_seq, seq)
            kind = str(rec.get("kind") or "")
            try:
                if kind == KIND_SET_DIRECTION:
                    res = self._apply_direction(now, bars, thesis, rec)
                elif kind == KIND_SET_TARGET:
                    res = self._apply_target(now, rec)
                elif kind == KIND_RESET_TARGET:
                    res = (self._executor.reset_target(now)
                           if self._executor is not None
                           else {"accepted": False, "reason": "no_plan"})
                else:
                    res = {"accepted": False, "reason": f"unknown_command:{kind}"}
            except Exception as exc:
                res = {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
            self._rec.operator_override(
                now=now, plan_id=(self._plan or {}).get("plan_id"), command=kind,
                accepted=bool(res.get("accepted")), reason=res.get("reason"),
                detail={**(res.get("detail") or {}),
                        **{k: v for k, v in rec.items() if k not in ("kind", "seq")}},
                seq=seq)

    def _apply_direction(self, now, bars, thesis, rec) -> dict:
        """Kill the standing plan and derive a new one in the requested direction.

        A NEW plan, not a mutated one: the plan id hashes its arm instant, `valid_while`
        and `armed_classes` are derived from the thesis direction, and the Executor binds
        against the plan it was constructed with. Mutating the dict in place would leave
        an Executor whose state (bindings, cooldown, blacklist) belongs to the other side.

        ATTEMPTS CARRY OVER by default (operator's call): a wrong-way morning must not be
        able to turn into six stop-outs. `reset_attempts` asks for a fresh budget.
        """
        want = str(rec.get("direction") or "").upper()
        if want not in ("UP", "DOWN"):
            return {"accepted": False, "reason": "bad_direction"}
        if thesis is None or self._plan is None:
            return {"accepted": False, "reason": "no_plan"}
        if self._executor is not None and self._executor.has_position():
            return {"accepted": False, "reason": "open_position"}
        if str(self._plan.get("direction") or "").upper() == want:
            return {"accepted": False, "reason": "already_that_direction"}

        used = 0 if rec.get("reset_attempts") else (
            self._executor.attempts_used() if self._executor is not None
            else int(self._plan.get("attempts_used") or 0))
        old_id = self._plan.get("plan_id")
        if self._executor is not None:
            self._executor.kill_plan(now, "operator_direction_change",
                                     {"to": want, "seq": rec.get("seq")})
        plan = self._derive({**dict(thesis), "bias": want}, bars, now)
        if plan is None:
            return {"accepted": False, "reason": "derive_failed"}
        plan["attempts_used"] = int(used)
        plan["operator_direction"] = want
        plan["replaces_plan_id"] = old_id
        self._plan = plan
        self._plans.put(plan)
        self._executor = Executor(self.state_dir, plan, arm_ts=now,
                                  maintainer=self._maint, requirement=self._req,
                                  order_port=self._order_port(),
                                  htf_extremes=self._htf_ctx)
        return {"accepted": True,
                "detail": {"direction": want, "plan_id": plan.get("plan_id"),
                           "replaces_plan_id": old_id, "attempts_used": int(used)}}

    def _apply_target(self, now, rec) -> dict:
        """Set the bound target. `level` names a row of the CURRENT menu (re-priced at
        this bar); `price` is the explicit-price path the CLI gates behind a flag."""
        if self._executor is None:
            return {"accepted": False, "reason": "no_plan"}
        price = rec.get("price")
        level = rec.get("level")
        if price is None and level:
            rows = self._executor.refresh_menu(now)
            match = next((r for r in rows
                          if str(r.get("id")) == str(level)
                          or str(r.get("level")) == str(level)), None)
            if match is None:
                return {"accepted": False, "reason": "level_not_in_menu",
                        "detail": {"level": level,
                                   "menu": [r.get("level") for r in rows]}}
            price, level = match.get("price"), match.get("level")
        if price is None:
            return {"accepted": False, "reason": "no_price"}
        return self._executor.set_target_override(now, price, level=level)

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

    def _order_port(self):
        """None without a sink — the Executor then builds its own `OrderSim`, which is
        every replay. With one, the simulation is wrapped so each event it returns is
        reported outward. The context is read lazily: the Executor does not exist yet."""
        if self._order_sink is None:
            return None
        return MirroringOrderPort(
            OrderSim(dol=None), self._order_sink,
            context=lambda: (self._executor.order_context()
                             if self._executor is not None
                             else {"plan_id": (self._plan or {}).get("plan_id")}))

    # -- the outside supervisor's surface (live only) ------------------------- #

    def health(self) -> dict:
        """`last_error`: what `on_bar` last swallowed. `order_error`: what the Executor's
        order book last swallowed. `last_bar`: the last bar handed in. Sticky — a fault
        that cleared itself is still a fault the supervisor has to hear about."""
        order_error = None
        if self._executor is not None:
            order_error = (self._executor.bind_state() or {}).get("order_error")
        return {"last_error": self._health["last_error"], "order_error": order_error,
                "last_bar": self._health["last_bar"]}

    def position_view(self) -> "dict | None":
        """The modelled position, or None. A copy: the supervisor reads, never writes."""
        return self._executor.position() if self._executor is not None else None

    def plan_alive(self) -> bool:
        return (self._executor is not None
                and bool((self._executor.bind_state() or {}).get("plan_alive")))

    def disarmed(self):
        return self._disarmed

    def external_kill(self, now, reason, void_position: bool = False) -> None:
        """The position changed and this chain did not change it. Stand down.

        Three pieces of state, in an order that matters: the PLAN DIES FIRST, so nothing
        can re-enter from this instant whatever happens next; then the modelled position
        is voided when the caller has established it is gone; then the record. The record
        is in a `finally` — if the void raises, why the session went quiet must still
        reach the disk. Never raises."""
        plan_id = (self._plan or {}).get("plan_id")
        failure = None
        try:
            if self._executor is not None:
                self._executor.kill_plan(now, "external_position_change",
                                         {"reason": reason})
                if void_position:
                    self._executor.void_position()
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            self._health["last_error"] = failure
        finally:
            self._rec.external_kill(now=now, plan_id=plan_id, reason=reason,
                                    void_position=void_position,
                                    detail={"error": failure} if failure else None)

    def disarm(self, now, reason) -> None:
        """Stop for the session: no further Analyzer, facts or Executor work. The
        caller has already dealt with any open position — nothing here manages one."""
        if self._disarmed is not None:
            return
        self._disarmed = str(reason)
        self._disarm_recorded = True
        self._rec.session_disarmed(now=now, reason=self._disarmed,
                                   plan_id=(self._plan or {}).get("plan_id"))

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

    def mark_open_position(self):
        """Book a position still open when the RUN ends. The runner's call, made after
        the bar loop — see `Executor.mark_open_position`. None when nothing was open,
        which includes a dark day with no Executor at all."""
        return (self._executor.mark_open_position()
                if self._executor is not None else None)

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

    def htf_loaded(self) -> bool:
        """Plan 40: whether this session's Executors get the HTF extremes."""
        return self._htf_ctx is not None

    @property
    def maintainer(self) -> FactsMaintainer:
        return self._maint


def _due(rec: dict, now) -> bool:
    """Is this control record due at bar instant `now`?

    A record with no (or an unparseable) `created_at` is due immediately: the stamp is a
    replay-fidelity aid, and a missing one must never strand an operator's command in a
    live session."""
    raw = rec.get("created_at")
    if not raw:
        return True
    try:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize(now.tz)
        return ts <= now
    except Exception:
        return True


def _as_frame(df):
    return df if df is not None else pd.DataFrame()


def _overlay_last_row(frame, raw, now):
    """A COPY of `frame` with its last row's High/Low replaced by `raw`'s, or None for a
    no-op. Always a copy: the frame may be the caller's, or the cached history slice."""
    try:
        if frame is None or not len(frame):
            return None
        second = raw.get("second_ts")
        if second is not None and pd.Timestamp(second) != now:
            return None
        hi, lo = raw.get("high"), raw.get("low")
        if hi is None or lo is None:
            return None
        cols = {str(c).lower(): i for i, c in enumerate(frame.columns)}
        if "high" not in cols or "low" not in cols:
            return None
        frame = frame.copy()
        frame.iloc[-1, cols["high"]] = float(hi)
        frame.iloc[-1, cols["low"]] = float(lo)
        return frame
    except Exception:
        return None


def _last_close(df) -> float:
    try:
        cols = {str(c).lower(): c for c in df.columns}
        return float(df[cols["close"]].iloc[-1])
    except Exception:
        return 0.0
