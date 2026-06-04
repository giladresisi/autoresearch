# _o5_feature_dump.py
# DIAGNOSTIC ONLY — no strategy change. Reads the output of a 1s regression run and dumps,
# for every o5 entry (fired + gated), the per-entry STRUCTURAL features we suspect separate
# o5 winners from losers, then reports whether each feature actually separates them.
#
# Purpose: decide rework-vs-delete for o5 entries on EVIDENCE at the entry level (not day level).
# The day-level "choppiness" signal was already shown NOT to separate o5 win/lose days
# (2026-06-04 analysis); this drops one altitude down to the individual entry, in 1s resolution
# (so the sub-minute whipsaws that hurt live are actually present).
#
# Inputs per date (produced by: `uv run python regression.py --dates <range> --mode 1s`):
#   data/regression/<date>/events_1s.jsonl   (o5 entries carry conf="o5" + stop; gated carry gated=)
#   data/regression/<date>/trades_1s.tsv     (outcomes: entry_time, pnl_dollars, exit_*)
#   data/regression/<date>/levels.json       (day_high/low, week_high/low -> mids; named levels)
#   data/MNQ_1s.parquet                       (session High/Low up to each entry instant)
#
# Headroom is computed with the SAME helpers the live gate uses, so it matches production exactly.

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pandas as pd

# Reuse production helpers so headroom/mid logic is identical to the merged R2 gate.
from strategy import _session_mids, _nearest_opposing_level, MIN_HEADROOM_PTS

_DIR_UP, _DIR_DOWN = "up", "down"
_REG = Path("data/regression")
# mode -> (events file, trades file, parquet for session extremes)
_MODE = {
    "1s": ("events_1s.jsonl", "trades_1s.tsv", Path("data/MNQ_1s.parquet")),
    "1m": ("events.jsonl",    "trades.tsv",    Path("data/MNQ_1m.parquet")),
}
# Backtest session window (mirror backtest_smt: prev-day 18:00 ET -> date 17:00 ET).
_SESSION_OPEN = "18:00"
_SESSION_CLOSE = "17:00"

_FIRE_KINDS = {"market-entry", "new-stop-entry", "move-stop-entry"}


