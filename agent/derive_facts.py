"""POC hard-truth extractor — computes FACTS ONLY from the two bar slices.

Usage (from anywhere):
    python derive_facts.py [--dir <folder-with-slices>] [--ath-mnq X] [--ath-mes Y]

Reads MNQ_1s_slice.parquet + MES_1s_slice.parquet from --dir (default: this file's
folder), treats the last bar as "now", and prints the deterministic context the
decision protocols need: level map (wick + body extremes), sweep times + depletion
states, cross-ticker sweep matrix (wick and 15m-body), equilibrium acceptance,
structure bars, FVGs (both tickers), checkpoint snapshot.

It makes NO decisions: no votes, no ledgers, no SMT fire adjudication, no
direction/confidence. Those are the decision agent's job per
decisions/daily-trend.md and decisions/next-move.md.

Engine-grounded conventions (v2 — aligned to the live engine after POC run 3):
- Bars in the CME maintenance window (after 16:55:00 ET, before 18:00 ET) are DROPPED;
  weekend/stub trade dates (Sat/Sun or < 1000 bars) are excluded from the date universe.
- True Day = 18:00 ET -> 16:55 ET next day; trade date = close date.
- Week extremes/mid use the ENGINE anchor (session_pipeline._week_start_ts):
  Sunday 18:00 ET, EXTENDED for early-week sessions — Monday session -> prev Wednesday
  18:00 ET, Tuesday session -> prev Thursday 18:00 ET (the start of that prior day's OWN
  full session, not just its last hour).
- Sweeps are INCLUSIVE (low <= level / high >= level), matching the engine.
- Depletion thresholds: MNQ week 80 / day 40 / session 20; MES week 12 / day 6 /
  session 3 (exact engine tables, not a ratio); DEPLETED at excursion >= threshold.
- Levels carry BODY (close) extremes too; body sweeps are reported on 15min closes
  (hidden-SMT convention, smt_detect HIDDEN_TFS).
- 4hr/1hr bars are 18:00-ET-SESSION-anchored resamples (CME/TradingView convention:
  18:00/22:00/02:00/06:00/10:00/14:00 ET, final bar ~3h before the maintenance gap) --
  NOT the midnight/epoch grid pandas defaults to.
- FVGs on COMPLETED 1hr and 4hr bars, BOTH tickers; "visited" counts any 1s re-entry
  strictly after the third bar's OPEN label (daily.py convention).
"""

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

TZ = "America/New_York"
DEPLETE = {
    "MNQ": {"week": 80.0, "day": 40.0, "session": 20.0},
    "MES": {"week": 12.0, "day": 6.0, "session": 3.0},
}

# plan 15 Task 5: tier-relative SMT "shelf life" — how many avg-1h-ranges of stretch (price
# distance from the SMT's fire price) an SMT candidate can absorb before it is CODE-SUGGESTED
# exhausted (a played-out old divergence, no longer continuation evidence). Each tier gets
# ~double the shelf life of the tier below (a week-tier divergence stays relevant far longer
# than a session-tier one). v1 seeds, pending calibration — same status as every other
# threshold in decisions/thesis.md (§2.1c stretch flag, the DEPLETE tables, etc.).
SMT_SHELF_LIFE = {"session": 2.0, "day": 4.0, "week": 8.0}

# 2026-08-02: P5 (FVG-fill) has no tier concept (session/day/week), so a single shelf life
# applies -- matching SMT_SHELF_LIFE's "day" value, a reasonable default since a filled
# 3-bar gap is a day-scale event, not week-scale. v1 seed, pending calibration, same status
# as SMT_SHELF_LIFE above.
FVG_SHELF_LIFE = 4.0

# thesis.md §2.1b/§2.1d: tier rank used to pick a single representative when two or more
# named levels turn out to be restatements of the same physical sweep (nesting tie-break,
# and the duplicate-simultaneous-sweep collapse) — week > day > session.
_TIER_RANK = {"week": 3, "day": 2, "session": 1}

# thesis.md §2.1b/§7 — v1 seed, pending calibration. How far back (from `now`, NOT from the
# current session's own open) derive_facts searches for a day/week-tier level's TRUE first
# touch, so a level whose prevN name shifts across a session rollover doesn't lose its real
# swept_at to a same-priced, unrelated LATER re-touch found only under the new name (the
# 2026-07-14 01:00 ET case: prev3_day_low's real sweep was 16:15 ET on 2026-07-13, ~8.75h
# before the call and within a prior session — session-scoped search only found an unrelated
# 18:55:51 re-touch instead). Session-tier sub-blocks are NOT widened — single-session
# concepts by design (thesis.md §2.1b).
SMT_LOOKBACK_HOURS = 24
# S9-only recency window for the P5 FVG-fill candidate list (thesis.md §2.1 P5). The raw
# S6 FVG list stays the full historical scan; S9 curates it to VISITED zones formed within
# this many days of now, mirroring how S9's SMT-candidate list is a curated subset of the
# raw S3 sweep matrix.
#
# Was 3 -- found (2026-07-30 root-cause audit) to silently exclude the exact zones that
# motivated this fix in the first place: evaluating 2026-07-27 09:20 ET, the 2026-07-23
# 07:00/08:00 ET bearish zones sit 4+ days back (a `now - 3 days` cutoff of ~07-24 09:20
# lands hours AFTER those same-morning zones formed), so a 3-day window drops them from
# the facts entirely -- not merely undeclared, structurally absent. 5 days reliably
# includes same-time-of-day zones a full 4 calendar days back while keeping the curated
# list well short of the original ~70-line/10-day dump (32-37 zones across several dates
# checked, vs. 3 days' 13-21).
FVG_LOOKBACK_DAYS = 5


def load(path):
    df = pd.read_parquet(path).rename(columns=str.lower)
    df = df[["open", "high", "low", "close"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize(TZ)
    # Engine processed window: drop CME maintenance bars (16:55, 18:00) — stray ticks
    # there create phantom sessions and corrupt TDO/prev-day levels (POC run-3 G12).
    t = df.index.time
    keep = (t <= datetime.time(16, 55)) | (t >= datetime.time(18, 0))
    return df[keep]


def trade_date(ts):
    return (ts + pd.Timedelta(hours=7)).date()


def week_start_ts(now):
    """Engine week anchor: Sun 18:00 ET, extended for early-week sessions so the window
    covers the FULL prior trading day's own session, not just part of it (Mon session ->
    prev Wed 18:00, the start of Thursday's OWN session; Tue session -> prev Thu 18:00, the
    start of Friday's OWN session). A trading day's session runs from 18:00 the evening
    before to ~17:00 that day (trade_date's +7h convention) -- anchoring at "Thursday 18:00"
    would only capture the LAST hour of Thursday's session (18:00-17:00 the next day is
    already trade-date Friday), missing the entire rest of it. This is a DELIBERATE
    divergence from session_pipeline._week_start_ts / hypothesis.py::compute_live_hl_mid
    (both still anchor one day later, e.g. prev Thursday for a Monday session) -- those are
    live production code, not touched here; this fixes the L1-thesis-only copy."""
    today = now.date()
    session_open = today if now.hour >= 18 else today - datetime.timedelta(days=1)
    wd = session_open.weekday()  # Mon=0 .. Sun=6
    if wd == 6:      # Sunday open -> Monday session
        anchor = session_open - datetime.timedelta(days=4)  # prev Wednesday (Thursday's own open)
    elif wd == 0:    # Monday open -> Tuesday session
        anchor = session_open - datetime.timedelta(days=4)  # prev Thursday (Friday's own open)
    else:
        anchor = session_open - datetime.timedelta(days=(wd + 1) % 7)
    return pd.Timestamp(datetime.datetime(anchor.year, anchor.month, anchor.day, 18, 0), tz=TZ)


def _day_start_ts(now: pd.Timestamp) -> pd.Timestamp:
    """Engine day-extreme anchor (hypothesis.py::compute_live_hl_mid), mirroring
    week_start_ts's own pattern: session-phase-dependent, not a fixed 18:00-today offset, so
    the running day_hi/day_lo/day_mid window is never degenerate right at/after the
    mandatory 18:00 ET session-open call (thesis.md §1). Asia (now.hour>=18): today at
    06:00 ET -- reaches into the PRIOR session's NY-morning-through-close (~12h of real data
    instead of ~0). London (now.hour<6): yesterday at 12:00 ET -- reaches into the prior
    session's NY-evening open. NY-morning onward (6<=now.hour<18): yesterday at 18:00 ET --
    exactly the current session's own open (no extension needed -- by then the session
    already has ample same-session data). Deliberately does NOT port
    compute_live_hl_mid's opening-spike outlier skip (excluding the first 90min from
    whichever side it distorts) -- window extension only, a separate refinement."""
    today = now.date()
    if now.hour >= 18:
        d, hr = today, 6
    elif now.hour < 6:
        d, hr = today - datetime.timedelta(days=1), 12
    else:
        d, hr = today - datetime.timedelta(days=1), 18
    return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, hr, 0), tz=TZ)


def ohlc(df, rule, offset=pd.Timedelta(hours=18)):
    """Resample to `rule`-length bars anchored to the 18:00 ET session open (CME/
    TradingView convention), not the midnight/epoch grid pandas defaults to. For a "4h"
    rule this gives 18:00/22:00/02:00/06:00/10:00/14:00 ET boundaries, with the final
    bar of the day naturally running only ~3h (14:00 -> the last bar before the 16:55-
    18:00 maintenance gap in the data) since there is no data to fill the rest of that
    bin -- no special-casing needed. The 18h offset is an exact multiple of every OTHER
    rule this helper is called with (1h/30min/15min/5min), so it is a no-op for those
    and safe to apply unconditionally."""
    return df.resample(rule, offset=offset).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()


def session_frame(df, td):
    # Vectorised trade-date mask (identical values to df.index.map(trade_date), but a
    # single C-level shift+.date instead of a Python per-row call — this is on the AI-decisions
    # hot path, called dozens of times per snapshot).
    tds = (df.index + pd.Timedelta(hours=7)).date
    return df[tds == td]


def sub_blocks(sess):
    hours = sess.index.hour
    return {
        "asia": sess[(hours >= 18)],
        "london": sess[(hours >= 0) & (hours < 6)],
        "ny_morning": sess[(hours >= 6) & (hours < 12)],
        "ny_evening": sess[(hours >= 12) & (hours < 17)],
    }


def hl(frame):
    if len(frame) == 0:
        return None, None, None, None
    return (float(frame["high"].max()), float(frame["low"].min()),
            float(frame["close"].max()), float(frame["close"].min()))


def first_cross(frame, level, side):
    """Inclusive wick cross (engine convention: <= / >=)."""
    hits = frame[frame["low"] <= level] if side == "below" else frame[frame["high"] >= level]
    return hits.index[0] if len(hits) else None


def first_body_cross_15m(frame, level, side):
    """First 15min bar CLOSE beyond the level (hidden/body-SMT convention)."""
    if len(frame) == 0:
        return None
    m15 = frame["close"].resample("15min").last().dropna()
    hits = m15[m15 <= level] if side == "below" else m15[m15 >= level]
    return hits.index[0] if len(hits) else None


def excursion_beyond(frame, level, side):
    if side == "below":
        return max(0.0, level - float(frame["low"].min()))
    return max(0.0, float(frame["high"].max()) - level)


def closest_approach(frame, level, side):
    """For a NOT-swept level: how close price came and when (laggard test-and-fail data)."""
    if len(frame) == 0:
        return None, None
    if side == "below":
        return float(frame["low"].min()) - level, frame["low"].idxmin()
    return level - float(frame["high"].max()), frame["high"].idxmax()


def _beyond_side(value: float, price: float, side: str) -> bool:
    return value > price if side == "above" else value < price


def _htf_close_status(df: pd.DataFrame, swept_at: Optional[pd.Timestamp], *,
                       price: float, side: str, now: pd.Timestamp) -> dict:
    """For a level swept at `swept_at`, classify the most recent COMPLETED 1h/4h bar
    (18:00-ET-session-anchored, CME/TradingView convention) that CLOSED strictly after the sweep and at/
    before `now`. Returns {"1h": None, "4h": None} when `swept_at` is None (never swept)
    or when no qualifying bar has closed yet since the sweep — the maturity gate
    (decisions/thesis.md §3): an immature sweep is not usable evidence in either
    direction. Otherwise {tf: {"close": float, "beyond": bool, "closed_at": Timestamp,
    "n_closed_since": int}} — `beyond` reflects the MOST RECENT qualifying close (a
    running/current-state read, matching the RUNNING-mid convention used elsewhere in
    this module), `n_closed_since` is how many qualifying bars exist (>=1 by
    construction when not None)."""
    out: dict = {}
    if swept_at is None:
        return {"1h": None, "4h": None}
    for tf, freq in (("1h", pd.Timedelta(hours=1)), ("4h", pd.Timedelta(hours=4))):
        bars = ohlc(df, tf).iloc[:-1]              # drop the still-forming trailing bar
        if len(bars) == 0:
            out[tf] = None
            continue
        closed_at = bars.index + freq              # left-labeled bin -> actual close time
        mask = (closed_at > swept_at) & (closed_at <= now)
        eligible = bars[mask]
        if len(eligible) == 0:
            out[tf] = None
            continue
        close = float(eligible.iloc[-1]["close"])
        out[tf] = {
            "close": close,
            "beyond": _beyond_side(close, price, side),
            "closed_at": (eligible.index + freq)[-1],
            "n_closed_since": int(len(eligible)),
        }
    return out


def _last_mid_crossing(close: pd.Series, mid: float) -> Optional[pd.Timestamp]:
    """Timestamp of the LAST genuine crossing of `mid` in a close series, else None.

    A crossing is a bar whose side of the mid differs from the prior bar's; a close exactly
    at the mid continues the previous side (equality is not its own crossing). None means
    price stayed on one side across the whole window (never swept) — giving the mid the same
    "not evidence" treatment the rendering loop already applies to unswept named levels."""
    if len(close) == 0:
        return None
    sign = pd.Series(float("nan"), index=close.index)
    sign[close > mid] = 1.0
    sign[close < mid] = -1.0
    sign = sign.ffill()                            # equality carries the previous side
    prev = sign.shift(1)
    mask = (sign.notna() & prev.notna() & (sign != prev)).to_numpy()
    crossings = sign.index[mask]
    return crossings[-1] if len(crossings) else None


_TF_FREQ = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4)}


def _last_completed_bar(df: pd.DataFrame, tf: str, now: pd.Timestamp) -> Optional[dict]:
    """The most recently COMPLETED <tf> bar strictly at/before `now` -- {open, high, low,
    close, closed_at}, or None if no completed bar exists yet. Used by _mid_tf_state and
    the §10 (2026-08-05) partial-bar reversal check, which need the completed bar's FULL
    range (not just its close, which _htf_close_status already exposes) to tell how far
    the currently-forming NEXT bar has already retraced back through it."""
    freq = _TF_FREQ[tf]
    bars = ohlc(df, tf).iloc[:-1]                   # drop the still-forming trailing bar
    if len(bars) == 0:
        return None
    closed_at = bars.index + freq
    eligible = bars[closed_at <= now]
    if len(eligible) == 0:
        return None
    row = eligible.iloc[-1]
    return {"open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
           "close": float(row["close"]), "closed_at": (eligible.index + freq)[-1]}


def _mid_tf_state(df: pd.DataFrame, mid: float, tf: str, now: pd.Timestamp,
                  hi_ts, lo_ts) -> tuple:
    """thesis.md §10 (2026-08-05 rewrite): per-tf mid status + P3-vs-P4 freshness, anchored
    to COMPLETED <tf>-bar closes rather than the single most recent 1s-level crossing.

    A mid, unlike a named P1 level (swept once, permanently 'active' thereafter), has no
    fixed side and is retested constantly -- normal, expected for an equilibrium. Anchoring
    maturity to "time since the single most recent crossing" (a plain swept level's own
    convention) means ANY re-touch, even one still inside the currently-FORMING bar, can
    hide an already-COMPLETED, decisive bar's own settled test. Motivating case (2026-07-22
    09:00-09:20 ET): MNQ's weekly_mid 08:00-09:00 1h bar closed decisively below the mid
    (swept from above), but a 1s-level re-touch at 09:02:47 ET -- INSIDE the still-forming
    09:00-10:00 bar -- reset the old single, 1s-anchored `swept_at` past that close
    entirely, making the whole test invisible (immature) to _htf_close_status.

    Fixed by decoupling two questions: (1) the VERDICT always reads the MOST RECENTLY
    COMPLETED <tf> bar's own close, mature whenever the mid has been crossed at least once
    among completed <tf> bars (against the mid's CURRENT value -- the same approximation
    the rest of this module already makes for a moving mid); (2) FRESHNESS (for the P3-vs-
    P4 promotion) is anchored to that SAME bar-to-bar crossing -- the last COMPLETED <tf>
    bar whose close changed sides relative to the PRECEDING completed <tf> bar's close --
    compared against the tier's own most-recent extreme timestamp on EITHER side (max of
    `hi_ts`/`lo_ts`; at/before the crossing -> fresh — 2026-08-15, see the inline comment).

    Returns (status, reclaim): `status` is _htf_close_status's per-tf shape ({"close",
    "beyond", "closed_at", "n_closed_since"}) or None (immature -- never crossed within
    `SMT_LOOKBACK_HOURS` (24h) at 1s granularity, or no completed bar exists yet); `reclaim`
    is {"fresh": bool, "cross_dir": "up"|"down"} or None (mirrors status's maturity).

    Two DIFFERENT bounds, for two different questions -- collapsing them into one caused a
    same-day regression (2026-07-27 09:20 ET, MNQ weekly_mid REJECTED): (1) "is this mid
    relevant AT ALL right now" is bounded to the recent 24h window (`SMT_LOOKBACK_HOURS`,
    1s-granularity `_last_mid_crossing` -- reused unchanged from the ORIGINAL, already-
    correct design) -- without this bound, a mid that has simply sat on ONE side for its
    entire visible history (2026-07-27 09:20 ET, MES weekly_mid) would spuriously "cross"
    days earlier purely because the mid's CURRENT value differs from where price was that
    far back. (2) "which bar's close is the verdict, and when was the crossing that
    produced it" is UNBOUNDED (searches the full available `df`) once (1) has confirmed
    relevance -- restricting bar-to-bar crossing search to the SAME 24h window as (1) can
    cut off the "before" bar needed to detect a crossing sitting near the window's edge,
    wrongly hiding an otherwise-valid, older verdict (the regression this fixes)."""
    freq = _TF_FREQ[tf]
    wide = df["close"].loc[now - pd.Timedelta(hours=SMT_LOOKBACK_HOURS):now]
    if _last_mid_crossing(wide, mid) is None:
        return None, None                          # not relevant right now -- never crossed recently
    bars = ohlc(df, tf).iloc[:-1]                   # unbounded: which bar/crossing, once relevant
    if len(bars) == 0:
        return None, None
    crossed_at_open = _last_mid_crossing(bars["close"], mid)
    if crossed_at_open is None:
        return None, None
    swept_at = crossed_at_open + freq
    cross_dir = "up" if float(bars["close"].loc[crossed_at_open]) > mid else "down"
    closed_at = bars.index[-1] + freq
    if closed_at > now:
        return None, None
    close = float(bars["close"].iloc[-1])
    status = {
        "close": close, "beyond": close > mid, "closed_at": closed_at,
        "n_closed_since": int(((bars.index + freq) > swept_at).sum()),
    }
    # 2026-08-15 (#2, from the 2026-08-10 09:20 ET audit): freshness compares against the
    # tier's LATEST extreme on EITHER side, not just the tested side — a new running
    # extreme printed AFTER the crossing means price has since moved decisively away from
    # the mid and the reclaim story is over (P3 position only) until a NEWER completed-bar
    # crossing re-arms it. The either-side extension specifically covers a wick-only new
    # extreme that never produced a completed-bar crossing back through the mid, which the
    # old tested-side-only check missed.
    _ext_ts = [t for t in (hi_ts, lo_ts) if t is not None]
    latest_extreme = max(_ext_ts) if _ext_ts else None
    reclaim = {"fresh": latest_extreme is None or latest_extreme <= swept_at,
               "cross_dir": cross_dir}
    return status, reclaim


