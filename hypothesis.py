# hypothesis.py
# Every-5m hypothesis module for SMT v2 pipeline.
# Entry: run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> None
# Reads/writes JSON state via smt_state.py. Emits no caller-routable signals.

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from session_times import cme_session_start as _cme_session_start

_ET = ZoneInfo("America/New_York")

CAUTIOUS_SECONDARY_MAX_DIST = 150  # pts — secondary (1m confirmation) max distance
CAUTIOUS_INITIAL_MAX_DIST   = 110  # pts — initial (5m confirmation) max distance
CAUTIOUS_MIN_DIST           =  40  # pts — below this secondary distance, skip the entry
CAUTIOUS_DIST_SHRINK_PCT    = 0.15  # fraction the two max-dist thresholds shrink per failed entry
CAUTIOUS_INITIAL_OFFSET_PTS   = 2.0  # pts — initial cautious target set this much closer than the level
CAUTIOUS_SECONDARY_OFFSET_PTS = 5.0  # pts — secondary cautious target set this much closer than the level

# SMT V2 relevance filter (Phase 2). When ACTIVE (in a position), a new SMT enters the
# active set only if its ref level is within RELEVANCE_X_PTS of a cautious target OR its
# tier outranks the backing tier. First-guess default; tunable.
RELEVANCE_X_PTS = 25.0  # pts — proximity gate for the ingest filter

# SMT V2 Part B — Rule B (recency-trend cross-tier suppression). SHIPS DEFAULT OFF; gated
# behind RULE_B_ENABLED. A fresher opposite-direction SMT suppresses an older contradicting
# one ACROSS tiers when min-age, adverse-move, and tier-slack gates all hold. Measured in
# shadow before any Phase-3 wiring; first-guess thresholds, tunable.
RULE_B_ENABLED      = False   # default OFF — Rule B is measured, not shipped active
RULE_B_MIN_AGE_MIN  = 15.0    # min minutes the newer opposite SMT must postdate the older
RULE_B_ADVERSE_PTS  = 40.0    # min pts price moved AGAINST the older SMT (MNQ)
RULE_B_TIER_SLACK   = 1       # newer tier may be this many ranks below the older and still win

# SMT V2 Part B — leg-scoped counter-trend suppression. After a FIXED level is swept-and-
# reclaimed, suppress dominant counter-trend SMTs that predate the reclaim until price
# returns to the swept origin. RECLAIM_MARGIN_PTS = how far beyond the level counts as a
# genuine breach (not noise) before the reclaim back through it.
RECLAIM_MARGIN_PTS  = 15.0    # pts — breach depth beyond a FIXED level to register a sweep

# P2b: suppress LOW-arm DOWN when price is this many points above the swept level.
# 50-pt overshoot zone = false positives (38% correct); 20-50pt zone is 80% correct.
LOW_ARM_DOWN_OVERSHOOT_SUPPRESS_PTS = 50.0

# P8: ATH guard ends at this hour (exclusive). Before this hour → UP (AMD expansion).
# At or after this hour → eligible for DOWN (distribution/stop-hunt).
# Set to 13 to restore pre-P8 behavior (original pm_kill_zone boundary).
P8_ATH_GUARD_HOUR = 12


def compute_cautious_prices(
    direction: str,
    current_close: float,
    liquidities: list,
    ath: float,
    dist_shrinks: int = 0,
) -> dict:
    """Return cautious price fields anchored at current_close.

    Called at hypothesis time and again at position entry so levels are
    re-evaluated against the actual fill price rather than the hypothesis price.

    `dist_shrinks` (the per-hypothesis `cautious_dist_shrinks` counter) shrinks both
    max-distance thresholds by `CAUTIOUS_DIST_SHRINK_PCT` per failed entry, floored at
    `CAUTIOUS_MIN_DIST`. At dist_shrinks=0 the thresholds are unchanged (the effective
    maxes equal the module constants), so output is identical to pre-change behavior.
    """
    _factor = (1.0 - CAUTIOUS_DIST_SHRINK_PCT) ** max(0, dist_shrinks)
    _sec_max = max(CAUTIOUS_MIN_DIST, CAUTIOUS_SECONDARY_MAX_DIST * _factor)
    _init_max = max(CAUTIOUS_MIN_DIST, CAUTIOUS_INITIAL_MAX_DIST * _factor)

    _cautious_all = []
    for liq in liquidities:
        liq_kind = liq.get("kind")
        if liq_kind == "level":
            p = liq.get("price")
        elif liq_kind == "fvg":
            # keep:True = fill-only yesterday-session FVG universe (SMT-fills); not an
            # actionable hypothesis level. Skip so trade behavior is unchanged.
            if liq.get("keep"):
                continue
            p = liq.get("bottom") if direction == "up" else liq.get("top")
        else:
            continue
        if p is None:
            continue
        if direction == "up" and current_close < p <= current_close + _sec_max:
            _cautious_all.append((p, liq.get("name", "")))
        elif direction == "down" and current_close - _sec_max <= p < current_close:
            _cautious_all.append((p, liq.get("name", "")))
    if direction == "up" and current_close < ath <= current_close + _sec_max:
        _cautious_all.append((ath, "ATH"))

    cautious_price_initial         = ""
    cautious_price_initial_level   = ""
    cautious_price_secondary       = ""
    cautious_price_secondary_level = ""

    if _cautious_all:
        _sec = max(_cautious_all, key=lambda x: x[0]) if direction == "up" \
               else min(_cautious_all, key=lambda x: x[0])
        cautious_price_secondary       = _sec[0] - CAUTIOUS_SECONDARY_OFFSET_PTS if direction == "up" \
                                         else _sec[0] + CAUTIOUS_SECONDARY_OFFSET_PTS
        cautious_price_secondary_level = _sec[1]

        # Filter using raw level price so the offset doesn't shrink the candidate pool.
        if direction == "up":
            _init_candidates = [(p, n) for p, n in _cautious_all
                                if p < _sec[0] and p <= current_close + _init_max]
        else:
            _init_candidates = [(p, n) for p, n in _cautious_all
                                if p > _sec[0] and p >= current_close - _init_max]

        if _init_candidates:
            _ini = max(_init_candidates, key=lambda x: x[0]) if direction == "up" \
                   else min(_init_candidates, key=lambda x: x[0])
            if abs(_ini[0] - current_close) >= CAUTIOUS_MIN_DIST:
                cautious_price_initial       = _ini[0] - CAUTIOUS_INITIAL_OFFSET_PTS if direction == "up" \
                                               else _ini[0] + CAUTIOUS_INITIAL_OFFSET_PTS
                cautious_price_initial_level = _ini[1]
            else:
                _syn_dist = 0.85 * abs(float(cautious_price_secondary) - current_close)
                if _syn_dist >= CAUTIOUS_MIN_DIST:
                    cautious_price_initial = (
                        current_close - _syn_dist if direction == "down"
                        else current_close + _syn_dist
                    )
                    cautious_price_initial_level = "synthetic_85pct"
        else:
            _syn_dist = 0.85 * abs(float(cautious_price_secondary) - current_close)
            if _syn_dist >= CAUTIOUS_MIN_DIST:
                cautious_price_initial = (
                    current_close - _syn_dist if direction == "down"
                    else current_close + _syn_dist
                )
                cautious_price_initial_level = "synthetic_85pct"
    else:
        _terminal_names = {"day_low", "week_low"} if direction == "down" else {"day_high", "week_high"}
        _terminal_candidates = []
        for liq in liquidities:
            if liq.get("name") in _terminal_names and liq.get("kind") == "level":
                p = liq.get("price")
                if p is None:
                    continue
                if direction == "down" and p < current_close:
                    _terminal_candidates.append((p, liq["name"]))
                elif direction == "up" and p > current_close:
                    _terminal_candidates.append((p, liq["name"]))
        if _terminal_candidates:
            _sec = max(_terminal_candidates, key=lambda x: x[0]) if direction == "down" \
                   else min(_terminal_candidates, key=lambda x: x[0])
            cautious_price_secondary       = _sec[0] - CAUTIOUS_SECONDARY_OFFSET_PTS if direction == "up" \
                                             else _sec[0] + CAUTIOUS_SECONDARY_OFFSET_PTS
            cautious_price_secondary_level = _sec[1]
            _syn_dist = 0.85 * abs(float(cautious_price_secondary) - current_close)
            if _syn_dist >= CAUTIOUS_MIN_DIST:
                cautious_price_initial = (
                    current_close - _syn_dist if direction == "down"
                    else current_close + _syn_dist
                )
                cautious_price_initial_level = "synthetic_85pct"

    return {
        "cautious_price_initial":        cautious_price_initial,
        "cautious_price_initial_level":  cautious_price_initial_level,
        "cautious_price_secondary":      cautious_price_secondary,
        "cautious_price_secondary_level": cautious_price_secondary_level,
    }


def recompute_cautious_for_fill(
    hypothesis: dict,
    fill_price: float,
    liquidities: list,
    ath,
    dist_shrinks: int = 0,
) -> dict:
    """Re-anchor the two-tier cautious ladder to the *actual fill price* (Addendum 4).

    `compute_cautious_prices` runs once at hypothesis formation, anchored to the
    formation `current_close`. When the entry fills far from that price (e.g. a short
    that fills 97 pts above formation), the stored ladder is stale/unreachable and
    protection never arms — the position then rides unmanaged. Calling this at fill
    re-derives the cautious levels against the fill so the targets stay relevant and
    up-to-date. Direction / liquidities / ATH are unchanged; only the cautious price
    fields are overwritten. Mutates and returns the hypothesis dict. No-op unless the
    direction is up/down, or while the manual direction lock is set (GIL-8: the
    user's locked ladder is preserved across fills until released).
    """
    direction = hypothesis.get("direction")
    if direction not in ("up", "down") or hypothesis.get("manual"):
        return hypothesis
    cp = compute_cautious_prices(direction, float(fill_price), liquidities, ath, dist_shrinks)
    hypothesis["cautious_price_initial"]         = cp["cautious_price_initial"]
    hypothesis["cautious_price_initial_level"]   = cp["cautious_price_initial_level"]
    hypothesis["cautious_price_secondary"]       = cp["cautious_price_secondary"]
    hypothesis["cautious_price_secondary_level"] = cp["cautious_price_secondary_level"]
    return hypothesis

LIQUIDITY_APPROACH_DIST    = 100   # pts — Rule 2: "nearly approaching" radius
NEAR_EXTREME_DIST          =  75   # pts — Rule 3a: proximity boost to daily extreme
MOMENTUM_BARS              =   5   # 1m bars — Rule 2: recent momentum window
BOS_SWING_N                =   2   # bars each side for swing high/low detection
BOS_LOOKBACK_1HR           =   8   # 1hr bars — recency window for BOS/CHoCH
BOS_LOOKBACK_4HR           =   3   # 4hr bars — recency window for BOS/CHoCH
DIRECTION_SCORE_THRESHOLD  =  0.35 # combined Rule 3+4 score required to commit
RULE2B_ANCHOR_MAX_AGE_HOURS  =  1.0 # hrs — combined with RULE2B_STALE_RECOVERY_PTS
RULE2B_STALE_RECOVERY_PTS   = 200.0 # pts — anchor is stale only when BOTH age > max_age AND
                                     #       price has recovered this far from the sweep level

from smt_state import (
    load_global,
    load_daily,
    load_hypothesis,
    load_position,
    save_hypothesis,
    save_position,
)
import strategy as _strategy
from strategy_smt import detect_smt_divergence, detect_smt_fill
from smt_detect import _level_class as _smt_level_class, _record_key as _smt_record_key


