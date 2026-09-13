"""Tests for the deterministic decision validator (GIL-44 Phase 1).

The validator machine-verifies every LLM decision across three layers
(syntactic / arithmetic / semantic). These tests seed a fixture of every known
past violation and assert the validator catches it, and assert zero false
positives on a clean decision.
"""

import os

import pytest

from validator import validate

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Clean baseline decision (structured), modelled on POC run 3 (2026-06-30).    #
# Every violation test deep-copies this and injects a single defect.           #
# --------------------------------------------------------------------------- #
def clean_decision():
    return {
        "daily_trend": {
            "direction": "up",
            "confidence": "medium",
            "regime": "hybrid",
            "checkpoint": "2026-06-30 09:20:00",
            "day_dol": "day/London high 30217.0",
            "weakens_to_neutral_if": "5m close below 29935.25",
            "flips_if": "2 consecutive 5m closes below 29835.0",
            "drivers": [
                {"id": "D1", "vote": 0, "weight": 3, "contribution": 0.0},
                {"id": "D2", "vote": 1, "weight": 2, "contribution": 2.0},
                {"id": "D3", "vote": 1, "weight": 1, "contribution": 1.0},
                {"id": "D4", "vote": 0, "weight": 2, "contribution": 0.0},
                {"id": "D5", "vote": 0, "weight": 1, "contribution": 0.0},
                {"id": "D6", "vote": 1, "weight": 1, "contribution": 1.0},
            ],
            "S": 4.0,
        },
        "next_move": {
            "direction": "neutral",
            "confidence": "low",
            "move_target": None,
            "flip_trigger": "resolution triggers both ways",
            "flipped_target": None,
            "arm_entry_confirmation": "no",
            "resolution": {
                "long_if": {"condition": "2 x 1m closes above 30076.12",
                            "price": 30076.12, "target": "day high 30217.0"},
                "short_if": {"condition": "2 x 1m closes below 29935.25",
                             "price": 29935.25, "target": "ny_evening(prev1)_low 29835.0"},
            },
            "bull_ledger": [],
            "bear_ledger": [
                {"name": "MES week-high SMT non-confirm", "type": "smt_divergence",
                 "tier": 3.0, "session_side": 1.5, "alignment": 0.3,
                 "freshness": 0.563, "whipsaw": 1.0, "score": 0.760},
                {"name": "london(cur)_low continuation", "type": "sweep",
                 "tier": 1.0, "session_side": 1.0, "alignment": 0.3,
                 "freshness": 0.806, "whipsaw": 1.0, "score": 0.242},
                {"name": "failed reclaim daily mid", "type": "failed_reclaim_daily_mid",
                 "tier": 2.0, "session_side": 1.0, "alignment": 0.3,
                 "freshness": 0.884, "whipsaw": 1.0, "score": 0.530},
            ],
            "N": -1.532,
            "vetoes": [
                {"id": 1, "triggered": False, "effect": "none"},
                {"id": 2, "triggered": False, "effect": "none"},
                {"id": 3, "triggered": False, "effect": "none"},
                {"id": 4, "triggered": True, "effect": "targets_only"},
            ],
        },
    }


def clean_facts():
    return {
        "now_price": 30035.0,
        "checkpoint": "2026-06-30 09:20:00",
        "whipsaw": True,
        "levels": {
            "day high 30217.0": {"price": 30217.0, "side": "above",
                                 "swept": False, "depleted": False},
            "ny_evening(prev1)_low 29835.0": {"price": 29835.0, "side": "below",
                                              "swept": False, "depleted": False},
            "ny_morning(cur)_low": {"price": 29616.5, "side": "below",
                                    "swept": False, "depleted": False},
        },
    }


# --------------------------------------------------------------------------- #
# Baseline: the clean decision passes all layers                              #
# --------------------------------------------------------------------------- #
def test_clean_decision_passes_without_facts():
    result = validate(clean_decision())
    assert result.ok, f"clean decision flagged: {result.codes()}"
    assert result.protocol_clean


