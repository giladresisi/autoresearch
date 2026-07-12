"""Phase-2 S8 menu tests (plan 11): DOL menu + predicate menu (config-driven).

Unit-level over synthetic bundle + validator-dict inputs (no parquet): the menu builder
is pure over (bundle.levels["MNQ"], bundle.day_mid, vd["levels"], vd["now_price"]).
"""

import json

from derive_facts import (
    FactsBundle, build_menus, render_menus_text, _MENU_PREDICATE_CFG,
)


# level tuple = (price, body_price, side, tier, active_from)
def _bundle():
    b = FactsBundle()
    b.now_price = 100.0
    b.day_mid = 99.0
    b.levels = {"MNQ": {
        "up_pool":       (110.0, 109.5, "above", "day", None),      # UP draw
        "up_pool_far":   (120.0, 119.0, "above", "week", None),     # UP draw
        "up_swept":      (108.0, 107.5, "above", "day", None),      # excluded: swept
        "up_depleted":   (112.0, 111.5, "above", "day", None),      # excluded: depleted
        "wrong_side":    (95.0, 94.5, "above", "day", None),        # excluded: resistance below price
        "down_pool":     (90.0, 90.5, "below", "day", None),        # DOWN draw + UP anti-pool
        "down_swept":    (92.0, 92.5, "below", "day", None),        # excluded: swept
        "sideless":      (100.0, None, None, "session", None),      # excluded: no side
    }}
    return b


def _vd():
    return {"now_price": 100.0, "levels": {
        "up_pool":     {"price": 110.0, "side": "high", "swept": False, "depleted": False},
        "up_pool_far": {"price": 120.0, "side": "high", "swept": False, "depleted": False},
        "up_swept":    {"price": 108.0, "side": "high", "swept": True,  "depleted": False},
        "up_depleted": {"price": 112.0, "side": "high", "swept": False, "depleted": True},
        "wrong_side":  {"price": 95.0,  "side": "high", "swept": False, "depleted": False},
        "down_pool":   {"price": 90.0,  "side": "low",  "swept": False, "depleted": False},
        "down_swept":  {"price": 92.0,  "side": "low",  "swept": True,  "depleted": False},
    }}


def test_dol_menu_only_eligible_pools():
    m = build_menus(_bundle(), _vd())
    up_levels = {e["level"] for e in m["dol"]["UP"]}
    down_levels = {e["level"] for e in m["dol"]["DOWN"]}
    assert up_levels == {"up_pool", "up_pool_far"}
    assert down_levels == {"down_pool"}
    # swept / depleted / wrong-side / side-less all excluded from BOTH directions.
    for bad in ("up_swept", "up_depleted", "wrong_side", "down_swept", "sideless"):
        assert bad not in up_levels and bad not in down_levels


def test_dol_menu_nearest_first_and_ids():
    m = build_menus(_bundle(), _vd())
    up = m["dol"]["UP"]
    assert [e["id"] for e in up] == ["D1", "D2"]
    assert up[0]["level"] == "up_pool" and up[1]["level"] == "up_pool_far"   # nearest first


def test_predicate_ids_unique_and_params_concrete():
    m = build_menus(_bundle(), _vd())
    for direction in ("UP", "DOWN"):
        entries = m["predicates"][direction]
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids)), f"dup ids in {direction}: {ids}"
        for e in entries:
            pred = e["predicate"]
            assert isinstance(pred.get("type"), str)
            # every param is a concrete literal (no placeholders / None).
            for k, v in pred.items():
                assert v is not None, (e["id"], k)
            if pred["type"] in ("price_beyond", "n_closes_beyond"):
                assert isinstance(pred["price"], (int, float))
                assert pred["side"] in ("above", "below")


def test_predicate_menu_families_present():
    m = build_menus(_bundle(), _vd())
    up_fams = {e["family"] for e in m["predicates"]["UP"]}
    # UP thesis: falsification (daily-mid + anti-pool), exhaustion (per DOL), recall.
    assert {"falsification", "exhaustion", "recall"} <= up_fams
    # exhaustion predicates == one price_beyond per UP DOL entry.
    x = [e for e in m["predicates"]["UP"] if e["family"] == "exhaustion"]
    assert len(x) == len(m["dol"]["UP"])
    assert all(e["predicate"]["type"] == "price_beyond" for e in x)
    # an anti-pool falsification exists (down_pool at 90, below price, side below).
    f_prices = {e["predicate"].get("price") for e in m["predicates"]["UP"]
                if e["predicate"]["type"] == "price_beyond"}
    assert 90.0 in f_prices


def test_config_drives_generation_no_hardcoded_level_names():
    # The config names families/level-classes/builders — never a concrete facts level
    # name (level CLASSES like "daily_mid"/"anti_pools" are fine; the names below are
    # actual facts-sheet level identifiers and must be resolved at build time).
    blob = json.dumps(_MENU_PREDICATE_CFG, default=str)
    for level_name in ("up_pool", "down_pool", "prev_day_high", "prev1_week_high",
                       "london(cur)_high", "asia(prev1)_low", "TDO", "TWO"):
        assert level_name not in blob


def _bundle_with_menus():
    b = _bundle()
    b.menus = build_menus(b, _vd())
    return b


def test_menu_text_deterministic():
    a = render_menus_text(_bundle_with_menus())
    b = render_menus_text(_bundle_with_menus())
    assert a == b
    assert a.startswith("## S8 MENUS")


def test_menu_text_lists_ids_and_params():
    txt = render_menus_text(_bundle_with_menus())
    assert "D1:" in txt and "X1" in txt and "F1" in txt and "R1" in txt
    assert "price_beyond(" in txt and "n_closes_beyond(" in txt
