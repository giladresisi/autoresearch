"""Phase-1 schema + cross-level contract validation tests (plan §Phase 1)."""

from schemas import Thesis, TradePlan
from validate_contracts import score_thesis_evidence, validate_thesis, validate_trade_plan


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
FACTS = {
    "now_price": 19800.0,
    "levels": {
        "prev_day_high": {"price": 20000.0, "side": "high", "swept": False, "depleted": False},
        "intraday_high": {"price": 19950.0, "side": "high", "swept": False, "depleted": False},
        "prev_day_low": {"price": 19500.0, "side": "low", "swept": False, "depleted": False},
        "spent_pool": {"price": 19960.0, "side": "high", "swept": True, "depleted": True},
    },
}


def valid_thesis() -> dict:
    return {
        "bias": "UP", "regime": "TREND", "confidence": "HIGH",
        "dol": {"level": "prev_day_high", "price": 20000.0},
        "falsified_if": [{"type": "n_closes_beyond", "price": 19500, "side": "below",
                          "tf": "5m", "n": 2}],
        "exhausted_if": [{"type": "price_beyond", "price": 20000, "side": "above"}],
        "recall": {"events": [], "max_age_min": 60},
        "reasoning": "audit",
    }


def valid_setup() -> dict:
    return {
        "plan_id": "pl_1", "thesis_id": "th_1", "verdict": "SETUP",
        "entry": {"direction": "LONG",
                  "mechanisms": [{"kind": "confirmation_bar", "params": {"tf": "5m"},
                                  "valid_while": []}]},
        "stop": {"price": 19600.0},                 # above the 19500 falsification level
        "breakeven": {"raise_to_be_if": []},
        "exit": {"target": {"level": "intraday_high", "price": 19950.0},
                 "management": [{"kind": "raise_to_breakeven", "params": {}, "when": []}]},
        "setup_falsified_if": [],
        "setup_exhausted_if": [],
        "on_dol_falsified": {"action": "MARKET_CLOSE", "params": {}},
        "recall": None, "reasoning": "audit",
    }


# --------------------------------------------------------------------------- #
# Schema tests                                                                 #
# --------------------------------------------------------------------------- #
def test_valid_thesis_accepted():
    assert validate_thesis(valid_thesis(), FACTS).ok


def test_valid_setup_accepted():
    r = validate_trade_plan(valid_setup(), thesis=valid_thesis(), facts=FACTS)
    assert r.ok, r.messages()


def test_unknown_mechanism_kind_rejected():
    p = valid_setup()
    p["entry"]["mechanisms"][0]["kind"] = "telepathy"
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SYN_UNKNOWN_MECHANISM" in r.codes()


def test_setup_missing_on_dol_falsified_rejected():
    p = valid_setup()
    p["on_dol_falsified"] = None
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SYN_MISSING_ON_DOL_FALSIFIED" in r.codes()


def test_setup_bad_on_dol_action_rejected():
    p = valid_setup()
    p["on_dol_falsified"] = {"action": "PRAY", "params": {}}
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SYN_MISSING_ON_DOL_FALSIFIED" in r.codes()


def test_wait_with_no_recall_rejected():
    plan = {"verdict": "WAIT", "recall": None, "reasoning": "x"}
    r = validate_trade_plan(plan, thesis=valid_thesis(), facts=FACTS)
    assert "SYN_WAIT_MISSING_RECALL" in r.codes()


def test_wait_with_recall_accepted():
    plan = {"verdict": "WAIT",
            "recall": {"events": [{"type": "level_swept", "name": "prev_day_low"}],
                       "max_age_min": 30},
            "reasoning": "x"}
    r = validate_trade_plan(plan, thesis=valid_thesis(), facts=FACTS)
    assert r.ok, r.messages()


def test_setup_bad_predicate_rejected():
    p = valid_setup()
    p["setup_falsified_if"] = [{"type": "not_a_predicate"}]
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SYN_BAD_PREDICATE" in r.codes()


