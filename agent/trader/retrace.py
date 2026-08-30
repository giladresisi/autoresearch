"""§5's two-phase binding gate: has price retraced INTO this gap on a FRESH tick?

SETTLED 2026-08-26. The retrace-in tick must be strictly after the settle window ends
(09:30:30). Penetrations completed before that — pre-arm or in-window — count for
eligibility and close-through tracking ONLY; they do not satisfy the precondition and do
not arm §2's crossed-trigger execution.

The superseded "or immediately if price is already inside / has already entered it"
clause is DELETED (§11.0's retired list). The 19-day A/B that settled it: FRESH +570.50
vs LITERAL +410.50, and the "max-distance guard alone suffices" hypothesis was REFUTED —
the guard delays the literal entry into a chase rather than blocking it. §11 also notes
the literal reading burns BUDGET as well as price: on 08-25 both readings enter, but
literal spends two attempts (−50.00) where fresh spends one (−25.00).

**"Into" is a CROSSING-IN, not mere presence.** §5 defines it as "any tick ENTERING the
gap's range", and §11's calibration is explicit that "continuous presence inside the gap
at the boundary does NOT count, which is what the validated 08-13 walk did with its fresh
09:32:35 re-entry". So a gap price is already sitting inside at the window end has NOT
been freshly retraced into: it must be left and re-entered. This is the one place this
module diverges from the shape sketched in the executing plan, which tested only "a tick
inside after the window"; the divergence is doc-literal and it is what makes the two
readings distinguishable on exactly the days §11 measured.

**`reset()` is deliberately NOT called on a stop-out**, even though it exists. §2's
stop-out cooldown "acts on the current state" with NO fresh-precondition requirement —
"that is the difference from the settle window" — and re-arming the gate after every stop
IS the §5 re-entry churn guard, which §11.0 lists as retired with zero motivating
examples remaining. The method is here because a caller may legitimately want to forget a
gap (a re-bind onto a different artifact, a test); wiring it to stop-outs would forfeit
07-24's +146.5 collapse capture, which is exactly the lockout §2's cooldown rule was
built to kill.
"""
from __future__ import annotations

import pandas as pd


class RetraceGate:
    """Per-gap two-phase state: OUTSIDE seen, then a fresh entry.

    Cheap enough for the per-second path — a handful of float comparisons per live gap.
    """

    def __init__(self, settle_end: pd.Timestamp) -> None:
        self._settle_end = settle_end
        self._seen: set = set()          # gaps that have earned a fresh retrace
        self._outside: set = set()       # gaps price has been observed OUTSIDE of

    # -- the tape -------------------------------------------------------------- #

    def note_tick(self, gap_id, price, gap_low, gap_high, now,
                  *, low=None, high=None) -> None:
        """Record one observation of `price` against one gap.

        `low`/`high` let a caller hand over a 1s bar's RANGE rather than a single print;
        they default to `price`. The range basis matters: `OrderSim` fills against the
        bar's extremes, so a retrace test reading only the close would miss a tick the
        tape demonstrably made — and would then place an order the fill model believes
        was already reachable.
        """
        if gap_id in self._seen or price is None or now is None:
            return
        lo = float(low if low is not None else price)
        hi = float(high if high is not None else price)
        g_lo, g_hi = float(gap_low), float(gap_high)

        touches = hi >= g_lo and lo <= g_hi
        if not touches:
            # Wholly beyond the gap on one side: the next penetration is an ENTRY.
            self._outside.add(gap_id)
            return

        if now <= self._settle_end:
            # In-window / pre-arm penetration: eligibility tracking ONLY (§2). It also
            # cancels any standing OUTSIDE flag, so a penetration that merely straddles
            # the boundary cannot be re-read as a fresh crossing one second later.
            self._outside.discard(gap_id)
            return

        if gap_id in self._outside:
            self._outside.discard(gap_id)
            self._seen.add(gap_id)

    # -- queries --------------------------------------------------------------- #

    def fresh_entry_seen(self, gap_id) -> bool:
        return gap_id in self._seen

    def reset(self, gap_id) -> None:
        """Forget a gap entirely — it must be left and re-entered to qualify again.

        NOT wired to stop-outs; see the module docstring.
        """
        self._seen.discard(gap_id)
        self._outside.discard(gap_id)

    def state(self) -> dict:
        return {"settle_end": self._settle_end,
                "fresh": sorted(self._seen), "outside": sorted(self._outside)}
