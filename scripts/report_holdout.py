"""Stage C: the one-shot holdout pass.

**This script spends a budget that cannot be refilled.** The 21 held-out sessions were fixed
on 2026-09-03, before any rule was searched for, and have not been read since. Everything
reported in Stage A and Stage B was computed without them.

The rule under test was frozen at commit 307f8a8, and it is two rules composed:

    B9 (confidence)  decline unless an eligible pool sits within ABSTAIN_MAX_D0 x avg_range_1h
                     of the move's origin.
    B8g (selection)  otherwise walk the pool stack outward and stop at the last pool before a
                     gap wider than ~1.5 x avg_range_1h.

Both parameters were chosen in-sample on the discovery set. That is the whole reason this pass
exists.

**The hazard table is fitted on discovery ONLY and applied blind.** Not leave-one-out within
the holdout — that would be a different and easier question. The headline is `act_accuracy`:
of every session the composed rule ACTS on, the share where it names the pool the move was
actually drawn to. An unexplained session inside the acted set counts as a miss, because in
production a target would have been named there and been wrong.

A separate script rather than a flag on `report_hazard.py`, so that spending the holdout is a
deliberate act with its own name rather than a one-character change to a filter.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.hazard import (ABSTAIN_MAX_D0, fit_hazard,          # noqa: E402
                                pool_observations, predict_stop_rank,
                                segment_d0)

CORPUS = os.path.join(_REPO, ".agents", "label-corpus")
OUT_DIR = os.path.join(_REPO, ".agents", "rule-search")
TICKERS = ("MNQ", "MES")
FAMILY = "B8g"
#: Stage B's discovery figures, for the side-by-side. A holdout number near these is the
#: result; a holdout number far below them means the discovery figures were fitted.
DISCOVERY = {"MNQ": {"act": 0.65, "cov": 0.58}, "MES": {"act": 0.68, "cov": 0.62}}


def _split(corpus):
    def rd(name):
        rows = [json.loads(l) for l in open(os.path.join(corpus, name), encoding="utf-8")
                if l.strip()]
        return [r for r in rows if r["role"] == "primary"]
    lab, cand = rd("labels.jsonl"), rd("candidates.jsonl")
    disc = ([r for r in lab if not r["holdout"]], [r for r in cand if not r["holdout"]])
    hold = ([r for r in lab if r["holdout"]], [r for r in cand if r["holdout"]])
    return disc, hold


def report(corpus: str, out_dir: str) -> dict:
    (d_lab, d_cand), (h_lab, h_cand) = _split(corpus)
    d_obs = pool_observations(d_lab, d_cand)
    h_obs = pool_observations(h_lab, h_cand)
    h_d0 = segment_d0(h_lab, h_cand)

    print("=" * 74)
    print("CYCLE-4 PHASE 3 / STAGE C — THE HOLDOUT PASS  (one shot, never repeatable)")
    print("   trained on %d discovery segments, applied blind to %d held-out segments"
          % (len({o.seg for o in d_obs}), len({k[:2] for k in h_d0})))
    print("   rule frozen at 307f8a8:  abstain if d0 > %.1f x avg_1h, else B8g"
          % ABSTAIN_MAX_D0)
    print("=" * 74)

    payload: dict = {"abstain_max_d0": ABSTAIN_MAX_D0, "family": FAMILY, "per_ticker": {}}
    for tk in TICKERS:
        hz, pooled = fit_hazard([o for o in d_obs if o.ticker == tk], FAMILY)

        by_seg: dict = {}
        for o in h_obs:
            if o.ticker == tk:
                by_seg.setdefault(o.seg, []).append(o)
        correct = {}
        for seg, rows in by_seg.items():
            truth = max(o.rank for o in rows if o.stop)
            correct[seg] = int(predict_stop_rank(rows, hz, pooled, FAMILY) == truth)

        keys = [k for k in h_d0 if k[2] == tk]
        res = {}
        for name, t in (("acted", ABSTAIN_MAX_D0), ("everything", None)):
            cov = [k for k in keys if t is None or h_d0[k][0] <= t]
            lab_cov = [k for k in cov if not h_d0[k][1]]
            hit = sum(correct.get((k[0], k[1]), 0) for k in lab_cov)
            res[name] = {
                "n_total": len(keys), "n_covered": len(cov),
                "coverage": round(len(cov) / max(len(keys), 1), 4),
                "p_labelled": round(len(lab_cov) / max(len(cov), 1), 4),
                "acc_given_labelled": round(hit / max(len(lab_cov), 1), 4),
                "act_accuracy": round(hit / max(len(cov), 1), 4)}
        payload["per_ticker"][tk] = res

        print("\n   %s" % tk)
        print("      %-12s %-13s %-13s %-15s %s"
              % ("", "coverage", "P(labelled)", "acc|labelled", "ACT ACCURACY"))
        for name in ("acted", "everything"):
            r = res[name]
            print("      %-12s %2d/%2d (%3.0f%%)  %-13s %-15s %.0f%%"
                  % (name, r["n_covered"], r["n_total"], 100 * r["coverage"],
                     "%.0f%%" % (100 * r["p_labelled"]),
                     "%.0f%%" % (100 * r["acc_given_labelled"]),
                     100 * r["act_accuracy"]))
        d = DISCOVERY[tk]
        print("      discovery was  act %.0f%% at %.0f%% coverage   ->   holdout "
              "act %.0f%% at %.0f%% coverage   (%+.0f pp)"
              % (100 * d["act"], 100 * d["cov"], 100 * res["acted"]["act_accuracy"],
                 100 * res["acted"]["coverage"],
                 100 * (res["acted"]["act_accuracy"] - d["act"])))

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "stage_c_holdout.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("\n   wrote %s" % path)
    print("   The holdout is now spent. Any further tuning needs a new one, and there")
    print("   is no more 2026-05..08 data to take it from.")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--spend-the-holdout", action="store_true", required=True,
                    help="required: makes spending the one-shot budget explicit")
    args = ap.parse_args()
    report(args.corpus, args.out)


if __name__ == "__main__":
    main()
