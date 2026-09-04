"""Spec §2's draw rule: which candidate was the move drawn to.

**The rule, and why it has no free parameter.** Applied to ONE instrument at a time:

1. *Eligible* = ahead of the move's origin in the move's direction, in that instrument's own
   price space, and not already swept at the evaluation instant (spec §3.3 — knowledge is
   monotone, eligibility is not).
2. Among eligible candidates REACHED inside the move window, the draw is the one reached
   **last**. "Completion coinciding with the turn" needs no tolerance in minutes: the move
   ran through everything nearer and stopped at the last thing it completed. A coincidence
   window would be a second 0.382 — a number taken from convention and defended afterwards.
   The **lag** is recorded as a RESULT instead, and its distribution is what would justify
   such a window later, with a date, in the spec.
3. If nothing was reached, the draw is the **nearest** eligible candidate whose closest
   approach falls inside the tolerance band — the test-and-fail case, and the only place the
   band is used (spec §2.3: never cross-instrument; §2.4: never against move-end).
4. Otherwise that instrument is **unexplained**, which is a headline number and never a
   fallback label.

Near-misses rank by distance and never by time, and that is not a detail: an unreached
candidate's closest approach IS the move extreme by construction, so a time-ranked rule
would hand every session to whichever pool the move never got to.

**One label per instrument, not one per session.** Spec §2.4: each instrument has its own
DOL, the two correspond, and role-swap invariance holds — whichever graph does the sweeping,
MNQ's DOL is still an MNQ pool. 2026-08-13 is the case that settles it: both instruments
reach their own `prev1_week_high`, MNQ's at 09:46 and MES's at 09:58, and a single
cross-instrument "latest wins" hands MNQ's session to an MES price, destroying exactly the
correspondence that is the finding. What the cross-instrument reading contributes (spec §3.1
— MNQ halting at no MNQ level because MES reached one of its own) is *recorded* rather than
substituted: `is_halt_driver` marks the instrument whose draw completed last, and
`counterpart_status` says what the other instrument did at the same named level.

**Result fields, which no phase-3 predictor may read.** `overshoot` and `lag_min` are both
computed from the move's end, which is lookahead by design (spec §1.1). They exist to be
explained, not to explain.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from derive_facts import closest_approach, first_cross

OUTCOME_DRAW = "draw"
OUTCOME_REACHED_PASSED = "reached_passed"
OUTCOME_NEAR_MISS_IN = "near_miss_in_band"
OUTCOME_NEAR_MISS_OUT = "near_miss_out_of_band"
OUTCOME_INELIGIBLE = "ineligible"

STATUS_LABELLED = "labelled"
STATUS_UNEXPLAINED = "unexplained"

VIA_REACHED_LAST = "reached_last"
VIA_NEAR_MISS = "near_miss"

DEFAULT_BAND_MULT = 0.15

#: The skeleton is built on 5m bars, so a segment's `extreme_ts` is the OPEN of the bar the
#: extreme printed in, not the instant of the extreme. The move window must run to that
#: bar's END. Truncating it to the bar's first minute is not a rounding error: on 2026-08-11
#: the extreme bar opens 09:45 and the day-low sweep prints at 09:46, so a truncated window
#: misses the very event the session is known for and labels a level 81 points short of it.
SEGMENT_GRAIN = pd.Timedelta(minutes=5)

#: Entry-proxy curve (spec 26, phase-2 definitions). Reporting only — never eligibility,
#: never labelling. A single proxy would be an unstated strategy choice: at move-start + 10
#: on 2026-08-13 the confirmation tax is already 175.75 pts, 43% of the whole move.
ENTRY_PROXY_MINUTES = (2, 5, 10)


@dataclass(frozen=True)
class CandidateRow:
    ticker: str
    names: tuple
    price: float
    cls: str
    tier: str
    outcome: str
    reached_ts: object
    closest_approach: object     # signed, + = fell short; None when reached
    dist_from_start: object      # None when ineligible

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "names": list(self.names), "price": self.price,
                "cls": self.cls, "tier": self.tier, "outcome": self.outcome,
                "reached_ts": None if self.reached_ts is None else str(self.reached_ts),
                "closest_approach": self.closest_approach,
                "dist_from_start": self.dist_from_start}


@dataclass(frozen=True)
class Label:
    status: str
    ticker: object = None
    via: object = None
    names: tuple = ()
    price: object = None
    cls: object = None
    tier: object = None
    reached_ts: object = None
    lag_min: object = None          # RESULT: extreme_ts - reached_ts
    overshoot: object = None        # RESULT: signed, + = the move went beyond the draw
    own_extreme: object = None      # RESULT: this instrument's extreme over the window
    ambiguous: bool = False
    counterpart_status: object = None
    is_halt_driver: bool = False    # this instrument's draw completed last of the two
    band: object = None
    dist_from_start: object = None
    dist_at_proxy: object = None    # {minutes: distance}
    n_eligible: int = 0
    n_reached: int = 0

    def to_dict(self) -> dict:
        return {"status": self.status, "ticker": self.ticker, "via": self.via,
                "names": list(self.names), "price": self.price, "cls": self.cls,
                "tier": self.tier,
                "reached_ts": None if self.reached_ts is None else str(self.reached_ts),
                "lag_min": self.lag_min, "overshoot": self.overshoot,
                "own_extreme": self.own_extreme,
                "ambiguous": self.ambiguous,
                "counterpart_status": self.counterpart_status,
                "is_halt_driver": self.is_halt_driver, "band": self.band,
                "dist_from_start": self.dist_from_start,
                "dist_at_proxy": self.dist_at_proxy,
                "n_eligible": self.n_eligible, "n_reached": self.n_reached}


def _window(bars: pd.DataFrame, start_ts, end_ts) -> pd.DataFrame:
    if bars is None or not len(bars):
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    return bars.loc[start_ts:end_ts]


def _origin_price(bars: pd.DataFrame, start_ts, direction: str):
    """The move's origin IN THIS INSTRUMENT'S price space.

    The skeleton's `start_price` is the 5m origin bar's own extreme, and a 5m bar's extreme
    is the extreme of its 1m constituents — so this reproduces `segment.start_price` exactly
    for MNQ and gives the honest analogue for MES, which has its own origin at the same
    instant and a different price.
    """
    head = _window(bars, start_ts, start_ts + SEGMENT_GRAIN - pd.Timedelta(minutes=1))
    if not len(head):
        return None
    return float(head["low"].min()) if direction == "up" else float(head["high"].max())


def _extreme_price(bars: pd.DataFrame, start_ts, end_ts, direction: str):
    w = _window(bars, start_ts, end_ts)
    if not len(w):
        return None
    return float(w["high"].max()) if direction == "up" else float(w["low"].min())


def _proxy_price(bars: pd.DataFrame, start_ts, minutes: int):
    w = _window(bars, start_ts, start_ts + pd.Timedelta(minutes=minutes))
    return float(w["close"].iloc[-1]) if len(w) else None


def _label_one(ticker, segment, bars, cands, band, start_ts, end_ts):
    """Rules 1-4 for a single instrument. -> `(Label, list[CandidateRow])`."""
    direction = segment.direction
    side = "above" if direction == "up" else "below"
    origin = _origin_price(bars, start_ts, direction)
    w = _window(bars, start_ts, end_ts)

    rows, reached, near = [], [], []
    for c in cands:
        ahead = origin is not None and (c.price > origin if direction == "up"
                                        else c.price < origin)
        if c.swept_before or not ahead or not len(w):
            rows.append(CandidateRow(ticker, c.names, c.price, c.cls, c.tier,
                                     OUTCOME_INELIGIBLE, None, None, None))
            continue

        dist = round(abs(c.price - origin), 4)
        ts = first_cross(w, c.price, side)
        if ts is not None:
            rec = CandidateRow(ticker, c.names, c.price, c.cls, c.tier,
                               OUTCOME_REACHED_PASSED, ts, None, dist)
            reached.append((ts, dist, rec, c))
        else:
            gap, _ = closest_approach(w, c.price, side)
            inside = band is not None and gap is not None and abs(gap) <= band
            rec = CandidateRow(
                ticker, c.names, c.price, c.cls, c.tier,
                OUTCOME_NEAR_MISS_IN if inside else OUTCOME_NEAR_MISS_OUT,
                None, None if gap is None else round(float(gap), 4), dist)
            if inside:
                near.append((abs(gap), rec, c))
        rows.append(rec)

    n_eligible = sum(1 for r in rows if r.outcome != OUTCOME_INELIGIBLE)
    own_extreme = _extreme_price(bars, start_ts, end_ts, direction)
    if reached:
        # Latest cross wins. Within one 1m bar the tape gives no ordering, so the
        # tie-break is "further along the move" — deterministic, and the only reading
        # consistent with the move having passed through both.
        best_ts = max(t for t, _d, _r, _c in reached)
        tied = [(d, r, c) for t, d, r, c in reached if t == best_ts]
        ambiguous = len({round(c.price, 4) for _d, _r, c in tied}) > 1
        _d, win_row, win = max(tied, key=lambda x: x[0])
        via, reached_ts = VIA_REACHED_LAST, best_ts
    elif near:
        _g, win_row, win = min(near, key=lambda x: x[0])
        via, reached_ts, ambiguous = VIA_NEAR_MISS, None, False
    else:
        return Label(status=STATUS_UNEXPLAINED, ticker=ticker, band=band,
                     own_extreme=own_extreme, n_eligible=n_eligible,
                     n_reached=0), rows

    rows = [CandidateRow(r.ticker, r.names, r.price, r.cls, r.tier, OUTCOME_DRAW,
                         r.reached_ts, r.closest_approach, r.dist_from_start)
            if r is win_row else r for r in rows]

    overshoot = None if own_extreme is None else round(
        (own_extreme - win.price) if direction == "up" else (win.price - own_extreme), 4)

    return Label(
        status=STATUS_LABELLED, ticker=ticker, via=via, names=win.names, price=win.price,
        cls=win.cls, tier=win.tier, reached_ts=reached_ts,
        lag_min=None if reached_ts is None else round(
            (segment.extreme_ts - reached_ts).total_seconds() / 60.0, 2),
        overshoot=overshoot, own_extreme=own_extreme, ambiguous=ambiguous,
        band=None if band is None else round(band, 4),
        dist_from_start=win_row.dist_from_start,
        dist_at_proxy={m: (None if (p := _proxy_price(bars, start_ts, m)) is None
                           else round(abs(win.price - p), 4))
                       for m in ENTRY_PROXY_MINUTES},
        n_eligible=n_eligible, n_reached=len(reached)), rows


def label_segment(segment, bundle, bars_by_ticker: dict, candidates,
                  *, band_mult: float = DEFAULT_BAND_MULT) -> tuple:
    """-> `({ticker: Label}, list[CandidateRow])`. Pure; never mutates its inputs."""
    start_ts = segment.start_ts
    end_ts = segment.extreme_ts + SEGMENT_GRAIN - pd.Timedelta(minutes=1)
    avg = bundle.avg_range_1h or {}

    labels, all_rows = {}, []
    for tkr, bars in bars_by_ticker.items():
        band = None if avg.get(tkr) is None else band_mult * float(avg[tkr])
        cands = [c for c in candidates if c.ticker == tkr]
        lbl, rows = _label_one(tkr, segment, bars, cands, band, start_ts, end_ts)
        labels[tkr] = lbl
        all_rows.extend(rows)

    # Cross-instrument overlay (spec §3.1), recorded and never substituted.
    driven = [(l.reached_ts, tkr) for tkr, l in labels.items() if l.reached_ts is not None]
    driver = max(driven)[1] if driven else None
    for tkr, lbl in labels.items():
        if lbl.status != STATUS_LABELLED:
            continue
        other = next((t for t in bars_by_ticker if t != tkr), None)
        counterpart = "absent"
        if other is not None:
            match = next((r for r in all_rows if r.ticker == other
                          and set(r.names) & set(lbl.names)), None)
            if match is not None:
                counterpart = ("reached" if match.outcome in (OUTCOME_DRAW,
                                                              OUTCOME_REACHED_PASSED)
                               else match.outcome)
        labels[tkr] = Label(**{**lbl.__dict__, "counterpart_status": counterpart,
                               "is_halt_driver": tkr == driver})
    return labels, all_rows
