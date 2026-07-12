"""Bench lifecycle state machine (plan 10 Phase 1; file named lifecycle.py, not
engine.py, so its bare module name never shadows the production agent/decisions/engine.py).

The L1 subset of spec §5, driven by an INJECTED decision provider + a bar frame — no
facts, no LLM in this module (those live in run_bench.py). Per date it walks 1m bars and
plays out each standing thesis's life: arrival-gated predicate evaluation
(agent/contracts/predicates.eval_predicate), the code-injected safety-net triggers, the
confidence-tiered TTL, the churn cap, and lifecycle-record emission. On any death it
re-calls L1 at the death bar (+latency). Low-confidence / failsafe / NEUTRAL theses do
not stand — they wait on their recall events / max_age_min.

The provider returns a `BenchDecision`; the engine owns only the mechanics, so the unit
tests script deterministic providers and synthetic bars with no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from predicates import MarketView, eval_any  # agent/contracts (path-wired by conftest)

_TF_RULES = (("1m", "1min"), ("5m", "5min"), ("15m", "15min"), ("1h", "1h"), ("4h", "4h"))


def _uses_closes(preds) -> bool:
    """Whether any predicate in the list needs per-timeframe completed closes (only
    n_closes_beyond does). Lets the walk skip the resample when nothing reads closes."""
    for p in preds or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "n_closes_beyond":
            return True
        if p.get("type") in ("all_of", "any_of") and _uses_closes(p.get("of")):
            return True
    return False


# --------------------------------------------------------------------------- #
# Injected decision (what the provider yields)                                 #
# --------------------------------------------------------------------------- #
@dataclass
class BenchDecision:
    """One L1 call's result, as the engine consumes it. run_bench builds these from
    decide_thesis + the confidence gate + facts; tests script them directly.

    `levels` maps each referenced level name -> {price, side, swept0, depleted0,
    threshold} so the engine can evolve sweep/depletion state over the walk without a
    facts dependency. daily_mid / sess_hi / sess_lo feed the safety nets.
    """

    decision_id: str
    born_ts: pd.Timestamp
    thesis: dict
    gate: str                      # "HIGH" | "MEDIUM" | "LOW"
    self_report: Optional[str] = None
    failsafe: bool = False
    levels: dict = field(default_factory=dict)
    daily_mid: Optional[float] = None
    sess_hi: Optional[float] = None
    sess_lo: Optional[float] = None
    facts_hash: Optional[str] = None

    @property
    def bias(self) -> Optional[str]:
        return (self.thesis or {}).get("bias")

    @property
    def dol_price(self) -> Optional[float]:
        dol = (self.thesis or {}).get("dol") or {}
        p = dol.get("price")
        return float(p) if isinstance(p, (int, float)) else None

    def stands(self) -> bool:
        """A thesis STANDS (code-evaluated per bar) only when it is a confident,
        directional call. Otherwise it waits on recall (low-conf path)."""
        return (not self.failsafe and self.gate in ("HIGH", "MEDIUM")
                and self.bias in ("UP", "DOWN"))


# --------------------------------------------------------------------------- #
# Emitted lifecycle record                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Lifecycle:
    decision_id: str
    born_ts: pd.Timestamp
    arrival_ts: pd.Timestamp
    died_ts: Optional[pd.Timestamp]
    cause: str                       # completed|falsified|safety_net|ttl|recall|failsafe|session_end
    cause_detail: Optional[str] = None   # the safety-net name, when cause == safety_net
    stood: bool = False
    bias: Optional[str] = None
    gate: Optional[str] = None
    self_report: Optional[str] = None
    dol_level: Optional[str] = None
    dol_price: Optional[float] = None
    arrival_price: Optional[float] = None
    time_alive_min: Optional[float] = None
    mfe: Optional[float] = None          # max favorable excursion (pts, toward bias)
    mae: Optional[float] = None          # max adverse excursion (pts, against bias)
    mfe_ts: Optional[pd.Timestamp] = None
    dist_to_dol_pct: Optional[float] = None
    facts_hash: Optional[str] = None
    # Phase-3 scoring enrichment (filled by score.py; None until then).
    false_kill: Optional[bool] = None
    late_kill_adverse: Optional[float] = None

    def to_record(self) -> dict:
        def _ts(t):
            return t.isoformat() if isinstance(t, pd.Timestamp) else t
        return {
            "decision_id": self.decision_id,
            "born_ts": _ts(self.born_ts), "arrival_ts": _ts(self.arrival_ts),
            "died_ts": _ts(self.died_ts), "cause": self.cause,
            "cause_detail": self.cause_detail, "stood": self.stood,
            "bias": self.bias, "gate": self.gate, "self_report": self.self_report,
            "dol_level": self.dol_level, "dol_price": self.dol_price,
            "arrival_price": self.arrival_price, "time_alive_min": self.time_alive_min,
            "mfe": self.mfe, "mae": self.mae, "mfe_ts": _ts(self.mfe_ts),
            "dist_to_dol_pct": self.dist_to_dol_pct, "facts_hash": self.facts_hash,
            "false_kill": self.false_kill, "late_kill_adverse": self.late_kill_adverse,
        }


@dataclass
class DayResult:
    date: str
    lifecycles: list = field(default_factory=list)
    day_outcome: str = "ok"          # "ok" | "churn_cap"
    n_calls: int = 0


# --------------------------------------------------------------------------- #
# MarketView construction over the walk                                        #
# --------------------------------------------------------------------------- #
def _closes_by_tf(bars_upto_now: pd.DataFrame) -> dict:
    """Completed closes per timeframe, oldest first. 1m is exact; higher-TF bins use the
    last close seen so far (a deterministic bench approximation — the workhorse
    predicates read price + 1m closes, which are exact)."""
    out = {}
    for tf, rule in _TF_RULES:
        s = bars_upto_now["close"].resample(rule).last().dropna()
        out[tf] = list(s.values)
    return out


class _SweepTracker:
    """Latches sweep/depletion per referenced level as the walk advances (inclusive
    wick cross == engine convention). Seeded from the facts' birth-time states."""

    def __init__(self, levels: dict):
        self.levels = levels or {}
        self.swept = {n: bool(v.get("swept0")) for n, v in self.levels.items()}
        self.exc = {n: 0.0 for n in self.levels}
        self.depleted = {n: bool(v.get("depleted0")) for n, v in self.levels.items()}

    def update(self, bar) -> None:
        hi, lo = float(bar["high"]), float(bar["low"])
        for name, v in self.levels.items():
            price, side = v.get("price"), v.get("side")
            if not isinstance(price, (int, float)) or side not in ("above", "below"):
                continue
            if side == "below":
                if lo <= price:
                    self.swept[name] = True
                    self.exc[name] = max(self.exc[name], price - lo)
            else:
                if hi >= price:
                    self.swept[name] = True
                    self.exc[name] = max(self.exc[name], hi - price)
            thr = v.get("threshold")
            if isinstance(thr, (int, float)) and self.exc[name] >= thr:
                self.depleted[name] = True

    def swept_set(self) -> set:
        return {n for n, s in self.swept.items() if s}

    def depleted_set(self) -> set:
        return {n for n, s in self.depleted.items() if s}


