"""Plan 33 Task 1: the entry feed — the real Executor, a synthetic plan, the FIRST fill.

**What this module is for.** Plan 33's policy simulator owns everything after the fill and
nothing before it (§4 (d)). The fill therefore has to come from the real entry mechanisms,
not from an oracle "best price before the extreme" — plan 31 §5 records what the oracle
convention costs: on 08-03 its 1m-open fill is stopped inside the entry bar itself, an
artifact of the convention rather than a result.

**The handoff is the emitted RECORD, not the order book.** The Executor's simulated order
lifecycle keeps its own stop and its own DOL take-profit; the policy simulator replaces
both. Reading the lifecycle's state here would give this module a second owner of the
stop, so it reads the Executor's own decision records instead: the fill event for the
time, price and direction, and the `bind` / `intended_entry` record for the artifact that
filled for the mechanism's initial stop. A test greps this file to keep it that way.

**The frames reproduce the 1s replay exactly.** In `mode="1s"` the backtest hands the
graft `today_*` as *completed 1m bars plus one running partial minute*, once per second,
with the pipeline's 1m history spliced on (`session_pipeline.py:1106`,
`backtest_smt.py:1637-1697`). It does NOT hand over 1s bars. `SessionTape` rebuilds that
shape directly, which is what makes an 84-session sweep affordable without a second copy
of any rule: the plan comes from the real `derive_plan`, the legs from the real
`segment_legs`, the binding and the fill from the real `Executor`.

**No DOL means no position** (§4 (a), clause 14). Production enforces that at the thesis
contract layer — `validate_contracts.py:98` refuses a directional thesis without
`dol.price` — and NOT in the Executor, which skips the DOL-floor veto entirely when the
plan carries no DOL and would therefore enter more freely without a target than with one.
Supplying DOLs synthetically bypasses that gate, so it is reproduced here explicitly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import paths
from agent.facts.bars import resample
from agent.facts.detectors.legs import segment_legs
from agent.trader.executor import Executor
from agent.trader.planner import derive_plan

TZ = "America/New_York"
CONTRACT_SUBDIR = "2026-09"
COLS = ("Open", "High", "Low", "Close", "Volume")
_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}

#: The ARM, and it is 09:21 rather than 09:20 deliberately. `replay.py` opens its window
#: at 09:20, but production's Executor does not exist then: the Analyzer's thesis arrives
#: ~40 s later (`replay.DEFAULT_ARRIVAL_LATENCY_SEC`) and `TraderGraft._run` derives the
#: plan on the NEXT bar close, so the first bar any Executor sees is 09:21. Arming at
#: 09:20 flat would hand the death check a minute production never judged the plan over:
#: measured on the corpus, that costs the production DOL track nothing (0 of 12 deaths)
#: but accounts for 5 of the oracle track's 23, which is a bias in exactly the numbers
#: the oracle track exists to bracket.
#:
#: The feed therefore STARTS at the arm, which is also what production does — under
#: `FACTS_ALL_SESSION = False` the graft drives no facts maintenance until an Executor
#: exists, so the store begins accumulating at the arm bar and not before.
ARM_ET = (9, 21)
#: Clause 10's hard close, and — since 2026-09-09 — `replay.WINDOW_END_ET` as well. The
#: two were 11:00 and 13:00 respectively while this feed was written, so the note here
#: used to explain why the feed ran two hours past the replay; they now agree.
WINDOW_END_ET = (13, 0)

#: `TraderGraft.EXECUTOR_HISTORY`. Bounded once, at the first bar, exactly as the graft
#: bounds it.
EXECUTOR_HISTORY = pd.Timedelta(days=17)
#: What `run_backtest_v2` reads before slicing (it caps 1s history at 60 days). 20 is
#: enough to survive the 17-day bound above plus weekends and holidays.
HIST_DAYS = 20

# Outcome codes. Every session lands on exactly one, and the yield report is their
# histogram: a corpus of "no fill" is not one outcome but four different sentences.
FILLED = "filled"
NO_DOL = "no_dol"                 # clause 14 -- no position on this track
DOL_BEHIND = "dol_behind_at_arm"  # the supplied DOL is not a forward draw
NO_TAPE = "no_tape"               # no 1s bars in the window
NO_MECHANISM = "no_mechanism"     # the Planner armed no entry mechanism
PLAN_DEAD = "plan_dead"           # DOL taken (or attempts spent) before any fill
NO_TRIGGER = "no_trigger"         # armed, bound perhaps, never triggered


@dataclass(frozen=True)
class Fill:
    """§4 (d)'s handoff, and nothing else."""
    date: str
    track: str
    direction: str
    ts: pd.Timestamp
    price: float
    stop: float
    mechanism: str
    artifact_id: str
    artifact_label: str
    #: The trigger the order rested at, and the UNCAPPED structural stop the bound gap
    #: implies. `stop` is `max(structural, trigger -/+ STOP_CAP_PTS)`, so on its own it
    #: cannot say whether the cap bound — and a stop-width study needs exactly that
    #: distinction: widening a stop and removing its structural anchoring are two
    #: different changes, and a fixed width does both at once.
    trigger: "float | None" = None
    structural_stop: "float | None" = None

    @property
    def cap_bound(self) -> "bool | None":
        """Did the 25-pt cap decide this stop, rather than the gap's own geometry?"""
        if self.structural_stop is None:
            return None
        return abs(self.stop - self.structural_stop) > 1e-9

    def to_dict(self) -> dict:
        return {"date": self.date, "track": self.track, "direction": self.direction,
                "ts": self.ts.isoformat(), "price": self.price, "stop": self.stop,
                "mechanism": self.mechanism, "artifact_id": self.artifact_id,
                "artifact_label": self.artifact_label, "trigger": self.trigger,
                "structural_stop": self.structural_stop, "cap_bound": self.cap_bound}


