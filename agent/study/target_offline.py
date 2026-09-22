"""Offline target selection: what would each selector have named, and where did price go.

Given a date, a clock time, a direction and a ticker, this prints the target three
selectors would name at that instant, side by side, plus a separately-marked lookahead
block showing what actually happened. It is a **measurement instrument, not a production
path**: nothing here is imported by the Executor, and building it changes no rule.

**There is no target selection at entry in production.** The DOL is an L1 decision taken
once at the 09:20 boundary (`planner.derive_plan` copies `thesis["dol"]` verbatim; the
Executor then reads that frozen price for its take-profit, its `dol_reached` exhaustion and
its remaining-room gate). Nothing between 09:20 and the fill reconsiders it. Re-running the
menu at an entry instant -- track T2 below -- is a thing production has never done.

The three tracks:

* **T1 "09:20 prod"** -- `build_menus` at the session's L1 boundary, nearest-first `D1`. The
  recorded L1 thesis, when one exists, is reported ALONGSIDE it and never as it: what the
  menu offers and what a model picked off it are different objects.
* **T2 "entry prod"** -- the same production rule, `build_menus` unchanged, re-anchored at
  the requested instant. The honest production-equivalent baseline for an entry decision.
* **T3 "entry B8g" (CLOSED, kept for reproducibility)** -- cycle 4's hazard argmax applied
  to **T2's own rows**.

**Verdicts, measured over the 84-date corpus at optimal fills (`l2-target-selection.md`).**
Captured points from the fill, against a 31,867-pt perfect-exit ceiling:
T1 11,814 / **T2 12,966** / T3 12,553 / menu oracle 16,486.

* **B8g is CLOSED.** It ranks the draw better (44.0% vs 29.8% draw-hits, discordant pairs
  18-6, p=0.023) and still captures 413 FEWER points, because it aims farther and fills less
  often (60.8% vs 67.1%). Ranking accuracy and captured points are different objectives.
  The implementation stays here so that result remains reproducible -- not because it is a
  live proposal.
* **T2 over T1 is worth +1,152 pts (+8.9%)** -- the same rule asked later. That is a
  CANDIDATE, not a rule: see `l2-target-selection.md` sec 7 and step 0 of the change protocol.
* Every number above assumes a perfect fill and carries **no stop and no losing side**, so
  none of it is P&L. `regression.py` is the only thing that settles it.

**T3 is the fix for a recorded MUST-FIX.** `l2-targets.md` records that the cycle-4 study
never used production's candidate set: the study universe took every level, while
`_dol_menu` also applies the proximity floor, the wrong-side filter, P1 suppression and
depletion. Ranking `build_menus`'s rows rather than a study-invented universe **is** that
fix.

**The slicing rule is not reimplemented.** `bundle_for_boundary` is the ONE
boundary -> slice rule, reached here through `StudyFacts`; a second copy of it is a failure
this project has already paid for twice. `OfflineFacts` only changes which parquet it reads.

**Lookahead is quarantined.** Everything under `evaluate()` reads bars after the instant.
No track function receives it, and `test_no_track_can_see_past_the_entry_instant` rebuilds
every track against a bar frame truncated at the instant to prove it.

**MES is out of scope** (see `offline-target-selection.md` sec 1.3 and
`derive_facts.build_menus`'s own "DOL remains MNQ-only"): the swept/depleted view the menu
consumes comes from MNQ-rendered facts, so a half-wired MES menu would be a silently wrong
one. It raises instead.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
from dataclasses import dataclass, field

import pandas as pd

from agent.study.candidates import Candidate, classify
from agent.study.facts_source import StudyFacts
from agent.study.hazard import fit_hazard, pool_observations, predict_stop_rank, PoolObs
from agent.study.labelling import (DEFAULT_BAND_MULT, SEGMENT_GRAIN, STATUS_LABELLED,
                                   label_segment)
from derive_facts import (build_menus, closest_approach, facts_to_validator_dict,
                          first_cross)

TZ = "America/New_York"
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_DIR = os.path.join(_REPO, ".agents", "label-corpus")
MANUAL_THESIS_DIR = os.path.join(_REPO, "manual-l1-thesis")

#: The Executor's arm instant -- the boundary production's own L1 decision is taken at.
L1_BOUNDARY_HHMM = "09:20"
#: How far forward the lookahead block looks. An argument, not a rule: "session close"
#: has three defensible readings in this repo (13:00 study RTH, 16:00 NY close, 16:59
#: bench session end) and the report prints the one it used.
DEFAULT_EVAL_END_HHMM = "16:00"
#: The selectors this module can run standalone. `nearest` is production's own rule
#: (`_dol_menu` returns nearest-first, so it is row 0); `b8g` is the cycle-4 hazard
#: argmax, study-only and never implemented in production.
SELECTORS = ("nearest", "b8g")
SUPPORTED_TICKERS = ("MNQ",)
RESOLUTIONS = ("1s", "1m")
B8G = "B8g"

VERDICT_HIT = "hit"
VERDICT_OVERSHOT = "overshot"
VERDICT_UNDERSHOT = "undershot"
VERDICT_MISS = "miss"
VERDICTS = (VERDICT_HIT, VERDICT_OVERSHOT, VERDICT_UNDERSHOT, VERDICT_MISS)

_MES_MSG = ("MES DOL menus are out of scope (offline-target-selection.md sec 1.3): "
            "`build_menus` is MNQ-only because the swept/depleted view it consumes is "
            "derived from MNQ-rendered facts. A half-wired MES menu would be silently "
            "wrong, so this raises instead of producing one.")


# --------------------------------------------------------------------------- #
# instants and bars
# --------------------------------------------------------------------------- #

def instant_ts(date, clock: str) -> pd.Timestamp:
    """`date` + an ET wall time (`HH:MM` or `HH:MM:SS`). Never a machine clock."""
    d = datetime.date.fromisoformat(date) if isinstance(date, str) else date
    return pd.Timestamp(f"{d} {clock}", tz=TZ)


class OfflineFacts(StudyFacts):
    """`StudyFacts` at a chosen resolution.

    1s is the default here and 1m is not: the study fixed on 1m because its range started
    2026-05-01, where the 1s lookback was empty. The 1s parquets begin 2026-05-01, so any
    August instant has its full 17-day lookback, and a second-resolution entry time
    deserves second-resolution bars. Where both have history the level sets agree
    (`test_facts_source.test_1m_and_1s_agree_where_both_have_history`).
    """

    def __init__(self, source: str = "1s", main_dir=None, tickers=("MNQ", "MES")) -> None:
        if source not in RESOLUTIONS:
            raise ValueError(f"source must be one of {RESOLUTIONS}, got {source!r}")
        self.source = source
        super().__init__(main_dir=main_dir, tickers=tickers)

    def source_path(self, ticker: str) -> str:
        return os.path.join(self.main_dir, f"{ticker}_{self.source}.parquet")

    def bars(self, ticker: str) -> pd.DataFrame:
        """The normalised frame at this source's resolution (o/h/l/c, tz-aware ET)."""
        return self._norm[ticker]


