"""Task 2: the DOL tracks and the initial target.

Two of these are 2026-09-02 pins. That date GENERATED the policy, so its numbers are an
acceptance test and never evidence — but that is exactly what makes them the right thing
to pin here: they were derived by hand in plan 33 §3, independently of this code, so
agreeing with them is a real check rather than a tautology.
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import targets_for_policy as tfp                  # noqa: E402

TZ = "America/New_York"
pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def facts():
    from agent.study.target_offline import OfflineFacts
    try:
        return OfflineFacts(source="1m")
    except (FileNotFoundError, OSError):                    # pragma: no cover
        pytest.skip("1m parquets unavailable")


@pytest.fixture(scope="module")
def bars():
    from agent.study import legacy_liquidities as ll
    try:
        return ll.load_1m("MNQ")
    except (FileNotFoundError, OSError):                    # pragma: no cover
        pytest.skip("1m parquet unavailable")


# --------------------------------------------------------------------------- #
# the production DOL                                                            #
# --------------------------------------------------------------------------- #

def test_the_production_dol_is_build_menus_d1_and_not_a_second_copy_of_the_rule(facts):
    """It must BE the menu's first row, not a re-derivation that happens to agree."""
    from agent.study.target_offline import menu_at, instant_ts

    got = tfp.production_dol(facts, "2026-09-02", "UP")
    rows = menu_at(facts, instant_ts("2026-09-02", tfp.L1_BOUNDARY_HHMM), "UP")["rows"]
    assert got["row"] == dict(rows[0])
    assert got["n_rows"] == len(rows)


def test_sep2_production_d1_is_the_projection_and_the_day_high_is_filtered(facts):
    """§3 D3, at the fill: `avg_range_1h` 79.9125 puts the draw floor at 79.91, and
    `day_high` 29171.25 sits 74.25 above price — INSIDE the floor, therefore excluded.
    D1 is the synthetic `projection_up`, and price never reaches it. This is the
    no-reachable-target session the design set out to examine, so the surprise is pinned
    rather than described."""
    got = tfp.production_dol(facts, "2026-09-02", "UP", clock="10:07")
    assert got["avg_range_1h"] == pytest.approx(79.9125)
    assert got["now_price"] == pytest.approx(29097.0)
    assert got["row"]["level"] == "projection_up"
    assert got["row"]["price"] == pytest.approx(29251.25)
    assert got["row"]["band"] == "PROJECTION"
    assert got["row"]["dist_ratio"] == pytest.approx(1.9302)
    # the day high the user's narrative named is 74.25 away, inside the 79.91 floor
    assert 29171.25 - 29097.0 < got["avg_range_1h"]


def test_the_entry_dol_is_the_0920_menu_and_the_fill_menu_is_recorded_beside_it(facts):
    """The settled divergence, asserted rather than left in prose: what the policy is
    handed is the 09:20 decision; the fill-time menu is carried but never substituted."""
    assert tfp.L1_BOUNDARY_HHMM == "09:20"
    at_arm = tfp.production_dol(facts, "2026-09-02", "UP")
    at_fill = tfp.production_dol(facts, "2026-09-02", "UP", clock="10:07")

    t = tfp.targets_for("2026-09-02", "UP", tfp.TRACK_PRODUCTION, facts=facts,
                        fill_price=29097.0,
                        fill_ts=pd.Timestamp("2026-09-02 10:07", tz=TZ))
    assert t.dol == pytest.approx(at_arm["row"]["price"])
    assert t.dol_source == "build_menus_d1_09:20"
    assert t.d1_at_fill == pytest.approx(at_fill["row"]["price"])


def test_absent_dol_is_reported_not_silently_defaulted(facts, monkeypatch):
    """An empty menu is clause 14, and it must arrive as `None` rather than as the
    nearest thing that happened to be lying around."""
    monkeypatch.setattr(tfp, "menu_at",
                        lambda *_a, **_kw: {"rows": (), "now_price": 1.0,
                                            "avg_range_1h": 2.0})
    got = tfp.production_dol(facts, "2026-09-02", "UP")
    assert got["row"] is None and got["n_rows"] == 0

    t = tfp.targets_for("2026-09-02", "UP", tfp.TRACK_PRODUCTION, facts=facts)
    assert t.dol is None and t.dol_level is None
    assert t.dol_source == "build_menus_d1_09:20"       # the source is still reported


# --------------------------------------------------------------------------- #
# the oracle DOL                                                                #
# --------------------------------------------------------------------------- #

def test_an_unexplained_session_yields_no_oracle_dol():
    assert tfp.oracle_dol({"status": "unexplained", "price": None}) == (None, None)
    assert tfp.oracle_dol(None) == (None, None)
    assert tfp.oracle_dol({"status": "labelled", "price": 100.0,
                           "names": ["asia(cur)_high"]}) == (100.0, "asia(cur)_high")


def test_the_label_corpus_is_read_primary_only():
    labels = tfp.load_labels()
    assert len(labels) == 84
    assert all(r["role"] == "primary" and r["ticker"] == "MNQ"
               for r in labels.values())
    n_labelled = sum(1 for r in labels.values() if r["status"] == "labelled")
    assert n_labelled == 64          # the other 20 are `unexplained` -> clause 14


# --------------------------------------------------------------------------- #
# the initial target                                                            #
# --------------------------------------------------------------------------- #

def test_the_initial_target_is_compute_cautious_prices_initial(bars):
    """Not the secondary rung, and not a level this module picked."""
    from hypothesis import compute_cautious_prices
    from agent.study import legacy_liquidities as ll

    at = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    price, level = tfp.initial_target(bars, "UP", 29097.0, at)
    cp = compute_cautious_prices("up", 29097.0, ll.reconstruct(bars, at),
                                 ll.historical_ath(bars, at), 0,
                                 invalidated_names=None, now=at)
    assert price == pytest.approx(cp["cautious_price_initial"])
    assert level == cp["cautious_price_initial_level"]
    assert price != pytest.approx(cp["cautious_price_secondary"])


def test_sep2_initial_target_is_ny_morning_high_at_29147_25(bars):
    """Plan 33 §3: `cautious_price_initial = 29147.25`, level `ny_morning_high` — the raw
    level 29149.25 minus the 2.0-pt offset, and the price clause 6 compares closes
    against (settled per §3 D4)."""
    at = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    price, level = tfp.initial_target(bars, "UP", 29097.0, at)
    assert price == pytest.approx(29147.25)
    assert level == "ny_morning_high"


def test_an_empty_ladder_is_reported_as_absent_rather_than_as_a_price(bars):
    """Clause 12's input. A fill far above every candidate leaves nothing in the capped
    pool, and that must arrive as None."""
    at = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    price, level = tfp.initial_target(bars, "UP", 999999.0, at)
    assert price is None and level is None


def test_an_unknown_direction_is_absent_rather_than_silently_empty(bars):
    at = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    assert tfp.initial_target(bars, "SIDEWAYS", 29097.0, at) == (None, None)
    # ... and the two spellings production uses both work
    assert tfp.initial_target(bars, "LONG", 29097.0, at)[0] == pytest.approx(29147.25)


def test_the_ladder_is_anchored_at_the_fill_not_at_the_arm(bars):
    """`recompute_cautious_for_fill`'s whole point: a ladder anchored at formation is
    stale by the time the entry fills."""
    at = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    near = tfp.initial_target(bars, "UP", 29097.0, at)
    far = tfp.initial_target(bars, "UP", 29140.0, at)
    assert near[0] != far[0] or near[1] != far[1]
