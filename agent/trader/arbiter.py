"""Phase 3: arbitration between the armed mechanisms (§2, §4, §6).

Three rules, and they are the whole module:

  **Single stop-entry policy.** When several mechanisms are armed, only ONE resting
  stop-entry exists at a time — the one whose trigger price is CLOSEST to current price.
  Other armed mechanisms may still fire by market/limit while it rests.

  **First trigger wins.** Whichever candidate the tape reaches first takes the trade; no
  ranking, no preference order between mechanisms.

  **The 3-attempt counter is SHARED per `plan_id` across every mechanism.** A §5 stop-out
  and a §7 fire spend from the same budget. This is the property that makes attempts a
  sharper change detector than P&L: a stopped-out attempt followed by a winner nets the
  same as one clean entry, so a row reproducing the right P&L on a different attempt
  count is a real discrepancy.

THE DELIBERATE ASYMMETRY, and why no cross-mechanism cancel is ever needed: §6 never
coexists with a resting 5m stop-entry, because a USABLE 5m gap disqualifies §6 entirely
(§6's own precondition). But a resting 1m negation stop from §4's widened fallback MAY
coexist with an armed §6 — the SAME 5m-unusable state arms both — and there the
single-stop-entry policy and first-trigger-wins govern.

WHAT THIS MODULE IS NOT MEASURED AGAINST, stated plainly: every named case elsewhere in
this cycle validates a mechanism IN ISOLATION. §7's own backcheck says so ("a §7-ONLY
backcheck, so nothing competes for the shared 3-attempt counter"), and §6.2's records are
single-mechanism too. §10's 08-11..08-14 forward test is the ONLY place the mechanisms'
INTERACTION was measured, which is why `test_arbiter_forward.py` exists beside the unit
tests here: internal consistency is precisely the property a bug preserves.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_ATTEMPTS = 3                  # §8; per plan_id, shared across mechanisms

#: Mechanisms that place a RESTING stop-entry. §6 and §7 enter by market only, so they
#: never compete for the resting slot — which is why §6's disqualification-by-usable-5m
#: is a precondition rather than a cancel.
RESTING_MECHANISMS = ("fvg_negation_reversal", "fvg_return_continuation")
MARKET_MECHANISMS = ("fvg_1m_post_extreme", "extreme_reject_close")


@dataclass(frozen=True)
class Candidate:
    """One mechanism's current entry intent."""

    mechanism: str
    #: "stop" (wants the resting slot) or "market" (fires on its own signal)
    kind: str
    trigger: "float | None" = None
    artifact_id: "str | None" = None
    timeframe: "str | None" = None


class Arbiter:
    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self._max = int(max_attempts)
        self._used = 0
        self._by_mechanism: dict = {}

    # -- the resting slot ------------------------------------------------------ #

    @staticmethod
    def resting_candidates(candidates) -> list:
        return [c for c in candidates if c.kind == "stop" and c.trigger is not None]

    def select(self, candidates, price) -> "str | None":
        """Which mechanism holds the single resting stop-entry slot right now.

        `None` when nothing wants it. Ties break on the mechanism name so two
        implementations agree; a tie means two triggers at the identical distance, which
        no recorded day produces.
        """
        if price is None:
            return None
        resting = self.resting_candidates(candidates)
        if not resting:
            return None
        best = min(resting, key=lambda c: (abs(float(c.trigger) - float(price)),
                                           c.mechanism))
        return best.mechanism

    @staticmethod
    def may_fire_by_market(candidate) -> bool:
        """Other armed mechanisms may fire by market/limit WHILE a stop-entry rests."""
        return candidate.kind == "market"

    @staticmethod
    def first_trigger(reached) -> "str | None":
        """First trigger wins: of the mechanisms whose entry condition the tape has
        satisfied, the one it satisfied EARLIEST.

        `reached` is `[(mechanism, when), ...]`. Taking the caller's list order instead
        would make this a no-op that re-asserts whatever order the caller happened to
        build — and the caller is the bar loop, which iterates mechanisms in a fixed
        order, not in tape order. Ties break on the mechanism name so two implementations
        agree; a tie means two conditions satisfied on the same tick, which no recorded
        day produces.
        """
        seq = [(when, mech) for mech, when in reached if when is not None]
        if not seq:
            return None
        return min(seq, key=lambda pair: (pair[0], pair[1]))[1]

    # -- the shared budget ----------------------------------------------------- #

    @property
    def attempts_used(self) -> int:
        return self._used

    @property
    def attempts_remaining(self) -> int:
        return max(0, self._max - self._used)

    def spend(self, mechanism: str) -> None:
        """One attempt, from the SHARED per-plan budget, whichever mechanism took it."""
        self._used += 1
        self._by_mechanism[mechanism] = self._by_mechanism.get(mechanism, 0) + 1

    def spent_by(self) -> dict:
        """Attempts per mechanism — RECORDED, never scored. §10.1's 07-21 ends with one
        attempt in reserve; that is an assertion, not a P&L term."""
        return dict(self._by_mechanism)

    def exhausted(self) -> bool:
        return self._used >= self._max

    # -- the §6 asymmetry ------------------------------------------------------ #

    @staticmethod
    def sec6_armed(*, usable_5m_gaps: int) -> bool:
        """§6's precondition, restated as the arbitration rule it implies.

        A usable 5m gap disqualifies §6 ENTIRELY, so §6 can never coexist with a resting
        5m stop-entry and no cross-mechanism cancel is ever needed. The four-part USABLE
        test itself lives with the gap query, not here.
        """
        return int(usable_5m_gaps) == 0

    @staticmethod
    def may_coexist(candidate, resting) -> bool:
        """May `candidate` stay armed while `resting` holds the slot?

        The one documented coexistence: a resting 1m negation stop (§4's widened
        fallback) beside an armed §6 — the same 5m-unusable state arms both. A resting
        5m stop-entry means a usable 5m gap exists, and that disqualifies §6.
        """
        if resting is None:
            return True
        if candidate.mechanism != "fvg_1m_post_extreme":
            return True
        return resting.timeframe != "5min"