# --------------------------------------------------------------------------- #
# tracks
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Track:
    key: str
    label: str
    boundary: pd.Timestamp
    now_ts: object
    now_price: object
    avg_range_1h: object
    rows: tuple = ()
    pick: object = None
    pick_rank: object = None
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "boundary": str(self.boundary),
                "now_ts": None if self.now_ts is None else str(self.now_ts),
                "now_price": self.now_price, "avg_range_1h": self.avg_range_1h,
                "rows": [dict(r) for r in self.rows],
                "pick": None if self.pick is None else dict(self.pick),
                "pick_rank": self.pick_rank, "note": self.note,
                "extra": dict(self.extra)}


def menu_at(facts, boundary: pd.Timestamp, direction: str, ticker: str = "MNQ",
            *, extra_pools=None) -> dict:
    """`build_menus` at `boundary`, unchanged, plus the anchor it was built on.

    The bundle is sliced strictly BEFORE `boundary`, so `now_price` is the last close
    before the requested time -- at 09:30:00 that is the last pre-open print. Correct and
    lookahead-free, and reported so a reader can see what the menu was anchored on.

    `extra_pools` (plan 40) is handed to `build_menus` verbatim -- build it with
    `htf_extra_pools` for the unnested weekly/monthly extremes measurement.
    """
    if ticker not in SUPPORTED_TICKERS:
        raise NotImplementedError(_MES_MSG)
    bundle = facts.bundle_at(boundary)
    menus = build_menus(bundle, facts_to_validator_dict(bundle), extra_pools=extra_pools)
    return {"bundle": bundle, "boundary": boundary, "now_ts": bundle.now,
            "now_price": bundle.now_price,
            "avg_range_1h": (bundle.avg_range_1h or {}).get(ticker),
            "rows": tuple(menus["dol"].get(direction) or ())}


