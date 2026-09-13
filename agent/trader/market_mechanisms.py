"""§6 and §7 wired to the bar loop, arbitrated by `arbiter.py`.

**What was missing.** `episode.py` (§6), `extreme_reject.py` (§7) and `arbiter.py` were
all written in commit 146629c and NONE of them ever acquired a production call path:
`git log -S` finds no commit in which `executor.py` imported any of the three, and
`Executor._entry_mechanism` has returned the same two 5m names since that commit. The
Planner nonetheless put all four classes in `armed_classes` unconditionally, so every
artifact READ as though four mechanisms were live while two could never fire. This module
is that missing seam, and nothing else: the state machines are used verbatim.

**Market, never resting.** §6 and §7 enter by MARKET (`arbiter.MARKET_MECHANISMS`), so
they never compete for §2's single stop-entry slot and no cross-mechanism cancel exists.
What they do share is the per-plan 3-attempt budget — a §5 stop-out and a §7 fire spend
from the same counter — which is why the budget is asked of the Executor's plan rather
than kept here.

**§6's precondition is an arbitration rule.** A USABLE 5m gap disqualifies §6 entirely
(`Arbiter.sec6_armed`), which is exactly why a resting 5m stop-entry and an armed §6 can
never coexist. A resting 1m negation stop MAY coexist with §6 — the same 5m-unusable
state arms both — and there first-trigger-wins governs.

**Tick level, not bar level.** §6.1 clause 1 makes intra-bar ordering load-bearing: the
episode begins at the TICK that first enters the gap, and the runaway test and the
excursion extreme are evaluated only over ticks at or after that one. So `on_tick` is
driven from the Executor's per-second path, not from the bar-close cascade.

**NOT VALIDATED BY A NAMED CASE.** `test_arbiter_forward.py` predates this seam and does
not import the arbiter — it drives `run_replay`, i.e. the Executor, which until now never
reached any of these modules. The §6/§7 figures in `l2-mechanisms.md` §10.1/§10.3 were
measured by manual walks, not by this code path. Treat every number this module produces
as unverified until those cases are re-measured against it.
"""
from __future__ import annotations

import pandas as pd

from agent.trader.arbiter import Arbiter, Candidate
from agent.trader.episode import Episode, evaluation_order
from agent.trader.extreme_reject import ExtremeReject, choose_track

_SHORT = ("DOWN", "SHORT")


def counter_thesis_extreme(mnq, direction, arm_ts):
    """(price, timestamp) of the 24h counter-thesis extreme as of the plan arm.

    §7 waits for a sweep of the pool price is working against the thesis, so for an UP
    thesis that is the day LOW. Returns (None, None) when the window is empty, which
    `choose_track` reads as "keep the strict track".
    """
    if mnq is None or not len(mnq) or arm_ts is None:
        return None, None
    window = mnq[(mnq.index > arm_ts - pd.Timedelta(hours=24)) & (mnq.index <= arm_ts)]
    if not len(window):
        return None, None
    if str(direction).upper() in _SHORT:
        return float(window["High"].max()), window["High"].idxmax()
    return float(window["Low"].min()), window["Low"].idxmin()