def _htf_reversal_tier(bar: dict, level_price: float, now_price: float) -> str:
    """thesis.md §10 (2026-08-05): does the CURRENTLY-FORMING next-tf bar, as of the call,
    already undermine `bar`'s (the just-completed bar) own accept/reject verdict at
    `level_price`? A partial bar is never its own evidence (still immature by construction,
    §3) -- but ignoring how far it has ALREADY reversed by call time treats a stale,
    already-invalidated verdict as if nothing has happened since.

    Three severities, gated on `now_price` having already recrossed `level_price` back to
    the OPPOSITE side from where `bar` closed (the necessary precondition for all three):
    - 'discount': recrossed the level, but `now_price` has not yet reached `bar`'s own OPEN
      (has not fully retraced/engulfed that bar's own body) -- lower the item's points.
    - 'omit': also past `bar`'s OPEN (a full body engulf of the completed bar) but not yet
      past its WICK extreme (high, reversing up; low, reversing down) -- drop the item from
      the ledger entirely; neither the original nor a flipped read is safe to assert.
    - 'reverse': also past `bar`'s own WICK extreme -- a clear break of that bar's ENTIRE
      range, an MSS by construction -- flip the verdict (accept<->reject) rather than merely
      discount or omit it.
    'none' if the level has not been recrossed at all -- `bar`'s verdict stands, unaffected,
    at full weight.

    Motivating case (2026-07-22 09:00-09:20 ET): MNQ's 08:00-09:00 1h bar (O29055.00
    H29072.00 L28961.25 C29016.75) closed BELOW both its own weekly_mid (29031.75) and
    london(cur)_low (29026.0) -- a bearish verdict on each. By 09:20 ET (now_price
    29079.25), the forming 09:00-10:00 bar has already traded not just back above both
    levels, but above the 08:00-09:00 bar's own OPEN (29055.00) AND its own HIGH
    (29072.00) -- 'reverse' on both: a clear MSS, not just a marginal poke back across."""
    closed_above = bar["close"] > level_price
    now_above = now_price > level_price
    if now_above == closed_above:
        return "none"
    if closed_above:            # bar closed ABOVE -> reversal tested DOWNWARD
        if now_price >= bar["open"]:
            return "discount"
        if now_price >= bar["low"]:
            return "omit"
        return "reverse"
    if now_price <= bar["open"]:    # bar closed BELOW -> reversal tested UPWARD
        return "discount"
    if now_price <= bar["high"]:
        return "omit"
    return "reverse"


def age_min(ts, now):
    return (now - ts).total_seconds() / 60.0


_PREV_LEVEL_RE = re.compile(r"^prev(\d+)_(day|week)_(high|low)$")


def _nested_prev_levels(lv: dict) -> set:
    """thesis.md §2.1b: within a same-asset, same-side prevN family (day_low, day_high,
    week_low, week_high), a level is NESTED (superseded) if some MORE RECENT (smaller N)
    level in the SAME family already extends beyond it — i.e. that nearer level's own
    static price is at least as extreme. Pure price comparison across the full tracked
    depth, independent of which one got swept first.

    Nested levels are excluded from FRESH P1 accept/reject evidence and from being a
    legitimate target for a NEW P2/SMT search going forward (render_evidence_text /
    score_thesis_evidence's `suppressed_p1_levels`) — but a P2/SMT divergence that
    ALREADY fired at a nested level remains valid evidence (S3's cross-ticker matrix is
    NOT filtered by this — see render_evidence_text's smt-candidate-site exemption)."""
    families: dict = {}
    for name, (price, _body, _side, _tier, _active_from) in lv.items():
        m = _PREV_LEVEL_RE.match(name)
        if not m:
            continue
        n, fam_tier, fam_side = int(m.group(1)), m.group(2), m.group(3)
        families.setdefault((fam_tier, fam_side), {})[n] = price
    nested = set()
    for (fam_tier, fam_side), by_n in families.items():
        order = sorted(by_n)                                  # 1 = most recent
        for idx, n in enumerate(order):
            price_n = by_n[n]
            more_recent = [by_n[m] for m in order[:idx]]
            if fam_side == "low":
                if any(p <= price_n for p in more_recent):
                    nested.add(f"prev{n}_{fam_tier}_low")
            elif any(p >= price_n for p in more_recent):
                nested.add(f"prev{n}_{fam_tier}_high")
    return nested


_SESSION_LEVEL_RE = re.compile(
    r"^(asia|london|ny_morning|ny_evening)\((cur|prev1)\)_(high|low)$")

# Fixed chronological order of 6hr-session blocks: a trading day always runs
# asia -> london -> ny_morning -> ny_evening, and EVERY (prev1) session (yesterday's)
# precedes EVERY (cur) session (today's) -- position in this tuple IS recency (later =
# more recent). Session-tier levels carry no per-block timestamp usable for recency
# (unlike prevN_day/week's numeric N) -- (prev1)'s stored `_active_from` is the same
# constant for all four sub-blocks (see the `sess_now.index[0]` assignment above), so
# this fixed sequence is the only reliable ordering source.
_SESSION_RECENCY_ORDER = (
    ("asia", "prev1"), ("london", "prev1"), ("ny_morning", "prev1"), ("ny_evening", "prev1"),
    ("asia", "cur"), ("london", "cur"), ("ny_morning", "cur"), ("ny_evening", "cur"),
)
_SESSION_RECENCY_INDEX = {key: i for i, key in enumerate(_SESSION_RECENCY_ORDER)}


def _nested_session_levels(lv: dict) -> set:
    """thesis.md §2.1b extension (2026-08-01 root-cause audit): the SAME nesting/
    supersession concept as `_nested_prev_levels`, but for 6hr-SESSION-tier levels
    (asia/london/ny_morning/ny_evening, `(cur)`/`(prev1)` tags) instead of
    `prevN_day/week` families. These were previously invisible to nesting suppression
    entirely (`_nested_prev_levels`'s regex only matches `prevN_day/week` names) —
    confirmed via two real historical cases: 2026-07-15's `ny_evening(prev1)_high`
    nested under `asia(cur)_high`/`london(cur)_high`, and 2026-07-27's `asia(cur)_high`
    nested under `london(cur)_high` (which was also that day's running high). A level
    is nested if some level LATER in `_SESSION_RECENCY_ORDER` (same side) reaches at
    least as far — same "more-recent, at-least-as-deep supersedes" rule as the prevN
    version, just keyed on fixed session sequence instead of numeric N."""
    families: dict = {}   # "high"|"low" -> {(session, tag): price}
    for name, (price, _body, _side, tier, _active_from) in lv.items():
        if tier != "session":
            continue
        m = _SESSION_LEVEL_RE.match(name)
        if not m:
            continue
        sess, tag, sub = m.group(1), m.group(2), m.group(3)
        key = (sess, tag)
        if key not in _SESSION_RECENCY_INDEX:
            continue
        families.setdefault(sub, {})[key] = price
    nested = set()
    for sub, by_key in families.items():
        for key, price in by_key.items():
            idx = _SESSION_RECENCY_INDEX[key]
            more_recent = [p for k, p in by_key.items() if _SESSION_RECENCY_INDEX[k] > idx]
            if not more_recent:
                continue
            sess, tag = key
            if sub == "low":
                if any(p <= price for p in more_recent):
                    nested.add(f"{sess}({tag})_low")
            elif any(p >= price for p in more_recent):
                nested.add(f"{sess}({tag})_high")
    return nested


def _wide_day_week_smt_scan(levels: dict, wide_cache: dict) -> list:
    """thesis.md §2.1b/§7: re-scan day/week-tier shared levels for wick/body divergences over
    the WIDE (SMT_LOOKBACK_HOURS, cross-session) frame — the exact same detection shape as
    the S3 cross-ticker matrix loop, but over a frame that reaches back past the current
    session's own open. Session-scoped detection (first_cross(sess_cache[tkr].loc[af:], ...))
    can only ever find a touch within the CURRENT session; a level whose true divergence
    formed entirely in a PRIOR session (before its own name shifted, e.g. prev2_day_low ->
    prev3_day_low across a rollover) is either invisible (no re-touch this session) or
    misattributed to an unrelated later re-touch (the 2026-07-14 01:00 ET case).

    Deliberately SEPARATE from the S3 loop rather than widening it in place: S3's own
    rendered text and t1/t2 values are part of the shared, HASHED S0-S7 core also read by
    the live daily-trend.md/next-move.md KB — this function touches neither `bundle.lines`
    nor S3's own candidate list, it only supplies the REPLACEMENT day/week-tier candidates
    that `compute_facts` swaps in afterward (session-tier candidates from the original S3
    loop are left exactly as they were)."""
    out = []
    shared = sorted(set(levels.get("MNQ", {})) & set(levels.get("MES", {})))
    for name in shared:
        p1, b1, side, tier, _af1 = levels["MNQ"][name]
        p2, b2, _, _, _af2 = levels["MES"][name]
        if side is None or tier not in ("day", "week"):
            continue
        f1, f2 = wide_cache.get("MNQ"), wide_cache.get("MES")
        if f1 is None or f2 is None:
            continue
        t1, t2 = first_cross(f1, p1, side), first_cross(f2, p2, side)
        tb1, tb2 = first_body_cross_15m(f1, p1, side), first_body_cross_15m(f2, p2, side)
        if (t1 is None) != (t2 is None):
            swept_tkr, unswept_tkr = ("MNQ", "MES") if t1 is not None else ("MES", "MNQ")
            out.append({
                "level": name, "tier": tier, "side": side,
                "swept_ticker": swept_tkr, "unswept_ticker": unswept_tkr,
                "swept_at": t1 if t1 is not None else t2, "type": "wick",
                "meaningful": True,
            })
        if (tb1 is None) != (tb2 is None):
            swept_tkr_b, unswept_tkr_b = ("MNQ", "MES") if tb1 is not None else ("MES", "MNQ")
            out.append({
                "level": name, "tier": tier, "side": side,
                "swept_ticker": swept_tkr_b, "unswept_ticker": unswept_tkr_b,
                "swept_at": tb1 if tb1 is not None else tb2, "type": "body",
                "meaningful": True,
            })
    return out


def _p2_nesting_suppression(bundle: "FactsBundle") -> dict:
    """thesis.md §2.1b (2026-08-03: grandfather exception dropped). A P2/SMT candidate at a
    nested prevN_day/week level is suppressed exactly like a plain nested P1 level, with no
    exception for a divergence that fired before its level became nested. Nesting is a
    STATIC, price-only fact about the CURRENT level table (`_nested_prev_levels`) -- it does
    not depend on when the candidate's own divergence happened to fire.

    Rationale (2026-07-13/14 case): dropping a name's grandfather status does not drop the
    underlying signal -- the more extreme, un-nested member of the same family (e.g.
    prev1_day_low) is BY CONSTRUCTION never nested (nothing more recent exists to nest it),
    so once price actually sweeps that frontier level, it fires its OWN, independent,
    un-suppressed candidate. A touch that only reaches a nested (shallower) level without
    also reaching the frontier is deliberately treated as not-yet-meaningful, same as any
    other nested P1 read.

    Annotates each bundle.smt_candidates entry in place with `p2_suppressed`. Returns
    {tkr: set(level names)} of the P2-suppressed sites, meant to be threaded into
    score_thesis_evidence alongside suppressed_p1_levels."""
    out = {"MNQ": set(), "MES": set()}
    for tkr in ("MNQ", "MES"):
        suppressed = (bundle.suppressed_p1_levels or {}).get(tkr, set())
        for cand in bundle.smt_candidates:
            if cand.get("swept_ticker") != tkr:
                continue
            name, tier = cand.get("level"), cand.get("tier")
            is_suppressed = bool(
                cand.get("meaningful") and tier in ("day", "week")
                and _PREV_LEVEL_RE.match(name or "") and name in suppressed)
            cand["p2_suppressed"] = is_suppressed
            if is_suppressed:
                out[tkr].add(name)
    return out


def _p1_equilibrium_staleness(bundle: "FactsBundle", data: dict) -> dict:
    """thesis.md §2.1c: a P1 item's own sweep can go stale even while still 'mature' if
    price has since traveled all the way to (and tested) the relevant equilibrium -- daily
    mid for a day-tier item, weekly mid for a week-tier item. That MORE RECENT behavior
    around equilibrium is more meaningful than the original sweep from hours earlier
    (motivating case: 2026-07-23 07:00 ET, MNQ prev1_day_low swept ~3h before the call;
    price fully round-tripped back to the daily mid since). 2026-08-15 (#6, from the
    2026-08-10 09:20 ET audit — MES prev1_day_high, accepted many hours earlier, tagged
    stale on both tf rows yet still the tally's largest item): promoted from a code
    SUGGESTION to a HARD P1 gate — bench/facts.py exports the true names as
    `p1_stale_levels` and score_thesis_evidence skips them in the P1 auto-injection and
    zeroes any declared P1 at them, same "zeroed, not scored" mechanic as the other
    suppression gates. Structure-based on purpose (not a raw age cutoff): the market
    re-based at equilibrium, so the old acceptance/rejection belongs to a previous swing.
    Returns {tkr: {name: bool}}."""
    out = {"MNQ": {}, "MES": {}}
    for tkr in ("MNQ", "MES"):
        swept_map = (bundle.swept_at or {}).get(tkr, {})
        for name, tup in (bundle.levels or {}).get(tkr, {}).items():
            tier = tup[3]
            swept_at = swept_map.get(name)
            if swept_at is None or tier not in ("day", "week"):
                continue
            if tier == "day":
                mid_hi, mid_lo = bundle.day_hi.get(tkr), bundle.day_lo.get(tkr)
            else:
                mid_hi, mid_lo = bundle.week_hi.get(tkr), bundle.week_lo.get(tkr)
            if mid_hi is None or mid_lo is None:
                continue
            mid = (mid_hi + mid_lo) / 2.0
            window = data[tkr].loc[swept_at:]
            if len(window) == 0:
                continue
            out[tkr][name] = bool(float(window["low"].min()) <= mid <= float(window["high"].max()))
    return out


def _duplicate_sweep_losers(lv: dict, swept_at: dict) -> set:
    """thesis.md §2.1d: when two or more named levels for the SAME asset share the
    identical sweep timestamp and side, they MAY be restatements of one physical price
    move — but a shared 1-second timestamp alone is not proof of that: a fast multi-level
    break can cross several genuinely DISTINCT prices within the same second, which is not
    a duplicate, just a quick market. Collapsing purely on timestamp match (any price)
    wrongly killed `prev1_day_high` (2026-07-10 12:00 ET MES case — never nested by
    definition, dropped only because it shared a tick with the unrelated `prev1_week_high`
    one point away) and, separately, `prev1_day_low` (2026-07-16 09:00 ET MNQ case).

    Only collapse a group when EITHER:
    - the shared timestamp IS the session's own opening bar (`lv["TDO"]`'s active_from) —
      the legitimate "already breached before this session's visible history began" gap-
      cascade case, where the price spread among the crossed levels is irrelevant; or
    - the colliding items share the EXACT same price — a true structural duplicate (e.g. a
      prior day's own NY-evening sub-block low IS that day's day-low).
    A same-second collision that is neither is left alone; each level is scored on its own.

    Keeps only the highest-tier-weighted representative within a collapsing group (week >
    day > session; ties broken by the more extreme price) and suppresses the rest from
    fresh P1 evidence."""
    session_open = (lv.get("TDO") or (None,) * 5)[4]
    groups: dict = {}
    for name, (price, _body, side, tier, _active_from) in lv.items():
        t = swept_at.get(name)
        if side is None or t is None:
            continue
        groups.setdefault((side, t), []).append((name, price, tier))
    losers = set()

    def _winner(items):
        return max(
            items,
            key=lambda it: (_TIER_RANK.get(it[2], 0), -it[1] if side == "below" else it[1]))

    for (side, t), items in groups.items():
        if len(items) < 2:
            continue
        if t == session_open:
            losers.update(it[0] for it in items if it is not _winner(items))
            continue
        by_price: dict = {}
        for it in items:
            by_price.setdefault(it[1], []).append(it)
        for _price, same_price_items in by_price.items():
            if len(same_price_items) < 2:
                continue
            losers.update(it[0] for it in same_price_items if it is not _winner(same_price_items))
    return losers


def _extremity_shadowed_levels(lv: dict, swept_at: dict) -> set:
    """thesis.md §2.1b extension (2026-08-15, from the 2026-08-10 09:20 ET audit): among
    ONE asset's SWEPT same-side levels — ANY tier, ANY family — only the MOST EXTREME one
    is fresh P1 evidence; every shallower swept same-side level is suppressed. This is §6's
    own "only the most extreme swept level should be treated" principle applied to P1
    tallying: a deeper sweep necessarily passed through every shallower same-side level on
    the way, so their accept/reject reads are echoes of the same displacement, not
    independent events (2026-08-10: MNQ's london(cur)_low read stacked 1.0 pt on top of the
    deeper asia(cur)_low swept in the same decline). STRICT by user decision: applies even
    while the deeper level's own HTF read is still immature — the framework prefers waiting
    over scoring a shadowed shallower read. Winner ties (identical price under two names —
    the §2.1d exact-duplicate shape) keep the higher-tier representative, mirroring
    `_duplicate_sweep_losers._winner`."""
    groups: dict = {}   # "above"|"below" -> [(name, price, tier)]
    for name, (price, _body, side, tier, _af) in lv.items():
        if side not in ("above", "below") or swept_at.get(name) is None:
            continue
        groups.setdefault(side, []).append((name, price, tier))
    out = set()
    for side, items in groups.items():
        if len(items) < 2:
            continue
        winner = max(items, key=lambda it: (
            -it[1] if side == "below" else it[1], _TIER_RANK.get(it[2], 0)))
        out.update(it[0] for it in items if it is not winner)
    return out


