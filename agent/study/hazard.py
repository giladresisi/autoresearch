"""Stage B: the draw as a per-pool stopping hazard.

`h(pool) = P(the move stops at this pool | it reached this pool)`. Walking the stack outward
and composing the hazards gives a distribution over stopping points, whose argmax is a named
target — so this is scored against exactly the Stage A number (MNQ 56.2%, MES 54.5%) and the
reformulation costs no comparability.

**Why per-pool rather than per-segment.** Same outcome, different estimation problem: 48 MNQ
segments become ~106 pool-observations; the question "will it continue past THIS pool" is
answered by local facts rather than a global ranking; and a per-pool continue/stop estimate is
exactly what a mid-move switching rule consumes, so §7's one-off and its switches become one
estimator at two instants instead of two mechanisms.

**Everything from Stage A survives the change.** reach-m is a degenerate hazard — zero below
the multiple, one above it. Chaining (B8) is a hazard that also reads the gap ahead.

**Estimation discipline, fixed before fitting.** Hazards are bucketed tables, not a fitted
model: with 45 usable sessions an interpretable table is both defensible and expressible as a
sentence the Executor could implement. Every score is **leave-one-session-out** — the table is
built without the session it is scored on — and every interval bootstraps over **sessions**,
never over pool-observations, which are not independent (all pools before the stop are
"continue" by construction).

Result fields are unreadable here as predictors. `outcome == "draw"` supplies the training
label and appears only in `pool_observations`; nothing in the feature set derives from the
move's end.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass

import numpy as np

from agent.study.baseline import eligible_by_segment

SEED = 20260904

#: Feature bucket edges, chosen from Stage A's reported tables and fixed before fitting.
DIST_EDGES = (1.0, 2.0, 3.0)
GAP_EDGES = (0.5, 1.5)
#: A pool with nothing beyond it is right-censored: the move could not have continued, so it
#: contributes its stop but never trains a "continue".
NO_POOL_AHEAD = "none"


def _bucket(x, edges) -> int:
    for i, e in enumerate(edges):
        if x <= e:
            return i
    return len(edges)


@dataclass(frozen=True)
class PoolObs:
    seg: tuple            # (date, segment) -- the clustering unit for every interval
    ticker: str
    rank: int
    dist: float           # from the origin, in avg_range_1h units
    gap_ahead: object     # to the next pool; None when there is none
    gap_behind: object
    tier: str
    cls: str
    stop: bool            # TRAINING LABEL, from the phase-2 draw
    censored: bool        # no pool ahead: the move had nowhere to continue to
    #: B3: the gap ahead in the OTHER instrument's stack, measured at this pool's price
    #: translated into that instrument's space. None when the counterpart is unavailable.
    alt_gap_ahead: object = None


def _stack(lab, elig):
    """One instrument's ordered pool stack for one segment, with its own origin."""
    tk = lab["ticker"]
    rows = elig.get((lab["date"], lab["segment"], tk), [])
    avg = (lab.get("avg_range_1h") or {}).get(tk)
    if not rows or not avg:
        return None
    sign = 1 if lab["direction"] == "up" else -1
    return {"rows": rows, "avg": avg, "sign": sign,
            "origin": rows[0]["price"] - sign * rows[0]["dist_from_start"],
            "stop": next((j for j, r in enumerate(rows)
                          if r.get("outcome") == "draw"), None)}


def pool_observations(labels, cands, tickers=("MNQ", "MES")) -> "list[PoolObs]":
    """One record per pool the move actually REACHED, in stack order.

    Pools beyond the stop are not observations: the move never got to them, so whether it
    would have continued past them is unobserved, not negative.
    """
    elig = eligible_by_segment(cands)
    by_seg: dict = {}
    for lab in labels:
        if lab.get("ticker") in tickers and lab.get("status") == "labelled":
            by_seg.setdefault((lab["date"], lab["segment"]), {})[lab["ticker"]] = lab

    out = []
    for seg, per_tk in by_seg.items():
        stacks = {tk: _stack(lab, elig) for tk, lab in per_tk.items()}
        stacks = {tk: v for tk, v in stacks.items() if v and v["stop"] is not None}
        ratio = None
        if len(stacks) == 2 and all(v["origin"] for v in stacks.values()):
            ratio = stacks["MNQ"]["origin"] / stacks["MES"]["origin"]
        for tk, s in stacks.items():
            other = next((o for o in stacks if o != tk), None)
            o = stacks.get(other)
            d = [r["dist_from_start"] / s["avg"] for r in s["rows"]]
            for j in range(s["stop"] + 1):
                price = s["rows"][j]["price"]
                alt = None
                if o is not None and ratio:
                    p_alt = price / ratio if tk == "MNQ" else price * ratio
                    ahead = [r["price"] for r in o["rows"]
                             if (r["price"] - p_alt) * o["sign"] > 0]
                    if ahead:
                        nearest = min(ahead, key=lambda x: abs(x - p_alt))
                        alt = (nearest - p_alt) * o["sign"] / o["avg"]
                out.append(PoolObs(
                    seg=seg, ticker=tk, rank=j, dist=d[j],
                    gap_ahead=(d[j + 1] - d[j]) if j + 1 < len(d) else None,
                    gap_behind=(d[j] - d[j - 1]) if j > 0 else d[j],
                    tier=s["rows"][j]["tier"], cls=s["rows"][j]["cls"],
                    stop=(j == s["stop"]), censored=(j + 1 >= len(d)),
                    alt_gap_ahead=alt))
    out.sort(key=lambda o: (str(o.seg), o.ticker, o.rank))
    return out