@dataclass
class FeedResult:
    date: str
    track: str
    direction: "str | None" = None
    dol: "float | None" = None
    reason: str = NO_TRIGGER
    fill: "Fill | None" = None
    all_fills: list = field(default_factory=list)
    plan_dead: "str | None" = None
    plan_dead_ts: "str | None" = None
    armed_classes: tuple = ()
    n_binds: int = 0
    veto_reasons: dict = field(default_factory=dict)
    executor_ran: bool = False
    last_bar: "str | None" = None

    @property
    def n_fills(self) -> int:
        return len(self.all_fills)

    def to_dict(self) -> dict:
        return {"date": self.date, "track": self.track, "direction": self.direction,
                "dol": self.dol, "reason": self.reason,
                "fill": None if self.fill is None else self.fill.to_dict(),
                "n_fills": self.n_fills, "plan_dead": self.plan_dead,
                "plan_dead_ts": self.plan_dead_ts,
                "armed_classes": list(self.armed_classes), "n_binds": self.n_binds,
                "veto_reasons": dict(self.veto_reasons),
                "executor_ran": self.executor_ran, "last_bar": self.last_bar}


class _Capture:
    """A `DecisionRecorder` that keeps records in memory and writes nothing.

    The production recorder appends to `trader_decisions.jsonl` AND prints an
    `[TRADER]` line per record. An 84-session sweep would produce tens of thousands of
    both. Same call surface, no I/O.
    """

    def __init__(self) -> None:
        self.records: list = []

    def _put(self, kind, now, plan_id, mechanism, extra) -> None:
        rec = {"kind": kind, "time": now, "plan_id": plan_id, "mechanism": mechanism}
        rec.update(extra)
        self.records.append(rec)

    def intended_entry(self, *, now, plan_id, mechanism, **extra) -> None:
        self._put("intended_entry", now, plan_id, mechanism, extra)

    def bind(self, *, now, plan_id, mechanism, **extra) -> None:
        self._put("bind", now, plan_id, mechanism, extra)

    def unbind(self, *, now, plan_id, mechanism, **extra) -> None:
        self._put("unbind", now, plan_id, mechanism, extra)

    def veto(self, *, now, plan_id, mechanism, **extra) -> None:
        self._put("veto", now, plan_id, mechanism, extra)

    def order_event(self, *, now, plan_id, mechanism, kind, **extra) -> None:
        extra.pop("time", None)
        self._put(str(kind), now, plan_id, mechanism, extra)

    def would_have_falsified(self, *, now, plan_id, **extra) -> None:
        self._put("would_have_falsified", now, plan_id, None, extra)

    def plan_dead(self, *, now, plan_id, **extra) -> None:
        self._put("plan_dead", now, plan_id, None, extra)


