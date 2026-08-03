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


class ParquetFactsSource:
    """Loads the full 1s (facts) + 1m (walk) parquets once, then serves facts snapshots
    at arbitrary boundaries and per-date session bars. `main_dir` defaults to the
    machine's back-adjusted 2026-09 main; tests point it at a tmp dir."""

    def __init__(self, main_dir: str = DEFAULT_MAIN, tickers=("MNQ", "MES")):
        self.main_dir = main_dir
        self.tickers = tuple(tickers)
        self._raw_1s = {}     # capital-col frames (for ATH parity with prepare_cuts)
        self._norm_1s = {}    # derive_facts.load-normalised (maintenance-dropped)
        self._norm_1m = {}
        for tk in self.tickers:
            self._raw_1s[tk] = self._load_raw(os.path.join(main_dir, f"{tk}_1s.parquet"))
            self._norm_1s[tk] = load(os.path.join(main_dir, f"{tk}_1s.parquet"))
            self._norm_1m[tk] = load(os.path.join(main_dir, f"{tk}_1m.parquet"))

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
            prim = {}
            ath = {}
            for tk in self.tickers:
                raw = self._raw_1s[tk]
                before = raw[raw.index < boundary]
                if len(before) == 0:
                    res.degraded = True
                    res.error = "no-data-before-boundary"
                    return res
                ath[tk] = float(before["High"].max())
                norm = self._norm_1s[tk]
                prim[tk] = norm[(norm.index >= boundary - LOOKBACK) & (norm.index < boundary)]
                if len(prim[tk]) == 0:
                    res.degraded = True
                    res.error = "empty-primary-slice"
                    return res

            bundle = compute_facts(
                prim["MNQ"], prim["MES"], ath_mnq=ath.get("MNQ"), ath_mes=ath.get("MES"),
                hist_mnq=self._norm_1s.get("MNQ"), hist_mes=self._norm_1s.get("MES"),
                now=None)
        except (IndexError, KeyError, ValueError) as exc:
            res.degraded = True
            res.error = f"compute_facts:{type(exc).__name__}"
            return res

        res.text = render_facts_text(bundle)
        res.validator_dict = facts_to_validator_dict(bundle)   # core view (shadow-parity)
        # S8 menus: an additive L1 overlay on the bench's OWN validator_dict copy (the
        # shared facts_to_validator_dict stays byte-stable for the shadow engine's hash).
        bundle.menus = build_menus(bundle, res.validator_dict)
        res.validator_dict["menus"] = bundle.menus
        # plan 15 Task 4: FVG-zone ids allowed as P5 evidence `level` values (additive overlay
        # on the bench's own validator_dict copy, like menus — never in the shadow-hash dict).
        res.validator_dict["fvg_zones"] = [z["id"] for z in bundle.fvg_zones]
        # 2026-08-02: structured FVG-zone metadata (keyed by id) for score_thesis_evidence's
        # same-move dedup -- the plain id list above is schema-facing only (level-name
        # validation), this carries what the dedup pre-pass needs to detect "adjacent bars,
        # one continuous move" (asset/tf/kind/ts) without re-parsing the id string. `ts` is
        # stringified (JSON/artifact-safe, like fvg_zones above) and re-parsed with
        # pd.Timestamp on the scoring side.
        res.validator_dict["fvg_zone_meta"] = {
            z["id"]: {"asset": z["asset"], "tf": z["tf"], "kind": z["kind"], "ts": str(z["ts"])}
            for z in (bundle.fvg_zones or [])
        }
        # plan 15 Task 7: `now` for the audit-only pending_resolution.resolves_at > now check.
        res.validator_dict["now"] = str(bundle.now) if bundle.now is not None else None
        # thesis.md §2.1b/§2.1d: nested/duplicate-sweep levels excluded from fresh P1
        # evidence (lists, not sets -- JSON/artifact-safe, like fvg_zones above).
        res.validator_dict["suppressed_p1_levels"] = {
            tkr: sorted(names) for tkr, names in (bundle.suppressed_p1_levels or {}).items()
        }
        # thesis.md §2.1b: P2/SMT nesting suppression (additive overlay, like
        # suppressed_p1_levels above — never in the shadow-hash dict).
        res.validator_dict["suppressed_p2_sites"] = {
            tkr: sorted(names) for tkr, names in (bundle.suppressed_p2_sites or {}).items()
        }
        # thesis.md §3a: near-maturity pre-confirmation candidates (additive overlay, like
        # suppressed_p1_levels above — never in the shadow-hash dict).
        res.validator_dict["near_maturity_candidates"] = list(bundle.near_maturity_candidates or [])
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
        res.validator_dict["level_htf_close_status"] = {
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
        res.validator_dict["level_tiers"] = {
            tkr: {name: {"tier": tier, "price": price}
                  for name, (price, _body, side, tier, _active) in
                  (bundle.levels.get(tkr) or {}).items() if side is not None}
            for tkr in ("MNQ", "MES")
        }
        # 2026-08-02 P2 auto-derivation: JSON-safe view of bundle.smt_candidates (drops
        # swept_at/type -- not needed for scoring) so score_thesis_evidence can auto-inject
        # a P2 item for every meaningful, unsuppressed divergence directly, the same way
        # P3 is auto-derived from level_htf_close_status.
        res.validator_dict["smt_candidates"] = [
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
        res.validator_dict["week_extremes"] = {
            tkr: {"hi": bundle.week_hi.get(tkr), "lo": bundle.week_lo.get(tkr)}
            for tkr in ("MNQ", "MES")
        }
        res.menu_text = render_menus_text(bundle)              # reuses cached bundle.menus
        res.evidence_magnitude = build_evidence_magnitude(bundle)  # plan 14 Task 5: code-derived
        # magnitude threaded into the render so the model can SEE the WEAK/NORMAL/STRONG
        # clearance label before declaring bias (gap fix: same ratio, no new computation).
        res.evidence_text = render_evidence_text(bundle, magnitude=res.evidence_magnitude)
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
        norm = self._norm_1m["MNQ"]
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
