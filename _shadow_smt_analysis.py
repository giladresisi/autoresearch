# _shadow_smt_analysis.py — THROWAWAY multi-day shadow analysis (NOT part of the plan; unstaged).
#
# Examines the EFFECT of adverse-run invalidation + fulfillment across several regime-diverse
# days WITHOUT merging anything into the trading strategy. Invalidation/fulfillment are pure
# detect_state observables (not read by entry/exit), so each day's regression produces identical
# trades/P&L (shadow) while the trails/flags expose the signal lifecycle. This reads, per day:
#   - the actual smt_invalidations.json trail (producer threshold) + smts.json detect_state,
#   - every smt-div FIRE from events_1s.jsonl,
#   - the 1s MNQ close series (for the false-positive recovery test + offline threshold sweep).
#
# Because changing INVALIDATE_PTS cannot alter fires/fulfillment/trades (invalidation never feeds
# re-arm), the day-threshold sweep is computed OFFLINE from fires + price — one regression per day.
# The offline model is cross-checked against the real trail at the producer threshold (match %).
#
# Usage:  uv run python _shadow_smt_analysis.py [date ...]   (defaults to the 8-day set below)

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import sys
import numpy as np
import pandas as pd

from smt_detect import FULFILL_PTS_MNQ, INVALIDATE_PTS_MNQ, _level_class

DEFAULT_DATES = ["2026-05-06", "2026-05-12", "2026-05-20", "2026-05-27",
                 "2026-05-29", "2026-06-03", "2026-06-05", "2026-06-10"]
DATES = sys.argv[1:] or DEFAULT_DATES

MNQ_1S = Path(r"C:/Users/gilad/projects/auto-co-trader/global/general/main/MNQ_1s.parquet")
SESS = Path("regression/sessions")

# Day-tier INVALIDATE_PTS values to sweep (week/session held at their defaults).
DAY_SWEEP = [20.0, 30.0, 35.0, 40.0, 50.0, 65.0]
RECOVER_MIN = 180  # minutes after an invalidation to look for the thesis still fulfilling


def _latest_run(date: str) -> "Path | None":
    # Pick the most RECENTLY MODIFIED run (the current batch), NOT max-by-name: an old run can
    # have a later wall-clock name yet predate the invalidation feature (no trail / no flags).
    base = SESS / date
    runs = [p for p in base.glob("*") if (p / "smts.json").exists()] if base.exists() else []
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


# Load the full 1s MNQ close series once, in ET.
_mnq = pd.read_parquet(MNQ_1S)
if _mnq.index.tz is None:
    _mnq.index = _mnq.index.tz_localize("UTC")
_mnq = _mnq.tz_convert("America/New_York") if _mnq.index.tz != "America/New_York" else _mnq
_mnq.index = _mnq.index.tz_convert("America/New_York")
_close = _mnq["Close"]
_idx = _close.index
_vals = _close.to_numpy()
_ts_ns = _idx.view("int64")


def _regime(date: str):
    d = pd.Timestamp(date).date()
    g = _mnq[(_idx.date == d)]
    import datetime as dt
    rth = g[(g.index.time >= dt.time(9, 30)) & (g.index.time <= dt.time(16, 0))]
    if len(rth) < 600:
        return ("?", 0.0, 0.0)
    net = rth["Close"].iloc[-1] - rth["Open"].iloc[0]
    rng = rth["High"].max() - rth["Low"].min()
    dr = abs(net) / rng if rng else 0.0
    reg = "CHOP" if dr < 0.33 else ("UP" if net > 0 else "DOWN")
    return (reg, float(net), float(dr))


def _fires_from_events(run: Path):
    """Every level-SMT fire emitted in the run (time, ref_name, direction, fire_close, tier)."""
    out = []
    f = run / "events_1s.jsonl"
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("kind") != "smt-div" or o.get("type") in ("fill_a", "fill_b"):
            continue
        rn = o.get("ref_name", "")
        kind, tier = _level_class(rn)
        out.append({
            "time": o["time"], "ts": pd.Timestamp(o["time"]),
            "ref_name": rn, "tier": tier, "kind": kind,
            "direction": "short" if o.get("side") == "bearish" else "long",
            "fire_close": float(o.get("price")),
        })
    return out


def _slice_after(ts: pd.Timestamp, horizon_min=None):
    lo = np.searchsorted(_ts_ns, ts.value, side="right")
    hi = len(_vals)
    if horizon_min is not None:
        hi = np.searchsorted(_ts_ns, (ts + pd.Timedelta(minutes=horizon_min)).value, side="right")
    return _idx[lo:hi], _vals[lo:hi]


