# smt_conviction.py
# GIL-32 — standing SMT conviction (Phase 1: standalone, ungated).
#
# A *pure* module that maintains a "standing SMT conviction" set — built SEPARATELY
# from the legacy relevance-filtered `smt_active_set` / `_compute_smt_score_v2` /
# `_co_evaluate_with_smt` confidence path (all of which are left UNTOUCHED, per GIL-32)
# — and scores it into a single signed conviction in [-1, +1] (bearish negative, bullish
# positive). The score is consumed by `hypothesis._determine_direction` to optionally flip
# rule2b's direction at a trend start.
#
# Lifecycle persistence fix vs. `smt_detect`'s relevance behavior (GIL-32 component 1):
#   - decayed RESIDUAL after fulfillment (a fulfilled SMT keeps a linearly-decaying voice
#     for CONVICTION_RESIDUAL_MIN minutes instead of vanishing the instant it fulfills),
#   - a small BIRTH GRACE (no adverse-run drop within CONVICTION_GRACE_MIN of the fire), and
#   - SUSTAIN on adverse-run drop (CONVICTION_SUSTAIN consecutive adverse closes required to
#     drop, vs. detect_state's single-close invalidation).
# Adverse-run thresholds REUSE smt_detect.INVALIDATE_PTS — NOT widened (GIL-32 constraint).
#
# Everything here is pure python (JSON-serializable in/out, no IO, never raises). The two
# entry points are total functions: degenerate input → safe defaults.

from __future__ import annotations

import datetime
from typing import Any

import smt_detect

# ---------------------------------------------------------------------------
# Tunable module constants (documented together — GIL-32 §"Tunable module constants").
# ---------------------------------------------------------------------------
CONVICTION_STRONG = 0.5        # |conviction| >= this is required to flip rule2b's direction.
CONVICTION_RESIDUAL_MIN = 180  # minutes a *fulfilled* SMT keeps a linearly-decayed residual.
CONVICTION_GRACE_MIN = 5       # no adverse-run drop within this many min of fire (the sweep).
CONVICTION_SUSTAIN = 2         # adverse-run drop requires this many consecutive adverse closes.

# Tier weights — reuse the live SMT-V2 tier authority so conviction weighting matches the
# rest of the engine (ATH/week 3, day 2, fill 1.5, session 1).
_TIER_WEIGHT = {"ATH": 3.0, "week": 3.0, "day": 2.0, "fill": 1.5, "session": 1.0}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_iso(iso: "str | None") -> "datetime.datetime | None":
    """Best-effort ISO-8601 → datetime. None on failure. Total."""
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return None


def _minutes_between(start_iso: "str | None", now_iso: "str | None") -> "float | None":
    """Minutes from start_iso → now_iso (>= 0 if now after start). None if either unparseable."""
    a = _parse_iso(start_iso)
    b = _parse_iso(now_iso)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def _tier_of(ref_name: "str | None", rec_kind: "str | None", explicit_tier: "str | None") -> str:
    """Tier for a record: explicit tier wins (e.g. ATH carried on the div); fills → 'fill';
    else classify the level name via smt_detect._level_class. Total → 'session' fallback."""
    if explicit_tier:
        return explicit_tier
    if rec_kind == "fill":
        return "fill"
    try:
        return smt_detect._level_class(ref_name or "")[1]
    except Exception:
        return "session"


def _tier_weight(tier: "str | None") -> float:
    return _TIER_WEIGHT.get(tier or "", 1.0)


def _logical_key(rec: dict) -> tuple:
    """Collapse key — one logical SMT per (ref_name, direction), mirroring ingest_active_set."""
    return (rec.get("ref_name"), rec.get("direction"))


def _confirm_strength(rec_type: "str | None") -> int:
    """wick supersedes body; fills/other sit between (mirror ingest_smts._confirm_strength)."""
    if rec_type == "wick":
        return 2
    if rec_type == "body":
        return 0
    return 1


def _detect_key(ref_name: "str | None", direction: "str | None", rec_type: "str | None") -> str:
    """The smt_detect.detect_state key for a record — mirrors smt_detect._record_key:
    level SMTs (wick/body) → ``ref_name|direction|type``; fills → the bare ref_name."""
    if rec_type in ("wick", "body"):
        return f"{ref_name}|{direction}|{rec_type}"
    return str(ref_name)


def _collapsed_status(keys: "list[str] | None", status_map: dict) -> str:
    """Aggregate status over a record's underlying detect keys (mirror
    hypothesis.collapsed_relevance precedence): fulfilled if ANY fulfilled; else gone if
    ALL present statuses are gone; else unfulfilled.

    A key absent from ``status_map`` defaults to ``"unfulfilled"`` (NOT gone) — the map is
    populated by ``smt_detect.smt_status`` which already returns the explicit ``"gone"`` for
    detect-state-absent keys; an un-queried key must not silently drop the record (this
    mirrors the prior single-key ``sm.get(det_key, "unfulfilled")`` semantics)."""
    ks = [k for k in (keys or []) if k]
    if not ks:
        return "unfulfilled"
    sts = [status_map.get(k, "unfulfilled") for k in ks]
    if any(s == "fulfilled" for s in sts):
        return "fulfilled"
    if all(s == "gone" for s in sts):
        return "gone"
    return "unfulfilled"