def structural_stop_for(store, artifact_id, direction, trigger, recorded_stop):
    """The bound gap's UNCAPPED structural stop, or None.

    `executor._levels_for` computes `structural = far_edge -/+ STOP_BUFFER_PTS` and then
    `stop = max(structural, trigger -/+ STOP_CAP_PTS)`. Only the capped result is
    recorded, so the structural half is re-derived here from the gap's own edges — which
    is one arithmetic expression, not a rule, and it is CHECKED rather than trusted: the
    recorded stop must equal the capped combination of what we derived, or we return None
    instead of a number nothing verified.

    Reads the facts STORE, never the order lifecycle. Facts are session state; the
    lifecycle is the thing §4 (d) keeps this module out of.
    """
    from agent.trader.executor import STOP_BUFFER_PTS, STOP_CAP_PTS

    try:
        gap = store.get(artifact_id)
    except Exception:
        return None
    if gap is None or trigger is None:
        return None
    try:
        long_ = str(direction).upper() in ("UP", "LONG")
        structural = (float(gap.price_low) - STOP_BUFFER_PTS if long_
                      else float(gap.price_high) + STOP_BUFFER_PTS)
        capped = (max(structural, float(trigger) - STOP_CAP_PTS) if long_
                  else min(structural, float(trigger) + STOP_CAP_PTS))
    except (TypeError, ValueError):
        return None
    if abs(capped - float(recorded_stop)) > 1e-6:
        return None                     # derivation disagrees with the record: report it
    return structural


def trigger_for(records, artifact_id, at):
    """The trigger from the same binding record `stop_for` reads."""
    best = None
    for rec in records:
        if rec.get("kind") not in ("bind", "intended_entry"):
            continue
        if rec.get("artifact_id") != artifact_id or rec.get("trigger") is None:
            continue
        ts = rec.get("time")
        if at is not None and ts is not None and pd.Timestamp(ts) > pd.Timestamp(at):
            continue
        best = rec
    return None if best is None else float(best["trigger"])


def stop_for(records, artifact_id, at) -> tuple:
    """`(stop, label)` for `artifact_id` from the LAST binding record at or before `at`.

    The binding layer churns freely (`executor.py:1073`), so "the most recent bind" and
    "the bind for the gap that actually filled" are different records on most sessions.
    `bind` and `intended_entry` both carry the pair `_levels_for(gap)` returns, and both
    are recomputed from the gap's fixed edges, so either is the mechanism's own stop.
    """
    best = None
    for rec in records:
        if rec.get("kind") not in ("bind", "intended_entry"):
            continue
        if rec.get("artifact_id") != artifact_id or rec.get("stop") is None:
            continue
        ts = rec.get("time")
        if at is not None and ts is not None and pd.Timestamp(ts) > pd.Timestamp(at):
            continue
        best = rec
    if best is None:
        return None, None
    return float(best["stop"]), best.get("artifact_label")


def build_plan(direction, dol, frame_at_arm, arm_ts) -> dict:
    """A synthetic thesis through the REAL Planner.

    `derive_plan` owns the arming rule (§3-§7: oppose the last trend -> negation, agree
    -> continuation), and `segment_legs` owns "the last trend". Hand-writing
    `armed_classes` here would be a second copy of both, and the mechanism that fires is
    exactly what this study is measuring the policy on top of.

    `falsified_if` is empty because nothing acts on falsification
    (`executor.py:478-502`); carrying a synthetic predicate would only add a record.
    """
    legs = segment_legs(resample(frame_at_arm, "5min"), arm_ts, "MNQ")
    thesis = {"thesis_id": "study-position-policy",
              "bias": str(direction).upper(),
              "dol": {"level": "study_dol", "price": float(dol)},
              "falsified_if": []}
    # `five_min=True`: this study measures the position policy ON TOP OF the 5m entry
    # path (`_entry_class` knows only those two names), so it opts out of the production
    # suspension explicitly rather than reporting NO_MECHANISM on every date.
    return derive_plan(thesis, legs, arm_ts, five_min=True)


