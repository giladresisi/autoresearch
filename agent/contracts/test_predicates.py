"""Phase-1 predicate language tests (plan §Phase 1 test cases)."""

import pandas as pd

from predicates import (
    MarketView, eval_predicate, validate_predicate, validate_predicate_list,
)


def _mv(price=100.0, closes=None, swept=None, depleted=None, now=None, since=None):
    return MarketView(price=price, closes_by_tf=closes or {}, swept=set(swept or []),
                      depleted=set(depleted or []), now=now, since=since)


# -- each atom true / false ------------------------------------------------- #
def test_price_beyond_above_true_false():
    assert eval_predicate({"type": "price_beyond", "price": 90, "side": "above"}, _mv(100))
    assert not eval_predicate({"type": "price_beyond", "price": 110, "side": "above"}, _mv(100))


def test_price_beyond_below_true_false():
    assert eval_predicate({"type": "price_beyond", "price": 110, "side": "below"}, _mv(100))
    assert not eval_predicate({"type": "price_beyond", "price": 90, "side": "below"}, _mv(100))


def test_level_swept_and_depleted():
    mv = _mv(swept=["prev_day_high"], depleted=["overnight_low"])
    assert eval_predicate({"type": "level_swept", "name": "prev_day_high"}, mv)
    assert not eval_predicate({"type": "level_swept", "name": "overnight_low"}, mv)
    assert eval_predicate({"type": "level_depleted", "name": "overnight_low"}, mv)


def test_time_elapsed_and_clock_after():
    now = pd.Timestamp("2026-05-19 10:30", tz="America/New_York")
    since = pd.Timestamp("2026-05-19 09:00", tz="America/New_York")
    mv = _mv(now=now, since=since)
    assert eval_predicate({"type": "time_elapsed", "minutes": 60}, mv)          # 90 >= 60
    assert not eval_predicate({"type": "time_elapsed", "minutes": 120}, mv)     # 90 < 120
    assert eval_predicate({"type": "clock_after", "et_time": "10:00"}, mv)
    assert not eval_predicate({"type": "clock_after", "et_time": "11:00"}, mv)


def test_time_predicates_missing_refs_are_false():
    # No since/now → cannot evaluate → False (never a spurious firing).
    assert not eval_predicate({"type": "time_elapsed", "minutes": 1}, _mv())
    assert not eval_predicate({"type": "clock_after", "et_time": "00:00"}, _mv())


# -- n_closes_beyond boundary (n-1 vs n) ------------------------------------ #
def test_n_closes_beyond_boundary():
    pred = {"type": "n_closes_beyond", "price": 100, "side": "below", "tf": "5m", "n": 2}
    # Only 1 close below (n-1) → False; last two below (n) → True.
    assert not eval_predicate(pred, _mv(closes={"5m": [101, 102, 99]}))
    assert eval_predicate(pred, _mv(closes={"5m": [101, 98, 99]}))
    # Fewer than n closes available → False.
    assert not eval_predicate(pred, _mv(closes={"5m": [99]}))


# -- all_of / any_of nesting ------------------------------------------------ #
def test_all_of_any_of_nesting():
    a = {"type": "price_beyond", "price": 90, "side": "above"}    # True at 100
    b = {"type": "price_beyond", "price": 110, "side": "above"}   # False at 100
    assert eval_predicate({"type": "any_of", "of": [a, b]}, _mv(100))
    assert not eval_predicate({"type": "all_of", "of": [a, b]}, _mv(100))
    nested = {"type": "all_of", "of": [a, {"type": "any_of", "of": [b, a]}]}
    assert eval_predicate(nested, _mv(100))


# -- validation: unknown type + malformed atoms rejected -------------------- #
def test_unknown_predicate_type_rejected():
    assert validate_predicate({"type": "moon_phase", "x": 1})
    assert validate_predicate({"type": "price_beyond", "price": 1, "side": "sideways"})
    assert validate_predicate({"type": "n_closes_beyond", "price": 1, "side": "above",
                               "tf": "3m", "n": 2})   # bad tf
    assert validate_predicate({"type": "n_closes_beyond", "price": 1, "side": "above",
                               "tf": "5m", "n": 0})   # n < 1
    assert validate_predicate({"type": "clock_after", "et_time": "25:00"})
    assert validate_predicate({"type": "all_of", "of": []})       # empty composite


def test_valid_predicates_accepted():
    for p in (
        {"type": "price_beyond", "price": 1.5, "side": "below"},
        {"type": "n_closes_beyond", "price": 1, "side": "above", "tf": "1h", "n": 3},
        {"type": "level_swept", "name": "x"},
        {"type": "time_elapsed", "minutes": 30},
        {"type": "clock_after", "et_time": "09:20"},
        {"type": "any_of", "of": [{"type": "price_beyond", "price": 1, "side": "above"}]},
    ):
        assert validate_predicate(p) == [], p


def test_validate_predicate_list_reports_index():
    errs = validate_predicate_list([{"type": "price_beyond", "price": 1, "side": "above"},
                                    {"type": "bogus"}], "field")
    assert errs and "field[1]" in errs[0]
