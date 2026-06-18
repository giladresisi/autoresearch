# tests/test_stop_utils.py
# Pure-function tests for stop_utils.valid_stop_for_fill — the single source of truth
# for "a valid protective stop given a fill" (GIL-36). No fixtures / no browser.

import pytest

from stop_utils import MIN_STOP_DISTANCE, valid_stop_for_fill


def test_min_stop_distance_constant():
    # Must stay in sync with live_orders._MIN_FILL_STOP_DISTANCE = 10.0
    assert MIN_STOP_DISTANCE == 10.0


def test_valid_unchanged_long():
    # stop is 20pt below fill (>= 10) → already valid → returned unchanged
    assert valid_stop_for_fill("long", 30400.0, 30380.0, 30390.0) == pytest.approx(30380.0)


def test_valid_unchanged_short():
    # stop is 20pt above fill (>= 10) → already valid → returned unchanged
    assert valid_stop_for_fill("short", 30400.0, 30420.0, 30410.0) == pytest.approx(30420.0)


def test_long_wrong_side_ex1_stp_mkt():
    # The exact 2026-06-17 10:11 STP->MKT case: fill 30366.25, intended stop 30368.75 sits
    # ABOVE the fill (wrong side) → re-anchor to fill - risk; risk = max(|30368.75-30388.75|,10)=20.
    assert valid_stop_for_fill("long", 30366.25, 30368.75, 30388.75) == pytest.approx(30346.25)


def test_short_wrong_side():
    # fill 30400, intended stop 30398 is BELOW fill+10 (wrong side) →
    # risk = max(|30398-30380|, 10) = 18 → fill + risk = 30418
    assert valid_stop_for_fill("short", 30400.0, 30398.0, 30380.0) == pytest.approx(30418.0)


def test_too_close_long_risk_floored():
    # stop 30398 is within 10pt below fill (not <= fill-10) → re-anchor;
    # risk = max(|30398-30401|, 10) = 10 → fill - 10 = 30390
    assert valid_stop_for_fill("long", 30400.0, 30398.0, 30401.0) == pytest.approx(30390.0)


def test_too_close_short_risk_floored():
    # stop 30402 not >= fill+10 → re-anchor; risk = max(|30402-30399|, 10) = 10 → fill + 10 = 30410
    assert valid_stop_for_fill("short", 30400.0, 30402.0, 30399.0) == pytest.approx(30410.0)


def test_direction_alias_up_matches_long():
    # "up" behaves identically to "long" (case 3 inputs) → 30346.25
    assert valid_stop_for_fill("up", 30366.25, 30368.75, 30388.75) == pytest.approx(30346.25)


def test_direction_alias_down_matches_short():
    # "down" behaves identically to "short" (case 4 inputs) → 30418.0
    assert valid_stop_for_fill("down", 30400.0, 30398.0, 30380.0) == pytest.approx(30418.0)


def test_unknown_direction_raises():
    # The single source of truth must fail loudly on an unknown direction rather than
    # silently defaulting to "short" (a typo would otherwise place a wrong-side stop).
    with pytest.raises(ValueError):
        valid_stop_for_fill("sideways", 30400.0, 30398.0, 30380.0)
    with pytest.raises(ValueError):
        valid_stop_for_fill("", 30400.0, 30398.0, 30380.0)