# ===========================================================================
# SMT V2 relevance-filter infrastructure (Phase 2) — Contract B.
#
# Pure functions (total, no IO, JSON-serializable in/out) consumed by the shadow
# active-set wiring in session_pipeline (Phase 2) and the dominant→direction wiring
# in Phase 3. In Phase 2 these compute the active set + dominant but DO NOT drive
# direction — the existing direction engine is unchanged.
#
# --- divs record schema (the persisted active-set record) ---
# A divs record is a flat JSON-serializable dict with these fields:
#   kind       : "smt" | "fill"
#   type       : "wick" | "body" | "fill_a" | "fill_b"
#   side       : "bullish" | "bearish"
#   direction  : "long" | "short"
#   timeframe  : "1m" | "15m" | "30m" | "1h"
#   time       : ISO string (fire time)
#   leader     : "mnq" | "mes"
#   ref_name   : level name (e.g. "day_high") or FVG name (e.g. "fvg_<ts>_<side>")
#   tier       : "ATH" | "week" | "day" | "fill" | "session"
#   key        : the smt_detect detect_state key (for fulfillment queries),
#                produced by smt_detect._record_key — always matches detect_state.
#   fulfilled  : bool (freshly-emitted SMTs are unfulfilled; invalidation updates this)
#   prices     : mnq_price, mes_price, mnq_lvl_price (level/zone comparison price),
#                and mes_lvl_price when present. Fills carry the FVG-derived ref price
#                where available; otherwise mnq_lvl_price=None.
# ===========================================================================

# Tier authority ranks (higher = more authoritative). ATH and week share the top
# bucket; day > fill (1hr-FVG-fill) > session (6hr). Unknown → 0.
_TIER_RANK = {"ATH": 4, "week": 4, "day": 3, "fill": 2, "session": 1}


def _tier_rank(tier: "str | None") -> int:
    """Authority rank for a tier (higher = more authoritative). Unknown/None → 0."""
    return _TIER_RANK.get(tier or "", 0)


def collapsed_status(record: dict, status_map: dict) -> str:
    """Aggregate a (possibly collapsed) record's fulfillment over its underlying detect
    keys. A collapsed record carries ``keys: list[str]`` (>=1) — wick+body folded into
    one logical SMT. Per-record rule (Contract C aggregation):
      - "fulfilled" if ANY underlying key is fulfilled,
      - "gone"      if ALL underlying keys are gone,
      - "unfulfilled" otherwise.

    ``status_map`` is ``{key: "unfulfilled"|"fulfilled"|"gone"}`` (from
    ``smt_detect.fulfillment_status``). Total/None-safe: missing keys → treated as "gone";
    a record lacking ``keys`` falls back to its scalar ``key``; never raises.
    """
    if not isinstance(record, dict):
        return "gone"
    ks = record.get("keys") or [record.get("key")]
    sm = status_map if isinstance(status_map, dict) else {}
    sts = [sm.get(k, "gone") for k in ks]
    if any(s == "fulfilled" for s in sts):
        return "fulfilled"
    if sts and all(s == "gone" for s in sts):
        return "gone"
    return "unfulfilled"


def collapsed_relevance(record: dict, status_map: dict) -> str:
    """Aggregate a (possibly collapsed) record's relevance over its underlying detect
    keys, treating ``invalidated`` as a TERMINAL state alongside ``fulfilled``/``gone``.

    Extends :func:`collapsed_status` to the C-STATUS vocabulary
    (``unfulfilled|fulfilled|invalidated|gone``). ``status_map`` is the output of
    ``smt_detect.smt_status`` (``{key: status}``). Per-record aggregation precedence:
      - "fulfilled"   if ANY underlying key is fulfilled,
      - "invalidated" else if ANY underlying key is invalidated,
      - "gone"        else if ALL underlying keys are gone,
      - "unfulfilled" otherwise.

    The shadow active-set drop treats fulfilled/invalidated/gone all as terminal, so a
    collapsed record is dropped when this returns anything other than "unfulfilled".

    Total/None-safe: missing keys → treated as "gone"; a record lacking ``keys`` falls
    back to its scalar ``key``; never raises.
    """
    if not isinstance(record, dict):
        return "gone"
    ks = record.get("keys") or [record.get("key")]
    sm = status_map if isinstance(status_map, dict) else {}
    sts = [sm.get(k, "gone") for k in ks]
    if any(s == "fulfilled" for s in sts):
        return "fulfilled"
    if any(s == "invalidated" for s in sts):
        return "invalidated"
    if sts and all(s == "gone" for s in sts):
        return "gone"
    return "unfulfilled"


def to_record(emission: dict) -> dict:
    """Map a `smt_detect` emission to the divs record schema (see module docstring).

    Accepts a level/fill emission from `_detect_level_smts`/`detect_fill_smts` (or the
    `smt-div` shadow event built in `_run_smt_v2_detection`). Total: missing fields
    default to None/False; never raises.

    `tier`: for kind=="smt" use `_level_class(ref_name)[1]` (week/day/session); ATH is
    special-cased — `ref_name=="ATH"` OR `is_ath=True` (carried on the emission) → "ATH".
    For kind=="fill" → "fill". `key`: `smt_detect._record_key(emission)`. `fulfilled`
    defaults False (invalidation updates it via Contract C).
    """
    if not isinstance(emission, dict):
        emission = {}
    kind = emission.get("kind")
    ref_name = emission.get("ref_name")

    if kind == "fill":
        tier = "fill"
    else:
        # ATH special-case: explicit flag or synthetic "ATH" ref_name. Phase 3 supplies
        # the ATH context (e.g. flagging a week_high SMT as ATH); Phase 2 keeps it simple.
        if ref_name == "ATH" or emission.get("is_ath") is True:
            tier = "ATH"
        else:
            try:
                tier = _smt_level_class(ref_name or "")[1]
            except Exception:
                tier = None

    rec = {
        "kind":          kind,
        "type":          emission.get("type"),
        "side":          emission.get("side"),
        "direction":     emission.get("direction"),
        "timeframe":     emission.get("timeframe"),
        "time":          emission.get("time"),
        "leader":        emission.get("leader"),
        "ref_name":      ref_name,
        "tier":          tier,
        "key":           _smt_record_key(emission),
        "fulfilled":     bool(emission.get("fulfilled", False)),
        "invalidated":   bool(emission.get("invalidated", False)),
        "mnq_price":     emission.get("mnq_price"),
        "mes_price":     emission.get("mes_price"),
        "mnq_lvl_price": emission.get("mnq_lvl_price"),
    }
    # Logical-collapse support: a fresh record maps to exactly one detect key. The list
    # form lets ingest_smts merge wick+body variants while fulfillment aggregates over all
    # underlying keys (Contract C). None-safe: if key is None, [None] is fine downstream.
    rec["keys"] = [rec["key"]]
    if emission.get("mes_lvl_price") is not None:
        rec["mes_lvl_price"] = emission.get("mes_lvl_price")
    return rec


def smt_authority(record: dict) -> tuple:
    """Sortable authority tuple for a divs record (LARGER = more authoritative).

    `dominant = max(active_set, key=smt_authority)`. Tuple order (most-significant first):
      (tier_rank, kind_rank, recency_value, tf_rank)
      - tier_rank : ATH≡week (4) > day (3) > fill (2) > session (1); unknown → 0.
      - kind_rank : wick (1) > body (0); fills/other → 1 (a fill isn't penalized below a
                    hidden SMT of the same tier — tier already dominates across levels).
      - recency   : `time` parsed to ns (pandas.Timestamp); on parse failure → 0 (epoch).
                    Larger = more recent.
      - tf_rank   : 30m (1) > 15m (0); others → 0 — lowest-significance sub-tiebreak.

    Total: non-dict / missing fields → safe defaults; never raises.
    """
    if not isinstance(record, dict):
        return (0, 0, 0, 0)
    tier_rank = _tier_rank(record.get("tier"))
    rec_type = record.get("type")
    kind_rank = 1 if rec_type == "wick" else (0 if rec_type == "body" else 1)
    try:
        ts = pd.Timestamp(record.get("time"))
        recency = int(ts.value) if ts is not pd.NaT else 0
    except Exception:
        recency = 0
    tf = record.get("timeframe")
    tf_rank = {"30m": 1, "15m": 0}.get(tf, 0)
    return (tier_rank, kind_rank, recency, tf_rank)


def dominant(active_set: "list[dict] | None") -> "dict | None":
    """The single most-authoritative record in the active set, or None on empty/None."""
    if not active_set:
        return None
    return max(active_set, key=smt_authority)


def ingest_smts(
    new_records: "list[dict] | None",
    active_set: "list[dict] | None",
    *,
    flat: bool,
    cautious_targets: "dict | None",
    backing_tier: "str | None",
    x_pts: float,
    apply_rule_a_step: bool = True,
) -> list[dict]:
    """Ingest fresh SMT records into the active set per the LOCKED relevance pipeline.

    `new_records` are in divs-record schema (callers run `to_record` first; defensively
    re-run on records lacking key/tier). `active_set` is the current persisted list.

    Pipeline:
      1. Drop fulfilled/ineligible incoming records (fulfilled is True, or missing a
         usable key/direction).
      2. Gate by position state:
         - FLAT (flat=True): any tier may enter.
         - ACTIVE (flat=False): enter only if EITHER the ref level price is within
           `x_pts` of a cautious target (initial OR secondary; inclusive `<=`), OR
           `_tier_rank(tier) >= _tier_rank(backing_tier)` (inclusive `>=`).
      3. Collapse/supersede by the LOGICAL key `(ref_name, direction)`: at most one active
         record per logical key. Wick+body variants of the same logical SMT collapse into
         one member — wick supersedes body (stronger confirmation), then newer `time` wins.
         The surviving member carries `keys: list[str]` = the union of all folded detect
         keys (so fulfillment can aggregate across both variants), and keeps its scalar
         `key` for back-compat. Fills (unique per FVG) are unaffected — collapse is a no-op.

    Returns a NEW list (does not mutate inputs). Total: new_records=None → copy of
    active_set; never raises.
    """
    base = list(active_set or [])
    if not new_records:
        return base

    ct = cautious_targets or {}

    def _target_price(field: str) -> "float | None":
        v = ct.get(field, "")
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    init_t = _target_price("cautious_price_initial")
    sec_t = _target_price("cautious_price_secondary")
    backing_rank = _tier_rank(backing_tier)

    def _logical_key(r: dict):
        return (r.get("ref_name"), r.get("direction"))

    def _record_time(r: dict):
        try:
            ts = pd.Timestamp(r.get("time"))
            return int(ts.value) if ts is not pd.NaT else 0
        except Exception:
            return 0

    def _confirm_strength(r: dict) -> int:
        # wick supersedes body; non-wick/body (fills) are not down-ranked below body.
        t = r.get("type")
        if t == "wick":
            return 2
        if t == "body":
            return 0
        return 1

    def _rec_keys(r: dict) -> list:
        ks = r.get("keys")
        if isinstance(ks, list) and ks:
            return list(ks)
        return [r.get("key")]

    def _merge_keys(a: dict, b: dict) -> list:
        merged: list = []
        for k in _rec_keys(a) + _rec_keys(b):
            if k not in merged:
                merged.append(k)
        return merged

    def _supersedes(new: dict, old: dict) -> bool:
        # wick>body first; equal confirmation strength → newer time wins (>= keeps the
        # incoming record, matching the prior newer-wins dedup semantics).
        ns, os_ = _confirm_strength(new), _confirm_strength(old)
        if ns != os_:
            return ns > os_
        return _record_time(new) >= _record_time(old)

    # Index existing active set by the logical key for collapse/supersede.
    by_key: dict = {}
    order: list = []
    for r in base:
        lk = _logical_key(r)
        if lk not in by_key:
            order.append(lk)
        by_key[lk] = r

    for raw in new_records:
        rec = raw
        # Defensive: re-derive schema if a record lacks key/tier.
        if not isinstance(rec, dict) or "key" not in rec or "tier" not in rec:
            rec = to_record(rec if isinstance(rec, dict) else {})
        # (1) Drop fulfilled / invalidated / ineligible. `invalidated` is a terminal
        # producer-side state (Part A); absent flag → False → no drop (forward-compatible).
        if rec.get("fulfilled") is True or rec.get("invalidated") is True:
            continue
        key = rec.get("key")
        if not key or not rec.get("direction"):
            continue
        # (2) Gate by position state.
        if not flat:
            tier = rec.get("tier")
            level_price = rec.get("mnq_lvl_price")
            if level_price is None:
                level_price = rec.get("mnq_price")
            prox_ok = False
            if level_price is not None:
                try:
                    lp = float(level_price)
                    for t in (init_t, sec_t):
                        if t is not None and abs(lp - t) <= x_pts:
                            prox_ok = True
                            break
                except (TypeError, ValueError):
                    prox_ok = False
            tier_ok = _tier_rank(tier) >= backing_rank
            if not (prox_ok or tier_ok):
                continue
        # Ensure the incoming record carries the list form (defensive for raw dicts that
        # bypassed to_record but already had key/tier).
        if not isinstance(rec.get("keys"), list) or not rec.get("keys"):
            rec = dict(rec)
            rec["keys"] = [rec.get("key")]
        # (3) Collapse / supersede by logical (ref_name, direction) key.
        lk = _logical_key(rec)
        existing = by_key.get(lk)
        if existing is None:
            by_key[lk] = rec
            order.append(lk)
        else:
            merged_keys = _merge_keys(existing, rec)
            if _supersedes(rec, existing):
                survivor = dict(rec)
            else:
                survivor = dict(existing)
            survivor["keys"] = merged_keys
            by_key[lk] = survivor

    collapsed = [by_key[k] for k in order]
    # Rule A — same-level latest-take-out-wins. Operates ON the collapsed set (above already
    # merged same-(ref_name,direction)); Rule A resolves OPPOSITE-direction same-ref_name by
    # recency. Pure: returns (kept, events). Callers that want the superseded-event TRAIL
    # pass `apply_rule_a_step=False` and run `apply_rule_a` themselves (capturing events);
    # the default keeps `ingest_smts` self-contained for direct callers/tests.
    if not apply_rule_a_step:
        return collapsed
    kept, _events = apply_rule_a(collapsed)
    return kept


