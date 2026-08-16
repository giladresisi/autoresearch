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
        "dol_rationale": "nearest eligible UP pool, prev_day_high",
        "dol": {"level": "prev_day_high", "price": 20000.0},
        "falsified_if_rationale": "close back below prev_day_low invalidates the UP read",
        "falsified_if": [{"type": "n_closes_beyond", "price": 19500, "side": "below",
                          "tf": "5m", "n": 2}],
        "exhausted_if_rationale": "the DOL itself is the exhaustion price",
        "exhausted_if": [{"type": "price_beyond", "price": 20000, "side": "above"}],
        "recall": {"events": [], "max_age_min": 60},
        # A directional (UP/DOWN) bias with an EMPTY evidence ledger is rejected
        # (ARI_THESIS_BIAS) — this fixture's bias is UP, so it needs a real, UP-consistent
        # item by default; tests that care about evidence override this explicitly.
        "evidence": [_ev()],
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
        "evidence": [_ev(level="prev_day_low", direction="accept")],   # accept-beyond a low = DOWN
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


def test_mid_tier_is_code_derived_not_model_declared():
    # 2026-07-20 root-cause: the model declared all 3 mid items at tier="session",
    # under-weighting real evidence -- P3 now uses a fixed point value per its own
    # identity (_P3_MID_POINTS), ignoring the declared tier entirely (2026-08-02:
    # no longer even a tf_mult x tier_mult computation, a flat 1.0/1.5).
    daily = _ev(criterion="P3", level="daily_mid", tier="session", tf="1h", direction="accept")
    weekly = _ev(criterion="P3", level="weekly_mid", tier="session", tf="1h", direction="reject")
    scored = score_thesis_evidence([daily, weekly])["scored_evidence"]
    assert scored[0]["points"] == 1.0    # fixed P3 daily_mid value, not session (0.5x) or day (0.75x)
    assert scored[1]["points"] == 1.5    # fixed P3 weekly_mid value, not session (0.5x) or week (1.0x)


def test_p4_mid_tier_also_code_derived():
    item = _ev(criterion="P4", level="weekly_mid_high", tier="session", tf="1h", direction="accept")
    scored = score_thesis_evidence([item])["scored_evidence"]
    assert scored[0]["points"] == 2.0    # week tier, not the declared session tier


def test_mid_tier_override_does_not_touch_p1_or_p2():
    # the override is scoped to P3/P4 only -- a P1/P2 item literally named "daily_mid"
    # (not a realistic level, but guards the scoping) keeps its declared tier as-is.
    item = _ev(criterion="P1", level="daily_mid", tier="session", tf="1h", direction="accept")
    scored = score_thesis_evidence([item])["scored_evidence"]
    assert scored[0]["points"] == 1.0    # session tier honored, no override applied


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
# P4-dominates-P3 (thesis.md §6 extension)                                     #
# --------------------------------------------------------------------------- #
def test_p4_reclaim_dominates_contradicting_p3_on_other_asset():
    # MNQ: HTF-confirmed failed reclaim ABOVE the weekly mid (bearish, DOWN).
    p4 = _ev(criterion="P4", asset="MNQ", level="weekly_mid_high", tier="week", tf="1h",
             direction="reject", mature=True)
    # MES: merely sits above its OWN weekly mid (bullish position, UP) -- contradicts MNQ's
    # dynamic P4 read at the same (weekly) mid.
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="1h",
             direction="accept", mature=True)
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] == 0.0
    assert scoring["expected_bias"] == "DOWN"   # only the P4 item's points count


def test_p4_reclaim_does_not_dominate_agreeing_p3():
    p4 = _ev(criterion="P4", asset="MNQ", level="weekly_mid_high", tier="week", tf="1h",
             direction="reject", mature=True)   # DOWN
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="1h",
             direction="reject", mature=True)    # also DOWN -- agrees, not dominated
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] > 0.0


def test_immature_p4_does_not_dominate_p3():
    p4 = _ev(criterion="P4", asset="MNQ", level="weekly_mid_high", tier="week", tf="1h",
             direction="reject", mature=False)   # immature -- can't dominate anything
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="1h",
             direction="accept", mature=True)
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] > 0.0


def test_exhausted_p4_does_not_dominate_p3():
    p4 = {**_ev(criterion="P4", asset="MNQ", level="weekly_mid_high", tier="week", tf="1h",
                direction="reject", mature=True), "exhausted": True}
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="1h",
             direction="accept", mature=True)
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] > 0.0


def test_p4_daily_mid_does_not_dominate_p3_weekly_mid():
    # Different mid TYPE -- daily P4 must not dominate a weekly P3, even with opposite sides.
    p4 = _ev(criterion="P4", asset="MNQ", level="daily_mid_high", tier="day", tf="1h",
             direction="reject", mature=True)    # DOWN, daily
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="1h",
             direction="accept", mature=True)     # UP, weekly
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] > 0.0


def test_p4_does_not_dominate_p3_on_the_same_asset():
    # The rule is specifically OTHER-asset domination -- a same-asset P4/P3 split (an
    # internal inconsistency, not a cross-asset staleness case) is untouched by this gate.
    p4 = _ev(criterion="P4", asset="MNQ", level="weekly_mid_high", tier="week", tf="1h",
             direction="reject", mature=True)    # DOWN
    p3 = _ev(criterion="P3", asset="MNQ", level="weekly_mid", tier="week", tf="1h",
             direction="accept", mature=True)     # UP, SAME asset as the P4 item
    scoring = score_thesis_evidence([p4, p3])
    p3_scored = next(e for e in scoring["scored_evidence"] if e["criterion"] == "P3")
    assert p3_scored["points"] > 0.0


# --------------------------------------------------------------------------- #
# tf-dedup (same level/direction cited at both 1h and 4h)                      #
# --------------------------------------------------------------------------- #
def test_tf_dedup_keeps_4h_zeroes_1h_when_both_mature():
    h1 = _ev(level="prev_day_high", tier="day", tf="1h", direction="accept", mature=True)
    h4 = _ev(level="prev_day_high", tier="day", tf="4h", direction="accept", mature=True)
    scoring = score_thesis_evidence([h1, h4])
    by_tf = {e["tf"]: e for e in scoring["scored_evidence"]}
    assert by_tf["1h"]["points"] == 0.0
    assert by_tf["4h"]["points"] > 0.0
    assert scoring["net_score"] == by_tf["4h"]["points"]   # only the 4h counted


def test_tf_dedup_leaves_1h_alone_when_4h_immature():
    h1 = _ev(level="prev_day_high", tier="day", tf="1h", direction="accept", mature=True)
    h4 = _ev(level="prev_day_high", tier="day", tf="4h", direction="accept", mature=False)
    scoring = score_thesis_evidence([h1, h4])
    by_tf = {e["tf"]: e for e in scoring["scored_evidence"]}
    assert by_tf["1h"]["points"] > 0.0      # rule 2: only 1h closed -> take it
    assert by_tf["4h"]["points"] == 0.0     # immature anyway, zeroed by the maturity gate


def test_tf_dedup_scoped_to_same_level_direction_only():
    # Different LEVEL -- not the same physical event, both should score independently.
    h1 = _ev(level="prev_day_high", tier="day", tf="1h", direction="accept", mature=True)
    h4 = _ev(level="prev2_day_high", tier="day", tf="4h", direction="accept", mature=True)
    scoring = score_thesis_evidence([h1, h4])
    assert all(e["points"] > 0.0 for e in scoring["scored_evidence"])

    # Same level, OPPOSITE direction (a genuine same-level cross-timeframe contradiction,
    # e.g. the 2026-07-27 MNQ prev1_day_high case) -- not a duplicate, both score.
    reject_1h = _ev(level="prev_day_high", tier="day", tf="1h", direction="reject", mature=True)
    accept_4h = _ev(level="prev_day_high", tier="day", tf="4h", direction="accept", mature=True)
    scoring2 = score_thesis_evidence([reject_1h, accept_4h])
    assert all(e["points"] > 0.0 for e in scoring2["scored_evidence"])