def _first_cross(vals, thr, above):
    hit = np.argmax(vals >= thr) if above else np.argmax(vals <= thr)
    ok = (vals[hit] >= thr) if above else (vals[hit] <= thr)
    return hit if ok and len(vals) else None


def _simulate(fire, inv_pts):
    """Offline terminal invalidation for one fire under a given INVALIDATE_PTS (tier-correct for
    week/session; `inv_pts` overrides the day tier). Returns (invalidated, inval_ts, recovered)."""
    tier = fire["tier"]
    fc = fire["fire_close"]
    ful = FULFILL_PTS_MNQ.get(tier, FULFILL_PTS_MNQ["session"])
    inv = inv_pts if tier == "day" else INVALIDATE_PTS_MNQ.get(tier, INVALIDATE_PTS_MNQ["session"])
    ts_after, v = _slice_after(fire["ts"])
    if not len(v):
        return (False, None, False)
    if fire["direction"] == "short":
        ic = _first_cross(v, fc + inv, above=True)     # adverse = up
        fcx = _first_cross(v, fc - ful, above=False)   # favorable = down
    else:
        ic = _first_cross(v, fc - inv, above=False)    # adverse = down
        fcx = _first_cross(v, fc + ful, above=True)    # favorable = up
    invalidated = ic is not None and (fcx is None or ic < fcx)
    if not invalidated:
        return (False, None, False)
    inval_ts = ts_after[ic]
    # "Premature" (false-positive proxy): after being invalidated, does the SMT's ORIGINAL thesis
    # still play out — i.e. price reaches the FULFILLMENT target in its favor within RECOVER_MIN?
    # (short → close <= fc - FULFILL; long → close >= fc + FULFILL.) This mirrors the real
    # detect_state "inval+ful" category (invalidated then later fulfilled).
    rts, rv = _slice_after(inval_ts, horizon_min=RECOVER_MIN)
    if len(rv):
        recovered = bool((rv <= fc - ful).any()) if fire["direction"] == "short" \
            else bool((rv >= fc + ful).any())
    else:
        recovered = False
    return (True, inval_ts, recovered)


print("=" * 96)
print(f"SHADOW SMT ANALYSIS — invalidation + fulfillment across {len(DATES)} days (P&L-independent)")
print("=" * 96)

per_day = []
agg_partition = Counter()
sweep_tot = {d: Counter() for d in DAY_SWEEP}      # day-tier: invalidated / recovered counts
model_match = [0, 0]                                # offline@producer vs actual trail (day-tier)