def htf_extra_pools(frame_1m, date: str, boundary: pd.Timestamp,
                    ticker: str = "MNQ") -> list:
    """Plan 40's T2 rows at `boundary`, from the SAME pure functions the Executor uses:
    the completed list and the running-period seed as of `date`'s session open (from
    `frame_1m`, the per-contract 1m parquet), pruned and combined with the bars in
    [open, boundary). Reads nothing at or after `boundary`."""
    from agent.facts.htf_extremes import (compute_unnested_extremes, menu_rows, pools_at,
                                          running_period_seed, session_as_of)
    as_of = session_as_of(date)
    ctx = {"as_of": as_of,
           "extremes": compute_unnested_extremes(frame_1m, as_of, ticker=ticker),
           "seed": running_period_seed(frame_1m, as_of, ticker=ticker)}
    return menu_rows(pools_at(ctx, frame_1m, boundary))


def _nearest_first_pick(rows):
    """Production's own offer: `D1`, the nearest eligible draw."""
    return (rows[0], 0) if rows else (None, None)


def pool_observations_from_menu(rows, ticker: str = "MNQ") -> "list[PoolObs]":
    """`PoolObs` records built from the production menu's own rows.

    `stop=False` on every record: at prediction time there is no label. `stop` is a
    training field and nothing in the prediction path may read it -- which is asserted
    behaviourally rather than trusted.
    """
    d = [r.get("dist_ratio") for r in rows]
    if not d or any(x is None for x in d):
        return []
    out = []
    for j, r in enumerate(rows):
        try:
            cls = classify(r["level"])[0]
        except KeyError:
            cls = r["tier"]          # `projection` -- synthetic, not a named family
        out.append(PoolObs(
            seg=("offline", 0), ticker=ticker, rank=j, dist=d[j],
            gap_ahead=(d[j + 1] - d[j]) if j + 1 < len(d) else None,
            gap_behind=(d[j] - d[j - 1]) if j > 0 else d[j],
            tier=r["tier"], cls=cls, stop=False,
            censored=(j + 1 >= len(d)), alt_gap_ahead=None))
    return out


def load_corpus(corpus_dir: str = CORPUS_DIR) -> tuple:
    """`(labels, candidates)` -- primary segments only, exactly as the Stage B report reads
    them. Read-only; the committed corpus is never written by this harness."""
    def rd(name):
        path = os.path.join(corpus_dir, name)
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if r.get("role") == "primary"]
    return rd("labels.jsonl"), rd("candidates.jsonl")


def fit_b8g(labels, cands, ticker: str = "MNQ", exclude_date=None,
            family: str = B8G) -> dict:
    """Fit the hazard table once, holding out nothing EXCEPT the date under test.

    The date under test is a fresh observation rather than part of the corpus, so there is
    no leave-one-out to do -- but if it IS in the corpus, leaving it in would leak the
    answer into the predictor on the one date being looked at, which is the exact failure
    cycle 4 spent a phase correcting. So it is dropped, and the report says so.
    """
    keep_labels = [r for r in labels if r.get("date") != exclude_date]
    keep_cands = [r for r in cands if r.get("date") != exclude_date]
    excluded = len(keep_labels) != len(labels)
    obs = [o for o in pool_observations(keep_labels, keep_cands) if o.ticker == ticker]
    hazard, pooled = fit_hazard(obs, family)
    sessions = sorted({o.seg for o in obs})
    note = (f"{exclude_date} is in the label corpus and was EXCLUDED from the B8g fit"
            if excluded else
            f"{exclude_date} is not in the label corpus; nothing excluded"
            if exclude_date else "no date excluded")
    return {"family": family, "ticker": ticker, "hazard": hazard, "pooled": pooled,
            "n_obs": len(obs), "n_sessions": len(sessions), "sessions": sessions,
            "exclude_date": exclude_date, "excluded": excluded, "note": note}