def test_clean_decision_passes_with_facts():
    result = validate(clean_decision(), facts=clean_facts())
    assert result.ok, f"clean decision flagged: {result.codes()}"


# --------------------------------------------------------------------------- #
# Layer 1 — Syntactic                                                          #
# --------------------------------------------------------------------------- #
def test_missing_required_field_flagged():
    d = clean_decision()
    del d["daily_trend"]["direction"]
    result = validate(d)
    assert "SYN_MISSING_FIELD" in result.codes()


def test_bad_direction_enum_flagged():
    d = clean_decision()
    d["daily_trend"]["direction"] = "sideways"
    result = validate(d)
    assert "SYN_BAD_ENUM" in result.codes()


def test_bad_confidence_enum_flagged():
    d = clean_decision()
    d["next_move"]["confidence"] = "very-high"
    result = validate(d)
    assert "SYN_BAD_ENUM" in result.codes()


def test_neutral_next_move_missing_resolution_flagged():
    d = clean_decision()
    del d["next_move"]["resolution"]
    result = validate(d)
    assert "SYN_NEUTRAL_MISSING_RESOLUTION" in result.codes()


def test_neutral_missing_one_resolution_trigger_flagged():
    d = clean_decision()
    del d["next_move"]["resolution"]["short_if"]
    result = validate(d)
    assert "SYN_NEUTRAL_MISSING_RESOLUTION" in result.codes()


def test_directional_next_move_missing_target_flagged():
    d = clean_decision()
    d["next_move"]["direction"] = "down"
    d["next_move"]["move_target"] = None
    # N stays -1.532 -> also a direction-arith issue, but we assert on the target one
    result = validate(d)
    assert "SYN_DIRECTIONAL_MISSING_TARGET" in result.codes()


def test_directional_daily_missing_day_dol_flagged():
    d = clean_decision()
    d["daily_trend"]["day_dol"] = None
    result = validate(d)
    assert "SYN_DIRECTIONAL_MISSING_TARGET" in result.codes()


# --------------------------------------------------------------------------- #
# Layer 2 — Arithmetic / protocol                                             #
# --------------------------------------------------------------------------- #
def test_fractional_vote_flagged():
    """Run-4 violation: D1 vote +0.5, D3 vote +0.5 (votes must be -1/0/+1)."""
    d = clean_decision()
    d["daily_trend"]["drivers"][0]["vote"] = 0.5
    d["daily_trend"]["drivers"][0]["contribution"] = 1.5  # 3 x 0.5, internally consistent
    d["daily_trend"]["S"] = 5.5
    result = validate(d)
    assert "ARI_FRACTIONAL_VOTE" in result.codes()


def test_integer_votes_not_flagged():
    d = clean_decision()
    d["daily_trend"]["drivers"][0]["vote"] = -1
    d["daily_trend"]["drivers"][0]["contribution"] = -3.0
    d["daily_trend"]["S"] = 1.0
    d["daily_trend"]["direction"] = "neutral"
    d["daily_trend"]["confidence"] = "low"
    d["daily_trend"]["day_dol"] = None
    result = validate(d)
    assert "ARI_FRACTIONAL_VOTE" not in result.codes()


def test_d1_halved_contribution_allowed():
    """D1 is the sole sanctioned fraction: contribution may be weight x vote x 0.5."""
    d = clean_decision()
    d["daily_trend"]["drivers"][0]["vote"] = 1        # integer vote
    d["daily_trend"]["drivers"][0]["contribution"] = 1.5  # 3 x 1 x 0.5 (1hr disagrees)
    d["daily_trend"]["drivers"][0]["halved"] = True
    d["daily_trend"]["S"] = 5.5
    d["daily_trend"]["confidence"] = "medium"
    result = validate(d)
    assert "ARI_BAD_CONTRIBUTION" not in result.codes()
    assert "ARI_FRACTIONAL_VOTE" not in result.codes()