def apply_rule_a(active: "list[dict] | None") -> "tuple[list[dict], list[dict]]":
    """Rule A — same-level latest-take-out-wins (Contract C-RULEA).

    Groups the active set by ``ref_name``; if a single ``ref_name`` holds BOTH directions,
    keep only the most-recent (by ``time``) and drop the older opposite-direction record(s).
    Same-direction sets are untouched (collapse already merged those upstream). Records
    with no ``ref_name`` are passed through unchanged.

    Pure / total: returns ``(kept, superseded_events)`` where each event is::

        {"event": "superseded_same_level", "ref_name": ..., "kept_key": ...,
         "kept_direction": ..., "kept_time": ..., "dropped_key": ...,
         "dropped_direction": ..., "dropped_time": ...}

    Never mutates its input; never raises.
    """
    src = list(active or [])
    if not src:
        return src, []

    def _rtime(r: dict) -> int:
        try:
            ts = pd.Timestamp(r.get("time"))
            return int(ts.value) if ts is not pd.NaT else 0
        except Exception:
            return 0

    # Group indices by ref_name preserving first-seen order.
    groups: dict = {}
    order_rn: list = []
    for i, r in enumerate(src):
        rn = r.get("ref_name") if isinstance(r, dict) else None
        if rn is None:
            continue
        if rn not in groups:
            groups[rn] = []
            order_rn.append(rn)
        groups[rn].append(i)

    drop_idx: set = set()
    events: list = []
    for rn in order_rn:
        idxs = groups[rn]
        non_none_dirs = {src[i].get("direction") for i in idxs
                         if src[i].get("direction") is not None}
        # Only act when at least two DISTINCT non-None directions are present.
        if len(non_none_dirs) < 2:
            continue
        # Keep the single most-recent record for this ref_name; drop every older record
        # whose direction differs from the winner's (opposite-direction supersession).
        winner = max(idxs, key=lambda i: _rtime(src[i]))
        win_dir = src[winner].get("direction")
        for i in idxs:
            if i == winner:
                continue
            if src[i].get("direction") != win_dir:
                drop_idx.add(i)
                events.append({
                    "event":             "superseded_same_level",
                    "ref_name":          rn,
                    "kept_key":          src[winner].get("key"),
                    "kept_direction":    win_dir,
                    "kept_time":         src[winner].get("time"),
                    "dropped_key":       src[i].get("key"),
                    "dropped_direction": src[i].get("direction"),
                    "dropped_time":      src[i].get("time"),
                })

    kept = [src[i] for i in range(len(src)) if i not in drop_idx]
    return kept, events


def _ts_value(t) -> "int | None":
    """Parse a time-ish value to ns since epoch; None on failure (total/never-raises)."""
    try:
        ts = pd.Timestamp(t)
        return int(ts.value) if ts is not pd.NaT else None
    except Exception:
        return None


def apply_rule_b(
    active: "list[dict] | None",
    *,
    now_close: "float | None",
    enabled: bool,
    min_age_min: float,
    adverse_pts: float,
    tier_slack: int,
) -> "tuple[list[dict], list[dict]]":
    """Rule B — recency-trend cross-tier suppression (Contract C-RULEB). GATED.

    A record ``O`` is suppressed iff a newer opposite-direction record ``N`` exists with:
      - ``N.time - O.time >= min_age_min`` (the newer signal is meaningfully fresher),
      - price has moved AGAINST ``O`` by ``>= adverse_pts`` — for a SHORT ``O`` (expects
        price down) adverse = ``now_close - O_level >= adverse_pts``; for a LONG ``O``
        (expects price up) adverse = ``O_level - now_close >= adverse_pts``, where
        ``O_level`` is ``O.mnq_lvl_price`` (fallback ``O.mnq_price``), and
      - ``_tier_rank(N.tier) >= _tier_rank(O.tier) - tier_slack`` (cross-tier: ``N`` need
        not strictly outrank ``O`` — it may be ``tier_slack`` ranks below and still win).

    Pure / total: returns ``(kept, suppressed_events)``. No-op (identity, empty events)
    when ``enabled=False`` or ``now_close`` is None. Never mutates input; never raises.
    """
    src = list(active or [])
    if not enabled or now_close is None or not src:
        return src, []
    try:
        nc = float(now_close)
    except (TypeError, ValueError):
        return src, []

    def _olevel(r: dict) -> "float | None":
        v = r.get("mnq_lvl_price")
        if v is None:
            v = r.get("mnq_price")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    drop_idx: set = set()
    events: list = []
    for oi, o in enumerate(src):
        o_dir = o.get("direction")
        if o_dir not in ("long", "short"):
            continue
        o_t = _ts_value(o.get("time"))
        o_lvl = _olevel(o)
        if o_t is None or o_lvl is None:
            continue
        # adverse-move gate (against O).
        if o_dir == "short":
            adverse_ok = (nc - o_lvl) >= adverse_pts
        else:
            adverse_ok = (o_lvl - nc) >= adverse_pts
        if not adverse_ok:
            continue
        o_rank = _tier_rank(o.get("tier"))
        # find a qualifying newer opposite N.
        for ni, n in enumerate(src):
            if ni == oi:
                continue
            if n.get("direction") not in ("long", "short"):
                continue
            if n.get("direction") == o_dir:
                continue  # must be opposite
            n_t = _ts_value(n.get("time"))
            if n_t is None:
                continue
            age_min = (n_t - o_t) / 6e10  # ns → minutes
            if age_min < min_age_min:
                continue
            if _tier_rank(n.get("tier")) < (o_rank - tier_slack):
                continue
            drop_idx.add(oi)
            events.append({
                "event":             "suppressed_by_trend",
                "ref_name":          o.get("ref_name"),
                "dropped_key":       o.get("key"),
                "dropped_direction": o_dir,
                "dropped_time":      o.get("time"),
                "by_key":            n.get("key"),
                "by_direction":      n.get("direction"),
                "by_time":           n.get("time"),
                "now_close":         nc,
            })
            break

    kept = [src[i] for i in range(len(src)) if i not in drop_idx]
    return kept, events


def _fixed_level_candidates(fixed_levels: "list[dict] | None") -> list:
    """Normalize fixed-level inputs to ``[{"name","price"}]`` (total/None-safe).

    Accepts liquidity dicts (``{"name","kind":"level","price"}``) — only FIXED-tier levels
    (``_smt_level_class(name)[0] == "fixed"``) with a high/low sub are kept, so dynamic
    running ``day_*``/``week_*`` extremes are excluded (only prev*/session fixed levels
    qualify as swept-and-reclaimed leg origins).
    """
    out: list = []
    for lv in fixed_levels or []:
        if not isinstance(lv, dict):
            continue
        if lv.get("kind") not in (None, "level"):
            continue
        name = lv.get("name")
        price = lv.get("price")
        if name is None or price is None:
            continue
        try:
            kind_cls, _tier = _smt_level_class(name)
        except Exception:
            continue
        if kind_cls != "fixed":
            continue
        # Only high/low levels are touch/sweep targets.
        if not (str(name).endswith("_high") or str(name).endswith("_low")):
            continue
        try:
            out.append({"name": name, "price": float(price)})
        except (TypeError, ValueError):
            continue
    return out


def update_leg(
    leg_state: "dict | None",
    *,
    fixed_levels: "list[dict] | None",
    now_close: "float | None",
    now_time,
) -> dict:
    """Track the most-recently swept-and-reclaimed FIXED level (Contract C-LEG).

    A leg is registered when a FIXED level is breached beyond by ``RECLAIM_MARGIN_PTS`` and
    then price closes back THROUGH it (the reclaim). The level is chosen DYNAMICALLY from
    ``fixed_levels`` — never hardcoded. Tracks per-candidate breach progress in
    ``leg_state["_breaches"]``; the active leg is recorded under ``leg_state["leg"]`` as::

        {"level_name", "level_price", "origin_price", "reclaim_time", "recovery_dir"}

    ``recovery_dir`` is ``"long"`` when a low was breached-down then reclaimed-up,
    ``"short"`` when a high was breached-up then reclaimed-down. ``origin_price`` is the
    swept level price — the leg clears (handled by ``suppress_counter_trend``) once
    ``now_close`` returns to it.

    Pure-ish: returns a NEW leg_state dict (does not mutate the input). Total: degenerate
    input → unchanged copy; never raises.
    """
    st = dict(leg_state) if isinstance(leg_state, dict) else {}
    breaches = dict(st.get("_breaches") or {})
    st["_breaches"] = breaches
    if now_close is None:
        return st
    try:
        nc = float(now_close)
    except (TypeError, ValueError):
        return st

    cands = _fixed_level_candidates(fixed_levels)
    iso = None
    try:
        iso = pd.Timestamp(now_time).isoformat() if now_time is not None else None
    except Exception:
        iso = None

    for c in cands:
        name = c["name"]
        price = c["price"]
        is_high = str(name).endswith("_high")
        b = breaches.get(name) or {"breached": False}
        # Register a breach beyond the level by the margin.
        if is_high:
            if nc >= price + RECLAIM_MARGIN_PTS:
                b["breached"] = True
            elif b.get("breached") and nc < price:
                # Reclaim: closed back below the high after breaching above → recovery short.
                st["leg"] = {
                    "level_name":   name,
                    "level_price":  price,
                    "origin_price": price,
                    "reclaim_time": iso,
                    "recovery_dir": "short",
                }
                b["breached"] = False
        else:  # low
            if nc <= price - RECLAIM_MARGIN_PTS:
                b["breached"] = True
            elif b.get("breached") and nc > price:
                # Reclaim: closed back above the low after breaching below → recovery long.
                st["leg"] = {
                    "level_name":   name,
                    "level_price":  price,
                    "origin_price": price,
                    "reclaim_time": iso,
                    "recovery_dir": "long",
                }
                b["breached"] = False
        breaches[name] = b

    return st