def b8g_pick(rows, fit: dict):
    """(row, rank) the composed hazard's argmax names, over the production menu's rows."""
    obs = pool_observations_from_menu(rows, ticker=fit.get("ticker", "MNQ"))
    if not obs:
        return None, None
    rank = predict_stop_rank(obs, fit["hazard"], fit["pooled"], fit["family"])
    return (rows[rank], rank) if 0 <= rank < len(rows) else (None, None)


def select_target(facts, date: str, clock: str, direction: str, ticker: str = "MNQ",
                  *, selector: str = "nearest", labels=None, cands=None,
                  corpus_dir: str = CORPUS_DIR, b8g_fit: "dict | None" = None,
                  extra_pools=None) -> dict:
    """The target `selector` names at `clock` -- the module's standalone decision path.

    This is the whole target-selection module, runnable offline on any date without the
    evaluation harness around it. **It reads no bar at or after `clock`**, which is not
    merely intended but asserted: `test_select_target_is_identical_with_the_future_deleted`
    reruns it against a bar frame truncated at the instant and requires an identical result.

    The selection is menu-only by construction -- the pick is always one of
    `build_menus`'s own rows -- and the instant is an argument, so the caller decides how
    late the decision is taken. The latest honest instant is the entry fill.
    """
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector {selector!r}; expected one of {SELECTORS}")
    if ticker not in SUPPORTED_TICKERS:
        raise NotImplementedError(_MES_MSG)
    boundary = instant_ts(date, clock)
    m = menu_at(facts, boundary, direction, ticker, extra_pools=extra_pools)
    out = {"selector": selector, "date": date, "time": clock, "direction": direction,
           "ticker": ticker, "source": facts.source, "boundary": boundary,
           "now_ts": m["now_ts"], "now_price": m["now_price"],
           "avg_range_1h": m["avg_range_1h"], "menu": [dict(r) for r in m["rows"]]}
    if selector == "nearest":
        pick, rank = _nearest_first_pick(m["rows"])
        out["note"] = "production's own rule: the nearest eligible draw (D1)"
    else:
        if b8g_fit is None:
            if labels is None or cands is None:
                labels, cands = load_corpus(corpus_dir)
            b8g_fit = fit_b8g(labels, cands, ticker=ticker, exclude_date=date)
        pick, rank = b8g_pick(m["rows"], b8g_fit)
        out["fit"] = {k: v for k, v in b8g_fit.items()
                      if k in ("family", "n_obs", "n_sessions", "excluded",
                               "exclude_date", "note", "pooled")}
        out["note"] = "cycle-4 B8g hazard argmax over the same rows; study-only"
    out["pick"] = None if pick is None else dict(pick)
    out["pick_rank"] = rank
    return out


def build_tracks(facts, date: str, clock: str, direction: str, ticker: str = "MNQ",
                 *, labels=None, cands=None, corpus_dir: str = CORPUS_DIR,
                 b8g_fit: "dict | None" = None) -> "list[Track]":
    """The three tracks, side by side, from one code path. Reads nothing after `clock`."""
    if ticker not in SUPPORTED_TICKERS:
        raise NotImplementedError(_MES_MSG)
    entry_ts = instant_ts(date, clock)
    l1_ts = instant_ts(date, L1_BOUNDARY_HHMM)

    m1 = menu_at(facts, l1_ts, direction, ticker)
    p1, r1 = _nearest_first_pick(m1["rows"])
    t1 = Track("T1", "09:20 prod (the menu L1 chose from)", l1_ts, m1["now_ts"],
               m1["now_price"], m1["avg_range_1h"], m1["rows"], p1, r1,
               note="nearest-first D1; this is what production OFFERS, not what it picked")

    m2 = menu_at(facts, entry_ts, direction, ticker)
    p2, r2 = _nearest_first_pick(m2["rows"])
    t2 = Track("T2", "entry prod (build_menus re-anchored at the instant)", entry_ts,
               m2["now_ts"], m2["now_price"], m2["avg_range_1h"], m2["rows"], p2, r2,
               note="production's own rule asked later; production never does this")

    if b8g_fit is None:
        if labels is None or cands is None:
            labels, cands = load_corpus(corpus_dir)
        b8g_fit = fit_b8g(labels, cands, ticker=ticker, exclude_date=date)
    p3, r3 = b8g_pick(m2["rows"], b8g_fit)
    t3 = Track("T3", "entry B8g (CLOSED) -- hazard argmax over T2's rows", entry_ts,
               m2["now_ts"], m2["now_price"], m2["avg_range_1h"], m2["rows"], p3, r3,
               note="study output; CLOSED -- ranks better, captures less (l2-target-selection.md sec 4)",
               extra={"fit": {k: v for k, v in b8g_fit.items()
                              if k in ("family", "n_obs", "n_sessions", "excluded",
                                       "exclude_date", "note", "pooled")}})
    return [t1, t2, t3]


