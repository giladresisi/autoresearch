"""Initial-target stage: a structural checkpoint between the fill and the T2 target.

Plan 35 (`.agents/plans/35.initial-target-stage.md`), selection rule v2 (§2.2–§2.3). The
Executor holds two numbers for an open position — the fixed stop and ONE T2 target. This
module adds a third, the INITIAL target: a structural level between the fill and T2 that
the trade should reach on the way, offset 2 pts toward the fill; or, with no such level,
a synthetic 85 % of the way to the target. Reaching it is a one-way flip observed on
COMPLETED 1m bars (touch AND close beyond), never on a tick.

Pure by construction: no facts, no bars store, no clock, no recorder. The Executor
hands in the level universe it already built for the T2 pick (`target.level_universe`),
drives `InitialTargetTracker` on the completed 1m bar it already computes for §6/§7,
and records what comes back. What happens AFTER the flip (plan §2.5: move the stop /
exit on the first opposite close / nothing) is the Executor's decision behind
`INITIAL_TARGET_ACTION`; the tracker only observes, and after the flip it keeps
observing what each action WOULD have done, so the record-only default still produces
the data the choice needs.

This is a RE-IMPLEMENTATION on the facts layer, not a call into the legacy engine —
`agent/trader/__init__.py` states that boundary. The v2 rule applies the T2 menu's own
eligibility (`derive_facts._dol_menu`: correct side, unswept, undepleted, not nested)
and then chooses by the KIND of structure (session extreme > mid/open > prevN level)
rather than by raw distance; see `select_initial_target`.
"""
from __future__ import annotations

import re

import pandas as pd

MIN_DIST_PTS = 40.0              # an initial nearer than this to the fill is not a stage
INITIAL_OFFSET_PTS = 2.0         # the initial sits this much closer to the fill than its level
SYNTHETIC_RATIO = 0.85           # fallback: this fraction of the way from the fill to T2
SYNTHETIC_LEVEL = "synthetic_85pct"
BAND = (0.20, 0.80)              # a level is eligible only inside this share of |T2 - fill|

#: The two knobs plan §2.3 leaves as PARAMETERS, settled by the 30-day rerun (§5).
#: EXTREME_MIN: a session extreme (tier 1) qualifies only if it is at least this far from
#: the fill — ("pts", 150.0) is variant A, ("frac", 0.65) (a share of |T2 - fill|) is
#: variant F. MID_PREFERENCE: which mid/open wins inside tier 2.
EXTREME_MIN: tuple = ("pts", 150.0)
MID_PREFERENCE = "session_mid_first"
MID_PREFERENCES = ("nearest", "farthest", "session_mid_first")
EXTREME_MIN_KINDS = ("pts", "frac")

#: Tier 1: session extremes, most recent session first (index = recency). `rth` is the
#: running post-09:30 extreme `target.level_universe` adds for plan 35; the others are
#: the bundle's closed 6h blocks or the in-progress one, all tagged `(cur)`. The tag is
#: optional in the pattern only so a legacy-shaped list (`ny_morning_low`, the shape the
#: pre-facts engine logged) classifies the same way; production never emits bare names,
#: and `(prev1)` sessions are deliberately NOT a tier-1 family.
SESSION_RECENCY = ("asia", "london", "ny_morning", "rth")
_SESSION_EXTREME_RE = re.compile(r"^(rth|ny_morning|london|asia)(?:\(cur\))?_(high|low)$")
#: Tier 2: mids and opens, eligible in BOTH directions.
SESSION_MID = "ny_morning(cur)_mid"
MID_NAMES = (SESSION_MID, "day_mid", "week_mid", "TDO", "TWO")
#: Tier 3: prevN day/week levels.
_PREVN_RE = re.compile(r"^prev\d+_(day|week)_(high|low)$")

#: Tier codes in the returned dict / `initial_target_selected` record.
TIER_SESSION_EXTREME, TIER_MID, TIER_PREVN, TIER_SYNTHETIC = 1, 2, 3, 0

_SHORT = ("DOWN", "SHORT")


def _side(direction) -> int:
    """+1 for a long (the trade travels up), -1 for a short."""
    return -1 if str(direction or "").upper() in _SHORT else 1