def drive(plan, arm_ts, feed, *, date, track, dol=None) -> FeedResult:
    """Run the real Executor over `feed` and return the FIRST fill.

    `feed` yields `(now, {ticker: frame})` — whatever shape the caller reproduces. The
    Executor owns its own facts maintainer here (its documented standalone case), so the
    cascade runs exactly once per bar, before the binding.
    """
    res = FeedResult(date=str(date), track=str(track), dol=dol,
                     direction=(plan or {}).get("direction"),
                     armed_classes=tuple((plan or {}).get("armed_classes") or ()))
    rec = _Capture()
    ex = Executor(os.devnull, plan, arm_ts=arm_ts, recorder=rec, ticker="MNQ")
    res.executor_ran = True

    seen = 0
    for now, bars in feed:
        ex.on_bar(now, bars, bar_complete=False)
        res.last_bar = str(now)
        if len(rec.records) == seen:
            continue
        new, seen = rec.records[seen:], len(rec.records)
        for r in new:
            kind = r.get("kind")
            if kind == "bind":
                res.n_binds += 1
            elif kind == "veto":
                reason = r.get("reason")
                res.veto_reasons[reason] = res.veto_reasons.get(reason, 0) + 1
            elif kind == "plan_dead" and res.plan_dead is None:
                res.plan_dead = r.get("reason")
                res.plan_dead_ts = str(r.get("time"))
            elif kind == "fill":
                aid = r.get("artifact_id")
                stop, label = stop_for(rec.records, aid, r.get("time"))
                if stop is None:
                    # Every bound gap emits a `bind`, so a fill whose artifact has none
                    # means the handoff is broken. Returning a stopless Fill would push
                    # the failure downstream into the policy, where it reads as a session
                    # with no risk rather than as a defect.
                    raise RuntimeError(
                        f"{date}/{track}: fill on {aid} carries no bind record, so the "
                        "mechanism's own stop cannot be recovered")
                trig = trigger_for(rec.records, aid, r.get("time"))
                structural = structural_stop_for(
                    ex.maintainer.store, aid, r.get("direction"), trig, stop)
                res.all_fills.append(Fill(
                    trigger=trig, structural_stop=structural,
                    date=str(date), track=str(track),
                    direction=str(r.get("direction")).upper(),
                    ts=pd.Timestamp(r.get("time")), price=float(r.get("price")),
                    stop=stop, mechanism=r.get("mechanism"),
                    artifact_id=aid, artifact_label=label or r.get("artifact_label")))
        if res.all_fills:
            break                       # clause 1: one position per session, the first

    if res.all_fills:
        res.fill = res.all_fills[0]
        res.reason = FILLED
    elif not res.armed_classes or not _entry_class(res.armed_classes):
        res.reason = NO_MECHANISM
    elif res.plan_dead is not None:
        res.reason = PLAN_DEAD
    else:
        res.reason = NO_TRIGGER
    return res


def _entry_class(armed) -> "str | None":
    for name in ("fvg_negation_reversal", "fvg_return_continuation"):
        if name in armed:
            return name
    return None


def is_forward_draw(dol, direction, price_at_arm) -> bool:
    """Is `dol` still AHEAD of price, in the trade direction, at the arm?

    Production's own menu guarantees this by construction — `_dol_menu` keeps only
    correct-side pools and applies an ATR-scaled proximity floor — so on the production
    track this test never fires. It exists for a supplied DOL that has no such guarantee.

    The oracle draw from the label corpus is exactly that case: it is *the last pool the
    move REACHED*, chosen with hindsight, and on a quarter of the corpus the tape has
    already taken it by the arm. Handed to the Executor such a price is not a target, it
    is an immediate `dol_reached` plan death (`executor.py:466`) — the session is then
    scored on the entry mechanisms' silence rather than on the policy. Excluded sessions
    are reported as their own category, never as absences.
    """
    if dol is None or price_at_arm is None:
        return False
    return (float(dol) > float(price_at_arm)) if str(direction).upper() in ("UP", "LONG") \
        else (float(dol) < float(price_at_arm))


def _last_close(frame) -> float:
    """The price at the arm. Raises rather than returning None.

    A swallowed failure here would be laundered into a `dol_behind_at_arm` exclusion,
    which is a REPORTED category with a methodological justification behind it. A
    malformed arm frame must not be able to hide inside that count.
    """
    return float(frame["Close"].iloc[-1])


