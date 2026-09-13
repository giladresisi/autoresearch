"""Stage A report: the number every phase-3 rule has to beat.

Everything here is computed on the **63 discovery sessions only**. The 21 held-out sessions
are not read, not counted and not summarised — Stage C spends that budget once.

Nothing in this report searches for a rule. It measures the cheapest theory that could be
true ("the move clears k pools"), re-tests the two phase-2 findings that are confounded, and
asks whether prediction at the decision instant is even well-posed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.baseline import (already_reached, class_pull_by_distance,   # noqa: E402
                                  null_cases, ordinal_baseline, reach_baseline)
from agent.study.nulls import null_a, null_b                                 # noqa: E402

DEFAULT_DIR = os.path.join(_REPO, ".agents", "label-corpus")
TICKERS = ("MNQ", "MES")


def _load(d, primary_only=True, discovery_only=True):
    def rd(name):
        rows = [json.loads(l) for l in open(os.path.join(d, name), encoding="utf-8")
                if l.strip()]
        if primary_only:
            rows = [r for r in rows if r["role"] == "primary"]
        if discovery_only:
            rows = [r for r in rows if not r["holdout"]]
        return rows
    return rd("labels.jsonl"), rd("candidates.jsonl")


def _hdr(n, title):
    print("\n%s\nA%d. %s\n%s" % ("-" * 74, n, title, "-" * 74))


def report(d: str) -> None:
    labels, cands = _load(d)
    n_sess = len({r["date"] for r in labels})
    lab_ok = [r for r in labels if r["status"] == "labelled"]

    print("=" * 74)
    print("CYCLE-4 PHASE 3 / STAGE A — BASELINES   (%d discovery sessions, primary "
          "segments)" % n_sess)
    print("   holdout not read. %d labelled instrument-sessions scoreable."
          % len(lab_ok))
    print("=" * 74)

    ob = ordinal_baseline(labels, cands)

    _hdr(1, "Rank of the draw among eligible pools (0 = nearest ahead)")
    for tk in TICKERS:
        o = ob[tk]
        if not o.get("n"):
            continue
        print("   %s  n=%d   median rank=%s   mean=%.2f   median pools eligible=%s"
              % (tk, o["n"], o["median_rank"], o["mean_rank"], o["median_n_eligible"]))
        bars = "  ".join("%s:%d" % (k, v) for k, v in o["hist"].items() if v)
        print("      histogram   %s" % bars)

    _hdr(1, 'Fixed-k baseline — "always name the k-th nearest eligible pool"')
    print("   %-4s %6s %10s %10s %8s" % ("", "k", "correct", "coverage", "acc"))
    best = {}
    for tk in TICKERS:
        o = ob[tk]
        if not o.get("n"):
            continue
        for k, v in o["fixed_k"].items():
            if v["coverage"]:
                print("   %-4s %6d %10d %10d %7.1f%%"
                      % (tk, k, v["correct"], v["coverage"], 100 * v["acc"]))
        cand = max(o["fixed_k"].items(),
                   key=lambda kv: (kv[1]["correct"], kv[1]["acc"]))
        best[tk] = ("fixed-k=%d" % cand[0], cand[1]["correct"], o["n"],
                    cand[1]["correct"] / max(o["n"], 1))
        print()

    _hdr(1, 'Reach-m baseline — "the furthest pool within m x avg_range_1h"')
    rb = reach_baseline(labels, cands)
    print("   %-4s %6s %10s %10s %10s %12s"
          % ("", "m", "correct", "coverage", "acc|cov", "acc|all"))
    for tk in TICKERS:
        for m, v in rb[tk].items():
            print("   %-4s %6.1f %10d %10d %9.1f%% %11.1f%%"
                  % (tk, m, v["correct"], v["coverage"], 100 * v["acc"],
                     100 * v["acc_over_all"]))
        top = max(rb[tk].items(), key=lambda kv: kv[1]["correct"])
        if top[1]["correct"] > best.get(tk, ("", 0, 1, 0))[1]:
            best[tk] = ("reach-m=%.1f" % top[0], top[1]["correct"], top[1]["total"],
                        top[1]["acc_over_all"])
        print()

    _hdr(2, "Class pull INSIDE distance buckets (G1) — 0 = nearest quarter")
    buckets = class_pull_by_distance(labels, cands, n_buckets=4)
    for tk in TICKERS:
        print("   %s" % tk)
        for b in sorted(buckets[tk]):
            data = buckets[tk][b]
            if not data["draws"]:
                print("      bucket %d: no draws" % b)
                continue
            out = null_b(data["draws"], data["eligible"], n_boot=1500)
            print("      bucket %d  (%d draws / %d eligible)"
                  % (b, len(data["draws"]), len(data["eligible"])))
            for cls, v in sorted(out.items(), key=lambda kv: -kv[1]["delta"]):
                if v["n_draws"] == 0 and v["eligible_share"] < 0.02:
                    continue
                flag = ("  <-- PULL" if v["lo"] > 0
                        else "  <-- avoided" if v["hi"] < 0 else "")
                print("         %-16s draws %5.1f%%  census %5.1f%%  delta %+6.1f%%"
                      "  [%+.1f%%, %+.1f%%]%s"
                      % (cls, 100 * v["draw_share"], 100 * v["eligible_share"],
                         100 * v["delta"], 100 * v["lo"], 100 * v["hi"], flag))

    _hdr(3, "Null A, wide vs reached-only (G2)")
    wide = null_cases(labels, cands)
    tight = null_cases(labels, cands, reached_only=True)
    for tk in TICKERS:
        w, t = null_a(wide[tk]), null_a(tight[tk])
        print("   %s" % tk)
        print("      wide          n=%-3s observed=%-9s null=%-9s beats=%s"
              % (w["n"], w["observed_median"], w["null_median"], w["beats_null_frac"]))
        print("      reached-only  n=%-3s observed=%-9s null=%-9s beats=%s"
              % (t["n"], t["observed_median"], t["null_median"], t["beats_null_frac"]))

    _hdr(4, "Was the draw already taken by the time a fill could exist?")
    ar = already_reached(labels, cands)
    for tk in TICKERS:
        for m, v in ar[tk].items():
            print("   %-4s +%2dmin   already reached %2d/%2d (%.0f%%)   "
                  "median rank among pools still open: %s (n=%d)"
                  % (tk, m, v["already_reached"], v["total"], 100 * v["frac"],
                     v["median_rank_among_unreached"], v["n_ranked"]))

    print("\n%s\nTHE NUMBER TO BEAT\n%s" % ("=" * 74, "=" * 74))
    for tk in TICKERS:
        if tk in best:
            name, correct, n, acc = best[tk]
            print("   %-4s  %-14s  %d/%d labelled segments  =  %.1f%%"
                  % (tk, name, correct, n, 100 * acc))
    print("   Stage B families are scored against exactly these, on these sessions.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    args = ap.parse_args()
    report(args.dir)


if __name__ == "__main__":
    main()