def _promoted_session_extremes(data: dict, levels: dict, swept_at: dict,
                               now, wk_anchor) -> dict:
    """thesis.md §2.1 P2's meaningful-tier list includes RUNNING day/week extremes — but a
    running extreme that formed inside a 6hr sub-session is only ever named as that
    session's own extreme (asia(cur)_low, ...), so it was invisible to the day/week tier
    checks (2026-08-10 09:20 ET: MNQ's asia(cur)_low WAS the running day low when swept at
    08:36 ET, and the bullish SMT there was tagged meaningful=False as mere session tier).

    A session-tier level qualifies for promotion when it was the running day/week extreme
    at its REFERENCE TIME — sweep time for a swept level (nothing more extreme existed
    before the sweep), `now` for an unswept one (nothing more extreme exists yet). Week is
    checked before day (a running week extreme is by construction also the running day
    extreme). Returns {tkr: {name: "day"|"week"}}.

    By user decision (2026-08-15) promotion applies to BOTH P1 tier weight (via the
    bench/facts.py level_tiers override) and P2 meaningful-tier eligibility (via the SMT-
    candidate promotion in compute_facts). The level's NAME and the hashed S0-S7 text are
    untouched — promotion is an additive map plus an S9 tag."""
    out = {"MNQ": {}, "MES": {}}
    day_start = _day_start_ts(now)
    # Early-week guard: when the week window does not reach back before the day window
    # (Monday's own session — the engine week anchor IS the session open), every running
    # day extreme is trivially also the "week" extreme; promoting those to week tier would
    # overweight what is really a day-scale event. Week promotion requires the week window
    # to genuinely predate the day window; otherwise day is the ceiling.
    week_eligible = wk_anchor is not None and wk_anchor < day_start

    def _running_extreme_before(cm: pd.Series, t) -> Optional[float]:
        """Value of a cummin/cummax series just BEFORE `t` (the sweep bar itself is
        excluded), or its final value when t is None (unswept -> judged at now)."""
        if len(cm) == 0:
            return None
        if t is None:
            return float(cm.iloc[-1])
        i = cm.index.searchsorted(t)          # first position at/after t
        return float(cm.iloc[i - 1]) if i > 0 else None

    for tkr, df in data.items():
        cm_cache: dict = {}                    # anchor -> (cummin(low), cummax(high))
        for name, (price, _body, side, tier, _af) in levels.get(tkr, {}).items():
            if tier != "session" or side not in ("above", "below"):
                continue
            t = swept_at.get(tkr, {}).get(name)
            for anchor, ptier in ((wk_anchor, "week"), (day_start, "day")):
                if anchor is None or (ptier == "week" and not week_eligible):
                    continue
                if anchor not in cm_cache:
                    frame = df.loc[anchor:now]
                    cm_cache[anchor] = (frame["low"].cummin(), frame["high"].cummax())
                lo_cm, hi_cm = cm_cache[anchor]
                if side == "below":
                    ext = _running_extreme_before(lo_cm, t)
                    is_extreme = ext is not None and ext >= price - 1e-9
                else:
                    ext = _running_extreme_before(hi_cm, t)
                    is_extreme = ext is not None and ext <= price + 1e-9
                if is_extreme:
                    out[tkr][name] = ptier
                    break
    return out


def fvgs(bars):
    out = []
    for i in range(1, len(bars) - 1):
        ph, pl = bars["high"].iloc[i - 1], bars["low"].iloc[i - 1]
        nh, nl = bars["high"].iloc[i + 1], bars["low"].iloc[i + 1]
        if nl > ph:
            out.append((bars.index[i], "bull", float(ph), float(nl), bars.index[i + 1]))
        elif nh < pl:
            out.append((bars.index[i], "bear", float(nh), float(pl), bars.index[i + 1]))
    return out


def _long_horizon_extremes(hist_df: pd.DataFrame, now: pd.Timestamp) -> dict:
    """Aggregate the full-history 1s frame into per-True-Day and per-trade-week extreme
    rows, truncated at `now` (no lookahead — identical `<= now` slice to the S5b block).

    Returns {"daily": [row, ...], "weekly": [row, ...]} where each daily row is a dict
    {trade_date, open, high, low, close, close_high, close_low} and each weekly row is
    {week_start, open, high, low, close, close_high, close_low}. `close_high`/`close_low`
    are the max/min CLOSE (body extreme) over the period via hl() — the S5b render table
    itself does not need them, but the prev3-7_day / prev2-3_week named levels DO (they
    carry the same (price, body_price, side, tier, active_from) tuple shape prev1/prev2_day
    already use). Computed ONCE per ticker and reused for both the S5b render table and the
    new named levels, so the long-horizon frame is scanned only once (no naive re-scan of
    the primary frame — plan 15 Task 1 / thesis.md §2.1b)."""
    hf = hist_df[hist_df.index <= now]
    htd = pd.Series((hf.index + pd.Timedelta(hours=7)).date, index=hf.index)
    counts = htd.value_counts()
    days = sorted(d for d, n in counts.items() if d.weekday() < 5 and n >= 1000)
    daily = []
    for d in days[-60:]:                                   # same cap as the S5b render table
        s = hf[htd.values == d]
        h, l, ch, cl = hl(s)
        daily.append({
            "trade_date": d, "open": float(s["open"].iloc[0]),
            "high": h, "low": l, "close": float(s["close"].iloc[-1]),
            "close_high": ch, "close_low": cl,
        })
    weeks: dict = {}
    for r in daily:
        weeks.setdefault(r["trade_date"].isocalendar()[:2], []).append(r)
    weekly = []
    for _iso, rs in weeks.items():                         # dict preserves oldest-first order
        weekly.append({
            "week_start": rs[0]["trade_date"], "open": rs[0]["open"],
            "high": max(r["high"] for r in rs), "low": min(r["low"] for r in rs),
            "close": rs[-1]["close"],
            "close_high": max(r["close_high"] for r in rs),
            "close_low": min(r["close_low"] for r in rs),
        })
    return {"daily": daily, "weekly": weekly}


def mid_cross_table(closes_1m, mid_1m, cap=24):
    j = pd.DataFrame({"close": closes_1m, "mid": mid_1m}).dropna()
    j["above"] = j["close"] > j["mid"]
    flips = j[j["above"] != j["above"].shift()][1:]
    return j, flips.tail(cap)


def compute_two(df, week_tds, now):
    """TWO with daily.py's fallback chain: Monday 18:00 -> (if before it) prev Monday
    18:00 -> Monday 00:00 -> first bar of the week."""
    if not week_tds:
        return None, None
    monday = min(week_tds)
    mon18 = pd.Timestamp(monday, tz=TZ) + pd.Timedelta(hours=18)
    bars = df.loc[mon18:]
    if len(bars) and bars.index[0] - mon18 < pd.Timedelta(hours=1):
        return float(bars["open"].iloc[0]), bars.index[0]
    if now < mon18:
        prev18 = mon18 - pd.Timedelta(days=7)
        bars = df.loc[prev18:]
        if len(bars) and bars.index[0] - prev18 < pd.Timedelta(hours=1):
            return float(bars["open"].iloc[0]), bars.index[0]
    mon00 = pd.Timestamp(monday, tz=TZ)
    bars = df.loc[mon00:]
    if len(bars) and bars.index[0] - mon00 < pd.Timedelta(hours=1):
        return float(bars["open"].iloc[0]), bars.index[0]
    wk = pd.concat([session_frame(df, d) for d in week_tds])
    return (float(wk["open"].iloc[0]), wk.index[0]) if len(wk) else (None, None)


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)


@dataclass
class FactsBundle:
    """Structured result of compute_facts: the rendered fact-sheet lines plus the
    load-bearing structured values (used by the online adapter, records, and the
    validator view). `lines` reproduces the exact stdout the print-script emitted."""

    lines: list = field(default_factory=list)
    now: Optional[pd.Timestamp] = None
    td_now: Optional[datetime.date] = None
    ckpt: Optional[pd.Timestamp] = None
    # levels[tkr][name] = (price, body_price, side, tier, active_from) — the S1 map.
    levels: dict = field(default_factory=dict)
    now_price: Optional[float] = None   # MNQ last close (the decision ticker's price)
    day_mid: Optional[float] = None     # MNQ running day mid at now (S8 menu input)
    weekly_mid: Optional[float] = None  # MNQ running week mid at now (thesis.md P3/P4 input)
    swept_at: dict = field(default_factory=dict)  # swept_at[tkr][name] = ts | None
    htf_close_status: dict = field(default_factory=dict)  # [tkr][name] = {"1h":.., "4h":..}
    smt_candidates: list = field(default_factory=list)     # cross-ticker divergence candidates
    # --- plan 14: ATR-like primitives + per-asset day extremes (Task 1) ---
    avg_range_1h: dict = field(default_factory=dict)   # {tkr: mean last-20 completed 1h TR} v1 seed
    avg_range_4h: dict = field(default_factory=dict)   # {tkr: mean last-10 completed 4h TR} v1 seed
    # {tkr: running day high/low} — EXTENDED window (_day_start_ts, hypothesis.py parity),
    # NOT the narrow current-session window the S1 "day running" text line uses (thesis.md
    # stretch/§2.1c, S8 daily_mid).
    day_hi: dict = field(default_factory=dict)
    day_lo: dict = field(default_factory=dict)
    # {tkr: running week high/low} — per-asset (unlike weekly_mid, which is MNQ-only), the
    # engine week-anchor window (week_start_ts). Used by the P1 equilibrium-staleness decay
    # (thesis.md §2.1c) to read EACH asset's own weekly mid, not just MNQ's.
    week_hi: dict = field(default_factory=dict)
    week_lo: dict = field(default_factory=dict)
    # --- plan 14: session-maturity soft prior (Task 3) ---
    session_elapsed_frac: Optional[float] = None       # fraction of current session elapsed at now
    mature_evidence_count: int = 0                     # count of P1/P2-eligible items right now
    # --- plan 14: cross-family confluence source rows (Task 8, audit-only) ---
    historical_extremes: dict = field(default_factory=dict)  # {tkr:{"daily":[...],"weekly":[...]}}
    # --- plan 15 Task 4: FVG zones as structured P5-fill evidence candidates ---
    fvg_zones: list = field(default_factory=list)      # [{id, asset, tf, ts, kind, lo, hi, visited, visited_at, fill_verdict}]
    # S8 menus (plan 11): computed lazily by facts_to_validator_dict / render_menus_text.
    menus: Optional[dict] = None
    # thesis.md §2.1b/§2.1d refinement: {tkr: set(level names)} excluded from FRESH P1
    # accept/reject evidence (nested prevN levels + duplicate-simultaneous-sweep losers).
    # Never applied to P2/SMT candidacy — see _nested_prev_levels/_duplicate_sweep_losers.
    suppressed_p1_levels: dict = field(default_factory=dict)
    # thesis.md §2.1b: {tkr: set(level names)} of SMT/P2 candidates whose level is nested —
    # see _p2_nesting_suppression. Threaded into score_thesis_evidence alongside
    # suppressed_p1_levels, but ONLY zeroes P2 items (never P1, which suppressed_p1_levels
    # already covers).
    suppressed_p2_sites: dict = field(default_factory=dict)
    # thesis.md §2.1c: {tkr: {name: bool}} — a mature P1 item whose sweep has since been
    # followed by price reaching the relevant (day/week) equilibrium. 2026-08-15 (#6, from
    # the 2026-08-10 09:20 ET audit): promoted from a code-SUGGESTED tag to a HARD gate —
    # bench/facts.py exports it as `p1_stale_levels` and score_thesis_evidence zeroes/skips
    # stale P1 items outright (the model override no longer applies to P1 staleness).
    p1_equilibrium_stale: dict = field(default_factory=dict)
    # thesis.md §2.1 (2026-08-15): {tkr: {name: "day"|"week"}} — session-tier levels that
    # ARE the running day/week extreme, tier-promoted for P1 weight + P2 eligibility.
    # See _promoted_session_extremes.
    promoted_session_levels: dict = field(default_factory=dict)
    # thesis.md §2.1 P3 (2026-08-15, #3): {tkr: {"daily_mid"|"weekly_mid": {"price",
    # "side" ("above"|"below"), "dist_pts", "dist_ratio"}}} — UNCONDITIONAL per-asset
    # position vs each mid, computed even when the mid has never been crossed (the case
    # the close-status machinery skips entirely). Rendered in S9 and used by
    # score_thesis_evidence to inject a position-only P3 when no HTF crossing exists.
    mid_position: dict = field(default_factory=dict)
    # thesis.md §2.1 P2 Stage 2 input (2026-08-15): {tkr: {level_or_mid_name: ratio}} —
    # |now_price − level| / avg_range_1h for every level/mid that has an htf_reversal
    # entry. Used by score_thesis_evidence's EXPERIMENTAL distance-safe discount-fire
    # rung (p2_discount_fire_ratio); inert in production (the knob defaults to None).
    recross_distance: dict = field(default_factory=dict)
    # thesis.md §3a: near-maturity pre-confirmation candidates (bounded exception to §3) —
    # see _near_maturity_candidates for the shape of each entry.
    near_maturity_candidates: list = field(default_factory=list)
    # thesis.md §10 (2026-08-05): {tkr: timestamp} of the SAME extended-window frames
    # day_hi/day_lo/week_hi/week_lo are computed from — needed by the P3-vs-P4 mid
    # auto-derivation (_mid_tf_state) to tell whether a mid's crossing predates or
    # postdates the tier's own most-recent extreme.
    day_hi_ts: dict = field(default_factory=dict)
    day_lo_ts: dict = field(default_factory=dict)
    week_hi_ts: dict = field(default_factory=dict)
    week_lo_ts: dict = field(default_factory=dict)
    # thesis.md §10 (2026-08-05): {tkr: {"daily_mid"|"weekly_mid": price}} — the synthetic
    # mid's own price, needed to compute its clearance magnitude (build_evidence_magnitude)
    # the same way a named level's price already is.
    mid_price: dict = field(default_factory=dict)
    # thesis.md §10 (2026-08-05): {tkr: {"daily_mid"|"weekly_mid": {"1h"|"4h": {"fresh":
    # bool, "cross_dir": "up"|"down"}}}} — see _mid_tf_state. PER TF (a mid's 1h and 4h
    # crossings can differ). "fresh" = the mid's crossing on THIS tf postdates (or ties)
    # the tier's own most-recent extreme on the tested side, i.e. nothing MORE extreme has
    # happened since — a live, still-relevant reclaim test, P4-eligible. "stale" (fresh=
    # False) = a later, more extreme move has already superseded the crossing — old news,
    # plain P3 position only.
    mid_reclaim: dict = field(default_factory=dict)
    # thesis.md §10 (2026-08-05): {tkr: {level_or_mid_name: {"1h"|"4h": "none"|"discount"|
    # "omit"|"reverse"}}} — see _htf_reversal_tier. Whether the CURRENTLY-FORMING next-tf
    # bar, as of the call, has already undermined the just-completed bar's own accept/
    # reject verdict at this level. Applies to P1 (named levels) and P3/P4 (mids) alike —
    # mid entries are additionally registered under both P4-promoted names (`{mid}_high`/
    # `{mid}_low`), same pattern as mid_price/build_evidence_magnitude.
    htf_reversal: dict = field(default_factory=dict)


def render_facts_text(bundle: FactsBundle) -> str:
    """Reproduce the exact stdout the print-script produced (byte-for-byte).

    Each captured line corresponds to one `print(...)` call; joining with a
    newline and a trailing newline reconstructs stdout exactly.
    """
    return "\n".join(bundle.lines) + "\n"


def _parse_facts():
    """Lazily import calibration.validate_results.parse_facts (the text→dict
    reverse the semantic validator consumes) without a hard module dependency."""
    calib = os.path.join(REPO_ROOT, "calibration")
    if calib not in sys.path:
        sys.path.insert(0, calib)
    from validate_results import parse_facts  # noqa: E402
    return parse_facts


def facts_to_validator_dict(bundle: FactsBundle) -> dict:
    """The JSON view the semantic validator (validator._check_semantic) + the audit
    consume: {now_price, checkpoint, whipsaw, levels{name:{price, side, swept,
    depleted}}}.

    Implemented as parse_facts(render_facts_text(bundle)) ON PURPOSE — "one source,
    two views" (prod-agent.md). Deriving the validator view from the SAME rendered
    text the offline calibration bench parses is what guarantees the online semantic
    layer behaves identically to the bench (HOLE H4); it also makes the Wave-1.1
    validator-dict-parity test true by construction. tier is intentionally omitted
    here (the validator never reads it — richer per-level data lives in
    bundle.levels for the records/audit layer).

    NOTE (plan 11): the S8 `menus` block is NOT injected here — this dict is hashed as
    the shadow engine's facts identity (facts_adapter._canonical_hash), so it must stay
    byte-stable. Menus are an additive, bench/L1-only overlay: build them with
    `build_menus(bundle, facts_to_validator_dict(bundle))` and attach under a "menus"
    key on a COPY (the bench does this; render_menus_text caches it on the bundle).
    """
    return _parse_facts()(render_facts_text(bundle))


# --------------------------------------------------------------------------- #
# S8 menus (plan 11): DOL menu + predicate menu, config-driven                 #
# --------------------------------------------------------------------------- #
_EPS = 1e-6

# plan 12 Fix 2 — minimum distance (pts) a DOL pool must sit beyond current price to be offered
# as a draw. A pool nearer than this is one that price is effectively already sitting on: there
# is no forward draw to it, and offering it is race-prone — price can cross the level within the
# call-latency window before the thesis stands, satisfying `price_beyond(DOL)` at arrival for a
# bogus minutes-long "completion" (07-02 th_02: DOL was 0.75 pts away). Deliberately conservative
# (catches the already-at-the-pool case without over-excluding legitimate near-term targets); the
# validator's SEM_DOL_WRONG_SIDE check + the scoring suspect_completion flag are the
# belt-and-suspenders for the residual latency race.
DOL_MIN_DRAW_DISTANCE_PTS = 5.0