def test_thesis_directional_missing_dol_rejected():
    t = valid_thesis()
    t["dol"] = None
    assert "SYN_DIRECTIONAL_MISSING_DOL" in validate_thesis(t, FACTS).codes()


def test_thesis_bad_bias_rejected():
    t = valid_thesis()
    t["bias"] = "SIDEWAYS"
    assert "SYN_BAD_BIAS" in validate_thesis(t, FACTS).codes()


# --------------------------------------------------------------------------- #
# Semantic tests                                                               #
# --------------------------------------------------------------------------- #
def test_thesis_level_not_in_facts_rejected():
    t = valid_thesis()
    t["falsified_if"] = [{"type": "level_swept", "name": "ghost_level"}]
    assert "SEM_LEVEL_NOT_IN_FACTS" in validate_thesis(t, FACTS).codes()


def test_thesis_dol_wrong_side_down_rejected():
    # DOWN thesis but the DOL sits ABOVE current price (19800) → SEM_DOL_WRONG_SIDE. This is
    # the 07-02 th_02 class: a "low" pool above price is already reached, so the exhaustion is
    # trivially satisfied and the thesis "completes" bogusly.
    t = {
        "bias": "DOWN", "regime": "RANGE", "confidence": "MEDIUM",
        "dol": {"level": "prev_day_high", "price": 20000.0},         # above 19800 — wrong side
        "falsified_if": [],
        "exhausted_if": [{"type": "price_beyond", "price": 20000, "side": "below"}],
        "recall": {"events": [], "max_age_min": 60}, "reasoning": "x",
    }
    assert "SEM_DOL_WRONG_SIDE" in validate_thesis(t, FACTS).codes()


def test_thesis_dol_wrong_side_up_rejected():
    t = valid_thesis()                                              # UP
    t["dol"] = {"level": "prev_day_low", "price": 19500.0}          # below 19800 — wrong for UP
    assert "SEM_DOL_WRONG_SIDE" in validate_thesis(t, FACTS).codes()


def test_thesis_dol_correct_side_accepted():
    # A DOWN thesis whose DOL sits below price is on the correct side (no SEM_DOL_WRONG_SIDE).
    t = {
        "bias": "DOWN", "regime": "RANGE", "confidence": "MEDIUM",
        "dol": {"level": "prev_day_low", "price": 19500.0},         # below 19800 — correct
        "falsified_if": [],
        "exhausted_if": [{"type": "price_beyond", "price": 19500, "side": "below"}],
        "recall": {"events": [], "max_age_min": 60}, "reasoning": "x",
    }
    r = validate_thesis(t, FACTS)
    assert "SEM_DOL_WRONG_SIDE" not in r.codes()
    assert r.ok, r.messages()


def test_target_wrong_side_rejected():
    p = valid_setup()
    p["exit"]["target"] = {"level": "prev_day_low", "price": 19500.0}   # below price, LONG
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SEM_TARGET_WRONG_SIDE" in r.codes()


def test_target_swept_depleted_rejected():
    p = valid_setup()
    p["exit"]["target"] = {"level": "spent_pool", "price": 19960.0}
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "SEM_TARGET_SWEPT_DEPLETED" in r.codes()


# --------------------------------------------------------------------------- #
# Cross-level tests                                                            #
# --------------------------------------------------------------------------- #
def test_stop_trips_thesis_falsification_rejected():
    p = valid_setup()
    p["stop"] = {"price": 19400.0}          # below 19500 → trips n_closes_beyond(below)
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "XL_STOP_TRIPS_THESIS_FALSIFICATION" in r.codes()


def test_target_equal_to_dol_exhaustion_rejected():
    p = valid_setup()
    p["exit"]["target"] = {"level": "prev_day_high", "price": 20000.0}   # == DOL price
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "XL_TARGET_AT_DOL" in r.codes()