def test_tf_dedup_does_not_double_zero_an_already_exhausted_4h():
    # A 4h item that's mature but flagged exhausted doesn't count as "has_mature_4h" for
    # dedup purposes (it contributes zero itself already) -- the 1h should NOT also be
    # zeroed out from under a case where the 4h isn't actually usable.
    h1 = _ev(level="prev_day_high", tier="day", tf="1h", direction="accept", mature=True)
    h4 = {**_ev(level="prev_day_high", tier="day", tf="4h", direction="accept", mature=True),
          "exhausted": True}
    scoring = score_thesis_evidence([h1, h4])
    by_tf = {e["tf"]: e for e in scoring["scored_evidence"]}
    assert by_tf["1h"]["points"] > 0.0
    assert by_tf["4h"]["points"] == 0.0     # zeroed by its OWN exhausted flag, not dedup


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


def test_empty_evidence_neutral_bias_exempt_from_bias_check():
    t = valid_thesis()
    t["bias"] = "NEUTRAL"
    t["evidence"] = []
    assert "ARI_THESIS_BIAS" not in validate_thesis(t, FACTS).codes()


def test_empty_evidence_directional_bias_rejected():
    # 2026-07-27 09:20 ET case: a directional bias with a genuinely empty ledger must not be
    # silently waved through — it bypasses the entire evidence-scoring sandwich regardless of
    # how much free-text reasoning cites facts that were never transcribed into `evidence`.
    t = valid_thesis()   # bias UP
    t["evidence"] = []
    assert "ARI_THESIS_BIAS" in validate_thesis(t, FACTS).codes()


def test_bias_check_no_magnitude_key_is_a_noop():
    # Byte-identical to every pre-2026-08-05 call site: no "evidence_magnitude" in facts ->
    # magnitude=None -> unweighted x1.0, exactly as before this fix.
    t = valid_thesis()   # bias UP
    t["evidence"] = [
        _ev(asset="MNQ", level="prev_day_high", tf="4h", direction="accept"),   # UP, day/4h: 2.25
        _ev(asset="MNQ", level="prev_day_low", tf="1h", direction="accept"),    # DOWN, day/1h: 1.5
    ]
    assert "ARI_THESIS_BIAS" not in validate_thesis(t, FACTS).codes()   # unweighted net = +0.75, UP


def test_bias_check_uses_evidence_magnitude_2026_07_20_regression():
    # 2026-08-05 fix, real 2026-07-20 09:20 ET regression: the unweighted net for this
    # ledger shape is +0.75 (UP, matches the declared bias, passes clean -- see the no-op
    # test above). But a WEAK clearance on the UP item (x0.75 -> 1.6875) and a STRONG
    # clearance on the DOWN item (x1.25 -> 1.875) flips the REAL, magnitude-weighted net to
    # -0.1875 (DOWN) -- the declared UP bias must now be rejected. Previously
    # validate_thesis's own re-check never applied magnitude at all, so this exact class of
    # discrepancy slipped through clean (the real 07-20 case was a razor-thin 0.0 tie, not a
    # full sign flip like this test, but the root cause -- an unweighted re-check -- is the
    # same; a full flip makes the assertion unambiguous).
    t = valid_thesis()   # bias UP
    t["evidence"] = [
        _ev(asset="MNQ", level="prev_day_high", tf="4h", direction="accept"),   # UP, day/4h
        _ev(asset="MNQ", level="prev_day_low", tf="1h", direction="accept"),    # DOWN, day/1h
    ]
    facts = {**FACTS, "evidence_magnitude": {
        "MNQ": {"prev_day_high": {"4h": 0.1}, "prev_day_low": {"1h": 2.0}},
    }}
    assert "ARI_THESIS_BIAS" in validate_thesis(t, facts).codes()


# --------------------------------------------------------------------------- #
# level_htf_close_status: SEM_EVIDENCE_DIRECTION_MISMATCH + P3 auto-derivation #
# --------------------------------------------------------------------------- #
_LEVEL_STATUS_FACTS = {
    **FACTS,
    "level_htf_close_status": {
        "MNQ": {
            "prev_day_high": {"1h": True, "4h": None},     # accept on 1h, 4h not closed yet
            "daily_mid": {"1h": True, "4h": False},        # 1h accept, 4h reject (prefer 4h)
            "weekly_mid": {"1h": None, "4h": None},        # immature both tf
        },
        "MES": {
            "prev_day_high": {"1h": None, "4h": None},     # never swept
            "daily_mid": {"1h": None, "4h": None},
            "weekly_mid": {"1h": None, "4h": None},
        },
    },
}


def test_direction_mismatch_flags_fabricated_maturity():
    # 2026-07-15 09:20 ET root cause: a level NEVER swept (every tf entry None) declared
    # mature=True -- the exact fabrication class this check exists to catch.
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MES", level="prev_day_high", direction="accept", mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" in validate_thesis(t, _LEVEL_STATUS_FACTS).codes()


def test_direction_mismatch_flags_wrong_direction():
    # MNQ prev_day_high 1h actually ACCEPTED per the facts -- declaring reject is wrong.
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="1h", direction="reject",
                         mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" in validate_thesis(t, _LEVEL_STATUS_FACTS).codes()


def test_direction_matching_facts_not_flagged():
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="1h", direction="accept",
                         mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" not in validate_thesis(t, _LEVEL_STATUS_FACTS).codes()


def test_direction_mismatch_accounts_for_reversal_2026_07_22_regression():
    # Real bug found 2026-07-22 09:20 ET, same day htf_reversal shipped: MNQ prev_day_high
    # is raw-'accept' per the facts, but a partial-bar 'reverse' means the CORRECT declared
    # direction is 'reject' -- without folding in htf_reversal here, a correctly-declared
    # 'reject' was rejected every retry against the STALE raw fact (an infinite loop to
    # failsafe -- verified live against the real harness).
    facts = {**_LEVEL_STATUS_FACTS,
            "htf_reversal": {"MNQ": {"prev_day_high": {"1h": "reverse"}}, "MES": {}}}
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="1h", direction="reject",
                         mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" not in validate_thesis(t, facts).codes()
    # and the RAW (un-reversed) direction is now the one that's wrong.
    t2 = valid_thesis()
    t2["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="1h", direction="accept",
                          mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" in validate_thesis(t2, facts).codes()


def test_direction_mismatch_skipped_for_omitted_item():
    # An 'omit' tier item scores nothing either way -- direction correctness is moot, must
    # not block validation regardless of what was declared.
    facts = {**_LEVEL_STATUS_FACTS,
            "htf_reversal": {"MNQ": {"prev_day_high": {"1h": "omit"}}, "MES": {}}}
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="1h", direction="reject",
                         mature=True)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" not in validate_thesis(t, facts).codes()


def test_immature_declaration_not_checked_against_direction():
    # mature=False items are never scored regardless of direction, so a mismatch there
    # isn't fabrication -- only a declared mature=True claim is cross-checked.
    t = valid_thesis()
    t["evidence"] = [_ev(asset="MNQ", level="prev_day_high", tf="4h", direction="reject",
                         mature=False)]
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" not in validate_thesis(t, _LEVEL_STATUS_FACTS).codes()


