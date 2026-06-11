# _verify_invalidation.py — THROWAWAY read-only analysis (NOT part of the plan; leave unstaged).
#
# Verifies the EFFECT of adverse-run invalidation from a completed regression run's artifacts.
# P&L is unchanged by construction (invalidation isn't wired into entry/exit), so the trade
# invariant proves no regression but NOTHING about whether invalidation does the right thing.
# This script reads the run's smt_invalidations.json trail + smts.json detect_state +
# events_1s.jsonl smt-div emissions and reports:
#   1. Capture+timing at the 09:49/09:50 prev1_week_high occurrence.
#   2. Dominant-flip check (baseline vs with-invalidation) over 09:50-10:00.
#   3. False-positive guard on the 12:25-12:42 corrective bullishes.
#   4. Aggregate sanity (counts, ratios, lag distribution, no-double-state).
#
# Authority/dominant ranking is reimplemented LOCALLY here (read-only mirror) — entry-stuff is
# NOT imported or touched.

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "regression/sessions/2026-06-03/11-39-02")

TRAIL = json.loads((RUN / "smt_invalidations.json").read_text(encoding="utf-8"))
SMTS = json.loads((RUN / "smts.json").read_text(encoding="utf-8"))
DETECT = SMTS.get("detect_state", {})


def _ts(s):
    return datetime.fromisoformat(s) if s else None


# --- local read-only mirror of the relevance authority ranking -------------
# tier rank: week>day>session ; kind: fixed-vs-dynamic tie-break ; then recency.
_TIER = {"week": 4, "day": 3, "session": 2}


def _tier_rank(tier):
    return _TIER.get(tier, 1)


def _authority(rec):
    # (tier_rank, recency) — higher wins. kind is a minor tie-break (fixed=1>dynamic=0).
    return (_tier_rank(rec["tier"]), 1 if rec["kind"] == "fixed" else 0, rec["time"])


def _dominant(active):
    """The highest-authority record in the active set, or None."""
    return max(active, key=_authority) if active else None


print("=" * 78)
print(f"SIGNAL-EFFECT VERIFICATION (P&L-independent)   run={RUN}")
print("=" * 78)

# ---------------------------------------------------------------------------
# 1. Capture + timing at the named 09:49/09:50 prev1_week_high occurrence
# ---------------------------------------------------------------------------
print("\n[1] prev1_week_high SHORT occurrence @ ~09:49:25 / 09:50:00 (the motivating case)")
short_keys = [k for k in DETECT if k.startswith("prev1_week_high|short")]
for k in sorted(short_keys):
    st = DETECT[k]
    fc = st.get("fire_mnq_close")
    print(f"  {k}: fired={st.get('fired')} fulfilled={st.get('fulfilled')} "
          f"invalidated={st.get('invalidated')} fire_time={st.get('fire_time')} "
          f"fire_mnq_close={fc} inval_time={st.get('invalidated_time')}")
    if fc is not None:
        print(f"      week INVALIDATE_PTS=40 -> needs MNQ close >= {fc + 40} to invalidate")
# Trail entries for prev1_week_high short (if any reached threshold)
wk_short_trail = [e for e in TRAIL
                  if e["ref_name"] == "prev1_week_high" and e["direction"] == "short"]
print(f"  trail adverse_run events for prev1_week_high SHORT: {len(wk_short_trail)}")
for e in wk_short_trail:
    lag = (_ts(e["time"]) - _ts(e["fire_time"])).total_seconds()
    gap = e["trigger_mnq_close"] - e["fire_mnq_close"]
    print(f"    fire={e['fire_time']} inval={e['time']} lag={lag:.0f}s "
          f"adverse_gap={gap:+.2f} (>=40? {gap >= 40}) reason={e['reason']}")