def _iter_levels(levels):
    """Yield (name, price, flags) from a list of pairs or of {name, price, ...} dicts;
    junk skipped. `flags` = (swept, depleted, suppressed), all False for bare pairs."""
    for item in levels or ():
        try:
            if isinstance(item, dict):
                name, price = item.get("name"), item.get("price")
                flags = (bool(item.get("swept")), bool(item.get("depleted")),
                         bool(item.get("suppressed")))
            else:
                name, price = item[0], item[1]
                flags = (False, False, False)
            if price is None or isinstance(price, bool):
                continue
            price = float(price)
            if price != price:                      # NaN
                continue
            yield (str(name) if name is not None else ""), price, flags
        except (TypeError, ValueError, IndexError):
            continue


def _knobs(extreme_min, mid_preference) -> tuple:
    """The (extreme_min, mid_preference) pair in force: the call's overrides or the
    module defaults, validated (a typo must not silently select as some other rule)."""
    em = EXTREME_MIN if extreme_min is None else extreme_min
    mp = MID_PREFERENCE if mid_preference is None else str(mid_preference)
    try:
        kind, value = str(em[0]), float(em[1])
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"EXTREME_MIN must be ('pts'|'frac', number), got {em!r}")
    if kind not in EXTREME_MIN_KINDS or value != value or value < 0:
        raise ValueError(f"EXTREME_MIN must be ('pts'|'frac', number >= 0), got {em!r}")
    if mp not in MID_PREFERENCES:
        raise ValueError(f"MID_PREFERENCE must be one of {MID_PREFERENCES}, got {mp!r}")
    return (kind, value), mp


def variant_label(extreme_min=None, mid_preference=None) -> str:
    """`"pts:150|session_mid_first"`-style tag of the knobs in force, for the record and
    the study's per-variant files."""
    (kind, value), mp = _knobs(extreme_min, mid_preference)
    return f"{kind}:{value:g}|{mp}"


