"""
Simulate daily loss limits on 11d 1s regression trades (May 1-15).
Reads data/regression/<date>/trades_1s.tsv        -- with strategy chop filter
Reads data/regression/<date>/trades_1s_nostrat.tsv -- without chop filter (committed baseline)
"""
import csv
from pathlib import Path

DATES = [
    "2026-05-01", "2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07",
    "2026-05-08", "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14",
    "2026-05-15",
]
REG_DIR = Path("data/regression")
LIMITS = [100, 150, 200, 250, 300, 350, 400, 450, 500, 800]


def load_day(date: str, suffix: str = "") -> list[float]:
    fname = f"trades_1s{suffix}.tsv"
    p = REG_DIR / date / fname
    if not p.exists():
        return []
    rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))
    return [float(r["pnl_dollars"]) for r in rows]


def apply_limit(pnls: list[float], limit: float) -> float:
    cum = 0.0
    for p in pnls:
        cum += p
        if cum <= -limit:
            return cum
    return cum


def analyse(label: str, suffix: str = "") -> dict:
    days: dict[str, list[float]] = {}
    for d in DATES:
        pnls = load_day(d, suffix)
        if pnls:
            days[d] = pnls

    baseline_total = sum(sum(v) for v in days.values())
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"  Baseline total (no limit): ${baseline_total:,.2f}  ({len(days)} trading days)")
    print(f"{'='*65}")

    hdr = f"  {'Limit':>6}  {'Total':>10}  {'vs Base':>10}  {'FalseStops':>11}  {'NoSaves':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    best_net = -1e9
    best_limit = None
    results = {}
    for limit in LIMITS:
        total = 0.0
        false_stops = 0
        no_saves = 0
        for date, pnls in days.items():
            actual = sum(pnls)
            result = apply_limit(pnls, limit)
            total += result
            fired = result < actual
            if fired:
                if actual > 0:
                    false_stops += 1
                elif result < actual:
                    no_saves += 1
        vs_base = total - baseline_total
        results[limit] = (total, vs_base, false_stops, no_saves)
        if vs_base > best_net:
            best_net = vs_base
            best_limit = limit
        marker = " <-- best" if (best_limit == limit and vs_base > 0) else ""
        print(f"  ${limit:>5}  ${total:>10,.2f}  {vs_base:>+10.2f}  {false_stops:>11}  {no_saves:>8}{marker}")

    print(f"\n  Best limit: ${best_limit} (net {best_net:+.2f} vs no limit)")

    # Per-day detail for $150
    print(f"\n  --- $150 limit per-day detail ---")
    for date, pnls in sorted(days.items()):
        actual = sum(pnls)
        result = apply_limit(pnls, 150)
        fired = result < actual
        tag = " <<FIRED" if fired else ""
        min_cum = 0.0
        cum = 0.0
        for p in pnls:
            cum += p
            min_cum = min(min_cum, cum)
        actual_tag = "WIN" if actual >= 0 else "LOS"
        print(f"  {date} [{actual_tag}] actual={actual:+8.2f}  limited={result:+8.2f}  min_cum={min_cum:+8.2f}{tag}")

    return {"baseline": baseline_total, "best_limit": best_limit, "best_net": best_net, "results": results, "days": days}


if __name__ == "__main__":
    r_nostrat = analyse("11d 1s — WITHOUT strategy chop filter (committed baseline)", "_nostrat")
    r_strat   = analyse("11d 1s — WITH strategy chop filter", "")

    print("\n\n" + "="*65)
    print("  COMPARISON: chop filter effect on daily loss limit performance")
    print("="*65)
    print(f"  {'':30s}  {'No filter':>12}  {'With filter':>12}  {'Delta':>8}")
    print(f"  {'-'*65}")
    print(f"  {'Total PnL (no limit)':30s}  ${r_nostrat['baseline']:>11,.2f}  ${r_strat['baseline']:>11,.2f}  {r_strat['baseline']-r_nostrat['baseline']:>+8.2f}")
    print(f"  {'Best limit':30s}  {'$'+str(r_nostrat['best_limit']):>12}  {'$'+str(r_strat['best_limit']):>12}")
    print(f"  {'Best limit net gain':30s}  {r_nostrat['best_net']:>+12.2f}  {r_strat['best_net']:>+12.2f}")
    for lim in [150, 450]:
        nv = r_nostrat["results"].get(lim, (0, 0, 0, 0))
        sv = r_strat["results"].get(lim, (0, 0, 0, 0))
        print(f"  {'$'+str(lim)+' limit vs base':30s}  {nv[1]:>+12.2f}  {sv[1]:>+12.2f}")
        print(f"  {'$'+str(lim)+' false stops':30s}  {nv[2]:>12}  {sv[2]:>12}")
