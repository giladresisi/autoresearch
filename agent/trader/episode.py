"""§6 `fvg_1m_post_extreme`: the episode machine over one 1m continuation gap.

1m gaps are transient, so this mechanism trades their REJECTION rather than resting an
order beyond them. An *episode* begins when price enters a candidate gap STRICTLY inside
— at least one tick beyond the near edge, edge touches don't count, no traversal
requirement — and tracks the excursion extreme until a trade starts. Adverse 1m closes
beyond the gap do NOT invert a candidate here (unlike §2's close-based eligibility): the
close-colour gates replace close-through invalidation, and the episode survives escapes
and re-entries (validated 08-05, where the winning entry came two bars after a close
above the gap top).

§6.1's FOUR NORMATIVE CLAUSES, implemented exactly:

1. **Intra-bar ordering is load-bearing.** The episode begins at the TICK that first
   enters the gap strictly beyond the near edge. The early-runaway test and the excursion
   extreme are evaluated ONLY over ticks at or after that tick — never over the whole
   entering bar. Named trap: 08-06's 09:38 bar prints 29437.00 (= gap top + 25) at
   09:38:14, THIRTEEN SECONDS before price enters the gap at 09:38:27; scanning the whole
   bar fires a phantom runaway on a print that preceded the episode.
2. **Early-runaway fills at the TRIGGER price** (`exit-side edge ± 25`), not the market
   mid. Named test: 07-21 gap B fills 29149.75 = 29174.75 − 25 exactly. Close-verdict and
   exit-tick entries DO fill at the 1s mid at placement.
3. **The stop-out cooldown gates entry evaluation** — no cycle may complete while it is
   in force, and all gap cycles reset to idle across it (`reset_cycle`). Without this the
   machine re-enters within seconds of every stop-out.
4. **Candidate-gap evaluation order: most-recently-ENTERED gap first** (`entered_at`).
   Measured OUTCOME-NEUTRAL on all four recorded days, so it is a tie-break convention
   specified only so two implementations agree.

GEOMETRY. The near edge and the exit-side edge are THE SAME edge, and that is not a
simplification: a continuation gap is retraced into from the trade side and left the same
way. For a short the gap is entered from below and exited downward through `gap_low`
(07-21's runaway fills gap_low − 25); for a long it is entered from above and exited
upward through `gap_high` (08-06's phantom-runaway print is gap_top + 25).

THE SL-CAP GATE (`sl_cap_gate=True`) is §8's re-entry modification and ONLY that: a
CLOSE-VERDICT entry whose excursion-anchored SL distance exceeds the 30-pt cap is
SKIPPED, the cycle consumed, and a closer cycle awaited. Exit-tick and early-runaway
entries are NEVER length-gated — their SL distance is inherently small at the zone edge,
and a fixed length gate forfeits 07-17's whole winner (the 51-pt breakdown bar enters via
exit-tick). Extending this gate to §6-proper is on §11.0's DO-NOT-IMPLEMENT list: idea
only, zero net evidence, and on 08-06 it would have skipped a WINNER.
"""
from __future__ import annotations

# §9 starting values.
EARLY_RUNAWAY_PTS = 25.0          # beyond the exit-side edge
SL_BUFFER_PTS = 2.0               # beyond the episode's excursion extreme
SL_CAP_PTS = 30.0                 # from the entry price

_SHORT = ("DOWN", "SHORT", "BEAR")

IDLE, ENTERING, LIVE = "idle", "entering", "live"


