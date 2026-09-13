"""Spec §4.2's second discipline: the null models, without which a hit-rate is decoration.

With two instruments and a couple of dozen pools apiece, *something* is almost always swept
near a turn. Two questions therefore have to be answered before any labelled rate means
anything.

**Null A — is the draw closer to the turn than an arbitrary eligible pool?** The draw rule
names something whenever anything was reached, so its "hit rate" is 100% by construction and
says nothing. What is testable is the labelled candidate's distance to the move's own extreme
against the distance of a uniformly-drawn eligible candidate from the same session. If the
draw is no closer than chance, the label is census rather than structure.

**Null B — does a class draw more than its census?** A class that is 40% of eligible
candidates and 40% of draws has no pull; it merely has population. This is spec §3.4's
attractiveness hypothesis made falsifiable, and it is the reason the corpus records what the
move passed THROUGH and not only what it stopped at.

Determinism is not optional here. A fixed `SEED` and an explicit generator; an unseeded draw
would make the report unreproducible, which in this project is indistinguishable from untrue.
"""
from __future__ import annotations

import numpy as np

SEED = 20260903


def _quantiles(xs, ps=(0.1, 0.5, 0.9)):
    a = np.asarray(xs, dtype=float)
    return [float(np.quantile(a, p)) for p in ps]


def null_a(cases, n_draws: int = 500, seed: int = SEED) -> dict:
    """`cases` = `[{"label_dist": float, "candidate_dists": [float, ...]}, ...]`.

    `label_dist` is |move-end − label price| in the instrument's own space, and
    `candidate_dists` the same for every eligible candidate that session.

    Returns the observed median against the null distribution of medians, plus
    `beats_null_frac` — the share of null draws the observed median is at least as close
    as. That is the number to read: ~0.5 means the label is census.
    """
    cases = [c for c in cases if c.get("candidate_dists")]
    if not cases:
        return {"n": 0, "observed_median": None, "null_median": None,
                "null_p10": None, "null_p90": None, "beats_null_frac": None}

    rng = np.random.default_rng(seed)
    observed = float(np.median([c["label_dist"] for c in cases]))
    pools = [np.asarray(c["candidate_dists"], dtype=float) for c in cases]
    medians = np.empty(n_draws, dtype=float)
    for i in range(n_draws):
        medians[i] = np.median([p[rng.integers(len(p))] for p in pools])
    p10, p50, p90 = _quantiles(medians)
    return {"n": len(cases), "observed_median": round(observed, 4),
            "null_median": round(p50, 4), "null_p10": round(p10, 4),
            "null_p90": round(p90, 4),
            "beats_null_frac": round(float(np.mean(medians >= observed)), 4)}


def null_b(draw_classes, eligible_classes, n_boot: int = 2000,
           seed: int = SEED) -> dict:
    """Per-class draw share vs eligible share, with a bootstrap interval on the delta.

    `draw_classes` = one class per labelled segment. `eligible_classes` = one class per
    eligible candidate across all segments. A class whose interval straddles zero has
    shown no pull beyond its census.
    """
    draws = list(draw_classes)
    elig = list(eligible_classes)
    classes = sorted(set(draws) | set(elig))
    if not draws or not elig:
        return {c: {"draw_share": 0.0, "eligible_share": 0.0, "delta": 0.0,
                    "lo": 0.0, "hi": 0.0, "n_draws": 0} for c in classes}

    rng = np.random.default_rng(seed)
    d_idx = np.asarray([classes.index(c) for c in draws])
    e_share = np.asarray([elig.count(c) / len(elig) for c in classes])
    d_share = np.asarray([draws.count(c) / len(draws) for c in classes])

    boot = np.empty((n_boot, len(classes)), dtype=float)
    for i in range(n_boot):
        pick = d_idx[rng.integers(0, len(d_idx), len(d_idx))]
        counts = np.bincount(pick, minlength=len(classes)).astype(float)
        boot[i] = counts / counts.sum() - e_share

    out = {}
    for j, c in enumerate(classes):
        lo, _mid, hi = _quantiles(boot[:, j], (0.05, 0.5, 0.95))
        out[c] = {"draw_share": round(float(d_share[j]), 4),
                  "eligible_share": round(float(e_share[j]), 4),
                  "delta": round(float(d_share[j] - e_share[j]), 4),
                  "lo": round(lo, 4), "hi": round(hi, 4),
                  "n_draws": draws.count(c)}
    return out