def _session_bounds(date_str: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    d = pd.Timestamp(date_str).date()
    prev = d - pd.Timedelta(days=1)
    start = pd.Timestamp(f"{prev} {_SESSION_OPEN}", tz="America/New_York")
    end = pd.Timestamp(f"{d} {_SESSION_CLOSE}", tz="America/New_York")
    return start, end


def _level_prices(levels: list) -> list[float]:
    """All named 'level' prices — used as draw-on-liquidity target candidates."""
    return [l["price"] for l in levels if l.get("kind") == "level" and l.get("price") is not None]


def _to_et(ts) -> pd.Timestamp:
    """Normalize a timestamp to the America/New_York zone (events carry a fixed UTC offset;
    the parquet index is the named zone — slicing requires matching tz)."""
    t = pd.Timestamp(ts)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t.tz_convert("America/New_York")


def _features_for_entry(ev: dict, levels: list, mnq: pd.DataFrame,
                        sess_start: pd.Timestamp) -> dict:
    t = _to_et(ev["time"])
    price = float(ev["price"])
    direction = ev.get("direction")
    stop = ev.get("stop")
    risk = abs(price - float(stop)) if stop is not None else float("nan")

    daily_mid, weekly_mid = _session_mids(levels)
    # nearest opposing level ahead = nearest of {day_mid, week_mid, nearest named level ahead}.
    targets = [{"price": p} for p in _level_prices(levels)]
    lvl = _nearest_opposing_level(price, direction, daily_mid, weekly_mid, targets)
    headroom = abs(lvl - price) if lvl is not None else float("inf")
    rr = headroom / risk if risk and risk == risk and risk > 0 else float("inf")

    # Session extreme SO FAR (strictly up to the entry instant) from the 1s bars.
    win = mnq.loc[sess_start:t]
    sh = float(win["High"].max()) if len(win) else float("nan")
    sl = float(win["Low"].min()) if len(win) else float("nan")
    if direction == _DIR_UP:
        dist_from_extreme = sh - price          # how far BELOW the session high we are entering
        objective_tagged = (lvl is not None and sh >= lvl)   # draw already consumed before entry
    else:
        dist_from_extreme = price - sl
        objective_tagged = (lvl is not None and sl <= lvl)

    return {
        "date": ev.get("_date"),
        "time": ev["time"],
        "dir": direction,
        "kind": ev["kind"],
        "fired": ev["kind"] in _FIRE_KINDS,
        "price": round(price, 2),
        "headroom": round(headroom, 1) if headroom != float("inf") else "",
        "risk": round(risk, 1) if risk == risk else "",
        "rr": round(rr, 2) if rr != float("inf") else "",
        "dist_day_mid": round(daily_mid - price, 1) if daily_mid is not None else "",
        "dist_week_mid": round(weekly_mid - price, 1) if weekly_mid is not None else "",
        "dist_from_extreme": round(dist_from_extreme, 1) if dist_from_extreme == dist_from_extreme else "",
        "objective_tagged": int(objective_tagged),
        "gated": ev.get("gated", ""),
    }


def _collect(dates: list[str], mode: str) -> list[dict]:
    ev_name, tr_name, parquet = _MODE[mode]
    mnq = pd.read_parquet(parquet)
    rows: list[dict] = []
    for date_str in dates:
        ddir = _REG / date_str
        ev_p = ddir / ev_name
        lv_p = ddir / "levels.json"
        if not ev_p.exists() or not lv_p.exists():
            print(f"[skip] {date_str}: missing {ev_name} or levels.json")
            continue
        levels = json.load(open(lv_p))["liquidities"]
        evs = [json.loads(l) for l in ev_p.read_text().splitlines() if l.strip()]
        o5 = [e for e in evs if e.get("conf") == "o5"
              and (e["kind"] in _FIRE_KINDS or e["kind"] == "entry-gated")]
        if not o5:
            continue
        sess_start, _ = _session_bounds(date_str)
        # join outcomes for fired entries
        tr_p = ddir / tr_name
        trades = {}
        if tr_p.exists():
            for r in csv.DictReader(open(tr_p), delimiter="\t"):
                if r.get("entry_time"):
                    trades[r["entry_time"]] = r
        for e in o5:
            e["_date"] = date_str
            row = _features_for_entry(e, levels, mnq, sess_start)
            tr = trades.get(e["time"]) if row["fired"] else None
            row["pnl"] = round(float(tr["pnl_dollars"]), 1) if tr else ""
            row["win"] = (1 if float(tr["pnl_dollars"]) > 0 else 0) if tr else ""
            row["exit_reason"] = tr.get("exit_reason", "") if tr else ""
            rows.append(row)
    return rows


def _separation_report(rows: list[dict]) -> None:
    """For fired o5 trades, compare each feature's distribution for winners vs losers."""
    fired = [r for r in rows if r["fired"] and r["win"] != ""]
    wins = [r for r in fired if r["win"] == 1]
    losses = [r for r in fired if r["win"] == 0]
    print(f"\n=== separation (fired o5 trades): {len(wins)} win / {len(losses)} loss ===")
    if not wins or not losses:
        print("  (need both winners and losers to compare)")
        return

    def med(rs, key):
        vals = sorted(float(r[key]) for r in rs if r[key] not in ("", None))
        return vals[len(vals) // 2] if vals else float("nan")

    print(f"{'feature':20} {'median(win)':>12} {'median(loss)':>13}  separates?")
    for key in ("headroom", "rr", "dist_from_extreme", "dist_day_mid", "dist_week_mid"):
        mw, ml = med(wins, key), med(losses, key)
        # crude flag: medians differ by >25% of the larger magnitude
        denom = max(abs(mw), abs(ml), 1e-9)
        flag = "YES" if abs(mw - ml) / denom > 0.25 else "—"
        print(f"{key:20} {mw:>12.1f} {ml:>13.1f}  {flag}")
    # objective_tagged is boolean -> compare rates
    rw = sum(r["objective_tagged"] for r in wins) / len(wins)
    rl = sum(r["objective_tagged"] for r in losses) / len(losses)
    print(f"{'objective_tagged%':20} {rw*100:>12.0f} {rl*100:>13.0f}  "
          f"{'YES' if abs(rw - rl) > 0.25 else '—'}")
    print(f"\nMIN_HEADROOM_PTS (gate floor) = {MIN_HEADROOM_PTS}")
    print("Read: if losers cluster at low headroom/rr, high dist_from_extreme, or high "
          "objective_tagged%, that feature is a candidate o5 suppression gate.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump structural features for o5 entries (1s).")
    ap.add_argument("dates", nargs="*", help="dates YYYY-MM-DD; default = all with the mode's events file")
    ap.add_argument("--mode", choices=["1s", "1m"], default="1s",
                    help="1s = real run incl. sub-minute whipsaws (preferred); 1m = quick preview")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    ev_name = _MODE[args.mode][0]
    dates = args.dates or sorted(p.parent.name for p in _REG.glob(f"*/{ev_name}"))
    if not dates:
        print(f"No {args.mode} regression output found. Run: "
              f"uv run python regression.py --dates <range> --mode {args.mode}")
        return 1
    rows = _collect(dates, args.mode)
    if not rows:
        print("No o5 entries found in the given 1s runs.")
        return 0
    cols = ["date", "time", "dir", "kind", "fired", "price", "headroom", "risk", "rr",
            "dist_day_mid", "dist_week_mid", "dist_from_extreme", "objective_tagged",
            "gated", "pnl", "win", "exit_reason"]
    out = Path(args.out or f"data/regression/_o5_features_{args.mode}.tsv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} o5-entry rows -> {out}")
    _separation_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
