"""Plan 37 D1 — the deterministic direction override, as a pure predicate.

**What this is, and it is not a small thing.** When the pre-09:20 stretch meets the criteria
below, the session's direction is decided HERE, as the opposite of the stretch, and the L1
model call, the declared evidence ledger, the scoring and the weights are all skipped. There
is no model to disagree, no confidence to gate on, and (see `analyzer._arm`) no contract
validation.

**It overrides the repo's own arithmetic, not a model error.** Scored with ZERO model input
— an empty declared ledger plus the code's own P3 auto-derivation over `mid_position` —
2026-09-01's facts produce net **-12.125, expected DOWN**, from the mid reads alone (price
below both the daily and the weekly mid on both assets). L1 did not misjudge that day; it
reported the scoring faithfully, and its twelve unanimous DOWN items were the same
conclusion the deterministic path reaches unaided. So a firing override asserts the opposite
of a double-digit code-computed ledger. That is legitimate — the scoring is a hypothesis,
not ground truth — but it is a deliberate override, and every later loosening of these
thresholds loosens how far we are willing to go against it.

**THE DISCRIMINATOR IS THE CLOCK, NOT THE SIZE.** Over 94 sessions (2026-05-01..09-11, MNQ,
full 09:20-13:00 coverage), a stretch whose extreme formed <= 30 min before 09:20 preceded a
session that ran the OTHER way 15/22 (68%) against a 53% base rate; > 30 min gives 35/72
(49%). Conditioning on SIZE does not merely fail, it INVERTS: >= 450 pts unreversed-to-mid
reverses 10/23 (43%) and >= 400 pts 14/34 (41%). `SIZE_FLOOR` is therefore a weak floor that
keeps noise out, and must never become the discriminating term.

n = 22 in the load-bearing bucket, so roughly +/- 20 pp. **32% of firings (7 of 22) will be
forced the wrong way with nothing able to intervene.** These thresholds are expected to move
as replays and live runs accumulate; this module is the seam, not the final numbers.

**Being right about direction is not the same as making money.** 2026-09-01 forced to UP
still loses 60.25 pts, because all three attempts are spent 72 seconds before the 09:41:36
low that launched the move. That is the entry layer's problem (plan 36). Judge this module on
aggregate direction accuracy, never on one day's P&L.
"""
from __future__ import annotations

#: Minutes before the boundary within which the stretch counts as STILL EXTENDING. The
#: load-bearing threshold: 68% vs a 53% base rate at 30 minutes.
AGE_MAX_MIN = 30.0

#: THE RETRACE ARM IS REMOVED — it was measurably WRONG, and this records why so it is not
#: reintroduced. The first cut fired on "still extending OR halted-but-barely-retraced",
#: which looked like a faithful reading of the idea. D0 over 94 sessions says otherwise:
#:
#:     age<=30 ONLY                      22 days   68%   <- the edge
#:     age<=30 or retrace<=25 (first cut) 47 days   55%   <- barely above the 53% base rate
#:     the retrace arm ALONE (age>30):
#:        retrace<= 5   4 days  25%
#:        retrace<=10   9 days  22%
#:        retrace<=25  25 days  44%
#:
#: A stale stretch that has NOT retraced does not reverse — it CONTINUES, well below the
#: base rate. Adding the arm did not dilute the signal, it imported an anti-signal.
_RETRACE_ARM_REMOVED = True

#: A noise floor only, and it must stay one. D0 measured every size gate as HARMFUL:
#: `age<=30 AND size>=300` gives 61% and `>=450` gives 50%, against 68% for age alone —
#: the same inversion the >=450/>=400 buckets showed (43%/41%). 2026-09-01 reads 488.25 on
#: MNQ and 66.50 on MES, an order of magnitude apart on one day, which is the other reason
#: a tight gate would silently make this an MNQ-only rule.
SIZE_FLOOR_PTS = 40.0

#: Which asset's stretch decides. MNQ is the traded instrument; MES's reading is a different
#: magnitude on the same day (09-01: 66.50 vs 488.25) and agreeing on 09-01 means that date
#: cannot discriminate between the options. MNQ-only until D0's distribution says otherwise.
DECIDING_TICKER = "MNQ"

_OPPOSITE = {"UP": "DOWN", "DOWN": "UP"}


def stretch_override(session_stretch, ticker: str = DECIDING_TICKER) -> dict:
    """`{"fires", "direction", "reason", "stretch"}` — never raises.

    `session_stretch` is `FactsBundle.session_stretch`, i.e. `{ticker: {...}}` as built by
    `derive_facts._session_stretch`.

    A missing or unreadable stretch returns `fires=False` with a reason, and the caller MUST
    fall through to the model rather than guess a direction — `analyzer._arm` does. A
    degraded snapshot has to lose the override, never invent a call.
    """
    st = (session_stretch or {}).get(ticker) if isinstance(session_stretch, dict) else None
    if not isinstance(st, dict):
        return _no(f"no session_stretch for {ticker}", None)

    direction = str(st.get("direction") or "").upper()
    if direction not in _OPPOSITE:
        return _no(f"stretch direction {direction!r} is not UP/DOWN", st)

    try:
        size = float(st["size"])
        age = float(st["age_minutes"])
        retr = float(st["retrace_pct"])
    except (KeyError, TypeError, ValueError):
        return _no("stretch is missing size/age_minutes/retrace_pct", st)

    if size < SIZE_FLOOR_PTS:
        return _no(f"size {size} < floor {SIZE_FLOOR_PTS}", st)

    # THE CLOCK IS THE WHOLE RULE. No retrace arm (see the constant above), no size term
    # beyond the noise floor.
    if age > AGE_MAX_MIN:
        return _no(f"age {age}m > {AGE_MAX_MIN}m (stretch already halted)", st)

    return {"fires": True, "direction": _OPPOSITE[direction], "stretch": st,
            "reason": (f"{ticker} stretch {direction} {size} pts still extending "
                       f"(age {age}m, retraced {retr}%) -> force "
                       f"{_OPPOSITE[direction]}")}


def _no(reason: str, st) -> dict:
    return {"fires": False, "direction": None, "reason": reason, "stretch": st}
