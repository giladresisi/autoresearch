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
    micro-session of the day (09:00-10:30) has no previous one ON THE GRID, so O4 is
    inert until 10:30 -- but O3's ENTRY reads 07:30-09:00 as its predecessor (§7a.1,
    below).
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
    COMPLETED prior micro-session, never the current one, and the whole-session catch-up
    (below) reads only bars labelled before the current bar's own label.
  * One fire per micro-session (latched), for both the entry and the exit detector,
    matching `tmso_reject`'s convention -- an unlatched machine re-fires on every later
    poke at the same divergence.

**§7a.1 -- the pre-open pair (operator, 2026-09-28).** For the 09:00-10:30 micro-session
ONLY, O3's predecessor is 07:30-09:00 (the same grid one step back;
`entry_previous_micro_extremes`), with its own entry window `MICRO_SMT_PREOPEN_WINDOW_ET`
= 09:30 <= entry < 10:30 ET. Motivating day 2026-09-28: 07:30-09:00 highs MNQ 30753.50,
MES 7781.50; bar 09:32 takes MNQ to 30759.00 while MES holds, bar 09:33 closes down on
both -> fires 09:34:00 @ 30707.75, stop 30722.75. O4 keeps `previous_micro_extremes`.

**The divergence is a live state over the WHOLE micro-session (§7a.1, both pairs).** The
Executor asks O3 only on bars where it may enter -- inside its window, flat, out of
cooldown -- but a break on any other bar of the micro-session still counts. The caller
passes its session frames (`mnq_hist` / `mes_hist`, 1s or 1m, truncated to `now`), and
`on_bar_close` folds every bar from the micro-session's open up to the current bar's label
into the broken flags and MNQ's running extreme before judging the current bar, one
COMPLETED 1m bar at a time (1s history is resampled, left-labelled). Nothing FIRES during
the catch-up, but -- operator, 2026-09-29 -- the FIRST confirmation on a bar completing at
or after the entry window's open (`latch_from`, `entry_latch_from`) CONSUMES the session:
an unasked in-window confirmation spends the one fire without an entry (2026-08-14: MNQ
breaks 09:05, bar 09:32 confirms while `tmso_reject`'s short is open -> no 09:47 short).
A pre-window confirmation still does not latch.

**The stop (O3 only).** "The swept extreme" is read off MNQ specifically -- the traded
instrument -- as MNQ's own running High (bearish) / Low (bullish) since the CURRENT
micro-session opened, regardless of which asset (MNQ or MES) is the one that actually
broke its level. `SL_BUFFER_PTS` (2.0, per the operator's worked example) beyond that,
capped at `SL_CAP_PTS` (15.0, borrowed from §7 and `tmso_reject`; UNTUNED here) from
entry -- the nearer of the two, `reject_core.capped_stop`.

**The stop-bar retry (O1, operator 2026-09-30; `l2-mechanisms.md` §7c).** When the O3
position is STOPPED OUT and the 1m bar that took the stop (bar B) closes with the signal
on BOTH assets (the same both-assets test the confirmation uses), and the divergence is
still live at B's close (not cancelled by the second asset breaking), O3 fires ONE
re-entry by market at B's close. Armed by the stop-out alone (`arm_retry`), judged once
(`retry_on_bar_close`), never chained; the one-fire-per-micro-session latch is untouched.
Stop: O3's own rule -- MNQ's running micro-session extreme (which by then includes B) + 2,
capped at 15 from the retry's entry. `STOP_BAR_RETRY = False` restores the pre-O1
behaviour byte for byte. Motivating day 2026-09-29 (`sessions/2026-09-29/comments.md`
09:55): the 09:31:00 short @ 30675.75 was stopped 09:31:14 by a bar that then closed
30676.75 -> 30661.25.
"""
from __future__ import annotations

import pandas as pd

from agent.trader.reject_core import capped_stop
from agent.trader.tmso_reject import (MICRO_SESSION_MINUTES, micro_session_start,
                                      retry_bar_reason, retry_of, retry_state)

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
#: §7a.1 (operator, 2026-09-28): O3's ENTRY on the 07:30-09:00 -> 09:00-10:30 pair. ON by
#: default; off restores §7a's inert-before-10:30 entry (the whole-session reading stays).
MICRO_SMT_PREOPEN_PAIR_ENABLED = True
#: That pair's own window: RTH open to the shared 10:30 `ENTRY_CUTOFF_ET`. `None` removes
#: it (the pair then never enters), like `MICRO_SMT_ENTRY_WINDOW_ET`.
MICRO_SMT_PREOPEN_WINDOW_ET = ((9, 30), (10, 30))
#: The operator's worked example: MNQ's own swept extreme + 2 pts, capped at 15 from entry.
SL_BUFFER_PTS = 2.0
SL_CAP_PTS = 15.0
#: O1 (§7c): an O3 stop-out whose bar closes with the signal on both assets re-enters
#: once at that bar's close. False restores the pre-O1 stream byte for byte; read at CALL
#: time like every flag above.
STOP_BAR_RETRY = True


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


def entry_latch_from(session_start: pd.Timestamp) -> pd.Timestamp:
    """The instant from which a confirmation in `session_start`'s micro-session consumes
    O3's one fire (operator, 2026-09-29): the open of the ENTRY window its own pair
    trades in -- `MICRO_SMT_PREOPEN_WINDOW_ET` for the day's first micro-session,
    `MICRO_SMT_ENTRY_WINDOW_ET` otherwise -- never before the session itself opens."""
    first = micro_session_start(
        session_start - pd.Timedelta(minutes=MICRO_SESSION_MINUTES)) is None
    window = MICRO_SMT_PREOPEN_WINDOW_ET if first else MICRO_SMT_ENTRY_WINDOW_ET
    if window is None:
        return session_start
    (h, m), _ = window
    return max(session_start, session_start.normalize() + pd.Timedelta(hours=h, minutes=m))


def entry_previous_micro_extremes(mnq, mes, now: pd.Timestamp) -> "dict | None":
    """O3's predecessor read (§7a.1): `previous_micro_extremes` wherever that exists, and
    for the day's FIRST micro-session (09:00-10:30), when `MICRO_SMT_PREOPEN_PAIR_ENABLED`,
    the extremes over 07:30-09:00 -- the grid one step back -- with `session_start` still
    the current micro-session's. O4 must keep reading `previous_micro_extremes`."""
    prev = previous_micro_extremes(mnq, mes, now)
    if prev is not None or not MICRO_SMT_PREOPEN_PAIR_ENABLED:
        return prev
    if now is None or mnq is None or mes is None or not len(mnq) or not len(mes):
        return None
    cur_start = micro_session_start(now)
    if cur_start is None:
        return None
    prev_start = cur_start - pd.Timedelta(minutes=MICRO_SESSION_MINUTES)
    # Only the FIRST micro-session: a later one whose predecessor is merely missing from
    # the frames stays None rather than reaching back further.
    if micro_session_start(prev_start) is not None:
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
        self._caught_up_to = None       # history folded in up to (excluding) this label
        self._fired_sessions: set = set()
        self._retry = None              # O1 (§7c): the armed stop-bar retry, if any

    def state(self) -> dict:
        return {"session": self._session, "mnq_broken": self._mnq_broken,
                "mes_broken": self._mes_broken, "mnq_extreme": self._mnq_extreme,
                "armed": self._mnq_broken != self._mes_broken,
                "fired_sessions": sorted(str(s) for s in self._fired_sessions),
                "bearish": self._bearish,
                "retry": None if self._retry is None else dict(self._retry)}

    # -- O1: the stop-bar retry (§7c) ------------------------------------------- #

    def arm_retry(self, stop_out: dict) -> "dict | None":
        """Arm the single retry off the stop-out of this machine's own fire (the fire
        latched `_session`). None when the constant is off or nothing of ours fired."""
        if not STOP_BAR_RETRY or self._session is None \
                or self._session not in self._fired_sessions:
            return None
        self._retry = retry_state(stop_out, self._session)
        return dict(self._retry)

    def retry_pending(self) -> "dict | None":
        return None if self._retry is None else dict(self._retry)

    def drop_retry(self) -> "dict | None":
        r, self._retry = self._retry, None
        return r

    def retry_on_bar_close(self, now, mnq_bar, mes_bar, prev_extremes, *, mnq_hist=None,
                           mes_hist=None) -> "tuple[dict | None, str | None]":
        """Judge bar B at its close: `(fire, None)`, or `(None, reason)`. Disarms either
        way. The bars the position was open through are folded in first (the machine
        stops folding once its session is latched), so the divergence's live state and
        MNQ's running extreme are read over the WHOLE micro-session, as §7a.1 pins."""
        r = self.drop_retry()
        if r is None:
            return None, None
        reason = retry_bar_reason(now, r)
        if reason is not None:
            return None, reason
        if mnq_bar is None or mes_bar is None:
            return None, "missing_bar"
        session = (prev_extremes or {}).get("session_start")
        if session != r["session"] or session != self._session:
            return None, "micro_session_ended"
        label = now.floor("1min") - pd.Timedelta(minutes=1)
        if mnq_hist is not None and mes_hist is not None:
            if label > self._caught_up_to:
                self._catch_up(mnq_hist, mes_hist, label, None)
            self._caught_up_to = max(self._caught_up_to, label + pd.Timedelta(minutes=1))
        self._fold(self._adverse_of(mnq_bar), self._adverse_of(mes_bar))
        if self._mnq_broken == self._mes_broken:
            return None, "divergence_cancelled"
        if not self._closes_with(mnq_bar):
            return None, "adverse_close"
        if not self._closes_with(mes_bar):
            return None, "mes_adverse_close"
        entry = float(mnq_bar["Close"])
        stop = capped_stop(entry, self._mnq_extreme, buffer_pts=SL_BUFFER_PTS,
                           cap_pts=SL_CAP_PTS, short=self._bearish)
        return {"time": now, "price": entry, "stop": stop,
                "direction": "DOWN" if self._bearish else "UP",
                "swept_by": "MNQ" if self._mnq_broken else "MES",
                "mechanism": None, "retry_of": retry_of(r)}, None

    def on_bar_close(self, now, mnq_bar, mes_bar, prev_extremes, *, mnq_hist=None,
                     mes_hist=None, latch_from=None) -> "dict | None":
        """`prev_extremes` is the caller's `previous_micro_extremes(...)` (or, for O3,
        `entry_previous_micro_extremes(...)`) result -- this stays a pure state machine
        over bars, like `tmso_reject.TmsoReject`.

        `mnq_hist` / `mes_hist`, when BOTH are given, are the session frames up to `now`
        (1s or 1m): every bar of them from the micro-session's open up to, excluding, the
        current bar's label is folded in first (the whole-session live state, §7a.1),
        as completed 1m bars. A catch-up bar that completes at or after `latch_from` and
        confirms a live divergence latches the session (no fire); `None` = never latch
        during the catch-up. Without history the machine sees only the bars it is
        handed, as before."""
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
            self._caught_up_to = session
        if session in self._fired_sessions:
            return None

        if mnq_hist is not None and mes_hist is not None:
            # The current bar's label, as `executor._completed_1m` computes it.
            label = now.floor("1min") - pd.Timedelta(minutes=1)
            if label > self._caught_up_to:
                self._catch_up(mnq_hist, mes_hist, label, latch_from)
            # The current bar is judged below; the next catch-up starts after it.
            self._caught_up_to = max(self._caught_up_to, label + pd.Timedelta(minutes=1))
            if session in self._fired_sessions:
                return None

        prev_mnq = self._prev["mnq_high"] if self._bearish else self._prev["mnq_low"]
        prev_mes = self._prev["mes_high"] if self._bearish else self._prev["mes_low"]
        self._fold(float(mnq_bar["High"]) if self._bearish else float(mnq_bar["Low"]),
                   float(mes_bar["High"]) if self._bearish else float(mes_bar["Low"]))

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

    def _catch_up(self, mnq_hist, mes_hist, label, latch_from) -> None:
        """Fold every completed 1m bar in [`_caught_up_to`, `label`) in time order, and
        latch the session on the first one that completes at or after `latch_from` and
        confirms a live divergence. Stops at the latch: the session is spent."""
        mnq_1m = self._minutes(mnq_hist, self._caught_up_to, label)
        mes_1m = self._minutes(mes_hist, self._caught_up_to, label)
        for bar_label in mnq_1m.index.union(mes_1m.index):
            mnq_b = mnq_1m.loc[bar_label] if bar_label in mnq_1m.index else None
            mes_b = mes_1m.loc[bar_label] if bar_label in mes_1m.index else None
            self._fold(None if mnq_b is None else self._adverse_of(mnq_b),
                       None if mes_b is None else self._adverse_of(mes_b))
            if (latch_from is not None and mnq_b is not None and mes_b is not None
                    and bar_label + pd.Timedelta(minutes=1) >= latch_from
                    and self._mnq_broken != self._mes_broken
                    and self._closes_with(mnq_b) and self._closes_with(mes_b)):
                self._fired_sessions.add(self._session)
                return

    @staticmethod
    def _minutes(frame, start, end) -> pd.DataFrame:
        """`frame` over [start, end) as left-labelled 1m OHLC bars (identity on 1m)."""
        if frame is None or not len(frame):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        seg = frame[(frame.index >= start) & (frame.index < end)]
        if not len(seg):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        return seg[["Open", "High", "Low", "Close"]].resample(
            "1min", label="left", closed="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()

    def _adverse_of(self, bar) -> float:
        return float(bar["High"]) if self._bearish else float(bar["Low"])

    def _fold(self, mnq_adverse, mes_adverse) -> None:
        """Book one span's adverse extremes into MNQ's running extreme and both broken
        flags (monotonic, so folding a span equals folding its bars one by one)."""
        if mnq_adverse is not None:
            if self._mnq_extreme is None or (
                    mnq_adverse > self._mnq_extreme if self._bearish
                    else mnq_adverse < self._mnq_extreme):
                self._mnq_extreme = mnq_adverse
            prev_mnq = self._prev["mnq_high"] if self._bearish else self._prev["mnq_low"]
            broke = mnq_adverse > prev_mnq if self._bearish else mnq_adverse < prev_mnq
            self._mnq_broken = self._mnq_broken or broke
        if mes_adverse is not None:
            prev_mes = self._prev["mes_high"] if self._bearish else self._prev["mes_low"]
            broke = mes_adverse > prev_mes if self._bearish else mes_adverse < prev_mes
            self._mes_broken = self._mes_broken or broke

    def _closes_with(self, bar) -> bool:
        c, o = float(bar["Close"]), float(bar["Open"])
        return c < o if self._bearish else c > o