def test_target_beyond_exhaustion_rejected():
    p = valid_setup()
    # exhausted_if fires strictly above 20000; a target past it trips the predicate check.
    p["exit"]["target"] = {"level": "prev_day_high", "price": 20100.0}
    r = validate_trade_plan(p, thesis=valid_thesis(), facts=FACTS)
    assert "XL_TARGET_IS_THESIS_EXHAUSTION" in r.codes()


def test_valid_pair_accepted():
    r = validate_trade_plan(valid_setup(), thesis=valid_thesis(), facts=FACTS)
    assert r.ok, r.messages()


def test_dataclass_roundtrip():
    t = Thesis.from_dict(valid_thesis())
    assert Thesis.from_dict(t.to_dict()).bias == "UP"
    p = TradePlan.from_dict(valid_setup())
    assert p.is_setup() and p.direction() == "LONG"
    assert TradePlan.from_dict(p.to_dict()).plan_id == "pl_1"
    # to_dict is stable (no mutation of the source).
    src = valid_setup()
    TradePlan.from_dict(src).to_dict()
    assert src == valid_setup()


# --------------------------------------------------------------------------- #
# Issuance-time consistency gate (2026-07-02 08:00 bug)                        #
# --------------------------------------------------------------------------- #
def test_falsified_if_already_true_rejected():
    t = valid_thesis()
    # now_price=19800; already above 19700 on the 'above' side -> already true.
    t["falsified_if"] = [{"type": "n_closes_beyond", "price": 19700, "side": "above",
                          "tf": "5m", "n": 2}]
    assert "XL_FALSIFIED_IF_ALREADY_TRUE" in validate_thesis(t, FACTS).codes()


def test_exhausted_if_already_true_rejected():
    t = valid_thesis()
    t["exhausted_if"] = [{"type": "price_beyond", "price": 19700, "side": "above"}]
    assert "XL_EXHAUSTED_IF_ALREADY_TRUE" in validate_thesis(t, FACTS).codes()


def test_falsified_if_not_yet_true_accepted():
    r = validate_thesis(valid_thesis(), FACTS)   # falsifies below 19500; price is 19800
    assert "XL_FALSIFIED_IF_ALREADY_TRUE" not in r.codes()
    assert "XL_EXHAUSTED_IF_ALREADY_TRUE" not in r.codes()


# --------------------------------------------------------------------------- #
# Evidence ledger — syntactic/semantic validation                              #
# --------------------------------------------------------------------------- #
def _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", tf="1h",
        direction="accept", mature=True):
    return {"criterion": criterion, "asset": asset, "level": level, "tier": tier,
            "tf": tf, "direction": direction, "mature": mature}


def test_evidence_item_bad_enum_rejected():
    t = valid_thesis()
    t["evidence"] = [_ev(criterion="P9")]
    assert "SYN_BAD_EVIDENCE_ITEM" in validate_thesis(t, FACTS).codes()


def test_evidence_item_level_not_in_facts_rejected():
    t = valid_thesis()
    t["evidence"] = [_ev(level="ghost_level")]
    assert "SEM_LEVEL_NOT_IN_FACTS" in validate_thesis(t, FACTS).codes()


# --------------------------------------------------------------------------- #
# score_thesis_evidence — pure arithmetic                                      #
# --------------------------------------------------------------------------- #
def test_score_evidence_high_accept_is_up():
    scoring = score_thesis_evidence([_ev(level="prev_day_high", direction="accept",
                                          tier="week", tf="4h")])
    assert scoring["net_score"] == 3.0     # 2.0 base * 1.5 (4h) * 1.0 (week)
    assert scoring["expected_bias"] == "UP"
    assert scoring["scored_evidence"][0]["side"] == "UP"


def test_score_evidence_low_reject_is_up_not_down():
    # The exact 2026-07-02 08:00 bug shape: rejecting a LOW sweep is bullish, not bearish.
    scoring = score_thesis_evidence([_ev(level="prev_day_low", direction="reject")])
    assert scoring["expected_bias"] == "UP"
    assert scoring["scored_evidence"][0]["side"] == "UP"


