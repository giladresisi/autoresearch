"""
Deep analysis:
  1. Pre-9:30 trades: exact minute distribution, why they lose
  2. Midday chop / consecutive streaks: market structure before entry
     - stop distance vs recent range
     - how "fresh" the swept level is (how many times price revisited it)
     - whether price was trending or ranging in the 20min before entry
"""

import pandas as pd
import numpy as np

# ── Load data ────────────────────────────────────────────────────────────────
df = pd.read_csv("trades.tsv", sep="\t")
df["entry_dt"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("America/New_York")
df["exit_dt"]  = pd.to_datetime(df["exit_time"],  utc=True).dt.tz_convert("America/New_York")
df["win"]      = df["pnl_dollars"] > 0
df["hour"]     = df["entry_dt"].dt.hour
df["minute"]   = df["entry_dt"].dt.minute
df["hm"]       = df["hour"] * 60 + df["minute"]

# Reconstruct stop price for stopped-out trades (exit_price IS the stop)
df["stop_price"] = df["exit_price"].where(df["exit_reason"] == "stopped-out", other=float("nan"))

# Load 1m data
mnq = pd.read_parquet("data/MNQ_1m.parquet")
if mnq.index.tz is None:
    mnq.index = mnq.index.tz_localize("America/New_York")
else:
    mnq.index = mnq.index.tz_convert("America/New_York")

SEP = "=" * 70

# ────────────────────────────────────────────────────────────────────────────
# PART 1: Pre-9:30 trades
# ────────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("PART 1: PRE-9:30 TRADES — EXACT MINUTE DISTRIBUTION")
print(SEP)

pre930 = df[df["hm"] < 570].copy()  # before 9:30
print(f"Total trades before 9:30: {len(pre930)}")
print()
for (h, m), g in pre930.groupby(["hour", "minute"]):
    wr = g["win"].mean()
    avg = g["pnl_dollars"].mean()
    print(f"  {h:02d}:{m:02d}  n={len(g):3d}  wr={wr:.3f}  avg_pnl=${avg:.2f}  "
          f"  up={len(g[g['direction']=='up'])}  down={len(g[g['direction']=='down'])}")

print()
print("Pre-9:30 by direction:")
for d, g in pre930.groupby("direction"):
    print(f"  {d:5s}  n={len(g):3d}  wr={g['win'].mean():.3f}  avg_pnl=${g['pnl_dollars'].mean():.2f}")

print()
print("Pre-9:30 exit reasons:")
for r, g in pre930.groupby("exit_reason"):
    print(f"  {r:30s}  n={len(g):3d}  wr={g['win'].mean():.3f}  avg_pnl=${g['pnl_dollars'].mean():.2f}")

# Specific: can a market-entry at 9:30 follow a pre-9:30 hypothesis?
# Look at: how many pre-9:30 stopped-out trades then saw a profitable 9:30+ trade same day?
print()
print("Pre-9:30 stop-outs — what happened next in the same session?")
pre930_stops = pre930[pre930["exit_reason"] == "stopped-out"]
# Get the session date for each
pre930_stops = pre930_stops.copy()
pre930_stops["date"] = pre930_stops["entry_dt"].dt.date
# Count what came after in our trade data
df["date"] = df["entry_dt"].dt.date
after_counts = []
for _, row in pre930_stops.iterrows():
    same_day_later = df[(df["date"] == row["date"]) & (df["hm"] >= 570)]
    if not same_day_later.empty:
        best = same_day_later["pnl_dollars"].max()
        worst = same_day_later["pnl_dollars"].min()
        after_counts.append({"had_later": True, "best": best, "won": best > 0})
    else:
        after_counts.append({"had_later": False, "best": 0, "won": False})
if after_counts:
    had_later = sum(1 for x in after_counts if x["had_later"])
    won_later = sum(1 for x in after_counts if x["won"])
    print(f"  pre-9:30 stop-outs: {len(pre930_stops)}")
    print(f"  had a later trade same session: {had_later}")
    print(f"  later session had at least one winner: {won_later}")


# ────────────────────────────────────────────────────────────────────────────
# PART 2: Market structure analysis for each trade
# Build features using lookback 1m bars:
#   - stop_distance: pts from entry to stop
#   - range_20m: 20-min H-L range before entry
#   - stop_vs_range: stop_distance / range_20m (tightness)
#   - revisits: how many times price crossed the stop level in 20m before entry
#   - trend_slope_5m: linear regression slope of 5m closes in 20m before entry
#   - bars_since_session_open: distance from session open
# ────────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("PART 2: COMPUTING MARKET STRUCTURE FEATURES FOR ALL TRADES")
print(SEP)

_LOOKBACK_BARS = 20  # 20 x 1m = 20min of context

records = []
for _, row in df.iterrows():
    et    = row["entry_dt"]
    ep    = float(row["entry_price"])
    direc = row["direction"]
    stop  = row["stop_price"]

    if pd.isna(stop):
        # Non-stop-out: estimate stop from pnl_points direction
        stop_dist = None
    else:
        stop_dist = abs(ep - float(stop))

    # 1m bars ending just before entry
    window = mnq[mnq.index < et].tail(_LOOKBACK_BARS)
    if len(window) < 5:
        records.append({**row, "stop_dist": stop_dist, "range_20m": None,
                        "revisits": None, "trend_slope": None,
                        "bars_from_open": None, "range_ratio": None})
        continue

    hi    = float(window["High"].max())
    lo    = float(window["Low"].min())
    rng   = hi - lo

    # How many times in the lookback window did price visit the stop level?
    if stop_dist is not None and stop is not None:
        stop_f = float(stop)
        if direc == "up":
            # Stop is below entry; count bars where Low touched near the stop
            revisits = int(((window["Low"] <= stop_f + 0.5) & (window["Low"] >= stop_f - 2.0)).sum())
        else:
            # Stop is above entry; count bars where High touched near the stop
            revisits = int(((window["High"] >= stop_f - 0.5) & (window["High"] <= stop_f + 2.0)).sum())
    else:
        revisits = None

    # Slope of 1m closes over lookback (sign: + = uptrend, - = downtrend)
    closes = window["Close"].values.astype(float)
    xs = np.arange(len(closes))
    slope = float(np.polyfit(xs, closes, 1)[0]) if len(closes) >= 3 else None

    # Session open is 09:00 ET for this instrument
    session_open = pd.Timestamp(et.date()).tz_localize("America/New_York").replace(hour=9, minute=0)
    bars_from_open = int((et - session_open).total_seconds() / 60)

    range_ratio = (stop_dist / rng) if (stop_dist is not None and rng > 0) else None

    records.append({
        **row,
        "stop_dist":     stop_dist,
        "range_20m":     rng,
        "revisits":      revisits,
        "trend_slope":   slope,
        "bars_from_open": bars_from_open,
        "range_ratio":   range_ratio,
    })

feat = pd.DataFrame(records)
feat = feat[feat["range_20m"].notna()].copy()
feat["win"] = feat["pnl_dollars"] > 0
feat["hm"]  = feat["entry_dt"].dt.hour * 60 + feat["entry_dt"].dt.minute

print(f"Trades with features: {len(feat)}")

# ── Feature distributions: winners vs losers ──────────────────────────────
print(f"\n{SEP}")
print("FEATURE DISTRIBUTIONS: WINNERS vs LOSERS")
print(SEP)

for feat_col, label in [
    ("stop_dist",   "Stop distance (pts)"),
    ("range_20m",   "20min range (pts)"),
    ("range_ratio", "Stop / 20min range"),
    ("revisits",    "Stop-level revisits in 20min"),
    ("trend_slope", "1m-close slope (20 bars)"),
    ("bars_from_open", "Bars from session open"),
]:
    w = feat[feat["win"]][feat_col].dropna()
    l = feat[~feat["win"]][feat_col].dropna()
    if w.empty or l.empty:
        continue
    print(f"\n  {label}")
    print(f"    Winners  median={w.median():.2f}  mean={w.mean():.2f}  p25={w.quantile(.25):.2f}  p75={w.quantile(.75):.2f}")
    print(f"    Losers   median={l.median():.2f}  mean={l.mean():.2f}  p25={l.quantile(.25):.2f}  p75={l.quantile(.75):.2f}")

# ── Revisit bucketing ─────────────────────────────────────────────────────
print(f"\n{SEP}")
print("STOP-LEVEL REVISITS BEFORE ENTRY -> WR and avg PnL")
print(SEP)
rev_sub = feat[feat["revisits"].notna() & feat["exit_reason"].eq("stopped-out") | feat["win"]].copy()
# Actually use all trades
rev_sub = feat[feat["revisits"].notna()].copy()
for rv in sorted(rev_sub["revisits"].unique()):
    g = rev_sub[rev_sub["revisits"] == rv]
    wr = g["win"].mean()
    avg = g["pnl_dollars"].mean()
    print(f"  revisits={rv:2.0f}  n={len(g):4d}  wr={wr:.3f}  avg_pnl=${avg:.2f}")

# ── Range ratio bucketing ─────────────────────────────────────────────────
print(f"\n{SEP}")
print("STOP/RANGE RATIO (how tight stop is relative to recent range)")
print("  < 0.15 = very tight (easily stopped by noise)")
print("  > 0.40 = wide (needs a significant reversal to reach stop)")
print(SEP)
rr_sub = feat[feat["range_ratio"].notna()].copy()
bins = [0, 0.10, 0.20, 0.30, 0.40, 0.60, 1.0, 9.9]
labels_b = ["<0.10", "0.10-0.20", "0.20-0.30", "0.30-0.40", "0.40-0.60", "0.60-1.0", ">1.0"]
rr_sub["rr_bin"] = pd.cut(rr_sub["range_ratio"], bins=bins, labels=labels_b)
for lb, g in rr_sub.groupby("rr_bin", observed=True):
    wr = g["win"].mean()
    avg = g["pnl_dollars"].mean()
    print(f"  {lb:12s}  n={len(g):4d}  wr={wr:.3f}  avg_pnl=${avg:.2f}")

# ── Midday trades (12:00-14:00) deep dive ────────────────────────────────
print(f"\n{SEP}")
print("MIDDAY TRADES (12:00-14:00) — STOP FRESHNESS AND STRUCTURE")
print(SEP)
midday = feat[(feat["hm"] >= 720) & (feat["hm"] < 840)].copy()
morning = feat[(feat["hm"] >= 570) & (feat["hm"] < 720)].copy()
print(f"Midday n={len(midday)}  WR={midday['win'].mean():.3f}  avg=${midday['pnl_dollars'].mean():.2f}")
print(f"Morning n={len(morning)} WR={morning['win'].mean():.3f}  avg=${morning['pnl_dollars'].mean():.2f}")
print()
for feat_col, label in [
    ("revisits",    "Revisits"),
    ("range_ratio", "Stop/Range"),
    ("trend_slope", "Trend slope"),
    ("stop_dist",   "Stop dist"),
    ("range_20m",   "20m range"),
]:
    m_w = midday[midday["win"]][feat_col].dropna()
    m_l = midday[~midday["win"]][feat_col].dropna()
    r_w = morning[morning["win"]][feat_col].dropna()
    r_l = morning[~morning["win"]][feat_col].dropna()
    if m_w.empty and m_l.empty:
        continue
    print(f"  {label}")
    if not m_w.empty: print(f"    Midday  winners median={m_w.median():.2f}")
    if not m_l.empty: print(f"    Midday  losers  median={m_l.median():.2f}")
    if not r_w.empty: print(f"    Morning winners median={r_w.median():.2f}")
    if not r_l.empty: print(f"    Morning losers  median={r_l.median():.2f}")

# ── Consecutive streak analysis with features ─────────────────────────────
print(f"\n{SEP}")
print("CONSECUTIVE LOSS STREAKS — FEATURE PROFILE DURING vs AFTER STREAKS")
print(SEP)

# Tag each row with the length of the streak it belongs to
feat_sorted = feat.sort_values("entry_dt").reset_index(drop=True)
streak_len = []
current = 0
for w in feat_sorted["win"]:
    if not w:
        current += 1
    else:
        current = 0
    streak_len.append(current)
feat_sorted["streak_pos"] = streak_len

for threshold, label in [(0, "Not in streak"), (1, "Streak pos 1-3"), (4, "Streak pos 4-7"), (8, "Streak pos 8+")]:
    if threshold == 0:
        g = feat_sorted[feat_sorted["streak_pos"] == 0]
    elif threshold == 1:
        g = feat_sorted[(feat_sorted["streak_pos"] >= 1) & (feat_sorted["streak_pos"] <= 3)]
    elif threshold == 4:
        g = feat_sorted[(feat_sorted["streak_pos"] >= 4) & (feat_sorted["streak_pos"] <= 7)]
    else:
        g = feat_sorted[feat_sorted["streak_pos"] >= 8]
    if g.empty:
        continue
    rev = g["revisits"].dropna()
    rrat = g["range_ratio"].dropna()
    slp = g["trend_slope"].dropna()
    print(f"  {label} (n={len(g)})")
    if not rev.empty:    print(f"    revisits    median={rev.median():.2f}  mean={rev.mean():.2f}")
    if not rrat.empty:   print(f"    range_ratio median={rrat.median():.2f}  mean={rrat.mean():.2f}")
    if not slp.empty:    print(f"    trend_slope median={slp.median():.2f}  mean={slp.mean():.2f}")

# ── Trend direction alignment ─────────────────────────────────────────────
# Does slope agree with trade direction?
print(f"\n{SEP}")
print("TREND ALIGNMENT: slope agrees with direction vs opposes")
print("  aligned = slope > 0 for UP trades, slope < 0 for DOWN trades")
print(SEP)
al = feat.copy()
al["aligned"] = ((al["direction"] == "up") & (al["trend_slope"] > 0)) | \
                ((al["direction"] == "down") & (al["trend_slope"] < 0))
for aligned, g in al.groupby("aligned"):
    label = "Aligned" if aligned else "Counter-trend"
    print(f"  {label:15s}  n={len(g):4d}  wr={g['win'].mean():.3f}  avg_pnl=${g['pnl_dollars'].mean():.2f}")

# Break down counter-trend by time-of-day
print()
print("  Counter-trend by time bucket:")
ct = al[~al["aligned"]].copy()
ct["bucket"] = (ct["hm"] // 30) * 30
for bkt, g in ct.groupby("bucket"):
    h, m = divmod(bkt, 60)
    print(f"    {h:02d}:{m:02d}  n={len(g):3d}  wr={g['win'].mean():.3f}  avg_pnl=${g['pnl_dollars'].mean():.2f}")

# ── Where do the big loss streaks cluster? ───────────────────────────────
print(f"\n{SEP}")
print("BIG STREAKS (>=8 consecutive losses): DATE CLUSTERS")
print(SEP)
big_streak = feat_sorted[feat_sorted["streak_pos"] >= 8].copy()
big_streak["date"] = big_streak["entry_dt"].dt.date
for date, g in big_streak.groupby("date"):
    folds = g["fold"].unique()
    print(f"  {date}  n={len(g):3d}  folds={list(folds)}  "
          f"directions: up={len(g[g['direction']=='up'])} down={len(g[g['direction']=='down'])}")
