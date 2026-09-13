"""Plan 33 Task 2: the two DOL tracks and the initial target, as INJECTED prices.

§2.1 is the contract this module serves: the policy simulator never selects a target and
its logic never depends on how one was selected. It is handed two prices — a DOL and an
initial target — and is indifferent to their provenance. Everything about provenance
therefore lives here, and nothing about it leaks downstream.

**The two DOL tracks** (§4 (b): reported separately, never pooled).

  * `production` — `derive_facts.build_menus`'s D1, the nearest eligible draw. Reached
    through `target_offline.menu_at`, which is production's rule with no copy of it
    anywhere: `_dol_menu` owns the proximity floor, the wrong-side filter, P1 suppression
    and depletion, and re-deriving any of that here is the failure this project has
    already paid for twice.
  * `oracle` — the phase-2 label corpus's draw for the session's MNQ primary segment.

**The entry DOL is the 09:20 menu, and that is a settled divergence from the plan text.**
§2.1 says "D1 at the fill", which is circular for an entry feed: the fill cannot exist
until the Executor has a DOL, because the DOL is what its `dol_floor` veto and its
`dol_reached` death are computed against. It is also not what production does —
`target_offline`'s own docstring states it plainly: "The DOL is an L1 decision taken once
at the 09:20 boundary ... Nothing between 09:20 and the fill reconsiders it." So the
09:20 D1 is both the implementable reading and the faithful one. `d1_at_fill` is computed
and RECORDED beside it, unused by the policy, because the target layer's fill re-anchor
work needs it and it costs one more menu build.

**The initial target** is `hypothesis.compute_cautious_prices`'s `cautious_price_initial`
at the fill — production's own two-tier cautious ladder, re-anchored to the fill price
exactly as `recompute_cautious_for_fill` does in production. Its `liquidities` input comes
from `legacy_liquidities.reconstruct`, which is validated against the one archived
`daily.json` and carries the four deviations it cannot reconstruct.

**`menu_at` is a SHARED CONTRACT — do not widen it.** This module and the target-selection
study both depend on `target_offline.menu_at` keeping its exact signature and return shape,
so neither side changes it unilaterally. If plan 33's layer ever needs more from the menu,
ask for a NEW function rather than adding a parameter to that one. The coupling surface is
deliberately three names — `menu_at`, and `OfflineFacts` / `instant_ts` in the tests — and
nothing here imports `_dol_menu`, `_nearest_first_pick`, or any private of that module.

**Forming sub-session extremes are already in this layer, and that is not an accident.** The
legacy `_session_bars` window for `ny_morning` is 06:00-12:00, so at a mid-morning fill the
reconstruction samples the RUNNING high of a session that has not closed yet. On 2026-09-02
at the 10:07 fill it reads 247 bars (06:00 -> 10:06) and returns 29149.25, printed at 08:21 —
which is precisely the level plan 33 §3 names, and where the fixture's 29147.25 comes from
after the 2.0-pt offset. The DOL layer cannot see that level (it sits 52.25 pts away, inside
the 79.91 draw floor); the cautious ladder can, because its initial band is 40-110 pts. The
two layers have different reach BY CONSTRUCTION, and this one is the near one.

**`target_offline.below_floor` is NOT a complete near-band picture — do not treat it as
one.** It collects only FORMING candidates. A menu level excluded by the ATR-scaled draw
floor is absent from it, because `menu_at` returns only what passed the floor and recovering
the rest would mean re-walking `bundle.levels`. On 2026-09-02 that means `asia(cur)_high` at
+74.25 does not appear there, even though it is inside the floor. If this layer ever needs
the FULL set of candidates between the cautious band's flat 40 and the ATR-scaled DOL floor,
it must come from `bundle.levels` directly rather than from that output. Recorded here
because the asymmetry is deliberate on their side and invisible from this one.

**Absent is a value, not a failure.** An empty ladder is clause 12 (no regime flip ever
occurs) and an empty menu is clause 14 (no position on this track). Both are reported and
counted; neither is ever silently defaulted to something reachable, which is the failure
mode that would quietly turn "we had no target" into "we had an easy one".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pandas as pd

from agent.study import legacy_liquidities as _ll
from agent.study.target_offline import menu_at

TZ = "America/New_York"
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_DIR = os.path.join(_REPO, ".agents", "label-corpus")

TRACK_PRODUCTION = "production"
TRACK_ORACLE = "oracle"
TRACKS = (TRACK_PRODUCTION, TRACK_ORACLE)

#: `entries.ARM_ET`, restated as a clock string for `menu_at`. The menu is built at the
#: L1 boundary production takes its own DOL at, which is 09:20 — the Executor arms one
#: minute later, and the menu it acts on is the one already decided.
L1_BOUNDARY_HHMM = "09:20"


@dataclass(frozen=True)
class Targets:
    """Everything the policy is handed, plus the provenance it never reads."""
    date: str
    track: str
    direction: str
    dol: "float | None" = None
    dol_level: "str | None" = None
    dol_source: "str | None" = None
    initial: "float | None" = None
    initial_level: "str | None" = None
    #: Recorded, never acted on — the target layer's fill re-anchor question.
    d1_at_fill: "float | None" = None
    d1_at_fill_level: "str | None" = None
    #: Reporting only: how far the menu had to look, and what it was anchored on.
    n_menu_rows: int = 0
    menu_now_price: "float | None" = None
    avg_range_1h: "float | None" = None

    def to_dict(self) -> dict:
        return {"date": self.date, "track": self.track, "direction": self.direction,
                "dol": self.dol, "dol_level": self.dol_level,
                "dol_source": self.dol_source, "initial": self.initial,
                "initial_level": self.initial_level, "d1_at_fill": self.d1_at_fill,
                "d1_at_fill_level": self.d1_at_fill_level,
                "n_menu_rows": self.n_menu_rows,
                "menu_now_price": self.menu_now_price,
                "avg_range_1h": self.avg_range_1h}


# --------------------------------------------------------------------------- #
# DOL tracks                                                                    #
# --------------------------------------------------------------------------- #

def load_labels(corpus_dir: str = CORPUS_DIR, ticker: str = "MNQ") -> dict:
    """`{date: label row}` for the PRIMARY segment of each session, one instrument.

    Primary only, per spec §4.3: a secondary continuation's draw is an extension no fact
    at entry could have anticipated, and the policy is armed once per session.
    """
    out = {}
    with open(os.path.join(corpus_dir, "labels.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("role") == "primary" and row.get("ticker") == ticker:
                out[row["date"]] = row
    return out


def oracle_dol(label_row) -> tuple:
    """`(price, level_names)` for a LABELLED draw, else `(None, None)`.

    An `unexplained` session has no draw at all — spec §2's rule 4 makes that a headline
    number and never a fallback label — so it yields no DOL and, by clause 14, no
    position on this track.
    """
    if not label_row or label_row.get("status") != "labelled":
        return None, None
    price = label_row.get("price")
    if price is None:
        return None, None
    names = label_row.get("names") or ()
    return float(price), "+".join(names) if names else None


def production_dol(facts, date, direction, *, clock: str = L1_BOUNDARY_HHMM) -> dict:
    """`build_menus`'s D1 at `clock`, plus the anchor it was built on.

    D1 is row 0 because `_dol_menu` sorts nearest-first; `target_offline._nearest_first_pick`
    is the same statement and this defers to the menu's own ordering rather than
    re-sorting it.
    """
    ts = pd.Timestamp(f"{date} {clock}", tz=TZ)
    menu = menu_at(facts, ts, str(direction).upper())
    rows = menu.get("rows") or ()
    top = dict(rows[0]) if rows else None
    return {"row": top, "n_rows": len(rows), "now_price": menu.get("now_price"),
            "avg_range_1h": menu.get("avg_range_1h")}


# --------------------------------------------------------------------------- #
# The initial target                                                            #
# --------------------------------------------------------------------------- #

def initial_target(bars_1m, direction, fill_price, at) -> tuple:
    """`(price, level)` = production's `cautious_price_initial` at the fill, or
    `(None, None)` when the ladder is empty.

    `direction` is lowercased because `compute_cautious_prices` compares against the
    literals `"up"` / `"down"`; anything else silently produces an empty ladder, which
    would read exactly like clause 12 and be indistinguishable from it in the report.
    """
    d = _DIRECTION.get(str(direction).upper())
    if d is None or fill_price is None:
        return None, None
    from hypothesis import compute_cautious_prices

    ts = pd.Timestamp(at)
    liq = _ll.reconstruct(bars_1m, ts)
    if not liq:
        return None, None
    cp = compute_cautious_prices(d, float(fill_price), liq,
                                 _ll.historical_ath(bars_1m, ts), 0,
                                 invalidated_names=None, now=ts)
    price = cp.get("cautious_price_initial")
    if price == "" or price is None:
        return None, None
    return float(price), cp.get("cautious_price_initial_level") or None


_DIRECTION = {"UP": "up", "LONG": "up", "DOWN": "down", "SHORT": "down"}


def targets_for(date, direction, track, *, facts=None, label_row=None, bars_1m=None,
                fill_price=None, fill_ts=None) -> Targets:
    """Assemble one session's injected prices for one track.

    Every input is optional so a caller can build the DOL half before the fill exists and
    the initial-target half after — which is the actual order of events, since the fill
    is what the ladder is anchored to.
    """
    base = dict(date=str(date), track=str(track), direction=str(direction).upper())
    dol = level = source = None
    n_rows = 0
    now_price = avg1h = None

    if track == TRACK_ORACLE:
        dol, level = oracle_dol(label_row)
        source = "label_corpus_draw"
    elif track == TRACK_PRODUCTION:
        if facts is None:
            raise ValueError("the production track needs a facts source")
        got = production_dol(facts, date, direction)
        row, n_rows = got["row"], got["n_rows"]
        now_price, avg1h = got["now_price"], got["avg_range_1h"]
        dol = None if row is None else float(row["price"])
        level = None if row is None else row.get("level")
        source = f"build_menus_d1_{L1_BOUNDARY_HHMM}"
    else:
        raise ValueError(f"unknown track {track!r}")

    initial = initial_level = None
    d1_fill = d1_fill_level = None
    if fill_price is not None and fill_ts is not None:
        if bars_1m is not None:
            initial, initial_level = initial_target(bars_1m, direction, fill_price,
                                                    fill_ts)
        if facts is not None:
            ts = pd.Timestamp(fill_ts)
            got = production_dol(facts, date, direction,
                                 clock=ts.strftime("%H:%M:%S"))
            row = got["row"]
            d1_fill = None if row is None else float(row["price"])
            d1_fill_level = None if row is None else row.get("level")

    return Targets(**base, dol=dol, dol_level=level, dol_source=source,
                   initial=initial, initial_level=initial_level,
                   d1_at_fill=d1_fill, d1_at_fill_level=d1_fill_level,
                   n_menu_rows=n_rows, menu_now_price=now_price, avg_range_1h=avg1h)