# --------------------------------------------------------------------------- #
# One lifecycle                                                                #
# --------------------------------------------------------------------------- #
def run_lifecycle(decision: BenchDecision, bars: pd.DataFrame, cfg,
                  session_end_ts: pd.Timestamp) -> Lifecycle:
    """Play one standing (or waiting) decision from arrival to its death.

    Predicate evaluation and excursion tracking begin at the arrival bar (production
    parity): a predicate that was true between the trigger and arrival never fires.
    """
    born = decision.born_ts
    arrival = born + cfg.latency()
    walk = bars[(bars.index >= arrival) & (bars.index <= session_end_ts)]

    lc = Lifecycle(
        decision_id=decision.decision_id, born_ts=born, arrival_ts=arrival,
        died_ts=None, cause="session_end", stood=decision.stands(),
        bias=decision.bias, gate=decision.gate, self_report=decision.self_report,
        facts_hash=decision.facts_hash,
    )
    dol = (decision.thesis or {}).get("dol") or {}
    lc.dol_level = dol.get("level")
    lc.dol_price = decision.dol_price

    if len(walk) == 0:
        # Arrival is past session end — the decision never gets a bar; close immediately.
        lc.died_ts = session_end_ts
        lc.cause = "failsafe" if decision.failsafe else "session_end"
        lc.time_alive_min = 0.0
        return lc

    ref = float(walk["close"].iloc[0])
    lc.arrival_price = round(ref, 2)

    if not decision.stands():
        _run_waiting(decision, walk, cfg, session_end_ts, lc)
    else:
        _run_standing(decision, walk, cfg, session_end_ts, ref, lc)

    end = lc.died_ts if lc.died_ts is not None else session_end_ts
    lc.time_alive_min = round((end - born).total_seconds() / 60.0, 2)
    return lc


