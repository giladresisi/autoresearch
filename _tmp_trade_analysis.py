import pandas as pd
import numpy as np

df = pd.read_csv("trades.tsv", sep="\t")
df["entry_dt"] = pd.to_datetime(df["entry_time"], utc=True)
df["exit_dt"]  = pd.to_datetime(df["exit_time"],  utc=True)
# Convert to ET for time-of-day
df["entry_et"] = df["entry_dt"].dt.tz_convert("America/New_York")
df["win"]      = df["pnl_dollars"] > 0
df["hour"]     = df["entry_et"].dt.hour
df["minute"]   = df["entry_et"].dt.minute
df["hm"]       = df["hour"] * 60 + df["minute"]
df["bars_held"] = (df["exit_dt"] - df["entry_dt"]).dt.total_seconds() / 60

SEP = "=" * 70

# ── Overall ──────────────────────────────────────────────────────────────────
n = len(df)
wr = df["win"].mean()
avg_pnl = df["pnl_dollars"].mean()
print(f"\n{SEP}")
print(f"OVERALL   trades={n}  win_rate={wr:.3f}  avg_pnl=${avg_pnl:.2f}")
print(SEP)

# ── Direction ─────────────────────────────────────────────────────────────────
for d, g in df.groupby("direction"):
    print(f"  {d:5s}  trades={len(g):4d}  wr={g['win'].mean():.3f}  "
          f"avg_pnl=${g['pnl_dollars'].mean():.2f}  "
          f"total=${g['pnl_dollars'].sum():.0f}")

# ── Exit reason ───────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("EXIT REASON BREAKDOWN")
print(SEP)
for r, g in df.groupby("exit_reason"):
    pct = len(g) / n * 100
    wr_r = g["win"].mean()
    avg = g["pnl_dollars"].mean()
    print(f"  {r:30s}  n={len(g):4d} ({pct:4.1f}%)  wr={wr_r:.3f}  avg_pnl=${avg:.2f}")

