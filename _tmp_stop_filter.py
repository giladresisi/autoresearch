"""
Post-hoc sweep: max stop distance filter.

For stopped-out trades: stop_dist = |pnl_points| (exact — exit_price IS the stop).
Non-stopped-out trades: stop_dist unknown, treated as unaffected (conservative).

Shows recovered PnL, foregone PnL, net, and per-fold consistency for each threshold.
"""

import pandas as pd

df = pd.read_csv("trades.tsv", sep="\t")
df["win"] = df["pnl_dollars"] > 0

# Stopped-out trades: we know the exact stop distance
stopped = df[df["exit_reason"] == "stopped-out"].copy()
stopped["stop_dist"] = stopped["pnl_points"].abs()

other = df[df["exit_reason"] != "stopped-out"].copy()

base_total  = df["pnl_dollars"].sum()
base_trades = len(df)
base_wr     = df["win"].mean()

SEP = "=" * 72

print(f"\n{SEP}")
print(f"BASELINE  trades={base_trades}  total_pnl=${base_total:.0f}  wr={base_wr:.3f}")
print(f"  stopped-out: {len(stopped)}  non-stopped: {len(other)}")
print(SEP)

thresholds = [5, 6, 7, 8, 9, 10, 12, 15]

print(f"\n{'thresh':>6}  {'blocked':>7}  {'bl_losers':>9}  {'bl_winners':>10}  "
      f"{'recovered':>10}  {'foregone':>9}  {'net':>8}  {'new_total':>10}  "
      f"{'new_wr':>7}  {'min_fold':>9}")
print("-" * 100)

for T in thresholds:
    blocked = stopped[stopped["stop_dist"] > T]
    blocked_losers  = blocked[~blocked["win"]]
    blocked_winners = blocked[blocked["win"]]

    recovered = abs(blocked_losers["pnl_dollars"].sum())  # we avoided these losses
    foregone  = blocked_winners["pnl_dollars"].sum()       # we missed these gains

    net       = recovered - foregone
    new_total = base_total + net

    # Recompute WR: remove blocked trades from the denominator
    kept = df[~df.index.isin(blocked.index)]
    new_wr = kept["win"].mean() if len(kept) else float("nan")

    # Per-fold breakdown for min fold
    fold_pnls = []
    for fld in sorted(df["fold"].unique()):
        fold_all    = df[df["fold"] == fld]
        fold_blocked = blocked[blocked["fold"] == fld]
        fold_net    = fold_all["pnl_dollars"].sum() + fold_blocked[~fold_blocked["win"]]["pnl_dollars"].sum().abs() \
                      - fold_blocked[fold_blocked["win"]]["pnl_dollars"].sum()
        fold_pnls.append(fold_net)

    min_fold = min(fold_pnls)

    print(f"  T={T:>2}    {len(blocked):>7}  {len(blocked_losers):>9}  {len(blocked_winners):>10}  "
          f"  ${recovered:>7.0f}    ${foregone:>6.0f}    ${net:>6.0f}   ${new_total:>8.0f}  "
          f"  {new_wr:.3f}   ${min_fold:>7.0f}")

# Detailed breakdown at T=8 (likely sweet spot)
T = 8
print(f"\n{SEP}")
print(f"DETAILED FOLD BREAKDOWN AT T={T}pts")
print(SEP)
blocked = stopped[stopped["stop_dist"] > T]

for fld in sorted(df["fold"].unique()):
    fold_df     = df[df["fold"] == fld]
    fold_blocked = blocked[blocked["fold"] == fld]

    bl_l = fold_blocked[~fold_blocked["win"]]
    bl_w = fold_blocked[fold_blocked["win"]]

    rec  = abs(bl_l["pnl_dollars"].sum())
    fore = bl_w["pnl_dollars"].sum()
    net  = rec - fore

    orig_pnl = fold_df["pnl_dollars"].sum()
    new_pnl  = orig_pnl + net

    kept = fold_df[~fold_df.index.isin(fold_blocked.index)]
    new_wr = kept["win"].mean() if len(kept) else float("nan")

    print(f"  fold{fld}  blocked={len(fold_blocked):3d} (losers={len(bl_l)} winners={len(bl_w)})  "
          f"rec=${rec:.0f} fore=${fore:.0f} net=${net:.0f}  "
          f"pnl: ${orig_pnl:.0f} -> ${new_pnl:.0f}  new_wr={new_wr:.3f}")

# Combined with pre-9:30 block (the other filter discussed)
print(f"\n{SEP}")
print(f"COMBINED: stop<=8pts AND block pre-9:30 entries (9:25-9:29)")
print(SEP)
df["entry_dt"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("America/New_York")
df["hm"] = df["entry_dt"].dt.hour * 60 + df["entry_dt"].dt.minute
pre930 = df[df["hm"] < 570]
pre930_stopped = pre930[pre930["exit_reason"] == "stopped-out"]

T = 8
stop_blocked = stopped[stopped["stop_dist"] > T]

# Union of both filters (avoiding double-count)
all_blocked_idx = set(stop_blocked.index) | set(pre930_stopped.index)
# Pre-9:30 non-stopped (e.g., market-close winners) — block these too
pre930_nonstopped = pre930[pre930["exit_reason"] != "stopped-out"]
all_blocked_idx |= set(pre930_nonstopped.index)

all_blocked = df.loc[list(all_blocked_idx)]
bl_l = all_blocked[~all_blocked["win"]]
bl_w = all_blocked[all_blocked["win"]]

rec  = bl_l["pnl_dollars"].sum().abs()
fore = bl_w["pnl_dollars"].sum()
net  = rec - fore

kept = df.loc[~df.index.isin(all_blocked_idx)]
new_total = kept["pnl_dollars"].sum()
new_wr    = kept["win"].mean()

print(f"  Blocked: {len(all_blocked)} trades (losers={len(bl_l)} winners={len(bl_w)})")
print(f"  Recovered: ${rec:.0f}  Foregone: ${fore:.0f}  Net: ${net:.0f}")
print(f"  Total PnL: ${base_total:.0f} -> ${new_total:.0f}  WR: {base_wr:.3f} -> {new_wr:.3f}")
print()
for fld in sorted(df["fold"].unique()):
    fold_kept  = kept[kept["fold"] == fld]
    print(f"  fold{fld}  kept={len(fold_kept):3d}  pnl=${fold_kept['pnl_dollars'].sum():.0f}  "
          f"wr={fold_kept['win'].mean():.3f}")
