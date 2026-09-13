"""Stage A: what any phase-3 rule has to beat.

Nothing here searches for anything. It measures three things that must be settled before a
rule search can mean anything, and a fourth that decides whether the search is even
well-posed.

**Why an ordinal baseline at all.** The phase-2 label is the *last* eligible candidate the
move reached, so predicting it is predicting how far the move will go, counted in pools
cleared — not which pool is intrinsically special. The cheapest theory that could be true is
"the move clears k pools", and until that is measured no richer theory means anything. Two
encodings of it are scored, because they fail differently:

  * **fixed-k** — always name the k-th nearest eligible pool. Simple, and scale-free by
    construction, but blind to how far apart the pools happen to sit that day.
  * **reach-m** — name the furthest eligible pool within `m x avg_range_1h` of the origin.
    This is the one that could become a Planner rule, since it speaks in distance.

**Why class pull is re-measured here (G1).** Phase 2 reported `session_extreme` and
`open_price` drawing far beyond their census. Their mean normalised distance ranks are 0.21
and 0.13 against day 0.52 and week 0.81 — the identical ordering. A rule that stops at the
last pool it reaches names near pools disproportionately whatever their tier, so the class
effect has to be re-measured INSIDE distance buckets or it is proximity wearing a costume.

**Result fields are not readable from here.** `overshoot`, `lag_min`, `own_extreme` and
`extreme_ts` are all computed from the move's end. A source-level test asserts none of them
appears in this module; `reached_ts` is the one exception and appears only in `already_reached`,
which asks a question about the DECISION instant rather than making a prediction.
"""
from __future__ import annotations

import statistics as st

import pandas as pd

SEG_KEY = ("date", "segment", "ticker")
FIXED_K = tuple(range(8))
REACH_MULTS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0)
PROXY_MINUTES = (2, 5, 10)


def _key(row) -> tuple:
    return tuple(row[k] for k in SEG_KEY)


def eligible_by_segment(cand_rows) -> dict:
    """`(date, segment, ticker) -> [eligible rows, nearest first]`.

    Ineligible rows do not consume a rank: a pool that was already swept, or that sits
    behind the origin, was never a choice the decision could have made.
    """
    out: dict = {}
    for r in cand_rows:
        if r.get("outcome") == "ineligible" or r.get("dist_from_start") is None:
            continue
        out.setdefault(_key(r), []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["dist_from_start"])
    return out


def label_rank(elig_rows) -> "int | None":
    """0-based rank of the drawn-to candidate among eligible ones ordered by distance."""
    for i, r in enumerate(elig_rows):
        if r.get("outcome") == "draw":
            return i
    return None


def _labelled(labels):
    return [r for r in labels if r.get("status") == "labelled"]


def ordinal_baseline(labels, cands, tickers=("MNQ", "MES")) -> dict:
    """Rank distribution of the draw, and the top-1 accuracy of naming a fixed rank."""
    elig = eligible_by_segment(cands)
    out = {}
    for tk in tickers:
        ranks, n_elig = [], []
        for lab in _labelled(labels):
            if lab["ticker"] != tk:
                continue
            rows = elig.get(_key(lab), [])
            rk = label_rank(rows)
            if rk is not None:
                ranks.append(rk)
                n_elig.append(len(rows))
        if not ranks:
            out[tk] = {"n": 0}
            continue
        hist = {k: ranks.count(k) for k in FIXED_K}
        hist["8+"] = sum(1 for r in ranks if r >= 8)
        out[tk] = {
            "n": len(ranks), "hist": hist,
            "median_rank": st.median(ranks), "mean_rank": round(st.mean(ranks), 3),
            "median_n_eligible": st.median(n_elig),
            # Coverage differs per k: naming "the 5th nearest" is impossible on a segment
            # with 4 eligible pools, and scoring that as a miss would flatter larger k.
            "fixed_k": {k: {"correct": hist[k],
                            "coverage": sum(1 for m in n_elig if m > k),
                            "acc": round(hist[k] / max(sum(1 for m in n_elig if m > k), 1), 4)}
                        for k in FIXED_K},
        }
    return out


def reach_baseline(labels, cands, mults=REACH_MULTS, tickers=("MNQ", "MES")) -> dict:
    """"Name the furthest eligible pool within `m x avg_range_1h` of the origin."."""
    elig = eligible_by_segment(cands)
    out = {}
    for tk in tickers:
        out[tk] = {}
        for m in mults:
            correct = covered = total = 0
            for lab in _labelled(labels):
                if lab["ticker"] != tk:
                    continue
                rows = elig.get(_key(lab), [])
                if not rows:
                    continue
                total += 1
                avg = (lab.get("avg_range_1h") or {}).get(tk)
                if not avg:
                    continue
                inside = [r for r in rows if r["dist_from_start"] <= m * avg]
                if not inside:
                    continue
                covered += 1
                correct += int(inside[-1].get("outcome") == "draw")
            out[tk][m] = {"total": total, "coverage": covered, "correct": correct,
                          "acc": round(correct / max(covered, 1), 4),
                          "acc_over_all": round(correct / max(total, 1), 4)}
    return out


