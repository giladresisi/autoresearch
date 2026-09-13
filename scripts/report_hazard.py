"""Stage B report: the draw as a per-pool stopping hazard.

Regenerates every number plan 29 §B.5 quotes. Discovery sessions only — the 21 held-out
sessions are not read, not counted and not summarised; Stage C spends that budget once.

Writes a JSON alongside the printed report so a later phase can diff against it rather than
re-deriving it from prose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.hazard import (DIST_EDGES, GAP_EDGES, abstain_curve,     # noqa: E402
                                bootstrap_delta, features, fit_hazard,
                                permutation_gap_test, pool_observations, score_loso)

CORPUS = os.path.join(_REPO, ".agents", "label-corpus")
OUT_DIR = os.path.join(_REPO, ".agents", "rule-search")
TICKERS = ("MNQ", "MES")
FAMILIES = ("B0", "B8", "B8g", "B8xB4", "B3", "B8g+B3")
#: Stage A's reach-m optimum, the bar every family is measured against. In-sample (its `m`
#: was chosen on these same sessions), so a LOSO family clearing it is clearing a
#: FLATTERED number.
STAGE_A_BAR = {"MNQ": 0.562, "MES": 0.545}
DIST_LABELS = ("<=1x", "1-2x", "2-3x", ">3x")
GAP_LABELS = ("<=0.5x", "0.5-1.5x", ">1.5x")


def _load(d):
    def rd(name):
        return [json.loads(l) for l in open(os.path.join(d, name), encoding="utf-8")
                if l.strip() and json.loads(l)["role"] == "primary"
                and not json.loads(l)["holdout"]]
    return rd("labels.jsonl"), rd("candidates.jsonl")


def _hdr(title):
    print("\n%s\n%s\n%s" % ("-" * 74, title, "-" * 74))


def report(corpus: str, out_dir: str) -> dict:
    labels, cands = _load(corpus)
    obs = pool_observations(labels, cands)
    payload: dict = {"families": {}, "permutation": {}, "deltas": {}, "tables": {}}

    print("=" * 74)
    print("CYCLE-4 PHASE 3 / STAGE B — THE STOPPING HAZARD")
    print("   %d pool-observations from %d discovery segments. Holdout not read."
          % (len(obs), len({o.seg for o in obs})))
    print("=" * 74)

    _hdr("B8 gate: does the gap ahead beat a rank-stratified permutation?")
    for tk in TICKERS:
        r = permutation_gap_test(obs, tk)
        payload["permutation"][tk] = r
        print("   %-4s stoppers' median gap %.2f vs continuers' %.2f   (n=%d obs / %d seg)"
              % (tk, r["stop_median_gap"], r["continue_median_gap"], r["n_obs"],
                 r["n_segments"]))
        print("        observed diff %+.2f   null median %+.2f   p=%.5f   %s"
              % (r["observed"], r["null_median"], r["p"],
                 "SURVIVES" if r["p"] < 0.05 else "does not survive"))

    _hdr("LOSO induced top-1 and per-pool Brier, vs the Stage A bar")
    for tk in TICKERS:
        print("   %s   (bar %.1f%%, in-sample)" % (tk, 100 * STAGE_A_BAR[tk]))
        for fam in FAMILIES:
            r = score_loso(obs, fam, tk)
            payload["families"]["%s/%s" % (fam, tk)] = {
                k: v for k, v in r.items() if k != "per_seg"}
            print("      %-7s acc=%5.1f%%  (%2d/%2d)   brier=%.4f   vs bar %+5.1f pp"
                  % (fam, 100 * r["acc"], r["correct"], r["n_segments"], r["brier"],
                     100 * (r["acc"] - STAGE_A_BAR[tk])))

    _hdr("Session-clustered bootstrap of the differences (90% interval)")
    pairs = (("B8", "B0"), ("B8g", "B8"), ("B8xB4", "B8"),
             ("B8g+B3", "B8g"), ("B3", "B8g"))
    for tk in TICKERS:
        for a, b in pairs:
            d = bootstrap_delta(score_loso(obs, a, tk), score_loso(obs, b, tk))
            payload["deltas"]["%s-%s/%s" % (a, b, tk)] = d
            print("   %-4s %-9s %+6.1f pp  [%+.1f, %+.1f]   P(>0)=%.3f"
                  % (tk, "%s-%s" % (a, b), 100 * d["delta"], 100 * d["lo"],
                     100 * d["hi"], d["p_gt_0"]))

    _hdr("The fitted rule, as a table:  h = P(stop here | reached here)")
    for tk in TICKERS:
        v = [o for o in obs if o.ticker == tk]
        hz, pooled = fit_hazard(v, "B8")
        cnt: dict = {}
        for o in v:
            k = features(o, "B8")
            n, s = cnt.get(k, (0, 0))
            cnt[k] = (n + 1, s + int(o.stop))
        payload["tables"][tk] = {str(k): [round(h, 4), cnt[k][0]]
                                 for k, h in sorted(hz.items(), key=lambda kv: str(kv[0]))}
        print("   %s   (pooled hazard %.2f)" % (tk, pooled))
        print("      %-8s | %-12s %-12s %-12s %s"
              % ("dist", *GAP_LABELS, "no pool ahead"))
        for di, dl in enumerate(DIST_LABELS):
            cells = []
            for gi in (0, 1, 2, "none"):
                k = (di, gi)
                cells.append("%.2f (n=%d)" % (hz[k], cnt[k][0]) if k in hz else "    -")
            print("      %-8s | %-12s %-12s %-12s %s" % (dl, *cells))

    print("\n   Read as a sentence: the move keeps going while the next pool is close and")
    print("   stops when the next one is more than ~1.5 hourly ranges away — regardless of")
    print("   how far it has already travelled (B8g matches B8 without reading distance).")

    _hdr("B9: abstain when the nearest pool is far — the honest production number")
    payload["abstain"] = {}
    print("   %-4s %-6s %-12s %-13s %-14s %s"
          % ("", "t", "coverage", "P(labelled)", "acc|labelled", "ACT ACCURACY"))
    for tk in TICKERS:
        payload["abstain"][tk] = abstain_curve(labels, cands, obs, tk)
        for r in payload["abstain"][tk]:
            print("   %-4s %-6s %2d/%2d (%3.0f%%)  %-13s %-14s %.0f%%"
                  % (tk, "all" if r["threshold"] is None else "%.1f" % r["threshold"],
                     r["n_covered"], r["n_total"], 100 * r["coverage"],
                     "%.0f%%" % (100 * r["p_labelled"]),
                     "%.0f%%" % (100 * r["acc_given_labelled"]),
                     100 * r["act_accuracy"]))
    print("")
    print("   act_accuracy counts an unexplained session as a miss, because in")
    print("   production we would have named a target there and been wrong.")
    print("   It is what shipping delivers; acc|labelled is not.")

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "stage_b_hazard.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("\n   wrote %s" % path)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()
    report(args.corpus, args.out)


if __name__ == "__main__":
    main()
