"""`fvg_1h_reject`: a new post-09:30 extreme INSIDE the 07:00 1h FVG that closes back.

**The observation (2026-09-02, MNQ).** The 07:00 1h bar leaves a bull FVG
[28995.50, 29033.00] — identified by the middle bar, existing once the 08:00 bar closes at
09:00, so it is fully formed before the 09:20 arm. Price works down into it all morning and
the 09:55 1m bar makes the session's low at 29017.25, inside the zone, and closes UP
against its own open. Entry at that close, 09:56:00 at 29030.75: +181 pts of favourable
excursion over the next 45 minutes against 7.25 adverse.

**Measured over the 17 sessions (2026-05-01..09-11) that have a 07:00 1h FVG on MNQ**, the
signature fires 6 times:

    06-08 10:01  +236.00 / -18.00      07-02 09:31  +215.50 / -48.75
    07-09 09:33  +230.00 / -20.25      08-20 09:31   +38.00 / -123.00
    08-10 10:12   +94.00 / -19.75      09-02 09:56  +181.00 /  -7.25

Four of the six survive a 25-pt stop and run 94-236 pts. **Both failures fire on the 09:30
bar**, and that is not coincidence: on the first bar of the session every extreme is "a new
post-09:30 extreme" by construction, so the signature is nearly free there and carries no
information. `SKIP_OPENING_BAR` excludes it, which removes both losers at no cost to the
four winners. n=6 — this is a lead, not a result.

**Scope, deliberately narrow.** The 07:00 middle bar is the specification, not a
generalisation: it is the most recent 1h FVG that is fully formed before the arm, and it is
what the numbers above were measured on. Widening this to "any live same-direction 1h FVG"
is UNTESTED and would invalidate the measurement.

**Not a fact, and not a DOL candidate.** The zone is derived here with `derive_facts.fvgs`
— the same detector the facts layer uses, so the definition cannot drift — from the bars
the loop already carries. Nothing enters `derive_facts`, `build_menus`, the L1 schema or
the KB, so no recorded thesis is re-keyed. It is a level to reject at, never one to draw
toward. (L1 may separately cite the same zone as P5 evidence once it has been VISITED;
on 09-02 it correctly did not, because at 09:20 price had not yet entered it.)

**The both-assets filter is NOT applied.** Requiring MES to carry its own 07:00 FVG drops
07-09 (+230) and 08-10 (+94) — two of the four winners — while dropping one loser and
keeping the other. It is separable and should be measured on its own once there are more
fires to filter.
"""
from __future__ import annotations

import pandas as pd

from agent.derive_facts import fvgs
from agent.trader.reject_core import capped_stop, closes_with_thesis, is_short

#: The FVG's MIDDLE bar. Its third bar (08:00) completes at 09:00, before the 09:20 arm.
MIDDLE_HOUR = 7
#: The session open the 1h grid is anchored on, matching the repo's convention.
SESSION_OPEN_HOUR = 18
#: RTH open. The bar labelled here is excluded — see the module docstring.
SKIP_OPENING_BAR = (9, 30)
#: Borrowed from §7. NOT fitted for this mechanism; see `reject_core`.
SL_BUFFER_PTS = 3.0
SL_CAP_PTS = 25.0


def zone_at_0700(mnq, now) -> "dict | None":
    """The 07:00 1h FVG as of `now`, or None.

    Uses `derive_facts.fvgs` on 18:00-anchored 1h bars, so the zone definition is
    production's own: bull = next_low > prev_high -> [prev_high, next_low].
    """
    if mnq is None or not len(mnq) or now is None:
        return None
    try:
        day = pd.Timestamp(now).normalize()
        anchor = day - pd.Timedelta(hours=24 - SESSION_OPEN_HOUR)
        seg = mnq.loc[anchor:now]
        if not len(seg):
            return None
        h = seg.resample("1h", origin=anchor).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        h.columns = [c.lower() for c in h.columns]
        for ts, kind, lo, hi, third in fvgs(h):
            if ts.hour == MIDDLE_HOUR and ts.date() == day.date():
                # EXISTENCE is third-bar completion, never the middle bar's own stamp —
                # the §2 convention, and the §11 erratum that admitted every gap ten
                # minutes early for a whole cycle.
                if now < third + pd.Timedelta(hours=1):
                    return None
                return {"kind": kind, "lo": float(lo), "hi": float(hi)}
    except Exception:
        return None
    return None


class FvgReject:
    """New post-09:30 extreme inside the zone + a close back with the thesis.

    Driven one COMPLETED 1m bar at a time. `on_bar_close` returns a fire dict or None.
    One fire per plan: the zone does not change, so an unlatched machine re-fires on
    every later poke into it.
    """

    def __init__(self, direction: str) -> None:
        self._short = is_short(direction)
        self._extreme = None            # the running post-09:30 adverse extreme
        self._fired = False

    def state(self) -> dict:
        return {"extreme": self._extreme, "fired": self._fired, "short": self._short}

    def on_bar_close(self, now, bar, zone) -> "dict | None":
        """`zone` is `{lo, hi, kind}` (the caller supplies it, so this stays a pure state
        machine over bars). A zone facing the wrong way is not a continuation zone."""
        if self._fired or zone is None or bar is None:
            return None
        if zone.get("kind") != ("bear" if self._short else "bull"):
            return None

        adverse = float(bar["High"]) if self._short else float(bar["Low"])
        new_extreme = self._extreme is None or (adverse > self._extreme if self._short
                                                else adverse < self._extreme)
        if new_extreme:
            self._extreme = adverse

        # The opening bar is excluded AFTER the extreme is tracked: it still seeds the
        # running extreme (otherwise the second bar inherits "first extreme of the
        # session" and the exclusion just moves the problem one bar later), it simply
        # cannot fire.
        label = pd.Timestamp(now) - pd.Timedelta(minutes=1)
        if (label.hour, label.minute) == SKIP_OPENING_BAR:
            return None

        if not new_extreme:
            return None
        if not (zone["lo"] <= adverse <= zone["hi"]):
            return None
        if not closes_with_thesis(bar, self._short):
            return None

        self._fired = True
        entry = float(bar["Close"])
        return {"time": now, "price": entry,
                "stop": capped_stop(entry, adverse, buffer_pts=SL_BUFFER_PTS,
                                    cap_pts=SL_CAP_PTS, short=self._short),
                "direction": "DOWN" if self._short else "UP",
                "zone": [zone["lo"], zone["hi"]], "mechanism": "fvg_1h_reject"}