class MarketMechanisms:
    """Owns §7's machine and §6's per-gap episodes for one plan.

    Returns fire dicts; it never touches `OrderSim`, the recorder or the plan. The
    Executor does all of that, so this stays testable without a session.
    """

    def __init__(self, direction, arm_ts, *, max_attempts=3) -> None:
        self._direction = direction
        self._arm_ts = arm_ts
        self._arbiter = Arbiter(max_attempts=max_attempts)
        self._sec7 = None
        self._episodes: dict = {}
        self._seeded = False

    # -- inspection ------------------------------------------------------------ #

    @property
    def arbiter(self) -> Arbiter:
        return self._arbiter

    def state(self) -> dict:
        return {"sec7": self._sec7.state() if self._sec7 is not None else None,
                "episodes": {k: e.state() for k, e in self._episodes.items()},
                "seeded": self._seeded}

    # -- §7 -------------------------------------------------------------------- #

    def seed_sec7(self, mnq) -> None:
        """Build §7's machine once, on the first bar that can measure the anchor.

        The track is keyed on the AGE of the counter-thesis extreme AT THE PLAN ARM
        (`extreme_reject` re-keyed this from distance on 2026-08-26, and the doc is
        explicit that the measurement instant must be pinned), so this is seeded from
        the arm and never re-derived.
        """
        if self._seeded:
            return
        price, ts = counter_thesis_extreme(mnq, self._direction, self._arm_ts)
        age = None if ts is None else (self._arm_ts - ts)
        track = choose_track(age)
        self._sec7 = ExtremeReject(self._direction, self._arm_ts, track=track,
                                   extreme=(price if track == "24h" else None))
        self._seeded = True

    def sec7_on_bar_close(self, now, bar) -> "dict | None":
        if self._sec7 is None:
            return None
        return self._sec7.on_bar_close(now, bar)

    # -- §6 -------------------------------------------------------------------- #

    def sync_episodes(self, gaps, *, armed: bool) -> None:
        """Keep one `Episode` per live 1m candidate gap. Disarmed clears them all.

        Clearing rather than pausing is deliberate: §6's precondition is that no usable
        5m gap exists, and an episode built under that condition has no meaning once it
        stops holding. Re-arming rebuilds from the gaps that are live then.
        """
        if not armed:
            self._episodes.clear()
            return
        live = {}
        for g in gaps or ():
            ep = self._episodes.get(g.id)
            if ep is None:
                ep = Episode(g.price_low, g.price_high, self._direction, gap_id=g.id)
            live[g.id] = ep
        self._episodes = live

    def sec6_on_tick(self, now, price, *, bar_open=None, mid=None) -> "dict | None":
        """Clause 4: most-recently-ENTERED gap first. Measured outcome-neutral on all
        four recorded days, so it is a tie-break convention, specified only so two
        implementations agree."""
        for ep in evaluation_order(list(self._episodes.values())):
            fire = ep.on_tick(now, price, bar_open=bar_open, mid=mid)
            if fire is not None:
                return fire
        return None

    def sec6_on_bar_close(self, now, bar, *, mid=None) -> "dict | None":
        for ep in evaluation_order(list(self._episodes.values())):
            fire = ep.on_bar_close(now, bar, mid=mid)
            if fire is not None:
                return fire
        return None

    def reset_cycles(self) -> None:
        """§6.1 clause 3: the stop-out cooldown resets every gap cycle to idle."""
        for ep in self._episodes.values():
            ep.reset_cycle()

    # -- arbitration ----------------------------------------------------------- #

    def candidates(self, *, resting_mechanism=None, resting_trigger=None,
                   resting_tf=None) -> list:
        out = []
        if resting_mechanism is not None:
            out.append(Candidate(resting_mechanism, "stop", trigger=resting_trigger,
                                 timeframe=resting_tf))
        if self._sec7 is not None:
            out.append(Candidate("extreme_reject_close", "market"))
        if self._episodes:
            out.append(Candidate("fvg_1m_post_extreme", "market"))
        return out

    def pick(self, fires) -> "dict | None":
        """First trigger wins across the market mechanisms that fired this instant.

        `fires` is `[(mechanism, fire_dict), ...]`. Arbitration is by the fire's own
        time, NOT by the caller's iteration order — the bar loop iterates mechanisms in a
        fixed order, and taking that order would make `first_trigger` a no-op.
        """
        live = [(m, f) for m, f in fires if f is not None]
        if not live:
            return None
        if len(live) == 1:
            return live[0][1]
        chosen = self._arbiter.first_trigger([(m, f.get("time")) for m, f in live])
        for m, f in live:
            if m == chosen:
                return f
        return live[0][1]