for date in DATES:
    run = _latest_run(date)
    reg, net, dr = _regime(date)
    if run is None:
        print(f"\n### {date}  [{reg}]  — NO RUN DIR (skipped)")
        continue
    trail = json.loads((run / "smt_invalidations.json").read_text(encoding="utf-8")) \
        if (run / "smt_invalidations.json").exists() else []
    smts = json.loads((run / "smts.json").read_text(encoding="utf-8"))
    detect = smts.get("detect_state", smts)
    fires = _fires_from_events(run)

    # --- fulfillment/invalidation partition from REAL detect_state (final flags) ---
    part = Counter()
    by_tier_part = defaultdict(Counter)
    for k, st in detect.items():
        if "|" not in k or not isinstance(st, dict) or not st.get("fired"):
            continue
        tier = _level_class(k.split("|")[0])[1]
        if st.get("invalidated") and not st.get("fulfilled"):
            cat = "invalidated"
        elif st.get("fulfilled") and not st.get("invalidated"):
            cat = "fulfilled"
        elif st.get("fulfilled") and st.get("invalidated"):
            cat = "inval+ful"     # cross-bar (invalidated then later fulfilled) — fixed only
        else:
            cat = "live"
        part[cat] += 1
        by_tier_part[tier][cat] += 1
    agg_partition.update(part)

    # --- recovery analysis on the ACTUAL trail: did the thesis still fulfill after invalidation? ---
    rec_yes = rec_no = 0
    for e in trail:
        ts = pd.Timestamp(e["time"])
        fc = e.get("fire_mnq_close", e.get("fire_close"))
        ful = FULFILL_PTS_MNQ.get(e["tier"], FULFILL_PTS_MNQ["session"])
        rts, rv = _slice_after(ts, horizon_min=RECOVER_MIN)
        if not len(rv):
            rec_no += 1
            continue
        back = (rv <= fc - ful).any() if e["direction"] == "short" else (rv >= fc + ful).any()
        rec_yes += int(bool(back))
        rec_no += int(not bool(back))

    # --- offline day-threshold sweep + model cross-check (day-tier fires) ---
    # Restrict to FIXED day-tier levels (prev*_day_*): each fires once, so the offline single-
    # lifecycle model is EXACT. Dynamic day_high/day_low re-fire on re-arm — excluded here (their
    # behavior is in the real partition above). The motivating correctives are fixed day-tier.
    actual_day_inval_keys = {(e["ref_name"], e["direction"]) for e in trail
                             if e["tier"] == "day" and _level_class(e["ref_name"])[0] == "fixed"}
    day_fires = [f for f in fires if f["tier"] == "day" and f["kind"] == "fixed"]
    # dedup fires to one per (ref,direction) keeping earliest (mirrors fixed single-fire; for
    # dynamic this under-counts re-fires but is adequate for the threshold-tradeoff trend).
    seen = {}
    for f in sorted(day_fires, key=lambda x: x["ts"]):
        seen.setdefault((f["ref_name"], f["direction"]), f)
    uniq_day = list(seen.values())
    for dthr in DAY_SWEEP:
        inv_keys = set()
        rec = 0
        for f in uniq_day:
            invd, _, recovered = _simulate(f, dthr)
            if invd:
                inv_keys.add((f["ref_name"], f["direction"]))
                rec += int(recovered)
        sweep_tot[dthr]["inval"] += len(inv_keys)
        sweep_tot[dthr]["recovered"] += rec
        if dthr == INVALIDATE_PTS_MNQ["day"]:
            model_match[0] += len(inv_keys & actual_day_inval_keys)
            model_match[1] += len(actual_day_inval_keys)

    reg_tag = f"{reg:4s} net={net:+8.1f} dr={dr:.2f}"
    print(f"\n### {date}  [{reg_tag}]   run={run.name}")
    print(f"  trail events={len(trail):3d}  fired-level-SMTs partition: "
          + "  ".join(f"{k}={part[k]}" for k in ('fulfilled', 'invalidated', 'inval+ful', 'live')))
    print(f"  by tier: " + " | ".join(
        f"{t}:" + ",".join(f"{c}={by_tier_part[t][c]}" for c in ('fulfilled', 'invalidated', 'inval+ful', 'live'))
        for t in ('week', 'day', 'session') if by_tier_part[t]))
    print(f"  trail-invalidation recovery within {RECOVER_MIN}m: recovered(premature?)={rec_yes}  stayed-dead={rec_no}")
    per_day.append((date, reg, len(trail), part, rec_yes, rec_no))

# ---------------------------------------------------------------------------
print("\n" + "=" * 96)
print("AGGREGATE")
print("=" * 96)
print("\nFulfilled/invalidated partition over ALL fired level-SMTs (real detect_state):")
for k in ("fulfilled", "invalidated", "inval+ful", "live"):
    print(f"   {k:11s}: {agg_partition[k]}")

print("\nBy regime — trail invalidation recovery (premature vs stayed-dead):")
reg_rec = defaultdict(lambda: [0, 0])
for date, reg, n, part, ry, rn in per_day:
    reg_rec[reg][0] += ry
    reg_rec[reg][1] += rn
for reg, (ry, rn) in sorted(reg_rec.items()):
    tot = ry + rn
    print(f"   {reg:5s}: recovered={ry:3d}  stayed-dead={rn:3d}  premature-rate={ry/tot:.0%}" if tot else f"   {reg}: -")

print("\nFIXED day-tier (prev*_day_*) INVALIDATE_PTS SWEEP (offline; week/session held at default).")
print(f"'recovered' = the invalidated thesis still reached its fulfillment target within {RECOVER_MIN}m")
print("= likely-premature invalidation. Want: few invalidations AND low premature%, i.e. high kept-dead.")
print(f"  {'day_thr':>8} | {'invalidated':>11} | {'recovered':>9} | {'premature%':>10} | {'kept-dead':>9}")
for d in DAY_SWEEP:
    iv = sweep_tot[d]["inval"]
    rc = sweep_tot[d]["recovered"]
    pct = (rc / iv) if iv else 0.0
    print(f"  {d:8.0f} | {iv:11d} | {rc:9d} | {pct:9.0%} | {iv-rc:9d}")

mm = f"{model_match[0]}/{model_match[1]}" if model_match[1] else "n/a"
print(f"\nOffline-model cross-check @ producer day_thr={INVALIDATE_PTS_MNQ['day']:.0f} "
      f"(offline day-tier invalidations that match the real trail): {mm}")
print("\nNOTE: P&L/trades are identical to baseline on every day (invalidation is shadow-only).")
