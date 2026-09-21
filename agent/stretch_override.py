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

**TWO ARMS since 2026-09-17.** (1) STILL EXTENDING: the extreme is <= `AGE_MAX_MIN` old,
whatever the retrace. (2) HALTED BUT UNRETRACED: the extreme is older than that — with NO
upper bound, 2.5 hours is fine — and price has given back <= `RETRACE_MAX_PCT` of the
stretch. Arm 2 is a USER DECISION taken AGAINST the D0 measurement; the numbers and the
reason are on `RETRACE_MAX_PCT` below. They are two different claims and neither contains
the other: arm 1 has no retrace test (a 25-minute-old extreme that has retraced 40% still
fires), and arm 2 has no clock. Widening arm 1's clock instead of adding arm 2 would have
been the wrong generalisation — it admits stretches that HAVE meaningfully retraced, which
is exactly what "unretraced" is meant to exclude.

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

#: Arm 2 — HALTED BUT UNRETRACED. Percent of the stretch given back at the boundary, at or
#: under which a stretch whose extreme is OLDER than `AGE_MAX_MIN` still fires.
#:
#: **RESTORED BY USER DECISION 2026-09-17, AGAINST THE MEASUREMENT — read this before
#: trusting it.** The first cut had this arm, and D0 over 94 sessions removed it:
#:
#:     age<=30 ONLY                      22 days   68%   <- arm 1, the measured edge
#:     this arm ALONE (age>30):
#:        retrace<= 5   4 days  25%
#:        retrace<=10   9 days  22%
#:        retrace<=25  25 days  44%      (base rate 53%)
#:
#: i.e. on that labelling a stale unretraced stretch CONTINUED more often than it reversed.
#: What changed is not the data but the weight put on it: the buckets are n=4 and n=9, the
#: "which way did 09:30 go" oracle behind them was found unreliable (two definitions
#: disagreed on 52 of 94 days) and was abandoned in favour of the user's chart reads, and
#: 2026-09-17 — UP 505.75 pts, high at 08:34 (age 46 min), 5.0% retraced — went DOWN ~130
#: pts off the 09:30 high. One morning is not a sample either. This arm is a hypothesis
#: under live test, and the FIRST thing to re-measure once a trustworthy direction label
#: exists.
#:
#: 10, not 25: "unretraced" has to mean it. At 25% of a 500-pt stretch price has given back
#: 125 pts, which is a retrace by any reading; the 44% bucket above is also the one closest
#: to noise. NOT FITTED — nothing here can distinguish 5 from 10 from 15.
RETRACE_MAX_PCT = 10.0

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

    # Two arms, no size term beyond the noise floor. Arm 1 has NO retrace test and arm 2
    # has NO clock — see the module docstring for why neither is a special case of the other.
    if age <= AGE_MAX_MIN:
        how = "still extending"
    elif retr <= RETRACE_MAX_PCT:
        how = "halted but unretraced"
    else:
        return _no(f"age {age}m > {AGE_MAX_MIN}m and retraced {retr}% > "
                   f"{RETRACE_MAX_PCT}% (stretch halted AND retraced)", st)

    return {"fires": True, "direction": _OPPOSITE[direction], "stretch": st,
            "reason": (f"{ticker} stretch {direction} {size} pts {how} "
                       f"(age {age}m, retraced {retr}%) -> force "
                       f"{_OPPOSITE[direction]}")}


def _no(reason: str, st) -> dict:
    return {"fires": False, "direction": None, "reason": reason, "stretch": st}