def test_non_d1_halving_not_allowed():
    """Only D1 may halve; a halved D3 contribution is an arithmetic error."""
    d = clean_decision()
    d["daily_trend"]["drivers"][2]["vote"] = 1     # D3
    d["daily_trend"]["drivers"][2]["contribution"] = 0.5   # 1 x 1 x 0.5 -> illegal for D3
    result = validate(d)
    assert "ARI_BAD_CONTRIBUTION" in result.codes()


def test_contribution_mismatch_flagged():
    d = clean_decision()
    d["daily_trend"]["drivers"][1]["contribution"] = 5.0  # weight 2 x vote 1 != 5
    result = validate(d)
    assert "ARI_BAD_CONTRIBUTION" in result.codes()


def test_sum_S_mismatch_flagged():
    d = clean_decision()
    d["daily_trend"]["S"] = 9.0  # real sum is 4.0
    result = validate(d)
    assert "ARI_S_MISMATCH" in result.codes()


def test_daily_direction_inconsistent_with_S_flagged():
    d = clean_decision()
    d["daily_trend"]["direction"] = "down"  # S = +4 -> should be up
    result = validate(d)
    assert "ARI_DAILY_DIRECTION" in result.codes()


def test_daily_confidence_overclaim_flagged():
    d = clean_decision()
    d["daily_trend"]["confidence"] = "high"  # |S| = 4 < 6, cannot be high
    result = validate(d)
    assert "ARI_DAILY_CONFIDENCE" in result.codes()


def test_daily_confidence_underclaim_allowed():
    """Down-tiering (e.g. unverifiable input penalty) is always allowed."""
    d = clean_decision()
    d["daily_trend"]["confidence"] = "low"  # |S| = 4 supports medium; low is conservative
    result = validate(d)
    assert "ARI_DAILY_CONFIDENCE" not in result.codes()


def test_ledger_item_score_mismatch_flagged():
    d = clean_decision()
    d["next_move"]["bear_ledger"][0]["score"] = 2.5  # product is 0.760
    result = validate(d)
    assert "ARI_ITEM_SCORE" in result.codes()


def test_bad_tier_weight_flagged():
    d = clean_decision()
    item = d["next_move"]["bear_ledger"][0]
    item["tier"] = 2.5  # not in {3.0, 2.0, 1.5, 1.0}
    item["score"] = 2.5 * 1.5 * 0.3 * 0.563 * 1.0  # keep score==product so only tier trips
    result = validate(d)
    assert "ARI_BAD_MULTIPLIER" in result.codes()


def test_invented_item_type_flagged():
    """Known past violation: inventing a ledger item type outside the closed list."""
    d = clean_decision()
    d["next_move"]["bear_ledger"][0]["type"] = "lunar_phase_alignment"
    result = validate(d)
    assert "ARI_INVENTED_ITEM_TYPE" in result.codes()


def test_all_closed_list_item_types_allowed():
    d = clean_decision()
    types = ["smt_divergence", "sweep", "failed_reclaim_daily_mid",
             "displacement_mss", "mid_rejection", "sustained_acceptance", "laggard_fail"]
    d["next_move"]["bear_ledger"] = [
        {"name": f"item {t}", "type": t, "tier": 1.0, "session_side": 1.0,
         "alignment": 0.3, "freshness": 0.5, "whipsaw": 1.0, "score": 0.15}
        for t in types
    ]
    d["next_move"]["N"] = -0.15 * len(types)
    result = validate(d)
    assert "ARI_INVENTED_ITEM_TYPE" not in result.codes()


def test_N_mismatch_flagged():
    d = clean_decision()
    d["next_move"]["N"] = 5.0  # bull 0 - bear 1.532 = -1.532
    result = validate(d)
    assert "ARI_N_MISMATCH" in result.codes()


