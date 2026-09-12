"""Offline facts builder for the bench (plan 10 Phase 2).

Builds the deterministic S0-S7 fact sheet as of an arbitrary intra-day timestamp from
the main 1s parquets, EXACTLY mirroring calibration/prepare_cuts.py so a burned cut's
facts.txt reproduces byte-for-byte (the facts-parity test). The 17-day primary slice +
the full-history hist frames feed derive_facts.compute_facts (reused as-is); the primary
is truncated strictly before the boundary timestamp — no lookahead.

It also yields the per-date 1m session bars the engine walks, and the engine-facing level
table (price/side/sweep-state/depletion-threshold per level) plus the day mid / session
extremes the safety nets read — all derived from the SAME facts bundle so the bench and
the executor cannot diverge on the level universe.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_AGENT, os.path.join(_AGENT, "contracts"), os.path.join(_REPO, "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402
from derive_facts import (  # noqa: E402
    DEPLETE, TZ, build_evidence_magnitude, build_menus, compute_facts,
    facts_to_validator_dict, load, render_facts_text, render_menus_text,
    render_evidence_text, session_frame, trade_date,
)

DEFAULT_MAIN = os.path.expanduser(
    "~/projects/auto-co-trader/global/general/main/2026-09")
LOOKBACK = pd.Timedelta(days=17)      # prepare_cuts.LOOKBACK_DAYS
_MAINT_LO = datetime.time(16, 55)
_MAINT_HI = datetime.time(18, 0)


@dataclass
class FactsResult:
    boundary: pd.Timestamp
    text: str = ""
    menu_text: str = ""              # S8 menu block (appended to the model prompt; NOT hashed)
    evidence_text: str = ""          # S9 evidence block (weekly_mid, HTF close-status, SMT
                                      # candidates; appended to the model prompt; NOT hashed)
    evidence_magnitude: dict = field(default_factory=dict)  # {(asset, level, tf): ratio} (plan 14)
    validator_dict: dict = field(default_factory=dict)
    content_hash: str = ""
    max_ts: Optional[pd.Timestamp] = None
    now: Optional[pd.Timestamp] = None
    levels: dict = field(default_factory=dict)     # engine level table (see _engine_levels)
    daily_mid: Optional[float] = None
    sess_hi: Optional[float] = None
    sess_lo: Optional[float] = None
    now_price: Optional[float] = None
    degraded: bool = False
    error: Optional[str] = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class BoundarySliceError(Exception):
    """Raised by `slice_for_boundary` when the frames cannot support a bundle.

    Carries the SAME `reason` strings `build_facts` has always reported, so the
    degraded-result contract is unchanged.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def slice_for_boundary(raw_by_ticker: dict, norm_by_ticker: dict,
                       boundary: pd.Timestamp, tickers=("MNQ", "MES"),
                       lookback: pd.Timedelta = LOOKBACK) -> tuple:
    """The ONE boundary -> (primary slice, ATH) rule. -> (prim, ath).

    Extracted (cycle-1 addendum change E) so the OFFLINE parquet source
    (`ParquetFactsSource.build_facts`) and the ONLINE in-memory assembler
    (`agent.facts.assemble.build_bundle`) cannot drift. They differ only in where the
    two frames come from; every rule below is shared:

      * `prim` = the `lookback` window ending strictly BEFORE `boundary`, taken from the
        MAINTENANCE-DROPPED frame — this is what `compute_facts` derives levels from.
      * `ath` = the max high strictly before `boundary` from the RAW frame, maintenance
        bars INCLUDED. Dropping them first can move the maximum, so the two frames are
        not interchangeable here.

    Two copies of this is precisely the failure mode this project has already paid for
    twice (the 2026-07-08 online/offline facts divergence, and the near-miss that
    prompted this extraction).
    """
    prim, ath = {}, {}
    for tk in tickers:
        raw = raw_by_ticker.get(tk)
        if raw is None or len(raw) == 0:
            raise BoundarySliceError("no-data-before-boundary")
        before = raw[raw.index < boundary]
        if len(before) == 0:
            raise BoundarySliceError("no-data-before-boundary")
        ath[tk] = float(before["High"].max()) if "High" in before.columns             else float(before["high"].max())
        norm = norm_by_ticker.get(tk)
        if norm is None or len(norm) == 0:
            raise BoundarySliceError("empty-primary-slice")
        sl = norm[(norm.index >= boundary - lookback) & (norm.index < boundary)]
        if len(sl) == 0:
            raise BoundarySliceError("empty-primary-slice")
        prim[tk] = sl
    return prim, ath


