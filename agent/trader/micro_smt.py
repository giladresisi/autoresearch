"""`micro_smt_reject` (O3) and `micro_smt_exit` (O4): a divergence between MNQ and MES at
a completed 90-min micro-session's extreme, confirmed by a 1m close with the signal on
BOTH assets.

**The observation this came from (2026-09-24, MNQ/MES).** The 09:00-10:30 micro-session
highs were MNQ 30638.00 (10:27) and MES 7758.25 (10:08). The bar labelled 10:35 (the
current, 10:30-12:00 micro-session) took MNQ to 30643.00 -- strictly above its own
previous-micro-session high -- while MES reached only 7754.50, never touching 7758.25.
That bar itself closed GREEN (30631.50 -> 30643.00), so it armed but did not confirm; the
next bar, labelled 10:36, closed RED on both (MNQ 30638.00 -> 30613.25, MES 7754.75 ->
7752.25). Under this module's convention a bar's CLOSE is booked at the instant it
COMPLETES, so the confirmation bar's close (30613.25) fires at 10:37:00 -- exactly the
operator's hypothetical entry (`comments.md` 2026-09-24 11:29). The stop is MNQ's own
running micro-session high (30643.00) + 2 pts, capped at 15 pts from entry: min(30645.00,
30628.25) = 30628.25, never touched (the highest later print is 30621.00 at 10:37).

The mirrored exit signature fired at the LOWS of the same pair of micro-sessions: the
09:00-10:30 lows were MNQ 30485.00 (09:05) and MES 7730.00 (09:31). MES undercut its low
at 11:13 (7727.75) while MNQ held above 30485.00; the bar labelled 11:15 closed UP on both
(MNQ 30510.25 -> 30520.00, MES 7726.25 -> 7729.00), booking a close at 11:16:00. Against
the hypothetical 10:37 short @30613.25 that is +93.25 pts of banked MFE toward a T2 that
sat 133 pts further away.

**ADOPTED as a rule, 2026-09-26** (`l2-mechanisms.md` §7a / §7b) — promoted from the §11
CANDIDATE entry after the 30-day A/B (adoption PR description, branch autoresearch/micro-smt-o3o4): summed Δ(B−A)
+54.50 pts over 30 dates, every non-zero per-date Δ traced to an O3 entry or an O4 exit,
no unexplained knock-on. The operator then narrowed O3's window from the A/B's 10:30-12:30
to **10:30 ≤ entry < 11:00 ET**; none of the 3 A/B entries (11:08, 11:16, 12:02) fall
inside that narrower window, so the +54.50 figure is NOT reproduced by the shipped window
— see §7a for the re-run under the narrower window. O4 is wired live (`MirroringOrderPort`
now has `flatten`, plan feat/live-flatten-exits, 2026-09-26).

**Both flags default True.** `MICRO_SMT_ENTRY_ENABLED` gates O3 (a market-entry mechanism,
`micro_smt_reject`); `MICRO_SMT_EXIT_ENABLED` gates O4 (a market-close exit trigger,
`micro_smt_exit`, on an OPEN position regardless of mechanism). Flipped as plain module
attributes -- `agent.trader.micro_smt.MICRO_SMT_ENTRY_ENABLED = False` -- and read at CALL
TIME by every caller (`micro_smt_entry_armed()` / `micro_smt_exit_armed()`, and
`executor.py` reading the module attribute directly), never captured at import, so an A/B
harness (or a rollback) can flip them between runs in one process.

**Definition, pinned here because two readings would disagree:**

  * The micro-session grid is `tmso_reject.micro_session_start` (90 min, unchanged
    09:00 ET anchor -- that anchor is itself still an open question, inherited, not
    re-litigated here).
  * "Previous micro-session extreme" = the high (bearish) / low (bullish) of EACH asset
    over the immediately preceding, COMPLETED 90-min micro-session. The first
    micro-session of the day (09:00-10:30) has no previous one and the mechanism is
    inert until 10:30.
  * Bearish micro-SMT: within the CURRENT micro-session, one asset's High trades
    STRICTLY above its own previous-micro-session high while the other asset's High has
    NOT (yet) exceeded its own previous-micro-session high. Bullish mirrors at the Lows.
    **Pinned reading:** this is a LIVE STATE, not a one-bar coincidence -- it stays armed
    for as long as exactly one of the two assets has broken its own level, and is
    CANCELLED (un-armed, no fire) the moment the second asset also breaks its own level.
    The 2026-09-24 evidence does not distinguish this from the weaker one-bar reading
    (MES never broke 7758.25 before the 10:36 confirmation), so this is a rule-level
    choice made without a discriminating example -- flag it if a future date needs the
    weaker reading.
  * Confirmation: a 1m bar -- the sweep bar itself, or ANY later bar within the same
    micro-session while the divergence still holds -- closes WITH the signal on BOTH
    assets (bearish: Close < Open on MNQ AND on MES; bullish: Close > Open on both).
    Fires on THAT bar's CLOSE, booked at the bar's completion instant (left-labelled
    convention, `executor._completed_1m`).
  * No lookahead: every read is of bars strictly before `now` (the completed 1m bar) or
    the running extreme up to and including it; `previous_micro_extremes` only reads the
    COMPLETED prior micro-session, never the current one.
  * One fire per micro-session (latched), for both the entry and the exit detector,
    matching `tmso_reject`'s convention -- an unlatched machine re-fires on every later
    poke at the same divergence.

**The stop (O3 only).** "The swept extreme" is read off MNQ specifically -- the traded
instrument -- as MNQ's own running High (bearish) / Low (bullish) since the CURRENT
micro-session opened, regardless of which asset (MNQ or MES) is the one that actually
broke its level. `SL_BUFFER_PTS` (2.0, per the operator's worked example) beyond that,
capped at `SL_CAP_PTS` (15.0, borrowed from §7 and `tmso_reject`; UNTUNED here) from
entry -- the nearer of the two, `reject_core.capped_stop`.
"""
from __future__ import annotations