def test_score_evidence_low_accept_is_down():
    scoring = score_thesis_evidence([_ev(level="prev_day_low", direction="accept")])
    assert scoring["expected_bias"] == "DOWN"


def test_score_evidence_immature_scores_zero():
    scoring = score_thesis_evidence([_ev(level="prev_day_high", direction="accept",
                                          mature=False)])
    assert scoring["net_score"] == 0.0
    assert scoring["expected_bias"] == "NEUTRAL"
    assert scoring["scored_evidence"][0]["points"] == 0.0


def test_score_evidence_tier_ladder():
    session = score_thesis_evidence([_ev(tier="session")])["net_score"]
    day = score_thesis_evidence([_ev(tier="day")])["net_score"]
    week = score_thesis_evidence([_ev(tier="week")])["net_score"]
    assert 0 < session < day < week


def test_score_evidence_contradiction_caps_ceiling():
    # Same level, MNQ accepts (UP) while MES rejects (DOWN) -> a live P1 contradiction.
    # Net score is thin (1 UP week item - 1 DOWN day item), so even a real lean tops at MEDIUM.
    ev = [_ev(asset="MNQ", level="prev1_day_high", direction="accept", tier="week", tf="4h"),
          _ev(asset="MES", level="prev1_day_high", direction="reject", tier="day", tf="1h")]
    scoring = score_thesis_evidence(ev)
    assert scoring["contradiction"] is True
    assert scoring["confidence_ceiling"] in ("MEDIUM", "LOW")
    assert scoring["confidence_ceiling"] != "HIGH"


def test_score_evidence_no_contradiction_allows_high():
    ev = [_ev(asset="MNQ", tier="week", tf="4h"),
          _ev(asset="MES", tier="week", tf="4h")]
    scoring = score_thesis_evidence(ev)
    assert scoring["contradiction"] is False
    assert scoring["confidence_ceiling"] == "HIGH"


# --------------------------------------------------------------------------- #
# P5 (FVG-fill) — _fvg_side sign derivation (plan 15 Task 4)                    #
# --------------------------------------------------------------------------- #
def test_score_p5_bull_visited_accept_is_up():
    # example #10: MES 1hr bull zone held (accept) -> bullish continuation.
    item = _ev(criterion="P5", asset="MES",
               level="MES 1hr 2026-07-14 00:00:00-04:00 bull", direction="accept")
    scoring = score_thesis_evidence([item])
    assert scoring["scored_evidence"][0]["side"] == "UP"
    assert scoring["expected_bias"] == "UP"


def test_score_p5_bear_visited_accept_is_down():
    item = _ev(criterion="P5", asset="MNQ",
               level="MNQ 4hr 2026-07-14 00:00:00-04:00 bear", direction="accept")
    assert score_thesis_evidence([item])["expected_bias"] == "DOWN"


def test_score_p5_bull_reject_is_down():
    # A bull zone that was violated (closed through) reverses -> bearish.
    item = _ev(criterion="P5", asset="MES",
               level="MES 1hr 2026-07-14 00:00:00-04:00 bull", direction="reject")
    assert score_thesis_evidence([item])["expected_bias"] == "DOWN"


def test_score_p5_no_kind_scores_no_side():
    # A level string with neither bull nor bear -> no sign, contributes nothing.
    item = _ev(criterion="P5", level="some 1hr zone", direction="accept")
    scoring = score_thesis_evidence([item])
    assert scoring["scored_evidence"][0]["side"] is None
    assert scoring["net_score"] == 0.0


def test_p5_evidence_level_exempt_from_facts_check():
    # A P5 item's level is an FVG-zone id, not a named price level -> not SEM_LEVEL_NOT_IN_FACTS.
    t = valid_thesis()
    t["evidence"] = [_ev(criterion="P5", asset="MES",
                         level="MES 1hr 2026-07-14 00:00:00-04:00 bull", direction="accept")]
    assert "SEM_LEVEL_NOT_IN_FACTS" not in validate_thesis(t, FACTS).codes()