def first_fill(date, direction, dol, *, tape, track) -> FeedResult:
    """The composition: clause 14's gate, the forward-draw gate, the plan, the Executor."""
    if dol is None:
        return FeedResult(date=str(date), track=str(track), direction=direction,
                          dol=None, reason=NO_DOL)
    arm_frame = tape.arm_frame(date)
    if arm_frame is None or not len(arm_frame):
        return FeedResult(date=str(date), track=str(track), direction=direction,
                          dol=dol, reason=NO_TAPE)
    price_at_arm = _last_close(arm_frame)
    if not is_forward_draw(dol, direction, price_at_arm):
        return FeedResult(date=str(date), track=str(track), direction=direction,
                          dol=dol, reason=DOL_BEHIND)
    got = tape.feed(date)
    if got is None:
        return FeedResult(date=str(date), track=str(track), direction=direction,
                          dol=dol, reason=NO_TAPE)
    arm_ts, feed = got
    plan = build_plan(direction, dol, arm_frame, arm_ts)
    return drive(plan, arm_ts, feed, date=date, track=track, dol=dol)


# --------------------------------------------------------------------------- #
# The tape                                                                      #
# --------------------------------------------------------------------------- #

class SessionTape:
    """The 1s replay's frames, rebuilt without the backtest.

    Serves, per session, the `(now, bars)` sequence the graft is handed: history in 1m,
    today's completed minutes in 1m, and one running partial minute whose High/Low/Close
    are cumulative to `now`.

    Two loading modes, and the second one is not an optimisation. The MNQ and MES 1s
    parquets are 7.3M and 6.9M rows — 680 MB resident together, before anything is
    computed. `cache_dir` (built by `build_tape_cache`) holds the 1m aggregates plus one
    per-session 1s slice each, so a sweep worker holds ~3 MB of tape at a time instead of
    680 MB. On a machine that may be running a live trading process, that is the
    difference between a background sweep and an out-of-memory event.
    """

    def __init__(self, main_dir=None, tickers=("MNQ", "MES"), cache_dir=None) -> None:
        self.main_dir = main_dir or os.path.join(paths.general_main_dir(),
                                                 CONTRACT_SUBDIR)
        self.tickers = tuple(tickers)
        self.cache_dir = cache_dir
        self._1s: dict = {}
        self._1m: dict = {}
        for tk in self.tickers:
            if cache_dir:
                self._1m[tk] = pd.read_parquet(
                    os.path.join(cache_dir, f"{tk}_1m.parquet")).sort_index()
                continue
            df = pd.read_parquet(self.source_path(tk)).sort_index()
            self._1s[tk] = df
            self._1m[tk] = df.resample("1min", label="left").agg(_AGG).dropna(
                subset=["Open"])

    def source_path(self, ticker: str) -> str:
        return os.path.join(self.main_dir, f"{ticker}_1s.parquet")

    def _bars_1s(self, ticker: str, date) -> pd.DataFrame:
        if not self.cache_dir:
            return self._1s[ticker]
        key = (ticker, str(date))
        if key not in self._1s:
            # One SESSION resident at a time, both tickers of it.
            for stale in [k for k in self._1s if k[1] != str(date)]:
                self._1s.pop(stale, None)
            path = os.path.join(self.cache_dir, f"{ticker}_{date}.parquet")
            self._1s[key] = (pd.read_parquet(path).sort_index()
                             if os.path.exists(path)
                             else pd.DataFrame(columns=list(COLS)))
        return self._1s[key]

    # -- windows ------------------------------------------------------------- #

    @staticmethod
    def window(date) -> tuple:
        day = pd.Timestamp(str(date), tz=TZ).normalize()
        return (day + pd.Timedelta(hours=ARM_ET[0], minutes=ARM_ET[1]),
                day + pd.Timedelta(hours=WINDOW_END_ET[0], minutes=WINDOW_END_ET[1]))

    def _session_start(self, date) -> pd.Timestamp:
        day = pd.Timestamp(str(date), tz=TZ).normalize()
        return day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)

    def _hist(self, ticker, date, w0) -> pd.DataFrame:
        """1m history to the session open, bridged to `w0` — `replay_slice`'s rule.

        Without the bridge the whole overnight session vanishes from the merged frame
        and nothing reports it (the cycle-1 B2 defect). The 17-day bound is the graft's,
        applied once at the first bar as the graft applies it.
        """
        start = self._session_start(date)
        agg = self._1m[ticker]
        hist = agg[(agg.index >= start - pd.Timedelta(days=HIST_DAYS))
                   & (agg.index < start)]
        raw = self._bars_1s(ticker, date)
        bridge_src = raw[(raw.index >= start) & (raw.index < w0)]
        if len(bridge_src):
            bridge = bridge_src.resample("1min", label="left").agg(_AGG).dropna(
                subset=["Open"])
            bridge = bridge[bridge.index < w0]
            if len(bridge):
                hist = pd.concat([hist, bridge[hist.columns]])
        return hist[hist.index >= w0 - EXECUTOR_HISTORY]

    def arm_frame(self, date) -> pd.DataFrame:
        w0, _ = self.window(date)
        return self._hist("MNQ", date, w0)

    def policy_frames(self, date) -> tuple:
        """`(bars_1m, bars_1s)` for the policy simulator — §4 (c)'s two resolutions.

        The window runs from the CME session open to clause 10's 13:00 close. It starts
        at the session open rather than at the arm because `detect_fvgs` needs the two
        bars PRECEDING the one it is testing, and a frame that began at the fill would
        silently miss every gap whose pattern straddles it.
        """
        _, w1 = self.window(date)
        raw = self._bars_1s("MNQ", date)
        day = pd.Timestamp(str(date), tz=TZ).normalize()
        start = day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
        bars_1s = raw[(raw.index >= start) & (raw.index <= w1)]
        if not len(bars_1s):
            return pd.DataFrame(), pd.DataFrame()
        bars_1m = bars_1s.resample("1min", label="left").agg(_AGG).dropna(
            subset=["Open"])
        return bars_1m, bars_1s

    def feed(self, date):
        """`(arm_ts, generator)` or None when the window holds no 1s bars."""
        w0, w1 = self.window(date)
        hist = {tk: self._hist(tk, date, w0) for tk in self.tickers}
        raw = self._bars_1s("MNQ", date)
        tape_mnq = raw[(raw.index >= w0) & (raw.index < w1)]
        if not len(tape_mnq):
            return None
        raw_e = self._bars_1s("MES", date)
        tape_mes = raw_e[(raw_e.index >= w0) & (raw_e.index < w1)]
        return tape_mnq.index[0], _replay_frames(hist, tape_mnq, tape_mes)


