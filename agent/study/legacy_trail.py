"""The INCUMBENT's position management, reconstructed for study — a trail, not a ladder.

**What this corrects.** Plan 33 and the earlier legacy-ladder study both modelled the
incumbent as a take-profit ladder: reach the cautious rungs, exit. `trend.py` says
otherwise. The rungs never exit a position — **they arm and then trail a structural
stop**, and the only take-profit-on-touch in the file (`SECONDARY_TP_ON_TOUCH_0930`) is
`False` by default and window-limited to 09:30-09:45. An incumbent modelled as "static
stop plus two take-profits" understates it badly.

So the honest comparison against this plan's clause 5 is **trail against trail, differing
only in the anchor**: the incumbent trails off same-direction 5m REFERENCE BARS floored at
breakeven; clause 5 trails off the PREVIOUS FVG's far edge.

**The rules are imported, not restated.** `_last_same_dir_ref_bar` and
`_floored_break_price` come from `trend.py` itself — they are pure functions of bars,
direction and fill price. What is reproduced here is the STATE MACHINE around them, which
is a `run_trend` branch structure over mutable `position.json` state and cannot be called
directly. That reproduction is the part most likely to be subtly wrong, which is why
`test_legacy_trail.py` pins it against recorded `new-stop-exit` / `move-stop-exit` events
from real 1s regression runs before any corpus number is produced.

**The state machine** (`trend.py:478-730`), for a long; mirrored for a short:

  `no` -> the position is unarmed and the mechanism's own stop stands.
     * secondary surpassed (wick) AND closed beyond -> `secondary`, stop = the last
       same-direction 1m reference bar's body bound floored at the fill (no buffer);
       or the FILL PRICE itself when the secondary is at ATH territory.
     * secondary surpassed, no close beyond -> `secondary_surpassed`, no stop yet.
     * initial surpassed, but ONLY if the rung is at least `INITIAL_STOP_MIN_DIST_PTS`
       from the fill -- otherwise touching it confirms nothing and the original stop
       stands.
       - closed beyond -> `initial`, stop = the last same-direction 5m reference bar's
         body bound floored at fill -/+ `BREAKEVEN_BUFFER_PTS`.
       - wick only -> `initial_surpassed`, stop = the MIDPOINT of the original stop and
         the initial rung.
  `initial_surpassed` -> break-check the midpoint stop; upgrade on the secondary; arm on
     a close beyond the initial OR on an opposite-close pullback bar, tighten-only.
  `initial` / `secondary` -> upgrade to secondary where applicable, then TRAIL every 5m
     off the newest same-direction 5m reference bar, tighten-only, and break-check.

**Deviations, and they are the same four `legacy_liquidities` records** plus two more that
belong to this layer:

  * `session_ath` is the historical data max rather than `global.json`'s live value. It
    affects only the `_ath_secondary` branch (secondary at or above the session ATH), and
    only when the secondary rung is `day_high` or `week_high`.
  * The ATH-secondary 20-minute confirmation branch is NOT reproduced: it changes when a
    position leaves, not where the stop sits, and it fires only inside that branch. Runs
    that enter it are flagged rather than silently approximated.

**STATUS: the end-to-end fidelity gate FAILS; the module is used only as a BRACKETED
SENSITIVITY.** Two numbers, and they must travel together:

  * the stop-price ARITHMETIC reproduces **39 of 39** recorded stops exactly;
  * the SCHEDULE does not — 8 of 17 rung names, 4 of 39 stops at the right minute-and-price.

For a trailing system the schedule IS the behaviour, so "right price, wrong time" is not a
small error. The residual is the LADDER this consumes, which depends on
`cautious_dist_shrinks`, the depletion set, and list iteration order — none recoverable from
bars. Rather than guess them, plan 33 §9 SWEEPS them (0-3 x full/pruned pool) and reports
whether the verdict is stable; it is, in sign, at all eight configurations.

**The only claim any number from this module may make:** *the incumbent's management rules,
as we implement them, applied to these entries, across the plausible range of one
unrecoverable parameter.* **Never "what the incumbent would have done."** See plan 33 §9.

READ-ONLY. No legacy state is loaded or written; every input is passed in.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trend import (BREAKEVEN_BUFFER_PTS, INITIAL_STOP_MIN_DIST_PTS,
                   _floored_break_price, _last_same_dir_ref_bar)

TZ = "America/New_York"
ATH_SECONDARY_LEVELS = frozenset({"day_high", "week_high"})

STATE_NONE = "no"
STATE_INITIAL_SURPASSED = "initial_surpassed"
STATE_INITIAL = "initial"
STATE_SECONDARY_SURPASSED = "secondary_surpassed"
STATE_SECONDARY = "secondary"

EXIT_STOP = "stop-exit"
EXIT_ORIGINAL_STOP = "original-stop"
EXIT_TIME = "session-end"


@dataclass(frozen=True)
class StopEvent:
    """One recorded stop placement or move, shaped like `trend.py`'s own event."""
    kind: str                 # new-stop-exit | move-stop-exit
    at: pd.Timestamp
    price: float
    level: str                # initial | initial_mid | secondary
    level_name: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at.isoformat(), "price": self.price,
                "level": self.level, "level_name": self.level_name}


