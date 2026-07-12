"""Phase-1 strict predicate-schema tests (plan 11).

The predicate vocabulary is enforced at GENERATION time by the structured-output JSON
schema itself: these tests validate the `_PREDICATE` fragment (and the full THESIS /
TRADE_PLAN schemas that embed it) with jsonschema, proving an unknown predicate `type`
is impossible to emit and that composition is capped at depth 1.
"""

import jsonschema
from jsonschema import Draft202012Validator as V

from schemas import (
    _ATOM_REFS, THESIS_SCHEMA, TRADE_PLAN_SCHEMA, predicate_schema,
    failsafe_plan, failsafe_thesis,
)
from predicates import validate_predicate

_PV = V(predicate_schema())


def _atom_examples() -> list:
    return [
        {"type": "price_beyond", "price": 20000.0, "side": "above"},
        {"type": "n_closes_beyond", "price": 19500.0, "side": "below", "tf": "5m", "n": 2},
        {"type": "level_swept", "name": "prev_day_high"},
        {"type": "level_depleted", "name": "prev_day_low"},
        {"type": "time_elapsed", "minutes": 30},
        {"type": "clock_after", "et_time": "09:30"},
    ]


# --------------------------------------------------------------------------- #
# The fragment (and the schemas embedding it) are themselves valid schemas.     #
# --------------------------------------------------------------------------- #
def test_schemas_are_well_formed():
    V.check_schema(predicate_schema())
    V.check_schema(THESIS_SCHEMA)
    V.check_schema(TRADE_PLAN_SCHEMA)


def test_one_fragment_per_atom():
    # six atom $refs + two composites in the predicate anyOf.
    assert len(_ATOM_REFS) == 6
    assert len(predicate_schema()["anyOf"]) == 8


# --------------------------------------------------------------------------- #
# Each atom is accepted by the schema.                                          #
# --------------------------------------------------------------------------- #
def test_every_atom_accepted():
    for atom in _atom_examples():
        assert _PV.is_valid(atom), atom


# --------------------------------------------------------------------------- #
# An unknown type is impossible to generate — rejected BY THE SCHEMA.           #
# --------------------------------------------------------------------------- #
def test_unknown_type_rejected_by_schema():
    assert not _PV.is_valid({"type": "telepathy", "price": 1.0, "side": "above"})
    # the exact SYN_BAD_PREDICATE shape the bench saw (type: None) is now unschemable.
    assert not _PV.is_valid({"type": None})
    assert not _PV.is_valid({"price": 1.0, "side": "above"})   # no type at all


def test_atom_missing_or_extra_param_rejected_by_schema():
    assert not _PV.is_valid({"type": "price_beyond", "price": 1.0})            # missing side
    assert not _PV.is_valid({"type": "price_beyond", "price": 1.0, "side": "sideways"})
    assert not _PV.is_valid({"type": "price_beyond", "price": 1.0, "side": "above",
                             "extra": 1})                                       # extra prop
    assert not _PV.is_valid({"type": "n_closes_beyond", "price": 1.0, "side": "above",
                             "tf": "3m", "n": 2})                               # bad tf


def test_numeric_bounds_enforced_by_validator_not_schema():
    # minimum/exclusiveMinimum are omitted from the schema (Anthropic rejects them); the
    # deterministic predicate validator carries the bound instead.
    n0 = {"type": "n_closes_beyond", "price": 1.0, "side": "above", "tf": "5m", "n": 0}
    assert _PV.is_valid(n0)                       # schema no longer rejects n < 1
    assert validate_predicate(n0)                 # but the validator does
    m0 = {"type": "time_elapsed", "minutes": 0}
    assert _PV.is_valid(m0)
    assert validate_predicate(m0)


# --------------------------------------------------------------------------- #
# Composition: depth-1 accepted, depth-2 rejected by the schema.                #
# --------------------------------------------------------------------------- #
def test_all_of_over_atoms_accepted():
    comp = {"type": "all_of", "of": _atom_examples()}
    assert _PV.is_valid(comp)
    assert _PV.is_valid({"type": "any_of", "of": _atom_examples()[:2]})


def test_empty_composite_rejected_by_validator():
    # minItems is omitted from the schema; the predicate validator rejects an empty `of`.
    empty = {"type": "all_of", "of": []}
    assert _PV.is_valid(empty)
    assert validate_predicate(empty)


def test_nested_composite_rejected_by_schema():
    # all_of nested inside any_of — depth 2 — must be rejected by the schema (backends
    # handle recursive schemas poorly; the evaluator still supports nesting at runtime).
    nested = {"type": "any_of",
              "of": [{"type": "all_of",
                      "of": [{"type": "price_beyond", "price": 1.0, "side": "above"}]}]}
    assert not _PV.is_valid(nested)


# --------------------------------------------------------------------------- #
# The full schemas accept the fail-safe blocks and reject a bad predicate.      #
# --------------------------------------------------------------------------- #
def test_thesis_schema_accepts_failsafe_and_valid_predicates():
    t = failsafe_thesis()
    t["falsified_if"] = [_atom_examples()[1]]
    t["exhausted_if"] = [{"type": "all_of", "of": _atom_examples()[:2]}]
    V(THESIS_SCHEMA).validate(t)


def test_thesis_schema_rejects_unknown_predicate():
    t = failsafe_thesis()
    t["falsified_if"] = [{"type": "bogus"}]
    assert not V(THESIS_SCHEMA).is_valid(t)


def test_trade_plan_schema_accepts_failsafe():
    V(TRADE_PLAN_SCHEMA).validate(failsafe_plan())


def test_failsafe_thesis_recall_max_age_is_60():
    # plan 11 Phase 4: a failsafe schedules its own retry.
    assert failsafe_thesis()["recall"]["max_age_min"] == 60