def features(obs: PoolObs, family: str) -> tuple:
    """The bucket key a family conditions on. Adding a family means adding a branch here,
    never widening an existing one."""
    dist = _bucket(obs.dist, DIST_EDGES)
    if family == "B0":
        return (dist,)
    gap = NO_POOL_AHEAD if obs.gap_ahead is None else _bucket(obs.gap_ahead, GAP_EDGES)
    if family == "B8":
        return (dist, gap)
    if family == "B8g":
        # SIMPLIFICATION test, declared 2026-09-04 AFTER seeing the fitted B8 table: the
        # hazard varies strongly down the gap column and weakly across the distance rows,
        # so the question is whether distance earns its place at all. This is model
        # selection between two encodings of one confirmed hypothesis, not a new family.
        return (gap,)
    alt = (NO_POOL_AHEAD if obs.alt_gap_ahead is None
           else _bucket(obs.alt_gap_ahead, GAP_EDGES))
    if family == "B3":
        return (alt,)
    if family == "B8g+B3":
        return (gap, alt)
    if family == "B8xB4":
        return (dist, gap, obs.tier)
    raise KeyError(f"unknown family: {family!r}")


def fit_hazard(train, family: str, prior_weight: float = 3.0) -> dict:
    """Bucket -> hazard, smoothed toward the pooled rate.

    The smoothing is not decoration: several buckets hold fewer than ten observations, and an
    unsmoothed 0/3 would assert a hazard of exactly zero on evidence that cannot support it.
    """
    pooled = (sum(o.stop for o in train) / len(train)) if train else 0.5
    agg: dict = {}
    for o in train:
        k = features(o, family)
        n, s = agg.get(k, (0, 0))
        agg[k] = (n + 1, s + int(o.stop))
    return {k: (s + prior_weight * pooled) / (n + prior_weight)
            for k, (n, s) in agg.items()}, pooled


def predict_stop_rank(seg_obs, hazard, pooled, family: str) -> int:
    """Compose the hazards along one segment's stack into a stopping distribution and take
    its argmax. Survival form: P(stop at j) = h_j * prod_{i<j} (1 - h_i)."""
    best, best_p, surv = 0, -1.0, 1.0
    for j, o in enumerate(sorted(seg_obs, key=lambda x: x.rank)):
        h = hazard.get(features(o, family), pooled)
        p = surv * h
        if p > best_p:
            best, best_p = o.rank, p
        surv *= (1.0 - h)
    return best


def score_loso(obs, family: str, ticker: str) -> dict:
    """Leave-one-session-out induced top-1, plus the per-pool Brier score."""
    v = [o for o in obs if o.ticker == ticker]
    by_seg: dict = {}
    for o in v:
        by_seg.setdefault(o.seg, []).append(o)
    correct, briers = [], []
    for seg, rows in by_seg.items():
        train = [o for o in v if o.seg != seg]
        hz, pooled = fit_hazard(train, family)
        truth = max(o.rank for o in rows if o.stop)
        correct.append((seg, int(predict_stop_rank(rows, hz, pooled, family) == truth)))
        for o in rows:
            briers.append((hz.get(features(o, family), pooled) - int(o.stop)) ** 2)
    n = len(correct)
    return {"family": family, "ticker": ticker, "n_segments": n,
            "n_obs": len(v), "correct": sum(c for _s, c in correct),
            "acc": round(sum(c for _s, c in correct) / max(n, 1), 4),
            "brier": round(st.mean(briers), 4) if briers else None,
            "per_seg": correct}