def test_no_level_status_in_facts_is_a_noop():
    # Older/minimal facts dicts with no "level_htf_close_status" key at all (e.g. FACTS
    # itself) must not trip this check.
    t = valid_thesis()
    assert "SEM_EVIDENCE_DIRECTION_MISMATCH" not in validate_thesis(t, FACTS).codes()


def test_p3_auto_injected_when_undeclared():
    # 2026-07-20/07-22 09:20 ET root cause: the model declares nothing for P3 at all --
    # code now injects it directly from the facts, no declaration required. MNQ daily_mid
    # is mature on BOTH tf and they DISAGREE (1h accept, 4h reject) -- both are injected as
    # distinct items (a real contradiction, not a duplicate); MNQ weekly_mid + both MES
    # mids are immature and stay uninjected.
    scoring = score_thesis_evidence(
        [], level_htf_close_status=_LEVEL_STATUS_FACTS["level_htf_close_status"])
    p3 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"]
    assert len(p3) == 2
    assert all(it["asset"] == "MNQ" and it["level"] == "daily_mid" and it["tier"] == "day"
               for it in p3)
    by_tf = {it["tf"]: it["direction"] for it in p3}
    assert by_tf == {"1h": "accept", "4h": "reject"}


def test_p3_declared_item_discarded_and_replaced():
    # A model-declared P3 item for the same mid is dropped, not netted alongside the
    # auto-derived one(s) -- 2026-07-20's tier-mislabeling bug (declared tier="session")
    # cannot recur because the declaration is never trusted in the first place.
    bad = _ev(criterion="P3", asset="MNQ", level="daily_mid", tier="session", tf="1h",
              direction="accept")
    scoring = score_thesis_evidence(
        [bad], level_htf_close_status=_LEVEL_STATUS_FACTS["level_htf_close_status"])
    p3 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"]
    assert len(p3) == 2   # code's read (both disagreeing tf), not the model's single guess
    assert all(it["tier"] == "day" for it in p3)
    assert {it["direction"] for it in p3} == {"accept", "reject"}