def test_next_direction_inconsistent_with_N_flagged():
    d = clean_decision()
    d["next_move"]["direction"] = "down"  # N = -1.532 in (-3, 3) -> neutral
    d["next_move"]["move_target"] = {"level": "x", "price": 1.0}
    result = validate(d)
    assert "ARI_NEXT_DIRECTION" in result.codes()


def test_next_confidence_overclaim_flagged():
    d = clean_decision()
    # make a clean directional-down next-move but overclaim high
    d["next_move"]["bear_ledger"][0]["score"] = 3.5
    d["next_move"]["bear_ledger"][0]["tier"] = 3.0
    d["next_move"]["bear_ledger"][0]["session_side"] = 1.5
    d["next_move"]["bear_ledger"][0]["alignment"] = 1.0
    d["next_move"]["bear_ledger"][0]["freshness"] = 0.778
    d["next_move"]["bear_ledger"][0]["whipsaw"] = 1.0
    # score = 3.0*1.5*1.0*0.778*1.0 = 3.501
    d["next_move"]["bear_ledger"][1]["score"] = 0.242
    d["next_move"]["bear_ledger"][2]["score"] = 0.530
    total = 3.501 + 0.242 + 0.530
    d["next_move"]["N"] = -total  # ~ -4.27 -> down, |N|<6 so high not allowed
    d["next_move"]["direction"] = "down"
    d["next_move"]["confidence"] = "high"
    d["next_move"]["move_target"] = {"level": "ny_morning(cur)_low", "price": 29616.5}
    del d["next_move"]["resolution"]
    result = validate(d)
    assert "ARI_NEXT_CONFIDENCE" in result.codes()


def test_pattern_vs_event_double_count_flagged():
    """Known past violation: two drivers claim the same underlying event at full
    weight (correlation audit skipped) — the second appearance must be x0.5."""
    d = clean_decision()
    drv = d["daily_trend"]["drivers"]
    # D2 and D4 (both weight-2) both cite the same overnight mid-reclaim event at
    # full weight, with no sharing discount applied to the second.
    drv[1]["vote"] = -1
    drv[1]["contribution"] = -2.0
    drv[1]["events"] = ["overnight_mid_reclaim_fail"]
    drv[3]["vote"] = -1
    drv[3]["contribution"] = -2.0
    drv[3]["events"] = ["overnight_mid_reclaim_fail"]
    d["daily_trend"]["S"] = -2.0  # 0 -2 +1 -2 +0 +1
    d["daily_trend"]["direction"] = "neutral"
    d["daily_trend"]["confidence"] = "low"
    d["daily_trend"]["day_dol"] = None
    result = validate(d)
    assert "ARI_PATTERN_EVENT_DOUBLE_COUNT" in result.codes()


def test_shared_event_with_discount_not_flagged():
    """When the second driver marks the shared event discounted, it is legal."""
    d = clean_decision()
    drv = d["daily_trend"]["drivers"]
    drv[1]["vote"] = -1
    drv[1]["contribution"] = -2.0
    drv[1]["events"] = ["overnight_mid_reclaim_fail"]
    drv[3]["vote"] = -1
    drv[3]["contribution"] = -1.0  # weight 2 x -1 x 0.5 (second-appearance discount)
    drv[3]["events"] = ["overnight_mid_reclaim_fail"]
    drv[3]["discounted_events"] = ["overnight_mid_reclaim_fail"]
    d["daily_trend"]["S"] = -1.0
    d["daily_trend"]["direction"] = "neutral"
    d["daily_trend"]["confidence"] = "low"
    d["daily_trend"]["day_dol"] = None
    result = validate(d)
    assert "ARI_PATTERN_EVENT_DOUBLE_COUNT" not in result.codes()