def distance_buckets(elig_rows, n: int = 4) -> dict:
    """Rank -> bucket index, splitting each segment's eligible list into `n` even parts.

    Bucketing WITHIN a segment rather than globally is what holds proximity fixed: a
    segment with 3 eligible pools and one with 20 both contribute a "nearest quarter".
    """
    total = len(elig_rows)
    if total == 0:
        return {}
    return {i: min(int(i * n / total), n - 1) for i in range(total)}


def class_pull_by_distance(labels, cands, n_buckets: int = 4,
                           tickers=("MNQ", "MES")) -> dict:
    """G1: per-class draw share vs census, computed INSIDE distance buckets.

    Returns `{ticker: {bucket: {"draws": [cls...], "eligible": [cls...]}}}` — the caller
    runs `nulls.null_b` per bucket so the null model stays in one place.
    """
    elig = eligible_by_segment(cands)
    out = {tk: {b: {"draws": [], "eligible": []} for b in range(n_buckets)}
           for tk in tickers}
    for lab in _labelled(labels):
        tk = lab["ticker"]
        if tk not in out:
            continue
        rows = elig.get(_key(lab), [])
        buckets = distance_buckets(rows, n_buckets)
        for i, r in enumerate(rows):
            b = buckets[i]
            out[tk][b]["eligible"].append(r["cls"])
            if r.get("outcome") == "draw":
                out[tk][b]["draws"].append(r["cls"])
    return out


def null_cases(labels, cands, reached_only: bool = False,
               tickers=("MNQ", "MES")) -> dict:
    """Case lists for `nulls.null_a`, per instrument.

    `reached_only=True` is G2's tighter null: the pool is restricted to candidates the
    move actually CROSSED. "Last reached" is by construction the furthest-along crossed
    pool, so it must beat a null drawn from the whole eligible set — which includes every
    pool the move never approached. The honest question is whether, among the pools it
    did cross, the one it stopped after was closer to the turn than the others.

    `own_extreme` is read here and ONLY here: this is the null's own scoring, not a
    predictor.
    """
    by_seg = eligible_by_segment(cands)
    out = {tk: [] for tk in tickers}
    for lab in _labelled(labels):
        tk = lab["ticker"]
        if tk not in out or lab.get("own_extreme") is None:
            continue
        rows = by_seg.get(_key(lab), [])
        if reached_only:
            rows = [r for r in rows if r.get("reached_ts") is not None]
        pool = [abs(lab["own_extreme"] - r["price"]) for r in rows]
        if len(pool) >= (2 if reached_only else 1):
            out[tk].append({"label_dist": abs(lab["own_extreme"] - lab["price"]),
                            "candidate_dists": pool})
    return out


def already_reached(labels, cands, minutes=PROXY_MINUTES,
                    tickers=("MNQ", "MES")) -> dict:
    """A4: had the draw already been crossed by the time a fill could exist?

    If a large share of draws are taken inside the first two minutes then prediction at
    the decision instant is degenerate — the rule would be naming something the tape has
    already passed — and the target has to become the next pool after it.

    Also reports the draw's rank among candidates STILL UNREACHED at that instant, which
    is the baseline the shifted target would be scored against.
    """
    elig = eligible_by_segment(cands)
    out = {}
    for tk in tickers:
        out[tk] = {}
        for m in minutes:
            taken = total = 0
            ranks = []
            for lab in _labelled(labels):
                if lab["ticker"] != tk or not lab.get("reached_ts"):
                    continue
                total += 1
                cut = pd.Timestamp(lab["start_ts"]) + pd.Timedelta(minutes=m)
                if pd.Timestamp(lab["reached_ts"]) <= cut:
                    taken += 1
                    continue
                open_rows = [r for r in elig.get(_key(lab), [])
                             if r.get("reached_ts") is None
                             or pd.Timestamp(r["reached_ts"]) > cut]
                rk = label_rank(open_rows)
                if rk is not None:
                    ranks.append(rk)
            out[tk][m] = {"total": total, "already_reached": taken,
                          "frac": round(taken / max(total, 1), 4),
                          "median_rank_among_unreached": (st.median(ranks) if ranks
                                                          else None),
                          "n_ranked": len(ranks)}
    return out