def select_initial_target(direction, anchor, secondary, levels, *, suppressed=(),
                          attempts_used=0, extreme_min=None,
                          mid_preference=None) -> "dict | None":
    """The initial target for a position filled at `anchor` whose T2 target is `secondary`
    (plan 35 §2.2–§2.3, v2).

    `levels` is the level universe as (name, price) pairs or {name, price, swept,
    depleted, suppressed} dicts (`target.level_universe`). With D = |secondary - anchor|
    and `along` a level's signed distance from the fill along the trade, a level is
    ELIGIBLE when it is strictly between the fill and T2; on the correct side (a `_low`
    is never an initial for a long, a `_high` never for a short; mids/opens go both
    ways); not swept, not depleted, not nested (`suppressed` flag or name in
    `suppressed`); inside the band `0.20·D <= along <= 0.80·D`; and still >= 40 pts from
    the fill AFTER the 2-pt offset. Then, by tier:

      1. session extremes in the trade direction (`rth(cur)`, `ny_morning(cur)`,
         `london(cur)`, `asia(cur)`) that also clear EXTREME_MIN → the MOST RECENT session;
      2. mids/opens (`ny_morning(cur)_mid`, `day_mid`, `week_mid`, `TDO`, `TWO`) per
         MID_PREFERENCE: "nearest" | "farthest" | "session_mid_first" (the NY-morning mid
         if eligible, else the farthest of the rest);
      3. prevN day/week levels → the farthest from the fill;
      fallback: anchor ± 0.85·D as `synthetic_85pct` if 0.85·D - 2 >= 40, else None.

    Nothing else in the universe (day/week extremes, `(prev1)` sessions, projections) is
    a candidate. The legacy per-attempt shrink is gone (the band scales with D);
    `attempts_used` is only echoed. `extreme_min` / `mid_preference` override the module
    defaults per call (the study's grid). Returns None when there is no initial stage —
    a real outcome (T2 too close for even the fallback to clear the floor), not an
    error; the position then runs on its stop and target alone.

    Returned dict: `price` (the initial, offset applied), `level` (the level's name or
    `synthetic_85pct`), `level_price` (raw level, None for the fallback), `tier` (1/2/3,
    0 = synthetic), `anchor`, `secondary`, `band` ([lo, hi] in pts from the fill),
    `n_candidates` (levels that passed eligibility, across tiers), `variant`,
    `attempts_used`.
    """
    (ext_kind, ext_value), mid_pref = _knobs(extreme_min, mid_preference)
    variant = f"{ext_kind}:{ext_value:g}|{mid_pref}"
    try:
        n_attempts = max(0, int(attempts_used or 0))
    except (TypeError, ValueError):
        n_attempts = 0
    if anchor is None or secondary is None:
        return None
    try:
        p, s = float(anchor), float(secondary)
    except (TypeError, ValueError):
        return None
    side = _side(direction)
    if (s - p) * side <= 0:
        return None                       # the target is not on the trade side of the fill
    d_total = abs(s - p)
    band_lo, band_hi = BAND[0] * d_total, BAND[1] * d_total
    ext_min_pts = ext_value if ext_kind == "pts" else ext_value * d_total
    want_sub = "low" if side < 0 else "high"
    suppressed_names = set(suppressed or ())

    tier1: list = []      # (recency, along, price, name)
    tier2: dict = {}      # name -> (along, price)
    tier3: list = []      # (along, price, name)
    n_cands = 0
    for name, price, (swept, depleted, nested) in _iter_levels(levels):
        along = (price - p) * side        # signed distance from the fill, along the trade
        if along <= 0 or (s - price) * side <= 0:
            continue                      # not strictly between the fill and the target
        if swept or depleted or nested or name in suppressed_names:
            continue
        m_ext = _SESSION_EXTREME_RE.match(name)
        m_prev = None if m_ext else _PREVN_RE.match(name)
        if m_ext:
            sub = m_ext.group(2)
        elif name in MID_NAMES:
            sub = None
        elif m_prev:
            sub = m_prev.group(2)
        else:
            continue                      # not a family the selector draws from
        if sub is not None and sub != want_sub:
            continue                      # wrong side (a _low for a long, a _high for a short)
        if not (band_lo <= along <= band_hi):
            continue
        if along - INITIAL_OFFSET_PTS < MIN_DIST_PTS:
            continue                      # the floor is tested on the OFFSET price
        n_cands += 1
        if m_ext:
            if along < ext_min_pts:
                continue                  # a session extreme too near to be the stage
            tier1.append((SESSION_RECENCY.index(m_ext.group(1)), along, price, name))
        elif m_prev:
            tier3.append((along, price, name))
        else:
            tier2[name] = (along, price)

    base = {"anchor": p, "secondary": s, "band": [round(band_lo, 2), round(band_hi, 2)],
            "n_candidates": n_cands, "variant": variant, "attempts_used": n_attempts}

    def _pick(tier, price, name):
        return {**base, "price": price - side * INITIAL_OFFSET_PTS, "level": name,
                "level_price": price, "tier": tier}

    if tier1:
        _rec, _along, price, name = max(tier1)         # most recent session; then farthest
        return _pick(TIER_SESSION_EXTREME, price, name)
    if tier2:
        if mid_pref == "session_mid_first" and SESSION_MID in tier2:
            return _pick(TIER_MID, tier2[SESSION_MID][1], SESSION_MID)
        ranked = sorted((along, price, name) for name, (along, price) in tier2.items())
        _along, price, name = ranked[0] if mid_pref == "nearest" else ranked[-1]
        return _pick(TIER_MID, price, name)
    if tier3:
        _along, price, name = max(tier3)
        return _pick(TIER_PREVN, price, name)
    d = SYNTHETIC_RATIO * d_total
    if d - INITIAL_OFFSET_PTS >= MIN_DIST_PTS:
        return {**base, "price": p + side * d, "level": SYNTHETIC_LEVEL,
                "level_price": None, "tier": TIER_SYNTHETIC}
    return None


def _label(bar):
    name = getattr(bar, "name", None)
    return name if name is not None else (bar.get("time") if isinstance(bar, dict) else None)


