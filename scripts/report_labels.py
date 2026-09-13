"""The phase-2 report: what the label corpus says, with the nulls beside every rate.

Reads `.agents/label-corpus/{labels,candidates}.jsonl` and prints the ten blocks plan 28
Task 6 step 6 enumerates, in that order. Nothing here is tuned and nothing is hidden: the
unexplained fraction leads, the censored sessions are broken out (spec §4.4), and every
per-class share is shown against its own census (spec §3.4).

Band sensitivity is recomputed from the stored `closest_approach` values rather than by
re-running the build -- the corpus keeps the raw gap for every candidate, so the tolerance
band is a REPORTING threshold here and can be swept for free.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.nulls import null_a, null_b     # noqa: E402

DEFAULT_DIR = os.path.join(_REPO, ".agents", "label-corpus")
BANDS = (0.10, 0.15, 0.25, 0.50)


def _load(d):
    rd = lambda n: [json.loads(l) for l in open(os.path.join(d, n), encoding="utf-8")   # noqa: E731
                    if l.strip()]
    return rd("labels.jsonl"), rd("candidates.jsonl")


def _q(xs, p):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else None


def _dist(xs, unit=""):
    if not xs:
        return "n=0"
    return ("n=%d  p10=%.1f%s  med=%.1f%s  p90=%.1f%s  mean=%.1f%s"
            % (len(xs), _q(xs, .1), unit, _q(xs, .5), unit, _q(xs, .9), unit,
               st.mean(xs), unit))


def _hdr(n, title):
    print("\n%s\n%d. %s\n%s" % ("-" * 74, n, title, "-" * 74))


def report(d: str, primary_only: bool = True) -> None:
    labels, cands = _load(d)
    if primary_only:
        labels = [r for r in labels if r["role"] == "primary"]
        cands = [r for r in cands if r["role"] == "primary"]
    key = lambda r: (r["date"], r["segment"], r["ticker"])     # noqa: E731
    lab_by = {key(r): r for r in labels}
    live = [r for r in labels if not r["censored"]]
    cens = [r for r in labels if r["censored"]]

    print("=" * 74)
    print("CYCLE-4 PHASE 2 — LABEL REPORT   (%d label rows over %d sessions%s)"
          % (len(labels), len({r["date"] for r in labels}),
             ", primary segments only" if primary_only else ""))
    print("=" * 74)

    _hdr(1, "Unexplained fraction — the headline")
    for name, rows in (("all", labels), ("completed moves", live),
                       ("censored (spec 4.4)", cens)):
        for tkr in ("MNQ", "MES"):
            sub = [r for r in rows if r["ticker"] == tkr]
            if not sub:
                continue
            u = sum(1 for r in sub if r["status"] == "unexplained")
            print("   %-22s %-4s  %2d/%2d unexplained  (%.1f%%)"
                  % (name, tkr, u, len(sub), 100.0 * u / len(sub)))

    _hdr(2, "How the draw was identified")
    for tkr in ("MNQ", "MES"):
        sub = [r for r in labels if r["ticker"] == tkr and r["status"] == "labelled"]
        via = {v: sum(1 for r in sub if r["via"] == v) for v in ("reached_last",
                                                                "near_miss")}
        amb = sum(1 for r in sub if r["ambiguous"])
        print("   %-4s  reached_last=%-3d near_miss=%-3d  ambiguous=%d"
              % (tkr, via["reached_last"], via["near_miss"], amb))

    _hdr(3, "Lag: turn minus completion (minutes) — decides if a window is ever needed")
    for tkr in ("MNQ", "MES"):
        lags = [r["lag_min"] for r in labels
                if r["ticker"] == tkr and r.get("lag_min") is not None]
        print("   %-4s %s" % (tkr, _dist(lags, "m")))
        if lags:
            print("        <=5m: %d   <=15m: %d   >30m: %d"
                  % (sum(1 for x in lags if x <= 5), sum(1 for x in lags if x <= 15),
                     sum(1 for x in lags if x > 30)))

    _hdr(4, "Overshoot: move-end minus the draw (spec 2.4 — normal, never identifying)")
    for tkr in ("MNQ", "MES"):
        sub = [r for r in labels if r["ticker"] == tkr and r["status"] == "labelled"
               and r.get("overshoot") is not None]
        print("   %-4s raw   %s" % (tkr, _dist([r["overshoot"] for r in sub], "pt")))
        ratio = [r["overshoot"] / r["avg_range_1h"][tkr] for r in sub
                 if r["avg_range_1h"].get(tkr)]
        print("        /1h   %s" % _dist(ratio))
        print("        undershoot (move stopped short of the draw): %d/%d"
              % (sum(1 for r in sub if r["overshoot"] < 0), len(sub)))

    _hdr(5, "Per-class draw share vs census — spec 3.4's attractiveness hypothesis")
    for tkr in ("MNQ", "MES"):
        draws = [r["cls"] for r in labels
                 if r["ticker"] == tkr and r["status"] == "labelled"]
        elig = [r["cls"] for r in cands
                if r["ticker"] == tkr and r["outcome"] != "ineligible"]
        out = null_b(draws, elig)
        print("   %s" % tkr)
        print("      %-16s %8s %8s %8s   %s" % ("class", "draws", "census", "delta",
                                                "90% interval"))
        for cls, v in sorted(out.items(), key=lambda kv: -kv[1]["delta"]):
            flag = "  <-- pull" if v["lo"] > 0 else ("  <-- avoided" if v["hi"] < 0
                                                     else "")
            print("      %-16s %7.1f%% %7.1f%% %+7.1f%%   [%+.1f%%, %+.1f%%]%s"
                  % (cls, 100 * v["draw_share"], 100 * v["eligible_share"],
                     100 * v["delta"], 100 * v["lo"], 100 * v["hi"], flag))

    _hdr(6, "Null A — is the draw closer to the turn than an arbitrary eligible pool?")
    for tkr in ("MNQ", "MES"):
        cases = []
        for lab in labels:
            if lab["ticker"] != tkr or lab["status"] != "labelled":
                continue
            if lab.get("own_extreme") is None:
                continue
            pool = [abs(lab["own_extreme"] - c["price"]) for c in cands
                    if c["ticker"] == tkr and c["date"] == lab["date"]
                    and c["segment"] == lab["segment"] and c["outcome"] != "ineligible"]
            if pool:
                cases.append({"label_dist": abs(lab["own_extreme"] - lab["price"]),
                              "candidate_dists": pool})
        r = null_a(cases)
        print("   %-4s observed median |move-end - draw| = %s pt   null median = %s pt"
              % (tkr, r["observed_median"], r["null_median"]))
        print("        null 10-90%%: [%s, %s]   beats_null_frac = %s   (0.5 = census)"
              % (r["null_p10"], r["null_p90"], r["beats_null_frac"]))

    _hdr(7, "Cross-instrument — spec 3.1's premise pointed at the target")
    drv = {tkr: sum(1 for r in labels if r["ticker"] == tkr and r["is_halt_driver"])
           for tkr in ("MNQ", "MES")}
    print("   halt driver (its draw completed last): MNQ=%d  MES=%d" % (drv["MNQ"],
                                                                        drv["MES"]))
    for tkr in ("MNQ", "MES"):
        sub = [r for r in labels if r["ticker"] == tkr and r["status"] == "labelled"]
        cnt = {}
        for r in sub:
            cnt[r["counterpart_status"]] = cnt.get(r["counterpart_status"], 0) + 1
        print("   %-4s counterpart at the same named level: %s"
              % (tkr, ", ".join("%s=%d" % kv for kv in sorted(cnt.items()))))
    both = sum(1 for k, r in lab_by.items()
               if k[2] == "MNQ" and r["status"] == "labelled"
               and lab_by.get((k[0], k[1], "MES"), {}).get("status") == "labelled"
               and set(r["names"]) & set(lab_by[(k[0], k[1], "MES")]["names"]))
    n_pairs = len({(k[0], k[1]) for k in lab_by})
    print("   both instruments named the SAME level: %d/%d segments" % (both, n_pairs))

    _hdr(8, "Confirmation-tax curve — distance to the draw as entry is delayed")
    for tkr in ("MNQ", "MES"):
        sub = [r for r in labels if r["ticker"] == tkr and r["status"] == "labelled"]
        if not sub:
            continue
        print("   %s" % tkr)
        base = [r["dist_from_start"] for r in sub if r.get("dist_from_start") is not None]
        print("      %-14s %s" % ("at move-start", _dist(base, "pt")))
        for m in ("2", "5", "10"):
            v = [r["dist_at_proxy"][m] for r in sub
                 if r.get("dist_at_proxy", {}).get(m) is not None]
            rat = [r["dist_at_proxy"][m] / r["avg_range_1h"][tkr] for r in sub
                   if r.get("dist_at_proxy", {}).get(m) is not None
                   and r["avg_range_1h"].get(tkr)]
            print("      %-14s %s" % ("at +%s min" % m, _dist(v, "pt")))
            print("      %-14s %s" % ("   /avg_1h", _dist(rat)))

    _hdr(9, "Band sensitivity — recomputed from stored gaps, no re-run")
    for mult in BANDS:
        line = []
        for tkr in ("MNQ", "MES"):
            sub = [r for r in labels if r["ticker"] == tkr]
            resolved = 0
            for r in sub:
                if r["status"] == "labelled" and r["via"] == "reached_last":
                    resolved += 1
                    continue
                gaps = [c["closest_approach"] for c in cands
                        if c["ticker"] == tkr and c["date"] == r["date"]
                        and c["segment"] == r["segment"]
                        and c["closest_approach"] is not None]
                band = mult * (r["avg_range_1h"].get(tkr) or 0.0)
                if any(abs(g) <= band for g in gaps):
                    resolved += 1
            line.append("%s %2d/%2d (%.0f%% unexplained)"
                        % (tkr, resolved, len(sub),
                           100.0 * (len(sub) - resolved) / max(len(sub), 1)))
        print("   band=%.2f x avg_1h   %s" % (mult, "   ".join(line)))

    _hdr(10, "Discovery / holdout")
    for name, flag in (("discovery", False), ("holdout", True)):
        sub = [r for r in labels if r["holdout"] is flag]
        print("   %-10s %2d sessions, %3d label rows"
              % (name, len({r["date"] for r in sub}), len(sub)))
    print("   Every rate above is computed on ALL sessions: phase 2 is labelling, which")
    print("   is mechanical and cannot overfit. The split binds phase 3's rule search.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--all-segments", action="store_true",
                    help="include secondary segments (default: primary only)")
    args = ap.parse_args()
    report(args.dir, primary_only=not args.all_segments)


if __name__ == "__main__":
    main()