def _run_waiting(decision, walk, cfg, session_end_ts, lc: Lifecycle) -> None:
    """Low-conf / NEUTRAL / failsafe: does not stand. Wait on recall.events / max_age_min;
    death cause is `failsafe` when the decision itself failsafed, else `recall`."""
    recall = (decision.thesis or {}).get("recall") or {}
    events = recall.get("events") or []
    max_age = recall.get("max_age_min")
    tracker = _SweepTracker(decision.levels)
    # Decision quality dominates the label: a failsafe waiting-lifecycle is recorded as
    # `failsafe` whether it dies by recall or spans to session end.
    recall_cause = "failsafe" if decision.failsafe else "recall"
    survive_cause = "failsafe" if decision.failsafe else "session_end"
    need_closes = _uses_closes(events)

    for ts, bar in walk.iterrows():
        tracker.update(bar)
        if isinstance(max_age, (int, float)) and max_age > 0 \
                and (ts - decision.born_ts).total_seconds() / 60.0 >= max_age:
            lc.died_ts = ts
            lc.cause = recall_cause
            return
        # No recall events (e.g. the failsafe thesis) → nothing to evaluate per bar.
        if events:
            mv = _market_view(decision, walk, ts, tracker, need_closes)
            if eval_any(events, mv):
                lc.died_ts = ts
                lc.cause = recall_cause
                return
    # Survived to session end without a recall firing.
    lc.died_ts = session_end_ts
    lc.cause = survive_cause


def _run_standing(decision, walk, cfg, session_end_ts, ref, lc: Lifecycle) -> None:
    """Confident directional thesis: evaluate exhaustion (completed), falsification, the
    enabled safety nets, and the confidence-tiered TTL each bar from arrival."""
    bias = decision.bias
    dol_price = decision.dol_price
    thesis = decision.thesis or {}
    falsified_if = thesis.get("falsified_if") or []
    exhausted_if = thesis.get("exhausted_if") or []
    tracker = _SweepTracker(decision.levels)
    ttl_min = cfg.ttl_for(decision.gate) if cfg.net_on("ttl") else None
    need_closes = _uses_closes(falsified_if) or _uses_closes(exhausted_if)

    mfe = mae = 0.0
    mfe_ts = walk.index[0]
    flip_run = 0                     # consecutive anti-thesis closes (acceptance_flip)

    for ts, bar in walk.iterrows():
        tracker.update(bar)
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

        # Excursions (favorable == toward bias).
        if bias == "UP":
            fav, adv = hi - ref, ref - lo
        else:
            fav, adv = ref - lo, hi - ref
        if fav > mfe:
            mfe, mfe_ts = fav, ts
        if adv > mae:
            mae = adv

        # (1) completed — DOL drawn (or an exhausted_if predicate fires).
        mv = _market_view(decision, walk, ts, tracker, need_closes)
        dol_touched = dol_price is not None and (
            (bias == "UP" and hi >= dol_price) or (bias == "DOWN" and lo <= dol_price))
        if dol_touched or (exhausted_if and eval_any(exhausted_if, mv)):
            _finish(lc, ts, "completed", ref, bias, dol_price, mfe, mae, mfe_ts)
            return

        # (2) falsified — a falsified_if predicate fires.
        if falsified_if and eval_any(falsified_if, mv):
            _finish(lc, ts, "falsified", ref, bias, dol_price, mfe, mae, mfe_ts)
            return

        # (3) safety nets (config-toggleable).
        net = _safety_net_fires(decision, cfg, bar, ref, bias, close, flip_run)
        flip_run = net["flip_run"]
        if net["name"]:
            _finish(lc, ts, "safety_net", ref, bias, dol_price, mfe, mae, mfe_ts,
                    detail=net["name"])
            return

        # (4) TTL — confidence-tiered.
        if ttl_min is not None and (ts - decision.born_ts).total_seconds() / 60.0 >= ttl_min:
            _finish(lc, ts, "ttl", ref, bias, dol_price, mfe, mae, mfe_ts)
            return

    # Survived to session end while standing.
    _finish(lc, session_end_ts, "session_end", ref, bias, dol_price, mfe, mae, mfe_ts)