def test_p3_not_injected_when_immature():
    status = {"MNQ": {"weekly_mid": {"1h": None, "4h": None}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status)
    assert not [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"]


def test_level_htf_close_status_none_leaves_p3_untouched():
    # Default (no facts wired) -- byte-identical to pre-2026-08-02 behavior: a declared
    # P3 item is neither dropped nor duplicated by an auto-injected replacement.
    item = _ev(criterion="P3", asset="MNQ", level="daily_mid", tier="session", tf="1h",
              direction="accept")
    scoring = score_thesis_evidence([item])
    p3 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"]
    assert len(p3) == 1
    assert p3[0]["direction"] == "accept"   # the model's own declared direction, unreplaced


def test_directional_bias_backed_only_by_auto_injected_p3_is_valid():
    # The empty-ledger-directional-bias rejection must not fire when auto-injected P3
    # evidence alone backs the call -- the 2026-07-20/22 empty-ledger bug's fix.
    t = valid_thesis()   # bias UP
    t["evidence"] = []
    facts = {**FACTS, "level_htf_close_status": {
        "MNQ": {"daily_mid": {"1h": True, "4h": None}},   # accept -> UP, matches bias
        "MES": {},
    }}
    codes = validate_thesis(t, facts).codes()
    assert "ARI_THESIS_BIAS" not in codes


# --------------------------------------------------------------------------- #
# P3-vs-P4 mid promotion via mid_reclaim (2026-08-05, thesis.md §10)          #
# --------------------------------------------------------------------------- #

def test_fresh_up_cross_promotes_to_p4_daily_mid_high():
    # 2026-07-22 09:20 ET (weekly_mid case): a fresh up-cross with a mature close beyond
    # is a live reclaim EVENT -- P4, not P3.
    status = {"MNQ": {"daily_mid": {"1h": True, "4h": None}}, "MES": {}}
    reclaim = {"MNQ": {"daily_mid": {"1h": {"fresh": True, "cross_dir": "up"}}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim)
    ev = scoring["scored_evidence"]
    assert not [it for it in ev if it["criterion"] == "P3"]
    p4 = [it for it in ev if it["criterion"] == "P4"]
    assert len(p4) == 1
    assert p4[0]["level"] == "daily_mid_high"
    assert p4[0]["direction"] == "accept"   # up-cross + beyond=True -> held -> accept


def test_fresh_down_cross_accept_reject_is_inverted():
    # A down-cross tests the mid as support-turned-resistance (daily_mid_low): "accept"
    # means the breakdown HELD (closed below, beyond=False) -- the inverse of P3's own
    # beyond-based mapping, not a straight passthrough.
    status = {"MNQ": {"weekly_mid": {"1h": False, "4h": None}}, "MES": {}}
    reclaim = {"MNQ": {"weekly_mid": {"1h": {"fresh": True, "cross_dir": "down"}}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim)
    p4 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P4"]
    assert len(p4) == 1
    assert p4[0]["level"] == "weekly_mid_low"
    assert p4[0]["direction"] == "accept"   # down-cross + beyond=False -> held -> accept

    status_reject = {"MNQ": {"weekly_mid": {"1h": True, "4h": None}}, "MES": {}}
    scoring2 = score_thesis_evidence([], level_htf_close_status=status_reject, mid_reclaim=reclaim)
    p4_2 = [it for it in scoring2["scored_evidence"] if it["criterion"] == "P4"]
    assert p4_2[0]["direction"] == "reject"   # down-cross + beyond=True -> snapped back -> reject


def test_stale_crossing_stays_p3_not_p4():
    # 2026-07-22 09:20 ET (daily_mid case): the crossing predates a later, deeper daily
    # low -- old news, plain P3 position only, exactly as before mid_reclaim existed.
    status = {"MNQ": {"daily_mid": {"1h": False, "4h": None}}, "MES": {}}
    reclaim = {"MNQ": {"daily_mid": {"1h": {"fresh": False, "cross_dir": "down"}}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim)
    ev = scoring["scored_evidence"]
    assert not [it for it in ev if it["criterion"] == "P4"]
    p3 = [it for it in ev if it["criterion"] == "P3"]
    assert len(p3) == 1 and p3[0]["level"] == "daily_mid" and p3[0]["direction"] == "reject"


def test_mid_reclaim_none_leaves_p3_only_unchanged():
    # Default (mid_reclaim not wired) -- byte-identical to pre-2026-08-05 behavior.
    status = {"MNQ": {"daily_mid": {"1h": True, "4h": None}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status)
    ev = scoring["scored_evidence"]
    assert not [it for it in ev if it["criterion"] == "P4"]
    assert len([it for it in ev if it["criterion"] == "P3"]) == 1


def test_declared_p4_mid_item_discarded_and_replaced_by_auto_derivation():
    bad = _ev(criterion="P4", asset="MNQ", level="daily_mid_high", tier="day", tf="1h",
              direction="reject")
    status = {"MNQ": {"daily_mid": {"1h": True, "4h": None}}, "MES": {}}
    reclaim = {"MNQ": {"daily_mid": {"1h": {"fresh": True, "cross_dir": "up"}}}, "MES": {}}
    scoring = score_thesis_evidence([bad], level_htf_close_status=status, mid_reclaim=reclaim)
    p4 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P4"]
    assert len(p4) == 1
    assert p4[0]["direction"] == "accept"   # code's read, not the model's discarded "reject"


# --------------------------------------------------------------------------- #
# P4-supersedes-P3 per (asset, mid) + position-only P3 + P1 stale gate         #
# (2026-08-15, from the 2026-08-10 09:20 ET audit)                             #
# --------------------------------------------------------------------------- #

def test_fresh_p4_on_one_tf_supersedes_stale_tf_p3_same_mid():
    # 2026-08-10 09:20 ET: MES's daily_mid had a fresh 1h crossing (P4) AND a stale 4h
    # crossing that previously injected a redundant P3 restatement of the same mid.
    status = {"MNQ": {"daily_mid": {"1h": False, "4h": False}}, "MES": {}}
    reclaim = {"MNQ": {"daily_mid": {"1h": {"fresh": True, "cross_dir": "down"},
                                     "4h": {"fresh": False, "cross_dir": "down"}}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim)
    ev = scoring["scored_evidence"]
    p4 = [it for it in ev if it["criterion"] == "P4"]
    assert len(p4) == 1 and p4[0]["tf"] == "1h" and p4[0]["level"] == "daily_mid_low"
    assert not [it for it in ev if it["criterion"] == "P3"]   # stale 4h P3 superseded


def test_stale_only_tfs_still_inject_p3_when_no_fresh_p4():
    # No fresh crossing on ANY tf -> the old per-tf P3 behavior is unchanged.
    status = {"MNQ": {"daily_mid": {"1h": False, "4h": False}}, "MES": {}}
    reclaim = {"MNQ": {"daily_mid": {"1h": {"fresh": False, "cross_dir": "down"},
                                     "4h": {"fresh": False, "cross_dir": "down"}}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim)
    ev = scoring["scored_evidence"]
    assert not [it for it in ev if it["criterion"] == "P4"]
    assert len([it for it in ev if it["criterion"] == "P3"]) == 2   # 1h + 4h


def test_position_only_p3_injected_when_mid_never_crossed():
    # 2026-08-10 09:20 ET: both assets sat ~200pts ABOVE their weekly mids with no
    # crossing in the lookback -- previously zero P3 items existed for the strongest
    # position case of all.
    status = {"MNQ": {}, "MES": {}}
    mid_position = {"MNQ": {"weekly_mid": {"price": 29613.0, "side": "above",
                                           "dist_pts": 202.5, "dist_ratio": 2.71}},
                    "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status,
                                    mid_position=mid_position)
    ev = scoring["scored_evidence"]
    assert len(ev) == 1
    assert ev[0]["criterion"] == "P3" and ev[0]["level"] == "weekly_mid"
    assert ev[0]["direction"] == "accept" and ev[0]["side"] == "UP"
    assert ev[0]["points"] == 1.5   # _P3_MID_POINTS["week"], magnitude-neutral


def test_position_only_p3_not_injected_when_crossing_evidence_exists():
    # A mature crossing status for the SAME mid must win over the position fallback --
    # exactly one item, from the crossing, not two.
    status = {"MNQ": {"daily_mid": {"1h": True, "4h": None}}, "MES": {}}
    mid_position = {"MNQ": {"daily_mid": {"price": 100.0, "side": "above",
                                          "dist_pts": 5.0, "dist_ratio": 0.2}}, "MES": {}}
    scoring = score_thesis_evidence([], level_htf_close_status=status,
                                    mid_position=mid_position)
    p3 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"]
    assert len(p3) == 1 and p3[0]["tf"] == "1h"


def test_no_mid_position_leaves_never_crossed_mid_unevidenced():
    # Backward compat: mid_position absent (older facts dicts) -> exactly the old gap.
    scoring = score_thesis_evidence([], level_htf_close_status={"MNQ": {}, "MES": {}})
    assert scoring["scored_evidence"] == []


def test_p1_stale_level_zeroes_declared_item():
    item = _ev(asset="MNQ", level="prev_day_high", direction="accept")
    base = score_thesis_evidence([item])["net_score"]
    assert base != 0.0
    scoring = score_thesis_evidence([_ev(asset="MNQ", level="prev_day_high",
                                         direction="accept")],
                                    p1_stale_levels={"MNQ": ["prev_day_high"]})
    assert scoring["net_score"] == 0.0
    assert scoring["scored_evidence"][0]["points"] == 0.0


def test_p1_stale_level_skipped_by_auto_injection():
    scoring = score_thesis_evidence(
        [], level_tiers={"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}},
                         "MES": {}},
        level_htf_close_status={"MNQ": {"prev_day_high": {"1h": True, "4h": None}},
                                "MES": {}},
        p1_stale_levels={"MNQ": ["prev_day_high"]})
    assert not [it for it in scoring["scored_evidence"] if it["criterion"] == "P1"]


def test_p2_effective_verdict_reverse_on_accept_fires_p2():
    # Stage 1: a fully-MSS'd ('reverse') acceptance at a meaningful divergence site fires
    # P2 (effective reject) instead of a flipped P1.
    tiers = {"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}}, "MES": {}}
    status = {"MNQ": {"prev_day_high": {"1h": True, "4h": None}}, "MES": {}}   # raw accept
    reversal = {"MNQ": {"prev_day_high": {"1h": "reverse"}}}
    cands = [{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
              "unswept_ticker": "MES", "meaningful": True}]
    scoring = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                    smt_candidates=cands, htf_reversal=reversal)
    ev = scoring["scored_evidence"]
    assert len(ev) == 1
    assert ev[0]["criterion"] == "P2" and ev[0]["direction"] == "reject"
    assert ev[0]["points"] == 1.5   # day-tier P2, FULL weight at 'reverse'


def test_p2_effective_verdict_reverse_on_reject_falls_back_to_flipped_p1():
    # Stage 1: a fully-MSS'd REJECTION stops firing P2 (effective accept); it falls to P1
    # and the reversal pre-pass flips it — previously the stale P2 escaped the machinery.
    tiers = {"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}}, "MES": {}}
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}   # raw reject
    reversal = {"MNQ": {"prev_day_high": {"1h": "reverse"}}}
    cands = [{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
              "unswept_ticker": "MES", "meaningful": True}]
    scoring = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                    smt_candidates=cands, htf_reversal=reversal)
    ev = scoring["scored_evidence"]
    assert len(ev) == 1
    assert ev[0]["criterion"] == "P1" and ev[0]["direction"] == "accept"   # flipped
    assert ev[0]["side"] == "UP"


def test_p2_discount_stage_reject_scores_half():
    # Stage 1: a P2 fired on a 'discount'-stage rejection inherits the same x0.5 the
    # pre-pass gives P1 — previously P2 escaped the discount entirely.
    tiers = {"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}}, "MES": {}}
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}
    reversal = {"MNQ": {"prev_day_high": {"1h": "discount"}}}
    cands = [{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
              "unswept_ticker": "MES", "meaningful": True}]
    scoring = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                    smt_candidates=cands, htf_reversal=reversal)
    p2 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P2"]
    assert len(p2) == 1 and p2[0]["points"] == 0.75   # 1.5 x 0.5


def test_p2_omit_blocks_both_p1_and_p2():
    tiers = {"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}}, "MES": {}}
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}
    reversal = {"MNQ": {"prev_day_high": {"1h": "omit"}}}
    cands = [{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
              "unswept_ticker": "MES", "meaningful": True}]
    scoring = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                    smt_candidates=cands, htf_reversal=reversal)
    assert scoring["scored_evidence"] == []


def test_p2_discount_fire_knob_distance_gated():
    # Stage 2 (experimental knob): a distance-safe 'discount' recross of an ACCEPT at a
    # divergence site fires P2 at x0.5; below the threshold it stays a discounted P1.
    tiers = {"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}}, "MES": {}}
    status = {"MNQ": {"prev_day_high": {"1h": True, "4h": None}}, "MES": {}}   # raw accept
    reversal = {"MNQ": {"prev_day_high": {"1h": "discount"}}}
    cands = [{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
              "unswept_ticker": "MES", "meaningful": True}]
    dist = {"MNQ": {"prev_day_high": 0.4}}
    fired = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                  smt_candidates=cands, htf_reversal=reversal,
                                  recross_distance=dist, p2_discount_fire_ratio=0.25)
    ev = fired["scored_evidence"]
    assert len(ev) == 1 and ev[0]["criterion"] == "P2" and ev[0]["points"] == 0.75
    held = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                 smt_candidates=cands, htf_reversal=reversal,
                                 recross_distance=dist, p2_discount_fire_ratio=0.5)
    ev2 = held["scored_evidence"]
    assert len(ev2) == 1 and ev2[0]["criterion"] == "P1" and ev2[0]["direction"] == "accept"
    assert ev2[0]["points"] == 0.75   # day P1 discounted x0.5 (1.5 x 0.5), unflipped
    off = score_thesis_evidence([], level_tiers=tiers, level_htf_close_status=status,
                                smt_candidates=cands, htf_reversal=reversal,
                                recross_distance=dist)   # knob unset -> Stage 1 only
    assert [it["criterion"] for it in off["scored_evidence"]] == ["P1"]


def test_p1_stale_gate_does_not_touch_p2():
    # P2 keeps its own tier-relative shelf-life mechanism -- a stale P1 level with a
    # genuine meaningful reject divergence still scores as P2.
    scoring = score_thesis_evidence(
        [], level_tiers={"MNQ": {"prev_day_high": {"tier": "day", "price": 20000.0}},
                         "MES": {}},
        level_htf_close_status={"MNQ": {"prev_day_high": {"1h": False, "4h": None}},
                                "MES": {}},
        smt_candidates=[{"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
                         "unswept_ticker": "MES", "meaningful": True}],
        p1_stale_levels={"MNQ": ["prev_day_high"]})
    p2 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P2"]
    assert len(p2) == 1 and p2[0]["points"] > 0.0


# --------------------------------------------------------------------------- #
# Partial-bar reversal via htf_reversal (2026-08-05, thesis.md §10)            #
# --------------------------------------------------------------------------- #

def test_htf_reversal_none_leaves_item_unaffected():
    item = _ev(criterion="P1", asset="MNQ", level="prev_day_low", direction="accept")
    reversal = {"MNQ": {"prev_day_low": {"1h": "none"}}}
    scoring = score_thesis_evidence([item], htf_reversal=reversal)
    assert scoring["scored_evidence"][0]["points"] == 1.5   # day/1h base, unaffected


def test_htf_reversal_discount_halves_points():
    item = _ev(criterion="P1", asset="MNQ", level="prev_day_low", direction="accept")
    reversal = {"MNQ": {"prev_day_low": {"1h": "discount"}}}
    scoring = score_thesis_evidence([item], htf_reversal=reversal)
    assert scoring["scored_evidence"][0]["points"] == 0.75   # 1.5 * 0.5
    assert scoring["scored_evidence"][0]["direction"] == "accept"   # direction untouched


def test_htf_reversal_omit_zeroes_the_item():
    item = _ev(criterion="P1", asset="MNQ", level="prev_day_low", direction="accept")
    reversal = {"MNQ": {"prev_day_low": {"1h": "omit"}}}
    scoring = score_thesis_evidence([item], htf_reversal=reversal)
    assert scoring["scored_evidence"] == []   # dropped before scoring, not merely zero-pointed


def test_htf_reversal_reverse_flips_direction_and_side():
    # 2026-07-22 09:20 ET: MNQ london(cur)_low declared accept (DOWN); the forming 09:00
    # bar broke past the completed bar's own wick -- reverse to reject (UP).
    item = _ev(criterion="P1", asset="MNQ", level="london_low", tier="session",
              direction="accept")
    reversal = {"MNQ": {"london_low": {"1h": "reverse"}}}
    scoring = score_thesis_evidence([item], htf_reversal=reversal)
    scored = scoring["scored_evidence"][0]
    assert scored["direction"] == "reject"
    assert scored["side"] == "UP"          # accept on a "low" level is DOWN; reject is UP
    assert scored["htf_reversal"] == "reverse"


def test_htf_reversal_applies_to_auto_injected_p4_mid_item():
    # The exact 2026-07-22 MNQ weekly_mid shape: fresh up-cross promotes to P4 (accept),
    # but the partial 09:00 bar's own MSS reverses it to reject regardless.
    status = {"MNQ": {"weekly_mid": {"1h": False, "4h": None}}, "MES": {}}
    reclaim = {"MNQ": {"weekly_mid": {"1h": {"fresh": True, "cross_dir": "down"}}}, "MES": {}}
    reversal = {"MNQ": {"weekly_mid_low": {"1h": "reverse"}}}
    scoring = score_thesis_evidence([], level_htf_close_status=status, mid_reclaim=reclaim,
                                    htf_reversal=reversal)
    p4 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P4"]
    assert len(p4) == 1
    assert p4[0]["direction"] == "reject"   # accept (down-cross held) reversed -> reject


def test_htf_reversal_none_param_leaves_scoring_unchanged():
    item = _ev(criterion="P1", asset="MNQ", level="prev_day_low", direction="accept")
    scoring = score_thesis_evidence([item])   # no htf_reversal kwarg at all
    assert scoring["scored_evidence"][0]["points"] == 1.5


# --------------------------------------------------------------------------- #
# P1/P2 auto-derivation + P1/P2-dominates-P3 + §2.1e tier promotion (2026-08-02) #
# --------------------------------------------------------------------------- #
_LEVEL_TIERS = {
    "MNQ": {
        "prev_day_high": {"tier": "day", "price": 20000.0},
        "prev_day_low": {"tier": "day", "price": 19500.0},
    },
    "MES": {},
}
_LEVEL_STATUS = {
    "MNQ": {
        "prev_day_high": {"1h": True, "4h": None},    # accept
        "prev_day_low": {"1h": None, "4h": None},      # immature -- not injected
    },
    "MES": {},
}
_SMT_CANDIDATES = [
    {"level": "prev_day_high", "tier": "day", "swept_ticker": "MNQ",
     "unswept_ticker": "MES", "meaningful": True},
]


def test_p1_auto_injected_when_undeclared():
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=_LEVEL_STATUS)
    p1 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P1"]
    assert len(p1) == 1
    assert p1[0]["asset"] == "MNQ" and p1[0]["level"] == "prev_day_high"
    assert p1[0]["direction"] == "accept" and p1[0]["tier"] == "day"


def test_p1_not_injected_when_immature():
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=_LEVEL_STATUS)
    assert not any(it["level"] == "prev_day_low" for it in scoring["scored_evidence"])


def test_p1_not_injected_when_suppressed():
    scoring = score_thesis_evidence(
        [], level_tiers=_LEVEL_TIERS, level_htf_close_status=_LEVEL_STATUS,
        suppressed_p1_levels={"MNQ": ["prev_day_high"]})
    assert not [it for it in scoring["scored_evidence"] if it["criterion"] == "P1"]


def test_declared_p1_item_discarded_and_replaced():
    bad = _ev(asset="MNQ", level="prev_day_high", tier="week", direction="reject")
    scoring = score_thesis_evidence([bad], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=_LEVEL_STATUS)
    p1 = [it for it in scoring["scored_evidence"] if it["criterion"] == "P1"]
    assert len(p1) == 1
    assert p1[0]["tier"] == "day" and p1[0]["direction"] == "accept"   # code's read, not the model's


def test_exhausted_veto_zeroes_auto_injected_p1():
    veto = _ev(asset="MNQ", level="prev_day_high", direction="accept", mature=True)
    veto["exhausted"] = True
    scoring = score_thesis_evidence([veto], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=_LEVEL_STATUS)
    assert scoring["net_score"] == 0.0


def test_p2_injected_for_meaningful_unsuppressed_reject_divergence():
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}   # reject
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=status,
                                    smt_candidates=_SMT_CANDIDATES)
    scored = scoring["scored_evidence"]
    assert len(scored) == 1
    assert scored[0]["criterion"] == "P2" and scored[0]["direction"] == "reject"
    assert scored[0]["asset"] == "MNQ" and scored[0]["level"] == "prev_day_high"


