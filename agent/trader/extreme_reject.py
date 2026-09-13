"""§7 `extreme_reject_close`: the first rejecting 1m bar at a swept day extreme.

ICT basis: liquidity sweep / turtle soup. A stop-run through a fresh day extreme that
immediately rejects — the extreme-making 1m bar closing back in the thesis direction —
marks the manipulation completing. This mechanism trades the FIRST reversing 1m bar in
that specific signature, an entry the FVG mechanisms often catch only later.

STALE-EXTREME ANCHOR (re-keyed from DISTANCE to AGE, 2026-08-26). When the counter-thesis
24h day extreme is more than ~2h old AT THE PLAN ARM, the machine tracks the post-09:30
extreme instead. The invariant: this gate identifies the liquidity pool whose sweep the
mechanism waits for, and STALENESS decides which pool that is. An extreme set forty
minutes ago IS the liquidity the market is currently working; one belonging to an earlier
session phase is not, and the RTH leg prints its own signature.

The >150 pt DISTANCE key is RETIRED and must not be reintroduced: 08-07 (152.50) and
08-25 (123.50) are adjacent in distance and need opposite answers. Their ages — 0.60h and
3.55h — separate cleanly, and the measured ages over 07-15..08-25 leave a gap from 0.60h
to 1.90h with nothing in it, so T is not fitted (identical totals for any T in 1.0h-3.0h).

PIN THE MEASUREMENT INSTANT. The old distance key straddled its threshold between a 09:20
and a 09:30 reading of 08-07 (135.75 vs 152.50), so the same day was "strict" or
"fallback" depending only on when you measured. Age is measured at the PLAN ARM, and the
caller must supply that instant.

Scope: §7 lives and dies with the plan's `valid_while`. Unscoped it fires two losing
shorts into the 07-21 11:14/11:28 new-day-high rally, hours after the plan completed.
That scoping belongs to the Executor (which stops binding once a plan dies); this module
is a pure state machine over completed 1m bars.
"""
from __future__ import annotations

import pandas as pd

QUIET_CLOSES_TO_ARM = 3           # §9
SL_CAP_PTS = 15.0                 # §9; the cap15-vs-w3c30 A/B is a DEFERRED knob
STALE_EXTREME_AGE_HOURS = 2.0     # §9; NOT fitted — identical totals for any T in 1.0-3.0

_SHORT = ("DOWN", "SHORT")


def choose_track(age, threshold_h: float = STALE_EXTREME_AGE_HOURS) -> str:
    """Which extreme series §7 follows. `age` is measured AT THE PLAN ARM.

    `None` (no counter-thesis extreme fact yet) keeps the strict track: the fallback is
    an escape hatch for a STALE pool, not a default.
    """
    if age is None:
        return "24h"
    return "post_0930" if age > pd.Timedelta(hours=threshold_h) else "24h"


class ExtremeReject:
    """Quiet-close counter -> armed -> fire on a new-extreme bar that closes with thesis.

    Driven one COMPLETED 1m bar at a time. `on_bar_close` returns a fire dict or None.
    """

    def __init__(self, direction: str, plan_arm_ts, *, track: str = "24h",
                 extreme=None) -> None:
        self._short = str(direction).upper() in _SHORT
        self._arm = plan_arm_ts
        self._track = track
        # The standing counter-thesis extreme. Seeded when the caller already knows it
        # (the 24h track carries an overnight extreme into the RTH session); left None
        # on the post-09:30 track, where the first bar sets it.
        self._extreme = None if extreme is None else float(extreme)
        self._quiet = 0
        self._armed = False
        self._fired = 0
        # §7's FIRST clause: "After 09:30 ET price moves against the thesis and prints a
        # NEW day extreme. From the NEXT 1m bar, count consecutive 1m closes ...". The
        # quiet counter does not run before that sweep. Without this gate a seeded
        # overnight extreme that RTH never approaches still arms the machine off three
        # ordinary opening bars — on 08-10 that produced a spurious 09:36 fire two bars
        # before the day's real new low even printed. A seeded (24h) machine therefore
        # starts unswept; an unseeded (post-09:30) one sweeps on its first bar, which is
        # correct: that bar IS the first post-09:30 extreme.
        self._swept = extreme is None

    # -- inspection ------------------------------------------------------------ #

    def state(self) -> dict:
        return {"armed": self._armed, "quiet": self._quiet,
                "extreme": self._extreme, "track": self._track,
                "short": self._short, "fires": self._fired,
                "swept": self._swept, "age_measured_at": self._arm}

    # -- the tape -------------------------------------------------------------- #

    def on_bar_close(self, now, bar) -> "dict | None":
        """Drive one completed 1m bar. Returns a fire dict, or None.

        Tick-based extreme tracking: the bar's ADVERSE wick, not its close. §10.2's
        lesson is that this tracking has to be tick-level on both tracks — 07-31's day
        low was first taken out at 09:54:02, and both a 1m walk and a first 1s pass
        mis-timed it by minutes by reading closes.
        """
        adverse = float(bar["High"]) if self._short else float(bar["Low"])
        new_extreme = (self._extreme is None
                       or (adverse > self._extreme if self._short
                           else adverse < self._extreme))

        if new_extreme:
            self._extreme = adverse
            self._swept = True
            # A new-extreme TICK restarts the count WHATEVER the close colour — 08-10's
            # 09:35 bar crossed the 09:34 low seconds in and restarted the count despite
            # closing green.
            self._quiet = 0
            if self._armed and self._closes_in_thesis_direction(bar):
                self._fired += 1
                return self._fire(now, bar)
            # Adverse close: no fire, and NO disarm — the graph may still want to
            # extend, so stay armed for the next new-extreme bar.
            return None

        if not self._swept:
            return None            # nothing has been swept yet; there is nothing to reject
        self._quiet += 1
        if self._quiet >= QUIET_CLOSES_TO_ARM:
            self._armed = True
        return None

    # -- internals ------------------------------------------------------------- #

    def _closes_in_thesis_direction(self, bar) -> bool:
        c, o = float(bar["Close"]), float(bar["Open"])
        return c < o if self._short else c > o

    def _fire(self, now, bar) -> dict:
        """Market entry at the bar's CLOSE; stop at the opposite wick, capped at 15 pts.

        The cap places the stop inside the swept zone — a plain retest of the sweep kills
        the trade — which is the accepted cost of the tighter loss. The wick+3-capped-30
        alternative is a §9 knob A/B and is DEFERRED; cap15 stays the default.
        """
        entry = float(bar["Close"])
        wick = float(bar["High"]) if self._short else float(bar["Low"])
        capped = entry + SL_CAP_PTS if self._short else entry - SL_CAP_PTS
        stop = min(wick, capped) if self._short else max(wick, capped)
        return {"time": now, "price": entry, "stop": stop,
                "direction": "DOWN" if self._short else "UP",
                "mechanism": "extreme_reject_close"}
