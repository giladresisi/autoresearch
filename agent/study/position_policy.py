"""Plan 33 Task 3: the position-management policy, clause by clause.

The policy owns everything AFTER the fill and nothing before it (§4 (d)). It is handed
`(time, price, direction, initial_stop)` and two injected prices — a DOL and an initial
target — and it is indifferent to how either was chosen (§2.1). It never reads the
Executor's simulated order lifecycle: that lifecycle carries its own stop and its own DOL
take-profit, and two owners of one stop is a defect waiting to happen.

**The policy is causal, and the shape of this module is what makes that checkable.** Every
stop move is derived from a completed 1m bar and becomes effective at the OPEN of the next
1m bar (clause 11). So the schedule is built first, from bar data alone, and only then
walked against the tape. A clause that needed a future value could not be expressed in the
first half at all, which is a stronger guarantee than inspecting the walk.

**Two resolutions, deliberately** (§4 (c)): the RULES are 1m — FVG completion, closes, the
3-pt opposite bar — and the TOUCHES are 1s, which is production's own "bar time inside the
loop" shape. The one place this could have been ambiguous is clause 4's 50% test, and it
turns out not to be: a 1m bar's High is the maximum of its constituent 1s Highs, so "the
50% mark was reached during minute M" is the same statement at both resolutions, and
clause 11 puts the action at M+1's open either way.

**The FVG definition is the codebase's** — `agent/facts/detectors/fvg.py`, reached through
`detect_fvgs` rather than restated. A bull gap spans `[bar1.High, bar3.Low]` and completes
at bar 3; its FAR (adverse, for a long) edge is `bar1.High`, which the detector stores as
`price_low`. `exists_from` is the third bar's COMPLETION instant, which is already the next
bar's open — so it IS clause 11's effective instant, with no arithmetic of our own.

**Clauses 12 and 13 are counted, not merely handled.** An absent initial target, or one
sitting beyond the DOL, means the regime never flips and clause 7 is inert for that
session — half the policy. The rate at which that happens is a reported result, so it is a
field on the run rather than a branch taken silently.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agent.facts.detectors.fvg import detect_fvgs

TZ = "America/New_York"
TICKER = "MNQ"
TF = "1min"

#: Clause 5 / clause 7's buffer. Settled at 3 pts (§3 D5); the narrative's 5 was never
#: the rule and reproduces neither the 10:21 stop nor the §9 table.
TRAIL_BUFFER_PTS = 3.0
#: Clause 7's floor: an opposite bar smaller than this does not ratchet.
OPPOSITE_BODY_PTS = 3.0
#: Clause 4's fallback trigger, as a fraction of the fill -> DOL distance.
BE_FALLBACK_FRACTION = 0.5
#: Clause 10.
HARD_CLOSE_ET = (13, 0)

_LONG = ("UP", "LONG")

# Exit reasons.
EXIT_STOP = "stop"
EXIT_DOL = "dol_touch"
EXIT_TIME = "hard_close_1300"
#: The tape ended before 13:00. Distinct from a hard close on purpose — one is the policy
#: acting, the other is the data running out, and conflating them hides a truncated window.
EXIT_TAPE_END = "tape_end"
NO_ENTRY = "no_entry_no_dol"

# Initial-target status — §2.1's counted categories.
TARGET_PRESENT = "present"
TARGET_ABSENT = "absent"            # clause 12
TARGET_BEYOND_DOL = "beyond_dol"    # clause 13, treated exactly as absent


@dataclass(frozen=True)
class StopMove:
    """One stop change, with the clause that produced it and the bar that triggered it."""
    at: pd.Timestamp                 # effective instant = the next 1m bar's open
    price: float
    clause: int
    trigger: str

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "price": self.price, "clause": self.clause,
                "trigger": self.trigger}


@dataclass
class PolicyRun:
    date: str
    track: str
    direction: str
    fill_ts: "pd.Timestamp | None" = None
    fill_price: "float | None" = None
    initial_stop: "float | None" = None
    dol: "float | None" = None
    initial_target: "float | None" = None
    target_status: str = TARGET_ABSENT
    flipped_at: "pd.Timestamp | None" = None
    moves: tuple = ()
    exit_ts: "pd.Timestamp | None" = None
    exit_price: "float | None" = None
    exit_reason: "str | None" = None
    pnl: "float | None" = None
    touch_basis: str = "1s"
    n_fvgs: int = 0
    #: Which clause groups were ARMED for this run. `full` is the policy as specified;
    #: the ablation arms record what was withheld so an artifact row is self-describing.
    use_breakeven: bool = True

    @property
    def flipped(self) -> bool:
        return self.flipped_at is not None

    @property
    def ratchet_armed(self) -> bool:
        """Did clause 7 ever have a chance to fire? §2.1's counted quantity."""
        return self.target_status == TARGET_PRESENT

    def to_dict(self) -> dict:
        return {"date": self.date, "track": self.track, "direction": self.direction,
                "fill_ts": None if self.fill_ts is None else self.fill_ts.isoformat(),
                "fill_price": self.fill_price, "initial_stop": self.initial_stop,
                "dol": self.dol, "initial_target": self.initial_target,
                "target_status": self.target_status,
                "flipped_at": (None if self.flipped_at is None
                               else self.flipped_at.isoformat()),
                "moves": [m.to_dict() for m in self.moves],
                "exit_ts": None if self.exit_ts is None else self.exit_ts.isoformat(),
                "exit_price": self.exit_price, "exit_reason": self.exit_reason,
                "pnl": self.pnl, "touch_basis": self.touch_basis,
                "n_fvgs": self.n_fvgs, "ratchet_armed": self.ratchet_armed,
                "use_breakeven": self.use_breakeven}