class Episode:
    """One candidate gap's episode + cycle state. Pure; no I/O, no clock of its own."""

    def __init__(self, gap_low, gap_high, direction, *, gap_id=None,
                 sl_cap_gate: bool = False) -> None:
        self.gap_low = float(gap_low)
        self.gap_high = float(gap_high)
        self.gap_id = gap_id
        self._short = str(direction).upper() in _SHORT
        self._sl_cap_gate = bool(sl_cap_gate)

        self._cycle = IDLE
        self._started = False           # the EPISODE (excursion tracking) has begun
        self._excursion = None          # adverse extreme since the entering tick
        self.entered_at = None          # clause 4's ordering key
        self._bar_open = None           # the entering bar's open
        self._prev_bar = None           # the previous COMPLETED bar, for the gate
        self._defer = False             # gate says: wait for this bar's close
        self._pending = None            # the fire not yet consumed by the caller
        self.cycles_voided = 0
        self.cycles_skipped_by_sl_cap = 0

    # -- geometry --------------------------------------------------------------- #

    @property
    def exit_edge(self) -> float:
        """Near edge AND exit-side edge — see the module docstring."""
        return self.gap_low if self._short else self.gap_high

    def strictly_inside(self, price) -> bool:
        p = float(price)
        if self._short:
            return self.gap_low < p <= self.gap_high
        return self.gap_low <= p < self.gap_high

    def _beyond_exit(self, price) -> bool:
        return float(price) < self.gap_low if self._short else float(price) > self.gap_high

    def _beyond_adverse(self, price) -> bool:
        return float(price) > self.gap_high if self._short else float(price) < self.gap_low

    def _with_thesis(self, bar) -> bool:
        """Closed against its own open in the trade direction (red for a short)."""
        return (float(bar["Close"]) < float(bar["Open"])) if self._short \
            else (float(bar["Close"]) > float(bar["Open"]))

    def _runaway_trigger(self) -> float:
        return (self.gap_low - EARLY_RUNAWAY_PTS) if self._short \
            else (self.gap_high + EARLY_RUNAWAY_PTS)

    # -- inspection ------------------------------------------------------------- #

    def state(self) -> dict:
        return {"cycle": self._cycle, "started": self._started,
                "excursion": self._excursion, "entered_at": self.entered_at,
                "defer": self._defer, "gap_id": self.gap_id,
                "voided": self.cycles_voided,
                "sl_cap_skips": self.cycles_skipped_by_sl_cap}

    def pending_entry(self) -> "dict | None":
        return self._pending

    def take_pending(self) -> "dict | None":
        out, self._pending = self._pending, None
        return out

    def reset_cycle(self) -> None:
        """§6.1 clause 3: the stop-out cooldown resets every gap cycle to idle.

        The EPISODE's excursion survives — it measures how deep the rejection ran, which
        is what the stop anchors to, and it is not a property of the cycle. A fresh
        strictly-inside re-entry is required before anything can complete again.
        """
        self._cycle = IDLE
        self._defer = False
        self._prev_bar = None
        self._pending = None

    # -- the tape --------------------------------------------------------------- #

    def on_tick(self, now, price, *, bar_open=None, mid=None) -> "dict | None":
        """One tick. Returns an early-runaway or exit-tick fire, or None."""
        if price is None:
            return None
        p = float(price)

        if self._started:
            self._track_excursion(p)

        if self._cycle == IDLE:
            if self.strictly_inside(p):
                self._begin(now, p, bar_open)
            return None

        if self._cycle == ENTERING:
            # Clause 2: fills at the TRIGGER price, never the mid.
            trig = self._runaway_trigger()
            reached = (p <= trig) if self._short else (p >= trig)
            beyond_open = self._bar_open is None or (
                (p < float(self._bar_open)) if self._short
                else (p > float(self._bar_open)))
            # The beyond-open condition is LOAD-BEARING: it blocked a false fire on
            # 08-05 where the runaway price was still above the bar's open.
            if reached and beyond_open:
                return self._fire(now, trig, "early_runaway")
            return None

        if self._cycle == LIVE and not self._defer and self._beyond_exit(p):
            # Exit-tick market entry. NEVER length-gated, even in §8 re-entry mode.
            return self._fire(now, mid if mid is not None else p, "exit_tick")
        return None

    def on_bar_close(self, now, bar, *, mid=None) -> "dict | None":
        """One COMPLETED 1m bar, stamped at its CLOSE instant.

        `now` is the close instant (`label + 1min`), matching the document's own
        convention: 07-21's verdict on the 09:34 bar is "close-entry 09:35:00", and §7's
        fire on the 09:41 bar is "market short at 09:42:00".
        """
        close = float(bar["Close"])

        if self._cycle == ENTERING:
            # The entering bar NEVER triggers intra-bar; its verdict waits for the close.
            if self._beyond_exit(close):
                if self._with_thesis(bar):
                    return self._close_verdict(now, bar, mid)
                # A with-trend-coloured close beyond the exit side is a SKIP, and a skip
                # VOIDS the cycle: no entry fires on already-crossed state carried over,
                # and §2's crossed-trigger rule does NOT apply within this mechanism. A
                # fresh re-entry must complete a NEW cycle. (07-21: the 09:36 green skip
                # required the 09:38->09:39 re-entry before the winning entry.)
                self.cycles_voided += 1
                self._cycle = IDLE
                self._defer = False
                return None
            # Closed inside, or beyond on the adverse side: the cycle stays LIVE.
            # (08-14/07-17: "the 09:31 entering bar closes inside -> cycle live".)
            self._cycle = LIVE
            self._arm_gate(bar)
            return None

        if self._cycle == LIVE:
            fire = None
            if self._defer and self._beyond_exit(close):
                # Deferred verdict: enter at this close, accepting the worse price as
                # the cost of the missing conviction.
                fire = self._close_verdict(now, bar, mid)
            self._arm_gate(bar)
            return fire

        return None

    # -- internals -------------------------------------------------------------- #

    def _begin(self, now, price, bar_open) -> None:
        self._cycle = ENTERING
        self.entered_at = now
        self._bar_open = bar_open
        self._defer = False
        if not self._started:
            self._started = True
            # Clause 1: the excursion is seeded AT the entering tick, so a print earlier
            # in the same bar cannot enter the measurement.
            self._excursion = float(price)
        else:
            self._track_excursion(float(price))

    def _track_excursion(self, price: float) -> None:
        if self._excursion is None:
            self._excursion = price
            return
        self._excursion = max(self._excursion, price) if self._short \
            else min(self._excursion, price)

    def _arm_gate(self, bar) -> None:
        """Set the NEXT bar's exit-tick gate from this completed bar.

        Doc-literal (§6.1's known residual, deliberately left open): previous bar closed
        inside the gap, or beyond it on the exit side, or against its own open -> the
        exit tick fires. Closed with-trend-coloured beyond the gap on the ADVERSE side ->
        defer to the current bar's close.

        A raw exit-tick reading (no previous-bar gate) matches 08-06 better but is worse
        on 08-05 and contradicts §6's own statement that the colour gates "skipped the
        noise cycles a raw exit-tick rule would have taken". The +/-1 cycle discrepancy
        on 08-06 is a known uncertainty, NOT a defect to chase.
        """
        self._prev_bar = bar
        close = float(bar["Close"])
        self._defer = self._beyond_adverse(close) and not self._with_thesis(bar)

    def _close_verdict(self, now, bar, mid) -> "dict | None":
        entry = float(mid) if mid is not None else float(bar["Close"])
        raw = self._raw_stop(entry)
        if self._sl_cap_gate and abs(raw - entry) > SL_CAP_PTS:
            # §8 re-entry mode only: the cycle is CONSUMED; wait for a closer one.
            # Parameter-free — it reuses §6's own SL cap rather than adding a knob, and
            # that is why it skips 08-14's long noise bars while letting 07-17's 51-pt
            # breakdown bar enter by exit tick.
            self.cycles_skipped_by_sl_cap += 1
            self._cycle = IDLE
            self._defer = False
            return None
        return self._fire(now, entry, "close_verdict")

    def _raw_stop(self, entry: float) -> float:
        """The excursion-anchored stop BEFORE the 30-pt cap."""
        anchor = self._excursion if self._excursion is not None else entry
        return (anchor + SL_BUFFER_PTS) if self._short else (anchor - SL_BUFFER_PTS)

    def _fire(self, now, price, kind) -> dict:
        entry = float(price)
        raw = self._raw_stop(entry)
        capped = entry + SL_CAP_PTS if self._short else entry - SL_CAP_PTS
        stop = min(raw, capped) if self._short else max(raw, capped)
        self._cycle = IDLE
        self._defer = False
        self._pending = {"time": now, "price": entry, "stop": stop, "kind": kind,
                         "direction": "DOWN" if self._short else "UP",
                         "gap_id": self.gap_id, "excursion": self._excursion,
                         "mechanism": "fvg_1m_post_extreme"}
        return dict(self._pending)


def evaluation_order(episodes) -> list:
    """§6.1 clause 4: most-recently-ENTERED gap first; never-entered gaps last.

    Ties and never-entered gaps fall through to the caller's own order, which is the
    creation order everywhere this is used — stated as what it is rather than claimed as
    a tie-break term the key does not carry.

    Measured OUTCOME-NEUTRAL on all four recorded days, so this is a convention, not a
    result driver — specified only so two implementations agree.
    """
    def key(e):
        return (e.entered_at is not None, e.entered_at)
    return sorted(episodes, key=key, reverse=True)