def _safety_net_fires(decision, cfg, bar, ref, bias, close, flip_run) -> dict:
    """Evaluate the two standing code-injected nets. Returns the firing net name (or
    None) and the updated acceptance-flip run counter."""
    name = None
    # acceptance_flip: N consecutive 1m closes on the anti-thesis side of the daily mid.
    if cfg.net_on("acceptance_flip") and isinstance(decision.daily_mid, (int, float)):
        anti = (close < decision.daily_mid) if bias == "UP" else (close > decision.daily_mid)
        flip_run = flip_run + 1 if anti else 0
        if flip_run >= cfg.acceptance_flip_n:
            name = "acceptance_flip"
    # opposite_extreme: a new session extreme against the thesis direction.
    if name is None and cfg.net_on("opposite_extreme"):
        if bias == "UP" and isinstance(decision.sess_lo, (int, float)) \
                and float(bar["low"]) < decision.sess_lo:
            name = "opposite_extreme"
        elif bias == "DOWN" and isinstance(decision.sess_hi, (int, float)) \
                and float(bar["high"]) > decision.sess_hi:
            name = "opposite_extreme"
    return {"name": name, "flip_run": flip_run}


def _finish(lc: Lifecycle, ts, cause, ref, bias, dol_price, mfe, mae, mfe_ts,
            detail=None) -> None:
    lc.died_ts = ts
    lc.cause = cause
    lc.cause_detail = detail
    lc.mfe = round(mfe, 2)
    lc.mae = round(mae, 2)
    lc.mfe_ts = mfe_ts
    if dol_price is not None and ref is not None and abs(dol_price - ref) > 1e-9:
        covered = mfe / abs(dol_price - ref)
        lc.dist_to_dol_pct = round(max(0.0, min(1.0, covered)) * 100.0, 1)


def _market_view(decision, walk, ts, tracker: _SweepTracker,
                 need_closes: bool = True) -> MarketView:
    upto = walk[walk.index <= ts]
    price = float(upto["close"].iloc[-1])
    return MarketView(
        price=price, closes_by_tf=(_closes_by_tf(upto) if need_closes else {}),
        swept=tracker.swept_set(), depleted=tracker.depleted_set(),
        now=ts, since=decision.born_ts,
    )


# --------------------------------------------------------------------------- #
# One day                                                                      #
# --------------------------------------------------------------------------- #
def run_day(bars: pd.DataFrame, provider: Callable[[pd.Timestamp], BenchDecision],
            cfg, session_open_ts: pd.Timestamp, session_end_ts: pd.Timestamp,
            date: str = "") -> DayResult:
    """Walk one date: L1 at the open, then re-call at every death (+latency) until the
    session ends or the churn cap is hit.

    The provider is called once per L1 call with the trigger timestamp; each call counts
    toward the churn cap. Hitting the cap aborts the day with outcome `churn_cap` (the
    runaway-API guard, and a scorecard fact)."""
    result = DayResult(date=date)
    trigger = session_open_ts

    while True:
        if result.n_calls >= cfg.churn_cap:
            result.day_outcome = "churn_cap"
            break
        decision = provider(trigger)
        result.n_calls += 1
        lc = run_lifecycle(decision, bars, cfg, session_end_ts)
        result.lifecycles.append(lc)
        if lc.cause == "session_end" or lc.died_ts is None \
                or lc.died_ts >= session_end_ts:
            break
        # Re-call L1 at the death bar (arrival then adds latency).
        trigger = lc.died_ts

    return result
