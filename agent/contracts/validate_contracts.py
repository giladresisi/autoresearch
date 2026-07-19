"""Deterministic validator for the v2 thesis / trade-plan contracts (spec §2, §6).

Three layers, mirroring the v1 `agent/validator.py` shape:

  1. Syntactic  — enums, required fields per form, closed mechanism kinds, mandatory
                  `on_dol_falsified` on a SETUP, WAIT requires a recall, every predicate
                  in the closed vocabulary.
  2. Semantic   — every level referenced (dol, target, predicates) exists in the facts;
                  the entry target is unswept/undepleted and on the correct side of price
                  (carries the v1 TARGET CONTRACT).
  3. Cross-level — the stop must NOT satisfy any thesis `falsified_if` predicate, and the
                  exit target must NOT satisfy any thesis `exhausted_if` predicate (spec §6).

Public API mirrors `validator.py`: `validate_thesis`, `validate_trade_plan`, and a
combined `validate_contracts`. All return a `ContractValidation` (list of violations).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from schemas import (
    BIASES, CONFIDENCES, DAILY_REGIMES, DIRECTIONS, DOL_FALSIFIED_ACTIONS,
    ENTRY_MECHANISMS, EVIDENCE_ASSETS, EVIDENCE_CRITERIA, EVIDENCE_DIRECTIONS,
    EVIDENCE_TFS, EVIDENCE_TIERS, MGMT_MECHANISMS, VERDICTS, Thesis, TradePlan,
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

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> set:
        return {v.code for v in self.violations}

    def messages(self) -> list:
        return [str(v) for v in self.violations]

    def add(self, layer: str, code: str, message: str, where: str = "") -> None:
        self.violations.append(ContractViolation(layer, code, message, where))


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
    for msg in validate_predicate_list(t.exhausted_if, "thesis.exhausted_if"):
        r.add("syntactic", "SYN_BAD_PREDICATE", msg, "thesis.exhausted_if")

    # recall: when present, its events must be a valid predicate list.
    if t.recall is not None:
        for msg in validate_predicate_list((t.recall or {}).get("events"),
                                           "thesis.recall.events"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, "thesis.recall.events")

    for i, item in enumerate(t.evidence or []):
        _validate_evidence_item(item, f"thesis.evidence[{i}]", r)

    # ARI_THESIS_BIAS (§4/§9): the declared bias must match the sign of the code-computed
    # net score over the evidence ledger — the SAME retry-not-override pattern as
    # daily_trend/next_move's direction-vs-N check (agent/validator.py _check_arithmetic).
    # A thin/empty ledger (nothing declared yet, e.g. the NEUTRAL failsafe) is exempt.
    if t.evidence:
        scoring = score_thesis_evidence(t.evidence)
        if t.bias in BIASES and t.bias != scoring["expected_bias"]:
            r.add("arithmetic", "ARI_THESIS_BIAS",
                  f"bias '{t.bias}' inconsistent with the evidence ledger's net score "
                  f"{scoring['net_score']} (expected '{scoring['expected_bias']}')",
                  "thesis.bias")

    if facts is not None:
        _semantic_thesis(t, facts, r)
    return r


def _validate_evidence_item(item, where: str, r: ContractValidation) -> None:
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


def _semantic_thesis(t: Thesis, facts: dict, r: ContractValidation) -> None:
    levels = facts.get("levels") or {}
    names = set(_iter_predicate_levels(t.falsified_if)) \
        | set(_iter_predicate_levels(t.exhausted_if))
    if t.recall:
        names |= set(_iter_predicate_levels((t.recall or {}).get("events") or []))
    if isinstance(t.dol, dict) and t.dol.get("level"):
        names.add(t.dol["level"])
    for item in t.evidence or []:
        if isinstance(item, dict) and isinstance(item.get("level"), str) and item["level"]:
            names.add(item["level"])
    for name in sorted(names):
        if name not in levels:
            r.add("semantic", "SEM_LEVEL_NOT_IN_FACTS",
                  f"level '{name}' referenced by the thesis is not present in the facts",
                  "thesis")

    # XL_FALSIFIED_IF_ALREADY_TRUE / XL_EXHAUSTED_IF_ALREADY_TRUE: a thesis whose own
    # falsified_if/exhausted_if already evaluates true against the CURRENT price is
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
        if t.exhausted_if and eval_any(t.exhausted_if, view):
            r.add("cross_level", "XL_EXHAUSTED_IF_ALREADY_TRUE",
                  f"exhausted_if would already be satisfied at the current price {now_price} "
                  "(thesis is already exhausted at issuance)", "thesis.exhausted_if")

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


# --------------------------------------------------------------------------- #
# Evidence-ledger scoring (decisions/thesis.md §2.1/§4/§6) — the model judges,      #
# code computes. Self-contained: recomputes from t.evidence alone (no dependency   #
# on run_agent.py having run first), the same way agent/validator.py's             #
# _check_arithmetic independently recomputes S/N rather than trusting the block.   #
# --------------------------------------------------------------------------- #
_TF_MULT = {"1h": 1.0, "4h": 1.5}          # thesis.md §4: 4hr scores more than 1hr
_TIER_MULT = {"session": 0.5, "day": 0.75, "week": 1.0}   # §4: week > day > session
_BASE_POINTS = 2.0                          # v1 seed, pending calibration (thesis.md §4)


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


def score_thesis_evidence(evidence: list) -> dict:
    """Pure computation over the model-declared P1/P2 evidence ledger: per-item points
    (tier x tf multiplier, zeroed if immature — enforcing the §3 maturity gate in code,
    not trust), the net score, the expected bias sign, and the §6 cross-asset
    contradiction cap on confidence. Returns {net_score, expected_bias, contradiction,
    confidence_ceiling, scored_evidence}. `scored_evidence` echoes each item with its
    computed `points`/`side` attached, for the audit trail."""
    scored = []
    net = 0.0
    by_level: dict = {}   # level -> {asset: direction}, for the §6 contradiction check
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        tf_mult = _TF_MULT.get(item.get("tf"), 0.0)
        tier_mult = _TIER_MULT.get(item.get("tier"), 0.0)
        mature = bool(item.get("mature"))
        points = round(_BASE_POINTS * tf_mult * tier_mult, 4) if mature else 0.0
        side = _evidence_side(item)
        if side == "UP":
            net += points
        elif side == "DOWN":
            net -= points
        scored.append({**item, "points": points, "side": side})

        level, asset, direction = item.get("level"), item.get("asset"), item.get("direction")
        if item.get("criterion") == "P1" and mature and level and asset:
            by_level.setdefault(level, {})[asset] = direction

    contradiction = any(len(set(d.values())) > 1 for d in by_level.values() if len(d) > 1)
    net = round(net, 4)
    if net > _EPS:
        expected_bias = "UP"
    elif net < -_EPS:
        expected_bias = "DOWN"
    else:
        expected_bias = "NEUTRAL"

    if contradiction:
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

    for fld in ("setup_falsified_if", "setup_exhausted_if"):
        for msg in validate_predicate_list(getattr(p, fld), f"trade_plan.{fld}"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, f"trade_plan.{fld}")

    if isinstance(p.breakeven, dict):
        for msg in validate_predicate_list(p.breakeven.get("raise_to_be_if"),
                                           "trade_plan.breakeven.raise_to_be_if"):
            r.add("syntactic", "SYN_BAD_PREDICATE", msg, "trade_plan.breakeven")


def _semantic_plan(p: TradePlan, facts: dict, r: ContractValidation) -> None:
    levels = facts.get("levels") or {}
    now_price = facts.get("now_price")

    # every level referenced by the plan's predicates must exist in the facts.
    names = set()
    for fld in ("setup_falsified_if", "setup_exhausted_if"):
        names |= set(_iter_predicate_levels(getattr(p, fld)))
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
        # (a) strict-beyond exhaustion terms: does price AT the target trip exhausted_if.
        if t.exhausted_if and eval_any(t.exhausted_if, MarketView.price_only(tp)):
            r.add("cross_level", "XL_TARGET_IS_THESIS_EXHAUSTION",
                  f"exit target {target_price} would satisfy a thesis exhausted_if "
                  "predicate (targeting the thesis exhaustion point)",
                  "trade_plan.exit.target")
        # (b) the DOL-touch case: exhausted_if is "typically DOL touch" (spec §2.1), which
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

    Returns {direction, dol_menu_hit, dol_menu_id, fields{falsified_if/exhausted_if/recall:
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
        "exhausted_if": t.exhausted_if,
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