def test_p2_falls_through_to_p1_when_lagger_actually_accepted():
    # meaningful=True but the lagger's own close shows accept, not reject -- not a genuine
    # P2 divergence signal, so it scores as ordinary P1 instead, never as both.
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=_LEVEL_STATUS,   # accept
                                    smt_candidates=_SMT_CANDIDATES)
    scored = scoring["scored_evidence"]
    assert len(scored) == 1
    assert scored[0]["criterion"] == "P1" and scored[0]["direction"] == "accept"


def test_p2_suppressed_site_excludes_both_p1_and_p2():
    # thesis.md: a P2-suppressed level was "already nested when the divergence itself
    # fired -- no evidence at all here, not P1 and not P2". It must NOT fall through to
    # an ordinary P1 reading.
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}
    scoring = score_thesis_evidence(
        [], level_tiers=_LEVEL_TIERS, level_htf_close_status=status,
        smt_candidates=_SMT_CANDIDATES, suppressed_p2_sites={"MNQ": ["prev_day_high"]})
    assert not [it for it in scoring["scored_evidence"] if it["level"] == "prev_day_high"]


def test_disagreeing_1h_4h_scores_both_not_just_4h():
    # 2026-07-27 root-cause regression: MNQ prev1_day_high's 1h (fresh reject) and 4h
    # (stale accept) genuinely disagreed. An earlier version of this auto-derivation
    # unconditionally preferred 4h, silently discarding the fresh 1h contradiction --
    # both must score independently, mirroring tf-dedup's own "keep genuine
    # disagreements" scoping.
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": True}}, "MES": {}}
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS, level_htf_close_status=status)
    items = [it for it in scoring["scored_evidence"] if it["level"] == "prev_day_high"]
    assert len(items) == 2
    tfs = {it["tf"]: it["direction"] for it in items}
    assert tfs == {"1h": "reject", "4h": "accept"}


