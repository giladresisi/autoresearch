"""Build the phase-2 label corpus: what each 09:30 move was drawn to.

Reads the committed session skeleton, and for every PRIMARY and SECONDARY segment builds
the candidate universe at that segment's move-start and applies the draw rule, per
instrument. Counter segments are observations (spec §4.3), not labelled units.

Outputs, into `--out`:
  labels.jsonl      one row per segment per instrument
  candidates.jsonl  one row per candidate per segment per instrument, including the ones
                    the move passed through and the ones it never approached -- spec §3.4
                    needs the losers or the attractiveness question cannot be answered
                    afterwards without a full re-run
  summary.json      counts only; every rate lives in report_labels.py

No wall clock, no randomness: two builds are byte-identical.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import types

import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.candidates import universe                       # noqa: E402
from agent.study.facts_source import StudyFacts                   # noqa: E402
from agent.study.holdout import is_holdout                        # noqa: E402
from agent.study.labelling import (DEFAULT_BAND_MULT,             # noqa: E402
                                   STATUS_LABELLED, label_segment)

SKELETON = os.path.join(_REPO, ".agents", "session-skeleton", "session_skeleton.jsonl")
LABELLED_ROLES = ("primary", "secondary")
TICKERS = ("MNQ", "MES")


def _segments(path: str, dates=None, limit=None):
    """Skeleton rows -> segment namespaces, in file order."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("role") not in LABELLED_ROLES:
                continue
            if dates and r["date"] not in dates:
                continue
            out.append(types.SimpleNamespace(
                date=datetime.date.fromisoformat(r["date"]), index=r["index"],
                role=r["role"], censored=bool(r["censored"]),
                direction=r["direction"], extent=r["extent"],
                start_ts=pd.Timestamp(r["start_ts"]), start_price=r["start_price"],
                extreme_ts=pd.Timestamp(r["extreme_ts"]),
                extreme_price=r["extreme_price"]))
            if limit and len({s.date for s in out}) > limit:
                out.pop()
                break
    return out


def build(out_dir: str, dates=None, limit=None, band_mult: float = DEFAULT_BAND_MULT,
          skeleton: str = SKELETON) -> dict:
    segments = _segments(skeleton, dates=dates, limit=limit)
    sf = StudyFacts()
    bars = {tk: sf.bars_1m(tk) for tk in TICKERS}

    os.makedirs(out_dir, exist_ok=True)
    lab_path = os.path.join(out_dir, "labels.jsonl")
    cand_path = os.path.join(out_dir, "candidates.jsonl")
    n_lab = n_cand = 0
    stats = {"segments": 0, "labelled": 0, "unexplained": 0, "censored_segments": 0}

    with open(lab_path, "w", encoding="utf-8", newline="\n") as lf, \
            open(cand_path, "w", encoding="utf-8", newline="\n") as cf:
        for seg in segments:
            bundle = sf.bundle_at(seg.start_ts)
            labels, rows = label_segment(seg, bundle, bars, universe(bundle),
                                         band_mult=band_mult)
            stats["segments"] += 1
            stats["censored_segments"] += int(seg.censored)
            base = {"date": str(seg.date), "segment": seg.index, "role": seg.role,
                    "censored": seg.censored, "direction": seg.direction,
                    "extent": seg.extent, "holdout": is_holdout(seg.date),
                    "start_ts": str(seg.start_ts), "extreme_ts": str(seg.extreme_ts),
                    "avg_range_1h": {tk: round(float(v), 4)
                                     for tk, v in (bundle.avg_range_1h or {}).items()}}
            for tkr in TICKERS:
                lbl = labels[tkr]
                lf.write(json.dumps({**base, **lbl.to_dict()}) + "\n")
                n_lab += 1
                stats["labelled" if lbl.status == STATUS_LABELLED
                      else "unexplained"] += 1
            for r in rows:
                cf.write(json.dumps({**base, **r.to_dict()}) + "\n")
                n_cand += 1

    stats.update({"sessions": len({s.date for s in segments}),
                  "label_rows": n_lab, "candidate_rows": n_cand,
                  "band_mult": band_mult, "tickers": list(TICKERS)})
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(stats, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_REPO, ".agents", "label-corpus"))
    ap.add_argument("--dates", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--band-mult", type=float, default=DEFAULT_BAND_MULT)
    ap.add_argument("--skeleton", default=SKELETON)
    args = ap.parse_args()
    stats = build(args.out, dates=args.dates, limit=args.limit,
                  band_mult=args.band_mult, skeleton=args.skeleton)
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