# ---------------------------------------------------------------------------
# update_standing — maintain the standing conviction set
# ---------------------------------------------------------------------------
def update_standing(
    prev: "list[dict] | None",
    new_divs: "list[dict] | None",
    status_map: "dict | None",
    mnq_close: float,
    now_iso: str,
) -> list[dict]:
    """Advance the standing conviction set one bar.

    Each standing record:
      {ref_name, direction, side, tier, type, fire_iso, fire_close,
       adverse_streak, fulfilled_iso|None}

    Steps (in order):
      1. Add each new div, collapsing by ``(ref_name, direction)`` — wick supersedes body,
         then newer ``time`` wins (mirrors ``ingest_active_set`` collapse semantics).
      2. Per record, advance lifecycle against this bar:
         - ``gone`` in ``status_map`` → drop.
         - newly ``fulfilled`` (and ``fulfilled_iso`` is None) → stamp it (residual starts).
         - DROP a fulfilled record once ``now - fulfilled_iso > CONVICTION_RESIDUAL_MIN``.
         - Adverse-run (own, looser than detect_state): for a ``short``,
           ``adverse = mnq_close >= fire_close + INVALIDATE_PTS[tier]`` (reuse smt_detect
           thresholds; for a ``long``, ``mnq_close <= fire_close - INVALIDATE_PTS[tier]``).
           Within ``CONVICTION_GRACE_MIN`` of ``fire_iso`` → never adverse (streak forced 0).
           Increment ``adverse_streak`` on adverse closes, reset to 0 otherwise;
           DROP when ``adverse_streak >= CONVICTION_SUSTAIN``.

    Returns a NEW list (does not mutate ``prev``). Total: None inputs → safe; never raises.
    """
    try:
        sm = status_map if isinstance(status_map, dict) else {}

        # --- Index existing standing records by logical key ----------------------------
        by_key: dict = {}
        order: list = []
        for r in (prev or []):
            if not isinstance(r, dict):
                continue
            lk = _logical_key(r)
            if lk not in by_key:
                order.append(lk)
            by_key[lk] = dict(r)

        # --- (1) Add new divs (collapse by (ref_name, direction)) -----------------------
        for raw in (new_divs or []):
            if not isinstance(raw, dict):
                continue
            ref_name = raw.get("ref_name")
            direction = raw.get("direction")
            if not ref_name or not direction:
                continue
            rec_type = raw.get("type")
            tier = _tier_of(ref_name, raw.get("kind"), raw.get("tier"))
            fire_iso = raw.get("time") or now_iso
            fire_close = raw.get("mnq_price")
            try:
                fire_close = float(fire_close) if fire_close is not None else float(mnq_close)
            except (TypeError, ValueError):
                fire_close = float(mnq_close)
            new_rec = {
                "ref_name": ref_name,
                "direction": direction,
                "side": raw.get("side") or ("bearish" if direction == "short" else "bullish"),
                "tier": tier,
                "type": rec_type,
                "fire_iso": fire_iso,
                "fire_close": fire_close,
                "adverse_streak": 0,
                "fulfilled_iso": None,
                # Union of detect keys folded into this logical record (wick+body), so the
                # lifecycle status can aggregate over BOTH variants (mirror ingest_smts).
                "keys": [_detect_key(ref_name, direction, rec_type)],
            }
            lk = _logical_key(new_rec)
            existing = by_key.get(lk)
            if existing is None:
                by_key[lk] = new_rec
                order.append(lk)
            else:
                # wick supersedes body; equal strength → newer time wins (>=).
                ns = _confirm_strength(rec_type)
                os_ = _confirm_strength(existing.get("type"))
                supersede = ns > os_
                if ns == os_:
                    nm = _minutes_between(existing.get("fire_iso"), fire_iso)
                    supersede = nm is None or nm >= 0
                # Merge the folded detect-key union onto whichever record survives.
                merged_keys: list = list(existing.get("keys") or [])
                for k in new_rec["keys"]:
                    if k not in merged_keys:
                        merged_keys.append(k)
                survivor = new_rec if supersede else existing
                survivor["keys"] = merged_keys
                by_key[lk] = survivor

        # --- (2) Per-record lifecycle advance against this bar --------------------------
        try:
            mc = float(mnq_close)
        except (TypeError, ValueError):
            mc = None

        kept_order: list = []
        for lk in order:
            rec = by_key.get(lk)
            if rec is None:
                continue
            direction = rec.get("direction")
            tier = rec.get("tier")
            # Status over detect_state, aggregated over the record's folded detect keys
            # (wick+body union). Back-compat: a record lacking `keys` (e.g. seeded/legacy)
            # reconstructs its single detect key the same way smt_detect does.
            det_keys = rec.get("keys")
            if not det_keys:
                det_keys = [_detect_key(rec.get("ref_name"), direction, rec.get("type"))]
            status = _collapsed_status(det_keys, sm)

            # gone → drop.
            if status == "gone":
                continue

            # newly fulfilled → stamp residual start.
            if status == "fulfilled" and rec.get("fulfilled_iso") is None:
                rec["fulfilled_iso"] = now_iso

            # DROP a fulfilled record once its residual window has fully elapsed.
            if rec.get("fulfilled_iso") is not None:
                age = _minutes_between(rec.get("fulfilled_iso"), now_iso)
                if age is not None and age > CONVICTION_RESIDUAL_MIN:
                    continue

            # Adverse-run drop (own grace+sustain; reuse INVALIDATE_PTS — not widened).
            if mc is not None:
                grace = _minutes_between(rec.get("fire_iso"), now_iso)
                in_grace = grace is not None and grace <= CONVICTION_GRACE_MIN
                try:
                    fc = float(rec.get("fire_close"))
                except (TypeError, ValueError):
                    fc = None
                adverse = False
                if fc is not None and not in_grace:
                    inv = smt_detect._invalidate_pts(tier, "mnq")
                    if direction == "short":
                        adverse = mc >= fc + inv
                    else:  # long
                        adverse = mc <= fc - inv
                if in_grace:
                    rec["adverse_streak"] = 0
                elif adverse:
                    rec["adverse_streak"] = int(rec.get("adverse_streak") or 0) + 1
                else:
                    rec["adverse_streak"] = 0
                if int(rec.get("adverse_streak") or 0) >= CONVICTION_SUSTAIN:
                    continue

            kept_order.append(lk)

        return [by_key[lk] for lk in kept_order]
    except Exception:
        # Total: on any unexpected error, preserve the prior set unchanged (fail-safe).
        return list(prev or [])