def bundle_for_boundary(raw_by_ticker: dict, norm_by_ticker: dict,
                        boundary: pd.Timestamp, tickers=("MNQ", "MES"),
                        lookback: pd.Timedelta = LOOKBACK):
    """`slice_for_boundary` + `compute_facts`. -> (bundle, prim). Raises
    `BoundarySliceError` on a degraded input; `compute_facts` errors propagate."""
    prim, ath = slice_for_boundary(raw_by_ticker, norm_by_ticker, boundary,
                                   tickers=tickers, lookback=lookback)
    bundle = compute_facts(
        prim["MNQ"], prim["MES"], ath_mnq=ath.get("MNQ"), ath_mes=ath.get("MES"),
        hist_mnq=norm_by_ticker.get("MNQ"), hist_mes=norm_by_ticker.get("MES"),
        now=None)
    return bundle, prim


def bundle_to_l1_view(bundle) -> tuple:
    """The L1 view of a computed FactsBundle: (validator_dict, menu_text,
    evidence_text, evidence_magnitude).

    Extracted verbatim out of `ParquetFactsSource.build_facts` (cycle-1 Task 10) so the
    ONLINE assembler (`agent.facts.assemble.assemble_facts`) and the OFFLINE bench build
    the additive S8/S9 overlays from one piece of code. Two copies of this would make
    the facts-parity gate compare a fork against itself.

    Every key here is an ADDITIVE overlay on the bench's own dict copy — the shared
    `facts_to_validator_dict` result stays byte-stable for the shadow engine's hash.
    """
    vd = facts_to_validator_dict(bundle)   # core view (shadow-parity)
    # S8 menus: an additive L1 overlay on the bench's OWN validator_dict copy (the
    # shared facts_to_validator_dict stays byte-stable for the shadow engine's hash).
    bundle.menus = build_menus(bundle, vd)
    vd["menus"] = bundle.menus
    # plan 15 Task 4: FVG-zone ids allowed as P5 evidence `level` values (additive overlay
    # on the bench's own validator_dict copy, like menus — never in the shadow-hash dict).
    vd["fvg_zones"] = [z["id"] for z in bundle.fvg_zones]
    # 2026-08-02: structured FVG-zone metadata (keyed by id) for score_thesis_evidence's
    # same-move dedup -- the plain id list above is schema-facing only (level-name
    # validation), this carries what the dedup pre-pass needs to detect "adjacent bars,
    # one continuous move" (asset/tf/kind/ts) without re-parsing the id string. `ts` is
    # stringified (JSON/artifact-safe, like fvg_zones above) and re-parsed with
    # pd.Timestamp on the scoring side.
    vd["fvg_zone_meta"] = {
        z["id"]: {"asset": z["asset"], "tf": z["tf"], "kind": z["kind"], "ts": str(z["ts"])}
        for z in (bundle.fvg_zones or [])
    }
    # plan 15 Task 7: `now` for the audit-only pending_resolution.resolves_at > now check.
    vd["now"] = str(bundle.now) if bundle.now is not None else None
    # thesis.md §2.1b/§2.1d: nested/duplicate-sweep levels excluded from fresh P1
    # evidence (lists, not sets -- JSON/artifact-safe, like fvg_zones above).
    vd["suppressed_p1_levels"] = {
        tkr: sorted(names) for tkr, names in (bundle.suppressed_p1_levels or {}).items()
    }
    # thesis.md §2.1b: P2/SMT nesting suppression (additive overlay, like
    # suppressed_p1_levels above — never in the shadow-hash dict).
    vd["suppressed_p2_sites"] = {
        tkr: sorted(names) for tkr, names in (bundle.suppressed_p2_sites or {}).items()
    }
    # thesis.md §3a: near-maturity pre-confirmation candidates (additive overlay, like
    # suppressed_p1_levels above — never in the shadow-hash dict).
    vd["near_maturity_candidates"] = list(bundle.near_maturity_candidates or [])
    # 2026-08-02 evidence-direction ground truth: the SAME per-level HTF-close verdict
    # already computed for every named level (bundle.htf_close_status, incl. the
    # daily_mid/weekly_mid synthetic "levels" plan 17 Fix 3 added) exposed as a flat
    # None|bool per (asset, level, tf) -- None = never swept / no qualifying close yet
    # (immature), True = closed BEYOND (accept), False = closed back before (reject).
    # Additive overlay, like suppressed_p1_levels above — never in the shadow-hash
    # dict. Two consumers: (1) SEM_EVIDENCE_DIRECTION_MISMATCH cross-checks every
    # declared P1/P2 item's mature/direction claim against this instead of trusting it
    # (closes the 2026-07-15 prev1_week_high fabrication class — a level that was NEVER
    # swept has every tf entry None, directly contradicting a declared mature=True); (2)
    # score_thesis_evidence's P3 auto-derivation reads the two mid names straight out of
    # this same dict rather than needing its own separate overlay.
    vd["level_htf_close_status"] = {
        tkr: {name: {tf: (None if (status or {}).get(tf) is None
                          else bool(status[tf]["beyond"]))
                    for tf in ("1h", "4h")}
              for name, status in (bundle.htf_close_status.get(tkr) or {}).items()}
        for tkr in ("MNQ", "MES")
    }
    # 2026-08-02 P1/P2 auto-derivation: level_htf_close_status has no tier or price
    # (facts_to_validator_dict's own "levels" view deliberately omits tier -- see its
    # docstring -- and score_thesis_evidence only receives narrow sub-dicts, not the
    # full facts blob), so this carries both straight from bundle.levels for the day-
    # tier-confluent-with-week-tier tier bump (thesis.md §2.1e promotion) as well as
    # auto-injecting P1 items. Additive overlay, like suppressed_p1_levels above.
    vd["level_tiers"] = {
        tkr: {name: {"tier": tier, "price": price}
              for name, (price, _body, side, tier, _active) in
              (bundle.levels.get(tkr) or {}).items() if side is not None}
        for tkr in ("MNQ", "MES")
    }
    # thesis.md §2.1 (2026-08-15): running-extreme tier promotion — a session-tier
    # level that IS the running day/week extreme scores at that tier (P1 weight; the
    # matching P2 promotion lives on bundle.smt_candidates itself). Overrides the
    # tier straight in level_tiers so the P1 auto-injection and §2.1e machinery see
    # the promoted tier with no scorer change.
    for _tkr, _promos in (bundle.promoted_session_levels or {}).items():
        for _name, _ptier in (_promos or {}).items():
            _entry = (vd["level_tiers"].get(_tkr) or {}).get(_name)
            if _entry is not None:
                _entry["tier"] = _ptier
    # 2026-08-02 P2 auto-derivation: JSON-safe view of bundle.smt_candidates (drops
    # swept_at/type -- not needed for scoring) so score_thesis_evidence can auto-inject
    # a P2 item for every meaningful, unsuppressed divergence directly, the same way
    # P3 is auto-derived from level_htf_close_status.
    vd["smt_candidates"] = [
        {"level": c.get("level"), "tier": c.get("tier"),
         "swept_ticker": c.get("swept_ticker"), "unswept_ticker": c.get("unswept_ticker"),
         "meaningful": bool(c.get("meaningful"))}
        for c in (bundle.smt_candidates or [])
    ]
    # 2026-08-02 thesis.md §2.1e promotion: the CURRENT week's own high/low, for the
    # narrow day-tier-confluent-with-week-tier tier bump in score_thesis_evidence
    # (2026-07-15 root-cause: prev2_day_high sat within a tight cluster of the week's
    # own high on both assets -- a day-tier SMT that is ALSO the week's extreme deserves
    # week-tier weight, not day-tier). Deliberately NOT the same mechanism as the
    # existing _confluence_notes (audit-only, OLD untracked extremes only) -- this
    # compares against the CURRENT, actively-tracked week extreme.
    vd["week_extremes"] = {
        tkr: {"hi": bundle.week_hi.get(tkr), "lo": bundle.week_lo.get(tkr)}
        for tkr in ("MNQ", "MES")
    }
    # thesis.md §10 (2026-08-05): P3-vs-P4 mid promotion, PER TF — whether each mid's
    # crossing on that tf is still the live, un-superseded story (derive_facts.
    # _mid_tf_state). JSON-safe as-is ({tf: {"fresh": bool, "cross_dir": str}}, no
    # timestamps).
    vd["mid_reclaim"] = {
        tkr: dict(bundle.mid_reclaim.get(tkr) or {}) for tkr in ("MNQ", "MES")
    }
    # thesis.md §10 (2026-08-05): partial-bar reversal — does the currently-forming
    # next-tf bar already undermine a level/mid's just-completed bar verdict
    # (derive_facts._htf_reversal_tier). JSON-safe as-is ({level: {tf: str}}).
    vd["htf_reversal"] = {
        tkr: dict(bundle.htf_reversal.get(tkr) or {}) for tkr in ("MNQ", "MES")
    }
    # thesis.md §2.1 P3 (2026-08-15, #3): unconditional per-asset position vs each
    # mid — feeds the position-only P3 injection for mids with no HTF crossing at all.
    # JSON-safe as-is (floats/strings only).
    vd["mid_position"] = {
        tkr: dict(bundle.mid_position.get(tkr) or {}) for tkr in ("MNQ", "MES")
    }
    # thesis.md §2.1 P2 Stage 2 input (2026-08-15): recross distance in avg-1h-range
    # units per level/mid with a reversal entry — read only by the EXPERIMENTAL
    # discount-fire A/B (production leaves the knob off). JSON-safe as-is.
    vd["recross_distance"] = {
        tkr: dict(bundle.recross_distance.get(tkr) or {}) for tkr in ("MNQ", "MES")
    }
    # thesis.md §2.1c (2026-08-15, #6): equilibrium-reversion staleness as a HARD P1
    # gate — the flagged names only (lists, JSON-safe, like suppressed_p1_levels).
    vd["p1_stale_levels"] = {
        tkr: sorted(name for name, flag in
                    (bundle.p1_equilibrium_stale.get(tkr) or {}).items() if flag)
        for tkr in ("MNQ", "MES")
    }
    menu_text = render_menus_text(bundle)              # reuses cached bundle.menus
    magnitude = build_evidence_magnitude(bundle)  # plan 14 Task 5: code-derived
    # magnitude threaded into the render so the model can SEE the WEAK/NORMAL/STRONG
    # clearance label before declaring bias (gap fix: same ratio, no new computation).
    evidence_text = render_evidence_text(bundle, magnitude=magnitude)
    # 2026-08-05: same ratios, JSON-safe nested-dict shape ({asset: {level: {tf:
    # ratio}}}, tuple keys -> nested dicts) so validate_thesis's own ARI_THESIS_BIAS
    # re-check can apply the SAME clearance-magnitude weighting the real scoring path
    # (_derive_thesis_arithmetic) already does -- previously validate_thesis re-scored
    # with an unweighted x1.0 on every item, which could disagree with the REAL,
    # magnitude-weighted net score closely enough to flip which side of a tie the
    # declared bias fell on (2026-07-20 09:20 ET: true net score was an exact 0.0 tie
    # -- NEUTRAL -- but the unweighted re-check computed +0.5 UP and waved a declared
    # UP bias through clean).
    vd["evidence_magnitude"] = {}
    for (_asset, _level, _tf), _ratio in magnitude.items():
        vd["evidence_magnitude"].setdefault(_asset, {}).setdefault(
            _level, {})[_tf] = _ratio
    return vd, menu_text, evidence_text, magnitude