# 2026-08-16 DOL-menu refit (from the 08-10..08-14 09:20 forward tests). All ratios are
# multiples of MNQ's avg_range_1h (the same v1-seed ATR everything else uses):
# - DOL_MIN_DRAW_RATIO: the REAL draw floor — a pool nearer than this completes on noise
#   (08-12: 15pts/0.19x touched in 7min; 08-13: 10pts/0.13x "exhausted" a call that then
#   ran +350). Raised 0.5 -> 1.0 (2026-08-16, plan 18) on l2-mechanisms.md §10.3 v2
#   (commit 272cf7c: 73 days 05-04..08-14, 132 named-pool arms, pre/post-rollover split):
#   both segments agree 0.5-0.7x is unusable (60-75% swept pre-09:35) and >= 1.0x is safe
#   (<= 11%); the 0.7-1.0x band is regime-ambiguous (40% swept post-rollover) — 1.0x is
#   chosen on post-rollover (live-regime) weighting. The hold-for-D2 counterfactual on
#   the 26 swept near arms shows the deeper target is positive-EV in its own right
#   (D2 reached 73% by 16:00, median +72.6 pts vs TP-at-near-D1).
#   DOL_MIN_DRAW_DISTANCE_PTS above stays as the absolute latency-race guard
#   (plan 12 Fix 2) AND as the fallback floor when avg_range_1h is unavailable (fixtures/
#   standalone callers) — the two constants protect different things; don't merge them.
# - DOL_BAND_MAX_RATIO: entries beyond this are tagged FAR (rendered + audit-warned for a
#   RANGE regime — 08-10's 4.8x-away day-low draw), NOT excluded: a far pool is sometimes
#   the honest answer, and hard exclusion would silently rewrite no-liquidity semantics.
# - DOL_PROJECTION_RATIO: price-discovery synthetic draw — when a direction has NO named
#   pool inside the band, offer `projection_up`/`projection_down` at the running day
#   extreme +/- this multiple (unswept by construction; gives L2 a real TP). Gated on the
#   §2.1c stretch flag: no fresh trend-extension target once price is already stretched
#   > DOL_BAND_MAX_RATIO from the opposite-side day extreme (the 08-13-right/08-14-wrong
#   separator). Canonical names mirrored in schemas.DOL_PROJECTION_LEVELS.
DOL_MIN_DRAW_RATIO = 1.0
DOL_BAND_MAX_RATIO = 3.0
DOL_PROJECTION_RATIO = 1.0
# Direction-aware weekly-extension gate on projections: no projection in direction X when
# price already sits >= this many avg_1h beyond the WEEKLY mid TOWARD X — the day-window
# stretch gate alone misses a multi-day extension (08-14: Thursday's +350 rip was invisible
# to Friday's day window, day-stretch read 1.8x, and a projection_up re-armed a wrong UP
# against a correct no-liquidity NEUTRAL; MNQ sat 6.63x above its weekly mid). Benign
# projections observed at <= 3.35x (08-10 projection_down fired AGAINST the weekly
# extension at +2.7x above the mid — allowed by direction-awareness). v1 seed 4.0.
DOL_PROJECTION_WEEKLY_STRETCH_MAX = 4.0

# Predicate-menu generation config: families × level-classes × param variants. Adding a
# family / level-class / variant here changes the menu WITHOUT touching the generator, and
# nothing here names a specific level — level names are resolved from the facts at build
# time (the "config-driven, not hardcoded per level name" requirement).
_MENU_PREDICATE_CFG = (
    # (family, id_prefix, level_class, builder, variants, max_levels)
    ("falsification", "F", "daily_mid", "n_closes_beyond",
     ({"tf": "5m", "n": 2},), None),
    ("falsification", "F", "anti_pools", "price_beyond", ({},), 3),
    ("exhaustion", "X", "dol_pools", "price_beyond", ({},), None),
    ("recall", "R", "daily_mid", "n_closes_beyond", ({"tf": "5m", "n": 3},), None),
    ("recall", "R", "clock", "time_elapsed", ({"minutes": 60}, {"minutes": 120}), None),
    # --- decisions/thesis.md P1/P3/P4 additions (plan 13) ---
    ("falsification", "F", "daily_mid", "n_closes_beyond",
     ({"tf": "1h", "n": 1}, {"tf": "4h", "n": 1}), None),
    ("falsification", "F", "weekly_mid", "n_closes_beyond",
     ({"tf": "5m", "n": 2}, {"tf": "1h", "n": 1}, {"tf": "4h", "n": 1}), None),
    ("recall", "R", "weekly_mid", "n_closes_beyond", ({"tf": "5m", "n": 3},), None),
    ("evidence", "E", "swept_levels", "n_closes_beyond",
     ({"tf": "1h", "n": 1}, {"tf": "4h", "n": 1}), None),
    ("evidence", "E", "meaningful_smt_pools", "n_closes_beyond",
     ({"tf": "1h", "n": 1}, {"tf": "4h", "n": 1}), None),
    # --- plan 14: session-anchored recall (thesis.md §1 cadence) ---
    ("recall", "R", "next_subsession", "clock_after", ({},), None),
)


def _anti_side(direction: str) -> str:
    return "below" if direction == "UP" else "above"


def _thesis_side(direction: str) -> str:
    return "above" if direction == "UP" else "below"


def _dol_menu(mnq_levels: dict, vlevels: dict, now_price: float, suppressed=None, *,
              avg_range_1h=None, day_hi=None, day_lo=None, stretch_mult=None,
              weekly_mid=None) -> dict:
    """Eligible target pools per direction: in-facts, unswept AND undepleted, on the
    correct side of current price, AND at least DOL_MIN_DRAW_DISTANCE_PTS away. UP draws sit
    above price (nearest first); DOWN below.

    Proximity guard (plan 12 Fix 2): a pool nearer than DOL_MIN_DRAW_DISTANCE_PTS to price is
    EXCLUDED — price is already sitting on it, so there is no forward draw, and offering it is
    race-prone: between the facts snapshot and the thesis's arrival (born + call latency) price
    can cross the level, so the exhaustion (`price_beyond(DOL)`) is already satisfied at arrival
    and the thesis "completes" in minutes on a level it never actually drew to. This was the
    07-02 th_02 bug: DOL prev1_day_low was 0.75 pts below price at facts build; price crossed it
    during the latency window → bogus 2-minute completion.

    Nesting/duplicate guard (thesis.md §2.1b/§2.1d): `suppressed` is the caller's
    `bundle.suppressed_p1_levels["MNQ"]` set — a level already excluded from fresh P1 scoring
    because a more-recent same-family level supersedes it (or it's a duplicate-simultaneous-sweep
    loser) is equally not an independent draw target, so it's excluded here too. Without this, a
    nested level could still surface as the NEAREST (and therefore selected) DOL menu entry even
    though it can never be cited as P1 evidence — the 2026-07-13 18:00 ET case: `prev7_day_low`
    was nested under `prev3_day_low` (both unswept, but prev3 sits farther/deeper), yet it was the
    #1 DOWN entry and got selected as the thesis's DOL.

    2026-08-16 DOL-menu refit (see the constants' own comment block): `avg_range_1h`/
    `day_hi`/`day_lo`/`stretch_mult` (all MNQ, all optional — absent leaves this function
    byte-identical to before) enable (1) an ATR-scaled draw floor
    max(5pts, DOL_MIN_DRAW_RATIO x avg_1h),
    (2) per-entry `dist_ratio` + BAND/FAR tags, and (3) the stretch-gated
    `projection_up`/`projection_down` synthetic price-discovery draw appended when a
    direction has no named pool inside the band."""
    out = {"UP": [], "DOWN": []}
    if not isinstance(now_price, (int, float)):
        return out
    ar = avg_range_1h if isinstance(avg_range_1h, (int, float)) and avg_range_1h > 0 else None
    floor = (DOL_MIN_DRAW_DISTANCE_PTS if ar is None
             else max(DOL_MIN_DRAW_DISTANCE_PTS, DOL_MIN_DRAW_RATIO * ar))
    suppressed = suppressed or ()
    for name, tup in mnq_levels.items():
        if name in suppressed:
            continue
        price, body, side, tier, _active = tup
        if side not in ("above", "below") or not isinstance(price, (int, float)):
            continue
        v = vlevels.get(name, {})
        if v.get("swept") or v.get("depleted"):
            continue
        # Too close to be a draw (price is already sitting on the pool, or the draw would
        # complete on ordinary hourly noise) → race-prone / instantly-spent.
        if abs(price - now_price) < floor:
            continue
        # A draw is a resistance ABOVE price (up) or a support BELOW price (down); a
        # level whose price sits on the wrong side of its own tag (a resistance now below
        # price, or vice-versa) is spent, not a draw — excluded.
        if side == "above" and price > now_price + _EPS:
            out["UP"].append((name, price, body, tier, side))
        elif side == "below" and price < now_price - _EPS:
            out["DOWN"].append((name, price, body, tier, side))
    # 2026-08-16: stretch-gated synthetic price-discovery draw. When a direction has NO
    # named pool inside the band (everything unswept is FAR, or nothing is left at all —
    # price above/below every named pool, the 08-13 +350-run / 08-14 no-liquidity pair),
    # offer a projection at the running day extreme +/- DOL_PROJECTION_RATIO x avg_1h —
    # deterministic, unswept by construction, a real L2 take-profit. Gated on stretch: a
    # market already > DOL_BAND_MAX_RATIO from its opposite-side day extreme gets no fresh
    # trend-extension target (08-14: stood NEUTRAL correctly and must keep doing so).
    if ar is not None and not (isinstance(stretch_mult, (int, float))
                               and stretch_mult > DOL_BAND_MAX_RATIO):
        # Direction-aware weekly-extension gate (see DOL_PROJECTION_WEEKLY_STRETCH_MAX):
        # signed distance of price from the weekly mid in avg-1h units — positive = above.
        wk_ext = (round((now_price - weekly_mid) / ar, 4)
                  if isinstance(weekly_mid, (int, float)) else None)
        for direction, extreme, pside, pname in (
                ("UP", day_hi, "above", "projection_up"),
                ("DOWN", day_lo, "below", "projection_down")):
            if not isinstance(extreme, (int, float)):
                continue
            if wk_ext is not None and (
                    (direction == "UP" and wk_ext >= DOL_PROJECTION_WEEKLY_STRETCH_MAX)
                    or (direction == "DOWN" and wk_ext <= -DOL_PROJECTION_WEEKLY_STRETCH_MAX)):
                continue          # already multi-day-extended toward this direction
            has_band_pool = any(
                abs(price - now_price) / ar <= DOL_BAND_MAX_RATIO
                for (_n, price, _b, _t, _s) in out[direction])
            if has_band_pool:
                continue
            proj = (extreme + DOL_PROJECTION_RATIO * ar if direction == "UP"
                    else extreme - DOL_PROJECTION_RATIO * ar)
            proj = round(proj * 4) / 4.0            # snap to the 0.25 tick grid
            out[direction].append((pname, proj, None, "projection", pside))

    # Nearest draw first (UP ascending, DOWN descending), then assign stable IDs.
    out["UP"].sort(key=lambda e: e[1])
    out["DOWN"].sort(key=lambda e: -e[1])
    menu = {}
    for direction, entries in out.items():
        rows = []
        for i, (name, price, body, tier, side) in enumerate(entries):
            ratio = round(abs(price - now_price) / ar, 4) if ar is not None else None
            if tier == "projection":
                band = "PROJECTION"
            elif ratio is None:
                band = None
            else:
                band = "BAND" if ratio <= DOL_BAND_MAX_RATIO else "FAR"
            rows.append({"id": f"D{i + 1}", "level": name, "price": price,
                         "body": body, "tier": tier, "side": side,
                         "dist_ratio": ratio, "band": band})
        menu[direction] = rows
    return menu


def _resolve_level_class(level_class: str, direction: str, mnq_levels: dict,
                         vlevels: dict, now_price: float, day_mid, dol_menu: dict,
                         max_levels, *, weekly_mid=None, mes_levels=None,
                         mnq_swept_at=None, mes_swept_at=None,
                         smt_candidates=None, now=None) -> list:
    """Resolve a level-class to a list of (price, side) the builder fills into predicates."""
    if level_class == "daily_mid":
        if not isinstance(day_mid, (int, float)):
            return []
        anti = _anti_side(direction)
        # A reclaim-the-mid falsifier/recall only makes sense as a FORWARD-looking check —
        # if price already sits beyond the mid on the anti-side, the thesis never held the
        # mid in the first place, so "n_closes_beyond(mid, anti_side)" is already true at
        # issuance (MarketView.price_only's flat-price view trivially satisfies it),
        # tripping XL_FALSIFIED_IF_ALREADY_TRUE / a dead-on-arrival recall. Don't offer a
        # degenerate candidate — bug found via repeated manual runs on 2026-07-14 01:00.
        if isinstance(now_price, (int, float)) and _beyond_side(now_price, day_mid, anti):
            return []
        return [(day_mid, anti)]
    if level_class == "next_subsession":
        # plan 14 Task 4: session-anchored recall at the next sub-session boundary. The
        # et_time string rides the price slot; direction-neutral (same for UP/DOWN). Inert
        # when `now` is absent (keeps old fixtures/standalone callers unchanged).
        if now is None:
            return []
        return [(_next_subsession_boundary(now), None)]
    if level_class == "weekly_mid":
        if not isinstance(weekly_mid, (int, float)):
            return []
        anti = _anti_side(direction)
        # Same already-beyond-at-issuance guard as daily_mid above.
        if isinstance(now_price, (int, float)) and _beyond_side(now_price, weekly_mid, anti):
            return []
        return [(weekly_mid, anti)]
    if level_class == "clock":
        return [(None, None)]
    if level_class == "dol_pools":
        side = _thesis_side(direction)
        return [(e["price"], side) for e in dol_menu.get(direction, [])]
    if level_class == "anti_pools":
        # Nearest unswept pools on the ANTI-thesis side of price (support below an UP
        # thesis / resistance above a DOWN thesis); breaking them falsifies the thesis.
        side = _anti_side(direction)
        pools = []
        for name, tup in mnq_levels.items():
            price, _body, lside, _tier, _active = tup
            if lside not in ("above", "below") or not isinstance(price, (int, float)):
                continue
            v = vlevels.get(name, {})
            if v.get("swept") or v.get("depleted"):
                continue
            # Anti-thesis pools are the support UNDER an up-thesis (side below, below
            # price) or the resistance OVER a down-thesis (side above, above price).
            if direction == "UP" and lside == "below" and price < now_price - _EPS:
                pools.append(price)
            elif direction == "DOWN" and lside == "above" and price > now_price + _EPS:
                pools.append(price)
        pools.sort(key=lambda p: abs(p - now_price))   # nearest first
        if max_levels:
            pools = pools[:max_levels]
        return [(p, side) for p in pools]
    if level_class == "swept_levels":
        # thesis.md P1: any level on EITHER ticker that has been SWEPT (a swept_at entry
        # exists, not merely a bundle.levels entry) — this does NOT require the maturity
        # gate (>=1 qualifying HTF close since the sweep) to have passed. Maturity gating
        # is enforced only in render_evidence_text (Task 6), which reads htf_close_status
        # to flag "immature"; htf_close_status is not consumed here or anywhere in the
        # predicate-menu code. This is an intentional scope boundary for this stage of the
        # work: the menu offers a forward-looking n_closes_beyond predicate for any swept
        # level, mature or not, and the model/validator judges maturity from S9 evidence
        # text separately. Direction-neutral: both UP and DOWN offer the same swept-level
        # pools (P1 scores whichever side the model's read matches; the level's own `side`
        # decides "beyond" vs "before").
        out = []
        for levels_, swept_map in ((mnq_levels, mnq_swept_at or {}),
                                   (mes_levels or {}, mes_swept_at or {})):
            for name, tup in levels_.items():
                price, _body, lside, _tier, _active = tup
                if lside not in ("above", "below") or not isinstance(price, (int, float)):
                    continue
                if swept_map.get(name) is None:
                    continue
                out.append((price, lside))
        return out
    if level_class == "meaningful_smt_pools":
        # thesis.md P2: week/day-tier SMT candidates only, tested on the SWEPT ticker's
        # own price at the ANTI-side (a close BEFORE the liquidity on the unswept/
        # leader's side is what P2 scores as a reversal tell).
        out = []
        levels_by_tkr = {"MNQ": mnq_levels, "MES": mes_levels or {}}
        for cand in smt_candidates or []:
            if not cand.get("meaningful"):
                continue
            tup = levels_by_tkr.get(cand["swept_ticker"], {}).get(cand["level"])
            if tup is None:
                continue
            price = tup[0]
            anti_side = "below" if cand["side"] == "above" else "above"
            out.append((price, anti_side))
        return out
    return []


def _next_subsession_boundary(now) -> str:
    """Next sub-session boundary time-of-day ('HH:MM' ET) STRICTLY after `now`, drawn from
    the session cadence {00:00, 06:00, 12:00, 18:00} (thesis.md §1 — the subsess cutoffs).
    Wraps past midnight to 00:00. Format is what clock_after evaluates (confirmed across
    midnight in the walk-forward evaluator)."""
    tod = (now.hour, now.minute)
    for b in (0, 6, 12, 18):
        if (b, 0) > tod:
            return f"{b:02d}:00"
    return "00:00"


def _build_predicate(builder: str, price, side, variant: dict) -> Optional[dict]:
    if builder == "price_beyond":
        return {"type": "price_beyond", "price": price, "side": side}
    if builder == "n_closes_beyond":
        return {"type": "n_closes_beyond", "price": price, "side": side,
                "tf": variant["tf"], "n": variant["n"]}
    if builder == "time_elapsed":
        return {"type": "time_elapsed", "minutes": variant["minutes"]}
    if builder == "clock_after":
        return {"type": "clock_after", "et_time": price}   # price slot carries the et_time str
    return None


def _predicate_menu(mnq_levels: dict, vlevels: dict, now_price: float, day_mid,
                    dol_menu: dict, *, weekly_mid=None, mes_levels=None,
                    mnq_swept_at=None, mes_swept_at=None, smt_candidates=None,
                    now=None) -> dict:
    """Per-direction candidate falsification / exhaustion / recall / evidence predicates
    with unique IDs and concrete params, generated from _MENU_PREDICATE_CFG (config-driven).
    `evidence` family (thesis.md P1/P2) is informational scoring input, like the S3b
    candidate cards — not meant to be copied verbatim into falsified_if/
    recall, though nothing prevents reusing the same predicate there via the escape hatch."""
    out = {}
    for direction in ("UP", "DOWN"):
        entries = []
        counters = {}
        seen = set()
        for family, prefix, level_class, builder, variants, max_levels in _MENU_PREDICATE_CFG:
            targets = _resolve_level_class(level_class, direction, mnq_levels, vlevels,
                                           now_price, day_mid, dol_menu, max_levels,
                                           weekly_mid=weekly_mid, mes_levels=mes_levels,
                                           mnq_swept_at=mnq_swept_at, mes_swept_at=mes_swept_at,
                                           smt_candidates=smt_candidates, now=now)
            for price, side in targets:
                for variant in variants:
                    pred = _build_predicate(builder, price, side, variant)
                    if pred is None:
                        continue
                    key = json.dumps(pred, sort_keys=True)
                    if key in seen:            # dedupe identical concrete predicates
                        continue
                    seen.add(key)
                    counters[prefix] = counters.get(prefix, 0) + 1
                    entries.append({"id": f"{prefix}{counters[prefix]}",
                                    "family": family, "predicate": pred})
        # Falsifier ranking (2026-08-17, from the 07-15 sampling-stability check): four
        # identical calls chose four falsifier designs whose exits spread 75 minutes /
        # ~150pts on the same losing day — the menu offered candidates unranked, so the
        # model re-decided each run. Mark the FIRST falsification entry as the
        # recommended default (mirrors the DOL menu's D1-nearest convention, which
        # models follow 12/13 times). Config order makes that the tight 2x5m daily-mid
        # close form when available (bench 07-02: tighter close-based falsifiers beat
        # distant structural ones by ~85pts), else the nearest anti-pool — the
        # degenerate-mid guard in _resolve_level_class supplies that fallback naturally.
        for e in entries:
            if e["family"] == "falsification":
                e["recommended"] = True
                break
        out[direction] = entries
    return out


