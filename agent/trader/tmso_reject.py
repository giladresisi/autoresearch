"""`tmso_reject`: a sweep of the true micro-session open that closes back across it.

**The observation this came from (2026-09-03, MNQ).** Micro-session 09:00-10:30, TMSO
29214.50:

    09:36  O 29250.25  H 29254.50  L 29199.25  C 29220.75   swept 15.25 below, closed above, RED
    09:37  O 29221.00  H 29264.50  L 29216.50  C 29260.75   GREEN  -> entry at its close

and the identical shape at 10:56 in the next micro-session (TMSO 29292.50, swept to
29246.00, closed 29296.75). Both preceded the day's two up-legs.

**Why it is not just §7 again.** `extreme_reject_close` waits for a sweep of the DAY
extreme and requires the sweeping bar to close in the thesis direction against ITS OWN
OPEN. On 09-03 §7 was armed at the 09:35 close and saw the 09:36 bar — it declined,
because 09:36 closed RED. This mechanism's discriminator is different: the bar must close
back across the SWEPT LEVEL, which 09:36 does (29220.75 > 29214.50). Same machine shape,
different anchor and different accept test, and on that bar they disagree.

**TMSO.** ICT's true-session-open idea at micro scale: a 90-minute micro-session divided
into quarters, and the open of Q2 is the session's "true" open. The grid here starts at
09:00 ET (09:00 / 10:30 / 12:00), so Q2 opens at start + 22.5 min.

    THE GRID ANCHOR IS AN OPEN RULE QUESTION, not a settled one. 09:00 is what the
    motivating observation used, but it is not a boundary this repo models anywhere: the
    CME session opens 18:00 and RTH opens 09:30. An 18:00 + n*90 grid or a 09:30 grid
    would place Q2 elsewhere and select different bars. Settle it before this is trusted.

**Deliberately NOT a fact and NOT a DOL candidate.** TMSO is computed here, from the bars
the bar loop already carries. It never enters `derive_facts`, `build_menus`, the L1 schema
or the KB — which matters because `thesis_cache` keys on the KB bytes, so touching them
re-seeds every recorded thesis. It is a level to reject at, never a level to draw toward.

**The confirmation is a close WITH THE THESIS — on the sweep bar itself, or on the next
bar (2026-09-17).** The sweep bar alone fires far too often: on 09-03's 09:00
micro-session seven bars touch TMSO and close back above it. So a sweep is never enough;
what enters is the FIRST bar that closes with the thesis against its own open:

    sweep bar closes back across TMSO AND with the thesis  -> enter at ITS close
    sweep bar closes back across TMSO but AGAINST it       -> armed; the NEXT bar must
                                                              close with the thesis

The second line is the original two-bar signature, unchanged (09-03: 09:36 sweeps and
closes RED under an UP thesis, 09:37 confirms). The first line is `SWEEP_BAR_MAY_CONFIRM`,
and it is the same reading §7 was given in plan 36 D3 — "the first with-thesis close
enters, the same bar or the one after" — so the two rejection mechanisms say one thing.

Motivating day 2026-09-17, DOWN thesis: the 09:30 bar trades to 29737.75 over TMSO and
closes 29717.50, under it and RED by a tick. One-bar enters 09:31:00 @ 29717.50; the
two-bar form waited for 09:31 to close and entered 09:32:00 @ 29674.75, 42.75 pts later
in a move that was already running.

Measured SIGNAL-LEVEL over the 32 stretch-override sessions since 2026-05-01 (direction =
the override's, no oracle; stop capped 15, held 45 min): two-bar 23 fires / 3 survive /
-119.50 pts; with the sweep bar allowed to confirm 24 fires / 6 survive / +564.75. ONE
extra fire, because the per-micro-session latch still caps it — the docstring's original
worry does not return. Three trades carry the gain (08-06 +278.25, 08-19 +249.25, 08-12
+69.00), so treat the size of the edge as unproven and its sign as a lead.

**No opening-bar exclusion here**, unlike `fvg_1h_reject`. That exclusion exists because on
the 09:30 bar every extreme is "a new post-09:30 extreme" by construction. TMSO is a level
fixed at 09:22:30, before the bar opens, so a 09:30 bar that sweeps it and closes back is
real information. The five 09:31:00 entries in the sample: 2 held (+69.00, +86.75), 3
stopped at the cap — net +110.75. Thin, positive, and not the bulk of the gain.

**UNTUNED, and stated as such.** `SL_CAP_PTS` is borrowed from §7 and has been fitted to
nothing. No A/B exists. This is a candidate under
`docs/entry-mechanism-change-protocol.md`, not an adopted mechanism.
"""
from __future__ import annotations

import pandas as pd

