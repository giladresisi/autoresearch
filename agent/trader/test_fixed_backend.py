import pytest

from agent.trader.fixed_backend import (FixedThesisBackend, OracleThesisError,
                                        validate_oracle_thesis)


def _thesis(**over):
    t = {"bias": "DOWN", "regime": "TREND", "confidence": "MEDIUM",
         "dol": {"level": "oracle_dol", "price": 29000.0},
         "falsified_if": [{"type": "price_beyond", "price": 29420.0, "side": "above"}],
         "exhausted_if": [{"type": "price_beyond", "price": 29000.0, "side": "below"}]}
    t.update(over)
    return t


def test_a_well_formed_oracle_thesis_validates():
    assert validate_oracle_thesis(_thesis()) == []


def test_a_thesis_without_falsified_if_is_rejected():
    """Empty means the plan only dies at the DOL — a silent, wrong stress result."""
    errs = validate_oracle_thesis(_thesis(falsified_if=[]))
    assert any("falsified_if" in e for e in errs)


def test_a_thesis_without_exhausted_if_is_rejected():
    errs = validate_oracle_thesis(_thesis(exhausted_if=None))
    assert any("exhausted_if" in e for e in errs)


def test_an_unknown_predicate_kind_is_rejected_loudly():
    errs = validate_oracle_thesis(_thesis(
        falsified_if=[{"type": "vibes_beyond", "price": 1.0}]))
    assert errs


def test_a_malformed_price_beyond_is_rejected():
    errs = validate_oracle_thesis(_thesis(
        falsified_if=[{"type": "price_beyond", "side": "sideways"}]))
    assert errs


def test_a_directional_thesis_without_a_dol_is_rejected():
    errs = validate_oracle_thesis(_thesis(dol=None))
    assert any("dol" in e for e in errs)


def test_an_unknown_bias_is_rejected():
    errs = validate_oracle_thesis(_thesis(bias="SIDEWAYS"))
    assert errs


def test_the_backend_returns_the_thesis_and_a_meta_marked_injected():
    b = FixedThesisBackend(_thesis())
    thesis, meta = b("facts", "", {})
    assert thesis["bias"] == "DOWN"
    assert meta["thesis_source"] == "injected"


def test_the_backend_counts_its_calls():
    b = FixedThesisBackend(_thesis())
    b("f", "", {})
    b("f", "", {})
    assert b.calls == 2


def test_the_backend_refuses_an_invalid_thesis_at_construction():
    with pytest.raises(OracleThesisError):
        FixedThesisBackend(_thesis(falsified_if=[]))


def test_the_backend_returns_a_copy_so_a_caller_cannot_mutate_the_oracle():
    src = _thesis()
    b = FixedThesisBackend(src)
    thesis, _ = b("f", "", {})
    thesis["bias"] = "UP"
    again, _ = b("f", "", {})
    assert again["bias"] == "DOWN"


def test_the_backend_never_touches_a_cache():
    import inspect

    import agent.trader.fixed_backend as mod
    src = inspect.getsource(mod)
    assert "ThesisCache" not in src
    assert "thesis_key" not in src


def test_a_neutral_oracle_is_allowed_and_stands_false():
    """A dark-day oracle is a legitimate experiment; it just must not arm."""
    from agent.trader.analyzer import stands
    t = {"bias": "NEUTRAL", "regime": "RANGE", "confidence": "LOW", "dol": None,
         "falsified_if": [{"type": "clock_after", "et_time": "16:00"}],
         "exhausted_if": [{"type": "clock_after", "et_time": "16:00"}]}
    assert validate_oracle_thesis(t) == []
    assert stands(t) is False
