"""§8's deeper-gap takeover on re-entry.

When a **5m-bound** attempt (§4/§5) stops out and, between the stop-out and the next
attempt — **the stop-out bar itself included** — price ticks into a deeper eligible
thesis-appropriate FVG farther along the adverse path, **any timeframe, 1m included** —
the re-entry binds THAT gap and the failed gap is blacklisted for the plan.

**"ANY TIMEFRAME, 1m INCLUDED" IS THE WHOLE POINT.** §11 escalates it to an imperative:
"Takeover scanner MUST enumerate gaps of ALL timeframes", because two independent manual
walks scanned only 5m and produced a wrong 07-21 ledger (its takeover gap is the 1m
[29137.25, 29138.5]) and a mis-bound 07-31 takeover (the 1m [28455.75, 28477], not the
shallower 5m [28496.75, 28508.5]). A timeframe-blind scanner reproduces exactly that
failure, which is why this module takes the candidate list from `Executor._gaps_on` per
timeframe rather than from the 5m-only `_eligible_gaps`.

**The blacklist is strictly CONDITIONAL** on the deeper-gap penetration. With no deeper
gap penetrated, the same failed gap may re-bind as before: an unconditional blacklist
breaks 07-23's validated same-gap re-entry. The blacklist exists because a stop-run
THROUGH a gap's edge falsifies that edge, and that is only established once price has
moved on to a deeper one.

**Neither the min-height filter nor §4's creating-bar ≥ 09:30 filter applies to the
takeover gap.** Deepest-penetration binding role, same §2 exemption precedent as ladder
targets — 08-14's takeover gap is 3.0 pts tall and was created 09:11.

**Re-entry mode is §6's episode machinery, not a resting stop-entry**, with ONE
modification: a close-verdict (defer) entry whose excursion-anchored SL distance exceeds
the 30-pt cap is SKIPPED (cycle consumed; wait for a closer cycle). Exit-tick/intra-bar
entries are never length-gated. See `episode.Episode(..., sl_cap_gate=True)`.

**Crossed-trigger precedence at cooldown end.** When the takeover has fired and the
cooldown ends, check the FAILED binding's trigger FIRST. If price is already CROSSED
beyond it in the trade direction — momentum resumed without us — §2's crossed-trigger
market execution takes precedence and the takeover does NOT divert. If UNCROSSED — chop —
the takeover/episode path governs. Resolution order: **crossed trigger → market; else a
completed close verdict of the stop-out bar; else resting placement.**

07-24 is the case that forced this: two eligible deeper 1m gaps were penetrated, but at
the 09:36:00 cooldown end price sat below the trigger, and the market re-entry at 28579
captured +146.5 — which the episode's SL-cap gate would have SKIPPED at 45.25 pts,
recreating the exact lockout the §2 cooldown rule was built to kill.
"""
from __future__ import annotations

# Resolution order at the cooldown end, in words, so a caller cannot reorder it by
# accident. §8: "crossed trigger -> market (same FVG-derived SL); else a completed close
# verdict of the stop-out bar fires per the episode rules; else resting placement."
COOLDOWN_RESOLUTION_ORDER = ("crossed_trigger_market", "close_verdict", "resting")

_SHORT = ("DOWN", "SHORT")


def _is_short(direction) -> bool:
    return str(direction or "").upper() in _SHORT


def penetrated(gap, low, high) -> bool:
    """Did the observed price RANGE tick into this gap?

    Range basis, not close basis: on 08-14 "the SL tick and the takeover penetration are
    the SAME tick", so a close-based test would miss the penetration the rule is defined
    on. The stop-out bar itself is inside the window, which is what makes that possible.
    """
    if low is None or high is None:
        return False
    return float(high) >= float(gap.price_low) and float(low) <= float(gap.price_high)


def is_deeper(gap, failed, direction) -> bool:
    """Is `gap` strictly farther along the ADVERSE path than the failed binding?

    Adverse is UP for a short and DOWN for a long — the direction a stop-run travels.
    Strict, not `>=`: the failed gap is never its own takeover.
    """
    if failed is None or gap.id == failed.id:
        return False
    if _is_short(direction):
        return float(gap.price_high) > float(failed.price_high)
    return float(gap.price_low) < float(failed.price_low)


def deepest_penetrated(candidates, failed, direction, *, low, high):
    """The DEEPEST eligible gap the stop window's price range penetrated, or None.

    `candidates` must already span EVERY timeframe (see the module docstring). Height and
    creating-bar filters must NOT have been applied: this is the deepest-penetration
    binding role, and 08-14's takeover gap is 3.0 pts tall and created 09:11.
    """
    best = None
    short = _is_short(direction)
    for g in candidates:
        if not is_deeper(g, failed, direction):
            continue
        if not penetrated(g, low, high):
            continue
        if best is None:
            best = g
        elif (float(g.price_high) > float(best.price_high)) if short else \
                (float(g.price_low) < float(best.price_low)):
            best = g
    return best


def crossed(trigger, price, direction) -> bool:
    """Is the FAILED binding's trigger already beyond price in the trade direction?"""
    if trigger is None or price is None:
        return False
    return (float(price) <= float(trigger)) if _is_short(direction) \
        else (float(price) >= float(trigger))


def resolve_cooldown_end(*, failed_trigger, price, direction, close_verdict=None):
    """§8's precedence rule at the cooldown end. Returns one of
    `COOLDOWN_RESOLUTION_ORDER`.

    Checked in this order and no other: a crossed FAILED trigger means momentum resumed
    without us, and diverting to the takeover there is the lockout that watched 07-24's
    -280 collapse from the sidelines.
    """
    if crossed(failed_trigger, price, direction):
        return "crossed_trigger_market"
    if close_verdict is not None:
        return "close_verdict"
    return "resting"


class Takeover:
    """Per-plan takeover state: which gap took over, and what got blacklisted."""

    def __init__(self, direction: str) -> None:
        self._direction = direction
        self.gap = None
        self.failed_id = None
        self.blacklist: list = []

    def on_stop_out(self, *, failed, candidates, low, high):
        """Evaluate one stop-out. Returns the takeover gap, or None.

        Blacklisting happens HERE and only here, and only when a deeper gap was in fact
        penetrated — see the module docstring on 07-23.
        """
        if failed is None:
            return None
        self.failed_id = failed.id
        gap = deepest_penetrated(candidates, failed, self._direction,
                                 low=low, high=high)
        if gap is None:
            self.gap = None
            return None                # same failed gap may re-bind, as before
        self.gap = gap
        if failed.id not in self.blacklist:
            self.blacklist.append(failed.id)
        return gap

    def state(self) -> dict:
        return {"gap_id": getattr(self.gap, "id", None),
                "failed_id": self.failed_id, "blacklist": list(self.blacklist)}