def suppress_counter_trend(
    active: "list[dict] | None",
    leg_state: "dict | None",
    now_close: "float | None",
) -> "tuple[list[dict], list[dict], dict]":
    """Leg-scoped counter-trend suppression (Contract C-LEG, consumer side).

    Given an active leg (from :func:`update_leg`), drop dominant-eligible SMTs whose
    ``direction`` OPPOSES the leg's ``recovery_dir`` AND whose ``time < reclaim_time``,
    until ``now_close`` returns to ``origin_price`` (then the leg clears and nothing is
    suppressed). Records aligned with ``recovery_dir`` and records that postdate the
    reclaim are NEVER suppressed.

    Leg clears when price returns to origin:
      - recovery_dir "long"  (a low was reclaimed up): clears once ``now_close <= origin``.
      - recovery_dir "short" (a high was reclaimed down): clears once ``now_close >= origin``.

    Returns ``(kept, suppressed_events, new_leg_state)`` — new_leg_state has ``leg`` removed
    when the leg cleared. Pure/total: no leg or None inputs → identity; never raises.
    """
    src = list(active or [])
    st = dict(leg_state) if isinstance(leg_state, dict) else {}
    leg = st.get("leg")
    if not isinstance(leg, dict) or not src:
        return src, [], st

    origin = leg.get("origin_price")
    recovery_dir = leg.get("recovery_dir")
    reclaim_t = _ts_value(leg.get("reclaim_time"))
    try:
        nc = float(now_close) if now_close is not None else None
    except (TypeError, ValueError):
        nc = None

    # Leg-clear check: price returned to origin.
    if nc is not None and origin is not None:
        try:
            o = float(origin)
            if recovery_dir == "long" and nc <= o:
                st = dict(st)
                st.pop("leg", None)
                return src, [], st
            if recovery_dir == "short" and nc >= o:
                st = dict(st)
                st.pop("leg", None)
                return src, [], st
        except (TypeError, ValueError):
            pass

    if recovery_dir not in ("long", "short") or reclaim_t is None:
        return src, [], st

    counter = "short" if recovery_dir == "long" else "long"
    drop_idx: set = set()
    events: list = []
    for i, r in enumerate(src):
        if r.get("direction") != counter:
            continue
        r_t = _ts_value(r.get("time"))
        if r_t is None or r_t >= reclaim_t:
            continue  # only SMTs that PREDATE the reclaim
        drop_idx.add(i)
        events.append({
            "event":             "suppressed_by_leg",
            "ref_name":          r.get("ref_name"),
            "dropped_key":       r.get("key"),
            "dropped_direction": r.get("direction"),
            "dropped_time":      r.get("time"),
            "leg_level_name":    leg.get("level_name"),
            "leg_recovery_dir":  recovery_dir,
            "leg_reclaim_time":  leg.get("reclaim_time"),
        })

    kept = [src[i] for i in range(len(src)) if i not in drop_idx]
    return kept, events, st


