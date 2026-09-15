"""Deterministic validator for the v2 thesis / trade-plan contracts (spec §2, §6).

Three layers, mirroring the v1 `agent/validator.py` shape:

  1. Syntactic  — enums, required fields per form, closed mechanism kinds, mandatory
                  `on_dol_falsified` on a SETUP, WAIT requires a recall, every predicate
                  in the closed vocabulary.
  2. Semantic   — every level referenced (dol, target, predicates) exists in the facts;
                  the entry target is unswept/undepleted and on the correct side of price
                  (carries the v1 TARGET CONTRACT).
  3. Cross-level — the stop must NOT satisfy any thesis `falsified_if` predicate, and the

Public API mirrors `validator.py`: `validate_thesis`, `validate_trade_plan`, and a
combined `validate_contracts`. All return a `ContractValidation` (list of violations).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from schemas import (
    BIASES, CONFIDENCES, DAILY_REGIMES, DIRECTIONS, DOL_FALSIFIED_ACTIONS,
    DOL_PROJECTION_LEVELS, ENTRY_MECHANISMS, EVIDENCE_ASSETS, EVIDENCE_CRITERIA,
    EVIDENCE_DIRECTIONS, EVIDENCE_TFS, EVIDENCE_TIERS, MGMT_MECHANISMS, VERDICTS,
    Thesis, TradePlan,
)
from predicates import (
    MarketView, eval_any, referenced_levels, validate_predicate_list,
)

_EPS = 1e-6


@dataclass
class ContractViolation:
    layer: str            # "syntactic" | "semantic" | "cross_level"
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f" ({self.where})" if self.where else ""
        return f"[{self.code}]{loc} {self.message}"


@dataclass
class ContractValidation:
    violations: list = field(default_factory=list)
    # plan 15 Task 7: audit-only warnings — surfaced for the record, NEVER affect `ok`
    # (not a retry trigger). Kept separate from violations so nothing downstream that gates
    # on `ok`/`codes()` changes behavior.
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> set:
        return {v.code for v in self.violations}

    def messages(self) -> list:
        return [str(v) for v in self.violations]

    def warning_messages(self) -> list:
        return [str(v) for v in self.warnings]

    def add(self, layer: str, code: str, message: str, where: str = "") -> None:
        self.violations.append(ContractViolation(layer, code, message, where))

    def warn(self, code: str, message: str, where: str = "") -> None:
        self.warnings.append(ContractViolation("audit", code, message, where))


def _enum(value, allowed, where, code, r: ContractValidation) -> None:
    if value is None or value not in allowed:
        r.add("syntactic", code, f"{value!r} not in {sorted(allowed)}", where)


# --------------------------------------------------------------------------- #
# Thesis                                                                       #
# --------------------------------------------------------------------------- #
def validate_thesis(thesis, facts: Optional[dict] = None) -> ContractValidation:
    r = ContractValidation()
    d = thesis.to_dict() if isinstance(thesis, Thesis) else (thesis or {})
    t = Thesis.from_dict(d)

    _enum(t.bias, BIASES, "thesis.bias", "SYN_BAD_BIAS", r)
    _enum(t.regime, DAILY_REGIMES, "thesis.regime", "SYN_BAD_REGIME", r)
    _enum(t.confidence, CONFIDENCES, "thesis.confidence", "SYN_BAD_CONFIDENCE", r)

    # A directional thesis requires a DOL (day draw-on-liquidity) with a level + price.
    if t.is_directional():
        if not isinstance(t.dol, dict) or not t.dol.get("level") \
                or not isinstance(t.dol.get("price"), (int, float)):
            r.add("syntactic", "SYN_DIRECTIONAL_MISSING_DOL",
                  "directional thesis requires dol.level and dol.price", "thesis.dol")

    for msg in validate_predicate_list(t.falsified_if, "thesis.falsified_if"):
        r.add("syntactic", "SYN_BAD_PREDICATE", msg, "thesis.falsified_if")

    # recall: when present, its events must be a valid predicate list.
    if t.recall is not None:
        for msg in validate_predicate_list((t.recall or {}).get("events"),
                                           "thesis.recall.events"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, "thesis.recall.events")

    now_ts = (facts or {}).get("now")
    for i, item in enumerate(t.evidence or []):
        _validate_evidence_item(item, f"thesis.evidence[{i}]", r, now_ts=now_ts)

    # SEM_EVIDENCE_DIRECTION_MISMATCH (2026-08-02 root-cause audit): a P1/P2 item's
    # mature/direction claim is not taken on faith — it is cross-checked against the SAME
    # code-computed HTF-close verdict already used to render S9's per-level ACCEPTED/
    # REJECTED text, exposed here via facts["level_htf_close_status"] as {asset: {level:
    # {tf: Optional[bool]}}} (None = never swept / no qualifying close yet, True = accept,
    # False = reject). Closes the 2026-07-15 prev1_week_high fabrication class: a level
    # NEVER swept by either asset has every tf entry None, directly contradicting a
    # declared mature=True — previously this passed validation clean as long as the
    # (fabricated) net score happened to match the declared bias.
    level_htf_close_status = (facts or {}).get("level_htf_close_status")
    # thesis.md §10 (2026-08-05): a partial-bar reversal (htf_reversal) changes what the
    # "correct" declared direction actually is for a P1 item — a 'reverse' tier means the
    # auto-injected (or a correctly-declared) item's direction is the OPPOSITE of the
    # completed bar's own raw close, and an 'omit' tier means the item scores nothing
    # regardless of direction. Without this, a model (or the auto-injection itself)
    # correctly reflecting the reversal was flagged as a "mismatch" against the stale,
    # pre-reversal fact — a real bug found the same day this mechanism shipped (2026-07-22
    # 09:20 ET: MNQ london(cur)_low correctly declared 'reject' post-reversal, rejected on
    # every retry against the raw 'accept').
    htf_reversal = (facts or {}).get("htf_reversal")
    if level_htf_close_status:
        for i, item in enumerate(t.evidence or []):
            if not isinstance(item, dict) or item.get("criterion") not in ("P1", "P2"):
                continue
            if not bool(item.get("mature")):
                continue
            asset, level, tf = item.get("asset"), item.get("level"), item.get("tf")
            rev_tier = ((htf_reversal or {}).get(asset) or {}).get(level, {}).get(tf, "none")
            if rev_tier == "omit":
                continue          # scores nothing either way -- direction is moot
            actual = ((level_htf_close_status.get(asset) or {}).get(level) or {}).get(tf)
            if actual is None:
                r.add("semantic", "SEM_EVIDENCE_DIRECTION_MISMATCH",
                      f"{asset} {level} [{tf}] declared mature=True but the facts show no "
                      "qualifying HTF close since a sweep (never swept, or no close yet) "
                      "— this item cannot be mature", f"thesis.evidence[{i}]")
                continue
            actual_direction = "accept" if actual else "reject"
            if rev_tier == "reverse":
                actual_direction = "reject" if actual_direction == "accept" else "accept"
            if item.get("direction") != actual_direction:
                r.add("semantic", "SEM_EVIDENCE_DIRECTION_MISMATCH",
                      f"{asset} {level} [{tf}] declared direction={item.get('direction')!r} "
                      f"but the facts show it was actually {actual_direction!r}ed"
                      f"{' (post partial-bar reversal)' if rev_tier == 'reverse' else ''}",
                      f"thesis.evidence[{i}]")

    # ARI_THESIS_BIAS (§4/§9): the declared bias must match the sign of the code-computed
    # net score over the EFFECTIVE evidence ledger (declared + code-injected P3 mid
    # reads — see score_thesis_evidence's P3 auto-derivation) — the SAME retry-not-
    # override pattern as daily_trend/next_move's direction-vs-N check (agent/validator.py
    # _check_arithmetic). A thin/empty effective ledger is exempt ONLY for a NEUTRAL bias
    # (the legitimate failsafe/no-evidence-found case) — a declared UP/DOWN backed by
    # nothing, not even an auto-derived mid read, is rejected outright below, not silently
    # waved through: a directional call resting on zero structured evidence bypasses the
    # entire "model judges, code computes" sandwich regardless of how much free-text
    # reasoning cites facts that were never transcribed into `evidence` (2026-07-27 09:20
    # ET case: rich reasoning, bias UP, evidence == []).
    # Only build dol_available when facts actually carry a computed S8 menu — a facts
    # dict with no "menus" key at all (older/minimal test fixtures) means "we have no
    # information", which must default to available=True (score_thesis_evidence's own
    # no-`dol_available` default), NOT "both sides unavailable".
    menus = (facts or {}).get("menus")
    dol_available = None
    if menus is not None:
        dol_menu = menus.get("dol") or {}
        dol_available = {"UP": bool(dol_menu.get("UP")), "DOWN": bool(dol_menu.get("DOWN"))}
    suppressed_p1_levels = (facts or {}).get("suppressed_p1_levels")
    suppressed_p2_sites = (facts or {}).get("suppressed_p2_sites")
    level_tiers = (facts or {}).get("level_tiers")
    smt_candidates = (facts or {}).get("smt_candidates")
    week_extremes = (facts or {}).get("week_extremes")
    now_price = (facts or {}).get("now_price")
    fvg_zone_meta = (facts or {}).get("fvg_zone_meta")
    mid_reclaim = (facts or {}).get("mid_reclaim")
    htf_reversal = (facts or {}).get("htf_reversal")
    # 2026-08-15 (2026-08-10 09:20 ET audit): unconditional-position P3 + P1 equilibrium-
    # staleness hard gate — absent keys (older facts dicts) leave scoring unchanged.
    mid_position = (facts or {}).get("mid_position")
    p1_stale_levels = (facts or {}).get("p1_stale_levels")
    # 2026-08-05 fix: this re-check MUST use the SAME clearance-magnitude weighting the
    # real scoring path (_derive_thesis_arithmetic) applies -- an unweighted x1.0 re-check
    # can disagree with the true, magnitude-weighted net score closely enough to flip which
    # side of a tie the declared bias falls on (2026-07-20 09:20 ET: true net score was an
    # exact 0.0 tie -- NEUTRAL -- but an unweighted re-check computed +0.5 UP and waved a
    # declared UP bias through clean). `evidence_magnitude` ({asset: {level: {tf: ratio}}},
    # bench/facts.py) is the JSON-safe nested-dict form of build_evidence_magnitude's own
    # tuple-keyed dict; reconstructed here to the (asset, level, tf) key shape
    # score_thesis_evidence expects.
    _mag_nested = (facts or {}).get("evidence_magnitude")
    magnitude = None
    if _mag_nested:
        magnitude = {
            (_asset, _level, _tf): _ratio
            for _asset, _levels in _mag_nested.items()
            for _level, _tfs in (_levels or {}).items()
            for _tf, _ratio in (_tfs or {}).items()
        }
    scoring = score_thesis_evidence(t.evidence, magnitude=magnitude, dol_available=dol_available,
                                    suppressed_p1_levels=suppressed_p1_levels,
                                    suppressed_p2_sites=suppressed_p2_sites,
                                    level_htf_close_status=level_htf_close_status,
                                    level_tiers=level_tiers, smt_candidates=smt_candidates,
                                    week_extremes=week_extremes, now_price=now_price,
                                    fvg_zone_meta=fvg_zone_meta, mid_reclaim=mid_reclaim,
                                    htf_reversal=htf_reversal, mid_position=mid_position,
                                    p1_stale_levels=p1_stale_levels)
    # PLAN 37: a deterministic-override thesis is EXEMPT from the bias/net-score check.
    #
    # It carries no declared evidence by construction -- the override exists to decide
    # direction WITHOUT the ledger, the scoring or the weights -- so the check has nothing
    # of its own to check, and the code-injected P3 mid reads it would be judged against
    # are exactly what the override is overriding. 2026-09-01 is the worked case: its mid
    # reads alone score -12.125 (expected DOWN), which is the read the override contradicts
    # on purpose.
    #
    # Stated as an EXPLICIT exemption rather than relied on implicitly. Today `validate_thesis`
    # is only ever reached through `decide_thesis`, which the override bypasses, so an
    # override thesis is never validated anyway -- but that is an accident of where the call
    # sits. A future refactor that validated inside the Analyzer would otherwise start
    # rejecting every override silently.
    if str((thesis or {}).get("thesis_source") or "") == "stretch_override":
        pass
    elif scoring["scored_evidence"]:
        if t.bias in BIASES and t.bias != scoring["expected_bias"]:
            # Near-tie NEUTRAL dead-band (2026-08-17, plan-18 mini-diff finding): a
            # razor-thin net (2026-07-15 09:20 ET: +0.75 from a single mid row) must not
            # FORCE a directional call — the model answered NEUTRAL / DOWN / DOWN across
            # three attempts against a demanded 'UP' and died to the failsafe, on a day
            # that fell 374 pts. Declared NEUTRAL is accepted whenever |net| sits inside
            # ARI_NEUTRAL_BAND; a declared OPPOSITE direction is still rejected, and a
            # thin net still never rejects the sign-matching direction (expected_bias is
            # unchanged — this only widens what counts as consistent).
            if not (t.bias == "NEUTRAL"
                    and abs(scoring["net_score"]) < ARI_NEUTRAL_BAND):
                r.add("arithmetic", "ARI_THESIS_BIAS",
                      f"bias '{t.bias}' inconsistent with the evidence ledger's net score "
                      f"{scoring['net_score']} (expected '{scoring['expected_bias']}'"
                      f"; NEUTRAL is acceptable when |net| < {ARI_NEUTRAL_BAND})",
                      "thesis.bias")
    elif t.bias in ("UP", "DOWN"):
        r.add("arithmetic", "ARI_THESIS_BIAS",
              f"bias '{t.bias}' declared with an EMPTY evidence ledger — a directional call "
              "needs at least one declared P1-P5 item (or an auto-derived P3 mid read) "
              "backing it; declare NEUTRAL if you truly found no evidence, do not leave "
              "the ledger empty under a directional bias",
              "thesis.bias")

    if facts is not None:
        _semantic_thesis(t, facts, r)
    return r


def _validate_evidence_item(item, where: str, r: ContractValidation, now_ts=None) -> None:
    if not isinstance(item, dict):
        r.add("syntactic", "SYN_BAD_EVIDENCE_ITEM",
              f"evidence item must be an object, got {type(item).__name__}", where)
        return
    checks = (
        ("criterion", EVIDENCE_CRITERIA), ("asset", EVIDENCE_ASSETS),
        ("tier", EVIDENCE_TIERS), ("tf", EVIDENCE_TFS),
        ("direction", EVIDENCE_DIRECTIONS),
    )
    for field_name, allowed in checks:
        if item.get(field_name) not in allowed:
            r.add("syntactic", "SYN_BAD_EVIDENCE_ITEM",
                  f"{field_name} {item.get(field_name)!r} not in {sorted(allowed)}", where)
    if not isinstance(item.get("level"), str) or not item.get("level"):
        r.add("syntactic", "SYN_BAD_EVIDENCE_ITEM",
              "'level' must be a non-empty string", where)
    if not isinstance(item.get("mature"), bool):
        r.add("syntactic", "SYN_BAD_EVIDENCE_ITEM",
              "'mature' must be a boolean", where)
    _validate_pending_resolution(item, where, r, now_ts)


def _validate_pending_resolution(item: dict, where: str, r: ContractValidation, now_ts) -> None:
    """plan 15 Task 7 — AUDIT-ONLY structural check on an item's optional pending_resolution.
    A resolves_at that is not strictly after `now` is a warning (the resolution has, by its own
    claim, already passed — the model likely copied a stale timestamp), NOT a hard rejection —
    pending_resolution is informational and never scored. `now` absent (older fixtures) skips
    the time check."""
    pr = item.get("pending_resolution")
    if pr is None:
        return
    if not isinstance(pr, dict):
        r.warn("AUD_PENDING_RESOLUTION_SHAPE", "pending_resolution must be an object", where)
        return
    resolves_at = pr.get("resolves_at")
    if resolves_at is None or now_ts is None:
        return
    try:
        import pandas as pd
        if pd.Timestamp(resolves_at) <= pd.Timestamp(now_ts):
            r.warn("AUD_PENDING_RESOLUTION_STALE",
                   f"pending_resolution.resolves_at {resolves_at!r} is not after now "
                   f"{now_ts!r} (resolution already passed?)", where)
    except (ValueError, TypeError):
        r.warn("AUD_PENDING_RESOLUTION_SHAPE",
               f"pending_resolution.resolves_at {resolves_at!r} is not a parseable timestamp",
               where)


def _semantic_thesis(t: Thesis, facts: dict, r: ContractValidation) -> None:
    levels = facts.get("levels") or {}
    names = set(_iter_predicate_levels(t.falsified_if))
    if t.recall:
        names |= set(_iter_predicate_levels((t.recall or {}).get("events") or []))
    if isinstance(t.dol, dict) and t.dol.get("level"):
        # 2026-08-16 DOL-menu refit: the synthetic projection draws are menu-offered but
        # (deliberately) not facts.levels entries — exempt them here, DOL slot only.
        if t.dol["level"] not in DOL_PROJECTION_LEVELS:
            names.add(t.dol["level"])
    for item in t.evidence or []:
        # P3/P4/P5 reference a synthetic equilibrium mid or an FVG-zone id, not a named price
        # level in facts.levels — their level string is validated by the criterion-specific
        # sign derivers (_mid_side/_fvg_side), so exempt them from SEM_LEVEL_NOT_IN_FACTS
        # (plan 15 Tasks 4/6).
        if isinstance(item, dict) and item.get("criterion") in ("P3", "P4", "P5"):
            continue
        if isinstance(item, dict) and isinstance(item.get("level"), str) and item["level"]:
            names.add(item["level"])
    for name in sorted(names):
        if name not in levels:
            r.add("semantic", "SEM_LEVEL_NOT_IN_FACTS",
                  f"level '{name}' referenced by the thesis is not present in the facts",
                  "thesis")

    # XL_FALSIFIED_IF_ALREADY_TRUE: a thesis whose own
    # falsified_if already evaluates true against the CURRENT price is
    # self-invalidating (or self-completing) at issuance — the 2026-07-02 08:00 bug, where
    # falsified_if was anchored 5pts from a price already on the wrong side of it and fired
    # 10 minutes later regardless of what the market actually did. Reuses the same
    # MarketView.price_only() hypothetical the trade-plan cross-level check already uses for
    # "does the stop trip thesis falsification" (predicates.MarketView.price_only), just
    # applied one level earlier, against the thesis's own current price.
    now_price = facts.get("now_price")
    if isinstance(now_price, (int, float)):
        view = MarketView.price_only(float(now_price))
        if t.falsified_if and eval_any(t.falsified_if, view):
            r.add("cross_level", "XL_FALSIFIED_IF_ALREADY_TRUE",
                  f"falsified_if would already be satisfied at the current price {now_price} "
                  "(thesis self-invalidates at issuance)", "thesis.falsified_if")

    # SEM_DOL_WRONG_SIDE (plan 12 Fix 2): a directional thesis's DOL (draw-on-liquidity) must
    # sit on the bias side of the current price — an UP thesis draws to a pool ABOVE price, a
    # DOWN thesis to a pool BELOW price. A DOL on the wrong side is already reached (or being
    # reached against the bias), so the exhaustion (`price_beyond(DOL)`) is trivially/instantly
    # satisfiable — a bogus "completion". This hard-rejects the class regardless of menu
    # correctness (belt-and-suspenders to the menu's proximity guard).
    now_price = facts.get("now_price")
    dol_price = (t.dol or {}).get("price") if isinstance(t.dol, dict) else None
    if t.is_directional() and isinstance(now_price, (int, float)) \
            and isinstance(dol_price, (int, float)):
        if t.bias == "UP" and dol_price < now_price - _EPS:
            r.add("semantic", "SEM_DOL_WRONG_SIDE",
                  f"UP thesis but DOL {dol_price} is below current price {now_price} "
                  "(a draw must sit above price)", "thesis.dol")
        elif t.bias == "DOWN" and dol_price > now_price + _EPS:
            r.add("semantic", "SEM_DOL_WRONG_SIDE",
                  f"DOWN thesis but DOL {dol_price} is above current price {now_price} "
                  "(a draw must sit below price)", "thesis.dol")

    # SEM_FALSIFIER_WRONG_SIDE (2026-08-17, plan-18 mini-diff finding): a directional
    # thesis's falsified_if must describe ANTI-thesis price action — an UP thesis is
    # falsified by price/closes going BELOW something, never ABOVE (mirrored for DOWN).
    # A thesis-side falsifier fires on the thesis SUCCEEDING (2026-08-05 09:20 ET: an UP
    # thesis carried `price_beyond(29990.75, above)` — an old near pool recycled as a
    # "falsifier" — so the walk-forward "falsified" it 12 minutes before its genuine DOL
    # touch). Mirrors SEM_DOL_WRONG_SIDE. (Exhaustion was removed 2026-08-29 — its
    # DOL-touch term is thesis-side by design.
    if t.is_directional():
        _own_side = "above" if t.bias == "UP" else "below"

        def _walk_preds(preds):
            for _p in preds or []:
                if not isinstance(_p, dict):
                    continue
                if _p.get("type") in ("all_of", "any_of"):
                    yield from _walk_preds(_p.get("of"))
                else:
                    yield _p

        for _p in _walk_preds(t.falsified_if):
            if _p.get("type") in ("price_beyond", "n_closes_beyond") \
                    and _p.get("side") == _own_side:
                r.add("semantic", "SEM_FALSIFIER_WRONG_SIDE",
                      f"{t.bias} thesis with a falsifier on its OWN side "
                      f"({_p.get('type')} side={_p.get('side')} price={_p.get('price')}) "
                      "— a falsifier must describe anti-thesis price action",
                      "thesis.falsified_if")

    # AUD_DOL_FAR_FOR_REGIME (2026-08-16 DOL-menu refit): a RANGE-regime call drawing to a
    # FAR pool (beyond DOL_BAND_MAX_RATIO x avg_1h — the menu's own band tag) is a
    # tier/regime mismatch — a range read has no business targeting a multi-day moonshot
    # (2026-08-10 09:20 ET: RANGE call, DOL 4.8x away, L2 lost -59.5 while the near-band
    # counterfactual won). Audit-only warning, same non-gating status as the other AUD_
    # codes — FAR stays legitimate for a TREND call with HTF confirmation.
    if t.is_directional() and isinstance(t.dol, dict) and t.regime == "RANGE":
        _menu_rows = (((facts.get("menus") or {}).get("dol") or {}).get(t.bias)) or []
        for _e in _menu_rows:
            if _e.get("level") == t.dol.get("level"):
                if _e.get("band") == "FAR":
                    r.warn("AUD_DOL_FAR_FOR_REGIME",
                           f"RANGE regime drawing to a FAR pool "
                           f"({_e.get('level')} at {_e.get('dist_ratio')}x avg_1h) — "
                           "prefer a BAND draw or reconsider the regime", "thesis.dol")
                break


# --------------------------------------------------------------------------- #
# Evidence-ledger scoring (decisions/thesis.md §2.1/§4/§6) — the model judges,      #
# code computes. Self-contained: recomputes from t.evidence alone (no dependency   #
# on run_agent.py having run first), the same way agent/validator.py's             #
# _check_arithmetic independently recomputes S/N rather than trusting the block.   #
# --------------------------------------------------------------------------- #
_TF_MULT = {"1h": 1.0, "4h": 1.5}          # thesis.md §4: 4hr scores more than 1hr
_TIER_MULT = {"session": 0.5, "day": 0.75, "week": 1.0}   # §4: week > day > session
_BASE_POINTS = 2.0                          # v1 seed, pending calibration (thesis.md §4)

# thesis.md §2.1a (2026-08-02): P3 (daily_mid/weekly_mid position) is a static snapshot,
# not a confirmation event whose strength should scale with how long a bar took to close
# — so it gets its OWN fixed point value per tier instead of the shared tf_mult x
# tier_mult formula every other criterion uses (a 4h reading isn't "worth more" than a 1h
# one here, just possibly more current). v1 seed, pending calibration, same status as
# every other constant in this module.
_P3_MID_POINTS = {"day": 1.0, "week": 1.5}

# Near-tie NEUTRAL dead-band (2026-08-17): |net_score| below this permits a declared
# NEUTRAL through ARI_THESIS_BIAS (see validate_thesis) — one WEAK session read or a
# lone mid row is not enough evidence to FORCE a direction. v1 seed, pending
# calibration, same status as the confidence-ceiling thresholds (2.0/4.0).
ARI_NEUTRAL_BAND = 1.0


def _is_week_confluent(item: dict, level_tiers, week_extremes) -> bool:
    """thesis.md §2.1e promotion (2026-08-02, scoped narrowly): True when a day-tier P1/P2
    item's OWN price sits within 5% of the week's range of THIS ASSET's current week high/
    low — a day-tier level that is ALSO the week's own extreme, not merely a day-scale
    event. Confirmed real on 2026-07-15 (MNQ prev2_day_high sat ~20pts from the week's own
    high, a tight cluster; prev1_day_high, ~150pts away, did NOT cluster). Shared by the
    scoring loop's tier_mult override AND the dominance pre-passes below, so both agree on
    which items are "effectively week-tier" — deliberately narrower than derive_facts.
    _confluence_notes (audit-only, matches against OLD untracked extremes only)."""
    if item.get("tier") != "day" or not level_tiers:
        return False
    price = ((level_tiers.get(item.get("asset")) or {}).get(item.get("level")) or {}).get("price")
    wk = (week_extremes or {}).get(item.get("asset")) or {}
    whi, wlo = wk.get("hi"), wk.get("lo")
    if not (isinstance(price, (int, float)) and isinstance(whi, (int, float))
            and isinstance(wlo, (int, float)) and whi > wlo):
        return False
    tol = 0.05 * (whi - wlo)
    return abs(price - whi) <= tol or abs(price - wlo) <= tol


def _dominance_distance(item: dict, level_tiers, now_price) -> float:
    """2026-08-02: how far this item's OWN level sits from `now_price` — the general,
    comparable "how extreme/significant is this level" metric used to resolve same-asset
    dominance conflicts (thesis.md §6 extension). Distance-from-price generalizes across
    high/low families (unlike comparing raw price directly, which only makes sense within
    one family) and across level types. Returns 0.0 (never wins a tie) when now_price or
    the level's own price aren't available. Motivating case (2026-07-15 09:20 ET): MNQ's
    own prev1_day_high (accept, UP) and prev2_day_high (P2 reject, DOWN) disagreed on
    which side should dominate MES's contradicting P3 — prev2_day_high is genuinely the
    more extreme (further) level and should win, not whichever happened to be iterated
    first."""
    if not isinstance(now_price, (int, float)) or not level_tiers:
        return 0.0
    price = ((level_tiers.get(item.get("asset")) or {}).get(item.get("level")) or {}).get("price")
    if not isinstance(price, (int, float)):
        return 0.0
    return abs(price - now_price)

# thesis.md §4: clearance magnitude — |close - level| / avg_range[tf]. v1 seeds pending calibration.
# weak (<0.5x) x0.75, normal (0.5-1.5x) x1.0, strong (>1.5x) x1.25 — a shallow poke past a level
# scores less than a decisive clearance ("model judges which level/accept, code computes how far").
_MAG_BUCKETS = ((0.5, 0.75), (1.5, 1.0), (float("inf"), 1.25))   # (ratio_upper, multiplier)


def _magnitude_mult(ratio):
    if ratio is None:
        return 1.0            # no magnitude info (P2/immature/absent) -> neutral, unchanged
    for upper, mult in _MAG_BUCKETS:
        if ratio < upper:
            return mult
    return 1.0


_MAG_MULT_LABELS = {0.75: "WEAK", 1.0: "NORMAL", 1.25: "STRONG"}   # keyed off _MAG_BUCKETS' own multipliers


def magnitude_label(ratio) -> Optional[str]:
    """WEAK/NORMAL/STRONG for the same ratio _magnitude_mult scores — derived from
    _magnitude_mult's own multiplier (not a separate threshold copy, so it can never drift
    from the actual scoring buckets). Rendered in S9 (derive_facts.render_evidence_text) so
    the model can SEE which HTF closes will score more/less before declaring its own bias,
    instead of the multiplier being an invisible factor it has no way to anticipate
    (thesis.md §4/§9 evidence-ledger prompt note)."""
    if ratio is None:
        return None
    return _MAG_MULT_LABELS.get(_magnitude_mult(ratio))


def _level_polarity(name) -> Optional[str]:
    """'high' | 'low' | None from the level NAME's naming convention (prev1_day_high,
    asia(cur)_low, week_high, TDO, ...) — the level's own high/low identity is stable
    regardless of where price currently sits, unlike a level's transient 'side' (above/
    below price) which flips once the level is swept. Used to mechanically derive each
    evidence item's UP/DOWN sign so the model cannot mis-classify it (thesis.md §2.1 P1's
    own accept/reject rule; the 2026-07-02 08:00 bug labeled a rejected LOW sweep
    'bearish' when rejecting a low sweep is bullish)."""
    lname = (name or "").lower()
    if "high" in lname:
        return "high"
    if "low" in lname:
        return "low"
    return None


def _evidence_side(item: dict) -> Optional[str]:
    """UP or DOWN — mechanically derived from level polarity + accept/reject, never
    model-declared. accept-beyond continues the sweep's own direction; reject-before
    reverses it (thesis.md §2.1 P1)."""
    polarity = _level_polarity(item.get("level"))
    direction = item.get("direction")
    if polarity is None or direction not in EVIDENCE_DIRECTIONS:
        return None
    if polarity == "high":
        return "UP" if direction == "accept" else "DOWN"
    return "DOWN" if direction == "accept" else "UP"


def _fvg_side(item: dict) -> Optional[str]:
    """UP or DOWN for a P5 (FVG-fill) item — mechanically derived from the zone's bull/bear
    kind (parsed from the copied S6 identifier in `level`) plus accept/reject, never
    model-declared (plan 15 Task 4 / thesis.md §2.1 P5). A bull zone is polarity-'high', a
    bear zone polarity-'low', so the SAME accept/reject rule as _evidence_side applies:
    a bull zone that HELD (accept) is bullish continuation (UP), a violated one (reject) is
    DOWN; a bear zone that held (accept) is DOWN, a violated one (reject) is UP."""
    lname = (item.get("level") or "").lower()
    if "bull" in lname:
        polarity = "high"
    elif "bear" in lname:
        polarity = "low"
    else:
        return None
    direction = item.get("direction")
    if direction not in EVIDENCE_DIRECTIONS:
        return None
    if polarity == "high":
        return "UP" if direction == "accept" else "DOWN"
    return "DOWN" if direction == "accept" else "UP"


def _mid_side(item: dict) -> Optional[str]:
    """UP or DOWN for a P3 (equilibrium position) item — code-derived, never model-declared
    (plan 15 Task 6 / thesis.md §2.1 P3). P3 has no accept/reject-of-a-sweep; the model
    declares only WHERE price sits relative to the daily/weekly mid, mapped onto the
    accept/reject field: `accept` = price accepted ABOVE the mid (bullish lean -> UP),
    `reject` = price sits/closed BELOW the mid (bearish lean -> DOWN). Deliberately simpler
    than P1/P4: a position read, not a two-step reclaim."""
    direction = item.get("direction")
    if direction == "accept":
        return "UP"
    if direction == "reject":
        return "DOWN"
    return None


def _item_side(item: dict) -> Optional[str]:
    """Dispatch an evidence item to its criterion-specific sign deriver — all code-derived,
    never model-declared. P5 -> FVG bull/bear kind; P3 -> above/below equilibrium; P4 ->
    the reclaim direction encoded in the _high/_low mid name (reclaim IS an accept-beyond-
    then-hold pattern, so _evidence_side's polarity logic applies); P1/P2 -> level high/low
    polarity + accept/reject."""
    criterion = item.get("criterion")
    if criterion == "P5":
        return _fvg_side(item)
    if criterion == "P3":
        return _mid_side(item)
    return _evidence_side(item)


def score_thesis_evidence(evidence: list, magnitude=None, dol_available=None,
                           suppressed_p1_levels=None, suppressed_p2_sites=None,
                           level_htf_close_status=None, level_tiers=None,
                           smt_candidates=None, week_extremes=None,
                           now_price=None, fvg_zone_meta=None,
                           mid_reclaim=None, htf_reversal=None,
                           mid_position=None, p1_stale_levels=None,
                           recross_distance=None, p2_discount_fire_ratio=None) -> dict:
    """Pure computation over the model-declared P1/P2 evidence ledger: per-item points
    (tier x tf x magnitude multiplier, zeroed if immature — enforcing the §3 maturity gate
    in code, not trust), the net score, the expected bias sign, and the §6 cross-asset
    contradiction cap on confidence. Returns {net_score, expected_bias, contradiction,
    confidence_ceiling, scored_evidence, no_liquidity}. `scored_evidence` echoes each item
    with its computed `points`/`side`/`mag_ratio`/`mag_mult` attached, for the audit trail.

    tf-dedup (2026-08-01): when the SAME (criterion, asset, level, tier, direction) is
    declared at both `tf: "1h"` and `tf: "4h"` and BOTH are mature/non-exhausted, only the
    4h item scores — the 1h is zeroed, same "zeroed, not scored" mechanic as the other
    suppression gates below. Needs no caller input; computed internally from the declared
    list, like P4-dominates-P3. An item with only a mature 1h (4h still immature) is
    unaffected.

    `magnitude` (plan 14 Task 5) is an optional {(asset, level, tf): ratio} lookup where
    ratio = |close - level| / avg_range[tf] (thesis.md §4 clearance magnitude). A None
    lookup, or a missing key, yields a x1.0 multiplier — byte-identical to the pre-plan-14
    behavior (every existing call site passes no magnitude).

    `dol_available` is an optional {"UP": bool, "DOWN": bool} — whether the S8 DOL menu has
    ANY eligible pool for that direction (facts.menus.dol[direction], non-empty). When the
    net-score-implied direction has no eligible DOL, there is no liquidity left to draw to
    (the same situation as price beyond the all-time high, where no resistance exists above
    to reference) — the expected bias is downgraded to NEUTRAL rather than a directional
    read with nothing to draw to. `dol_available=None` (or a direction missing from it)
    defaults to available=True — byte-identical to before this existed.

    `suppressed_p1_levels` (thesis.md §2.1b/§2.1d) is an optional {asset: set(level names)}
    — nested prevN levels and duplicate-simultaneous-sweep restatements (derive_facts.
    _nested_prev_levels / _duplicate_sweep_losers). A P1 item at a suppressed level scores
    ZERO, same "zeroed, not scored" mechanic as immature/exhausted. This ONLY gates P1 —
    `suppressed_p2_sites` (thesis.md §2.1b, derive_facts._p2_nesting_suppression) is the
    separate {asset: set(level names)} that gates P2: a candidate whose level is nested
    scores ZERO too, no exception for when the divergence itself happened to fire.

    P4-dominates-P3 (thesis.md §6 extension): needs no caller input — computed internally
    from the SAME declared `evidence` list. When one asset carries an HTF-confirmed (mature,
    not exhausted) P4 reclaim/failed-reclaim at a given mid (daily or weekly), the OTHER
    asset's P3 (static position) item at that SAME mid scores ZERO if its direction
    contradicts the P4 read — a dynamic, HTF-confirmed reclaim signal should not be netted
    against a mere position snapshot as an equal, offsetting data point. Motivating case
    (2026-07-27 09:20 ET): MNQ repeatedly failed to reclaim its weekly mid over almost an
    hour (04:57-05:59 ET) while MES sat comfortably above its own weekly mid the whole
    session, not testing it until 09:58 — MES's P3 "above" reading was stale relative to
    MNQ's already-confirmed rejection, not a genuinely offsetting bullish signal.

    P3 auto-derivation (2026-08-02): `level_htf_close_status` ({asset: {level: {tf:
    Optional[bool]}}}, None = immature, True = accept, False = reject — bench/facts.py's
    flat view of derive_facts.compute_facts's own per-level HTF-close verdict, which
    already covers the daily_mid/weekly_mid synthetic "levels" plan 17 Fix 3 added)
    replaces trust in the model's declared P3 items entirely: any P3 item referencing
    daily_mid/weekly_mid is dropped and replaced with code-synthesized item(s) per
    asset+mid — one per (tf) with a mature reading, so a genuine 1h-vs-4h disagreement
    scores as two distinct items rather than silently picking one (2026-08-02 fix: an
    earlier version of this preferred 4h unconditionally, which discarded a real fresh-
    vs-stale contradiction the same way the P1 bug below did). P3's position read is not
    a judgment call — it is a direct fact lookup — so this closes the omission failure
    (model never declares it) and the misdeclaration failure (2026-07-20: all 3 mid items
    declared at the wrong tier). `level_htf_close_status=None` (every pre-2026-08-02 call
    site) leaves P3 scoring exactly as declared, unchanged. Points use `_P3_MID_POINTS`
    (fixed 1.0 day / 1.5 week, 2026-08-02) instead of the shared tf_mult x tier_mult
    formula — a position snapshot's magnitude shouldn't scale with which timeframe's bar
    happened to close, unlike a confirmation event.

    P1/P2 auto-derivation (2026-08-02): `level_tiers` ({asset: {level: {"tier", "price"}}})
    and `smt_candidates` ([{level, tier, swept_ticker, unswept_ticker, meaningful}]) extend
    the SAME auto-derivation pattern to named levels — every real, non-suppressed level
    gets one auto-injected item PER mature tf reading (P2 when a tf's reading is a
    meaningful, unsuppressed SMT divergence's genuine reject; ordinary P1 otherwise — a
    level can resolve as P2 on one tf and P1 on the other if they disagree), any
    model-declared P1/P2 item for that (asset, level) is dropped first, and the model's
    only remaining lever is declaring the SAME item with `exhausted: true` to veto it. A
    level flagged in `suppressed_p2_sites` gets NO evidence at all (neither P1 nor P2 —
    thesis.md: "already nested when the divergence itself fired... no evidence at all
    here"), not a fallback to P1. Gated on BOTH `level_tiers` and `level_htf_close_status`
    being provided (either `None` — every pre-2026-08-02 call site passes neither — leaves
    P1/P2 scoring exactly as declared, unchanged); the §2.1e tier promotion below only
    needs `level_tiers`, independent of this gate.

    P1/P2-dominates-P3 (2026-08-02 extension of P4-dominates-P3 above): a mature,
    non-exhausted P1/P2 item ALSO dominates the OTHER asset's contradicting P3 mid — a
    day-tier item claims that asset's daily mid, a week-tier item the weekly mid, and a
    day-tier item that is ALSO week-confluent (see _is_week_confluent) claims both. No
    caller input needed beyond `now_price` (below); computed from the SAME evidence list.
    A mature P2 (not P1) ALSO dominates a contradicting P3 on the SAME asset — a bearish
    SMT divergence is a distinct reversal signal, not a description of current position,
    so it can genuinely disagree with that asset's own P3 (P1 cannot: a fresh sweep-
    accept all but guarantees agreement with the same asset's own mid position, so
    same-asset P1-vs-P3 domination stays unbuilt).

    Extremity-based dominance resolution (2026-08-02): `now_price` (float) resolves ties
    when MULTIPLE items compete for the same (asset, mid_type) dominance slot — the one
    whose OWN level is furthest from now_price wins (see _dominance_distance), not
    whichever happened to be iterated first. P4 always outranks P1/P2 regardless of
    distance (a two-step, HTF-confirmed reclaim beats a single sweep/divergence read);
    among same-priority candidates, extremity breaks the tie. `now_price=None` (every
    pre-2026-08-02 call site) makes every distance 0.0, falling back to Python dict/list
    iteration order — the exact prior behavior.

    thesis.md §2.1e promotion (2026-08-02, scoped): `week_extremes` ({asset: {"hi", "lo"}})
    lets a day-tier P1/P2 item score at week-tier weight when its OWN price sits within
    5% of the week's range of THAT ASSET's current week high/low — a day-tier level that
    is ALSO the week's own extreme isn't merely a day-scale signal. `level_tiers=None` or
    `week_extremes=None` leaves this off.

    P5 same-move dedup (2026-08-02): `fvg_zone_meta` ({id: {asset, tf, kind, ts}},
    bench/facts.py) lets score_thesis_evidence detect FVG-fill zones formed on CONSECUTIVE
    bars of the same asset/kind/tf — typically one continuous move that kept creating new
    gaps as it went, not independent confirmations. Every member of such a run except the
    freshest (most recent) scores ZERO, same "zeroed, not scored" mechanic as tf-dedup
    above (which this mirrors for P5's own duplication shape). A lone zone, or one
    separated from its neighbors by a gap (a genuinely distinct move), is untouched.
    `fvg_zone_meta=None` (every pre-2026-08-02 call site) leaves P5 scoring exactly as
    declared, unchanged.

    P3-vs-P4 mid promotion (2026-08-05, PER TF as of the same-day rewrite): `mid_reclaim`
    ({asset: {"daily_mid"|"weekly_mid": {"1h"|"4h": {"fresh": bool, "cross_dir":
    "up"|"down"}}}}, derive_facts._mid_tf_state) decides WHICH criterion a mid's auto-
    derived item takes, per (asset, mid, tf) — a mid's 1h and 4h crossings can genuinely
    differ. A "fresh" crossing (the mid's crossing on THIS tf postdates, or ties, the
    tier's own most-recent extreme on the tested side — nothing more extreme has happened
    since) is a live, still-relevant reclaim/failed-reclaim EVENT and is promoted to P4
    (`{mid}_high` for an up-cross, `{mid}_low` for a down-cross), NOT declared as P3 — the
    same "P4-overrules-P3" logic already governs the dominance pre-pass above, just
    applied at declaration time instead of via a later zeroing pass, so the ledger doesn't
    carry a redundant P3 restatement of an event already covered by a P4 item. A "stale"
    crossing (superseded by a later, more extreme move) or no `mid_reclaim` entry at all
    (never crossed, or `mid_reclaim=None` — every pre-2026-08-05 call site) stays plain
    P3, exactly as before. Motivating case (2026-07-22 09:20 ET): MNQ/MES's daily_mid was
    crossed ~07:00-07:40 ET but BOTH assets then set a fresh, deeper daily low in the
    08:55 ET bar — stale, P3 only. Their weekly_mid was crossed ~09:00 ET with no later,
    deeper weekly low since, and a qualifying close already available — fresh, P4. Any
    model-declared P3/P4 item for a mid this auto-derivation covers is dropped and
    replaced — like P3 alone before it, this is now a plain fact lookup end to end
    (position AND reclaim verdict), not a judgment call, so unlike P1/P2's own auto-
    derivation there is no `exhausted: true` override lever here: a mid auto-derived as
    P4 is HTF-confirmed by construction, not a staleness candidate.

    Partial-bar reversal (2026-08-05): `htf_reversal` ({asset: {level_or_mid_name:
    {"1h"|"4h": "none"|"discount"|"omit"|"reverse"}}}, derive_facts._htf_reversal_tier)
    applies to EVERY P1/P3/P4 item (declared or auto-derived) whose (asset, level, tf) has
    an entry — a completed bar's own accept/reject verdict is a settled fact, but if the
    CURRENTLY-FORMING next bar has, by call time, already retraced back through the level
    it is no longer safe to treat that verdict as if nothing has happened since. 'none' (or
    no entry) leaves the item exactly as declared/auto-derived. 'discount' halves the
    item's points (a partial recross, not yet a full retracement of the completed bar's
    own body). 'omit' zeroes the item — same "zeroed, not scored" mechanic as the other
    suppression gates — once the partial bar has fully engulfed the completed bar's body
    but not yet its wick, neither the original nor a flipped read is safe to assert.
    'reverse' flips the item's `direction` (accept<->reject) before computing side/points —
    the partial bar has broken the completed bar's ENTIRE range (a clear MSS), the same
    "model judges, code computes" mechanical certainty as everything else in this ledger.
    `htf_reversal=None` (every pre-2026-08-05 call site) leaves every item exactly as
    declared/auto-derived, unaffected.

    P4-supersedes-P3 per (asset, mid) + position-only P3 (2026-08-15, from the 2026-08-10
    09:20 ET audit): within the P3/P4 auto-injection above, a fresh P4 on ANY tf now
    suppresses the other tf's stale P3 for the SAME asset+mid (one physical mid, one item —
    the per-tf split was stacking a P4 and a P3 restatement of the same equilibrium), and a
    mid with NO qualifying HTF crossing at all gets a position-only P3 injected from
    `mid_position` ({asset: {mid: {"side": "above"|"below", ...}}}, derive_facts.
    FactsBundle.mid_position) — P3 is a position snapshot, so "never crossed, price parked
    ~200pts one side" is the STRONGEST read, not a missing one. `mid_position=None` (every
    pre-2026-08-15 call site) skips the injection, exactly as before.

    P1 equilibrium-staleness hard gate (2026-08-15, #6): `p1_stale_levels` ({asset:
    set/list of level names}, derive_facts._p1_equilibrium_staleness truths) — a mature P1
    item at a stale level (price has since round-tripped to the tier's own equilibrium) is
    skipped by the P1 auto-injection and zeroed if declared, same "zeroed, not scored"
    mechanic as suppressed_p1_levels. Previously a model-overridable S9 suggestion the
    model ignored in practice (2026-08-10: MES prev1_day_high, tagged stale on both rows,
    was the tally's largest item). `p1_stale_levels=None` leaves scoring unchanged.

    P2 effective-verdict dispatch — "Stage 1" (2026-08-15): thesis.md §2.1 P2's condition
    (c) is now evaluated on the EFFECTIVE (post-partial-bar-reversal) verdict inside the
    P1/P2 auto-injection, not the raw completed-bar close: 'omit' blocks both P1 and P2;
    'reverse' flips the verdict (a fully-MSS'd acceptance at a meaningful divergence fires
    P2; a fully-MSS'd rejection falls back to P1 and gets flipped by the pre-pass instead
    of standing as a stale P2 — P2 previously escaped the §10 reversal machinery
    entirely); a P2 fired on a 'discount'-stage read carries the same x0.5 the pre-pass
    applies to P1. `htf_reversal=None` disables all of it, as before.

    P2 discount-fire — "Stage 2" (2026-08-15, EXPERIMENTAL, default OFF): with
    `p2_discount_fire_ratio` set (an avg_range_1h multiple), a 'discount'-stage recross of
    an ACCEPTED read at a meaningful, unsuppressed divergence site fires P2 early at x0.5
    when the recross is distance-safe: `recross_distance` ({asset: {level: ratio}},
    derive_facts.FactsBundle.recross_distance — |now_price − level| / avg_range_1h) must
    be >= the threshold. No production call site sets the ratio; it exists for the offline
    A/B that decides whether (and at what threshold) this rung ships."""
    if level_htf_close_status is not None:
        evidence = [
            it for it in (evidence or [])
            if not (isinstance(it, dict) and it.get("criterion") in ("P3", "P4")
                    and (it.get("level") or "").startswith(("daily_mid", "weekly_mid")))
        ]
        for _asset in ("MNQ", "MES"):
            for _mid_name, _tier in (("daily_mid", "day"), ("weekly_mid", "week")):
                _tf_map = (level_htf_close_status.get(_asset) or {}).get(_mid_name) or {}
                _reclaim_by_tf = (mid_reclaim or {}).get(_asset, {}).get(_mid_name) or {}
                # per-tf, not "prefer 4h": if 1h/4h genuinely disagree, inject BOTH (the
                # existing tf-dedup pre-pass below collapses them back to one when they
                # AGREE on direction -- same reasoning as the P1/P2 auto-derivation below).
                # 2026-08-15 (#4, from the 2026-08-10 09:20 ET audit): EXCEPT that a fresh
                # P4 on ANY tf supersedes the other tf's stale P3 for the SAME asset+mid —
                # a live, HTF-confirmed reclaim event and a position snapshot of the same
                # physical mid are not two independent data points (2026-08-10: MES carried
                # daily_mid_low P4 [1h] AND daily_mid P3 [4h], stacking 2.25 one-way).
                _has_fresh_p4 = any(
                    _tf_map.get(_tf) is not None
                    and (_reclaim_by_tf.get(_tf) or {}).get("fresh")
                    for _tf in ("1h", "4h"))
                _injected_any = False
                for _tf in ("1h", "4h"):
                    _val = _tf_map.get(_tf)
                    if _val is None:
                        continue
                    _reclaim = _reclaim_by_tf.get(_tf)
                    if _reclaim and _reclaim.get("fresh"):
                        _up = _reclaim.get("cross_dir") == "up"
                        _held = _val if _up else not _val
                        evidence.append({
                            "criterion": "P4", "asset": _asset,
                            "level": f"{_mid_name}_{'high' if _up else 'low'}",
                            "tier": _tier, "tf": _tf,
                            "direction": "accept" if _held else "reject",
                            "mature": True, "exhausted": False,
                        })
                        _injected_any = True
                    elif not _has_fresh_p4:
                        evidence.append({
                            "criterion": "P3", "asset": _asset, "level": _mid_name,
                            "tier": _tier, "tf": _tf,
                            "direction": "accept" if _val else "reject",
                            "mature": True, "exhausted": False,
                        })
                        _injected_any = True
                    else:
                        _injected_any = True     # stale tf superseded by the fresh P4
                # 2026-08-15 (#3): a mid with NO qualifying HTF crossing at all (price
                # parked on one side for the whole lookback — exactly the strongest
                # position case) previously produced NO P3 item anywhere, silently losing
                # the read (2026-08-10: both assets ~200pts above their weekly mids, zero
                # UP-side mid evidence). P3 is a position snapshot, not a crossing event —
                # inject a position-only P3 from `mid_position` (derive_facts, per-asset
                # last close vs its own mid). tf is nominally "1h" (schema requires one;
                # P3 points are tier-fixed, so tf carries no weight here).
                if not _injected_any:
                    _pos = ((mid_position or {}).get(_asset) or {}).get(_mid_name)
                    if _pos and _pos.get("side") in ("above", "below"):
                        evidence.append({
                            "criterion": "P3", "asset": _asset, "level": _mid_name,
                            "tier": _tier, "tf": "1h",
                            "direction": "accept" if _pos["side"] == "above" else "reject",
                            "mature": True, "exhausted": False,
                        })

    # P1/P2 auto-derivation (2026-08-02): every real named level's tier/direction/tf is
    # ALSO a plain fact lookup (bundle.levels + level_htf_close_status), not a judgment
    # call — the only genuine judgment left is relevance (which candidates are worth
    # citing), and volume-checking across the 10 sample dates found that pool stays small
    # (2-7/day for P1, 0-9/day for P2) after existing suppression, unlike P5's 30-53/day
    # (rejected for exactly this reason). `level_tiers` gates this block; `None` (every
    # pre-2026-08-02 call site) leaves P1/P2 scoring exactly as declared, unchanged. The
    # model's only remaining lever is `exhausted: true` on a matching (asset, level) item
    # — same veto mechanic P2 staleness already uses — which zeroes the auto-injected
    # item instead of overriding it silently.
    #
    # P2-vs-P1 resolution is PER-TF, not per-level: a level can be BOTH a real named
    # level AND a meaningful SMT/P2 candidate, but scoring the SAME tf's reading as both
    # would double-count one physical event -- so for each tf independently, a genuine
    # reject on a meaningful, unsuppressed candidate scores as P2; anything else
    # (accept, or reject on a non-candidate level) scores as ordinary P1. A level whose
    # 1h and 4h readings disagree can therefore resolve as P2 on one tf and P1 on the
    # other -- a real, distinct pair of facts, not a duplicate (mirrors the P3 fix above
    # and the tf-dedup pre-pass's own "keep genuine disagreements" scoping). A
    # `suppressed_p2_sites` level is excluded entirely (see docstring), never falls
    # through to P1.
    if level_tiers is not None and level_htf_close_status is not None:
        _declared_exhausted = {
            (it.get("asset"), it.get("level")) for it in (evidence or [])
            if isinstance(it, dict) and it.get("criterion") in ("P1", "P2")
            and bool(it.get("exhausted"))
        }
        _auto_pairs = {(_tkr, _name) for _tkr, _m in (level_tiers or {}).items() for _name in _m}
        evidence = [
            it for it in evidence
            if not (isinstance(it, dict) and it.get("criterion") in ("P1", "P2")
                    and (it.get("asset"), it.get("level")) in _auto_pairs)
        ]
        # meaningful, unsuppressed SMT candidates, keyed by (swept_ticker, level) -> tier.
        # A P2-suppressed level is EXCLUDED entirely below (not P1, not P2 -- thesis.md:
        # "already nested when the divergence itself fired... no evidence at all here"),
        # so it is never added to this lookup in the first place.
        _p2_lookup: dict = {}
        for _cand in (smt_candidates or []):
            _lvl, _tier2 = _cand.get("level"), _cand.get("tier")
            _swept = _cand.get("swept_ticker")
            if _cand.get("meaningful") and _swept is not None and _lvl is not None \
                    and _lvl not in ((suppressed_p2_sites or {}).get(_swept) or ()):
                _p2_lookup[(_swept, _lvl)] = _tier2
        for _asset2, _levels2 in (level_tiers or {}).items():
            _p2_excluded = set((suppressed_p2_sites or {}).get(_asset2) or ())
            for _name2, _meta2 in (_levels2 or {}).items():
                if (_asset2, _name2) in _declared_exhausted or _name2 in _p2_excluded:
                    continue
                _p1_suppressed_here = _name2 in ((suppressed_p1_levels or {}).get(_asset2) or ())
                # 2026-08-15 (#6): equilibrium-stale levels are hard-excluded from fresh
                # P1 injection (see docstring). P2 is deliberately untouched — divergences
                # have their own tier-relative shelf-life mechanism.
                _p1_stale_here = _name2 in ((p1_stale_levels or {}).get(_asset2) or ())
                _tf_map2 = (level_htf_close_status.get(_asset2) or {}).get(_name2) or {}
                # per-tf, not per-level: if 1h and 4h GENUINELY DISAGREE (a real
                # contradiction, not a duplicate -- 2026-07-27's MNQ prev1_day_high case,
                # 1h fresh reject vs 4h stale accept), injecting only one and discarding
                # the other would silently erase a real signal. Inject one item per
                # (asset, level, tf) that has a mature reading; the EXISTING tf-dedup
                # pre-pass below already collapses the 1h when both AGREE (same
                # criterion/direction), so no separate "prefer 4h" logic is needed here.
                for _tf3 in ("1h", "4h"):
                    _val3 = _tf_map2.get(_tf3)
                    if _val3 is None:
                        continue
                    # Stage 1 (2026-08-15): the P1<->P2 dispatch reads the EFFECTIVE
                    # (post-partial-bar-reversal) verdict, not the raw completed-bar close
                    # -- thesis.md §2.1 P2 condition (c). 'omit' blocks BOTH (neither the
                    # original nor a flipped read is safe -- same semantics the pre-pass
                    # below already applies to P1); 'reverse' flips the verdict, so a
                    # fully-MSS'd acceptance at a live divergence site fires P2 instead of
                    # a flipped P1, and a fully-MSS'd REJECTION stops firing P2 (it falls
                    # to P1, which the pre-pass then flips -- previously that stale P2
                    # escaped the reversal machinery entirely); 'discount' does not flip,
                    # but a P2 fired on a discount-stage read inherits the same x0.5 the
                    # pre-pass gives P1 (via the htf_reversal tag on the injected item).
                    _rev3 = (((htf_reversal or {}).get(_asset2) or {}).get(_name2)
                             or {}).get(_tf3, "none")
                    if _rev3 == "omit":
                        continue
                    _eff3 = (not _val3) if _rev3 == "reverse" else _val3
                    _p2_tier = _p2_lookup.get((_asset2, _name2))
                    # Stage 2 (2026-08-15, EXPERIMENTAL -- default off): optionally let a
                    # distance-safe 'discount'-stage recross of an ACCEPTED read at a live
                    # divergence site fire P2 early (at the x0.5 discount weight), without
                    # waiting for the full 'reverse' MSS. `p2_discount_fire_ratio` is the
                    # avg_range_1h multiple the recross must clear (`recross_distance`,
                    # derive_facts); None (every production call site) disables it.
                    _discount_fire = bool(
                        p2_discount_fire_ratio is not None
                        and _p2_tier is not None and _rev3 == "discount" and _eff3 is True
                        and (((recross_distance or {}).get(_asset2) or {}).get(_name2)
                             or 0.0) >= p2_discount_fire_ratio)
                    if _p2_tier is not None and (_eff3 is False or _discount_fire):
                        _p2_item = {
                            "criterion": "P2", "asset": _asset2, "level": _name2,
                            "tier": _p2_tier, "tf": _tf3, "direction": "reject",
                            "mature": True, "exhausted": False,
                        }
                        if _rev3 != "none":
                            _p2_item["htf_reversal"] = _rev3   # 'discount' -> x0.5 below
                        evidence.append(_p2_item)
                    elif not _p1_suppressed_here and not _p1_stale_here:
                        evidence.append({
                            "criterion": "P1", "asset": _asset2, "level": _name2,
                            "tier": _meta2.get("tier"), "tf": _tf3,
                            "direction": "accept" if _val3 else "reject",
                            "mature": True, "exhausted": False,
                        })

    # Partial-bar reversal (2026-08-05, see docstring): applied BEFORE the dominance/
    # dedup pre-passes below so they see the corrected picture too -- an 'omit'ted item
    # must not participate in a cross-asset contradiction or dominance vote either, and a
    # 'reverse'd item's flipped direction is what actually happened, not a footnote applied
    # only at final scoring. 'discount' needs no pre-processing here (direction is
    # unchanged, only its weight); tagged for the final points formula below instead.
    if htf_reversal is not None:
        _reversed_evidence = []
        for _it in (evidence or []):
            if not isinstance(_it, dict) or _it.get("criterion") not in ("P1", "P3", "P4"):
                _reversed_evidence.append(_it)
                continue
            _tier = ((htf_reversal.get(_it.get("asset")) or {}).get(_it.get("level")) or {}
                    ).get(_it.get("tf"), "none")
            if _tier == "omit":
                continue
            if _tier == "reverse" and _it.get("direction") in ("accept", "reject"):
                _it = {**_it, "direction": "reject" if _it["direction"] == "accept" else "accept",
                      "htf_reversal": "reverse"}
            elif _tier == "discount":
                _it = {**_it, "htf_reversal": "discount"}
            _reversed_evidence.append(_it)
        evidence = _reversed_evidence

    # plan 35 D3 (2026-09-13): a P1 item's TIER is a FACT about its level, not a judgment
    # call -- so code-derive it from `level_tiers` and overwrite whatever the model
    # declared, the same "model judges WHICH item, code computes its weight" split already
    # applied to sign (_item_side) and to P4's own tier (below). `level_tiers` is built in
    # bench/facts.py from derive_facts' level map with the per-asset running-extreme
    # promotion (thesis.md §2.1, 2026-08-15) already folded in.
    #
    # Why one pre-pass rather than a fix at the points formula: the declared tier is read
    # in FOUR places (the two dominance pre-passes' daily/weekly mid_type split, the
    # tf-dedup group key, and tier_mult), plus _is_week_confluent's day-tier precondition.
    # Patching only the multiplier would leave the other four disagreeing with it.
    # Normalising the list first makes every consumer, and the `scored_evidence` audit
    # echo, see one tier.
    #
    # P2 IS DELIBERATELY EXCLUDED. A P2/SMT candidate's tier is NOT `level_tiers`' per-asset
    # promotion: derive_facts promotes an SMT candidate only when the level is the running
    # extreme for BOTH assets, and resolves a mixed day/week qualification CONSERVATIVELY to
    # day (see the promotion loop in compute_facts). Overwriting that with the per-asset
    # tier silently reverses a deliberate pair-wise rule -- measured on 2026-09-04, where it
    # moved MNQ london(cur)_high day->week AND cascaded through P1/P2-dominates-P3 to zero
    # two P3 mid rows, taking the day's net score from -1.5 to -5.0. That may or may not be
    # an improvement; it is a different change from this one and needs its own measurement.
    #
    # What this is worth, stated honestly: on BOTH production call sites (here and
    # run_agent._derive_thesis_arithmetic) `level_htf_close_status` is also supplied, which
    # arms the 2026-08-02 auto-derivation above -- and that already DROPS every declared
    # P1/P2 at a level in `level_tiers` and re-injects it with the code tier. So the
    # model-declared-tier hole plan 35 set out to close is already shut on the live path,
    # and this pre-pass is defence in depth for any caller that passes `level_tiers`
    # WITHOUT `level_htf_close_status` (auto-derivation off). Verified inert in production:
    # the 22 recorded boundaries in <global>/thesis_cache score identically with and
    # without it.
    #
    # A level absent from `level_tiers` (synthetic mids, FVG zone ids, or no facts at all)
    # keeps its declared tier -- byte-identical to before this existed.
    if level_tiers:
        _retiered = []
        for _it in (evidence or []):
            if not isinstance(_it, dict) or _it.get("criterion") != "P1":
                _retiered.append(_it)
                continue
            _code_tier = ((level_tiers.get(_it.get("asset")) or {})
                          .get(_it.get("level")) or {}).get("tier")
            if _code_tier in _TIER_MULT and _code_tier != _it.get("tier"):
                _it = {**_it, "tier": _code_tier, "tier_declared": _it.get("tier")}
            _retiered.append(_it)
        evidence = _retiered

    # thesis.md §6 extension: an HTF-confirmed P4 reclaim/failed-reclaim on ONE asset should
    # outweigh a contradicting P3 (static position) read on the OTHER asset at the SAME mid,
    # not be netted against it as an equal, offsetting data point — P4 is a dynamic,
    # HTF-confirmed event, P3 is a plain snapshot. Pre-pass so a P4 item can dominate a P3
    # item regardless of which one appears first in the declared list.
    #
    # Extremity-based resolution (2026-08-02): when MULTIPLE items compete for the SAME
    # (asset, mid_type) dominance slot, the one whose OWN level is furthest from now_price
    # wins — not whichever happened to be iterated first (the previous `setdefault`
    # first-come behavior). P4 still always outranks P1/P2 (a two-step, HTF-confirmed
    # reclaim is a stronger signal than a single sweep read or SMT divergence); among
    # same-priority candidates, extremity breaks the tie. Motivating case (2026-07-15
    # 09:20 ET): MNQ's own prev1_day_high (P1 accept, UP, dist 33.5) and prev2_day_high
    # (P2 reject, DOWN, dist 87.0 — the more extreme, week-confluent level) disagreed on
    # which should dominate MES's contradicting P3 — prev2_day_high should win, and now
    # does, regardless of declaration order. Verified across the 10 sample dates first:
    # 2 genuine cross-level conflicts (07-15, 07-21), both resolved correctly by this rule
    # without flip risk elsewhere; same-level cross-tf disagreements (07-23, 07-27) have
    # identical distance by construction and are unaffected (tf-dedup handles those).
    _P4_MID_LEVELS = {"daily_mid_high": "daily", "daily_mid_low": "daily",
                      "weekly_mid_high": "weekly", "weekly_mid_low": "weekly"}
    _dominance_candidates: dict = {}   # (asset, "daily"|"weekly") -> [(priority, distance, side), ...]

    def _add_dominance_candidate(asset, mid_type, priority, dist, side):
        _dominance_candidates.setdefault((asset, mid_type), []).append((priority, dist, side))

    for item in evidence or []:
        if not isinstance(item, dict) or item.get("criterion") != "P4":
            continue
        mid_type = _P4_MID_LEVELS.get(item.get("level"))
        if mid_type is None or not bool(item.get("mature")) or bool(item.get("exhausted")):
            continue
        side = _item_side(item)
        if side is not None:
            _add_dominance_candidate(item.get("asset"), mid_type, 2,
                                     _dominance_distance(item, level_tiers, now_price), side)

    # P1/P2-dominates-P3 (2026-08-02 extension, thesis.md §6): the SAME cross-asset
    # domination shape as P4 above, just letting a mature, non-exhausted P1/P2 item ALSO
    # dominate the OTHER asset's contradicting P3 mid — a day-tier item dominates that
    # asset's DAILY mid, a week-tier item the WEEKLY mid (mirroring P4's own tier split,
    # since P1/P2 have no inherent mid_type the way P4's _high/_low levels do; session-
    # tier items dominate neither). A day-tier item that is ALSO week-confluent (see
    # _is_week_confluent) claims BOTH mid_types, not just one — it is genuinely dual-
    # natured: still a day-family level by name, but also the week's own extreme by price.
    # Volume-checked across the 10 sample dates first (nonzero on 8/10, often double
    # digits) — this is a common contradiction pattern, not an edge case.
    # NOTE: same-asset P1-vs-P3 "domination" is deliberately NOT built — now that P3 is
    # fully auto-derived from the SAME asset's own latest close (2026-08-02), a same-asset
    # P1 read and its own P3 position essentially never disagree (a fresh sweep beyond a
    # high all but guarantees that asset already sits above its own mid) — there is
    # nothing meaningful to dominate there. P2 is different (see p2_same_asset_dominance
    # below): a bearish SMT divergence is a distinct reversal signal, not a description of
    # current position, so it CAN meaningfully disagree with the same asset's own P3.
    for item in evidence or []:
        if not isinstance(item, dict) or item.get("criterion") not in ("P1", "P2"):
            continue
        if not bool(item.get("mature")) or bool(item.get("exhausted")):
            continue
        mid_types = set()
        if item.get("tier") == "day":
            mid_types.add("daily")
        elif item.get("tier") == "week":
            mid_types.add("weekly")
        if _is_week_confluent(item, level_tiers, week_extremes):
            mid_types.add("weekly")
        side = _item_side(item)
        if side is not None:
            dist = _dominance_distance(item, level_tiers, now_price)
            for mid_type in mid_types:
                _add_dominance_candidate(item.get("asset"), mid_type, 1, dist, side)

    p4_mid_dominance: dict = {   # (asset, "daily"|"weekly") -> "UP"|"DOWN"
        key: max(cands, key=lambda c: (c[0], c[1]))[2]
        for key, cands in _dominance_candidates.items()
    }

    # P2-same-asset-dominates-P3 (2026-08-02, thesis.md §6 further extension): a mature,
    # non-exhausted P2 divergence dominates a CONTRADICTING P3 reading on the SAME asset
    # too (P1 does not — see NOTE above). Motivating case (2026-07-15 09:20 ET): MNQ's own
    # P2 divergence at prev2_day_high (a genuine bearish reversal warning, week-confluent)
    # coexisted with MNQ's own P3 daily/weekly mid still reading "above" (a static fact
    # about where the last close sat) — two different questions, legitimately allowed to
    # disagree, unlike P1's "did I close beyond this level" which all but guarantees
    # agreement with the same asset's own mid position. Same extremity resolution as
    # above when multiple same-asset P2 candidates disagree.
    _p2_same_asset_candidates: dict = {}   # (asset, "daily"|"weekly") -> [(distance, side), ...]
    for item in evidence or []:
        if not isinstance(item, dict) or item.get("criterion") != "P2":
            continue
        if not bool(item.get("mature")) or bool(item.get("exhausted")):
            continue
        mid_types = set()
        if item.get("tier") == "day":
            mid_types.add("daily")
        elif item.get("tier") == "week":
            mid_types.add("weekly")
        if _is_week_confluent(item, level_tiers, week_extremes):
            mid_types.add("weekly")
        side = _item_side(item)
        if side is not None:
            dist = _dominance_distance(item, level_tiers, now_price)
            for mid_type in mid_types:
                _p2_same_asset_candidates.setdefault((item.get("asset"), mid_type), []).append((dist, side))
    p2_same_asset_dominance: dict = {   # (asset, "daily"|"weekly") -> "UP"|"DOWN"
        key: max(cands, key=lambda c: c[0])[1]
        for key, cands in _p2_same_asset_candidates.items()
    }

    # tf-dedup pre-pass (2026-08-01 root-cause audit): when a 1h AND a 4h item both exist
    # for the same (criterion, asset, level, tier, direction) -- the same physical event
    # cited at both qualifying timeframes -- and BOTH are mature/non-exhausted, keep only
    # the 4h (the stronger, higher tier_mult confirmed read) and zero the 1h. Citing both
    # double-counts one underlying continuation as two pieces of evidence; confirmed
    # contributing material padding to multiple wrong 2026-07 calls this session (e.g. one
    # event alone carried 4 of 5 UP-side points on 2026-07-15). An item with only a mature
    # 1h and an immature 4h is untouched -- we don't yet know how the 4h will resolve.
    _tf_dedup_zero: set = set()
    _tf_groups: dict = {}
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("criterion"), item.get("asset"), item.get("level"),
               item.get("tier"), item.get("direction"))
        _tf_groups.setdefault(key, []).append(item)
    for _key, _items in _tf_groups.items():
        _has_mature_4h = any(
            _it.get("tf") == "4h" and bool(_it.get("mature")) and not bool(_it.get("exhausted"))
            for _it in _items)
        if _has_mature_4h:
            for _it in _items:
                if _it.get("tf") == "1h":
                    _tf_dedup_zero.add(id(_it))

    # P5 same-move dedup (2026-08-02): several FVG-fill zones formed on CONSECUTIVE bars of
    # the same asset/kind(bull|bear)/tf are typically one continuous price move that kept
    # creating new gaps as it went -- citing each separately double(triple/...)-counts that
    # one move, the same "one physical event, don't score it twice" issue tf-dedup already
    # solves for P1/P2. Zero every member of a run of 2+ consecutive-bar zones EXCEPT the
    # freshest (most recent) one; a lone zone, or one separated from its neighbors by a gap
    # (a genuinely distinct move), is untouched. `fvg_zone_meta` ({id: {asset, tf, kind,
    # ts}}, bench/facts.py) is needed to resolve each declared P5 item's own asset/tf/kind/ts
    # without re-parsing the level-id string; `fvg_zone_meta=None` (every pre-2026-08-02
    # call site) leaves P5 scoring exactly as declared, unchanged.
    _p5_dedup_zero: set = set()
    if fvg_zone_meta is not None:
        import pandas as pd
        _bar_period = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4)}
        _p5_groups: dict = {}   # (asset, kind, tf) -> [(item, ts), ...]
        for item in evidence or []:
            if not isinstance(item, dict) or item.get("criterion") != "P5":
                continue
            meta = fvg_zone_meta.get(item.get("level"))
            if not meta:
                continue
            try:
                ts = pd.Timestamp(meta.get("ts"))
            except (ValueError, TypeError):
                continue
            key = (meta.get("asset"), meta.get("kind"), meta.get("tf"))
            _p5_groups.setdefault(key, []).append((item, ts))
        for (_asset3, _kind3, _tf4), _members in _p5_groups.items():
            _period = _bar_period.get(_tf4)
            if _period is None or len(_members) < 2:
                continue
            _members.sort(key=lambda m: m[1])
            _i = 0
            _n = len(_members)
            while _i < _n:
                _j = _i
                while _j + 1 < _n and _members[_j + 1][1] - _members[_j][1] == _period:
                    _j += 1
                if _j > _i:   # a run of 2+ consecutive-bar zones -- keep only the freshest
                    for _k in range(_i, _j):
                        _p5_dedup_zero.add(id(_members[_k][0]))
                _i = _j + 1

    scored = []
    net = 0.0
    by_level: dict = {}   # level -> {asset: direction}, for the §6 contradiction check
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        tf_mult = _TF_MULT.get(item.get("tf"), 0.0)
        tier_mult = _TIER_MULT.get(item.get("tier"), 0.0)
        # thesis.md §2.1a: a daily_mid_high/daily_mid_low/weekly_mid_high/weekly_mid_low
        # (P4) item's tier is fully determined by its own identity, never a judgment call
        # -- so it is code-derived here, the same "model judges, code computes" split
        # already applied to sign (_item_side). P3's daily_mid/weekly_mid gets its own
        # FIXED point value instead (see _P3_MID_POINTS below), not a tier_mult override.
        if item.get("criterion") == "P4":
            _lvl = item.get("level") or ""
            if _lvl.startswith("daily_mid"):
                tier_mult = _TIER_MULT["day"]
            elif _lvl.startswith("weekly_mid"):
                tier_mult = _TIER_MULT["week"]
        # thesis.md §2.1e promotion (2026-08-02, scoped narrowly) -- see _is_week_confluent.
        if item.get("criterion") in ("P1", "P2") and _is_week_confluent(item, level_tiers, week_extremes):
            tier_mult = _TIER_MULT["week"]
        mature = bool(item.get("mature"))
        ratio = None
        if magnitude:
            ratio = magnitude.get((item.get("asset"), item.get("level"), item.get("tf")))
        mag_mult = _magnitude_mult(ratio)
        # plan 15 Task 5: an item the model flags `exhausted: True` (a played-out stale SMT,
        # see S9 suggested_exhausted) contributes ZERO — same "zeroed, not scored" mechanic as
        # the immature gate, a pure code mechanic on a model judgment (no new sign logic).
        exhausted = bool(item.get("exhausted"))
        p1_suppressed = bool(
            item.get("criterion") == "P1" and suppressed_p1_levels
            and item.get("level") in (suppressed_p1_levels.get(item.get("asset")) or ()))
        # 2026-08-15 (#6): equilibrium-staleness hard gate — see docstring.
        p1_stale = bool(
            item.get("criterion") == "P1" and p1_stale_levels
            and item.get("level") in (p1_stale_levels.get(item.get("asset")) or ()))
        # thesis.md §2.1b: a P2/SMT candidate at a nested level scores ZERO too, same
        # mechanic as P1 — derive_facts._p2_nesting_suppression, no exception for when the
        # divergence itself happened to fire.
        p2_suppressed = bool(
            item.get("criterion") == "P2" and suppressed_p2_sites
            and item.get("level") in (suppressed_p2_sites.get(item.get("asset")) or ()))
        # thesis.md §6 extension (see pre-pass above): a P3 item whose OTHER asset carries an
        # HTF-confirmed P4/P1/P2 signal at the SAME mid, in the OPPOSITE direction, is
        # dominated by that dynamic signal and scores ZERO — same mechanic as the other
        # suppression gates, never applied to P1/P2/P4/P5. 2026-08-02: a mature P2
        # divergence ALSO dominates a contradicting P3 on the SAME asset (p2_same_asset_
        # dominance, pre-pass below) — a bearish SMT and "still above own mid" answer
        # different questions and can genuinely disagree even for one asset, unlike P1
        # (a fresh sweep-accept all but guarantees that asset is already above its own
        # mid, so same-asset P1-vs-P3 domination stays unbuilt as near-vacuous).
        p3_dominated = False
        if item.get("criterion") == "P3" and item.get("level") in ("daily_mid", "weekly_mid"):
            mid_type = "daily" if item.get("level") == "daily_mid" else "weekly"
            other_asset = "MES" if item.get("asset") == "MNQ" else "MNQ"
            this_asset = item.get("asset")
            dom_side = (p4_mid_dominance.get((other_asset, mid_type))
                        or p2_same_asset_dominance.get((this_asset, mid_type)))
            this_side = _mid_side(item)
            p3_dominated = dom_side is not None and this_side is not None and dom_side != this_side
        tf_deduped = id(item) in _tf_dedup_zero
        p5_deduped = id(item) in _p5_dedup_zero
        suppressed = (p1_suppressed or p1_stale or p2_suppressed or p3_dominated
                      or tf_deduped or p5_deduped)
        scored_mature = mature and not exhausted and not suppressed
        # thesis.md §10 (2026-08-05): a 'discount' partial-bar reversal (see the pre-pass
        # above) halves the item's points -- a partial recross of the level, not yet a
        # full retracement of the completed bar's own body.
        reversal_mult = 0.5 if item.get("htf_reversal") == "discount" else 1.0
        if item.get("criterion") == "P3" and item.get("level") in ("daily_mid", "weekly_mid"):
            _p3_base = _P3_MID_POINTS["day" if item.get("level") == "daily_mid" else "week"]
            points = round(_p3_base * mag_mult * reversal_mult, 4) if scored_mature else 0.0
        else:
            points = (round(_BASE_POINTS * tf_mult * tier_mult * mag_mult * reversal_mult, 4)
                     if scored_mature else 0.0)
        side = _item_side(item)
        if side == "UP":
            net += points
        elif side == "DOWN":
            net -= points
        scored.append({**item, "points": points, "side": side,
                       "mag_ratio": ratio, "mag_mult": mag_mult})

        level, asset, direction = item.get("level"), item.get("asset"), item.get("direction")
        if item.get("criterion") == "P1" and scored_mature and level and asset:
            by_level.setdefault(level, {})[asset] = direction

    contradiction = any(len(set(d.values())) > 1 for d in by_level.values() if len(d) > 1)
    net = round(net, 4)
    if net > _EPS:
        expected_bias = "UP"
    elif net < -_EPS:
        expected_bias = "DOWN"
    else:
        expected_bias = "NEUTRAL"

    # No-liquidity override: a directional read with no eligible DOL to draw to isn't a
    # real actionable direction (mirrors the ATH case — no resistance exists above it either).
    no_liquidity = (expected_bias in ("UP", "DOWN")
                    and dol_available is not None
                    and not dol_available.get(expected_bias, True))
    if no_liquidity:
        expected_bias = "NEUTRAL"

    if no_liquidity:
        ceiling = "LOW"          # no liquidity -> low confidence, regardless of net magnitude
    elif contradiction:
        # §6: tally normally, cap the ceiling — never HIGH while a live contradiction stands.
        ceiling = "MEDIUM" if abs(net) >= 2.0 else "LOW"
    elif abs(net) >= 4.0:
        ceiling = "HIGH"
    elif abs(net) >= 2.0:
        ceiling = "MEDIUM"
    else:
        ceiling = "LOW"

    return {
        "net_score": net, "expected_bias": expected_bias, "contradiction": contradiction,
        "no_liquidity": no_liquidity,
        "confidence_ceiling": ceiling, "scored_evidence": scored,
    }


# --------------------------------------------------------------------------- #
# Trade plan                                                                   #
# --------------------------------------------------------------------------- #
def validate_trade_plan(plan, thesis=None, facts: Optional[dict] = None
                        ) -> ContractValidation:
    r = ContractValidation()
    d = plan.to_dict() if isinstance(plan, TradePlan) else (plan or {})
    p = TradePlan.from_dict(d)

    _enum(p.verdict, VERDICTS, "trade_plan.verdict", "SYN_BAD_VERDICT", r)

    if p.verdict == "WAIT":
        if not p.recall or not isinstance((p.recall or {}).get("events"), list) \
                or not p.recall["events"]:
            r.add("syntactic", "SYN_WAIT_MISSING_RECALL",
                  "a WAIT trade_plan requires a non-empty recall.events", "trade_plan.recall")
        else:
            for msg in validate_predicate_list(p.recall["events"], "trade_plan.recall.events"):
                r.add("syntactic", "SYN_BAD_PREDICATE", msg, "trade_plan.recall.events")
    elif p.verdict == "SETUP":
        _syn_setup(p, r)

    if p.verdict in VERDICTS and facts is not None:
        _semantic_plan(p, facts, r)
    if p.verdict == "SETUP" and thesis is not None:
        _cross_level(p, thesis, r)
    return r


def _syn_setup(p: TradePlan, r: ContractValidation) -> None:
    # entry present, direction valid, at least one mechanism with a closed kind.
    entry = p.entry or {}
    _enum(entry.get("direction"), DIRECTIONS, "trade_plan.entry.direction",
          "SYN_BAD_DIRECTION", r)
    mechs = entry.get("mechanisms")
    if not isinstance(mechs, list) or not mechs:
        r.add("syntactic", "SYN_SETUP_MISSING_MECHANISM",
              "a SETUP requires at least one entry mechanism", "trade_plan.entry.mechanisms")
    else:
        for i, m in enumerate(mechs):
            where = f"trade_plan.entry.mechanisms[{i}]"
            if not isinstance(m, dict) or m.get("kind") not in ENTRY_MECHANISMS:
                r.add("syntactic", "SYN_UNKNOWN_MECHANISM",
                      f"entry mechanism kind {(m or {}).get('kind')!r} not in "
                      f"{sorted(ENTRY_MECHANISMS)}", where)
            for msg in validate_predicate_list((m or {}).get("valid_while"),
                                               f"{where}.valid_while"):
                r.add("syntactic", "SYN_BAD_PREDICATE", msg, where)

    # stop with a numeric price is required.
    if not isinstance(p.stop, dict) or not isinstance(p.stop.get("price"), (int, float)):
        r.add("syntactic", "SYN_SETUP_MISSING_STOP",
              "a SETUP requires stop.price (number)", "trade_plan.stop")

    # mandatory on_dol_falsified (refinement locked 2026-07-11).
    odf = p.on_dol_falsified
    if not isinstance(odf, dict) or odf.get("action") not in DOL_FALSIFIED_ACTIONS:
        r.add("syntactic", "SYN_MISSING_ON_DOL_FALSIFIED",
              f"a SETUP requires on_dol_falsified.action in {sorted(DOL_FALSIFIED_ACTIONS)}",
              "trade_plan.on_dol_falsified")

    # exit management mechanisms must use closed kinds.
    exit_blk = p.exit or {}
    for i, m in enumerate(exit_blk.get("management") or []):
        where = f"trade_plan.exit.management[{i}]"
        if not isinstance(m, dict) or m.get("kind") not in MGMT_MECHANISMS:
            r.add("syntactic", "SYN_UNKNOWN_MECHANISM",
                  f"management kind {(m or {}).get('kind')!r} not in "
                  f"{sorted(MGMT_MECHANISMS)}", where)
        for msg in validate_predicate_list((m or {}).get("when"), f"{where}.when"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, where)

    for msg in validate_predicate_list(p.setup_falsified_if, "trade_plan.setup_falsified_if"):
        r.add("syntactic", "SYN_BAD_PREDICATE", msg, "trade_plan.setup_falsified_if")

    if isinstance(p.breakeven, dict):
        for msg in validate_predicate_list(p.breakeven.get("raise_to_be_if"),
                                           "trade_plan.breakeven.raise_to_be_if"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, "trade_plan.breakeven")


def _semantic_plan(p: TradePlan, facts: dict, r: ContractValidation) -> None:
    levels = facts.get("levels") or {}
    now_price = facts.get("now_price")

    # every level referenced by the plan's predicates must exist in the facts.
    names = set()
    names |= set(_iter_predicate_levels(p.setup_falsified_if))
    for m in (p.entry or {}).get("mechanisms") or []:
        names |= set(_iter_predicate_levels((m or {}).get("valid_while") or []))
    if p.recall:
        names |= set(_iter_predicate_levels((p.recall or {}).get("events") or []))
    for name in sorted(names):
        if name not in levels:
            r.add("semantic", "SEM_LEVEL_NOT_IN_FACTS",
                  f"level '{name}' referenced by the plan is not present in the facts",
                  "trade_plan")

    # TARGET CONTRACT: entry target must exist, be unswept/undepleted, correct side.
    target = ((p.exit or {}).get("target")) if p.is_setup() else None
    if isinstance(target, dict) and target.get("level"):
        name = target["level"]
        lvl = levels.get(name)
        if lvl is None:
            r.add("semantic", "SEM_TARGET_NOT_IN_FACTS",
                  f"exit target level '{name}' is not present in the facts",
                  "trade_plan.exit.target")
        elif isinstance(lvl, dict) and lvl.get("swept") and lvl.get("depleted"):
            r.add("semantic", "SEM_TARGET_SWEPT_DEPLETED",
                  f"exit target level '{name}' is already swept and depleted (spent pool)",
                  "trade_plan.exit.target")
        direction = (p.entry or {}).get("direction")
        price = target.get("price")
        if isinstance(now_price, (int, float)) and isinstance(price, (int, float)):
            if direction == "LONG" and price < now_price - _EPS:
                r.add("semantic", "SEM_TARGET_WRONG_SIDE",
                      f"LONG but target {price} is below current price {now_price}",
                      "trade_plan.exit.target")
            elif direction == "SHORT" and price > now_price + _EPS:
                r.add("semantic", "SEM_TARGET_WRONG_SIDE",
                      f"SHORT but target {price} is above current price {now_price}",
                      "trade_plan.exit.target")


def _cross_level(p: TradePlan, thesis, r: ContractValidation) -> None:
    """Spec §6 cross-level consistency, evaluated against a price-only hypothetical view."""
    t = thesis if isinstance(thesis, Thesis) else Thesis.from_dict(thesis or {})

    stop_price = (p.stop or {}).get("price")
    if isinstance(stop_price, (int, float)) and t.falsified_if:
        if eval_any(t.falsified_if, MarketView.price_only(float(stop_price))):
            r.add("cross_level", "XL_STOP_TRIPS_THESIS_FALSIFICATION",
                  f"stop price {stop_price} would itself satisfy a thesis falsified_if "
                  "predicate (getting stopped out == thesis death — stop is redundant)",
                  "trade_plan.stop")

    target = ((p.exit or {}).get("target")) or {}
    target_price = target.get("price")
    if isinstance(target_price, (int, float)):
        tp = float(target_price)
        # The DOL-touch case. EXHAUSTION REMOVED 2026-08-29: reaching the DOL *is* the
        # exhaustion, so the two old checks (a thesis `exhausted_if` predicate, and the DOL
        # itself) collapse into this one. A strict price_beyond cannot fire at equality, so
        # a strict price_beyond can't fire at equality — the target must sit STRICTLY before
        # the DOL, never AT it (targeting the exhaustion draw itself).
        dol_price = (t.dol or {}).get("price")
        if t.is_directional() and isinstance(dol_price, (int, float)) \
                and abs(tp - float(dol_price)) <= _EPS:
            r.add("cross_level", "XL_TARGET_AT_DOL",
                  f"exit target {target_price} equals the thesis DOL price {dol_price} "
                  "(the exhaustion draw) — target must sit before the DOL",
                  "trade_plan.exit.target")


def _iter_predicate_levels(preds) -> set:
    out: set = set()
    for p in preds or []:
        out |= referenced_levels(p)
    return out


def validate_contracts(thesis=None, plan=None, facts: Optional[dict] = None
                       ) -> ContractValidation:
    """Validate a thesis and/or a dependent plan together, sharing the facts."""
    r = ContractValidation()
    if thesis is not None:
        r.violations.extend(validate_thesis(thesis, facts).violations)
    if plan is not None:
        r.violations.extend(validate_trade_plan(plan, thesis=thesis, facts=facts).violations)
    return r


# --------------------------------------------------------------------------- #
# Menu-membership audit (plan 11 Phase 3) — telemetry, not a gate.             #
# --------------------------------------------------------------------------- #
def _canon(pred) -> str:
    """Canonical form of a predicate for exact menu matching: numbers normalised to
    float (so 20000 == 20000.0), keys sorted, composites recursed."""
    def norm(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(pred), sort_keys=True)


def _menu_index(facts: Optional[dict], direction: Optional[str]) -> dict:
    """canonical-predicate -> menu id, for the thesis direction's predicate menu."""
    if not direction:
        return {}
    menus = (facts or {}).get("menus") or {}
    entries = (menus.get("predicates") or {}).get(direction) or []
    idx = {}
    for e in entries:
        pred = e.get("predicate")
        if isinstance(pred, dict):
            idx.setdefault(_canon(pred), e.get("id"))
    return idx


def classify_predicates(thesis, facts: Optional[dict] = None) -> dict:
    """Tag every thesis predicate as `menu_hit` (params exactly match a menu entry for the
    thesis direction) or `escape_hatch` (schema-valid + facts-grounded, but off-menu). This
    is PURE TELEMETRY for iteration — it adds no rejections; the schema (generation-time) and
    validate_thesis (grounding) are the gates. A failsafe/NEUTRAL thesis (no direction, no
    predicates) yields empty tags. Also tags whether the chosen DOL is a DOL-menu entry.

    Returns {direction, dol_menu_hit, dol_menu_id, fields{falsified_if/recall:
    [{predicate, tag, menu_id}]}, n_menu_hit, n_escape_hatch, menu_hit_ratio}.
    """
    d = thesis.to_dict() if isinstance(thesis, Thesis) else (thesis or {})
    t = Thesis.from_dict(d)
    direction = t.bias if t.bias in ("UP", "DOWN") else None
    idx = _menu_index(facts, direction)

    fields = {}
    n_hit = n_esc = 0
    field_map = {
        "falsified_if": t.falsified_if,
        "recall": ((t.recall or {}).get("events") or []),
    }
    for fname, preds in field_map.items():
        tagged = []
        for p in preds or []:
            menu_id = idx.get(_canon(p)) if isinstance(p, dict) else None
            if menu_id is not None:
                tag, n_hit = "menu_hit", n_hit + 1
            else:
                tag, n_esc = "escape_hatch", n_esc + 1
            tagged.append({"predicate": p, "tag": tag, "menu_id": menu_id})
        fields[fname] = tagged

    # DOL menu membership (telemetry only).
    dol_menu_hit, dol_menu_id = False, None
    if direction and isinstance(t.dol, dict) and t.dol.get("level"):
        dol_menu = ((facts or {}).get("menus") or {}).get("dol") or {}
        for e in dol_menu.get(direction) or []:
            if e.get("level") == t.dol.get("level"):
                dol_menu_hit, dol_menu_id = True, e.get("id")
                break

    total = n_hit + n_esc
    return {
        "direction": direction,
        "dol_menu_hit": dol_menu_hit, "dol_menu_id": dol_menu_id,
        "fields": fields,
        "n_menu_hit": n_hit, "n_escape_hatch": n_esc,
        "menu_hit_ratio": round(n_hit / total, 4) if total else None,
    }