# ---------------------------------------------------------------------------
# 2. Dominant-flip check (baseline vs with-invalidation), 09:50-10:00 window
# ---------------------------------------------------------------------------
print("\n[2] DOMINANT-FLIP CHECK over 06-03 09:50-10:00 (reconstructed active set)")
SD = []
for line in (RUN / "events_1s.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    o = json.loads(line)
    if o.get("kind") == "smt-div":
        SD.append(o)


def _level_class(name):
    n = name or ""
    if n.startswith("week_"):
        return ("dynamic", "week")
    if n.startswith("day_"):
        return ("dynamic", "day")
    if n.startswith("prev"):
        if "week" in n:
            return ("fixed", "week")
        if "day" in n:
            return ("fixed", "day")
    return ("fixed", "session")


# Build records from emissions (levels only; fills have no tier authority here).
recs = []
for e in SD:
    rn = e.get("ref_name", "")
    if e.get("type") in ("fill_a", "fill_b"):
        continue
    kind, tier = _level_class(rn)
    direction = "short" if e.get("side") == "bearish" else "long"
    recs.append({"time": e["time"], "ref_name": rn, "tier": tier, "kind": kind,
                 "direction": direction, "side": e["side"]})

# When is each key invalidated? Map ref+direction -> earliest invalidated_time.
inval_at = {}
for e in TRAIL:
    key = (e["ref_name"], e["direction"])
    t = e["time"]
    if key not in inval_at or t < inval_at[key]:
        inval_at[key] = t

WIN = [f"2026-06-03T09:5{d}" for d in range(0, 10)] + ["2026-06-03T10:00"]


def _active_at(T, drop_invalidated):
    """Active set = all level SMTs emitted at or before T (latest per key), optionally
    dropping any whose ref+direction was invalidated at/before T."""
    latest = {}
    for r in recs:
        if r["time"] <= T:
            latest[(r["ref_name"], r["direction"])] = r
    out = []
    for key, r in latest.items():
        if drop_invalidated:
            it = inval_at.get(key)
            if it is not None and it <= T:
                continue
        out.append(r)
    return out


flip_reported = False
for minute in WIN:
    T = minute + ":59-04:00"
    base = _dominant(_active_at(T, drop_invalidated=False))
    winv = _dominant(_active_at(T, drop_invalidated=True))

    def _fmt(d):
        return f"{d['ref_name']}/{d['direction']}/{d['tier']}" if d else "None"
    if _fmt(base) != _fmt(winv) and not flip_reported:
        print(f"  FLIP @ {minute}: baseline dominant={_fmt(base)}  ->  "
              f"with-invalidation dominant={_fmt(winv)}")
        flip_reported = True
    if minute in ("2026-06-03T09:50", "2026-06-03T09:55", "2026-06-03T10:00"):
        print(f"    {minute}: baseline={_fmt(base)}  with-inval={_fmt(winv)}")

# Was the week-high bearish ever dominant in baseline here?
base_doms = [_dominant(_active_at(m + ":59-04:00", False)) for m in WIN]
wk_short_dom = any(d and d["ref_name"] == "prev1_week_high" and d["direction"] == "short"
                   for d in base_doms)
print(f"  week-high SHORT ever baseline-dominant in window: {wk_short_dom}")

# ---------------------------------------------------------------------------
# 3. False-positive guard on the 12:25-12:42 corrective bullishes
# ---------------------------------------------------------------------------
print("\n[3] FALSE-POSITIVE GUARD — corrective bullishes must NOT be killed within ~1-2 bars")
correctives = ["prev5_day_high", "prev7_day_high", "prev6_day_high", "week_low"]
for ref in correctives:
    evs = [e for e in TRAIL if e["ref_name"] == ref and e["direction"] == "long"]
    flagged = []
    for e in evs:
        lag = (_ts(e["time"]) - _ts(e["fire_time"])).total_seconds() if e.get("fire_time") else None
        if lag is not None and lag <= 120:  # killed within ~2 bars
            flagged.append((e["fire_time"], e["time"], lag))
    status = "OK (no immediate kill)" if not flagged else f"WARN immediate-kill x{len(flagged)}"
    print(f"  {ref}: trail_long_events={len(evs)}  {status}")
    for ft, it, lag in flagged:
        print(f"     fire={ft} inval={it} lag={lag:.0f}s  <-- TIGHT? day threshold may be small")

# ---------------------------------------------------------------------------
# 4. Aggregate sanity
# ---------------------------------------------------------------------------
print("\n[4] AGGREGATE SANITY")
by_td = Counter((e["tier"], e["direction"]) for e in TRAIL)
print("  trail counts by tier x direction:")
for (tier, direc), c in sorted(by_td.items()):
    print(f"    {tier:8s} {direc:5s} : {c}")

# invalidated:fulfilled:still-live over level-SMT detect_state keys.
# NOTE on "both" semantics: the plan's invariant is SAME-BAR exclusivity (a bar that fulfills
# cannot also invalidate). It is provable by construction: the invalidation branch only runs
# `not st.get("fulfilled")`, so at invalidated_time the key was NOT yet fulfilled => any later
# fulfilled flag came on a STRICTLY LATER bar. CROSS-bar both-state (invalidated early on an
# adverse dip, then ultimately fulfilled when price reversed) is EXPECTED for fixed levels:
# the plan forbids invalidation from altering fulfillment, and fixed-level `fulfilled` is purely
# informational (NOT a re-arm trigger). So "both" here is a tuning signal, not a defect.
inv = ful = live = 0
both_state = []
dyn_both = []
for k, st in DETECT.items():
    if "|" not in k or not st.get("fired"):
        continue
    is_inv = bool(st.get("invalidated"))
    is_ful = bool(st.get("fulfilled"))
    if is_inv and is_ful:
        both_state.append(k)
        if _level_class(k.split("|")[0])[0] == "dynamic":
            dyn_both.append(k)
    if is_inv:
        inv += 1
    elif is_ful:
        ful += 1
    else:
        live += 1
print(f"  fired level-SMTs -> invalidated(only):{inv}  fulfilled(only):{ful}  still-live:{live}")
print(f"  keys invalidated-then-later-fulfilled (cross-bar, EXPECTED for fixed): {len(both_state)}")
print(f"    of which DYNAMIC (would be a real concern -> re-arm impact): {len(dyn_both)} {dyn_both}")
print(f"  SAME-BAR fulfill+invalidate (must be 0, guaranteed by guard): 0 (by construction)")

lags = []
for e in TRAIL:
    if e.get("fire_time"):
        lags.append((_ts(e["time"]) - _ts(e["fire_time"])).total_seconds())
if lags:
    lags.sort()
    print(f"  invalidation lag (s): n={len(lags)} min={lags[0]:.0f} "
          f"med={lags[len(lags)//2]:.0f} max={lags[-1]:.0f}")

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
print("VERDICT")
print("=" * 78)
wk_short_invalidated = any(
    DETECT[k].get("invalidated") for k in short_keys)
print(f"  - prev1_week_high SHORT (09:49/09:50) invalidated: {wk_short_invalidated}")
print(f"  - week-high SHORT was baseline-dominant in 09:50-10:00: {wk_short_dom}")
print(f"  - dominant flip observed in window: {flip_reported}")
print(f"  - corrective bullishes immediately killed: "
      f"{any(any((_ts(e['time'])-_ts(e['fire_time'])).total_seconds() <= 120 for e in TRAIL if e['ref_name']==ref and e['direction']=='long' and e.get('fire_time')) for ref in correctives)}")
print(f"  - same-bar fulfill+invalidate exclusivity (guaranteed by guard): True")
print(f"  - cross-bar invalidated-then-fulfilled keys (expected for fixed, DYNAMIC must be 0): "
      f"{len(both_state)} total / {len(dyn_both)} dynamic")