import pandas as pd

from agent.trader.reject_core import capped_stop
from agent.trader.tmso_reject import MICRO_SESSION_MINUTES, micro_session_start

#: O3: a new market-entry mechanism, `micro_smt_reject`. ON by default (operator adoption,
#: 2026-09-26 -- see `l2-mechanisms.md` §7a).
MICRO_SMT_ENTRY_ENABLED = True
#: O4: a market-close exit trigger, `micro_smt_exit`, on any open position. ON by default
#: (operator adoption, 2026-09-26 -- see `l2-mechanisms.md` §7a). Wired live the same day
#: via `MirroringOrderPort.flatten` -- see `executor._drive_micro_smt_exit`.
MICRO_SMT_EXIT_ENABLED = True
#: O3 is EXEMPT from the shared `executor.ENTRY_CUTOFF_ET` (10:30) -- an explicit operator
#: decision (2026-09-24) -- and instead carries its OWN window, pinned by the operator
#: (2026-09-26) to 10:30 <= entry < 11:00 ET rather than the wider 10:30-12:30 originally
#: implemented for the A/B: `((10, 30), (11, 0))`. `None` removes the window entirely
#: (no O3 entry ever), matching the convention `ENTRY_CUTOFF_ET` itself uses for its knob.
MICRO_SMT_ENTRY_WINDOW_ET = ((10, 30), (11, 0))
#: The operator's worked example: MNQ's own swept extreme + 2 pts, capped at 15 from entry.
SL_BUFFER_PTS = 2.0
SL_CAP_PTS = 15.0


def micro_smt_entry_armed() -> bool:
    """`MICRO_SMT_ENTRY_ENABLED`, read live -- see the module docstring on why this is a
    function and not a re-exported name (a `from ... import NAME` would freeze the value
    at import time, which breaks an A/B harness that flips the module attribute)."""
    return bool(MICRO_SMT_ENTRY_ENABLED)


def micro_smt_exit_armed() -> bool:
    return bool(MICRO_SMT_EXIT_ENABLED)


def previous_micro_extremes(mnq, mes, now: pd.Timestamp) -> "dict | None":
    """{mnq_high, mnq_low, mes_high, mes_low} over the COMPLETED micro-session
    immediately before the one containing `now`, or None when that session does not
    exist (before 10:30, or either frame is empty over the window)."""
    if now is None or mnq is None or mes is None or not len(mnq) or not len(mes):
        return None
    cur_start = micro_session_start(now)
    if cur_start is None:
        return None
    prev_start = cur_start - pd.Timedelta(minutes=MICRO_SESSION_MINUTES)
    # `micro_session_start` returns None before the 09:00 grid opens, so the day's FIRST
    # micro-session (09:00-10:30) has no previous one -- exactly the case this guards.
    if micro_session_start(prev_start) != prev_start:
        return None
    mnq_seg = mnq[(mnq.index >= prev_start) & (mnq.index < cur_start)]
    mes_seg = mes[(mes.index >= prev_start) & (mes.index < cur_start)]
    if not len(mnq_seg) or not len(mes_seg):
        return None
    return {"mnq_high": float(mnq_seg["High"].max()), "mnq_low": float(mnq_seg["Low"].min()),
            "mes_high": float(mes_seg["High"].max()), "mes_low": float(mes_seg["Low"].min()),
            "session_start": cur_start}