def test_agreeing_1h_4h_collapses_via_tf_dedup():
    status = {"MNQ": {"prev_day_high": {"1h": True, "4h": True}}, "MES": {}}
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS, level_htf_close_status=status)
    items = [it for it in scoring["scored_evidence"] if it["level"] == "prev_day_high"]
    assert len(items) == 2   # both injected...
    scored_pts = {it["tf"]: it["points"] for it in items}
    assert scored_pts["1h"] == 0.0    # ...but the 1h is zeroed by tf-dedup, matching direction
    assert scored_pts["4h"] > 0.0


def test_p1_p2_never_double_counted_for_same_level():
    status = {"MNQ": {"prev_day_high": {"1h": False, "4h": None}}, "MES": {}}
    scoring = score_thesis_evidence([], level_tiers=_LEVEL_TIERS,
                                    level_htf_close_status=status,
                                    smt_candidates=_SMT_CANDIDATES)
    assert len(scoring["scored_evidence"]) == 1   # never both P1 and P2 for the same level


def test_level_tiers_none_leaves_p1_p2_untouched():
    item = _ev(asset="MNQ", level="prev_day_high", direction="accept")
    scoring = score_thesis_evidence([item])
    assert len(scoring["scored_evidence"]) == 1
    assert scoring["scored_evidence"][0]["direction"] == "accept"