# --------------------------------------------------------------------------- #
# lookahead -- future data. No track function may receive any of this.
# --------------------------------------------------------------------------- #

LOOKAHEAD_WARNING = ("LOOKAHEAD -- everything below reads bars AFTER the entry instant. "
                     "It exists to score the tracks and is read by none of them.")


@dataclass(frozen=True)
class _EvalSegment:
    """The shape `labelling.label_segment` consumes: a move from the entry instant to the
    5m bar its extreme printed in. Constructed so the DRAW rule stays the corpus's own."""
    direction: str
    start_ts: pd.Timestamp
    extreme_ts: pd.Timestamp


def _candidates_from_rows(rows, ticker: str = "MNQ") -> "list[Candidate]":
    out = []
    for r in rows:
        try:
            cls, _tier = classify(r["level"])
        except KeyError:
            cls = r["tier"]
        # Unswept by construction: `_dol_menu` excludes swept and depleted pools.
        out.append(Candidate(ticker=ticker, names=(r["level"],), price=r["price"],
                             cls=cls, tier=r["tier"], swept_before=False))
    return out


def evaluate(tracks, facts, date: str, clock: str, direction: str, ticker: str = "MNQ",
             *, eval_end: str = DEFAULT_EVAL_END_HHMM,
             band_mult: float = DEFAULT_BAND_MULT) -> dict:
    """What actually happened, per menu entry and per track."""
    entry_ts = instant_ts(date, clock)
    end_ts = instant_ts(date, eval_end)
    bars = facts.bars(ticker)
    window = bars.loc[entry_ts:end_ts]
    up = direction == "UP"
    side = "above" if up else "below"
    sign = 1 if up else -1

    t2 = next(t for t in tracks if t.key == "T2")
    rows = list(t2.rows)
    avg = t2.avg_range_1h

    entries = []
    for r in rows:
        ts = first_cross(window, r["price"], side) if len(window) else None
        gap, gap_ts = ((None, None) if ts is not None or not len(window)
                       else closest_approach(window, r["price"], side))
        entries.append({
            "id": r["id"], "level": r["level"], "price": r["price"], "band": r["band"],
            "dist_ratio": r["dist_ratio"], "tier": r["tier"],
            "reached": ts is not None,
            "reached_ts": None if ts is None else str(ts),
            "closest_approach": None if gap is None else round(float(gap), 4),
            "closest_approach_ts": None if gap_ts is None else str(gap_ts)})

    extreme_price = extreme_ts = None
    if len(window):
        col = "high" if up else "low"
        extreme_price = float(window[col].max() if up else window[col].min())
        extreme_ts = window[col].idxmax() if up else window[col].idxmin()

    draw = {"status": "unexplained", "reason": "no bars in the evaluation window"}
    if extreme_ts is not None and rows:
        seg = _EvalSegment(direction="up" if up else "down", start_ts=entry_ts,
                           extreme_ts=extreme_ts.floor(SEGMENT_GRAIN))
        labels, _rows = label_segment(seg, _Bundle(avg, ticker),
                                      {ticker: bars}, _candidates_from_rows(rows, ticker),
                                      band_mult=band_mult)
        lab = labels[ticker]
        if lab.status == STATUS_LABELLED:
            row = next((r for r in rows if abs(r["price"] - lab.price) < 1e-6), None)
            draw = {"status": lab.status, "via": lab.via,
                    "id": None if row is None else row["id"],
                    "level": list(lab.names)[0] if lab.names else None,
                    "price": lab.price, "reached_ts": None if lab.reached_ts is None
                    else str(lab.reached_ts), "overshoot": lab.overshoot,
                    "own_extreme": lab.own_extreme, "n_eligible": lab.n_eligible,
                    "n_reached": lab.n_reached}
        else:
            draw = {"status": lab.status, "own_extreme": lab.own_extreme,
                    "n_eligible": lab.n_eligible, "n_reached": lab.n_reached,
                    "reason": "no menu entry was reached, and none came inside the "
                              f"{band_mult}x avg_1h near-miss band"}

    verdicts = {}
    for t in tracks:
        v = {"pick": None if t.pick is None else t.pick["id"],
             "pick_level": None if t.pick is None else t.pick["level"],
             "pick_price": None if t.pick is None else t.pick["price"]}
        # Matched on PRICE, never on the menu id: T1's menu is built at a different
        # boundary, so its `D1` can name a different level than T2's `D1`.
        if t.pick is not None:
            hit = next((e for e in entries
                        if abs(e["price"] - t.pick["price"]) < 1e-6), None)
            v["pick_reached"] = None if hit is None else hit["reached"]
            v["pick_reached_ts"] = None if hit is None else hit["reached_ts"]
        if t.pick is None or draw.get("price") is None:
            v["verdict"] = VERDICT_MISS
            v["error_pts"] = v["error_ratio"] = None
            v["why"] = ("no pick" if t.pick is None
                        else "no draw: the move reached no eligible menu entry")
        else:
            err = round((t.pick["price"] - draw["price"]) * sign, 4)
            v["error_pts"] = err
            v["error_ratio"] = None if not avg else round(err / avg, 4)
            v["verdict"] = (VERDICT_HIT if abs(err) < 1e-6
                            else VERDICT_OVERSHOT if err > 0 else VERDICT_UNDERSHOT)
            v["why"] = ("named the draw" if v["verdict"] == VERDICT_HIT else
                        "aimed beyond where the move turned" if err > 0 else
                        "aimed short of where the move turned")
        # RECORDED SEPARATELY, and it is not the verdict: the draw is the last named pool
        # the move reached, while the extreme is where price actually turned. They differ
        # by the overshoot, and a menu with no pool near the turn scores a clean `hit` on
        # the draw while naming a target the move blew through. That gap is the finding.
        if t.pick is not None and extreme_price is not None:
            v["extreme_error_pts"] = round((t.pick["price"] - extreme_price) * sign, 4)
            v["extreme_error_ratio"] = (None if not avg else
                                        round(v["extreme_error_pts"] / avg, 4))
        else:
            v["extreme_error_pts"] = v["extreme_error_ratio"] = None
        verdicts[t.key] = v

    return {"warning": LOOKAHEAD_WARNING,
            "window": {"start": str(entry_ts), "end": str(end_ts),
                       "n_bars": int(len(window)), "resolution": facts.source},
            "entries": entries,
            "extreme": {"price": extreme_price,
                        "ts": None if extreme_ts is None else str(extreme_ts),
                        "excursion_pts": (None if extreme_price is None
                                          or t2.now_price is None else
                                          round((extreme_price - t2.now_price) * sign, 4))},
            "draw": draw, "verdicts": verdicts,
            "agreement": _agreement(tracks)}


