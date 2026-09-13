"""Calibration sweep scorer (GIL-44).

Parses the RESULT block from each calibration/cuts/<id>/poc-result.md, joins the ground-truth
TSV (<global>/gil44_calibration_truth.tsv), labels each cut, and prints:
  - call x truth matrices (next-move on the 4h horizon; daily on EOD delta)
  - direction hit rate per confidence bucket
  - the entry-gate threshold curve (directional-call accuracy at conf >= X)

Truth labels (thresholds are calibration constants — tune here, not in the prep script):
  next-move (4h): up   if up_exc >= MOVE_MIN and up_exc >= RATIO * dn_exc
                  down if dn_exc >= MOVE_MIN and dn_exc >= RATIO * up_exc
                  else chop
  daily (EOD):    up/down if |eod_delta| >= DAY_MIN else neutral
"""

import os
import re

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
TRUTH_TSV = os.path.expanduser(r"~/projects/auto-co-trader/global/gil44_calibration_truth.tsv")
MOVE_MIN = 100.0
RATIO = 2.0
DAY_MIN = 150.0
KEYS = ("daily_direction", "daily_confidence", "next_direction", "next_confidence")


def parse_result(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    block = txt[txt.rfind("RESULT:"):]
    out = {}
    for k in KEYS + ("move_target", "long_resolution", "short_resolution"):
        m = re.search(rf"^{k}\s*:\s*([^\n|]+)", block, re.M | re.I)
        out[k] = m.group(1).strip().lower() if m else None
    return out


def label_next(r):
    if r.h4_up_exc >= MOVE_MIN and r.h4_up_exc >= RATIO * r.h4_dn_exc:
        return "up"
    if r.h4_dn_exc >= MOVE_MIN and r.h4_dn_exc >= RATIO * r.h4_up_exc:
        return "down"
    return "chop"


def label_daily(r):
    if r.eod_delta >= DAY_MIN:
        return "up"
    if r.eod_delta <= -DAY_MIN:
        return "down"
    return "neutral"


def main():
    truth = pd.read_csv(TRUTH_TSV, sep="\t")
    truth["next_truth"] = truth.apply(label_next, axis=1)
    truth["daily_truth"] = truth.apply(label_daily, axis=1)

    rows = []
    for cid in sorted(os.listdir(os.path.join(HERE, "cuts"))):
        p = os.path.join(HERE, "cuts", cid, "poc-result.md")
        if not os.path.exists(p):
            continue
        r = parse_result(p)
        r["run_id"] = cid
        r["cut_id"] = re.sub(r"__r\d+$", "", cid)  # replicates share the base cut's truth
        rows.append(r)
    if not rows:
        print("no poc-result.md files yet"); return
    allres = pd.DataFrame(rows).merge(truth, on="cut_id", how="inner")

    # ---- judgment-variance report (triplicated cuts) ----
    reps = allres[allres.groupby("cut_id")["run_id"].transform("count") > 1]
    if len(reps):
        print("== JUDGMENT VARIANCE (triplicated cuts) ==")
        for key in ("daily_direction", "daily_confidence", "next_direction", "next_confidence"):
            agree = reps.groupby("cut_id")[key].nunique()
            n_cuts = len(agree)
            print(f"{key}: unanimous on {(agree == 1).sum()}/{n_cuts} cuts"
                  + (f"  (split: {', '.join(agree[agree > 1].index)})" if (agree > 1).any() else ""))
        print()

    # hit-rate stats use one row per cut: the base run (replicas excluded to avoid double-weighting)
    res = allres[~allres["run_id"].str.contains("__r")]
    print(f"scored {len(res)} of {len(truth)} cuts ({len(allres) - len(res)} replicate runs)\n")

    for what, call, conf, tcol in (("NEXT-MOVE (4h horizon)", "next_direction", "next_confidence", "next_truth"),
                                   ("DAILY (EOD delta)", "daily_direction", "daily_confidence", "daily_truth")):
        print(f"== {what} ==")
        print(pd.crosstab(res[call], res[tcol], margins=True), "\n")
        d = res[res[call].isin(["up", "down"])]
        if len(d):
            d = d.assign(hit=d[call] == d[tcol])
            print("directional calls by confidence:")
            print(d.groupby(conf)["hit"].agg(["count", "mean"]).rename(columns={"mean": "hit_rate"}), "\n")
            for gate in ("high", "medium"):
                allowed = {"high": ["high"], "medium": ["high", "medium"]}[gate]
                g = d[d[conf].isin(allowed)]
                if len(g):
                    print(f"entry gate 'conf >= {gate}': {len(g)} calls, hit rate {g['hit'].mean():.0%}")
        print()


if __name__ == "__main__":
    main()
