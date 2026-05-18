"""
Analyse all 30d 1m regression trades and compute the P&L effect of each
candidate entry filter:

  F1 – Hypothesis churn   : >=4 new-hypothesis events in the 60 min before entry
  F2 – Daily-mid chop     : >=2 trend-broken(daily_mid) events in the 30 min before entry
  F3 – Double-premium up  : direction=up AND daily_zone=premium AND weekly_zone=premium
  F4 – Consecutive stops  : >=2 stopped-out trades in same direction since last
                             profitable exit or direction flip
  F5 – Early session      : entry time before 10:00 ET

For each filter we report:
  • #trades fired (would have been skipped)
  • losses saved  (skipped trades with negative PnL)
  • profits lost  (skipped trades with positive PnL)
  • net effect    = losses_saved - profits_lost
"""

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-04-06", "2026-05-15")]
REG_DIR = Path("data/regression")
DAILY_MID_LEVELS = {"daily_mid", "day_mid", "daily_midpoint"}

# Threshold constants
F1_HYP_WINDOW_MIN  = 60   # look-back window in minutes
F1_HYP_THRESHOLD   = 4    # number of hypothesis events that signals churn
F2_MID_WINDOW_MIN  = 30
F2_MID_THRESHOLD   = 2
F4_CONSEC_THRESHOLD = 2


def pt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ─── load all days ────────────────────────────────────────────────────────────

days_data = []  # list of (date, [trades], [events])
for d in DATES:
    base = REG_DIR / d
    if not (base / "trades.tsv").exists():
        continue
    trades = list(csv.DictReader((base / "trades.tsv").open(encoding="utf-8"), delimiter="\t"))
    if not trades:
        continue
    raw_evts = [
        json.loads(l)
        for l in (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    for e in raw_evts:
        e["_t"] = pt(e.get("time", "2000-01-01T00:00:00-04:00"))
    raw_evts.sort(key=lambda e: e["_t"])
    days_data.append((d, trades, raw_evts))


# ─── per-trade filter evaluation ──────────────────────────────────────────────

# Each filter collects records: (date, entry_time_str, direction, pnl, fired: bool)
records = {f"f{i}": [] for i in range(1, 6)}

for date, trades, evts in days_data:
    for i, trade in enumerate(trades):
        entry_t   = pt(trade["entry_time"])
        direction = trade["direction"]
        pnl       = float(trade["pnl_dollars"])

        # events that occurred at or before this entry
        pre = [e for e in evts if e["_t"] <= entry_t]

        # ── last hypothesis at entry time ──────────────────────────────────────
        hyp_evts = [e for e in pre if e["kind"] == "new-hypothesis"]
        last_hyp  = hyp_evts[-1] if hyp_evts else {}
        last_dr   = last_hyp.get("direction_reason", {})

        # ── F1: hypothesis churn ───────────────────────────────────────────────
        cutoff_60 = entry_t - timedelta(minutes=F1_HYP_WINDOW_MIN)
        hyp_in_60 = sum(
            1 for e in pre
            if e["kind"] == "new-hypothesis" and e["_t"] >= cutoff_60
        )
        f1 = hyp_in_60 >= F1_HYP_THRESHOLD
        records["f1"].append((date, trade["entry_time"], direction, pnl, f1))

        # ── F2: daily-mid chop ─────────────────────────────────────────────────
        cutoff_30 = entry_t - timedelta(minutes=F2_MID_WINDOW_MIN)
        mid_brkn  = sum(
            1 for e in pre
            if e["kind"] == "trend-broken"
            and e.get("level_name", "") in DAILY_MID_LEVELS
            and e["_t"] >= cutoff_30
        )
        f2 = mid_brkn >= F2_MID_THRESHOLD
        records["f2"].append((date, trade["entry_time"], direction, pnl, f2))

        # ── F3: double-premium up ──────────────────────────────────────────────
        f3 = (
            direction == "up"
            and last_dr.get("daily_zone")  == "premium"
            and last_dr.get("weekly_zone") == "premium"
        )
        records["f3"].append((date, trade["entry_time"], direction, pnl, f3))

        # ── F4: consecutive stops in same direction ────────────────────────────
        consec = 0
        for j in range(i - 1, -1, -1):
            prev = trades[j]
            prev_pnl = float(prev["pnl_dollars"])
            if prev_pnl > 0:
                break                          # profitable exit resets streak
            if prev["direction"] != direction:
                break                          # direction flip resets streak
            if prev["exit_reason"] == "stopped-out":
                consec += 1
        f4 = consec >= F4_CONSEC_THRESHOLD
        records["f4"].append((date, trade["entry_time"], direction, pnl, f4))

        # ── F5: early session (before 10:00 ET) ───────────────────────────────
        f5 = entry_t.hour < 10
        records["f5"].append((date, trade["entry_time"], direction, pnl, f5))


# ─── summary table ────────────────────────────────────────────────────────────

filter_names = {
    "f1": f"Hyp churn (>={F1_HYP_THRESHOLD} hyp in {F1_HYP_WINDOW_MIN}min)",
    "f2": f"Daily-mid chop (>={F2_MID_THRESHOLD} cross in {F2_MID_WINDOW_MIN}min)",
    "f3": "Double-premium up",
    "f4": f"Consec stops (>={F4_CONSEC_THRESHOLD} same dir)",
    "f5": "Early session (<10:00)",
}

TOTAL_PNL = sum(float(t["pnl_dollars"]) for _, trades, _ in days_data for t in trades)

print(f"\nTotal 30d PnL (all trades): ${TOTAL_PNL:,.2f}\n")
hdr = f"{'Filter':<40} {'Fired':>6} {'#Win':>5} {'#Los':>5} {'Lost$':>10} {'Saved$':>10} {'Net':>10}"
print(hdr)
print("-" * len(hdr))

for fk, name in filter_names.items():
    fired = [(d, t, dr, p) for d, t, dr, p, f in records[fk] if f]
    wins  = [(d, t, dr, p) for d, t, dr, p in fired if p > 0]
    loses = [(d, t, dr, p) for d, t, dr, p in fired if p < 0]
    profits_lost  = sum(p for _, _, _, p in wins)
    losses_saved  = sum(-p for _, _, _, p in loses)
    net = losses_saved - profits_lost
    print(
        f"{name:<40} {len(fired):>6} {len(wins):>5} {len(loses):>5}"
        f" {-profits_lost:>10.2f} {losses_saved:>10.2f} {net:>+10.2f}"
    )

print("-" * len(hdr))

# ─── per-filter trade detail ───────────────────────────────────────────────────

for fk, name in filter_names.items():
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"{'='*70}")
    fired = [(d, t, dr, p) for d, t, dr, p, f in records[fk] if f]
    for d, t, dr, p in fired:
        tag = "WIN" if p > 0 else "LOS"
        print(f"  {tag}  {d}  {t[11:16]}  {dr:4s}  {p:+8.2f}")
    if not fired:
        print("  (none)")