@dataclass(frozen=True)
class _Bundle:
    """Only what `label_segment` reads: the tolerance band's `avg_range_1h`."""
    _avg: object
    _ticker: str

    @property
    def avg_range_1h(self) -> dict:
        return {self._ticker: self._avg}


def _agreement(tracks) -> dict:
    picks = {t.key: (None if t.pick is None else t.pick["price"]) for t in tracks}
    distinct = {p for p in picks.values() if p is not None}
    return {"picks": picks, "n_distinct": len(distinct),
            "all_agree": len(distinct) == 1 and all(p is not None for p in picks.values()),
            "note": ("three selectors agreeing on one date is much weaker evidence than "
                     "three disagreeing")}


# --------------------------------------------------------------------------- #
# recorded theses -- optional, and never presented as "what production did"
# --------------------------------------------------------------------------- #

def recorded_theses(date: str, repo_root: str = _REPO) -> "list[dict]":
    """Any recorded L1 thesis for `date`. Empty for most dates, which is normal.

    Two sources, labelled differently and never merged: `manual-l1-thesis/` is committed
    WORKING STATE from hand-run boundaries, and `<global>/thesis_cache/` is the production
    recording. Read-only on both.
    """
    out = []
    stamp = date.replace("-", "")
    manual_dir = os.path.join(repo_root, os.path.basename(MANUAL_THESIS_DIR))
    for path in sorted(glob.glob(os.path.join(manual_dir, f"{stamp}_*.json"))):
        rec = _read_thesis(path, "manual")
        if rec:
            out.append(rec)
    for path in sorted(glob.glob(os.path.join(str(_cache_root() or ""), "*.json"))):
        rec = _read_thesis(path, "production")
        if rec and str(rec.get("boundary", "")).startswith(date):
            out.append(rec)
    return out