def test_shared_event_ignored_when_second_driver_votes_zero():
    """A driver voting 0 consumes nothing (daily-trend.md §2) — no double count."""
    d = clean_decision()
    drv = d["daily_trend"]["drivers"]
    drv[1]["events"] = ["shared_e"]           # D2 votes +1, contribution 2
    drv[3]["events"] = ["shared_e"]           # D4 votes 0, contribution 0
    result = validate(d)
    assert "ARI_PATTERN_EVENT_DOUBLE_COUNT" not in result.codes()


# --------------------------------------------------------------------------- #
# Layer 3 — Semantic sanity (facts required)                                  #
# --------------------------------------------------------------------------- #
def _down_move(d):
    """Convert clean neutral next-move into a clean directional-down one."""
    d["next_move"]["direction"] = "down"
    d["next_move"]["confidence"] = "low"
    d["next_move"]["N"] = -1.532
    del d["next_move"]["resolution"]
    return d


def test_wrong_side_target_flagged():
    """Known past violation: stated target on the wrong side of price."""
    d = _down_move(clean_decision())
    # down move but target is ABOVE current price 30035
    d["next_move"]["move_target"] = {"level": "day high 30217.0", "price": 30217.0}
    result = validate(d, facts=clean_facts())
    assert "SEM_TARGET_WRONG_SIDE" in result.codes()


def test_correct_side_target_not_flagged():
    d = _down_move(clean_decision())
    d["next_move"]["move_target"] = {"level": "ny_morning(cur)_low", "price": 29616.5}
    result = validate(d, facts=clean_facts())
    assert "SEM_TARGET_WRONG_SIDE" not in result.codes()


def test_target_not_in_facts_flagged():
    d = _down_move(clean_decision())
    d["next_move"]["move_target"] = {"level": "imaginary_pool_12345", "price": 29000.0}
    result = validate(d, facts=clean_facts())
    assert "SEM_TARGET_NOT_IN_FACTS" in result.codes()


def test_swept_depleted_target_flagged():
    d = _down_move(clean_decision())
    facts = clean_facts()
    facts["levels"]["ny_morning(cur)_low"]["swept"] = True
    facts["levels"]["ny_morning(cur)_low"]["depleted"] = True
    d["next_move"]["move_target"] = {"level": "ny_morning(cur)_low", "price": 29616.5}
    result = validate(d, facts=facts)
    assert "SEM_TARGET_SWEPT_DEPLETED" in result.codes()


def test_resolution_wrong_side_flagged():
    d = clean_decision()
    # long trigger price BELOW current price is wrong-side
    d["next_move"]["resolution"]["long_if"]["price"] = 29000.0
    result = validate(d, facts=clean_facts())
    assert "SEM_RESOLUTION_WRONG_SIDE" in result.codes()


def test_checkpoint_mismatch_flagged():
    d = clean_decision()
    d["daily_trend"]["checkpoint"] = "2026-06-30 13:00:00"  # facts say 09:20
    result = validate(d, facts=clean_facts())
    assert "SEM_CHECKPOINT_MISMATCH" in result.codes()


def test_semantic_skipped_without_facts():
    """No facts -> semantic layer is skipped, never a false positive."""
    d = _down_move(clean_decision())
    d["next_move"]["move_target"] = {"level": "day high 30217.0", "price": 30217.0}
    result = validate(d)  # no facts
    assert "SEM_TARGET_WRONG_SIDE" not in result.codes()


# --------------------------------------------------------------------------- #
# Failure policy helpers                                                       #
# --------------------------------------------------------------------------- #
def test_failsafe_decision_is_neutral_low():
    from validator import failsafe_decision
    fs = failsafe_decision()
    assert fs["daily_trend"]["direction"] == "neutral"
    assert fs["daily_trend"]["confidence"] == "low"
    assert fs["next_move"]["direction"] == "neutral"
    assert fs["next_move"]["confidence"] == "low"
    assert validate(fs).ok  # the fail-safe must itself be valid


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