# ── Time-of-day buckets (30-min) ──────────────────────────────────────────────
print(f"\n{SEP}")
print("TIME-OF-DAY  (30-min buckets, ET)")
print(SEP)
df["bucket"] = (df["hm"] // 30) * 30
for bkt, g in df.groupby("bucket"):
    h, m = divmod(bkt, 60)
    label = f"{h:02d}:{m:02d}"
    wr_b = g["win"].mean()
    avg  = g["pnl_dollars"].mean()
    bar  = "#" * int(wr_b * 20)
    print(f"  {label}  n={len(g):4d}  wr={wr_b:.3f}  avg_pnl=${avg:7.2f}  {bar}")

# ── PnL distribution of losers ───────────────────────────────────────────────
losers = df[~df["win"]]
print(f"\n{SEP}")
print(f"LOSER PNL DISTRIBUTION  (n={len(losers)})")
print(SEP)
for label, lo, hi in [
    ("< -200",     -9999, -200),
    ("-200..-100",  -200, -100),
    ("-100..-50",   -100,  -50),
    ("-50..-20",     -50,  -20),
    ("-20..0",       -20,    0),
]:
    cnt = ((losers["pnl_dollars"] >= lo) & (losers["pnl_dollars"] < hi)).sum()
    print(f"  {label:15s}  n={cnt:4d}  ({cnt/len(losers)*100:.1f}%)")

# ── Stop distance analysis ────────────────────────────────────────────────────
losers_stopped = df[(~df["win"]) & (df["exit_reason"] == "stopped-out")]
print(f"\n{SEP}")
print(f"STOP-OUT LOSS SIZES  (n={len(losers_stopped)})")
print(SEP)
for pct in [25, 50, 75, 90, 95]:
    val_pts = losers_stopped["pnl_points"].quantile(pct / 100)
    val_dlr = losers_stopped["pnl_dollars"].quantile(pct / 100)
    print(f"  p{pct:2d}  {val_pts:.2f} pts    ${val_dlr:.2f}")

# ── Winner pnl distribution ──────────────────────────────────────────────────
winners = df[df["win"]]
print(f"\n{SEP}")
print(f"WINNER PNL DISTRIBUTION  (n={len(winners)})")
print(SEP)
for label, lo, hi in [
    ("0..50",      0,    50),
    ("50..100",   50,   100),
    ("100..200",  100,  200),
    ("200..500",  200,  500),
    ("> 500",     500, 9999),
]:
    cnt = ((winners["pnl_dollars"] >= lo) & (winners["pnl_dollars"] < hi)).sum()
    print(f"  {label:12s}  n={cnt:4d}  ({cnt/len(winners)*100:.1f}%)")

# ── bars held: winners vs losers ─────────────────────────────────────────────
print(f"\n{SEP}")
print("BARS HELD (minutes)")
print(SEP)
for label, grp in [("Winners", df[df["win"]]), ("Losers", df[~df["win"]])]:
    bh = grp["bars_held"].dropna()
    print(f"  {label:8s}  median={bh.median():.0f}m  mean={bh.mean():.0f}m  "
          f"p25={bh.quantile(.25):.0f}m  p75={bh.quantile(.75):.0f}m")

# ── Fold breakdown ────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("FOLD BREAKDOWN")
print(SEP)
for fld, g in df.groupby("fold"):
    print(f"  fold{fld}  n={len(g):3d}  wr={g['win'].mean():.3f}  "
          f"avg_pnl=${g['pnl_dollars'].mean():.2f}  total=${g['pnl_dollars'].sum():.0f}")

# ── RR analysis: what's the actual avg loss vs avg win ───────────────────────
print(f"\n{SEP}")
print("RISK/REWARD REALITY CHECK")
print(SEP)
avg_win  = winners["pnl_dollars"].mean()
avg_loss = losers["pnl_dollars"].mean()
actual_rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
breakeven_wr = 1 / (1 + actual_rr)
print(f"  avg winner:   ${avg_win:.2f}")
print(f"  avg loser:    ${avg_loss:.2f}")
print(f"  actual R:R    {actual_rr:.2f}")
print(f"  break-even WR at this R:R: {breakeven_wr:.3f} ({breakeven_wr*100:.1f}%)")
print(f"  current WR:   {wr:.3f} ({wr*100:.1f}%)")
excess_wr = wr - breakeven_wr
print(f"  WR vs break-even: {excess_wr:+.3f}")

# ── Back-to-back losses ───────────────────────────────────────────────────────
print(f"\n{SEP}")
print("CONSECUTIVE LOSS STREAKS")
print(SEP)
streak = 0
max_streak = 0
streak_hist = {}
for w in df["win"]:
    if not w:
        streak += 1
        max_streak = max(max_streak, streak)
    else:
        streak_hist[streak] = streak_hist.get(streak, 0) + 1
        streak = 0
streak_hist[streak] = streak_hist.get(streak, 0) + 1
print(f"  max consecutive losses: {max_streak}")
for k in sorted(streak_hist):
    if k > 0:
        print(f"  streak={k:2d}  occurrences={streak_hist[k]}")

# ── Direction x time-of-day ──────────────────────────────────────────────────
print(f"\n{SEP}")
print("DIRECTION x TIME-OF-DAY (30-min buckets)")
print(SEP)
for d in ["up", "down"]:
    sub = df[df["direction"] == d]
    print(f"\n  direction={d}")
    for bkt, g in sub.groupby("bucket"):
        h, m = divmod(bkt, 60)
        label = f"{h:02d}:{m:02d}"
        wr_b = g["win"].mean()
        avg  = g["pnl_dollars"].mean()
        print(f"    {label}  n={len(g):3d}  wr={wr_b:.3f}  avg_pnl=${avg:7.2f}")

# ── Micro-loss analysis: trades that lost less than 1 tick ────────────────────
tiny_loss = df[(~df["win"]) & (df["pnl_points"] > -1.0)]
print(f"\n{SEP}")
print(f"MICRO-LOSSES (< 1pt, n={len(tiny_loss)}): immediate stop-outs / wicks")
print(SEP)
if not tiny_loss.empty:
    print(tiny_loss[["fold","entry_time","direction","pnl_points","pnl_dollars","exit_reason"]].to_string(index=False))