# --------------------------------------------------------------------------- #
# direction helpers                                                             #
# --------------------------------------------------------------------------- #

def _is_long(direction) -> bool:
    return str(direction or "").upper() in _LONG


def _tighter(a, b, long_: bool):
    """The tighter of two stops — clause 8's comparison, in one place.

    Tighter means CLOSER to price in the favourable direction, which is higher for a long
    and lower for a short. Every clause that proposes a stop goes through this, so a
    clause proposing a looser one is a no-op rather than a special case.
    """
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b) if long_ else min(a, b)


def _beyond(price, level, long_: bool) -> bool:
    """Is `price` beyond `level` in the FAVOURABLE direction?"""
    return price > level if long_ else price < level


def _far_edge(gap, long_: bool) -> float:
    """The gap's ADVERSE edge — `bar1.High` for a bull gap, `bar1.Low` for a bear gap.

    The detector stores the span as `[price_low, price_high]`, so for a bull gap the
    adverse edge is `price_low` and for a bear gap it is `price_high`. Clause 5 puts the
    stop `TRAIL_BUFFER_PTS` BEYOND it, so price must traverse both gaps to reach it.
    """
    return float(gap.price_low) if long_ else float(gap.price_high)


# --------------------------------------------------------------------------- #
# the schedule (1m logic)                                                       #
# --------------------------------------------------------------------------- #

def continuation_fvgs(bars_1m: pd.DataFrame, direction, after_ts,
                      min_height=None) -> list:
    """Direction-matching 1m gaps COMPLETING strictly after `after_ts`, in that order.

    `min_height` is a pre-registered variant, not a fixed choice (§6). The default is no
    filter, which admits the 1.25-pt gap that anchors a stop on Sep 2 — the direct
    mechanism behind the "ratchet converges too fast" risk, and therefore the thing the
    variant exists to measure rather than to prevent.
    """
    want = "bull" if _is_long(direction) else "bear"
    facts, _ = detect_fvgs(bars_1m, TF, TICKER, {})
    out = []
    for f in facts:
        if f.extra.get("direction") != want:
            continue
        exists = pd.Timestamp(f.extra["exists_from"])
        if exists <= pd.Timestamp(after_ts):
            continue
        if min_height is not None and float(f.extra.get("height") or 0.0) < min_height:
            continue
        out.append(f)
    out.sort(key=lambda f: (pd.Timestamp(f.extra["exists_from"]), f.id))
    return out


def _bars_after(bars_1m: pd.DataFrame, ts) -> pd.DataFrame:
    """Bars whose CLOSE falls strictly after `ts`.

    Keyed on the close and not the label: the bar containing the fill closes after the
    fill, so it is a bar the policy may act on, while the bar before it is not.
    """
    ts = pd.Timestamp(ts)
    idx = bars_1m.index
    return bars_1m[idx + pd.Timedelta(TF) > ts]