def test_day_tier_p1_dominates_other_assets_daily_p3():
    p1 = _ev(asset="MNQ", level="prev_day_high", tier="day", direction="accept")   # UP
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", tier="day", direction="reject")  # DOWN
    scoring = score_thesis_evidence([p1, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0


def test_week_tier_p2_dominates_other_assets_weekly_p3():
    p2 = _ev(criterion="P2", asset="MNQ", level="prev1_week_low", tier="week", direction="reject")  # UP (reject a low is bullish)
    p3 = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", direction="reject")  # DOWN
    scoring = score_thesis_evidence([p2, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0


def test_session_tier_p1_does_not_dominate_p3():
    p1 = _ev(asset="MNQ", level="prev_day_high", tier="session", direction="accept")
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", tier="day", direction="reject")
    scoring = score_thesis_evidence([p1, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0


def test_p4_dominance_wins_over_p1_p2_on_tie():
    p1 = _ev(asset="MNQ", level="prev_day_low", tier="day", direction="accept")   # DOWN (low accept)
    p4 = _ev(criterion="P4", asset="MNQ", level="daily_mid_high", tier="day", direction="accept")  # UP
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", tier="day", direction="accept")   # UP -- agrees with P4
    scoring = score_thesis_evidence([p1, p4, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0   # P4 (UP) does not contradict P3 (UP) -- not zeroed


_WEEK_EXTREMES = {"MNQ": {"hi": 20010.0, "lo": 19000.0}}   # range 1010, 5% tol = 50.5


def test_day_tier_confluent_with_week_high_scores_week_tier():
    # prev_day_high (20000.0) sits 10pts from the week high (20010.0), well within 5% tol.
    item = _ev(asset="MNQ", level="prev_day_high", tier="day", direction="accept")
    scoring = score_thesis_evidence([item], level_tiers=_LEVEL_TIERS, week_extremes=_WEEK_EXTREMES)
    assert scoring["scored_evidence"][0]["points"] == 2.0   # week tier (1.0) x 1h (1.0) x 2.0 base


def test_day_tier_not_confluent_scores_day_tier():
    # prev_day_low (19500.0) sits 500pts from either week extreme -- not confluent.
    item = _ev(asset="MNQ", level="prev_day_low", tier="day", direction="reject")
    scoring = score_thesis_evidence([item], level_tiers=_LEVEL_TIERS, week_extremes=_WEEK_EXTREMES)
    assert scoring["scored_evidence"][0]["points"] == 1.5   # day tier (0.75) x 1h (1.0) x 2.0 base


def test_confluence_promotion_noop_without_week_extremes():
    item = _ev(asset="MNQ", level="prev_day_high", tier="day", direction="accept")
    scoring = score_thesis_evidence([item], level_tiers=_LEVEL_TIERS)
    assert scoring["scored_evidence"][0]["points"] == 1.5   # unpromoted -- no week_extremes passed


# --------------------------------------------------------------------------- #
# P3 fixed point value + P2-same-asset-dominates-P3 (2026-08-02)               #
# --------------------------------------------------------------------------- #
def test_p3_fixed_points_independent_of_tf():
    daily_1h = _ev(criterion="P3", level="daily_mid", tf="1h", direction="accept")
    daily_4h = _ev(criterion="P3", asset="MES", level="daily_mid", tf="4h", direction="accept")
    weekly_1h = _ev(criterion="P3", level="weekly_mid", tier="week", tf="1h", direction="reject")
    weekly_4h = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", tf="4h", direction="reject")
    scored = score_thesis_evidence([daily_1h, daily_4h, weekly_1h, weekly_4h])["scored_evidence"]
    assert [it["points"] for it in scored] == [1.0, 1.0, 1.5, 1.5]   # tf never changes a P3 point value


def test_p3_clearance_magnitude_discounts_thin_close():
    # 2026-08-05: a razor-thin HTF close (e.g. MES weekly_mid, 2026-07-22 09:20 ET -- wicked
    # 10.75pts below the mid, closed only 1.0pt beyond) previously scored at an unweighted
    # x1.0 (build_evidence_magnitude had no entry for a synthetic mid) -- same as a
    # decisive, confidently-held close. A weak-clearance ratio (<0.5x avg range) must now
    # discount it exactly like a P1 item already does.
    item = _ev(criterion="P3", level="daily_mid", tf="1h", direction="accept")
    weak = score_thesis_evidence([item], magnitude={("MNQ", "daily_mid", "1h"): 0.1})
    strong = score_thesis_evidence([item], magnitude={("MNQ", "daily_mid", "1h"): 2.0})
    neutral = score_thesis_evidence([item])
    assert weak["scored_evidence"][0]["points"] == 0.75      # _P3_MID_POINTS["day"]=1.0 * 0.75
    assert strong["scored_evidence"][0]["points"] == 1.25    # 1.0 * 1.25
    assert neutral["scored_evidence"][0]["points"] == 1.0    # no magnitude -> x1.0, unchanged


def test_p2_dominates_contradicting_p3_on_same_asset():
    # 2026-07-15 09:20 ET motivating case: MNQ's own bearish P2 divergence at
    # prev2_day_high coexisted with MNQ's own P3 daily_mid still reading "above" --
    # the P2 divergence must dominate (zero) the contradicting same-asset P3.
    p2 = _ev(criterion="P2", asset="MNQ", level="prev_day_high", tier="day", direction="reject")  # DOWN
    p3 = _ev(criterion="P3", asset="MNQ", level="daily_mid", direction="accept")   # UP -- contradicts
    scoring = score_thesis_evidence([p2, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0


def test_p1_does_not_dominate_same_asset_p3():
    p1 = _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", direction="reject")  # DOWN
    p3 = _ev(criterion="P3", asset="MNQ", level="daily_mid", direction="accept")   # UP -- contradicts
    scoring = score_thesis_evidence([p1, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0   # P1 same-asset domination stays unbuilt


def test_p2_same_asset_dominance_agreeing_not_zeroed():
    p2 = _ev(criterion="P2", asset="MNQ", level="prev_day_high", tier="day", direction="accept")  # UP
    p3 = _ev(criterion="P3", asset="MNQ", level="daily_mid", direction="accept")   # UP -- agrees
    scoring = score_thesis_evidence([p2, p3])
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0


def test_week_confluent_day_tier_item_dominates_both_mids_cross_asset():
    # A day-tier item that is ALSO week-confluent (prev_day_high, per _WEEK_EXTREMES) is
    # dual-natured: it should claim BOTH the other asset's daily AND weekly mid, not just
    # the daily one a plain day-tier item would.
    p1 = _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", direction="reject")  # DOWN
    p3_daily = _ev(criterion="P3", asset="MES", level="daily_mid", direction="accept")    # UP -- contradicts
    p3_weekly = _ev(criterion="P3", asset="MES", level="weekly_mid", tier="week", direction="accept")  # UP -- contradicts
    scoring = score_thesis_evidence([p1, p3_daily, p3_weekly],
                                    level_tiers=_LEVEL_TIERS, week_extremes=_WEEK_EXTREMES)
    p3_scored = {it["level"]: it["points"] for it in scoring["scored_evidence"] if it["criterion"] == "P3"}
    assert p3_scored["daily_mid"] == 0.0
    assert p3_scored["weekly_mid"] == 0.0


# --------------------------------------------------------------------------- #
# Extremity-based dominance resolution (2026-08-02)                            #
# --------------------------------------------------------------------------- #
_NOW_PRICE = 19800.0   # prev_day_high (20000, dist 200) vs prev_day_low (19500, dist 300)


def test_more_extreme_item_wins_same_asset_dominance_conflict():
    # 2026-07-15 09:20 ET motivating case: MNQ's own prev1_day_high (dist 33.5, UP) and
    # prev2_day_high (dist 87.0, DOWN, the more extreme level) disagreed on which should
    # dominate MES's contradicting P3 -- the more extreme one must win regardless of
    # declaration order.
    high = _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", direction="accept")  # UP, dist 200
    low = _ev(criterion="P1", asset="MNQ", level="prev_day_low", tier="day", direction="accept")     # DOWN, dist 300 (more extreme)
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", direction="accept")   # UP -- contradicts the more extreme DOWN
    scoring = score_thesis_evidence([high, low, p3], level_tiers=_LEVEL_TIERS, now_price=_NOW_PRICE)
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0   # dominated by the more extreme (prev_day_low) item, not prev_day_high


def test_more_extreme_item_wins_regardless_of_declared_order():
    # Same conflict, items declared in the OPPOSITE order -- the winner must not depend
    # on iteration/declaration order once now_price is supplied.
    high = _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", direction="accept")  # UP, dist 200
    low = _ev(criterion="P1", asset="MNQ", level="prev_day_low", tier="day", direction="accept")     # DOWN, dist 300
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", direction="accept")   # UP
    scoring = score_thesis_evidence([p3, low, high], level_tiers=_LEVEL_TIERS, now_price=_NOW_PRICE)
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0


def test_p4_outranks_p1_p2_regardless_of_distance():
    # P4 must win the dominance slot even when the competing P1/P2 item is FURTHER away
    # (would otherwise win on pure extremity) -- a two-step HTF-confirmed reclaim beats a
    # single sweep/divergence read.
    far_p1 = _ev(criterion="P1", asset="MNQ", level="prev_day_low", tier="day", direction="accept")  # DOWN, dist 300
    p4 = _ev(criterion="P4", asset="MNQ", level="daily_mid_high", tier="day", direction="accept")     # UP, dist 0 (no level_tiers entry)
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", direction="accept")   # UP -- agrees with P4, not far_p1
    scoring = score_thesis_evidence([far_p1, p4, p3], level_tiers=_LEVEL_TIERS, now_price=_NOW_PRICE)
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0   # P4 (UP) wins over the more-distant P1 (DOWN) -- not zeroed


def test_now_price_none_falls_back_to_declaration_order():
    # Byte-identical to pre-2026-08-02 behavior when now_price isn't supplied: every
    # distance is 0.0, so the first-declared candidate wins (matching the old setdefault
    # first-come semantics).
    high = _ev(criterion="P1", asset="MNQ", level="prev_day_high", tier="day", direction="accept")  # UP, declared first
    low = _ev(criterion="P1", asset="MNQ", level="prev_day_low", tier="day", direction="accept")     # DOWN, declared second
    p3 = _ev(criterion="P3", asset="MES", level="daily_mid", direction="accept")   # UP -- agrees with the FIRST-declared item
    scoring = score_thesis_evidence([high, low, p3], level_tiers=_LEVEL_TIERS)   # no now_price
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] > 0.0   # first-declared (high, UP) wins the tie -- not zeroed


def test_p2_same_asset_extremity_resolution():
    p2_near = _ev(criterion="P2", asset="MNQ", level="prev_day_high", tier="day", direction="reject")  # DOWN, dist 200
    p2_far = _ev(criterion="P2", asset="MNQ", level="prev_day_low", tier="day", direction="reject")     # UP, dist 300 (more extreme)
    p3 = _ev(criterion="P3", asset="MNQ", level="daily_mid", direction="reject")   # DOWN -- contradicts the more extreme UP
    scoring = score_thesis_evidence([p2_near, p2_far, p3], level_tiers=_LEVEL_TIERS, now_price=_NOW_PRICE)
    p3_scored = [it for it in scoring["scored_evidence"] if it["criterion"] == "P3"][0]
    assert p3_scored["points"] == 0.0   # dominated by the more extreme same-asset P2 (prev_day_low)


# --------------------------------------------------------------------------- #
# P5 same-move dedup (2026-08-02)                                              #
# --------------------------------------------------------------------------- #
_FVG_ZONE_META = {
    "MNQ 1hr 2026-07-22 01:00:00-04:00 bear": {"asset": "MNQ", "tf": "1h", "kind": "bear",
                                               "ts": "2026-07-22 01:00:00-04:00"},
    "MNQ 1hr 2026-07-22 02:00:00-04:00 bear": {"asset": "MNQ", "tf": "1h", "kind": "bear",
                                               "ts": "2026-07-22 02:00:00-04:00"},
    "MNQ 1hr 2026-07-22 07:00:00-04:00 bear": {"asset": "MNQ", "tf": "1h", "kind": "bear",
                                               "ts": "2026-07-22 07:00:00-04:00"},
    "MES 1hr 2026-07-22 02:00:00-04:00 bear": {"asset": "MES", "tf": "1h", "kind": "bear",
                                               "ts": "2026-07-22 02:00:00-04:00"},
    "MNQ 1hr 2026-07-22 01:00:00-04:00 bull": {"asset": "MNQ", "tf": "1h", "kind": "bull",
                                               "ts": "2026-07-22 01:00:00-04:00"},
}


def _p5(level, asset="MNQ", direction="accept"):
    return _ev(criterion="P5", asset=asset, level=level, tier="day", direction=direction)


def test_adjacent_p5_zones_collapse_to_freshest():
    z1 = _p5("MNQ 1hr 2026-07-22 01:00:00-04:00 bear")
    z2 = _p5("MNQ 1hr 2026-07-22 02:00:00-04:00 bear")
    scoring = score_thesis_evidence([z1, z2], fvg_zone_meta=_FVG_ZONE_META)
    scored = {it["level"]: it["points"] for it in scoring["scored_evidence"]}
    assert scored["MNQ 1hr 2026-07-22 01:00:00-04:00 bear"] == 0.0    # older -- zeroed
    assert scored["MNQ 1hr 2026-07-22 02:00:00-04:00 bear"] > 0.0     # freshest -- survives


def test_non_adjacent_p5_zone_not_deduped():
    # 07:00 is 5 hours after 02:00 -- not a consecutive 1h bar, a genuinely separate move.
    z2 = _p5("MNQ 1hr 2026-07-22 02:00:00-04:00 bear")
    z3 = _p5("MNQ 1hr 2026-07-22 07:00:00-04:00 bear")
    scoring = score_thesis_evidence([z2, z3], fvg_zone_meta=_FVG_ZONE_META)
    scored = {it["level"]: it["points"] for it in scoring["scored_evidence"]}
    assert scored["MNQ 1hr 2026-07-22 02:00:00-04:00 bear"] > 0.0
    assert scored["MNQ 1hr 2026-07-22 07:00:00-04:00 bear"] > 0.0


def test_p5_dedup_scoped_to_same_asset():
    z1 = _p5("MNQ 1hr 2026-07-22 01:00:00-04:00 bear")
    z_mes = _p5("MES 1hr 2026-07-22 02:00:00-04:00 bear", asset="MES")
    scoring = score_thesis_evidence([z1, z_mes], fvg_zone_meta=_FVG_ZONE_META)
    scored = {it["level"]: it["points"] for it in scoring["scored_evidence"]}
    assert scored["MNQ 1hr 2026-07-22 01:00:00-04:00 bear"] > 0.0   # lone in its own asset group
    assert scored["MES 1hr 2026-07-22 02:00:00-04:00 bear"] > 0.0


def test_p5_dedup_scoped_to_same_kind():
    bear = _p5("MNQ 1hr 2026-07-22 01:00:00-04:00 bear")
    bull = _p5("MNQ 1hr 2026-07-22 01:00:00-04:00 bull", direction="reject")   # same asset/tf/ts, different kind
    scoring = score_thesis_evidence([bear, bull], fvg_zone_meta=_FVG_ZONE_META)
    assert all(it["points"] > 0.0 for it in scoring["scored_evidence"])   # never grouped together


def test_fvg_zone_meta_none_leaves_p5_untouched():
    z1 = _p5("MNQ 1hr 2026-07-22 01:00:00-04:00 bear")
    z2 = _p5("MNQ 1hr 2026-07-22 02:00:00-04:00 bear")
    scoring = score_thesis_evidence([z1, z2])   # no fvg_zone_meta
    assert all(it["points"] > 0.0 for it in scoring["scored_evidence"])   # dedup never runs


# --------------------------------------------------------------------------- #
# DOL-menu refit (2026-08-16): projection draws + band tags + RANGE/FAR audit  #
# --------------------------------------------------------------------------- #

def test_projection_dol_exempt_from_level_not_in_facts():
    from schemas import DOL_PROJECTION_LEVELS
    t = valid_thesis()
    t["dol"] = {"level": "projection_up", "price": 19910.0}
    t["exhausted_if"] = [{"type": "price_beyond", "price": 19910.0, "side": "above"}]
    assert "projection_up" in DOL_PROJECTION_LEVELS
    codes = validate_thesis(t, FACTS).codes()
    assert "SEM_LEVEL_NOT_IN_FACTS" not in codes


def test_build_thesis_schema_extra_dol_levels_dol_slot_only():
    from schemas import build_thesis_schema
    schema = build_thesis_schema(["prev_day_high"], extra_dol_levels=["projection_up"])
    dol_enum = schema["properties"]["dol"]["anyOf"][0]["properties"]["level"]["enum"]
    assert set(dol_enum) == {"prev_day_high", "projection_up"}
    # never a sweepable-level name, never an evidence level
    swept_enum = schema["$defs"]["pred_level_swept"]["properties"]["name"]["enum"]
    assert "projection_up" not in swept_enum
    ev_enum = schema["properties"]["evidence"]["items"]["properties"]["level"]["enum"]
    assert "projection_up" not in ev_enum


def test_aud_dol_far_for_regime_warns_on_range_only():
    menus = {"dol": {"UP": [{"id": "D1", "level": "prev_day_high", "price": 20000.0,
                             "tier": "day", "side": "above",
                             "dist_ratio": 4.8, "band": "FAR"}], "DOWN": []}}
    t = valid_thesis()
    t["regime"] = "RANGE"
    r = validate_thesis(t, {**FACTS, "menus": menus})
    assert "AUD_DOL_FAR_FOR_REGIME" in {w.code for w in r.warnings}

    t2 = valid_thesis()            # TREND regime -> FAR is legitimate, no warning
    r2 = validate_thesis(t2, {**FACTS, "menus": menus})
    assert "AUD_DOL_FAR_FOR_REGIME" not in {w.code for w in r2.warnings}

    band_menus = {"dol": {"UP": [{"id": "D1", "level": "prev_day_high", "price": 20000.0,
                                  "tier": "day", "side": "above",
                                  "dist_ratio": 2.6, "band": "BAND"}], "DOWN": []}}
    t3 = valid_thesis()
    t3["regime"] = "RANGE"
    r3 = validate_thesis(t3, {**FACTS, "menus": band_menus})
    assert "AUD_DOL_FAR_FOR_REGIME" not in {w.code for w in r3.warnings}