def build_menus(bundle: FactsBundle, vd: dict) -> dict:
    """The S8 menu object: {now_price, dol{UP,DOWN}, predicates{UP,DOWN}} — the structured
    menu shared by the rendered facts text (render_menus_text) and the validator's
    menu-membership check. Deterministic given the same facts. DOL remains MNQ-only (MNQ
    is the decision/executed ticker) — only the PREDICATE menu gains MES/weekly_mid/
    swept-level/SMT-candidate families (decisions/thesis.md P1-P4 both-asset requirement)."""
    mnq_levels = (bundle.levels or {}).get("MNQ", {})
    mes_levels = (bundle.levels or {}).get("MES", {})
    vlevels = vd.get("levels", {}) if isinstance(vd, dict) else {}
    now_price = vd.get("now_price") if isinstance(vd, dict) else bundle.now_price
    if not isinstance(now_price, (int, float)):
        now_price = bundle.now_price
    mnq_suppressed = (bundle.suppressed_p1_levels or {}).get("MNQ", set())
    # 2026-08-16 DOL-menu refit inputs (all MNQ): ATR floor + band tags + the stretch-gated
    # projection draw. stretch_mult is the SAME §2.1c definition S9 renders — distance from
    # the opposite-side (farther) running day extreme in avg-1h-range units.
    _ar = (bundle.avg_range_1h or {}).get("MNQ")
    _dhi, _dlo = (bundle.day_hi or {}).get("MNQ"), (bundle.day_lo or {}).get("MNQ")
    _stretch = None
    if (isinstance(now_price, (int, float)) and isinstance(_ar, (int, float)) and _ar > 0
            and isinstance(_dhi, (int, float)) and isinstance(_dlo, (int, float))):
        _stretch = round(max(abs(now_price - _dhi), abs(now_price - _dlo)) / _ar, 4)
    dol = _dol_menu(mnq_levels, vlevels, now_price, suppressed=mnq_suppressed,
                    avg_range_1h=_ar, day_hi=_dhi, day_lo=_dlo, stretch_mult=_stretch,
                    weekly_mid=bundle.weekly_mid)
    mnq_swept_at = (bundle.swept_at or {}).get("MNQ", {})
    mes_swept_at = (bundle.swept_at or {}).get("MES", {})
    preds = _predicate_menu(mnq_levels, vlevels, now_price, bundle.day_mid, dol,
                           weekly_mid=bundle.weekly_mid, mes_levels=mes_levels,
                           mnq_swept_at=mnq_swept_at, mes_swept_at=mes_swept_at,
                           smt_candidates=bundle.smt_candidates, now=bundle.now)
    return {"now_price": now_price, "daily_mid": bundle.day_mid,
            "weekly_mid": bundle.weekly_mid,
            "dol": dol, "predicates": preds}


def render_menus_text(bundle: FactsBundle) -> str:
    """Render the S8 menu block (compact tables with IDs). Byte-stable given the same
    facts. Kept SEPARATE from render_facts_text so the S0–S7 core (and its content hash)
    is unchanged — this block is appended to the model-facing prompt, not to bundle.lines."""
    if bundle.menus is None:
        bundle.menus = build_menus(bundle, facts_to_validator_dict(bundle))
    m = bundle.menus or {}
    out: list = []
    A = out.append
    A("## S8 MENUS (candidate DOLs + predicates; select by copying params exactly; "
      "escape hatch = any schema-valid, facts-grounded predicate)")
    A(f"now_price = {m.get('now_price')} | daily_mid = {m.get('daily_mid')}")
    dol = m.get("dol") or {}
    for direction in ("UP", "DOWN"):
        A(f"\nDOL menu [{direction}] (eligible draws: in-facts, unswept, undepleted, "
          f"correct side, >= max(5pts, {DOL_MIN_DRAW_RATIO}x avg_1h) away; prefer a BAND "
          f"entry (<= {DOL_BAND_MAX_RATIO}x avg_1h) — FAR is legitimate only for a TREND "
          f"call with HTF confirmation; 'projection' = synthetic price-discovery draw at "
          f"the running day extreme +/- {DOL_PROJECTION_RATIO}x avg_1h, offered only when "
          f"no named pool sits in the band):")
        rows = dol.get(direction) or []
        if not rows:
            A("  (none eligible)")
        for e in rows:
            body = "" if e.get("body") is None else f" body={e['body']}"
            dist_tag = ""
            if e.get("dist_ratio") is not None:
                dist_tag = f" dist={e['dist_ratio']}x avg_1h ({e.get('band')})"
            A(f"  {e['id']}: {e['level']} price={e['price']}{body} "
              f"[{e['side']}, {e['tier']}]{dist_tag}")
    preds = m.get("predicates") or {}
    for direction in ("UP", "DOWN"):
        A(f"\nPredicate menu [{direction}] (falsification F / exhaustion X / recall R; "
          f"falsification entries are RANKED — take the one tagged RECOMMENDED unless "
          f"falsified_if_rationale states a concrete reason to prefer another):")
        rows = preds.get(direction) or []
        if not rows:
            A("  (none)")
        for e in rows:
            rec = "  <-- RECOMMENDED default falsifier" if e.get("recommended") else ""
            A(f"  {e['id']} [{e['family']}]: {_fmt_predicate(e['predicate'])}{rec}")
    return "\n".join(out) + "\n"


def _fmt_predicate(pred: dict) -> str:
    """Compact human form of a predicate for the menu table (params echo the JSON)."""
    t = pred.get("type")
    if t == "price_beyond":
        return f"price_beyond(price={pred['price']}, side={pred['side']})"
    if t == "n_closes_beyond":
        return (f"n_closes_beyond(price={pred['price']}, side={pred['side']}, "
                f"tf={pred['tf']}, n={pred['n']})")
    if t == "time_elapsed":
        return f"time_elapsed(minutes={pred['minutes']})"
    if t == "clock_after":
        return f"clock_after(et_time={pred['et_time']})"
    return json.dumps(pred, sort_keys=True)


def _confluence_notes(price: float, hist_for_tkr: dict, tol: float) -> list:
    """Audit-only (thesis.md §2.1e): notes when `price` sits within `tol` of an OLD,
    UNTRACKED historical day/week extreme. Skips the tracked window — the last 2 daily rows
    (prev1/prev2 day are tracked levels) and the last 1 weekly row (prev1 week) — so a level
    never trivially matches its own tracked extreme. The skip-window is a v1-seed heuristic;
    `tol` is ATR-relative (0.25 x avg_range_1h), also a v1 seed pending calibration. Pure and
    deterministic — returns a short note per match."""
    notes: list = []
    daily = (hist_for_tkr.get("daily") or [])[:-2]    # drop tracked prev1/prev2 day rows
    weekly = (hist_for_tkr.get("weekly") or [])[:-1]  # drop tracked prev1 week row
    for label, rows in (("daily", daily), ("weekly", weekly)):
        for row in rows:
            when, hi, lo = row[0], row[1], row[2]
            for kind, ext in (("high", hi), ("low", lo)):
                if isinstance(ext, (int, float)) and abs(price - ext) <= tol:
                    notes.append(
                        f"coincides with untracked {label} {kind} {ext} from {when} "
                        f"(dist {abs(price - ext):.2f} <= tol {tol:.2f} = 0.25x avg_1h)")
    return notes


# (ratio_upper, label) — MUST match validate_contracts._MAG_BUCKETS' own thresholds.
# Duplicated here (not imported) because derive_facts is a pure facts module with no
# decision-layer dependency (module docstring: "makes NO decisions"). Update both if the
# scoring buckets ever change.
_MAG_LABEL_BUCKETS = ((0.5, "WEAK"), (1.5, "NORMAL"), (float("inf"), "STRONG"))


def _magnitude_label(ratio) -> Optional[str]:
    if ratio is None:
        return None
    for upper, label in _MAG_LABEL_BUCKETS:
        if ratio < upper:
            return label
    return None


# thesis.md §3a — v1 seeds, pending calibration (same status as every other threshold in
# this module). NEAR_MATURITY_DISTANCE_RATIO reuses the "STRONG" clearance bucket's own bar
# (_MAG_LABEL_BUCKETS[1][0] == 1.5) rather than a new flat-point constant — MNQ/MES trade at
# very different absolute point scales, so an ATR-normalized bar is the only one that means
# the same thing on both assets (same reasoning as _MAG_LABEL_BUCKETS itself).
NEAR_MATURITY_WINDOW_MIN = 10
NEAR_MATURITY_DISTANCE_RATIO = _MAG_LABEL_BUCKETS[1][0]


def _level_polarity_hl(name) -> Optional[str]:
    """'high' | 'low' | None from the level NAME's naming convention — mirrors
    validate_contracts._level_polarity. Duplicated locally, not imported: derive_facts is a
    pure facts module with no decision-layer dependency (see _MAG_LABEL_BUCKETS above for the
    same precedent). Update both if the naming convention ever changes."""
    lname = (name or "").lower()
    if "high" in lname:
        return "high"
    if "low" in lname:
        return "low"
    return None


def _implied_side(polarity: Optional[str], accept: Optional[bool]) -> Optional[str]:
    """UP/DOWN implied by a level's high/low polarity + accept(-beyond)/reject(-before) read
    — the same accept<->sign mapping as validate_contracts._evidence_side, duplicated locally
    for the reason above."""
    if polarity is None or accept is None:
        return None
    if polarity == "high":
        return "UP" if accept else "DOWN"
    return "DOWN" if accept else "UP"


def _near_maturity_candidates(bundle: "FactsBundle", data: dict, now) -> list:
    """thesis.md §3a — a BOUNDED, explicitly-scoped exception to the §3 maturity gate. For
    every swept day/week-tier level still awaiting a qualifying HTF (1h or 4h) close within
    NEAR_MATURITY_WINDOW_MIN minutes of `now`, compute whether the CURRENT (pre-close) price
    already clears NEAR_MATURITY_DISTANCE_RATIO in the level's own polarity direction, AND
    whether that implied UP/DOWN read is corroborated — the other asset's own copy of the
    SAME named level already closed the same way (cross-asset), a DIFFERENT tier on the SAME
    asset already closed the same way (cross-tier), or this level is itself a live (not
    suggested-exhausted) meaningful P2 SMT candidate — with NO other mature day/week-tier item
    on EITHER asset closing the opposite way (a live contradiction voids corroboration
    outright). Both checks feed `preconfirm_eligible`, a CODE-SUGGESTED green light the model
    MAY act on by declaring `mature: true` for that item early — the same "code suggests, model
    may override" shape as `suggested_exhausted` (§2.1c), never a hard schema/validator change:
    `mature` stays a plain model-declared boolean, exactly as before this function existed."""
    out: list = []
    if now is None:
        return out
    next_1h = now.ceil("h")
    if next_1h <= now:
        next_1h = next_1h + pd.Timedelta(hours=1)
    next_4h = now.ceil("4h")
    if next_4h <= now:
        next_4h = next_4h + pd.Timedelta(hours=4)
    next_close = {"1h": next_1h, "4h": next_4h}

    def _tier_of(tkr, name):
        return (bundle.levels.get(tkr, {}).get(name) or (None, None, None, None))[3]

    # Every currently-mature (already-closed) day/week-tier P1 read, per asset — used below
    # for the cross-asset/cross-tier corroboration checks and the contradiction veto.
    mature_reads = {"MNQ": {}, "MES": {}}
    for tkr in ("MNQ", "MES"):
        status = (bundle.htf_close_status or {}).get(tkr, {})
        swept_map = (bundle.swept_at or {}).get(tkr, {})
        suppressed = (bundle.suppressed_p1_levels or {}).get(tkr, set())
        for name, tf_map in status.items():
            if swept_map.get(name) is None or name in suppressed:
                continue
            if _tier_of(tkr, name) not in ("day", "week"):
                continue
            for tf in ("1h", "4h"):
                info = (tf_map or {}).get(tf)
                if info is not None:
                    side = _implied_side(_level_polarity_hl(name), bool(info["beyond"]))
                    if side is not None:
                        mature_reads[tkr][name] = side
                    break

    all_mature_sides = list(mature_reads["MNQ"].values()) + list(mature_reads["MES"].values())
    smt_by_site = {(c["swept_ticker"], c["level"]): c
                   for c in (bundle.smt_candidates or []) if c.get("meaningful")}

    for tkr in ("MNQ", "MES"):
        other = "MES" if tkr == "MNQ" else "MNQ"
        status = (bundle.htf_close_status or {}).get(tkr, {})
        swept_map = (bundle.swept_at or {}).get(tkr, {})
        suppressed = (bundle.suppressed_p1_levels or {}).get(tkr, set())
        for name, tf_map in status.items():
            if swept_map.get(name) is None or name in suppressed:
                continue
            tier = _tier_of(tkr, name)
            if tier not in ("day", "week"):
                continue
            tup = bundle.levels.get(tkr, {}).get(name)
            if not tup or not isinstance(tup[0], (int, float)):
                continue
            price, _body, side = tup[0], tup[1], tup[2]
            for tf in ("1h", "4h"):
                if (tf_map or {}).get(tf) is not None:
                    continue      # already mature on this tf — not a candidate
                resolves_at = next_close[tf]
                minutes_remaining = (resolves_at - now).total_seconds() / 60.0
                if minutes_remaining < 0 or minutes_remaining > NEAR_MATURITY_WINDOW_MIN:
                    continue
                now_price = float(data[tkr]["close"].iloc[-1])
                ar = (bundle.avg_range_1h if tf == "1h" else bundle.avg_range_4h or {}).get(tkr)
                ratio = None
                if isinstance(ar, (int, float)) and ar > 0:
                    ratio = round(abs(now_price - price) / ar, 4)
                distance_safe = ratio is not None and ratio >= NEAR_MATURITY_DISTANCE_RATIO
                implied = _implied_side(_level_polarity_hl(name), _beyond_side(now_price, price, side))

                contradiction = implied is not None and any(
                    d != implied for d in all_mature_sides)
                cross_asset = mature_reads[other].get(name) == implied if implied else False
                cross_tier = any(
                    n != name and d == implied and _tier_of(tkr, n) != tier
                    for n, d in mature_reads[tkr].items()) if implied else False
                smt_cand = smt_by_site.get((tkr, name))
                smt_live = smt_cand is not None and not smt_cand.get("suggested_exhausted")
                corroborated = implied is not None and not contradiction and (
                    cross_asset or cross_tier or smt_live)

                out.append({
                    "asset": tkr, "level": name, "tier": tier, "tf": tf,
                    "resolves_at": str(resolves_at),
                    "minutes_remaining": round(minutes_remaining, 1),
                    "now_distance_ratio": ratio, "implied_direction": implied,
                    "distance_safe": distance_safe, "corroborated": corroborated,
                    "preconfirm_eligible": bool(distance_safe and corroborated),
                })
    return out


