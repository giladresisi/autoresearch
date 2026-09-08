"""Plan 31's two counterfactual baselines, and plan 33 §6's capture metrics.

**Plan 31 §2 is the rule these obey**: a baseline never references the candidate exit's
time, price, or existence. It is a self-contained policy that runs from the fill to its own
termination and produces the same number no matter what it is being compared against.
Anchoring a trail to "the last FVG before the exit" is what produced two different answers
for the same 08-21 counterfactual, and that defect was in the instruction rather than in
either reading.

**Re-run on plan 33 §4 (c)'s basis, and that is the whole point of this module.** Plan 31
§4's pinned values were computed on 1m bars throughout. The policy is 1m-logic with 1s
touches, so a comparison against 1m-touch baselines drifts — and drifts in a known
direction: 1m with adverse-first resolution is a CONSERVATIVE upper bound on stop firing,
and 1s only refines it downward. Plan 31 §7 makes its §4 authoritative, so these values
supersede it FOR PLAN 33 ONLY and are recorded as a new section there rather than by
editing §4.

`STOP_CAP_PTS` is imported from the Executor rather than restated: plan 31 §3.1 takes it
from production deliberately, and it inherits the points-vs-ratio defect §6 of that
document records. The FVG far edge is `position_policy._far_edge` for the same reason —
one definition, one place.

**B-tight has NO buffer.** Plan 31 §3.3 moves the stop TO the far edge; plan 33's clause 5
puts it 3 pts BEYOND the *previous* gap's far edge. Those are different rules and the
difference is the thing being measured, so B-tight must not quietly acquire the policy's
buffer.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agent.study.position_policy import (TF, _far_edge, _is_long, _tighter,
                                         continuation_fvgs)
from agent.trader.executor import STOP_CAP_PTS

TZ = "America/New_York"

EXIT_STOP = "stop"
EXIT_MARK = "MARK"
EXIT_DOL = "dol_touch"

#: Plan 31 §3: both baselines run from the fill to 16:00 ET. Plan 33's policy hard-closes
#: at 13:00 (clause 10), so a like-for-like comparison needs the same window — the caller
#: chooses, and the result records which was used. Reporting a 16:00 MARK against a 13:00
#: policy exit would be the same category error as quoting a MARK as an exit.
PLAN31_CLOSE_ET = (16, 0)
POLICY_CLOSE_ET = (13, 0)


@dataclass
class BaselineRun:
    name: str
    date: str
    direction: str
    fill_ts: pd.Timestamp
    fill_price: float
    initial_stop: float
    exit_ts: "pd.Timestamp | None" = None
    exit_price: "float | None" = None
    exit_reason: "str | None" = None
    pnl: "float | None" = None
    n_moves: int = 0
    close_et: tuple = PLAN31_CLOSE_ET
    touch_basis: str = "1s"

    @property
    def is_mark(self) -> bool:
        """A position that never stopped is MARKED to the close and is NOT an exit.

        Plan 31 §3.1 is emphatic about this, and it is the artifact that made an earlier
        read appear to show the user's trigger losing on 08-24.
        """
        return self.exit_reason == EXIT_MARK

    def to_dict(self) -> dict:
        return {"name": self.name, "date": self.date, "direction": self.direction,
                "fill_ts": self.fill_ts.isoformat(), "fill_price": self.fill_price,
                "initial_stop": self.initial_stop,
                "exit_ts": None if self.exit_ts is None else self.exit_ts.isoformat(),
                "exit_price": self.exit_price, "exit_reason": self.exit_reason,
                "pnl": self.pnl, "n_moves": self.n_moves, "is_mark": self.is_mark,
                "close_et": list(self.close_et), "touch_basis": self.touch_basis}


def initial_stop(fill_price, direction, width_pts=None) -> float:
    """Plan 31 §3.1: fill ± `STOP_CAP_PTS`, adverse side. `width_pts` overrides the width.

    **The override is the whole point of the stop-width sweep and it is not optional
    there.** Both baselines are DEFINED from the initial stop — B-loose *is* the initial
    stop, never moved — so scoring a wide-stop policy against a 25-pt-stop baseline would
    hand the policy a win it never earned, and nothing in the output would show it. Every
    call site in the sweep therefore passes the same width to the policy and to both
    baselines.
    """
    w = STOP_CAP_PTS if width_pts is None else float(width_pts)
    return (float(fill_price) - w if _is_long(direction) else float(fill_price) + w)


def avg_range_1h(bars_1m, at) -> "float | None":
    """The ATR proxy at `at`: mean true range of the last `ATR_BARS` completed 1h bars.

    Mirrors `FactsMaintainer._refresh_avg_range`, whose constants are imported rather than
    restated. It is a private method on a stateful session-scoped object, so it cannot be
    called directly from here; what is reproduced is one expression, and both constants
    come from the maintainer so a change there reaches this.
    """
    from agent.facts.bars import resample
    from agent.facts.maintainer import ATR_BARS, ATR_TAIL

    if bars_1m is None or not len(bars_1m):
        return None
    at = pd.Timestamp(at)
    frame = bars_1m[bars_1m.index <= at]
    if not len(frame):
        return None
    try:
        h1 = resample(frame[frame.index >= frame.index[-1] - ATR_TAIL], "1h")
    except Exception:
        return None
    if not len(h1):
        return None
    tail = h1.tail(ATR_BARS)
    return float((tail["High"] - tail["Low"]).mean())


def stop_for_width(fill_price, direction, spec, *, avg_1h=None, structural=None):
    """Resolve one width SPEC to a stop price. `(kind, value)`:

      ("mechanism", None)     the entry mechanism's own stop, unchanged (the control)
      ("pts", w)              fill -/+ w points
      ("avg", m)              fill -/+ m * avg_range_1h
      ("struct_or_avg", m)    the WIDER of the uncapped structural stop and m * avg_1h

    The last one exists to separate two variables a fixed width conflates: production's
    stop is a STRUCTURAL level capped at 25, so replacing it with a fixed width both
    widens it AND discards the gap anchoring. `struct_or_avg` keeps the anchor and only
    refuses to be tighter than the width.
    """
    kind, value = spec
    long_ = _is_long(direction)
    if kind == "mechanism":
        return structural if structural is not None else None
    if kind == "pts":
        return initial_stop(fill_price, direction, float(value))
    if kind == "avg":
        if avg_1h is None:
            return None
        return initial_stop(fill_price, direction, float(value) * float(avg_1h))
    if kind == "struct_or_avg":
        if avg_1h is None or structural is None:
            return None
        by_width = initial_stop(fill_price, direction, float(value) * float(avg_1h))
        # "wider" = further from the fill in the adverse direction
        return min(structural, by_width) if long_ else max(structural, by_width)
    raise ValueError(f"unknown width spec {spec!r}")


def _walk(run: BaselineRun, tape: pd.DataFrame, moves, long_: bool, dol=None) -> None:
    close_ts = (run.fill_ts.normalize()
                + pd.Timedelta(hours=run.close_et[0], minutes=run.close_et[1]))
    window = tape[(tape.index >= run.fill_ts) & (tape.index <= close_ts)]
    stop = run.initial_stop
    pending = list(moves)
    last_close = run.fill_price

    for ts, row in window.iterrows():
        while pending and pending[0][0] <= ts:
            stop = pending.pop(0)[1]
        last_close = float(row["Close"])
        hit = (float(row["Low"]) <= stop) if long_ else (float(row["High"]) >= stop)
        # Adverse first, as everywhere else: a bar reaching both books the stop.
        if hit:
            _finish(run, ts, stop, EXIT_STOP, long_)
            return
        if dol is not None:
            reached = ((float(row["High"]) >= float(dol)) if long_
                       else (float(row["Low"]) <= float(dol)))
            if reached:
                _finish(run, ts, float(dol), EXIT_DOL, long_)
                return
    ts = window.index[-1] if len(window) else run.fill_ts
    _finish(run, ts, last_close, EXIT_MARK, long_)


def _finish(run: BaselineRun, ts, price, reason, long_: bool) -> None:
    run.exit_ts, run.exit_price, run.exit_reason = ts, float(price), reason
    run.pnl = round((float(price) - run.fill_price) if long_
                    else (run.fill_price - float(price)), 4)


def b_tight(bars_1m, bars_1s=None, *, date, direction, fill_ts, fill_price,
            close_et=PLAN31_CLOSE_ET, initial=None) -> BaselineRun:
    """Plan 31 §3.3 — the ratcheting FVG trail, the protective policy.

    Monotone by construction: a far edge looser than the standing stop is skipped, not
    applied. Plan 31 §5 records what this baseline is: it stops on 8 of 8 days, every one
    within 26 minutes of the fill, so it is scratch-prone and cheap to beat. It is in the
    comparison set precisely because failing against it would be the headline.
    """
    long_ = _is_long(direction)
    fill_ts = pd.Timestamp(fill_ts)
    run = BaselineRun("B-tight", str(date), str(direction).upper(), fill_ts,
                      float(fill_price),
                      float(initial) if initial is not None
                      else initial_stop(fill_price, direction),
                      close_et=tuple(close_et))

    moves, current = [], run.initial_stop
    for gap in continuation_fvgs(bars_1m, direction, fill_ts):
        edge = _far_edge(gap, long_)
        best = _tighter(current, edge, long_)
        if best == current:
            continue                                   # looser: a no-op
        current = best
        moves.append((pd.Timestamp(gap.extra["exists_from"]), current))
    run.n_moves = len(moves)

    tape = bars_1s if bars_1s is not None and len(bars_1s) else bars_1m
    run.touch_basis = "1s" if tape is bars_1s else "1m"
    _walk(run, tape, moves, long_)
    return run


def b_loose(bars_1m, bars_1s=None, *, date, direction, fill_ts, fill_price,
            close_et=PLAN31_CLOSE_ET, initial=None, dol=None) -> BaselineRun:
    """Plan 31 §3.4 — the unmanaged hold. A CEILING, not a policy.

    **`dol=` is a VARIANT, not plan 31's definition.** §3.4 is the initial stop never
    moved, exiting on a touch and otherwise `MARK` — no take-profit — and that stays the
    default so every §6 number keeps its meaning.

    But production's `OrderSim` DOES take profit at the plan's DOL (`order_sim.py:86-92`),
    so stop-only B-loose is not production's behaviour. The difference has a direction:
    without the cap B-loose keeps running past the DOL whenever price continues, so it
    books MORE than production would on exactly the trending sessions where a trail is
    supposed to win. **Stop-only B-loose is therefore a STRONGER opponent than production
    actually is, and every comparison against it has been harsher on the policy than
    production's own behaviour warrants.** Passing `dol=` gives the true production
    analogue, reported as its own column rather than silently replacing §3.4.
    """
    long_ = _is_long(direction)
    fill_ts = pd.Timestamp(fill_ts)
    run = BaselineRun("B-loose", str(date), str(direction).upper(), fill_ts,
                      float(fill_price),
                      float(initial) if initial is not None
                      else initial_stop(fill_price, direction),
                      close_et=tuple(close_et))
    tape = bars_1s if bars_1s is not None and len(bars_1s) else bars_1m
    run.touch_basis = "1s" if tape is bars_1s else "1m"
    if dol is not None:
        run.name = "B-loose+DOL"
    _walk(run, tape, (), long_, dol=dol)
    return run


# --------------------------------------------------------------------------- #
# §6's metrics                                                                  #
# --------------------------------------------------------------------------- #

def ceiling(fill_price, extreme_price, direction) -> "float | None":
    """The oracle ceiling: exiting exactly at the primary segment's extreme.

    `None` when the fill is already past the extreme — there is nothing left to capture,
    and scoring such a session as 0% capture would blame the policy for the entry's
    timing. Those sessions are counted and reported, never folded in.
    """
    d = 1.0 if _is_long(direction) else -1.0
    room = d * (float(extreme_price) - float(fill_price))
    return room if room > 0 else None


def capture(pnl, ceil) -> "float | None":
    return None if ceil in (None, 0) or pnl is None else round(float(pnl) / ceil, 6)


def is_scratch(exit_reason, exit_ts, extreme_ts) -> bool:
    """§6's scratch: stopped out BEFORE the move's extreme printed.

    A stop after the extreme is the trail doing its job; a stop before it is the false
    exit on a winner that cycle 5 exists to avoid.
    """
    if exit_reason != EXIT_STOP or exit_ts is None or extreme_ts is None:
        return False
    return pd.Timestamp(exit_ts) < pd.Timestamp(extreme_ts)


def per_point_risked(rows) -> dict:
    """Points captured per point RISKED — capture's counterpart for a width sweep.

    Raw capture ranks the widest stop first by construction: a 1.6 x avg_1h stop risks
    roughly six times a 25-pt one, and capture does not divide by that. Risk is measured
    as the distance from the fill to the INITIAL stop, which is the amount actually at
    hazard at entry under every arm.
    """
    usable = [r for r in rows
              if r.get("pnl") is not None and r.get("initial_stop") is not None
              and r.get("fill_price") is not None
              and abs(float(r["fill_price"]) - float(r["initial_stop"])) > 1e-9]
    if not usable:
        return {"n": 0, "pooled": None, "risk_total": 0.0}
    pts = sum(float(r["pnl"]) for r in usable)
    risk = sum(abs(float(r["fill_price"]) - float(r["initial_stop"])) for r in usable)
    return {"n": len(usable), "pooled": round(pts / risk, 6),
            "points": round(pts, 2), "risk_total": round(risk, 2),
            "risk_mean": round(risk / len(usable), 2)}


def pooled_capture(rows) -> "dict":
    """Points-weighted capture over a set of sessions, plus the per-session median.

    Both, because they answer different questions and disagree: the pooled figure is what
    a book earns and is dominated by large-extent sessions; the median says what a typical
    session returns. Reporting one alone is how a result gets overstated.
    """
    import statistics as st

    usable = [r for r in rows if r.get("ceiling") and r.get("pnl") is not None]
    if not usable:
        return {"n": 0, "pooled": None, "median": None, "points": 0.0, "ceiling": 0.0}
    pts = sum(float(r["pnl"]) for r in usable)
    ceil = sum(float(r["ceiling"]) for r in usable)
    per = [float(r["pnl"]) / float(r["ceiling"]) for r in usable]
    return {"n": len(usable), "pooled": round(pts / ceil, 6),
            "median": round(st.median(per), 6), "points": round(pts, 2),
            "ceiling": round(ceil, 2)}


def session_bootstrap(rows, n_boot: int = 2000, seed: int = 20260908) -> dict:
    """A 95% interval for pooled capture, resampling SESSIONS — never observations.

    §6 is explicit about this. One session contributes one fill and one exit, so an
    observation-clustered interval would treat a single session's stop moves as
    independent evidence and understate the interval by a wide margin.
    """
    import random

    usable = [r for r in rows if r.get("ceiling") and r.get("pnl") is not None]
    if len(usable) < 2:
        return {"n": len(usable), "lo": None, "hi": None}
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        draw = [usable[rng.randrange(len(usable))] for _ in usable]
        ceil = sum(float(r["ceiling"]) for r in draw)
        if ceil <= 0:
            continue
        stats.append(sum(float(r["pnl"]) for r in draw) / ceil)
    if not stats:
        return {"n": len(usable), "lo": None, "hi": None}
    stats.sort()
    lo = stats[int(0.025 * (len(stats) - 1))]
    hi = stats[int(0.975 * (len(stats) - 1))]
    return {"n": len(usable), "lo": round(lo, 6), "hi": round(hi, 6),
            "n_boot": len(stats)}


def extent_terciles(rows) -> dict:
    """§6: capture is known to concentrate in large-extent sessions, so it is reported
    by tercile of the primary segment's extent rather than only in aggregate."""
    usable = [r for r in rows if r.get("ceiling") and r.get("pnl") is not None
              and r.get("extent") is not None]
    if len(usable) < 3:
        return {}
    ordered = sorted(usable, key=lambda r: float(r["extent"]))
    n = len(ordered)
    cuts = [ordered[:n // 3], ordered[n // 3:2 * n // 3], ordered[2 * n // 3:]]
    names = ("small", "mid", "large")
    out = {}
    for name, group in zip(names, cuts):
        stat = pooled_capture(group)
        stat["extent_range"] = [round(float(group[0]["extent"]), 2),
                                round(float(group[-1]["extent"]), 2)] if group else None
        out[name] = stat
    return out