def build_tape_cache(dates, out_dir, main_dir=None, tickers=("MNQ", "MES")) -> str:
    """Write the 1m aggregate plus one per-session 1s slice per ticker into `out_dir`.

    Each ticker's 1s parquet is opened once and released before the next, so peak memory
    is one parquet rather than two. The slice runs from the CME session open (18:00 the
    previous day) so `_hist`'s bridge stays inside it.
    """
    os.makedirs(out_dir, exist_ok=True)
    main_dir = main_dir or os.path.join(paths.general_main_dir(), CONTRACT_SUBDIR)
    for tk in tickers:
        df = pd.read_parquet(os.path.join(main_dir, f"{tk}_1s.parquet")).sort_index()
        agg = df.resample("1min", label="left").agg(_AGG).dropna(subset=["Open"])
        agg.to_parquet(os.path.join(out_dir, f"{tk}_1m.parquet"))
        del agg
        for date in dates:
            _, w1 = SessionTape.window(date)
            day = pd.Timestamp(str(date), tz=TZ).normalize()
            start = day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
            # Inclusive of 13:00:00: `policy_frames` reads that second, so an
            # exclusive slice here would make the cached and eager tapes differ
            # on exactly the bar clause 10 acts at.
            sl = df[(df.index >= start) & (df.index <= w1)]
            sl.to_parquet(os.path.join(out_dir, f"{tk}_{date}.parquet"))
        del df
    return out_dir


