# stop_utils.py — pure, no project imports. The single source of truth for
# "a valid protective stop for a given fill." Imported by the preventive (live_orders)
# and corrective (broker_recon.reconcile) layers, so the definition of "a valid stop for
# this fill" lives in exactly one place.

MIN_STOP_DISTANCE = 10.0  # keep in sync with live_orders._MIN_FILL_STOP_DISTANCE


def valid_stop_for_fill(direction, fill, intended_stop, intended_entry):
    """Return a protective stop guaranteed on the correct side of `fill` at the intended
    risk. Keeps `intended_stop` unchanged when it is already valid (the common path → no
    backtest change); only re-anchors the edge cases (STP->MKT fill past the stop).

    `intended_entry` is the ORIGINAL trigger / expected entry price (NOT the actual fill) —
    so `intended_risk` is the strategy's planned risk size, preserved relative to the real
    fill. Passing the fill here by mistake would collapse the risk to ~0 and place a
    too-tight stop.
    """
    risk = max(abs(intended_stop - intended_entry), MIN_STOP_DISTANCE)
    if direction in ("up", "long"):
        if intended_stop <= fill - MIN_STOP_DISTANCE:   # already valid
            return intended_stop
        return fill - risk                              # re-anchor below fill at intended risk
    if direction in ("down", "short"):
        if intended_stop >= fill + MIN_STOP_DISTANCE:
            return intended_stop
        return fill + risk
    # Unknown direction: this is the single source of truth for both layers, so fail loudly
    # rather than silently defaulting to "short" (a typo/empty value would otherwise place a
    # stop on the wrong side).
    raise ValueError(
        f"valid_stop_for_fill: unknown direction {direction!r} "
        "(expected one of up/long/down/short)")