def _ohlc(bar):
    return (float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"]))


class InitialTargetTracker:
    """The reached-state machine for one position's initial target.

    Driven on COMPLETED 1m bars only, the fill's own minute included: that bar is judged
    over its FULL range, pre-fill ticks and all (the initial is >= 40 pts from the fill,
    so a same-minute flip is a genuine 40-pt displacement, not an artefact). `reached`
    flips once, when a bar both touches the
    initial (Low <= it for a short, High >= for a long) AND closes beyond it. A touch
    alone flips nothing. Same-bar precedence: when `stop` is supplied and the bar also
    touched it, the stop wins — the position is gone, so there is nothing to stage.
    (The Executor's `OrderSim` books that stop on the tick, before this ever runs; the
    argument makes the rule hold for a caller driving 1m bars directly.)

    After the flip the tracker keeps watching, once each, for what plan §2.5's two
    actions WOULD have done on later bars: A's stop at the initial level being touched
    (`cf_stop`) and B's first opposite 1m close (`cf_opp_close`). Those are
    observations for the record-only default; under the live actions they coincide with
    the real exit.
    """

    def __init__(self, direction, initial, level=None) -> None:
        self.direction = direction
        self.side = _side(direction)
        self.initial = float(initial)
        self.level = level
        self.reached = False
        self.reached_at = None
        self.reached_close = None
        self.stopped = False
        self.cf_stop_at = None
        self.cf_opp_close_at = None

    # -- the two tests, spelled once ------------------------------------------- #

    def _touched(self, hi, lo) -> bool:
        return (lo <= self.initial) if self.side < 0 else (hi >= self.initial)

    def _closed_beyond(self, close) -> bool:
        return (close < self.initial) if self.side < 0 else (close > self.initial)

    def _stop_touched(self, hi, lo, stop) -> bool:
        if stop is None:
            return False
        stop = float(stop)
        return (hi >= stop) if self.side < 0 else (lo <= stop)

    # -- driving --------------------------------------------------------------- #

    def on_bar_close(self, bar, stop=None) -> "dict | None":
        """One completed 1m bar. Returns the `reached` event exactly once, else None."""
        if self.reached or self.stopped:
            return None
        _o, hi, lo, close = _ohlc(bar)
        if self._stop_touched(hi, lo, stop):
            self.stopped = True           # the stop wins; the position is gone
            return None
        if self._touched(hi, lo) and self._closed_beyond(close):
            self.reached = True
            self.reached_at = _label(bar)
            self.reached_close = close
            return {"kind": "reached", "bar": self.reached_at, "price": self.initial,
                    "close": close, "level": self.level}
        return None

    def post_flip(self, bar) -> "list[dict]":
        """Bars AFTER the flip bar: the counterfactual exits of actions A and B, each
        reported once. Empty before the flip and on the flip bar itself."""
        out: list = []
        if not self.reached:
            return out
        label = _label(bar)
        if label is not None and self.reached_at is not None and label == self.reached_at:
            return out
        o, hi, lo, close = _ohlc(bar)
        if self.cf_stop_at is None and self._stop_touched(hi, lo, self.initial):
            self.cf_stop_at = label
            out.append({"kind": "cf_stop", "bar": label, "price": self.initial})
        opposite = (close > o) if self.side < 0 else (close < o)
        if self.cf_opp_close_at is None and opposite:
            self.cf_opp_close_at = label
            out.append({"kind": "cf_opp_close", "bar": label, "price": close})
        return out

    def state(self) -> dict:
        return {"initial": self.initial, "level": self.level, "reached": self.reached,
                "reached_at": (str(self.reached_at) if self.reached_at is not None else None),
                "stopped": self.stopped,
                "cf_stop_at": (str(self.cf_stop_at) if self.cf_stop_at is not None else None),
                "cf_opp_close_at": (str(self.cf_opp_close_at)
                                    if self.cf_opp_close_at is not None else None)}


def minute_of(ts) -> "pd.Timestamp | None":
    """The 1m label a bar instant belongs to (left-labelled, like `_completed_1m`)."""
    try:
        return pd.Timestamp(ts).floor("1min")
    except Exception:
        return None


# A typo in the module defaults must fail at import, like `INITIAL_TARGET_ACTION` does in
# the Executor — not inside `_arm_initial_target`'s try at the first live fill, where it
# would only surface as `initial_target_error` and swallow the "written on EVERY fill" record.
_knobs(None, None)
