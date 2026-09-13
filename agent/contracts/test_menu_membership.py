"""Phase-3 menu-membership + escape-hatch tagging tests (plan 11).

classify_predicates is pure telemetry (menu_hit / escape_hatch per predicate); the gates
remain the strict schema (generation time) and validate_thesis (grounding). These tests
prove: a menu-matching predicate is tagged menu_hit and validates; an off-menu but grounded
predicate is tagged escape_hatch and validates; an off-menu predicate naming a non-facts
level is REJECTED by validate_thesis; and the audit carries the tags.
"""

from validate_contracts import classify_predicates, validate_thesis

# A facts dict with a menu (mirrors derive_facts.build_menus output shape).
FACTS = {
    "now_price": 100.0,
    "levels": {
        "up_pool": {"price": 110.0, "side": "high", "swept": False, "depleted": False},
        "down_pool": {"price": 90.0, "side": "low", "swept": False, "depleted": False},
    },
    "menus": {
        "now_price": 100.0, "daily_mid": 99.0,
        "dol": {
            "UP": [{"id": "D1", "level": "up_pool", "price": 110.0, "body": 109.5,
                    "tier": "day", "side": "above"}],
            "DOWN": [],
        },
        "predicates": {
            "UP": [
                {"id": "F1", "family": "falsification",
                 "predicate": {"type": "n_closes_beyond", "price": 99.0, "side": "below",
                               "tf": "5m", "n": 2}},
                {"id": "X1", "family": "exhaustion",
                 "predicate": {"type": "price_beyond", "price": 110.0, "side": "above"}},
                {"id": "R1", "family": "recall",
                 "predicate": {"type": "time_elapsed", "minutes": 60}},
            ],
            "DOWN": [],
        },
    },
}


def _thesis(falsified=None, recall_events=None) -> dict:
    return {
        "bias": "UP", "regime": "TREND", "confidence": "HIGH",
        "dol": {"level": "up_pool", "price": 110.0},
        "falsified_if": falsified or [],
        "recall": {"events": recall_events or [], "max_age_min": 60},
        "reasoning": "x",
    }


def test_menu_matching_predicate_passes_and_tagged_menu_hit():
    t = _thesis(
        falsified=[{"type": "n_closes_beyond", "price": 99.0, "side": "below",
                    "tf": "5m", "n": 2}],
        recall_events=[{"type": "time_elapsed", "minutes": 60}])
    assert validate_thesis(t, FACTS).ok
    audit = classify_predicates(t, FACTS)
    assert audit["fields"]["falsified_if"][0]["tag"] == "menu_hit"
    assert audit["fields"]["falsified_if"][0]["menu_id"] == "F1"
    assert audit["fields"]["recall"][0]["menu_id"] == "R1"
    assert audit["n_menu_hit"] == 2 and audit["n_escape_hatch"] == 0
    assert audit["menu_hit_ratio"] == 1.0
    assert audit["dol_menu_hit"] and audit["dol_menu_id"] == "D1"


def test_int_vs_float_still_matches_menu():
    # the model may emit 99 where the menu has 99.0 — canonicalisation normalises.
    # Uses F1 (the falsification entry) since the X* exhaustion entries are no longer
    # selectable: exhaustion was removed 2026-08-29 (reaching the DOL IS the exhaustion).
    t = _thesis(falsified=[{"type": "n_closes_beyond", "price": 99, "side": "below",
                            "tf": "5m", "n": 2}])
    audit = classify_predicates(t, FACTS)
    assert audit["fields"]["falsified_if"][0]["tag"] == "menu_hit"


def test_escape_hatch_valid_predicate_passes_with_tag():
    # off-menu but schema-valid and facts-grounded (down_pool exists in facts).
    t = _thesis(falsified=[{"type": "level_swept", "name": "down_pool"}])
    assert validate_thesis(t, FACTS).ok
    audit = classify_predicates(t, FACTS)
    assert audit["fields"]["falsified_if"][0]["tag"] == "escape_hatch"
    assert audit["fields"]["falsified_if"][0]["menu_id"] is None
    assert audit["n_escape_hatch"] == 1


def test_escape_hatch_non_facts_level_rejected():
    t = _thesis(falsified=[{"type": "level_swept", "name": "ghost_level"}])
    r = validate_thesis(t, FACTS)
    assert "SEM_LEVEL_NOT_IN_FACTS" in r.codes()
    # classify still tags it escape_hatch (telemetry is independent of the reject).
    audit = classify_predicates(t, FACTS)
    assert audit["fields"]["falsified_if"][0]["tag"] == "escape_hatch"


def test_neutral_thesis_has_empty_tags():
    t = {"bias": "NEUTRAL", "regime": "RANGE", "confidence": "LOW", "dol": None,
         "falsified_if": [],
         "recall": {"events": [], "max_age_min": 60}, "reasoning": "x"}
    audit = classify_predicates(t, FACTS)
    assert audit["direction"] is None
    assert audit["n_menu_hit"] == 0 and audit["n_escape_hatch"] == 0
    assert audit["menu_hit_ratio"] is None


def test_classify_without_menus_all_escape_hatch():
    # production facts without a menu block → everything is escape_hatch (no crash).
    facts_no_menu = {"now_price": 100.0, "levels": FACTS["levels"]}
    t = _thesis(falsified=[{"type": "price_beyond", "price": 90.0, "side": "below"}])
    audit = classify_predicates(t, facts_no_menu)
    assert audit["fields"]["falsified_if"][0]["tag"] == "escape_hatch"
    assert audit["dol_menu_hit"] is False