def _replay_frames(hist: dict, tape_mnq: pd.DataFrame, tape_mes: pd.DataFrame):
    """Yield `(now, bars)` once per 1s bar, reproducing `run_backtest_v2`'s 1s loop.

    The frames are built into pre-allocated arrays and wrapped in a no-copy DataFrame
    per second — the same trick the backtest uses, and for the same reason: a
    `pd.concat` of a 24k-row history per second is the whole cost of the loop.

    **The yielded frames ALIAS one reused buffer.** Each iteration overwrites the running
    partial row in place, so a consumer that keeps a reference across iterations sees the
    LAST state rather than the one it was handed. Every consumer here uses the frame
    before advancing, which is exactly what the Executor does with the live one; anything
    that needs to retain a frame must copy it.
    """
    cidx = pd.Index(list(COLS))
    base = {tk: hist[tk][list(COLS)].to_numpy(dtype=float) for tk in hist}
    bidx = {tk: hist[tk].index for tk in hist}
    mes_by_min = ({k: g for k, g in tape_mes.groupby(tape_mes.index.floor("1min"))}
                  if len(tape_mes) else {})

    for bar_ts, mnq_min in tape_mnq.groupby(tape_mnq.index.floor("1min")):
        mes_min = mes_by_min.get(bar_ts)
        pidx = pd.DatetimeIndex([bar_ts], tz=tape_mnq.index.tz)
        idx = {tk: bidx[tk].append(pidx) for tk in bidx}
        nb = {tk: len(base[tk]) for tk in base}
        buf = {}
        for tk in base:
            arr = np.empty((nb[tk] + 1, len(COLS)), dtype=float)
            if nb[tk]:
                arr[:nb[tk]] = base[tk]
            buf[tk] = arr

        m_open = float(mnq_min.iloc[0]["Open"])
        m_hi = mnq_min["High"].to_numpy(dtype=float)
        m_lo = mnq_min["Low"].to_numpy(dtype=float)
        m_cl = mnq_min["Close"].to_numpy(dtype=float)
        m_ts = mnq_min.index.asi8
        c_hi = np.maximum.accumulate(m_hi)
        c_lo = np.minimum.accumulate(m_lo)
        c_vol = np.cumsum(mnq_min["Volume"].to_numpy(dtype=float))

        has_mes = mes_min is not None and len(mes_min)
        if has_mes:
            e_open = float(mes_min.iloc[0]["Open"])
            e_cl = mes_min["Close"].to_numpy(dtype=float)
            e_ts = mes_min.index.asi8
            e_hi = np.maximum.accumulate(mes_min["High"].to_numpy(dtype=float))
            e_lo = np.minimum.accumulate(mes_min["Low"].to_numpy(dtype=float))
            e_vol = np.cumsum(mes_min["Volume"].to_numpy(dtype=float))
        else:
            # MES silent for this minute: hold the MNQ shape, as the backtest does.
            e_open, e_cl, e_ts = m_open, m_cl, m_ts
            e_hi, e_lo, e_vol = c_hi, c_lo, c_vol

        for i in range(len(mnq_min)):
            now = mnq_min.index[i]
            # RAW per-second High/Low for MNQ, CUMULATIVE for MES. That asymmetry is
            # `backtest_smt.py:1690-1691` verbatim, and it is not cosmetic: the Executor
            # reads this row for `now_mid` — §11's calibrated market-fill price — and for
            # `now_high`/`now_low`, the crossed-trigger test (`executor.py:201-209`).
            # Feeding cumulative extremes for MNQ widens that mid over the whole elapsed
            # minute and prices every market fill on a basis the mechanisms were never
            # calibrated against. (The backtest computes cumulative MNQ values at :1680
            # and deliberately does not use them here.)
            buf["MNQ"][nb["MNQ"]] = [m_open, m_hi[i], m_lo[i], m_cl[i], c_vol[i]]
            if "MES" in buf:
                j = int(np.searchsorted(e_ts, m_ts[i], side="right")) - 1
                if j >= 0:
                    buf["MES"][nb["MES"]] = [e_open, e_hi[j], e_lo[j], e_cl[j],
                                             e_vol[j]]
                else:
                    buf["MES"][nb["MES"]] = [e_open, e_open, e_open, e_open, 0.0]
            yield now, {tk: pd.DataFrame(buf[tk], index=idx[tk], columns=cidx)
                        for tk in buf}

        for tk in base:
            if tk == "MNQ":
                row = np.array([[m_open, c_hi[-1], c_lo[-1], m_cl[-1], c_vol[-1]]])
            elif has_mes:
                k = len(mes_min) - 1
                row = np.array([[e_open, e_hi[k], e_lo[k], e_cl[k], e_vol[k]]])
            else:
                row = np.array([[e_open, e_open, e_open, e_open, 0.0]])
            base[tk] = np.vstack((base[tk], row)) if base[tk].size else row
            bidx[tk] = idx[tk]