def render_evidence_text(bundle: FactsBundle, magnitude: Optional[dict] = None) -> str:
    """Render the S9 thesis-evidence block (decisions/thesis.md P1/P3/P4 inputs): weekly
    mid, per-level HTF close-status on BOTH tickers, and SMT candidates with tier
    eligibility. Kept SEPARATE from render_facts_text (S0-S7 stays byte-identical) and
    from render_menus_text (S8) — additive, never hashed, never in bundle.lines.

    `magnitude` (optional, plan-14 gap fix): the SAME {(asset, level, tf): ratio} dict
    build_evidence_magnitude(bundle) produces for score_thesis_evidence — passed in here so
    each HTF close-status line can show a plain WEAK/NORMAL/STRONG clearance label BEFORE
    the model declares its bias, instead of the magnitude multiplier being an invisible
    factor it has no way to anticipate (thesis.md §9). No new computation: this renders the
    identical ratio already used for scoring, so the label can never drift from the actual
    multiplier applied. `None` (the default) renders exactly as before this fix."""
    out: list = []
    A = out.append
    A("## S9 THESIS EVIDENCE (decisions/thesis.md P1-P4 inputs)")
    A(f"weekly_mid = {bundle.weekly_mid}")
    A(f"daily_mid = {bundle.day_mid}")
    A("\nHTF close-status per swept level (maturity gate: 'immature' = no qualifying HTF "
      "close yet since the sweep -> NOT usable evidence, decisions/thesis.md §3):")
    A("(a level tagged 'nested/duplicate' below is NOT usable as a fresh, standalone P1 "
      "item — thesis.md §2.1b/§2.1d — unless it is ALSO listed under SMT candidates AND "
      "NOT tagged P2-SUPPRESSED there, in which case it is P2 evidence only, not P1):")
    # thesis.md §2.1b: a candidate that was ALREADY nested when its own divergence fired
    # (p2_suppressed=True) does NOT keep a nested level's line alive here — same treatment
    # as a plain nested level with no SMT at all (fully hidden, not P1, not P2).
    candidate_sites = {(c["swept_ticker"], c["level"]) for c in (bundle.smt_candidates or [])
                      if not c.get("p2_suppressed")}
    for tkr in ("MNQ", "MES"):
        status = (bundle.htf_close_status or {}).get(tkr, {})
        swept_map = (bundle.swept_at or {}).get(tkr, {})
        suppressed = (bundle.suppressed_p1_levels or {}).get(tkr, set())
        stale = (bundle.p1_equilibrium_stale or {}).get(tkr, {})
        reversal = (bundle.htf_reversal or {}).get(tkr, {})
        promo_map = (bundle.promoted_session_levels or {}).get(tkr, {})
        reclaim_map = (bundle.mid_reclaim or {}).get(tkr, {})
        rendered_any = False
        for name, tf_map in status.items():
            if swept_map.get(name) is None:      # never swept -> not evidence, skip entirely
                continue
            is_candidate_site = (tkr, name) in candidate_sites
            if name in suppressed and not is_candidate_site:
                continue          # nested / duplicate restatement, not fresh P1 evidence
            rendered_any = True
            note = " [nested/duplicate -- P2-candidate context only, NOT a P1 item]" \
                if name in suppressed else ""
            # thesis.md §2.1c (2026-08-15, #6): equilibrium-reversion staleness is now a
            # HARD gate (score_thesis_evidence zeroes/skips it), not a suggestion.
            stale_tag = " [STALE: price has since reached equilibrium -- NOT usable P1 " \
                        "evidence]" if stale.get(name) else ""
            # thesis.md §2.1 (2026-08-15): running-extreme tier promotion tag.
            promo = promo_map.get(name)
            promo_tag = (f" [PROMOTED: running {promo} extreme -- scores {promo}-tier]"
                         if promo else "")
            for tf in ("1h", "4h"):
                info = (tf_map or {}).get(tf)
                if info is None:
                    A(f"  {tkr} {name} [{tf}]: immature (no qualifying close yet){note}")
                else:
                    read = "ACCEPTED beyond" if info["beyond"] else "REJECTED (closed before)"
                    ratio = (magnitude or {}).get((tkr, name, tf))
                    label = _magnitude_label(ratio)
                    tag = f" [clearance: {label}]" if label else ""
                    rev = (reversal.get(name) or {}).get(tf, "none")
                    rev_tag = f" [PARTIAL-BAR {rev.upper()}]" if rev != "none" else ""
                    # thesis.md §10 (2026-08-15, #2/#4): per-tf P4 eligibility for the
                    # synthetic mids — fresh reclaim = P4; superseded by a newer tier
                    # extreme = P3 position only (and a fresh P4 on ANY tf supersedes the
                    # stale tf's P3 for the same asset+mid).
                    p4_tag = ""
                    if name in ("daily_mid", "weekly_mid"):
                        rec = (reclaim_map.get(name) or {}).get(tf)
                        if rec is not None:
                            p4_tag = (" [P4-ELIGIBLE: fresh reclaim test]" if rec.get("fresh")
                                      else " [P4-INELIGIBLE: a newer running extreme "
                                           "postdates this crossing -- P3 position only]")
                    A(f"  {tkr} {name} [{tf}]: close={info['close']} @ {info['closed_at']} "
                      f"(n={info['n_closed_since']}) -> {read}{tag}{stale_tag}{note}"
                      f"{rev_tag}{promo_tag}{p4_tag}")
        if not rendered_any:
            A(f"  {tkr}: (none)")

    # thesis.md §2.1 P3 (2026-08-15, #3): unconditional position vs BOTH mids for BOTH
    # assets — P3 is a position snapshot, it needs no crossing/sweep. When a mid has no
    # qualifying HTF crossing at all (no close-status row above), this block is the P3
    # input and code auto-scores it as a position-only P3 item.
    A("\nMID POSITION (P3 inputs; unconditional, each asset vs its OWN mids):")
    for tkr in ("MNQ", "MES"):
        pos_map = (bundle.mid_position or {}).get(tkr, {})
        if not pos_map:
            A(f"  {tkr}: (n/a)")
            continue
        for mname in ("daily_mid", "weekly_mid"):
            p = pos_map.get(mname)
            if not p:
                continue
            ratio_s = (f", {p['dist_ratio']}x avg_1h" if p.get("dist_ratio") is not None
                       else "")
            A(f"  {tkr} vs {mname} {p['price']}: last close {p['side'].upper()} "
              f"({p['dist_pts']} pts{ratio_s})")
    A("\nSMT candidates (meaningful = day/week tier, eligible for thesis.md P2; "
      "swept_ticker = confirmed/pushed through (lagger); unswept_ticker = failed to "
      "confirm (leader)):")
    if not bundle.smt_candidates:
        A("  (none)")
    for cand in bundle.smt_candidates:
        exh = cand.get("suggested_exhausted")
        stretch = cand.get("stretch_since_fire")
        exh_tag = ""
        if stretch is not None:
            thr = SMT_SHELF_LIFE.get(cand.get("tier"))
            exh_tag = (f" | stretch_since_fire={stretch}x avg_1h"
                       f"{f' [SUGGESTED EXHAUSTED > {thr}x shelf-life]' if exh else ''}")
        nest_tag = " | P2-SUPPRESSED (level is nested)" if cand.get("p2_suppressed") else ""
        if cand.get("promoted_from"):
            nest_tag += (f" | PROMOTED from session tier (level is the running "
                         f"{cand['tier']} extreme on both assets)")
        A(f"  {cand['level']} [{cand['side']}, {cand['tier']}]: "
          f"swept_ticker={cand['swept_ticker']} (lagger) "
          f"unswept_ticker={cand['unswept_ticker']} (leader) "
          f"type={cand['type']} swept_at={cand['swept_at']} "
          f"meaningful={cand['meaningful']}{exh_tag}{nest_tag}")

    # --- plan 15 Task 7: pending-resolution timestamps for immature day/week items ---
    now = bundle.now
    A("\nPENDING RESOLUTION (immature day/week items resolve at the next HTF close AFTER now — "
      "copy the matching timestamp into pending_resolution.resolves_at, do not compute it "
      "yourself; thesis.md §3):")
    if now is not None:
        next_1h = now.ceil("h")
        if next_1h <= now:
            next_1h = next_1h + pd.Timedelta(hours=1)
        next_4h = now.ceil("4h")
        if next_4h <= now:
            next_4h = next_4h + pd.Timedelta(hours=4)
        A(f"  next 1h close: {next_1h} | next 4h close: {next_4h}")
        immature = []
        for tkr in ("MNQ", "MES"):
            status = (bundle.htf_close_status or {}).get(tkr, {})
            swept_map = (bundle.swept_at or {}).get(tkr, {})
            suppressed = (bundle.suppressed_p1_levels or {}).get(tkr, set())
            for name, tf_map in status.items():
                if swept_map.get(name) is None:
                    continue
                if name in suppressed and (tkr, name) not in candidate_sites:
                    continue      # nested / duplicate restatement, not a fresh P1 item
                tier = (bundle.levels.get(tkr, {}).get(name) or (None, None, None, None))[3]
                if tier not in ("day", "week"):
                    continue
                pend = [tf for tf in ("1h", "4h") if (tf_map or {}).get(tf) is None]
                if pend:
                    immature.append(f"{tkr} {name} (awaiting {'/'.join(pend)})")
        A("  immature day/week items awaiting resolution: "
          + ("; ".join(immature) if immature else "(none)"))
    else:
        A("  (now unavailable)")

    # --- thesis.md §3a: near-maturity pre-confirmation (bounded exception to §3) ---
    A(f"\nNEAR-MATURITY PRE-CONFIRMATION CANDIDATES (a day/week-tier item within "
      f"{NEAR_MATURITY_WINDOW_MIN} min of its next qualifying HTF close — thesis.md §3a; "
      f"ONLY when preconfirm_eligible=True MAY you declare mature=true for that item now, "
      f"using implied_direction, instead of waiting for the literal close):")
    if not bundle.near_maturity_candidates:
        A("  (none)")
    for cand in bundle.near_maturity_candidates:
        A(f"  {cand['asset']} {cand['level']} [{cand['tier']}, {cand['tf']}]: "
          f"resolves_at={cand['resolves_at']} ({cand['minutes_remaining']}m away) | "
          f"now_distance={cand['now_distance_ratio']}x avg_range | "
          f"implied_direction={cand['implied_direction']} | "
          f"distance_safe={cand['distance_safe']} corroborated={cand['corroborated']} "
          f"-> preconfirm_eligible={cand['preconfirm_eligible']}")

    # --- plan 15 Task 4 / plan 17 Fix 2: curated + verdicted FVG-fill (P5) candidates ---
    A(f"\nFVG-FILL CANDIDATES (thesis.md §2.1 P5; curated to VISITED zones formed within "
      f"{FVG_LOOKBACK_DAYS} days of now — copy the id verbatim as the evidence `level`. The "
      f"HELD/VIOLATED verdict is code-derived from the same HTF-close maturity gate the named "
      f"levels use: HELD -> direction=accept (the zone held its bull=up/bear=down bias), "
      f"VIOLATED -> direction=reject (price closed decisively through it); "
      f"'too recent to verdict' = no qualifying HTF close since the fill yet, NOT usable. "
      f"'stretch_since_fill=Nx avg_1h' past shelf life ({FVG_SHELF_LIFE}x) carries a "
      f"'[SUGGESTED EXHAUSTED]' tag, same meaning/override as the SMT candidates' own tag "
      f"below — set exhausted: true on the P5 item if you judge it stale, or disagree and "
      f"score it normally. ADJACENT same-asset/same-kind/same-tf zones (consecutive bars, "
      f"one continuous move) are auto-collapsed by code to the freshest one when declared — "
      f"citing several from one continuous move does not add weight, so prefer the single "
      f"freshest zone from a run):")
    _cutoff = (bundle.now - pd.Timedelta(days=FVG_LOOKBACK_DAYS)) if bundle.now is not None else None
    _curated_fvg = [z for z in (bundle.fvg_zones or [])
                    if z.get("visited") and (_cutoff is None or z["ts"] >= _cutoff)]
    if not _curated_fvg:
        A("  (none visited within lookback)")
    for z in _curated_fvg:
        v = z.get("fill_verdict")
        vtag = (" -> HELD (accept)" if v == "held"
                else " -> VIOLATED (reject)" if v == "violated"
                else " -> (fill too recent to verdict)")
        stretch = z.get("stretch_since_visit")
        exh_tag = (f" | stretch_since_fill={stretch}x avg_1h"
                   + (" [SUGGESTED EXHAUSTED]" if z.get("suggested_exhausted") else "")
                   if stretch is not None else "")
        A(f"  {z['id']}: {z['kind']} zone {z['lo']}-{z['hi']} [{z['tf']}] VISITED{vtag}{exh_tag}")

    # --- plan 14 Task 2: stretch + nearest-meaningful-level distance (MNQ) ---
    ar = (bundle.avg_range_1h or {}).get("MNQ")
    np_ = bundle.now_price
    A("\nSTRETCH & DISTANCE (MNQ; normalized by avg 1h range, v1-seed ATR — thesis.md §2.1c):")
    A(f"  avg_range_1h = {ar} | avg_range_4h = {(bundle.avg_range_4h or {}).get('MNQ')}")
    dhi, dlo = (bundle.day_hi or {}).get("MNQ"), (bundle.day_lo or {}).get("MNQ")
    ext_dists = [abs(np_ - x) for x in (dhi, dlo)
                 if isinstance(np_, (int, float)) and isinstance(x, (int, float))]
    if ext_dists:
        # Distance from the FARTHER (opposite-side) extreme, not the nearer one: this is
        # "how far has price run from the extreme it moved away from" - large exactly when
        # a big one-directional move has occurred and price sits at/near its fresh extreme,
        # not the reverse. Using min() here was a spec bug (thesis.md §2.1c intent) that
        # zeroed the stretch signal on the most-extended cases it exists to flag.
        raw = max(ext_dists)
        if isinstance(ar, (int, float)) and ar > 0:
            mult = raw / ar
            flag = " [STRETCHED > 3.0x avg 1h range]" if mult > 3.0 else ""
            A(f"  stretch from opposite-side day extreme: raw={raw:.2f} pts | "
              f"{mult:.2f}x avg 1h range{flag}")
        else:
            A(f"  stretch from opposite-side day extreme: raw={raw:.2f} pts | "
              f"n/a (no avg_range_1h)")
    else:
        A("  stretch from opposite-side day extreme: n/a (missing price/day extremes)")
    dol_menu = (bundle.menus or {}).get("dol") if bundle.menus else None
    if dol_menu is None:
        dol_menu = _dol_menu((bundle.levels or {}).get("MNQ", {}), {}, np_,
                              suppressed=(bundle.suppressed_p1_levels or {}).get("MNQ", set()))
    for direction in ("UP", "DOWN"):
        rows = dol_menu.get(direction) or []
        if not rows or not isinstance(np_, (int, float)):
            A(f"  nearest named level [{direction}]: (none)")
            continue
        nearest = rows[0]
        d = abs(nearest["price"] - np_)
        if isinstance(ar, (int, float)) and ar > 0:
            mult = d / ar
            flag = " [SPARSE STRUCTURE > 3.0x avg 1h range]" if mult > 3.0 else ""
            A(f"  nearest named level [{direction}]: {nearest['level']} @ {nearest['price']} "
              f"raw={d:.2f} pts | {mult:.2f}x avg 1h range{flag}")
        else:
            A(f"  nearest named level [{direction}]: {nearest['level']} @ {nearest['price']} "
              f"raw={d:.2f} pts | n/a (no avg_range_1h)")

    # --- plan 14 Task 3: session-maturity soft prior ---
    A("\nSESSION MATURITY (thesis.md §2.2 — soft prior, not a code gate):")
    A(f"  session_elapsed_frac = {bundle.session_elapsed_frac} | mature P1/P2-eligible items "
      f"now = {bundle.mature_evidence_count} — a thin ledger this early is expected "
      f"data-scarcity (thesis.md §2.2), not market ambiguity; let it inform confidence, do "
      f"not read it as contradiction.")

    # --- plan 14 Task 8: cross-family price-cluster confluence (audit-only, not scored) ---
    A("\nCROSS-FAMILY CONFLUENCE (audit-only, not scored — thesis.md §2.1e):")
    he = bundle.historical_extremes or {}
    ar_by_tkr = bundle.avg_range_1h or {}
    any_note = False
    for tkr in ("MNQ", "MES"):
        hist_for = he.get(tkr)
        ar_t = ar_by_tkr.get(tkr)
        if not hist_for or not isinstance(ar_t, (int, float)) or ar_t <= 0:
            continue
        tol = 0.25 * ar_t   # v1 seed, pending calibration
        for name, tup in (bundle.levels or {}).get(tkr, {}).items():
            price = tup[0] if tup else None
            if not isinstance(price, (int, float)):
                continue
            for note in _confluence_notes(price, hist_for, tol):
                any_note = True
                A(f"  {tkr} {name} @ {price}: {note}")
    if not any_note:
        A("  (none)")

    return "\n".join(out) + "\n"


def build_evidence_magnitude(bundle: FactsBundle) -> dict:
    """plan 14 Task 5 — {(asset, level, tf): ratio} where ratio = |close - level_price| /
    avg_range[tf][asset], over every MATURE HTF close (thesis.md §4 clearance magnitude).
    Covers ALL swept levels x tf so score_thesis_evidence can look up whatever level/tf the
    model later declares; a missing key -> neutral x1.0. Skips immature/None closes and
    None/zero avg_range. Built where the FactsBundle lives (bench.build_facts), never
    recomputed in run_agent — the scorer receives only this flat ratio dict.

    2026-08-05: daily_mid/weekly_mid get a ratio too, sourced from `bundle.mid_price`
    (they carry no entry in `bundle.levels` — synthetic, not a named level) — previously
    silently skipped here, so every P3 mid item scored at an unweighted x1.0 regardless of
    how thin its clearance was (thesis.md §10, 2026-08-05). The SAME ratio is also registered under
    the level's two possible P4-promoted names (`{mid}_high`/`{mid}_low`, see _mid_tf_state)
    since the underlying close/level_price/avg_range distance is identical regardless of
    which criterion the auto-derivation ultimately picks."""
    out: dict = {}
    avg = {"1h": bundle.avg_range_1h or {}, "4h": bundle.avg_range_4h or {}}
    for tkr in ("MNQ", "MES"):
        status = (bundle.htf_close_status or {}).get(tkr, {})
        levels = (bundle.levels or {}).get(tkr, {})
        mid_prices = (bundle.mid_price or {}).get(tkr, {})
        for name, tf_map in status.items():
            tup = levels.get(name)
            if tup and isinstance(tup[0], (int, float)):
                level_price = tup[0]
                names = (name,)
            elif name in mid_prices and isinstance(mid_prices[name], (int, float)):
                level_price = mid_prices[name]
                names = (name, f"{name}_high", f"{name}_low")
            else:
                continue
            for tf in ("1h", "4h"):
                info = (tf_map or {}).get(tf)
                if not info:
                    continue
                ar = avg[tf].get(tkr)
                if not isinstance(ar, (int, float)) or ar <= 0:
                    continue
                ratio = round(abs(info["close"] - level_price) / ar, 4)
                for nm in names:
                    out[(tkr, nm, tf)] = ratio
    return out