class MicroSmt:
    """One divergence machine: bearish (watches Highs) or bullish (watches Lows).

    Driven one COMPLETED 1m bar at a time on BOTH assets. `on_bar_close` returns a fire
    dict or None. Reusable for both O3 (fire dict read as an entry: price/stop/direction)
    and O4 (fire dict read as an exit: price/time only) -- the divergence-and-confirmation
    logic is identical either way; only the caller's use of the result differs.
    """

    def __init__(self, kind: str) -> None:
        if kind not in ("bearish", "bullish"):
            raise ValueError(f"kind={kind!r} not in ('bearish', 'bullish')")
        self._bearish = kind == "bearish"
        self._session = None            # the micro-session this state belongs to
        self._prev = None               # {mnq_high, mnq_low, mes_high, mes_low}
        self._mnq_extreme = None        # MNQ's own running extreme THIS micro-session
        self._mnq_broken = False
        self._mes_broken = False
        self._fired_sessions: set = set()

    def state(self) -> dict:
        return {"session": self._session, "mnq_broken": self._mnq_broken,
                "mes_broken": self._mes_broken, "mnq_extreme": self._mnq_extreme,
                "armed": self._mnq_broken != self._mes_broken,
                "fired_sessions": sorted(str(s) for s in self._fired_sessions),
                "bearish": self._bearish}

    def on_bar_close(self, now, mnq_bar, mes_bar, prev_extremes) -> "dict | None":
        """`prev_extremes` is the caller's `previous_micro_extremes(...)` result -- this
        stays a pure state machine over bars, like `tmso_reject.TmsoReject`."""
        if prev_extremes is None or mnq_bar is None or mes_bar is None:
            return None
        session = prev_extremes.get("session_start")
        if session is None:
            return None
        if session != self._session:
            self._session = session     # a new (or the first-seen) micro-session: reset
            self._prev = prev_extremes
            self._mnq_extreme = None
            self._mnq_broken = False
            self._mes_broken = False
        if session in self._fired_sessions:
            return None

        mnq_adverse = float(mnq_bar["High"]) if self._bearish else float(mnq_bar["Low"])
        if self._mnq_extreme is None or (
                mnq_adverse > self._mnq_extreme if self._bearish
                else mnq_adverse < self._mnq_extreme):
            self._mnq_extreme = mnq_adverse

        prev_mnq = self._prev["mnq_high"] if self._bearish else self._prev["mnq_low"]
        prev_mes = self._prev["mes_high"] if self._bearish else self._prev["mes_low"]
        mes_adverse = float(mes_bar["High"]) if self._bearish else float(mes_bar["Low"])
        if self._bearish:
            self._mnq_broken = self._mnq_broken or (mnq_adverse > prev_mnq)
            self._mes_broken = self._mes_broken or (mes_adverse > prev_mes)
        else:
            self._mnq_broken = self._mnq_broken or (mnq_adverse < prev_mnq)
            self._mes_broken = self._mes_broken or (mes_adverse < prev_mes)

        # Exactly one broken: a live divergence. Both, or neither: nothing to confirm --
        # "both broken" CANCELS an armed divergence (the pinned reading; see the module
        # docstring), it does not merely fail to arm a new one.
        if self._mnq_broken == self._mes_broken:
            return None

        if not (self._closes_with(mnq_bar) and self._closes_with(mes_bar)):
            return None

        self._fired_sessions.add(session)
        entry = float(mnq_bar["Close"])
        stop = capped_stop(entry, self._mnq_extreme, buffer_pts=SL_BUFFER_PTS,
                           cap_pts=SL_CAP_PTS, short=self._bearish)
        return {"time": now, "price": entry, "stop": stop,
                "direction": "DOWN" if self._bearish else "UP",
                "swept_by": "MNQ" if self._mnq_broken else "MES",
                "prev_extreme": prev_mnq if self._mnq_broken else prev_mes,
                "mechanism": None}          # the caller names the mechanism (O3 vs O4 use)

    def _closes_with(self, bar) -> bool:
        c, o = float(bar["Close"]), float(bar["Open"])
        return c < o if self._bearish else c > o