# ---------------------------------------------------------------------------
# conviction_score — collapse + signed tier×residual weighting → [-1, 1]
# ---------------------------------------------------------------------------
def conviction_score(standing: "list[dict] | None", now_iso: str) -> "tuple[float, dict]":
    """Signed conviction in [-1, +1] over the standing set, with event-logging inputs.

    Per record: weight = ``tier_weight × residual_factor`` where
    ``residual_factor = 1.0`` if not fulfilled else ``max(0, 1 - age/RESIDUAL_MIN)``
    (``age`` = minutes since ``fulfilled_iso``). Signed: ``short → −``, ``long → +``.
    Collapsed by ``(ref_name, direction)`` (defensive — update_standing already collapses).
    ``score = sum(signed_weight) / sum(|weight|)`` clamped to ``[-1, 1]``; empty → 0.0.

    Returns ``(score, inputs)`` where ``inputs`` =
    ``{n, n_bear, n_bull, top_tier, refs}`` for event logging. Total; never raises.
    """
    empty = (0.0, {"n": 0, "n_bear": 0, "n_bull": 0, "top_tier": None, "refs": []})
    try:
        recs = standing or []
        if not recs:
            return empty

        # Defensive collapse by (ref_name, direction) — keep one record per logical key.
        by_key: dict = {}
        for r in recs:
            if not isinstance(r, dict):
                continue
            by_key[_logical_key(r)] = r
        collapsed = list(by_key.values())
        if not collapsed:
            return empty

        signed_sum = 0.0
        abs_sum = 0.0
        n_bear = 0
        n_bull = 0
        refs: list = []
        top_tier = None
        top_rank = -1
        _rank = {"ATH": 4, "week": 4, "day": 3, "fill": 2, "session": 1}

        for r in collapsed:
            tier = r.get("tier")
            w = _tier_weight(tier)
            # residual factor: full voice while unfulfilled; linearly decayed once fulfilled.
            residual_factor = 1.0
            if r.get("fulfilled_iso") is not None:
                age = _minutes_between(r.get("fulfilled_iso"), now_iso)
                if age is None:
                    residual_factor = 1.0
                else:
                    residual_factor = max(0.0, 1.0 - (age / float(CONVICTION_RESIDUAL_MIN)))
            weight = w * residual_factor
            if weight <= 0.0:
                continue
            direction = r.get("direction")
            side = r.get("side")
            sign = 1.0 if (direction == "long" or side == "bullish") else -1.0
            signed_sum += sign * weight
            abs_sum += abs(weight)
            if sign < 0:
                n_bear += 1
            else:
                n_bull += 1
            refs.append(r.get("ref_name"))
            rk = _rank.get(tier or "", 0)
            if rk > top_rank:
                top_rank = rk
                top_tier = tier

        if abs_sum == 0.0:
            return empty
        score = max(-1.0, min(1.0, signed_sum / abs_sum))
        inputs = {
            "n": n_bear + n_bull,
            "n_bear": n_bear,
            "n_bull": n_bull,
            "top_tier": top_tier,
            "refs": refs,
        }
        return (score, inputs)
    except Exception:
        return empty