def build_schedule(bars_1m, *, direction, fill_ts, fill_price, initial_stop, dol,
                   initial_target, min_fvg_height=None, use_breakeven=True) -> tuple:
    """`(moves, flipped_at, n_fvgs)` — every stop move the 1m tape implies, in order.

    Pure and total: no tape walk, no exits, no 1s bars. Each move carries the instant it
    becomes EFFECTIVE, which is always the open of the bar after the one that triggered
    it, so nothing here can act on a value dated later than the bar it read.
    """
    long_ = _is_long(direction)
    fill_ts = pd.Timestamp(fill_ts)
    span = pd.Timedelta(TF)
    raw: list = []

    # -- clauses 3 and 5: the FVG trail ------------------------------------- #
    gaps = continuation_fvgs(bars_1m, direction, fill_ts, min_height=min_fvg_height)
    be_at = None
    for i, gap in enumerate(gaps):
        at = pd.Timestamp(gap.extra["exists_from"])
        if i == 0:
            # Clause 3: the FIRST completion moves the stop to the fill price EXACTLY.
            # With `use_breakeven=False` (the C/D ablation arms) the first gap produces no
            # move at all, and the trail still starts from the SECOND — clause 5 always
            # reads the PREVIOUS gap, so gap 0 remains the anchor it reads from even when
            # it no longer triggers a move of its own.
            if not use_breakeven:
                continue
            be_at = at
            raw.append(StopMove(at, float(fill_price), 3,
                                f"first continuation FVG completed ({gap.label})"))
        else:
            prev = gaps[i - 1]
            edge = _far_edge(prev, long_)
            price = edge - TRAIL_BUFFER_PTS if long_ else edge + TRAIL_BUFFER_PTS
            raw.append(StopMove(at, price, 5,
                                f"previous FVG far edge {edge} -/+ {TRAIL_BUFFER_PTS}"))

    forward = _bars_after(bars_1m, fill_ts)

    # -- clause 4: the BE fallback ------------------------------------------ #
    # Only meaningful with a DOL to measure the halfway point against; clause 14 makes a
    # session without one a non-entry, so this never runs without it. Suppressed wholesale
    # on the no-breakeven arms: clause 4 is a FALLBACK for clause 3, so removing one
    # without the other would leave a breakeven move by another name.
    if dol is not None and use_breakeven:
        half = float(fill_price) + BE_FALLBACK_FRACTION * (float(dol) - float(fill_price))
        col = "High" if long_ else "Low"
        for ts, row in forward.iterrows():
            reached = (float(row[col]) >= half) if long_ else (float(row[col]) <= half)
            if not reached:
                continue
            at = ts + span
            if be_at is None or at < be_at:
                raw.append(StopMove(at, float(fill_price), 4,
                                    f"50% of fill->DOL reached ({half})"))
            break

    # -- clause 6: the regime flip ------------------------------------------ #
    flipped_at = None
    if initial_target is not None:
        for ts, row in forward.iterrows():
            if _beyond(float(row["Close"]), float(initial_target), long_):
                flipped_at = ts + span
                break

    # -- clause 7: the post-flip ratchet ------------------------------------ #
    if flipped_at is not None:
        after = forward[forward.index + span >= flipped_at]
        for ts, row in after.iterrows():
            body = float(row["Close"]) - float(row["Open"])
            opposite = -body if long_ else body
            if opposite < OPPOSITE_BODY_PTS:
                continue
            wick = float(row["Low"]) if long_ else float(row["High"])
            price = wick - TRAIL_BUFFER_PTS if long_ else wick + TRAIL_BUFFER_PTS
            raw.append(StopMove(ts + span, price, 7,
                                f"opposite bar body {round(opposite, 2)} >= "
                                f"{OPPOSITE_BODY_PTS}, wick {wick}"))

    return _monotone(raw, float(initial_stop), direction), flipped_at, len(gaps)


def _monotone(raw, initial_stop, direction) -> tuple:
    """Clause 8, and clause 7's "both remain active, take the tighter".

    Sorting by instant and folding with `_tighter` does both at once: two clauses landing
    on the same instant resolve to the tighter of the pair, and any proposal looser than
    the standing stop is dropped rather than applied. A dropped move is genuinely a
    no-op — it is not recorded, because a "move" that changed nothing is noise in an
    artifact whose whole purpose is to show what moved the stop and when.

    **The fold is seeded with the MECHANISM'S OWN STOP, not with nothing.** Clause 8 says
    the stop never loosens, and the stop it starts from is clause 2's — whatever the entry
    mechanism output. Seeding with `None` accepts the first proposal unconditionally, and
    clause 7 can be that first proposal: a regime flip needs no FVG, so on a session where
    the target is cleared before any continuation gap completes, a wick-anchored ratchet
    would be free to move the stop BELOW the mechanism's own. That is clause 8 and clause 2
    violated together, and it is silent — the run just books a different exit.
    """
    long_ = _is_long(direction)
    out: list = []
    current = float(initial_stop)
    for move in sorted(raw, key=lambda m: (m.at, m.clause)):
        best = _tighter(current, move.price, long_)
        if current is not None and best == current:
            continue                       # looser or equal: a no-op by clause 8
        current = best
        out.append(move)
    return tuple(out)