@dataclass
class LegacyRun:
    date: str
    direction: str
    fill_ts: pd.Timestamp
    fill_price: float
    original_stop: float
    cautious_initial: "float | None" = None
    cautious_secondary: "float | None" = None
    events: list = field(default_factory=list)
    exit_ts: "pd.Timestamp | None" = None
    exit_price: "float | None" = None
    exit_reason: "str | None" = None
    pnl: "float | None" = None
    final_state: str = STATE_NONE
    ath_secondary: bool = False

    def to_dict(self) -> dict:
        return {"date": self.date, "direction": self.direction,
                "fill_ts": self.fill_ts.isoformat(), "fill_price": self.fill_price,
                "original_stop": self.original_stop,
                "cautious_initial": self.cautious_initial,
                "cautious_secondary": self.cautious_secondary,
                "events": [e.to_dict() for e in self.events],
                "exit_ts": None if self.exit_ts is None else self.exit_ts.isoformat(),
                "exit_price": self.exit_price, "exit_reason": self.exit_reason,
                "pnl": self.pnl, "final_state": self.final_state,
                "ath_secondary": self.ath_secondary}


def _is_long(direction) -> bool:
    return str(direction or "").lower() in ("up", "long")


def run_legacy_trail(bars_1m: pd.DataFrame, *, date, direction, fill_ts, fill_price,
                     original_stop, cautious_initial=None, cautious_secondary=None,
                     level_initial="", level_secondary="", session_ath=None,
                     end_ts=None, bars_1s=None) -> LegacyRun:
    """Walk 1m bars from the fill and reproduce the incumbent's stop schedule.

    `bars_1m` must cover enough history before the fill for the 5m reference-bar lookup;
    the walk itself starts at the first bar strictly after `fill_ts`, because `run_trend`
    is called on COMPLETED bars.
    """
    long_ = _is_long(direction)
    d = "up" if long_ else "down"
    fill_ts = pd.Timestamp(fill_ts)
    run = LegacyRun(date=str(date), direction=d, fill_ts=fill_ts,
                    fill_price=float(fill_price), original_stop=float(original_stop),
                    cautious_initial=cautious_initial,
                    cautious_secondary=cautious_secondary)
    run.ath_secondary = bool(
        level_secondary in ATH_SECONDARY_LEVELS and session_ath
        and cautious_secondary is not None and float(cautious_secondary) >= float(session_ath))

    state = STATE_NONE
    cbp = None                                  # cautious_break_price
    walk = bars_1m[bars_1m.index > fill_ts]
    if end_ts is not None:
        walk = walk[walk.index <= pd.Timestamp(end_ts)]

    for ts, bar in walk.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        op, cl = float(bar["Open"]), float(bar["Close"])
        recent = bars_1m[bars_1m.index <= ts]
        bar_time_str = ts.isoformat()

        surpassed = (lambda p: hi >= p) if long_ else (lambda p: lo <= p)
        close_beyond = (lambda p: cl > p) if long_ else (lambda p: cl < p)
        opp_close = (cl < op) if long_ else (cl > op)
        broke = (lambda p: lo < p) if long_ else (lambda p: hi > p)

        # -- the ORIGINAL stop stands until something arms -------------------- #
        if cbp is None:
            orig_hit = (lo <= run.original_stop) if long_ else (hi >= run.original_stop)
            if orig_hit:
                _close(run, ts, run.original_stop, EXIT_ORIGINAL_STOP, long_, state)
                return run
        elif broke(cbp):
            _close(run, ts, cbp, EXIT_STOP, long_, state)
            return run

        sec, ini = run.cautious_secondary, run.cautious_initial

        if state in (STATE_NONE, STATE_INITIAL_SURPASSED):
            if sec is not None and surpassed(sec):
                if close_beyond(sec):
                    had = cbp is not None
                    cbp = (run.fill_price if run.ath_secondary else _floored_break_price(
                        _last_same_dir_ref_bar(recent, bar_time_str, d), d,
                        run.fill_price))
                    state = STATE_SECONDARY
                    if cbp is not None:
                        run.events.append(StopEvent(
                            "move-stop-exit" if had else "new-stop-exit", ts, float(cbp),
                            "secondary", level_secondary))
                    continue
                state = STATE_SECONDARY_SURPASSED
                continue

            if state == STATE_NONE:
                if ini is not None and surpassed(ini):
                    if abs(ini - run.fill_price) < INITIAL_STOP_MIN_DIST_PTS:
                        continue          # too close to the entry to confirm anything
                    if close_beyond(ini):
                        cbp = _floored_break_price(
                            _last_same_dir_ref_bar(recent, bar_time_str, d,
                                                   period_minutes=5),
                            d, run.fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
                        state = STATE_INITIAL
                        if cbp is not None:
                            run.events.append(StopEvent("new-stop-exit", ts, float(cbp),
                                                        "initial", level_initial))
                        continue
                    state = STATE_INITIAL_SURPASSED
                    cbp = (float(run.original_stop) + float(ini)) / 2.0
                    run.events.append(StopEvent("new-stop-exit", ts, float(cbp),
                                                "initial_mid", level_initial))
                continue

            # state == initial_surpassed: arm on a close beyond OR an opposite close
            if ini is not None and (close_beyond(ini) or opp_close):
                new = _floored_break_price(
                    _last_same_dir_ref_bar(recent, bar_time_str, d, period_minutes=5),
                    d, run.fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
                had = cbp is not None
                if new is not None and (cbp is None or _tighter_than(new, cbp, long_)):
                    cbp = new
                state = STATE_INITIAL
                if cbp is not None:
                    if broke(cbp):
                        _close(run, ts, cbp, EXIT_STOP, long_, state)
                        return run
                    run.events.append(StopEvent(
                        "move-stop-exit" if had else "new-stop-exit", ts, float(cbp),
                        "initial", level_initial))
            continue

        if state == STATE_SECONDARY_SURPASSED:
            if sec is not None and ((opp_close and not close_beyond(sec))
                                    or close_beyond(sec)):
                had = cbp is not None
                cbp = (run.fill_price if run.ath_secondary else _floored_break_price(
                    _last_same_dir_ref_bar(recent, bar_time_str, d), d, run.fill_price))
                state = STATE_SECONDARY
                if cbp is not None:
                    run.events.append(StopEvent(
                        "move-stop-exit" if had else "new-stop-exit", ts, float(cbp),
                        "secondary", level_secondary))
            continue

        # -- armed: upgrade to secondary, then TRAIL every 5m, tighten-only ---- #
        if state in (STATE_INITIAL, STATE_SECONDARY):
            if state == STATE_INITIAL and sec is not None and surpassed(sec):
                if close_beyond(sec):
                    had = cbp is not None
                    cbp = (run.fill_price if run.ath_secondary else _floored_break_price(
                        _last_same_dir_ref_bar(recent, bar_time_str, d), d,
                        run.fill_price))
                    state = STATE_SECONDARY
                    if cbp is not None:
                        run.events.append(StopEvent(
                            "move-stop-exit" if had else "new-stop-exit", ts, float(cbp),
                            "secondary", level_secondary))
                    continue
                state = STATE_SECONDARY_SURPASSED
                continue
            if run.ath_secondary and state == STATE_SECONDARY:
                continue                  # 20m-confirmation branch: no trail (see module docstring)
            new = _floored_break_price(
                _last_same_dir_ref_bar(recent, bar_time_str, d, period_minutes=5), d,
                run.fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
            if new is not None and cbp is not None and _tighter_than(new, cbp, long_):
                cbp = new
                run.events.append(StopEvent(
                    "move-stop-exit", ts, float(cbp),
                    "initial" if state == STATE_INITIAL else "secondary",
                    level_initial if state == STATE_INITIAL else level_secondary))

    ts = walk.index[-1] if len(walk) else fill_ts
    last = float(walk["Close"].iloc[-1]) if len(walk) else run.fill_price
    _close(run, ts, last, EXIT_TIME, long_, state)
    return run


def _tighter_than(a, b, long_: bool) -> bool:
    return (a > float(b)) if long_ else (a < float(b))


def _close(run: LegacyRun, ts, price, reason, long_: bool, state) -> None:
    run.exit_ts, run.exit_price, run.exit_reason = ts, float(price), reason
    run.final_state = state
    run.pnl = round((float(price) - run.fill_price) if long_
                    else (run.fill_price - float(price)), 4)