# --------------------------------------------------------------------------- #
# P-resolution — immature pending_resolution surfacing (plan 15 Task 7)         #
# --------------------------------------------------------------------------- #
def test_pending_resolution_accepted_when_immature():
    from schemas import build_thesis_schema
    from jsonschema import validate as js_validate
    item = _ev(criterion="P1", level="prev_day_high", direction="accept", mature=False)
    item["pending_resolution"] = {"resolves_tf": "1h",
                                  "resolves_at": "2026-07-14 02:00:00-04:00",
                                  "implied_direction_if_confirmed": "UP"}
    t = valid_thesis()
    t["evidence"] = [item]
    js_validate(t, build_thesis_schema())          # schema accepts the optional object
    assert "SYN_BAD_EVIDENCE_ITEM" not in validate_thesis(t, FACTS).codes()


def test_pending_resolution_omitted_when_mature_is_fine():
    t = valid_thesis()
    t["evidence"] = [_ev(level="prev_day_high", direction="accept", mature=True)]
    assert "SYN_BAD_EVIDENCE_ITEM" not in validate_thesis(t, FACTS).codes()


def test_pending_resolution_stale_is_warning_not_rejection():
    item = _ev(criterion="P1", level="prev_day_high", direction="accept", mature=False)
    item["pending_resolution"] = {"resolves_tf": "1h",
                                  "resolves_at": "2026-07-14 00:00:00-04:00",  # BEFORE now
                                  "implied_direction_if_confirmed": "UP"}
    t = valid_thesis()
    t["bias"] = "NEUTRAL"                            # immature item nets zero -> NEUTRAL
    t["evidence"] = [item]
    facts = {**FACTS, "now": "2026-07-14 01:00:00-04:00"}
    r = validate_thesis(t, facts)
    assert "AUD_PENDING_RESOLUTION_STALE" in {w.code for w in r.warnings}
    assert "AUD_PENDING_RESOLUTION_STALE" not in r.codes()   # not a hard violation


# --------------------------------------------------------------------------- #
# P3/P4 — equilibrium & reclaim code-derivation (plan 15 Task 6)               #
# --------------------------------------------------------------------------- #
def test_score_p3_above_mid_accept_is_up():
    item = _ev(criterion="P3", level="daily_mid", direction="accept")
    assert score_thesis_evidence([item])["expected_bias"] == "UP"


def test_score_p3_below_mid_reject_is_down():
    item = _ev(criterion="P3", level="weekly_mid", direction="reject")
    assert score_thesis_evidence([item])["expected_bias"] == "DOWN"


def test_score_p4_reclaim_high_accept_is_up():
    # reclaim of the mid holding above -> bullish (accept on a _high level).
    item = _ev(criterion="P4", level="daily_mid_high", direction="accept")
    assert score_thesis_evidence([item])["expected_bias"] == "UP"


def test_score_p4_failed_reclaim_high_reject_is_down():
    item = _ev(criterion="P4", level="weekly_mid_high", direction="reject")
    assert score_thesis_evidence([item])["expected_bias"] == "DOWN"


def test_p3_p4_levels_exempt_from_facts_check():
    t = valid_thesis()
    t["bias"] = "UP"
    t["evidence"] = [_ev(criterion="P3", level="daily_mid", direction="accept"),
                     _ev(criterion="P4", level="weekly_mid_high", direction="accept")]
    codes = validate_thesis(t, FACTS).codes()
    assert "SEM_LEVEL_NOT_IN_FACTS" not in codes


# --------------------------------------------------------------------------- #
# P2 exhausted override — zeroed like the maturity gate (plan 15 Task 5)        #
# --------------------------------------------------------------------------- #
def test_exhausted_item_scores_zero():
    item = _ev(criterion="P2", level="prev1_day_low", direction="reject", tier="week", tf="4h")
    base = score_thesis_evidence([item])["net_score"]
    assert base != 0.0                                  # would score if not exhausted
    item_exh = {**item, "exhausted": True}
    scoring = score_thesis_evidence([item_exh])
    assert scoring["net_score"] == 0.0
    assert scoring["scored_evidence"][0]["points"] == 0.0
    assert scoring["expected_bias"] == "NEUTRAL"