MICRO_SESSION_MINUTES = 90.0
#: Q2 of four equal quarters -> one quarter after the micro-session opens.
QUARTER_FRACTION = 0.25
#: The ET clock the micro-session grid starts from. See the docstring's open question.
GRID_START_HHMM = (9, 0)
#: Borrowed from §7 (`extreme_reject.SL_CAP_PTS`). NOT fitted for this mechanism.
SL_CAP_PTS = 15.0
#: A sweep bar that ALSO closes with the thesis enters at its own close instead of waiting
#: for the next bar. False restores the strict two-bar signature — one line, so the A/B
#: the docstring asks for is cheap.
SWEEP_BAR_MAY_CONFIRM = True

_SHORT = ("DOWN", "SHORT")


def micro_session_start(now: pd.Timestamp) -> "pd.Timestamp | None":
    """The start of the micro-session containing `now`, or None before the grid opens."""
    if now is None:
        return None
    grid = now.normalize() + pd.Timedelta(hours=GRID_START_HHMM[0],
                                          minutes=GRID_START_HHMM[1])
    if now < grid:
        return None
    n = int((now - grid) / pd.Timedelta(minutes=MICRO_SESSION_MINUTES))
    return grid + pd.Timedelta(minutes=MICRO_SESSION_MINUTES * n)


def tmso_for(mnq, now: pd.Timestamp):
    """(price, q2_timestamp) of the current micro-session's true open, or (None, None).

    The Q2 OPEN — the first print at or after the quarter boundary — never a close and
    never an average. Returns None while the quarter has not opened yet, which is a real
    state for the first 22.5 minutes of every micro-session.
    """
    start = micro_session_start(now)
    if start is None or mnq is None or not len(mnq):
        return None, None
    q2 = start + pd.Timedelta(minutes=MICRO_SESSION_MINUTES * QUARTER_FRACTION)
    if now < q2:
        return None, None
    seg = mnq[(mnq.index >= q2) & (mnq.index <= now)]
    if not len(seg):
        return None, None
    return float(seg.iloc[0]["Open"]), q2


class TmsoReject:
    """Sweep of TMSO that closes back across it, confirmed by the first with-thesis close
    — the sweep bar's own, or the next bar's.

    Driven one COMPLETED 1m bar at a time. `on_bar_close` returns a fire dict or None.
    One fire per micro-session: the level is the same all session, so an unlatched
    machine re-fires on every later poke at it.
    """

    def __init__(self, direction: str) -> None:
        self._short = str(direction).upper() in _SHORT
        self._armed_bar = None          # the sweep bar awaiting its confirmation
        self._armed_level = None
        self._armed_extreme = None
        self._fired_sessions: set = set()

    def state(self) -> dict:
        return {"armed": self._armed_bar is not None, "level": self._armed_level,
                "extreme": self._armed_extreme, "short": self._short,
                "fired_sessions": sorted(str(s) for s in self._fired_sessions)}

    # -- the tape -------------------------------------------------------------- #

    def on_bar_close(self, now, bar, tmso) -> "dict | None":
        """`tmso` is the current micro-session's true open (the caller computes it, so
        this stays a pure state machine over bars)."""
        if tmso is None:
            self._armed_bar = None
            return None
        session = micro_session_start(now)
        if session in self._fired_sessions:
            return None

        # Confirmation bar: closes WITH the thesis against its own open.
        if self._armed_bar is not None:
            if self._closes_with_thesis(bar):
                fire = self._fire(now, bar)
                self._armed_bar = None
                self._fired_sessions.add(session)
                return fire
            self._armed_bar = None      # confirmation missed; the setup is spent

        # Sweep bar: traded through TMSO on the adverse side and closed back across it.
        swept = (float(bar["High"]) >= tmso) if self._short else (float(bar["Low"]) <= tmso)
        closed_back = (float(bar["Close"]) < tmso) if self._short \
            else (float(bar["Close"]) > tmso)
        if swept and closed_back:
            self._armed_level = float(tmso)
            self._armed_extreme = (float(bar["High"]) if self._short
                                   else float(bar["Low"]))
            if SWEEP_BAR_MAY_CONFIRM and self._closes_with_thesis(bar):
                # One bar did both jobs. Same fire, same stop rule (this bar's own swept
                # extreme, capped), same one-per-micro-session latch.
                self._armed_bar = None
                self._fired_sessions.add(session)
                return self._fire(now, bar)
            self._armed_bar = now
        return None

    # -- internals ------------------------------------------------------------- #

    def _closes_with_thesis(self, bar) -> bool:
        c, o = float(bar["Close"]), float(bar["Open"])
        return c < o if self._short else c > o

    def _fire(self, now, bar) -> dict:
        """Market entry at the confirmation bar's CLOSE; stop beyond the swept extreme,
        capped. The cap is §7's and is UNTUNED here — on 09-03 the raw swept extreme is
        61.5 pts from the entry, which no other mechanism in this engine would risk."""
        entry = float(bar["Close"])
        wick = self._armed_extreme
        capped = entry + SL_CAP_PTS if self._short else entry - SL_CAP_PTS
        stop = min(wick, capped) if self._short else max(wick, capped)
        return {"time": now, "price": entry, "stop": stop,
                "direction": "DOWN" if self._short else "UP",
                "level": self._armed_level, "mechanism": "tmso_reject"}