def _mark_direction(records, direction: str) -> "list[dict]":
    """Flag whether a recording was taken under the direction being analysed.

    A DOL chosen under the OPPOSITE bias is not a comparable pick -- it came off the other
    side of the menu. 2026-08-12 is the live case: the production thesis is bias UP on a
    session whose 09:30 move was DOWN, and quoting its DOL beside a DOWN track without
    saying so would invite exactly the wrong comparison. `None` when the recording
    declared no bias (a NEUTRAL thesis names no draw).
    """
    out = []
    for r in records:
        bias = r.get("bias")
        out.append({**r, "matches_request":
                    None if bias not in ("UP", "DOWN") else bias == direction})
    return out


def _cache_root():
    try:
        from agent.trader.thesis_cache import cache_root
        root = cache_root()
        return root if os.path.isdir(root) else None
    except Exception:
        return None


def _read_thesis(path: str, kind: str):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    thesis = doc.get("thesis") or {}
    if not isinstance(thesis, dict):
        return None
    return {"kind": kind, "path": os.path.relpath(path, _REPO) if kind == "manual"
            else os.path.basename(path),
            "boundary": str(doc.get("boundary") or ""),
            "bias": thesis.get("bias"), "regime": thesis.get("regime"),
            "confidence": thesis.get("confidence"), "dol": thesis.get("dol"),
            "dol_rationale": thesis.get("dol_rationale"),
            "caveat": ("a manual-l1-thesis/ working-state run at its OWN boundary -- not a "
                       "production recording" if kind == "manual"
                       else "production thesis_cache recording")}


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #

def build_report(facts, date: str, clock: str, direction: str, ticker: str = "MNQ",
                 *, labels=None, cands=None, corpus_dir: str = CORPUS_DIR,
                 eval_end: str = DEFAULT_EVAL_END_HHMM) -> dict:
    """Tracks first, lookahead second. The tracks are built before any future bar is read."""
    if ticker not in SUPPORTED_TICKERS:
        raise NotImplementedError(_MES_MSG)
    if labels is None or cands is None:
        labels, cands = load_corpus(corpus_dir)
    fit = fit_b8g(labels, cands, ticker=ticker, exclude_date=date)
    tracks = build_tracks(facts, date, clock, direction, ticker, b8g_fit=fit)
    look = evaluate(tracks, facts, date, clock, direction, ticker, eval_end=eval_end)
    return {
        "request": {"date": date, "time": clock, "direction": direction,
                    "ticker": ticker, "source": facts.source, "eval_end": eval_end,
                    "main_dir": facts.main_dir},
        "production_context": (
            "SUPERSEDED (plan 16): T2 IS production now — `agent/trader/target.py` picks D1 at the entry fill and it is the take-profit. This report stays a MEASUREMENT harness "
            "(it reads parquets; production builds from in-memory bars), and T1/T3 remain study-only. Historical note: L1 used to pick one menu entry "
            "at the 09:20 boundary, planner.derive_plan copies it verbatim, and nothing "
            "between 09:20 and the fill reconsiders it. T2 and T3 are measurements, not a "
            "production path."),
        "b8g_fit": {k: v for k, v in fit.items() if k != "hazard" and k != "sessions"},
        "tracks": [t.to_dict() for t in tracks],
        "recorded_theses": _mark_direction(recorded_theses(date), direction),
        "lookahead": look,
    }