def test_exhausted_and_immature_together_no_error_no_double_penalty():
    item = _ev(criterion="P2", level="prev1_day_high", direction="accept",
               mature=False, tier="day", tf="1h")
    item["exhausted"] = True
    scoring = score_thesis_evidence([item])            # both flags -> zero, no exception
    assert scoring["scored_evidence"][0]["points"] == 0.0
    assert scoring["net_score"] == 0.0


# --------------------------------------------------------------------------- #
# suppressed_p1_levels — nested prevN / duplicate-simultaneous-sweep levels     #
# (thesis.md §2.1b/§2.1d): excluded from fresh P1 evidence, NEVER from P2.      #
# --------------------------------------------------------------------------- #
def test_suppressed_p1_level_scores_zero():
    item = _ev(criterion="P1", level="prev3_day_low", direction="reject", tier="day")
    base = score_thesis_evidence([item])["net_score"]
    assert base != 0.0                                  # would score if not suppressed
    scoring = score_thesis_evidence(
        [item], suppressed_p1_levels={"MNQ": ["prev3_day_low"]})
    assert scoring["scored_evidence"][0]["points"] == 0.0
    assert scoring["net_score"] == 0.0


def test_suppressed_level_p2_item_unaffected():
    # the SAME level, the SAME suppression set, but a P2 item — must score normally,
    # since a divergence that already fired stays valid regardless of P1 nesting.
    item = _ev(criterion="P2", level="prev3_day_low", direction="reject", tier="day")
    scoring = score_thesis_evidence(
        [item], suppressed_p1_levels={"MNQ": ["prev3_day_low"]})
    assert scoring["scored_evidence"][0]["points"] > 0.0
    assert scoring["net_score"] != 0.0


def test_suppressed_p1_levels_scoped_per_asset():
    # suppression for MNQ must not bleed onto MES's own reading of the same level name.
    item = _ev(criterion="P1", asset="MES", level="prev3_day_low", direction="reject",
              tier="day")
    scoring = score_thesis_evidence(
        [item], suppressed_p1_levels={"MNQ": ["prev3_day_low"]})
    assert scoring["scored_evidence"][0]["points"] > 0.0


def test_suppressed_p1_level_excluded_from_contradiction_tally():
    # a suppressed (zeroed) P1 item must not count toward §6's cross-asset contradiction
    # check either -- it carries no real information once suppressed.
    items = [
        _ev(criterion="P1", asset="MNQ", level="prev3_day_low", direction="reject", tier="day"),
        _ev(criterion="P1", asset="MES", level="prev3_day_low", direction="accept", tier="day"),
    ]
    scoring = score_thesis_evidence(
        items, suppressed_p1_levels={"MNQ": ["prev3_day_low"]})
    assert scoring["contradiction"] is False


# --------------------------------------------------------------------------- #
# ARI_THESIS_BIAS — declared bias vs. computed net score                       #
# --------------------------------------------------------------------------- #
def test_bias_inconsistent_with_evidence_rejected():
    t = valid_thesis()   # bias UP
    t["evidence"] = [_ev(level="prev_day_low", direction="accept")]   # nets DOWN
    assert "ARI_THESIS_BIAS" in validate_thesis(t, FACTS).codes()


def test_bias_consistent_with_evidence_accepted():
    t = valid_thesis()   # bias UP
    t["evidence"] = [_ev(level="prev_day_high", direction="accept")]  # nets UP
    r = validate_thesis(t, FACTS)
    assert "ARI_THESIS_BIAS" not in r.codes()


def test_empty_evidence_exempt_from_bias_check():
    t = valid_thesis()
    t["evidence"] = []
    assert "ARI_THESIS_BIAS" not in validate_thesis(t, FACTS).codes()