def bootstrap_delta(a, b, n_boot: int = 2000, seed: int = SEED) -> dict:
    """Session-clustered bootstrap of (family A accuracy - family B accuracy).

    Resamples SESSIONS. Resampling pool-observations would treat the pools of one session as
    independent draws when every pool before the stop is a "continue" by construction, and
    would report an interval far narrower than the evidence supports.
    """
    da, db = dict(a["per_seg"]), dict(b["per_seg"])
    segs = [s for s in da if s in db]
    if not segs:
        return {"delta": None}
    rng = np.random.default_rng(seed)
    obs = (sum(da[s] for s in segs) - sum(db[s] for s in segs)) / len(segs)
    draws = np.empty(n_boot)
    idx = np.arange(len(segs))
    for i in range(n_boot):
        pick = rng.choice(idx, len(idx), replace=True)
        draws[i] = st.mean([da[segs[j]] - db[segs[j]] for j in pick])
    lo, hi = float(np.quantile(draws, 0.05)), float(np.quantile(draws, 0.95))
    return {"delta": round(obs, 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "n_segments": len(segs),
            "p_gt_0": round(float(np.mean(draws > 0)), 4)}


def permutation_gap_test(obs, ticker: str, n_perm: int = 2000, seed: int = SEED) -> dict:
    """Does the gap ahead predict stopping, beyond what the pool's RANK already predicts?

    The mechanical objection to the chaining effect is that a wide gap catches stops by
    construction — the move stops at some distance and lands in whichever interval is
    widest. This shuffles the stop/continue labels WITHIN each rank stratum, which holds
    the rank distribution fixed and breaks only the gap-to-stop link. The statistic is the
    stoppers' median gap minus the continuers'.
    """
    v = [o for o in obs if o.ticker == ticker and o.gap_ahead is not None]
    if not v:
        return {"ticker": ticker, "n_obs": 0}

    def stat(flags):
        s = [x.gap_ahead for x, f in zip(v, flags) if f]
        c = [x.gap_ahead for x, f in zip(v, flags) if not f]
        return (st.median(s) - st.median(c)) if s and c else 0.0

    base = [x.stop for x in v]
    observed = stat(base)
    strata: dict = {}
    for i, x in enumerate(v):
        strata.setdefault(x.rank, []).append(i)
    rng = np.random.default_rng(seed)
    null = np.empty(n_perm)
    for b in range(n_perm):
        f = list(base)
        for _r, idxs in strata.items():
            vals = [base[j] for j in idxs]
            rng.shuffle(vals)
            for j, val in zip(idxs, vals):
                f[j] = val
        null[b] = stat(f)
    return {"ticker": ticker, "n_obs": len(v),
            "n_segments": len({x.seg for x in v}),
            "observed": round(observed, 4),
            "null_median": round(float(np.median(null)), 4),
            "p": round(float(np.mean(null >= observed)), 5),
            "stop_median_gap": round(st.median([x.gap_ahead for x in v if x.stop]), 4),
            "continue_median_gap": round(
                st.median([x.gap_ahead for x in v if not x.stop]), 4)}


#: B9: act only when the nearest eligible pool is within this multiple of `avg_range_1h`.
#: 2.0 is the joint optimum on both instruments; the curve rises to it and falls after, which
#: is the shape of a real optimum rather than a fitted edge. Chosen in-sample — Stage C tests it.
ABSTAIN_MAX_D0 = 2.0
ABSTAIN_THRESHOLDS = (1.0, 1.5, 2.0, 2.5, None)


def segment_d0(labels, cands, tickers=("MNQ", "MES")) -> dict:
    """`(date, segment, ticker) -> (distance to the NEAREST eligible pool, was unexplained)`.

    `d0` is forward-computable at the fill: it needs the candidate universe and the origin,
    both of which exist at the decision instant. `unexplained` is the outcome it predicts.
    """
    elig = eligible_by_segment(cands)
    out = {}
    for lab in labels:
        tk = lab.get("ticker")
        if tk not in tickers:
            continue
        rows = elig.get((lab["date"], lab["segment"], tk), [])
        avg = (lab.get("avg_range_1h") or {}).get(tk)
        if not rows or not avg:
            continue
        out[(lab["date"], lab["segment"], tk)] = (
            rows[0]["dist_from_start"] / avg, lab.get("status") == "unexplained")
    return out


def abstain_curve(labels, cands, obs, ticker: str,
                  thresholds=ABSTAIN_THRESHOLDS) -> "list[dict]":
    """Act only where `d0 <= t`; report what that buys.

    **act_accuracy** is the honest production number: of every session we ACT on, the share
    where we name the right pool. It counts an unexplained session as a miss, because in
    production we would have named a target there and been wrong — which is precisely what
    `acc | labelled` hides, and why B8g's 62-68% overstates what shipping it would deliver.
    """
    d0 = segment_d0(labels, cands, (ticker,))
    per = dict(score_loso(obs, "B8g", ticker)["per_seg"])
    rows = []
    keys = [k for k in d0 if k[2] == ticker]
    for t in thresholds:
        cov = [k for k in keys if t is None or d0[k][0] <= t]
        if not cov:
            continue
        lab_cov = [k for k in cov if not d0[k][1]]
        correct = sum(per.get((k[0], k[1]), 0) for k in lab_cov)
        rows.append({
            "threshold": t, "n_total": len(keys), "n_covered": len(cov),
            "coverage": round(len(cov) / len(keys), 4),
            "p_labelled": round(len(lab_cov) / len(cov), 4),
            "acc_given_labelled": round(correct / max(len(lab_cov), 1), 4),
            "act_accuracy": round(correct / len(cov), 4)})
    return rows