def main_dir_for_date(date_str: str) -> str:
    """Per-contract main folder for a TRADE DATE, per `<main>/rollover_ledger.json`.

    Same rule as `backtest_smt._main_dir_for_date` (and the three other copies in
    plot_session / regression.plot_regression / scripts.check_session_parquets): rows are
    newest-first, the first row whose `prep_date` is <= `date_str` wins, ISO dates compare
    correctly as strings, and anything missing falls back to the flat main dir.

    Duplicated rather than imported because `backtest_smt` is the legacy engine and pulling
    it in would drag a module-load manifest read into every facts build.
    """
    main = paths.general_main_dir()
    ledger = main / "rollover_ledger.json"
    if not ledger.exists():
        return str(main)
    try:
        rows = json.loads(ledger.read_text(encoding="utf-8"))
    except Exception:
        return str(main)
    for row in rows:
        if str(date_str) >= str(row.get("prep_date", "")):
            sub = main / str(row.get("subfolder", ""))
            return str(sub if sub.exists() else main)
    return str(main)


class ParquetFactsSource:
    """Loads the 1s (facts) + 1m (walk) parquets and serves facts snapshots at arbitrary
    boundaries plus per-date session bars.

    **`main_dir` now defaults to PER-DATE CONTRACT ROUTING (2026-09-12), not one folder.**
    It used to default to the latest contract for every boundary while `run_replay` routed
    per date through the rollover ledger, so on any pre-roll date the two described
    different instruments — measured at exactly 293.25 pts across the June->Sept roll,
    matching the gap the ledger itself records. Nothing errored: facts simply came back on
    the wrong price scale, and a DOL built from them landed on the wrong side of price,
    killing plans `dol_reached` at the arm. That trap cost three separate analyses in one
    session, and it re-arms at every roll.

    Passing `main_dir=` explicitly PINS that folder for every date, which is what the tests
    and `manual-l1-thesis` rely on; pinning still overrides the ledger completely.

    **Routing NEVER mutates this object.** `_raw_1s` / `_norm_1s` / `_norm_1m` stay bound to
    the default folder for the life of the source; `build_facts` and `session_bars` select
    their frames locally via `frames_for`. An earlier draft swapped the attributes in place
    and `test_facts.py`'s MODULE-SCOPED `source` fixture caught it immediately: one build on
    a pre-roll date left every later test in the module reading the other contract. That is
    the same silent cross-contamination this change exists to remove, so it must not be
    reintroduced as shared mutable state.

    Consequence worth knowing: code that reads the frame attributes DIRECTLY is unrouted and
    always sees the default contract (`manual-l1-thesis` checks `_raw_1s[tk].index[-1]`
    before its first build, and `test_facts._bundle_at` builds bundles straight off them).
    Callers that want a specific date's contract should ask `frames_for(date)`.
    """

    def __init__(self, main_dir: "str | None" = None, tickers=("MNQ", "MES")):
        self.pinned_main_dir = main_dir
        self.tickers = tuple(tickers)
        self._frames: dict = {}           # folder -> (raw_1s, norm_1s, norm_1m)
        self.main_dir = main_dir or DEFAULT_MAIN
        self._raw_1s, self._norm_1s, self._norm_1m = self._load_folder(self.main_dir)

    def main_dir_for(self, date_str: str) -> str:
        """The folder this source reads for `date_str` — the pin if one was given,
        otherwise the ledger's answer."""
        return self.pinned_main_dir or main_dir_for_date(str(date_str))

    def _load_folder(self, folder: str):
        """(raw_1s, norm_1s, norm_1m) for `folder`, loaded once and cached."""
        if folder not in self._frames:
            raw, n1s, n1m = {}, {}, {}
            for tk in self.tickers:
                raw[tk] = self._load_raw(os.path.join(folder, f"{tk}_1s.parquet"))
                n1s[tk] = load(os.path.join(folder, f"{tk}_1s.parquet"))
                n1m[tk] = load(os.path.join(folder, f"{tk}_1m.parquet"))
            self._frames[folder] = (raw, n1s, n1m)
        return self._frames[folder]

    def frames_for(self, date_str: str):
        """(raw_1s, norm_1s, norm_1m) for the contract that owns `date_str`."""
        return self._load_folder(self.main_dir_for(date_str))

    @staticmethod
    def _load_raw(path: str) -> pd.DataFrame:
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index)
        df.index = (df.index.tz_localize(TZ) if df.index.tz is None
                    else df.index.tz_convert(TZ))
        return df.sort_index()

    # ---- facts snapshot ---------------------------------------------------- #
    def build_facts(self, boundary: pd.Timestamp) -> FactsResult:
        """Facts as of strictly before `boundary` (mirrors prepare_cuts' `< cut` slice).
        A degraded result (no session / insufficient data) is returned marked, never
        raised — the caller records a failsafe and makes no API call (facts_adapter
        parity)."""
        res = FactsResult(boundary=boundary)
        try:
            raw_1s, norm_1s, _ = self.frames_for(
                pd.Timestamp(boundary).date().isoformat())
        except (OSError, ValueError) as exc:               # missing contract folder
            res.degraded = True
            res.error = f"contract_routing:{type(exc).__name__}"
            return res
        try:
            # The slicing rule lives in `bundle_for_boundary`, shared verbatim with the
            # online assembler (cycle-1 addendum change E).
            bundle, prim = bundle_for_boundary(raw_1s, norm_1s, boundary,
                                               tickers=tuple(self.tickers))
        except BoundarySliceError as exc:
            res.degraded = True
            res.error = exc.reason
            return res
        except (IndexError, KeyError, ValueError) as exc:
            res.degraded = True
            res.error = f"compute_facts:{type(exc).__name__}"
            return res

        res.text = render_facts_text(bundle)
        (res.validator_dict, res.menu_text, res.evidence_text,
         res.evidence_magnitude) = bundle_to_l1_view(bundle)
        res.content_hash = _sha(res.text)                      # core hash: S0–S7 only (parity)
        res.now = bundle.now
        res.max_ts = bundle.now
        res.now_price = bundle.now_price
        res.levels = _engine_levels(bundle, res.validator_dict)
        res.daily_mid, res.sess_hi, res.sess_lo = _day_extremes(bundle, prim["MNQ"])
        return res

    # ---- per-date walk bars ------------------------------------------------ #
    def session_window(self, date: str):
        """(session_open_ts, session_end_ts) for a trade date: prior-day 18:00 ET ->
        16:59 ET (design §Bench loop)."""
        d = datetime.date.fromisoformat(date)
        open_ts = pd.Timestamp(datetime.datetime.combine(
            d - datetime.timedelta(days=1), datetime.time(18, 0)), tz=TZ)
        end_ts = pd.Timestamp(datetime.datetime.combine(
            d, datetime.time(16, 59)), tz=TZ)
        return open_ts, end_ts

    def session_bars(self, date: str) -> pd.DataFrame:
        """The 1m MNQ session bars the engine walks (maintenance-dropped, session-scoped)."""
        d = datetime.date.fromisoformat(date)
        norm = self.frames_for(date)[2]["MNQ"]
        bars = norm[(norm.index + pd.Timedelta(hours=7)).date == d]
        return bars.sort_index()


