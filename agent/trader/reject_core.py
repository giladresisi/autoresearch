"""The vocabulary shared by the sweep-then-reject mechanisms.

A VOCABULARY, NOT A MACHINE, and that distinction is the whole design. §6
(`fvg_1m_post_extreme`), §7 (`extreme_reject_close`), `tmso_reject` and `fvg_1h_reject`
all have the shape "price sweeps a reference, then rejects back", but what they sweep is
NOT a parameter:

  * §7 and `tmso_reject` sweep a PRICE (a day extreme, a session open);
  * §6 and `fvg_1h_reject` sweep a ZONE, which has an inside, two edges and an exit side.

"Swept" and "closed back" mean structurally different things against those, so folding
them into one machine would mean inventing semantics none of the four documented rules
has. What IS genuinely common is the two computations below, currently written out three
times with three sets of constants.

**The constants stay with the mechanisms.** §7's 15-pt cap came out of a recorded A/B
against wick+3-capped-30 and is still a deferred §9 knob; §6's stop is excursion-anchored
with a 30-pt cap because its episodes run deep. Hoisting those into a shared default
would silently re-tune three measured mechanisms at once, so every caller passes its own.
"""
from __future__ import annotations

_SHORT = ("DOWN", "SHORT")


def is_short(direction) -> bool:
    return str(direction or "").upper() in _SHORT


def closes_with_thesis(bar, short: bool) -> bool:
    """The bar closed against its OWN OPEN in the trade direction.

    Deliberately not "closed back across the swept level" — that is a different test, and
    `tmso_reject` uses it instead. On 2026-09-02 the 09:55 bar closes 29030.75, INSIDE the
    zone it just swept, so it satisfies this predicate and fails the level-cross one; the
    two are not interchangeable.
    """
    return (float(bar["Close"]) < float(bar["Open"])) if short \
        else (float(bar["Close"]) > float(bar["Open"]))


def capped_stop(entry: float, anchor: float, *, buffer_pts: float, cap_pts: float,
                short: bool) -> float:
    """`anchor` +/- `buffer`, pulled no further than `cap` from `entry`.

    The NEARER of the structural and the capped stop, which is the rule §2 states for the
    resting mechanisms and the one §6/§7 each reimplement.
    """
    structural = (float(anchor) + buffer_pts) if short else (float(anchor) - buffer_pts)
    capped = (float(entry) + cap_pts) if short else (float(entry) - cap_pts)
    return min(structural, capped) if short else max(structural, capped)