def _build_5m_bar(mnq_1m: pd.DataFrame, now: datetime) -> dict | None:
    """Build the current 5m bar using bars from (now - 5min) to now.

    Per spec: "round now down to nearest 5m boundary, then take bars from now-5min to now."
    The bar_start is now rounded down to the nearest 5m boundary (= now - 5min when now
    is exactly on a 5m boundary). We include bars with timestamp in [bar_start, bar_end).

    Returns a dict with Open, High, Low, Close keys, or None if no bars fall in that window.
    """
    ts = pd.Timestamp(now)
    # Round down to nearest 5-minute boundary
    floored_minute = (ts.minute // 5) * 5
    bar_end = ts.replace(minute=floored_minute, second=0, microsecond=0)
    bar_start = bar_end - pd.Timedelta(minutes=5)

    # Filter bars in [bar_start, bar_end)
    window = mnq_1m[(mnq_1m.index >= bar_start) & (mnq_1m.index < bar_end)]
    if window.empty:
        return None

    return {
        "Open":  float(window["Open"].iloc[0]),
        "High":  float(window["High"].max()),
        "Low":   float(window["Low"].min()),
        "Close": float(window["Close"].iloc[-1]),
    }


def _get_liquidity_price(liq: dict) -> float | None:
    """Return the representative price for a liquidity entry."""
    if liq.get("kind") == "level":
        return liq.get("price")
    if liq.get("kind") == "fvg":
        # keep:True = fill-only yesterday-session FVG (SMT-fills universe); not actionable.
        if liq.get("keep"):
            return None
        # For FVGs, use the midpoint between top and bottom
        top = liq.get("top")
        bottom = liq.get("bottom")
        if top is not None and bottom is not None:
            return (top + bottom) / 2.0
    return None


def compute_mid_label(current_close: float, high_price: float, low_price: float) -> str:
    """Classify current_close relative to the midpoint of [low_price, high_price].

    Returns "mid" if within 10 points of the midpoint; "above" or "below" otherwise.
    """
    mid = (high_price + low_price) / 2.0
    diff = current_close - mid
    if abs(diff) <= 10:
        return "mid"
    return "above" if diff > 0 else "below"


def _find_last_liquidity(
    mnq_1m: pd.DataFrame,
    liquidities: list,
    extra_bars: pd.DataFrame | None = None,
) -> tuple[str, "pd.Timestamp | None"]:
    """Find the most recently-crossed meaningful liquidity level.

    Scans mnq_1m (session bars) and optionally extra_bars (True Day pre-session bars)
    so overnight/London-session level crosses are captured, not just post-09:20 bars.

    Restricted to: {week_high, week_low, day_high, day_low}.
    A level is "crossed" when the previous bar closed on the near side and the current
    bar's extreme reaches the far side (prev_close < price AND bar.High >= price for
    highs; prev_close > price AND bar.Low <= price for lows).
    Returns (name, cross_timestamp) of the most recently-crossed level, or ("", None).
    """
    meaningful_names = {
        "week_high", "week_low", "day_high", "day_low", "TDO", "TWO",
        "ny_morning_high", "ny_morning_low",
    }

    level_map = {}
    for liq in liquidities:
        if liq.get("name") in meaningful_names and liq.get("kind") == "level":
            level_map[liq["name"]] = liq["price"]

    if not level_map:
        return "", None

    if extra_bars is not None and not extra_bars.empty:
        bars_array = pd.concat([extra_bars, mnq_1m])
        bars_array = bars_array[~bars_array.index.duplicated(keep="last")].sort_index()
    else:
        bars_array = mnq_1m

    high_names = {"week_high", "day_high", "ny_morning_high"}
    best_idx   = -1
    best_name  = ""

    closes = bars_array["Close"].values
    highs  = bars_array["High"].values
    lows   = bars_array["Low"].values

    for name, price in level_map.items():
        if name in high_names:
            # upward cross: prev close below level, current bar reaches above
            crossed = (closes[:-1] < price) & (highs[1:] >= price)
        else:
            # downward cross: prev close above level, current bar reaches below
            crossed = (closes[:-1] > price) & (lows[1:] <= price)
        idxs = np.where(crossed)[0] + 1
        if len(idxs) > 0:
            last_cross = int(idxs[-1])
            if last_cross > best_idx:
                best_idx = last_cross
                best_name = name

    if best_idx < 0:
        return "", None
    return best_name, bars_array.index[best_idx]


def _compute_divs(
    mnq_1m: pd.DataFrame,
    mes_1m: pd.DataFrame,
) -> list:
    """Compute SMT divergences by resampling to 15m and 30m bars.

    Calls detect_smt_divergence and detect_smt_fill at each resampled bar.
    Returns a list of dicts: {timeframe, type, side, time}.
    """
    divs = []

    agg = {
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }

    for tf_label, tf_rule in [("15m", "15min"), ("30m", "30min")]:
        # Resample both instruments to the target timeframe
        mnq_rs = mnq_1m.resample(tf_rule, label="left").agg(agg).dropna(subset=["Open"])
        mes_rs = mes_1m.resample(tf_rule, label="left").agg(agg).dropna(subset=["Open"])

        # Align indices: only process bars present in both
        common_idx = mnq_rs.index.intersection(mes_rs.index)
        if len(common_idx) < 2:
            continue

        mnq_aligned = mnq_rs.loc[common_idx].reset_index(drop=False)
        mes_aligned = mes_rs.loc[common_idx].reset_index(drop=False)

        # Rebuild DataFrames with integer index for detect_smt_divergence
        mnq_df = mnq_aligned.set_index(mnq_aligned.columns[0])
        mes_df = mes_aligned.set_index(mes_aligned.columns[0])

        # Only check the most recently completed bar — prevents re-firing old divs
        # on each successive call as the session window grows.
        bar_idx = len(mnq_df) - 1
        if bar_idx < 1:
            continue
        bar_ts = mnq_df.index[bar_idx]

        mnq_pos = mnq_df.iloc[:bar_idx + 1].copy().reset_index(drop=True)
        mes_pos = mes_df.iloc[:bar_idx + 1].copy().reset_index(drop=True)

        result = detect_smt_divergence(
            mes_pos, mnq_pos, bar_idx=bar_idx, session_start_idx=0
        )
        bar_close = float(mnq_df.iloc[bar_idx]["Close"])

        if result is not None:
            direction, _sweep, _miss, smt_type, smt_level = result
            side = "bullish" if direction == "long" else "bearish"

            # Compute both MNQ and MES session extremes involved in the divergence,
            # independent of which instrument leads. smt_level alone is ambiguous
            # (it's MNQ for normal, MES for symmetric).
            _sess = slice(0, bar_idx)
            if "body" in smt_type:
                _mnq_hi = float(mnq_pos["Close"].iloc[_sess].max())
                _mnq_lo = float(mnq_pos["Close"].iloc[_sess].min())
                _mes_hi = float(mes_pos["Close"].iloc[_sess].max())
                _mes_lo = float(mes_pos["Close"].iloc[_sess].min())
            else:
                _mnq_hi = float(mnq_pos["High"].iloc[_sess].max())
                _mnq_lo = float(mnq_pos["Low"].iloc[_sess].min())
                _mes_hi = float(mes_pos["High"].iloc[_sess].max())
                _mes_lo = float(mes_pos["Low"].iloc[_sess].min())
            if direction == "short":  # bearish — a session high was swept
                mnq_div_price = _mnq_hi
                mes_div_price = _mes_hi
            else:                     # bullish — a session low was swept
                mnq_div_price = _mnq_lo
                mes_div_price = _mes_lo

            divs.append({
                "kind":          "smt-div",
                "time":          bar_ts.isoformat(),
                "side":          side,
                "timeframe":     tf_label,
                "type":          smt_type,
                "price":         bar_close,
                "mnq_div_price": mnq_div_price,
                "mes_div_price": mes_div_price,
            })

        fill_result = detect_smt_fill(mes_pos, mnq_pos, bar_idx=bar_idx)
        if fill_result is not None:
            fill_dir, _fvg_high, _fvg_low = fill_result
            fill_side = "bullish" if fill_dir == "long" else "bearish"
            divs.append({
                "kind":      "smt-div",
                "time":      bar_ts.isoformat(),
                "side":      fill_side,
                "timeframe": tf_label,
                "type":      "fill",
                "price":     bar_close,
                "level":     None,
            })

    return divs


def _find_nearest_bar(combined: pd.DataFrame, target_ts: pd.Timestamp) -> dict | None:
    """Find the bar nearest to target_ts in combined DataFrame (must be sorted)."""
    if combined.empty:
        return None
    idx = combined.index
    pos = idx.searchsorted(target_ts)
    pos = max(0, min(pos, len(idx) - 1))
    if pos > 0 and abs(idx[pos - 1] - target_ts) <= abs(idx[pos] - target_ts):
        pos -= 1
    bar = combined.iloc[pos]
    return {"Low": float(bar["Low"]), "High": float(bar["High"])}


# ---------------------------------------------------------------------------
# Direction-determination helpers (Rules 1-5)
# ---------------------------------------------------------------------------

def _named_price(liquidities: list, name: str) -> float | None:
    for liq in liquidities:
        if liq.get("name") == name and liq.get("kind") == "level":
            return float(liq["price"])
    return None


def _detect_fvg_1hr(
    hist_mnq_1m: pd.DataFrame,
    session_mnq_1m: pd.DataFrame,
    *,
    hist_1hr: "pd.DataFrame | None" = None,
) -> list:
    _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if hist_1hr is not None and not hist_1hr.empty:
        if not session_mnq_1m.empty:
            sess_1hr = session_mnq_1m.resample("1h").agg(_agg).dropna(subset=["Open"])
            bars = pd.concat([hist_1hr, sess_1hr])
            bars = bars[~bars.index.duplicated(keep="last")].sort_index()
        else:
            bars = hist_1hr
    else:
        combined = pd.concat([hist_mnq_1m, session_mnq_1m])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        bars = combined.resample("1h").agg(_agg).dropna(subset=["Open"])
    bars = bars.iloc[-(BOS_LOOKBACK_1HR + 5):]
    n = len(bars)
    fvgs = []
    for i in range(1, n - 1):
        hi_prev = float(bars["High"].iloc[i - 1])
        lo_prev = float(bars["Low"].iloc[i - 1])
        hi_next = float(bars["High"].iloc[i + 1])
        lo_next = float(bars["Low"].iloc[i + 1])
        if lo_next > hi_prev:
            fvgs.append({"kind": "bullish", "bottom": hi_prev, "top": lo_next,
                         "bar_time": bars.index[i], "bar_pos": i})
        elif hi_next < lo_prev:
            fvgs.append({"kind": "bearish", "bottom": hi_next, "top": lo_prev,
                         "bar_time": bars.index[i], "bar_pos": i})
    min_pos = n - 1 - 5
    return [f for f in fvgs if f["bar_pos"] >= min_pos]


def _build_meaningful_levels(liquidities: list, fvg_1hr: list) -> list:
    levels = []
    for liq in liquidities:
        name = liq.get("name")
        if liq.get("kind") != "level":
            continue
        if name == "week_high":
            levels.append({"price": float(liq["price"]), "kind": "high", "name": "week_high", "priority": 1})
        elif name == "week_low":
            levels.append({"price": float(liq["price"]), "kind": "low",  "name": "week_low",  "priority": 1})
        elif name == "day_high":
            levels.append({"price": float(liq["price"]), "kind": "high", "name": "day_high",  "priority": 2})
        elif name == "day_low":
            levels.append({"price": float(liq["price"]), "kind": "low",  "name": "day_low",   "priority": 2})
    for fvg in fvg_1hr:
        if fvg["kind"] == "bullish":
            levels.append({"price": fvg["bottom"], "kind": "low",  "name": "fvg_1hr_bull_bottom", "priority": 3})
        else:
            levels.append({"price": fvg["top"],    "kind": "high", "name": "fvg_1hr_bear_top",    "priority": 3})
    return sorted(levels, key=lambda lv: lv["priority"])


def _was_previously_touched(level: dict, prior_bars: pd.DataFrame) -> bool:
    if prior_bars.empty:
        return False
    if level["kind"] == "high":
        return bool((prior_bars["High"] >= level["price"]).any())
    return bool((prior_bars["Low"] <= level["price"]).any())


def _check_fresh_touch(current_bar: dict, prior_bars: pd.DataFrame, levels: list) -> dict | None:
    for level in levels:
        if level["kind"] == "high":
            touched_now = float(current_bar["High"]) >= level["price"]
        else:
            touched_now = float(current_bar["Low"]) <= level["price"]
        if not touched_now:
            continue
        if _was_previously_touched(level, prior_bars):
            continue
        direction = "down" if level["kind"] == "high" else "up"
        return {"direction": direction, "touched_level": level, "base_conf": 1.0}
    return None


def _co_evaluate_with_smt(direction: str, base_conf: float, smt_score: float) -> tuple:
    smt_sign = 1 if direction == "up" else -1
    aligned = (smt_sign * smt_score) > 0
    if aligned and abs(smt_score) >= 0.30:
        smt_alignment = "confirmed"
    elif not aligned and abs(smt_score) >= 0.60:
        smt_alignment = "contradicted"
    else:
        smt_alignment = "neutral"
    return base_conf, smt_alignment


def _check_approaching(
    current_bar: dict,
    prior_bars: pd.DataFrame,
    levels: list,
    mnq_1m: pd.DataFrame,
) -> dict | None:
    current_close = float(current_bar["Close"])
    for level in levels:
        if _was_previously_touched(level, prior_bars):
            continue
        dist = abs(level["price"] - current_close)
        if dist > LIQUIDITY_APPROACH_DIST:
            continue
        if level["kind"] == "high":
            approaching = current_close < level["price"]
        else:
            approaching = current_close > level["price"]
        if not approaching:
            continue
        recent = mnq_1m["Close"].iloc[-MOMENTUM_BARS:]
        if len(recent) < 2:
            continue
        if level["kind"] == "high":
            momentum_ok = float(recent.iloc[-1]) > float(recent.iloc[0])
        else:
            momentum_ok = float(recent.iloc[-1]) < float(recent.iloc[0])
        if not momentum_ok:
            continue
        direction = "up" if level["kind"] == "high" else "down"
        return {"direction": direction, "approaching_level": level, "dist": dist, "conf": 0.75}
    return None


def _compute_pd_score(
    close: float,
    week_h: float | None,
    week_l: float | None,
    day_h: float | None,
    day_l: float | None,
) -> float:
    if week_h is None or week_l is None or day_h is None or day_l is None:
        return 0.0
    week_mid = (week_h + week_l) / 2.0
    day_mid  = (day_h  + day_l)  / 2.0
    weekly_premium = close > week_mid
    daily_premium  = close > day_mid
    if weekly_premium and daily_premium:
        pd_score = -0.70
    elif not weekly_premium and not daily_premium:
        pd_score = +0.70
    elif weekly_premium and not daily_premium:
        pd_score = +0.30
    else:
        pd_score = -0.30
    if (day_h - close) < NEAR_EXTREME_DIST:
        pd_score -= 0.15
    if (close - day_l) < NEAR_EXTREME_DIST:
        pd_score += 0.15
    return max(-1.0, min(1.0, pd_score))


def _compute_bos_choch_score(bars_df: pd.DataFrame, swing_n: int, lookback: int) -> float:
    n = len(bars_df)
    if n < 2 * swing_n + 1:
        return 0.0
    highs  = bars_df["High"].values
    lows   = bars_df["Low"].values
    closes = bars_df["Close"].values
    shs = [i for i in range(swing_n, n - swing_n)
           if highs[i] == highs[i - swing_n: i + swing_n + 1].max()]
    sls = [i for i in range(swing_n, n - swing_n)
           if lows[i]  == lows[i  - swing_n: i + swing_n + 1].min()]
    last_idx = n - 1
    current_close = float(closes[-1])
    score = 0.0
    recent_shs = [i for i in shs if last_idx - i <= lookback]
    if recent_shs:
        latest_sh = max(recent_shs)
        if current_close > float(highs[latest_sh]):
            recency = 1.0 - (last_idx - latest_sh) / lookback
            score += recency
    recent_sls = [i for i in sls if last_idx - i <= lookback]
    if recent_sls:
        latest_sl = max(recent_sls)
        if current_close < float(lows[latest_sl]):
            recency = 1.0 - (last_idx - latest_sl) / lookback
            score -= recency
    return max(-1.0, min(1.0, score))


def _closest_level_name(price: float | None, liquidities: list) -> str | None:
    if price is None:
        return None
    best_name = None
    best_dist = float("inf")
    for liq in liquidities:
        if liq.get("kind") == "level":
            p = liq.get("price")
        elif liq.get("kind") == "fvg":
            # keep:True = fill-only yesterday-session FVG (SMT-fills universe); not actionable.
            if liq.get("keep"):
                continue
            top = liq.get("top")
            bottom = liq.get("bottom")
            if top is None or bottom is None:
                continue
            p = (top + bottom) / 2.0
        else:
            continue
        if p is None:
            continue
        d = abs(p - price)
        if d < best_dist:
            best_dist = d
            best_name = liq.get("name")
    return best_name if best_dist <= 10 else None


def _compute_smt_score(divs: list, liquidities: list) -> float:
    LEVEL_WEIGHT = {
        "week_high": 3, "week_low": 3,
        "day_high":  2, "day_low":  2,
        "ny_morning_high": 1, "ny_morning_low": 1,
        "london_high": 1,     "london_low": 1,
    }
    TF_WEIGHT   = {"30m": 2.0, "15m": 1.0}
    TYPE_WEIGHT = {"wick": 2.0, "wick_sym": 2.0, "body": 1.5, "body_sym": 1.5, "fill": 1.0}
    score = 0.0
    max_possible = 0.0
    for div in divs:
        level_name = _closest_level_name(div.get("mnq_div_price"), liquidities)
        lw = LEVEL_WEIGHT.get(level_name, 1) if level_name else 1
        tw = TF_WEIGHT.get(div.get("timeframe", ""), 1.0)
        yw = TYPE_WEIGHT.get(div.get("type", ""), 1.0)
        w  = lw * tw * yw
        sign = 1 if div.get("side") == "bullish" else -1
        score        += sign * w
        max_possible += w
    if max_possible == 0:
        return 0.0
    return max(-1.0, min(1.0, score / max_possible))


def _determine_direction(
    current_bar: dict,
    mnq_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    liquidities: list,
    global_state: dict,
    divs: list,
    now: "datetime",
    *,
    hist_1hr: "pd.DataFrame | None" = None,
    hist_4hr: "pd.DataFrame | None" = None,
) -> tuple:
    fvg_1hr = _detect_fvg_1hr(hist_mnq_1m, mnq_1m, hist_1hr=hist_1hr)
    levels  = _build_meaningful_levels(liquidities, fvg_1hr)
    prior   = mnq_1m.iloc[:-1]
    smt_sc  = _compute_smt_score(divs, liquidities)

    week_high = _named_price(liquidities, "week_high")
    week_low  = _named_price(liquidities, "week_low")
    day_high  = _named_price(liquidities, "day_high")
    day_low   = _named_price(liquidities, "day_low")
    current_close = float(current_bar["Close"])

    def _zone(close, hi, lo):
        if hi is None or lo is None:
            return "unknown"
        return "premium" if close > (hi + lo) / 2.0 else "discount"

    reason = {
        "rule":              None,
        "weekly_zone":       _zone(current_close, week_high, week_low),
        "daily_zone":        _zone(current_close, day_high,  day_low),
        "smt_score":         round(smt_sc, 3),
        "pd_score":          None,
        "bos_score_1hr":     None,
        "bos_score_4hr":     None,
        "rule3_score":       None,
        "combined_score":    None,
        "fresh_touch_level": None,
        "smt_alignment":     None,
        "approaching_level": None,
        "approaching_dist":  None,
        "last_swept_level":  None,
    }

    # Pre-session True Day bars (18:00 prior calendar day to session open).
    # Used by Rule 2b so overnight/London level touches are visible at NY open.
    _pre_session: pd.DataFrame | None = None
    if not mnq_1m.empty and not hist_mnq_1m.empty:
        _ts = mnq_1m.index[0]
        if _ts.tzinfo is None:
            _ts = _ts.tz_localize("America/New_York")
        else:
            _ts = _ts.tz_convert("America/New_York")
        _prior = _ts.date() - timedelta(days=1)
        _true_day_start = pd.Timestamp(
            datetime(_prior.year, _prior.month, _prior.day, 18, 0, 0),
            tz="America/New_York",
        )
        _pre_session = hist_mnq_1m[
            (hist_mnq_1m.index >= _true_day_start) & (hist_mnq_1m.index < _ts)
        ]

    # Rule 1: fresh sweep of a meaningful level — decisive state-change event.
    r1 = _check_fresh_touch(current_bar, prior, levels)
    if r1:
        _conf, smt_aln = _co_evaluate_with_smt(r1["direction"], r1["base_conf"], smt_sc)
        reason["rule"]              = "rule1"
        reason["fresh_touch_level"] = r1["touched_level"]["name"]
        reason["smt_alignment"]     = smt_aln
        return r1["direction"], reason

    # Rule 2b: last high-priority sweep + daily-mid position.
    # Scans the full True Day (pre-session + session) so overnight/London level touches are
    # visible at NY open.
    #
    # Same-side cases (last=low+below_mid, last=high+above_mid) use a two-layer check:
    #   Layer 1 (post-sweep): did price cross the mid after the sweep, then fail back?
    #     A failed attempt is the strongest signal — override everything else.
    #   Layer 2 (pre-sweep fallback): was there a committed directional mid-cross before
    #     the sweep, with no opposite-level revisit?  That marks a continuation sweep.
    #     If neither layer fires, treat as a liquidity grab → expect reversal.
    #
    #   last=low  + above mid                                                  => up  (bounce confirmed)
    #   last=low  + below mid + upward cross AFTER sweep (failed bullish)      => down
    #   last=low  + below mid + downward cross BEFORE sweep + high not hit     => down (continuation)
    #   last=low  + below mid + else                                           => up  (low grab → bounce)
    #   last=high + below mid                                                  => down (drop confirmed)
    #   last=high + above mid + downward cross AFTER sweep (failed bearish)    => up
    #   last=high + above mid + upward cross BEFORE sweep + low not hit        => up  (continuation)
    #   last=high + above mid + else                                           => down (high grab → drop)
    _low_names  = {"day_low", "week_low", "TDO", "TWO", "ny_morning_low"}
    _high_names = {"day_high", "week_high", "ny_morning_high"}
    _last_liq, _last_liq_ts = _find_last_liquidity(mnq_1m, liquidities, extra_bars=_pre_session)
    if _last_liq and day_high is not None and day_low is not None:
        _daily_mid = (day_high + day_low) / 2.0
        _above_mid = current_close > _daily_mid
        _liq_price_map = {l["name"]: float(l["price"]) for l in liquidities if l.get("kind") == "level"}

        # Gate: stale anchor — sweep is old AND price has structurally recovered from it.
        # Requiring both conditions prevents blocking trending days where price is simply
        # pulling back toward the sweep level (small recovery).
        # Pre-session touches use session open as the age reference so they're always fresh
        # at the session start regardless of when overnight they happened.
        _anchor_age_ok = True
        if _last_liq_ts is not None:
            _now_tz = pd.Timestamp(now)
            if _now_tz.tzinfo is None:
                _now_tz = _now_tz.tz_localize("America/New_York")
            _session_open = mnq_1m.index[0] if not mnq_1m.empty else _last_liq_ts
            _ref_ts = max(_last_liq_ts, _session_open)
            _age_h = (_now_tz - _ref_ts).total_seconds() / 3600.0
            _sweep_price = _liq_price_map.get(_last_liq) if _liq_price_map else None
            if _sweep_price is not None:
                _recovery = abs(current_close - _sweep_price)
                _anchor_age_ok = not (_age_h > RULE2B_ANCHOR_MAX_AGE_HOURS and _recovery > RULE2B_STALE_RECOVERY_PTS)
            else:
                _anchor_age_ok = _age_h <= RULE2B_ANCHOR_MAX_AGE_HOURS

        if _pre_session is not None and not _pre_session.empty:
            _true_day_bars = pd.concat([_pre_session, mnq_1m])
            _true_day_bars = _true_day_bars[~_true_day_bars.index.duplicated(keep="last")].sort_index()
        else:
            _true_day_bars = mnq_1m

        def _last_mid_cross_after(after_ts: "pd.Timestamp | None", upward: bool) -> "pd.Timestamp | None":
            _bars = _true_day_bars[_true_day_bars.index > after_ts] if after_ts is not None else _true_day_bars
            result = None
            for i in range(len(_bars)):
                if upward     and float(_bars["High"].iloc[i]) > _daily_mid:
                    result = _bars.index[i]
                if not upward and float(_bars["Low"].iloc[i])  < _daily_mid:
                    result = _bars.index[i]
            return result

        def _first_mid_cross_before(before_ts: "pd.Timestamp | None", upward: bool) -> "pd.Timestamp | None":
            _bars = _true_day_bars[_true_day_bars.index <= before_ts] if before_ts is not None else _true_day_bars
            for i in range(1, len(_bars)):
                c_now  = float(_bars["Close"].iloc[i])
                c_prev = float(_bars["Close"].iloc[i - 1])
                if upward     and c_now > _daily_mid and c_prev <= _daily_mid:
                    return _bars.index[i]
                if not upward and c_now < _daily_mid and c_prev >= _daily_mid:
                    return _bars.index[i]
            return None

        def _opp_level_touched(from_ts: "pd.Timestamp | None", to_ts: "pd.Timestamp | None",
                               names: set, check_high: bool) -> bool:
            _w = _true_day_bars
            if from_ts is not None:
                _w = _w[_w.index > from_ts]
            if to_ts is not None:
                _w = _w[_w.index <= to_ts]
            if _w.empty:
                return False
            for _n in names:
                _lp = _liq_price_map.get(_n)
                if _lp is None:
                    continue
                if check_high and (_w["High"] >= _lp).any():
                    return True
                if not check_high and (_w["Low"] <= _lp).any():
                    return True
            return False

        r2b_dir = None
        if _anchor_age_ok:
            if _last_liq in _low_names:
                if _above_mid:
                    r2b_dir = "up"
                else:
                    # Below daily mid after sweeping a low.
                    # In weekly discount: the sweep is an SSL accumulation event → reversal UP.
                    # Outside weekly discount: apply the existing layer checks.
                    if reason["weekly_zone"] == "discount":
                        r2b_dir = "up"
                    else:
                        # Layer 1: post-sweep upward cross that subsequently failed → bearish
                        _last_up_ts = _last_mid_cross_after(_last_liq_ts, upward=True)
                        if _last_up_ts is not None:
                            r2b_dir = "down"
                        else:
                            # Layer 2: pre-sweep committed bearish cross (continuation sweep)
                            _pre_cross_ts = _first_mid_cross_before(_last_liq_ts, upward=False)
                            if _pre_cross_ts is not None and not _opp_level_touched(
                                    _pre_cross_ts, _last_liq_ts, _high_names, check_high=True):
                                r2b_dir = "down"
                            else:
                                # Before assuming a bounce, check if a high level (incl.
                                # london_high) was swept in the True Day before the low grab.
                                # A prior high sweep means this is bearish continuation, not
                                # a reversal from discount.
                                _prior_high_names = {
                                    "day_high", "week_high", "ny_morning_high", "london_high"
                                }
                                _pre_low_bars = (
                                    _true_day_bars[_true_day_bars.index < _last_liq_ts]
                                    if _last_liq_ts is not None else pd.DataFrame()
                                )
                                _prior_high_swept = False
                                if len(_pre_low_bars) >= 2:
                                    _pc = _pre_low_bars["Close"].values
                                    _ph = _pre_low_bars["High"].values
                                    for _hn in _prior_high_names:
                                        _hlp = _liq_price_map.get(_hn)
                                        if _hlp is None:
                                            continue
                                        if ((_pc[:-1] < _hlp) & (_ph[1:] >= _hlp)).any():
                                            _prior_high_swept = True
                                            break
                                r2b_dir = "down" if _prior_high_swept else "up"
                    # Fix P2b: anchor price validation with 50-pt threshold.
                    # Suppress DOWN only when price is >50 pts above the swept low
                    # (the 5-20pt and 50+ pt overshoot zones are false positives;
                    # 20-50pt zone has 80% directional accuracy and is worth keeping).
                    if r2b_dir == "down":
                        _anchor_price_val = _liq_price_map.get(_last_liq)
                        if (_anchor_price_val is not None
                                and current_close > float(_anchor_price_val) + LOW_ARM_DOWN_OVERSHOOT_SUPPRESS_PTS):
                            r2b_dir = None
            elif _last_liq in _high_names:
                if not _above_mid:
                    r2b_dir = "down"
                else:
                    # Above daily mid after sweeping a high.
                    # BSL distribution reversal (→ DOWN) requires weekly premium PLUS the
                    # context is NOT one of the two false-positive patterns:
                    #
                    # False-positive A — ATH expansion: week_high == ATH and the swept level
                    # IS the week_high.  No prior sell-side structure exists above; this is
                    # genuine price discovery, not a stop hunt.  → UP (continuation).
                    #
                    # False-positive B — Morning sub-weekly high: before 13:00, sweeping a
                    # day_high or ny_morning_high while in weekly premium is ambiguous.  The
                    # NY morning (AMD accumulation/manipulation) often sweeps sub-weekly highs
                    # as part of the initial range expansion, not distribution.  → UP.
                    # Exception: the actual week_high is always a valid BSL signal regardless
                    # of time (week_high IS the structural BSL, not an intermediate level).
                    _wh_price = _liq_price_map.get("week_high")
                    _ath = global_state.get("all_time_high")
                    _wh_is_ath = (
                        _wh_price is not None
                        and _ath is not None
                        and abs(float(_wh_price) - float(_ath)) < 0.5
                    )
                    _now_tz = pd.Timestamp(now)
                    if _now_tz.tzinfo is None:
                        _now_tz = _now_tz.tz_localize("America/New_York")
                    _is_pm_kill_zone = _now_tz.hour >= 13
                    # False-positive A (extended): week_high IS the ATH this week →
                    # any high-level sweep is ATH expansion, not distribution. Covers
                    # both week_high sweeps AND sub-weekly (ny_morning_high, day_high)
                    # sweeps on ATH-expansion days where the week itself is making new ATH.
                    # Fix P1a: ATH guard is AM-only. PM sweeps of ATH-week highs are
                    # stop hunts, not genuine price discovery.
                    _is_false_pos_ath      = _wh_is_ath and (_now_tz.hour < P8_ATH_GUARD_HOUR)
                    _is_false_pos_morning  = (not _is_pm_kill_zone) and _last_liq != "week_high"
                    # False-positive C (recovery mode): current price is > 1.2% below the
                    # session-open ATH (session_ath is seeded at open and never updated
                    # intraday, unlike all_time_high which tracks the running bar high).
                    # Using current_close vs session_ath is stable — it doesn't drift as
                    # price makes new intraday highs (which would shrink a week_high-based
                    # gap and cause the guard to silently stop firing mid-session).
                    # Only applied in AM (before 13:00): PM kill-zone sweeps of highs in
                    # premium are valid BSL distribution signals even during a recovery week.
                    _session_ath_val = float(
                        global_state.get("session_ath") or _ath or 0
                    )
                    _recovery_gap = (
                        (_session_ath_val - current_close) / _session_ath_val
                        if _session_ath_val > current_close > 0
                        else 0.0
                    )
                    # PM kill zone applies a higher gap threshold (3%) because afternoon
                    # sweeps of highs in premium during genuine recovery weeks (gap > 3%)
                    # are still continuation moves, not distribution. The standard 2% AM
                    # threshold remains unchanged.
                    _recovery_threshold = 0.03 if _is_pm_kill_zone else 0.02
                    _is_false_pos_recovery = _recovery_gap > _recovery_threshold
                    if (reason["weekly_zone"] == "premium"
                            and not _is_false_pos_ath
                            and not _is_false_pos_morning
                            and not _is_false_pos_recovery):
                        r2b_dir = "down"
                    else:
                        r2b_dir = "up"
        if r2b_dir is not None:
            reason["rule"]             = "rule2b"
            reason["last_swept_level"] = _last_liq
            return r2b_dir, reason

    # Rule 2: trending toward an unvisited level with momentum — decisive continuation.
    r2 = _check_approaching(current_bar, prior, levels, mnq_1m)
    if r2:
        reason["rule"]              = "rule2"
        reason["approaching_level"] = r2["approaching_level"]["name"]
        reason["approaching_dist"]  = round(r2["dist"], 1)
        return r2["direction"], reason

    # Rules 3+4: multi-TF premium/discount bias + BOS/CHoCH + SMT scoring layer.
    pd_sc = _compute_pd_score(current_close, week_high, week_low, day_high, day_low)
    _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if hist_1hr is not None and hist_4hr is not None:
        _sess_1hr = mnq_1m.resample("1h").agg(_agg).dropna(subset=["Open"])
        mnq_1hr = pd.concat([hist_1hr, _sess_1hr])
        mnq_1hr = mnq_1hr[~mnq_1hr.index.duplicated(keep="last")].sort_index()
        _sess_4hr = mnq_1m.resample("4h").agg(_agg).dropna(subset=["Open"])
        mnq_4hr = pd.concat([hist_4hr, _sess_4hr])
        mnq_4hr = mnq_4hr[~mnq_4hr.index.duplicated(keep="last")].sort_index()
    else:
        combined_bars = pd.concat([hist_mnq_1m, mnq_1m])
        combined_bars = combined_bars[~combined_bars.index.duplicated(keep="last")].sort_index()
        mnq_1hr = combined_bars.resample("1h").agg(_agg).dropna(subset=["Open"])
        mnq_4hr = combined_bars.resample("4h").agg(_agg).dropna(subset=["Open"])
    b1hr = _compute_bos_choch_score(mnq_1hr, BOS_SWING_N, BOS_LOOKBACK_1HR)
    b4hr = _compute_bos_choch_score(mnq_4hr, BOS_SWING_N, BOS_LOOKBACK_4HR)
    bos_sc   = 0.35 * b1hr + 0.65 * b4hr
    r3_sc    = 0.55 * pd_sc + 0.45 * bos_sc
    combined = 0.65 * r3_sc + 0.35 * smt_sc

    reason["pd_score"]       = round(pd_sc,    3)
    reason["bos_score_1hr"]  = round(b1hr,     3)
    reason["bos_score_4hr"]  = round(b4hr,     3)
    reason["rule3_score"]    = round(r3_sc,    3)
    reason["combined_score"] = round(combined, 3)

    if abs(combined) >= DIRECTION_SCORE_THRESHOLD:
        reason["rule"] = "rule3_4"
        return ("up" if combined > 0 else "down"), reason

    # Rule 5: global trend fallback when no rule commits.
    reason["rule"] = "rule5_trend"
    return global_state.get("trend", "up"), reason


def compute_live_hl_mid(
    combined_1m: pd.DataFrame,
    now: "pd.Timestamp",
) -> dict:
    """Compute live day / week high, low, mid from bar data.

    Returns a dict containing day_high, day_low, day_mid, week_high, week_low, week_mid.
    Only keys where sufficient bar data exists are included.

    Parameters
    ----------
    combined_1m : deduplicated, sorted 1m bars covering the current futures day and week
    now         : current tz-aware ET timestamp
    """
    today  = now.date()
    result: dict = {}

    # Day: range from the current CME futures session open (18:00 ET) or earlier.
    # After 18:00 ET the new session opened today; before 18:00 ET it opened yesterday.
    # The opening 1.5h (18:00–19:30 ET) is skipped from whichever side (high or low) it
    # distorts, but only when its range is >2x the range of the rest of the day up to the
    # RTH open. Direction determines which side is the outlier: a big UP move creates an
    # outlier high (skip from day_high); a big DOWN move creates an outlier low (skip from
    # day_low). When the 1.5h move is modest relative to the rest of the day it is included
    # in both.
    #
    # Early sessions extend the lookback ~2 sessions back for extra context; later sessions
    # use only the current CME session. The window starts on the session-open day at:
    #   Asia   (now.hour >= 18): 06:00 ET — prior NY morning open  (2 sessions back from Asia)
    #   London (now.hour < 6):   12:00 ET — prior NY evening open   (2 sessions back from London)
    #   NY morning+ (6..18):     18:00 ET — current CME session open
    # London no longer reaches the prior NY morning (06:00 ET); both early sessions now look
    # exactly 2 sessions back from their own start.
    _day_open_cal = today if now.hour >= 18 else today - timedelta(days=1)
    if now.hour >= 18:
        _day_start_hour = 6     # Asia   → prior NY morning open
    elif now.hour < 6:
        _day_start_hour = 12    # London → prior NY evening open
    else:
        _day_start_hour = 18    # NY morning+ → current CME session open
    _day_start = pd.Timestamp(
        datetime(_day_open_cal.year, _day_open_cal.month, _day_open_cal.day, _day_start_hour, 0, 0),
        tz="America/New_York",
    )
    # CME Asia session open is always 18:00 ET — outlier check covers the first 1.5h
    # regardless of whether _day_start extends further back to 06:00 ET.
    _asia_open   = pd.Timestamp(
        datetime(_day_open_cal.year, _day_open_cal.month, _day_open_cal.day, 18, 0, 0),
        tz="America/New_York",
    )
    _init_end    = pd.Timestamp(
        datetime(_day_open_cal.year, _day_open_cal.month, _day_open_cal.day, 19, 30, 0),
        tz="America/New_York",
    )
    _rth_start   = pd.Timestamp(
        datetime(today.year, today.month, today.day, 9, 30, 0),
        tz="America/New_York",
    )

    _init_bars = combined_1m[(combined_1m.index >= _asia_open) & (combined_1m.index < _init_end)]
    _rest_bars = combined_1m[(combined_1m.index >= _init_end) & (combined_1m.index < _rth_start)]
    _full_bars = combined_1m[combined_1m.index >= _day_start]

    if not _full_bars.empty:
        skip_init_from_high = False
        skip_init_from_low  = False

        if not _init_bars.empty and not _rest_bars.empty:
            init_range = float(_init_bars["High"].max() - _init_bars["Low"].min())
            rest_range = float(_rest_bars["High"].max() - _rest_bars["Low"].min())
            if rest_range > 0 and init_range > 2 * rest_range:
                init_open  = float(_init_bars.iloc[0]["Open"])
                init_close = float(_init_bars.iloc[-1]["Close"])
                if init_close > init_open:   # big UP move → outlier high
                    skip_init_from_high = True
                else:                        # big DOWN move → outlier low
                    skip_init_from_low = True

        _high_start = _init_end if skip_init_from_high else _day_start
        _low_start  = _init_end if skip_init_from_low  else _day_start

        _high_bars = combined_1m[combined_1m.index >= _high_start]
        _low_bars  = combined_1m[combined_1m.index >= _low_start]

        dh = float(_high_bars["High"].max())
        dl = float(_low_bars["Low"].min())
        result["day_high"] = dh
        result["day_low"]  = dl
        result["day_mid"]  = (dh + dl) / 2.0

    # Week H/L start — extended lookback on Mon/Tue sessions, standard from Wed+.
    # Session-open day (ET) drives the choice: Sunday = Monday session, Monday = Tuesday, etc.
    # Monday session  → prev Thursday 18:00 ET (capture Sun/Mon pre-week range)
    # Tuesday session → prev Friday  18:00 ET  (capture Mon Asia range)
    # Wednesday+      → Sunday 18:00 ET (standard CME week open)
    _session_open_wd = _day_open_cal.weekday()  # Mon=0, Tue=1, ..., Sun=6
    if _session_open_wd == 6:  # Sunday → Monday session
        _week_anchor = _day_open_cal - timedelta(days=3)   # prev Thursday
    elif _session_open_wd == 0:  # Monday → Tuesday session
        _week_anchor = _day_open_cal - timedelta(days=3)   # prev Friday
    else:
        _days_to_sunday = (_session_open_wd + 1) % 7
        _week_anchor = _day_open_cal - timedelta(days=_days_to_sunday)
    _week_start = pd.Timestamp(
        datetime(_week_anchor.year, _week_anchor.month, _week_anchor.day, 18, 0, 0),
        tz="America/New_York",
    )
    _week_bars = combined_1m[combined_1m.index >= _week_start]
    if not _week_bars.empty:
        wh = float(_week_bars["High"].max())
        wl = float(_week_bars["Low"].min())
        result["week_high"] = wh
        result["week_low"]  = wl
        result["week_mid"]  = (wh + wl) / 2.0

    return result


def build_hypothesis_from_direction(
    direction: str,
    now,
    current_close: float,
    liquidities: list,
    global_state: dict,
    old_direction: str,
    weekly_mid: str,
    daily_mid: str,
    last_liquidity: str,
    divs: list,
    direction_reason: dict,
    *,
    hist_mnq_1m: "pd.DataFrame | None" = None,
    is_fresh_start: bool = False,
    skip_veto: bool = False,
    skip_position_reset: bool = False,
    old_formed_at: str = "",
) -> list:
    """Steps 7-11 of the hypothesis pipeline, callable standalone for manual entry.

    When hist_mnq_1m is None, entry_ranges is left empty (manual entries bypass O5).
    When skip_veto is True, the direction-veto check is skipped entirely.
    """
    # Step 7: targets — filter liquidities in direction from current close.
    targets = []
    for liq in liquidities:
        kind = liq.get("kind")
        if kind == "level":
            price = liq.get("price")
            if price is None:
                continue
            if direction == "up" and price > current_close:
                targets.append({"name": liq["name"], "price": price})
            elif direction == "down" and price < current_close:
                targets.append({"name": liq["name"], "price": price})
        elif kind == "fvg":
            # keep:True = fill-only yesterday-session FVG (SMT-fills universe); not a target.
            if liq.get("keep"):
                continue
            top = liq.get("top")
            bottom = liq.get("bottom")
            if top is None or bottom is None:
                continue
            if direction == "up" and bottom > current_close:
                targets.append({"name": liq["name"], "price": bottom})
            elif direction == "down" and top < current_close:
                targets.append({"name": liq["name"], "price": top})

    # Step 8: two-tier cautious prices. The per-hypothesis cautious_dist_shrinks counter
    # tightens the max-distance thresholds after each failed entry under this hypothesis.
    ath = global_state["all_time_high"]
    _dist_shrinks = load_position().get("cautious_dist_shrinks", 0)
    _cp = compute_cautious_prices(direction, current_close, liquidities, ath, _dist_shrinks)
    cautious_price_initial         = _cp["cautious_price_initial"]
    cautious_price_initial_level   = _cp["cautious_price_initial_level"]
    cautious_price_secondary       = _cp["cautious_price_secondary"]
    cautious_price_secondary_level = _cp["cautious_price_secondary_level"]

    # Step 8b: veto direction when entry conditions are unfavourable.
    if not skip_veto and direction != "none":
        sec_dist = (abs(float(cautious_price_secondary) - current_close)
                    if cautious_price_secondary != "" else 0)
        if not is_fresh_start and cautious_price_secondary != "" and sec_dist < CAUTIOUS_MIN_DIST:
            direction = "none"
        elif direction == "up" and current_close >= ath:
            direction = "none"
        elif not targets:
            direction = "none"

    # Step 9: entry_ranges — 12hr ago and 1week ago same time anchors.
    # None → entry_ranges = [] (manual entry: position already being placed, O5 bypassed).
    entry_ranges = []
    if hist_mnq_1m is not None:
        ts_now = pd.Timestamp(now)
        bar_12hr  = _find_nearest_bar(hist_mnq_1m, ts_now - pd.Timedelta(hours=12))
        bar_1week = _find_nearest_bar(hist_mnq_1m, ts_now - pd.Timedelta(weeks=1))
        if bar_12hr is not None:
            entry_ranges.append({"source": "12hr",  "low": bar_12hr["Low"],  "high": bar_12hr["High"]})
        if bar_1week is not None:
            entry_ranges.append({"source": "1week", "low": bar_1week["Low"], "high": bar_1week["High"]})

    # Write hypothesis.json
    if direction != old_direction:
        formed_at = pd.Timestamp(now).isoformat()
    else:
        formed_at = old_formed_at or pd.Timestamp(now).isoformat()

    new_hypothesis = {
        "direction":                      direction,
        "formed_at":                      formed_at,
        "weekly_mid":                     weekly_mid,
        "daily_mid":                      daily_mid,
        "last_liquidity":                 last_liquidity,
        "divs":                           divs,
        "targets":                        targets,
        "cautious_price":                 "",
        "cautious_price_initial":         cautious_price_initial,
        "cautious_price_initial_level":   cautious_price_initial_level,
        "cautious_price_secondary":       cautious_price_secondary,
        "cautious_price_secondary_level": cautious_price_secondary_level,
        "entry_ranges":                   entry_ranges,
    }
    save_hypothesis(new_hypothesis)

    # Step 10: On none -> up/down transition, reset position state.
    # skip_position_reset=True is passed by the pipeline when it temporarily cleared
    # direction to "none" for an unbiased level-swept re-evaluation.  In that case the
    # transition is artificial: failed_entries still resets (level sweep is a fresh
    # context) but stop_entry and conf_bar_entry are preserved so a pending stop entry
    # that was set before the sweep survives.
    if old_direction == "none" and direction != "none":
        if skip_position_reset:
            position = load_position()
            position["failed_entries"] = 0
            position["cautious_dist_shrinks"] = 0
            save_position(position)
        else:
            _strategy.reset_position_for_new_hypothesis()

    hyp_event = {
        "kind":          "new-hypothesis",
        "time":          pd.Timestamp(now).isoformat(),
        "direction":     direction,
        "price":         current_close,
        "weekly_mid":    weekly_mid,
        "daily_mid":     daily_mid,
        "last_liquidity": last_liquidity,
        "targets":       targets,
        "cautious_price_initial":         cautious_price_initial,
        "cautious_price_initial_level":   cautious_price_initial_level,
        "cautious_price_secondary":       cautious_price_secondary,
        "cautious_price_secondary_level": cautious_price_secondary_level,
        "entry_ranges":                   entry_ranges,
        "direction_reason":               direction_reason,
    }
    # smt-div SIGNAL emission moved to the SMT V2 per-1m detector
    # (session_pipeline._run_smt_v2_detection). `divs` are still computed and consumed
    # internally here (smt_score / direction rules) and embedded in hyp_event above —
    # they are simply no longer emitted as separate smt-div events for logging/plotting.
    if direction == "none":
        return []
    return [hyp_event]


def run_hypothesis(
    now: datetime,
    mnq_1m: pd.DataFrame,
    mes_1m: pd.DataFrame,
    hist_mnq_1m: pd.DataFrame,
    hist_mes_1m: pd.DataFrame,
    *,
    hist_1hr: "pd.DataFrame | None" = None,
    hist_4hr: "pd.DataFrame | None" = None,
    skip_position_reset: bool = False,
) -> list:
    """Run the hypothesis module for the current 5m boundary.

    Reads hypothesis.json; if direction is already set, returns early.
    Otherwise computes all hypothesis fields and writes hypothesis.json.
    Also handles position reset on direction transition from "none".

    Returns a list of smt-div event dicts found this bar (empty list if none).
    """
    # Step 1: Read hypothesis.json; early-exit if direction already set.
    hypothesis = load_hypothesis()
    old_direction = hypothesis["direction"]
    if old_direction != "none":
        return []

    # Fresh session start: DEFAULT_HYPOTHESIS has no formed_at. After trend-broken clears
    # direction to "none", formed_at remains from today — so absence of formed_at (or a
    # formed_at from a prior day) means this is the first hypothesis of the session.
    _formed_at_str = hypothesis.get("formed_at", "")
    _is_fresh_start = True
    if _formed_at_str:
        try:
            _formed_ts = pd.Timestamp(_formed_at_str)
            _now_pd = pd.Timestamp(now)
            if _now_pd.tzinfo is None:
                _now_pd = _now_pd.tz_localize("America/New_York")
            else:
                _now_pd = _now_pd.tz_convert("America/New_York")
            if _formed_ts.tzinfo is None:
                _formed_ts = _formed_ts.tz_localize("America/New_York")
            else:
                _formed_ts = _formed_ts.tz_convert("America/New_York")
            _is_fresh_start = _formed_ts.date() != _now_pd.date()
        except Exception:
            pass

    # Step 2: Read global.json ATH; build current 5m bar; check ATH gate.
    global_state = load_global()
    all_time_high = global_state["all_time_high"]

    bar = _build_5m_bar(mnq_1m, now)
    if bar is None:
        return []

    if bar["Low"] > all_time_high and bar["High"] > all_time_high:
        return []  # Both extremes above ATH — no entry opportunity

    current_close = bar["Close"]

    _ts_now = pd.Timestamp(now)
    if _ts_now.tzinfo is None:
        _ts_now = _ts_now.tz_localize("America/New_York")
    else:
        _ts_now = _ts_now.tz_convert("America/New_York")

    # Step 3: Compute weekly_mid and daily_mid.
    daily = load_daily()
    liquidities = daily.get("liquidities", [])

    week_high_price = None
    week_low_price = None
    day_high_price = None
    day_low_price = None

    for liq in liquidities:
        name = liq.get("name")
        if name == "week_high" and liq.get("kind") == "level":
            week_high_price = liq["price"]
        elif name == "week_low" and liq.get("kind") == "level":
            week_low_price = liq["price"]
        elif name == "day_high" and liq.get("kind") == "level":
            day_high_price = liq["price"]
        elif name == "day_low" and liq.get("kind") == "level":
            day_low_price = liq["price"]

    weekly_mid = ""
    if week_high_price is not None and week_low_price is not None:
        weekly_mid = compute_mid_label(current_close, week_high_price, week_low_price)

    daily_mid = ""
    if day_high_price is not None and day_low_price is not None:
        daily_mid = compute_mid_label(current_close, day_high_price, day_low_price)

    # Step 4: last_liquidity — most recently touched meaningful level (True Day scope).
    _prior_cal = _ts_now.date() - timedelta(days=1)
    _true_day_start_hyp = pd.Timestamp(
        datetime(_prior_cal.year, _prior_cal.month, _prior_cal.day, 18, 0, 0),
        tz="America/New_York",
    )
    _hyp_pre_session = hist_mnq_1m[
        (hist_mnq_1m.index >= _true_day_start_hyp)
        & (hist_mnq_1m.index < (mnq_1m.index[0] if not mnq_1m.empty else _ts_now))
    ]
    last_liquidity, _ = _find_last_liquidity(mnq_1m, liquidities, extra_bars=_hyp_pre_session)

    # Step 5: divs — SMT divergences at 15m and 30m.
    # Before NY morning (09:30 ET), extend the bar window back to the prior NY
    # evening session (12:00–18:00 ET on the session-open day) so that SMTs
    # against yesterday's afternoon lows/highs are detectable during Asia and
    # London. From 09:30 ET onward the current session window is long enough
    # that extending back to yesterday adds noise without value.
    _now_et = now.astimezone(_ET) if getattr(now, "tzinfo", None) else now
    _pre_ny_morning = _now_et.hour < 9 or (_now_et.hour == 9 and _now_et.minute < 30)
    if _pre_ny_morning and not hist_mnq_1m.empty and not mnq_1m.empty:
        _sess_open   = pd.Timestamp(_cme_session_start(now))
        _ny_eve_start = _sess_open - pd.Timedelta(hours=6)  # 12:00 ET on session-open day
        _prior_mnq = hist_mnq_1m[
            (hist_mnq_1m.index >= _ny_eve_start) & (hist_mnq_1m.index < _sess_open)
        ]
        _prior_mes = hist_mes_1m[
            (hist_mes_1m.index >= _ny_eve_start) & (hist_mes_1m.index < _sess_open)
        ] if not hist_mes_1m.empty else pd.DataFrame()
        if not _prior_mnq.empty:
            _div_mnq = pd.concat([_prior_mnq, mnq_1m]).sort_index()
            _div_mnq = _div_mnq[~_div_mnq.index.duplicated(keep="last")]
            _div_mes = pd.concat([_prior_mes, mes_1m]).sort_index() if not _prior_mes.empty else mes_1m
            _div_mes = _div_mes[~_div_mes.index.duplicated(keep="last")]
            divs = _compute_divs(_div_mnq, _div_mes)
        else:
            divs = _compute_divs(mnq_1m, mes_1m)
    else:
        divs = _compute_divs(mnq_1m, mes_1m)

    # Step 6: direction — determined by ICT rules (see direction.md).
    # confidence=high overrides all rules: direction follows the global trend unconditionally.
    if global_state.get("confidence") == "high":
        direction = global_state.get("trend", "up")
        direction_reason = {"rule": "global_confidence_high"}
    else:
        direction, direction_reason = _determine_direction(
            current_bar  = bar,
            mnq_1m       = mnq_1m,
            hist_mnq_1m  = hist_mnq_1m,
            liquidities  = liquidities,
            global_state = global_state,
            divs         = divs,
            now          = now,
            hist_1hr     = hist_1hr,
            hist_4hr     = hist_4hr,
        )

    return build_hypothesis_from_direction(
        direction, now, current_close, liquidities, global_state,
        old_direction, weekly_mid, daily_mid, last_liquidity, divs, direction_reason,
        hist_mnq_1m=hist_mnq_1m,
        is_fresh_start=_is_fresh_start,
        skip_position_reset=skip_position_reset,
        old_formed_at=hypothesis.get("formed_at", ""),
    )