def compute_facts(mnq_df: pd.DataFrame, mes_df: pd.DataFrame, *,
                  ath_mnq: Optional[float] = None, ath_mes: Optional[float] = None,
                  hist_mnq: Optional[pd.DataFrame] = None,
                  hist_mes: Optional[pd.DataFrame] = None,
                  now: Optional[pd.Timestamp] = None) -> FactsBundle:
    """Pure fact computation: the S0–S7 + S3b schema over two load()-normalised bar
    frames (lowercase o/h/l/c, tz-aware ET, CME-maintenance bars already dropped).

    Returns a FactsBundle carrying the rendered lines (render_facts_text) and the
    structured values. No maths changed vs the original print-script — the print
    statements now append to bundle.lines. `now` defaults to the earliest last-bar
    across the two frames; everything after `now` is dropped (no-lookahead).
    """
    bundle = FactsBundle()
    L = bundle.lines.append

    data = {"MNQ": mnq_df, "MES": mes_df}
    if now is None:
        now = min(df.index[-1] for df in data.values())
    # No-lookahead precondition: nothing strictly after `now` enters the bundle.
    data = {k: v[v.index <= now] for k, v in data.items()}
    hist = {"MNQ": hist_mnq, "MES": hist_mes}
    td_now = trade_date(now)

    # Trading-date universe: Mon-Fri only, real sessions only (>=1000 bars) — stray
    # ticks must not mint phantom prev-days (POC run-3 G12).
    counts = pd.Series((data["MNQ"].index + pd.Timedelta(hours=7)).date).value_counts()
    all_tds = sorted(d for d, n in counts.items()
                     if (d.weekday() < 5 and n >= 1000) or d == td_now)
    past_tds = [d for d in all_tds if d < td_now]
    prev1_td = past_tds[-1] if past_tds else None
    prev2_td = past_tds[-2] if len(past_tds) > 1 else None

    iso_now = td_now.isocalendar()[:2]
    week_tds = [d for d in all_tds if d.isocalendar()[:2] == iso_now]
    prev_week_all = sorted({d for d in all_tds if d.isocalendar()[:2] < iso_now})
    prev_iso = prev_week_all[-1].isocalendar()[:2] if prev_week_all else None
    prev1_week_tds = [d for d in all_tds if prev_iso and d.isocalendar()[:2] == prev_iso]

    wk_anchor = week_start_ts(now)

    # plan 15 Task 1: long-horizon day/week extremes, computed ONCE per ticker (only when the
    # full-history frame is supplied) and reused for both the new prev3-7_day / prev2-3_week
    # named levels (S1 loop below) and the S5b render table + historical_extremes (further
    # down) — no naive re-scan of the primary frame. Absent hist -> empty, byte-identical to
    # the pre-plan-15 no-hist path (the golden fixtures supply no hist).
    long_horizon = {}
    for tkr in data:
        if hist.get(tkr) is not None:
            long_horizon[tkr] = _long_horizon_extremes(hist[tkr], now)

    cands = []
    for d, h, m in ((td_now, 9, 20), (td_now, 13, 0)):
        c = pd.Timestamp(d, tz=TZ) + pd.Timedelta(hours=h, minutes=m)
        if c <= now:
            cands.append(c)
    sess_now_start = session_frame(data["MNQ"], td_now).index[0]
    ckpt = max(cands) if cands else sess_now_start

    bundle.now = now
    bundle.td_now = td_now
    bundle.ckpt = ckpt
    bundle.now_price = float(data["MNQ"]["close"].iloc[-1])
    # plan 14 Task 3: session-maturity soft prior — fraction of the current session elapsed
    # at now (minutes since the session's first bar over the ~1379-min full session span:
    # prior-day 18:00 -> 16:59 ET). Clamped to [0,1]. v1 seed span, pending calibration.
    _elapsed_min = (now - sess_now_start).total_seconds() / 60.0
    bundle.session_elapsed_frac = round(min(max(_elapsed_min / 1379.0, 0.0), 1.0), 3)

    L("## S0 META")
    L(f"now = {now}  (trade date {td_now}, {now.strftime('%A')})")
    subsess = ("asia" if now.hour >= 18 else "london" if now.hour < 6
               else "ny_morning" if now.hour < 12 else "ny_evening")
    in_whip = (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (11, 30)
    L(f"sub-session at cut: {subsess} | inside 09:15-11:30 whipsaw window: {in_whip}")
    L(f"daily-trend checkpoint (most recent at/before now): {ckpt}")
    L(f"current session first bar: {sess_now_start}")
    L(f"prev1 trade date: {prev1_td} | prev2: {prev2_td}")
    L(f"current-week trade dates: {week_tds} | prev1-week: {prev1_week_tds}")
    L(f"ENGINE week anchor (session_pipeline._week_start_ts): {wk_anchor}")

    levels = {}       # levels[tkr][name] = (price, body_price, side, tier, active_from)
    sess_cache = {}
    # thesis.md §2.1b/§7: a WIDER, non-session-scoped search frame for day/week-tier levels
    # only — [now - SMT_LOOKBACK_HOURS, now], clamped to available data. Session-tier levels
    # keep using sess_cache/sess_now (narrow, current-session-only) exactly as before.
    wide_cache = {}
    cards = []        # S3b candidate item cards (precomputed scoring inputs)
    TIER_W = {"week": 3.0, "day": 2.0, "session": 1.0}   # next-move.md §2 (fill 1.5 = FVGs, not carded)

    def window_of(ts):
        return ("asia" if ts.hour >= 18 else "london" if ts.hour < 6
                else "ny_morning" if ts.hour < 12 else "ny_evening")

    def add_card(kind, tkr_, name_, price_, side_, ts_, tier_, note_=""):
        age = age_min(ts_, now)
        if age > 360:
            return                                        # spent (~2× the 3h decay horizon)
        w = TIER_W[tier_]
        fresh = 2 ** (-age / 180)
        cards.append(
            f"{tkr_} {name_} {price_} [{side_}] {kind}: window={window_of(ts_)} @ {ts_} | "
            f"age {age:.0f}m | tier={tier_} w={w} | freshness={fresh:.3f} | "
            f"base(w×freshness)={w * fresh:.2f}{note_}")
    for tkr, df in data.items():
        sess_now = session_frame(df, td_now)
        sess_cache[tkr] = sess_now
        wide_start = now - pd.Timedelta(hours=SMT_LOOKBACK_HOURS)
        if len(df) and df.index[0] > wide_start:
            wide_start = df.index[0]
        wide_cache[tkr] = df.loc[wide_start:now]
        L(f"\n## S1 LEVELS {tkr} (price = wick extreme; body = close extreme)")
        tdo = float(sess_now["open"].iloc[0])
        L(f"TDO (session open {sess_now.index[0]}): {tdo}")
        two, two_ts = compute_two(df, week_tds, now)
        L(f"TWO (bar {two_ts}): {two}")

        lv = {}
        lv["TDO"] = (tdo, None, None, "session", sess_now.index[0])
        if two is not None:
            lv["TWO"] = (two, None, None, "session", sess_now.index[0])
        for lbl, d in (("prev1_day", prev1_td), ("prev2_day", prev2_td)):
            if d is None:
                continue
            s = session_frame(df, d)
            h, l, ch, cl = hl(s)
            c = float(s["close"].iloc[-1])
            pos = (c - l) / (h - l) if h != l else float("nan")
            L(f"{lbl} ({d}): high={h} (body {ch}) low={l} (body {cl}) close={c} (range pos {pos:.2f})")
            lv[f"{lbl}_high"] = (h, ch, "above", "day", sess_now.index[0])
            lv[f"{lbl}_low"] = (l, cl, "below", "day", sess_now.index[0])
        if prev1_week_tds:
            pw = pd.concat([session_frame(df, d) for d in prev1_week_tds])
            h, l, ch, cl = hl(pw)
            L(f"prev1_week ({prev1_week_tds[0]}..{prev1_week_tds[-1]}): high={h} (body {ch}) low={l} (body {cl})")
            lv["prev1_week_high"] = (h, ch, "above", "week", sess_now.index[0])
            lv["prev1_week_low"] = (l, cl, "below", "week", sess_now.index[0])

        # plan 15 Task 1: deeper prior-day/week named levels (prev3-7_day, prev2-3_week),
        # sourced from the reused long_horizon aggregation — NOT a fresh primary-frame scan.
        # prev1/prev2_day and prev1_week stay on the existing prim-based path above (byte-
        # identical for any caller not supplying hist). No L(...) render here: these are
        # additive named levels consumed by the generic S2/S3/S3b/S8 machinery (they iterate
        # lv/levels[tkr]), never a new S0-S7 line. Graceful degradation: assign only what
        # exists (thin history -> fewer levels, never an error). §2.1b: a nested deeper level
        # is still independent cross-asset P2/SMT evidence even when same-asset-superseded.
        lh = long_horizon.get(tkr)
        if lh:
            prior_days = [r for r in lh["daily"] if r["trade_date"] < td_now]
            # most-recent = 1; prev1/prev2 already assigned from prim -> deeper start at 3.
            recent_days = list(reversed(prior_days))       # newest first
            for i in range(3, 8):
                if len(recent_days) < i:
                    break
                r = recent_days[i - 1]
                lv[f"prev{i}_day_high"] = (r["high"], r["close_high"], "above", "day",
                                          sess_now.index[0])
                lv[f"prev{i}_day_low"] = (r["low"], r["close_low"], "below", "day",
                                         sess_now.index[0])
            prior_weeks = [r for r in lh["weekly"] if r["week_start"].isocalendar()[:2] < iso_now]
            recent_weeks = list(reversed(prior_weeks))     # newest first (prev1_week = index 0)
            for i in (2, 3):
                if len(recent_weeks) < i:
                    break
                r = recent_weeks[i - 1]
                lv[f"prev{i}_week_high"] = (r["high"], r["close_high"], "above", "week",
                                           sess_now.index[0])
                lv[f"prev{i}_week_low"] = (r["low"], r["close_low"], "below", "week",
                                          sess_now.index[0])

        wkf = df.loc[wk_anchor:now]
        h, l, ch, cl = hl(wkf)
        L(f"week running [ENGINE anchor {wk_anchor}]: high={h} low={l} mid={(h + l) / 2:.3f}")
        if tkr == "MNQ" and h is not None and l is not None:
            bundle.weekly_mid = round((h + l) / 2.0, 2)   # thesis.md input (not rendered in S1)
        # Per-asset (both tickers, unlike the MNQ-only weekly_mid scalar above) — thesis.md
        # §2.1c equilibrium-staleness decay needs EACH asset's own weekly mid.
        if h is not None:
            bundle.week_hi[tkr] = float(h)
        if l is not None:
            bundle.week_lo[tkr] = float(l)
        if len(wkf):
            bundle.week_hi_ts[tkr] = wkf["high"].idxmax()
            bundle.week_lo_ts[tkr] = wkf["low"].idxmin()
        dh, dl, dch, dcl = hl(sess_now)
        L(f"day running: high={dh} (at {sess_now['high'].idxmax()}) low={dl} "
              f"(at {sess_now['low'].idxmin()}) mid={(dh + dl) / 2}")
        # thesis.md §2.1c/P3 day extremes use an EXTENDED window (_day_start_ts, hypothesis.py
        # ::compute_live_hl_mid parity) — NOT sess_now's own narrow current-session frame,
        # which degenerates to ~0 bars right at/after the mandatory 18:00 ET call. The S1 "day
        # running" line above stays on sess_now UNCHANGED (hash parity with the shared S0-S7
        # core, read by the old daily-trend.md/next-move.md KB too) — only the structured
        # bundle.day_hi/day_lo/day_mid fields (thesis.md-only consumers: S8 daily_mid menu,
        # §2.1c stretch) use the extended frame computed here.
        day_ext_frame = df.loc[_day_start_ts(now):now]
        dh_ext, dl_ext, _dch_ext, _dcl_ext = hl(day_ext_frame)
        if tkr == "MNQ" and dh_ext is not None and dl_ext is not None:
            bundle.day_mid = round((dh_ext + dl_ext) / 2.0, 2)   # S8 menu input (not rendered in S1)
        # plan 14 Task 1: per-asset day extremes + ATR-like avg_range (completed HTF bars
        # only, simple high-low true range; windows are v1 seeds pending calibration). No
        # L(...) — additive FactsBundle fields, S0-S7 text unchanged.
        if dh_ext is not None:
            bundle.day_hi[tkr] = float(dh_ext)
        if dl_ext is not None:
            bundle.day_lo[tkr] = float(dl_ext)
        if len(day_ext_frame):
            bundle.day_hi_ts[tkr] = day_ext_frame["high"].idxmax()
            bundle.day_lo_ts[tkr] = day_ext_frame["low"].idxmin()
        for tf, win, dst in (("1h", 20, bundle.avg_range_1h), ("4h", 10, bundle.avg_range_4h)):
            bars = ohlc(df, tf).iloc[:-1]                 # completed bars only
            if len(bars) == 0:
                dst[tkr] = None
                continue
            tr = (bars["high"] - bars["low"]).tail(win)
            dst[tkr] = round(float(tr.mean()), 4) if len(tr) else None
        L(f"last close: {float(df['close'].iloc[-1])}")
        ath = ath_mnq if tkr == "MNQ" else ath_mes
        if ath:
            L(f"ATH {ath}: last close is {(ath - float(df['close'].iloc[-1])) / ath * 100:.2f}% below")

        if prev1_td is not None:
            # Time-gate (prev1) sub-session levels by the current ET clock, mirroring
            # production smt_detect.py::eligible_levels for THIS file's (cur)/(prev1)
            # model: during today's own forming Asia session (>=18:00 ET) only the two NY
            # sub-sessions' prev1 copies are still in scope; from London onward
            # (00:00-17:59 ET) every prev1 sub-session copy is stale/superseded by its
            # (cur) copy and none are eligible.
            if now.hour >= 18:
                prev1_eligible = {"ny_morning", "ny_evening"}
            else:
                prev1_eligible = set()
            pblocks = sub_blocks(session_frame(df, prev1_td))
            for name, frame in pblocks.items():
                if name not in prev1_eligible:
                    continue
                h, l, ch, cl = hl(frame)
                if h is None:
                    continue
                L(f"{name}(prev1): high={h} (body {ch}) low={l} (body {cl})")
                lv[f"{name}(prev1)_high"] = (h, ch, "above", "session", sess_now.index[0])
                lv[f"{name}(prev1)_low"] = (l, cl, "below", "session", sess_now.index[0])
        cblocks = sub_blocks(sess_now)
        closes_at = {"asia": pd.Timestamp(td_now, tz=TZ),
                     "london": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=6),
                     "ny_morning": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=12),
                     "ny_evening": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=17)}
        for name, frame in cblocks.items():
            if len(frame) == 0 or closes_at[name] > now:
                continue
            h, l, ch, cl = hl(frame)
            L(f"{name}(cur, closed): high={h} (body {ch}) low={l} (body {cl})")
            lv[f"{name}(cur)_high"] = (h, ch, "above", "session", closes_at[name])
            lv[f"{name}(cur)_low"] = (l, cl, "below", "session", closes_at[name])
        levels[tkr] = lv

        L(f"\n## S2 SWEEPS {tkr} (current True Day; inclusive wick cross; body = first 15m close beyond; ages vs now)")
        bundle.swept_at.setdefault(tkr, {})
        for name, (price, body, side, tier, active_from) in sorted(lv.items(), key=lambda kv: -kv[1][0]):
            if side is None:
                for s in ("above", "below"):
                    t = first_cross(sess_now.loc[active_from:], price, s)
                    if t is not None:
                        L(f"{name} {price}: first {s}-cross {t} (age {age_min(t, now):.0f}m)")
                continue
            frame = sess_now.loc[active_from:]
            t = first_cross(frame, price, side)
            if t is None:
                bundle.swept_at[tkr][name] = None
                dist, ats = closest_approach(frame, price, side)
                extra = (f" (closest approach {dist:.2f} short @ {ats}, age {age_min(ats, now):.0f}m)"
                         if dist is not None else "")
                L(f"{name} {price} [{side}]: NOT swept{extra}")
                continue
            bundle.swept_at[tkr][name] = t
            exc = excursion_beyond(frame.loc[t:], price, side)
            thr = DEPLETE[tkr][tier]
            tb = first_body_cross_15m(frame, price, side)
            tb_s = f"{tb} (age {age_min(tb, now):.0f}m)" if tb is not None else "—"
            L(f"{name} {price} [{side}]: swept {t} (age {age_min(t, now):.0f}m) | "
                  f"max excursion beyond {exc:.2f} "
                  f"({'DEPLETED' if exc >= thr else 'not depleted'}, thr {thr:.1f}) | body15m: {tb_s}")
            add_card("sweep", tkr, name, price, side, t, tier,
                     " | DEPLETED" if exc >= thr else "")

    bundle.levels = levels

    # thesis.md §2.1b/§2.1d refinement: nested prevN levels and duplicate-simultaneous-
    # sweep restatements are excluded from FRESH P1 evidence. P2/SMT candidacy gets its own,
    # separate suppression below (_p2_nesting_suppression, computed once
    # bundle.smt_candidates exists) — no longer unconditionally exempt.
    bundle.suppressed_p1_levels = {}
    for tkr in ("MNQ", "MES"):
        nested = _nested_prev_levels(levels[tkr])
        nested_session = _nested_session_levels(levels[tkr])
        dup_losers = _duplicate_sweep_losers(levels[tkr], bundle.swept_at.get(tkr, {}))
        # thesis.md §2.1b (2026-08-15): most-extreme-swept-only — see
        # _extremity_shadowed_levels.
        extremity = _extremity_shadowed_levels(levels[tkr], bundle.swept_at.get(tkr, {}))
        bundle.suppressed_p1_levels[tkr] = nested | nested_session | dup_losers | extremity

    # thesis.md §2.1 (2026-08-15): session-tier levels that ARE the running day/week
    # extreme — tier promotion map (P1 weight via bench/facts.py's level_tiers override,
    # P2 eligibility via the SMT-candidate promotion below).
    bundle.promoted_session_levels = _promoted_session_extremes(
        data, levels, bundle.swept_at, now, wk_anchor)

    bundle.htf_close_status = {}
    for tkr in ("MNQ", "MES"):
        bundle.htf_close_status[tkr] = {}
        bundle.htf_reversal[tkr] = {}
        bundle.recross_distance[tkr] = {}
        now_price_tkr = float(data[tkr]["close"].iloc[-1])
        _ar1h = (bundle.avg_range_1h or {}).get(tkr)
        for name, (price, body, side, tier, active_from) in levels[tkr].items():
            if side is None:
                continue
            status = _htf_close_status(
                data[tkr], bundle.swept_at[tkr].get(name), price=price, side=side, now=now)
            bundle.htf_close_status[tkr][name] = status
            # thesis.md §10 (2026-08-05): does the currently-forming next-tf bar already
            # undermine this (mature) verdict? See _htf_reversal_tier.
            rev_by_tf = {}
            for tf in ("1h", "4h"):
                if status.get(tf) is None:
                    continue
                bar = _last_completed_bar(data[tkr], tf, now)
                if bar is None:
                    continue
                rev_by_tf[tf] = _htf_reversal_tier(bar, price, now_price_tkr)
            if rev_by_tf:
                bundle.htf_reversal[tkr][name] = rev_by_tf
                # Stage 2 input (2026-08-15): how far the current price sits from the
                # level, in avg-1h-range units — the distance-safe check for the
                # experimental P2 discount-fire rung.
                if isinstance(_ar1h, (int, float)) and _ar1h > 0:
                    bundle.recross_distance[tkr][name] = round(
                        abs(now_price_tkr - price) / _ar1h, 4)

    # plan 17 Fix 3 / §10 (2026-08-05 rewrite): daily_mid / weekly_mid as synthetic per-
    # asset "levels" fed through the SAME _htf_close_status()-shaped machinery, so
    # render_evidence_text's per-level loop renders a [1h]/[4h] ACCEPTED/REJECTED verdict
    # for the mid -- the P4 (HTF-confirmed reclaim / failed-reclaim) anchor that was
    # missing (thesis.md §3), previously only a bare number. beyond=True means the
    # relevant COMPLETED <tf> bar CLOSED ABOVE the mid -> reclaim HELD (ACCEPTED,
    # bullish); below -> failed reclaim (REJECTED, bearish). Per-tf via _mid_tf_state
    # (2026-08-05): a mid is retested constantly (normal for an equilibrium, unlike a
    # one-time-swept named level), so the verdict/freshness are anchored to COMPLETED
    # <tf>-bar crossings, immune to a sub-bar re-touch inside a still-forming bar hiding
    # an already-settled test (the 2026-07-22 09:00-09:20 ET MNQ weekly_mid case).
    for tkr in ("MNQ", "MES"):
        dh, dl = bundle.day_hi.get(tkr), bundle.day_lo.get(tkr)
        wh, wl = bundle.week_hi.get(tkr), bundle.week_lo.get(tkr)
        mids = {}
        if dh is not None and dl is not None:
            mids["daily_mid"] = round((dh + dl) / 2.0, 2)
        if wh is not None and wl is not None:
            mids["weekly_mid"] = round((wh + wl) / 2.0, 2)
        now_price_tkr = float(data[tkr]["close"].iloc[-1])
        bundle.mid_price[tkr] = {}
        bundle.mid_reclaim[tkr] = {}
        bundle.mid_position[tkr] = {}
        for mname, mid in mids.items():
            bundle.mid_price[tkr][mname] = mid
            # thesis.md §2.1 P3 (2026-08-15, #3): unconditional position read — P3 is a
            # position snapshot and needs no crossing, so it must exist even when the
            # close-status machinery below yields nothing (never crossed within lookback).
            _ar1h = (bundle.avg_range_1h or {}).get(tkr)
            _dist = abs(now_price_tkr - mid)
            bundle.mid_position[tkr][mname] = {
                "price": mid,
                "side": "above" if now_price_tkr > mid else "below",
                "dist_pts": round(_dist, 2),
                "dist_ratio": (round(_dist / _ar1h, 4)
                               if isinstance(_ar1h, (int, float)) and _ar1h > 0 else None),
            }
            hi_ts, lo_ts = ((bundle.day_hi_ts.get(tkr), bundle.day_lo_ts.get(tkr))
                           if mname == "daily_mid"
                           else (bundle.week_hi_ts.get(tkr), bundle.week_lo_ts.get(tkr)))
            status_by_tf, reclaim_by_tf, rev_by_tf = {}, {}, {}
            for tf in ("1h", "4h"):
                status, reclaim = _mid_tf_state(data[tkr], mid, tf, now, hi_ts, lo_ts)
                status_by_tf[tf] = status
                if reclaim is not None:
                    reclaim_by_tf[tf] = reclaim
                if status is not None:
                    bar = _last_completed_bar(data[tkr], tf, now)
                    if bar is not None:
                        rev_by_tf[tf] = _htf_reversal_tier(bar, mid, now_price_tkr)
            bundle.htf_close_status[tkr][mname] = status_by_tf
            # rendering/legacy scalar ("was this mid ever swept at all" gate for the S9
            # close-status loop, which is shared with P1) -- prefer 1h, fall back to 4h so
            # a mature 4h-only reading (1h still immature) is not skipped entirely.
            bundle.swept_at[tkr][mname] = next(
                (status_by_tf[tf]["closed_at"] for tf in ("1h", "4h") if status_by_tf.get(tf)),
                None)
            if reclaim_by_tf:
                bundle.mid_reclaim[tkr][mname] = reclaim_by_tf
            if rev_by_tf:
                # register under all names a later criterion promotion could use (mirrors
                # build_evidence_magnitude's own {mid}/{mid}_high/{mid}_low pattern).
                _ar1h_mid = (bundle.avg_range_1h or {}).get(tkr)
                for nm in (mname, f"{mname}_high", f"{mname}_low"):
                    bundle.htf_reversal[tkr][nm] = rev_by_tf
                    if isinstance(_ar1h_mid, (int, float)) and _ar1h_mid > 0:
                        bundle.recross_distance[tkr][nm] = round(
                            abs(now_price_tkr - mid) / _ar1h_mid, 4)

    L("\n## S3 CROSS-TICKER SWEEP MATRIX (same level name; one swept + other not = divergence candidate)")
    shared = sorted(set(levels["MNQ"]) & set(levels["MES"]))
    for name in shared:
        p1, b1, side, tier, af1 = levels["MNQ"][name]
        p2, b2, _, _, af2 = levels["MES"][name]
        if side is None:
            continue
        f1, f2 = sess_cache["MNQ"].loc[af1:], sess_cache["MES"].loc[af2:]
        t1, t2 = first_cross(f1, p1, side), first_cross(f2, p2, side)
        tb1 = first_body_cross_15m(f1, p1, side)
        tb2 = first_body_cross_15m(f2, p2, side)
        tag = ""
        if (t1 is None) != (t2 is None):
            tag = "  <-- WICK DIVERGENCE CANDIDATE (lead " + ("MNQ" if t1 is not None else "MES") + ")"
            swept_tkr, unswept_tkr = ("MNQ", "MES") if t1 is not None else ("MES", "MNQ")
            bundle.smt_candidates.append({
                "level": name, "tier": tier, "side": side,
                "swept_ticker": swept_tkr, "unswept_ticker": unswept_tkr,
                "swept_at": t1 if t1 is not None else t2, "type": "wick",
                "meaningful": tier in ("day", "week"),
            })
        elif t1 is not None and t2 is not None and abs((t1 - t2).total_seconds()) > 900:
            tag = "  <-- both swept, >15min apart (transient divergence window)"
        btag = ""
        if (tb1 is None) != (tb2 is None):
            btag = "  <-- BODY(15m) DIVERGENCE CANDIDATE (lead " + ("MNQ" if tb1 is not None else "MES") + ")"
            swept_tkr_b, unswept_tkr_b = ("MNQ", "MES") if tb1 is not None else ("MES", "MNQ")
            bundle.smt_candidates.append({
                "level": name, "tier": tier, "side": side,
                "swept_ticker": swept_tkr_b, "unswept_ticker": unswept_tkr_b,
                "swept_at": tb1 if tb1 is not None else tb2, "type": "body",
                "meaningful": tier in ("day", "week"),
            })
        L(f"{name} [{side}, {tier}]: wick MNQ {t1 or 'not swept'} | MES {t2 or 'not swept'}{tag}")
        L(f"{'':>{len(name)}}   body MNQ {tb1 or '—'} | MES {tb2 or '—'}{btag}")
        # Laggard reach: how close the NON-sweeping ticker came, and when — the freshness of
        # the FAILURE leg (laggard test-and-fail candidate item, next-move.md §2).
        for lg, tl, ff, pp in (("MNQ", t1, f1, p1), ("MES", t2, f2, p2)):
            if tl is None:
                dist, ats = closest_approach(ff, pp, side)
                if dist is not None:
                    L(f"{'':>{len(name)}}   laggard {lg} max reach: {dist:.2f} short of "
                          f"{pp} @ {ats} (age {age_min(ats, now):.0f}m)")
                    # laggard test-and-fail candidate: other ticker swept, this one
                    # reached within 25% of its depletion threshold and failed
                    other_swept = (t2 if lg == "MNQ" else t1) is not None
                    gate = 0.25 * DEPLETE[lg][tier]
                    if other_swept and dist <= gate:
                        add_card("CANDIDATE laggard-fail", lg, name, pp, side, ats, tier,
                                 f" | reach {dist:.2f} short (25%-of-thr gate {gate:.2f}: QUALIFIES)")

    # thesis.md §2.1b/§7: replace the S3 loop's SESSION-SCOPED day/week-tier candidates with
    # a wide (SMT_LOOKBACK_HOURS) re-scan — additive/structural only, does not touch
    # bundle.lines or S3's own rendered text (see _wide_day_week_smt_scan's docstring).
    # Session-tier candidates from the S3 loop above are left exactly as they were.
    bundle.smt_candidates = (
        [c for c in bundle.smt_candidates if c.get("tier") not in ("day", "week")]
        + _wide_day_week_smt_scan(levels, wide_cache))

    # thesis.md §2.1 (2026-08-15): running-extreme tier promotion for session-tier SMT
    # candidates — promoted (meaningful, P2-eligible) only when the level qualifies as the
    # running day/week extreme for BOTH assets (each at its own reference time); a level
    # that is only one asset's running extreme stays a session-tier pool for the pair.
    # Mixed day/week qualification resolves conservatively to day.
    for c in bundle.smt_candidates:
        if c.get("tier") != "session":
            continue
        _pm = (bundle.promoted_session_levels.get("MNQ") or {}).get(c.get("level"))
        _pe = (bundle.promoted_session_levels.get("MES") or {}).get(c.get("level"))
        if _pm and _pe:
            c["tier"] = _pm if _pm == _pe else "day"
            c["meaningful"] = True
            c["promoted_from"] = "session"

    # plan 14 Task 3: count currently P1/P2-eligible items (mature P1 swept levels on either
    # ticker with >=1 non-None HTF close, plus meaningful P2 SMT candidates). Computed AFTER
    # htf_close_status and smt_candidates are populated. Additive field, no L(...).
    _mature_p1 = 0
    for _tkr in ("MNQ", "MES"):
        _status = bundle.htf_close_status.get(_tkr, {})
        _swept = bundle.swept_at.get(_tkr, {})
        for _name, _tf_map in _status.items():
            if _name not in levels.get(_tkr, {}):
                continue          # synthetic daily_mid/weekly_mid (Fix 3) are P4 pivots, not P1 levels
            if _swept.get(_name) is None:
                continue
            if any((_tf_map or {}).get(_tf) is not None for _tf in ("1h", "4h")):
                _mature_p1 += 1
    _mature_p2 = sum(1 for c in bundle.smt_candidates if c.get("meaningful"))
    bundle.mature_evidence_count = _mature_p1 + _mature_p2

    # plan 15 Task 5: code-SUGGESTED exhaustion per SMT candidate — tier-relative stretch of
    # the swept (lagger) ticker's price since the SMT fired, normalized by its avg 1h range.
    # Additive field on each candidate; a model-OVERRIDABLE hint, never a hard gate (the model
    # may agree or disagree via the P2 `exhausted` override — thesis.md §2.1c). Computed here
    # (not in the S3 loop) so avg_range_1h is already populated.
    for c in bundle.smt_candidates:
        c["suggested_exhausted"] = False
        c["stretch_since_fire"] = None
        tkr = c.get("swept_ticker")
        ar = (bundle.avg_range_1h or {}).get(tkr)
        fired_at = c.get("swept_at")
        if not isinstance(ar, (int, float)) or ar <= 0 or fired_at is None:
            continue
        closes = data[tkr]["close"]
        fire_px = closes.asof(fired_at)
        if fire_px is None or pd.isna(fire_px):
            continue
        stretch = abs(float(closes.iloc[-1]) - float(fire_px)) / ar
        c["stretch_since_fire"] = round(stretch, 3)
        c["suggested_exhausted"] = stretch > SMT_SHELF_LIFE.get(c.get("tier"), float("inf"))

    # thesis.md §2.1b: P2/SMT nesting suppression, no grandfather exception (see
    # _p2_nesting_suppression's own docstring).
    bundle.suppressed_p2_sites = _p2_nesting_suppression(bundle)

    # thesis.md §2.1c: per-item equilibrium-staleness suggestion for mature P1 items.
    bundle.p1_equilibrium_stale = _p1_equilibrium_staleness(bundle, data)

    # thesis.md §3a: near-maturity pre-confirmation candidates. Computed here (not earlier) so
    # htf_close_status, suppressed_p1_levels, smt_candidates (incl. suggested_exhausted), and
    # avg_range_1h/4h are all already populated — every input the check needs.
    bundle.near_maturity_candidates = _near_maturity_candidates(bundle, data, now)

    L("\n## S3b CANDIDATE ITEM CARDS (precomputed scoring inputs; multipliers per decisions/next-move.md §2)")
    L("score = tier_weight × session_side × alignment × freshness × whipsaw = base × session_side × alignment × whipsaw")
    L("Copy tier w and freshness from the card — do NOT recompute the exponent. session_side/")
    L("alignment/whipsaw are read-dependent: take them from the next-move.md tables.")
    L("REMINDER: whipsaw ×0.5 applies ONLY to intraday STRUCTURE items inside 09:15-11:30;")
    L("level sweeps/SMTs are NOT structure items (their whipsaw = 1.0).")
    L("Cards cover swept fixed levels (freshness = sweep time) and qualifying laggard-fail")
    L("candidates (freshness = failure time); items >6h old omitted (spent). Dynamic-extreme")
    L("events and continuation items are not carded — compute their freshness from S4/S5 times.")
    if cards:
        for line in cards:
            L(line)
    else:
        L("(no candidate items within the 6h window)")

    L("\n## S4 EQUILIBRIUM (1m closes vs RUNNING mids, current session; weekly mid = ENGINE anchor)")
    for tkr, df in data.items():
        sess_now = sess_cache[tkr]
        m1 = df["close"].resample("1min").last()
        dmid = (sess_now["high"].resample("1min").max().cummax()
                + sess_now["low"].resample("1min").min().cummin()) / 2
        wkf = df.loc[wk_anchor:]
        wmid = ((wkf["high"].resample("1min").max().cummax()
                 + wkf["low"].resample("1min").min().cummin()) / 2).loc[sess_now.index[0]:]
        for label, mid in (("daily", dmid), ("weekly[engine anchor]", wmid)):
            j, flips = mid_cross_table(m1.reindex(mid.index), mid)
            L(f"\n{tkr} {label} mid crosses (last {len(flips)}):")
            for ts, row in flips.iterrows():
                L(f"  {ts}  close {row['close']:.2f} {'ABOVE' if row['above'] else 'below'} mid {row['mid']:.2f}")
            for a, b, lbl in ((sess_now.index[0], ckpt, "session->ckpt"),
                              (ckpt - pd.Timedelta(hours=6), ckpt, "6h->ckpt"),
                              (ckpt, now, "ckpt->now")):
                seg = j.loc[a:b]
                if len(seg):
                    L(f"  acceptance {lbl}: {seg['above'].mean() * 100:.0f}% of closes above (n={len(seg)})")

    L("\n## S5 STRUCTURE BARS (MNQ; 4hr/1hr are 18:00-ET-SESSION-anchored = CME/TradingView convention)")
    df = data["MNQ"]
    L("\nTrue-Day bars (rows = session open time):")
    tds_series = pd.Series((df.index + pd.Timedelta(hours=7)).date, index=df.index)
    rows = []
    for d in all_tds:
        s = df[tds_series.values == d]
        if len(s) == 0:
            continue
        rows.append((s.index[0], float(s['open'].iloc[0]), float(s['high'].max()),
                     float(s['low'].min()), float(s['close'].iloc[-1])))
    L(pd.DataFrame(rows, columns=["open_ts", "open", "high", "low", "close"]).to_string(index=False))
    h4 = ohlc(df, "4h")
    L("\n4hr bars (18:00-ET-session-anchored), last 24:")
    L(h4.tail(24).to_string())
    h1 = ohlc(df, "1h")
    L("\n1hr bars, last 24:")
    L(h1.tail(24).to_string())
    L("\n30m bars, last 16:")
    L(ohlc(df, "30min").tail(16).to_string())
    L("\n15m bars, last 16:")
    L(ohlc(df, "15min").tail(16).to_string())
    L(f"\n5m bars, checkpoint {ckpt} -> now:")
    L(ohlc(df.loc[ckpt - pd.Timedelta(minutes=5):], "5min").loc[ckpt:].to_string())

    if hist["MNQ"] is not None and hist["MES"] is not None:
        L("\n## S5b LONG-HORIZON CONTEXT (full history truncated at now; True-Day daily bars + trade-week weekly bars)")
        for tkr in ("MNQ", "MES"):
            # plan 15 Task 1: reuse the long_horizon aggregation computed once above (same
            # `<= now` slice, same days[-60:] cap) instead of re-scanning hist here. The
            # rendered daily/weekly tables stay byte-identical (same rows, same open/high/
            # low/close columns, same dtab.groupby weekly agg); close_high/close_low are
            # dropped from the render, used only by the prev3-7/prev2-3_week named levels.
            lh = long_horizon.get(tkr) or {"daily": [], "weekly": []}
            rows = [(r["trade_date"], r["open"], r["high"], r["low"], r["close"])
                    for r in lh["daily"]]
            dtab = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"])
            L(f"\n{tkr} daily (True-Day) bars, last {len(dtab)} sessions:")
            L(dtab.to_string(index=False))
            dtab["iso"] = dtab["trade_date"].map(lambda d: d.isocalendar()[:2])
            wk = dtab.groupby("iso").agg(
                start=("trade_date", "first"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"))
            L(f"\n{tkr} weekly bars (trade weeks), last {min(len(wk), 12)}:")
            L(wk.tail(12).to_string(index=False))
            # plan 14 Task 8: daily/weekly extreme rows for audit-only cross-family confluence
            # tagging (S9), now read from the same long_horizon cache (no third pass).
            bundle.historical_extremes[tkr] = {
                "daily": [(r["trade_date"], float(r["high"]), float(r["low"]))
                          for r in lh["daily"]],
                "weekly": [(r["week_start"], float(r["high"]), float(r["low"]))
                           for r in lh["weekly"]],
            }

    L("\n## S6 FVGs (both tickers, completed bars only; visited = 1s tape re-entered zone after the 3rd bar's OPEN label — daily.py convention)")
    for tkr, dft in data.items():
        h1t = ohlc(dft, "1h")
        h4t = ohlc(dft, "4h")
        for label, tf_norm, bars in ((f"{tkr} 1hr", "1h", h1t.iloc[:-1]),
                                     (f"{tkr} 4hr", "4h", h4t.iloc[:-1])):
            recent = bars[bars.index >= now - pd.Timedelta(days=10)]
            for ts, kind, lo, hi, third_ts in fvgs(recent):
                seg = dft[dft.index > third_ts]
                hits = seg[(seg["low"] <= hi) & (seg["high"] >= lo)] if len(seg) else seg
                visited_at = hits.index[0] if len(hits) else None
                touched = visited_at is not None
                L(f"{label} {ts} {kind} zone {lo}-{hi} -> {'visited' if touched else 'UNVISITED'}")
                # plan 17 Fix 2: held/violated verdict for a VISITED zone, reusing the same
                # _htf_close_status() the named levels use — a bull zone is VIOLATED when a
                # qualifying HTF bar CLOSED below its LOW edge since the visit (side="below"
                # -> beyond=True), else HELD; a bear zone symmetrically on its HIGH edge. 1h
                # verdict preferred, 4h fallback; both immature -> None (too recent).
                fill_verdict = None
                if visited_at is not None:
                    edge, vside = (lo, "below") if kind == "bull" else (hi, "above")
                    st = _htf_close_status(dft, visited_at, price=edge, side=vside, now=now)
                    r = st["1h"] if st.get("1h") is not None else st.get("4h")
                    if r is not None:
                        fill_verdict = "violated" if r["beyond"] else "held"
                # 2026-08-02: code-SUGGESTED exhaustion for a filled FVG, same shape as the
                # SMT candidates' stretch_since_fire/suggested_exhausted above -- stretch of
                # price since the FILL (visited_at), not the zone's own formation, normalized
                # by avg_range_1h (P5 has no tier, so a single FVG_SHELF_LIFE applies). A
                # model-OVERRIDABLE hint via the existing P5 `exhausted` field, never a hard
                # gate.
                stretch_since_visit = None
                suggested_exhausted = False
                if visited_at is not None:
                    ar = (bundle.avg_range_1h or {}).get(tkr)
                    if isinstance(ar, (int, float)) and ar > 0:
                        visit_px = dft["close"].asof(visited_at)
                        if visit_px is not None and not pd.isna(visit_px):
                            stretch_since_visit = round(
                                abs(float(dft["close"].iloc[-1]) - float(visit_px)) / ar, 3)
                            suggested_exhausted = stretch_since_visit > FVG_SHELF_LIFE
                # plan 15 Task 4: additive P5-fill candidate — no change to the rendered S6
                # line above. `id` is the exact S6 identifier (asset + tf label + ts + kind)
                # so the model copies it verbatim as the evidence `level`; the bull/bear kind
                # in it is what _fvg_side derives the sign from.
                bundle.fvg_zones.append({
                    "id": f"{label} {ts} {kind}", "asset": tkr, "tf": tf_norm,
                    "ts": ts, "kind": kind, "lo": lo, "hi": hi, "visited": touched,
                    "visited_at": visited_at, "fill_verdict": fill_verdict,
                    "stretch_since_visit": stretch_since_visit,
                    "suggested_exhausted": suggested_exhausted,
                })

    L("\n## S7 CHECKPOINT SNAPSHOT")
    for tkr, dft in data.items():
        m1 = dft["close"].resample("1min").last().dropna()
        L(f"{tkr} close at checkpoint {ckpt}: {m1.asof(ckpt)} | at now: {float(dft['close'].iloc[-1])}")
    L("\nReminders (facts end here — decisions are yours):")
    L("- SMT fire adjudication, votes, correlation audit, ledgers, vetoes: NOT computed here.")
    L("- Week extremes/mid use the ENGINE anchor printed in S0 (equilibrium.md pins this).")
    L("- Dynamic day/week extremes and re-arm/depart states must be reasoned from S1/S5 times.")
    L("- Depletion uses exact per-ticker engine tables; boundary is >= (depleted at exactly thr).")

    return bundle


def _load_hist(hist_dir: str, tkr: str) -> pd.DataFrame:
    hp = os.path.join(hist_dir, f"{tkr}_1s_slice.parquet")
    if not os.path.exists(hp):
        hp = os.path.join(hist_dir, f"{tkr}_1s.parquet")
    return load(hp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--ath-mnq", type=float, default=None)
    ap.add_argument("--ath-mes", type=float, default=None)
    ap.add_argument("--hist-dir", default=None,
                    help="Optional dir with full {MNQ,MES}_1s.parquet for the long-horizon "
                         "daily/weekly tables (S5b). Data is truncated at 'now' — no lookahead.")
    args = ap.parse_args()

    mnq_df = load(os.path.join(args.dir, "MNQ_1s_slice.parquet"))
    mes_df = load(os.path.join(args.dir, "MES_1s_slice.parquet"))
    hist_mnq = _load_hist(args.hist_dir, "MNQ") if args.hist_dir else None
    hist_mes = _load_hist(args.hist_dir, "MES") if args.hist_dir else None

    bundle = compute_facts(mnq_df, mes_df, ath_mnq=args.ath_mnq, ath_mes=args.ath_mes,
                           hist_mnq=hist_mnq, hist_mes=hist_mes)
    # end="" — render_facts_text already terminates with the trailing newline that
    # the original per-print stdout carried, so the byte stream is identical.
    print(render_facts_text(bundle), end="")


if __name__ == "__main__":
    main()