# --------------------------------------------------------------------------- #
# the walk (1s touches)                                                         #
# --------------------------------------------------------------------------- #

def run_policy(bars_1m, bars_1s=None, *, date, track, direction, fill_ts, fill_price,
               initial_stop, dol, initial_target=None, min_fvg_height=None,
               use_breakeven=True) -> PolicyRun:
    """The whole policy for one session on one track."""
    long_ = _is_long(direction)
    run = PolicyRun(date=str(date), track=str(track),
                    direction=str(direction).upper(),
                    use_breakeven=bool(use_breakeven))

    # -- clause 14: no DOL, no position ------------------------------------- #
    if dol is None:
        run.exit_reason = NO_ENTRY
        return run

    fill_ts = pd.Timestamp(fill_ts)
    run.fill_ts, run.fill_price = fill_ts, float(fill_price)
    run.initial_stop, run.dol = float(initial_stop), float(dol)

    # -- clauses 12 and 13: what kind of initial target is this -------------- #
    if initial_target is None:
        run.target_status = TARGET_ABSENT
    elif _beyond(float(initial_target), float(dol), long_):
        # Clause 13: beyond the DOL is IGNORED ENTIRELY -- not clamped, not relocated.
        run.target_status = TARGET_BEYOND_DOL
        run.initial_target = float(initial_target)
        initial_target = None
    else:
        run.target_status = TARGET_PRESENT
        run.initial_target = float(initial_target)

    moves, flipped_at, n_fvgs = build_schedule(
        bars_1m, direction=direction, fill_ts=fill_ts, fill_price=fill_price,
        initial_stop=run.initial_stop, dol=dol, initial_target=initial_target,
        min_fvg_height=min_fvg_height, use_breakeven=use_breakeven)
    run.moves, run.flipped_at, run.n_fvgs = moves, flipped_at, n_fvgs

    tape = bars_1s if bars_1s is not None and len(bars_1s) else bars_1m
    run.touch_basis = "1s" if tape is bars_1s else "1m"
    _walk(run, tape, moves, long_)
    return run


def _walk(run: PolicyRun, tape: pd.DataFrame, moves, long_: bool) -> None:
    """Advance the stop on schedule, then test the touches, adverse side first."""
    if tape.index.tz is None:
        # Clause 10 is an ET wall time, and `normalize()` on a naive index would put the
        # hard close on the machine's midnight. The machine's timezone is Bangkok.
        raise ValueError("the tape must be tz-aware ET; a naive index would put clause "
                         "10's 13:00 on the machine's clock")
    hard_close = (run.fill_ts.normalize()
                  + pd.Timedelta(hours=HARD_CLOSE_ET[0], minutes=HARD_CLOSE_ET[1]))
    window = tape[(tape.index >= run.fill_ts) & (tape.index <= hard_close)]
    if not len(window):
        # A silent zero here would read exactly like a flat session. It is not one: it is
        # a tape that does not cover its own fill, which is a data fault.
        raise ValueError(f"{run.date}: no {run.touch_basis} bars between the fill "
                         f"{run.fill_ts} and {hard_close}")
    stop = run.initial_stop
    pending = list(moves)
    last_close = run.fill_price

    for ts, row in window.iterrows():
        while pending and pending[0].at <= ts:
            stop = pending.pop(0).price
        high, low = float(row["High"]), float(row["Low"])
        last_close = float(row["Close"])

        # Clause 9's DOL touch and the stop touch can land on the same bar. Adverse
        # resolution first, exactly as `order_sim` requires and plan 31 §3.1 restates:
        # booking the better of the two would silently inflate every result.
        hit_stop = (low <= stop) if long_ else (high >= stop)
        hit_dol = (high >= run.dol) if long_ else (low <= run.dol)
        if hit_stop:
            _close(run, ts, stop, EXIT_STOP, long_)
            return
        if hit_dol:
            _close(run, ts, run.dol, EXIT_DOL, long_)
            return

    # -- clause 10 ----------------------------------------------------------- #
    # Reported as a hard close only when the tape actually reached 13:00. A tape that
    # simply ran out earlier is a truncated window, not a session that stayed open, and
    # labelling it `hard_close_1300` would put a fabricated exit time in the artifact.
    ts = window.index[-1]
    reason = EXIT_TIME if ts >= hard_close - pd.Timedelta(TF) else EXIT_TAPE_END
    _close(run, ts, last_close, reason, long_)


def _close(run: PolicyRun, ts, price, reason, long_: bool) -> None:
    run.exit_ts, run.exit_price, run.exit_reason = ts, float(price), reason
    run.pnl = round((float(price) - run.fill_price) if long_
                    else (run.fill_price - float(price)), 4)