def _engine_levels(bundle, vd: dict) -> dict:
    """Engine level table: name -> {price, side, swept0, depleted0, threshold}. Price/side/
    tier come from the MNQ level map (bundle.levels); swept/depleted birth states from the
    validator view (one source, two views); the depletion threshold from the exact
    per-ticker engine table (derive_facts.DEPLETE)."""
    out = {}
    mnq = (bundle.levels or {}).get("MNQ", {})
    vlv = vd.get("levels", {})
    for name, tup in mnq.items():
        price, _body, side, tier, _active = tup
        if side not in ("above", "below"):
            continue
        v = vlv.get(name, {})
        out[name] = {
            "price": price, "side": side,
            "swept0": bool(v.get("swept")), "depleted0": bool(v.get("depleted")),
            "threshold": DEPLETE["MNQ"].get(tier),
        }
    return out


def _day_extremes(bundle, prim_mnq: pd.DataFrame):
    """MNQ running day high/low/mid at `now` (the acceptance_flip / opposite_extreme
    references), from the primary session bars up to now."""
    now = bundle.now
    td = trade_date(now)
    sess = session_frame(prim_mnq, td)
    if len(sess) == 0:
        return None, None, None
    hi = float(sess["high"].max())
    lo = float(sess["low"].min())
    return round((hi + lo) / 2.0, 2), hi, lo
