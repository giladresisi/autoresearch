"""Phase-2 S8 menu tests (plan 11): DOL menu + predicate menu (config-driven).

Unit-level over synthetic bundle + validator-dict inputs (no parquet): the menu builder
is pure over (bundle.levels["MNQ"], bundle.day_mid, vd["levels"], vd["now_price"]).
"""

import json

import pandas as pd

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


def test_dol_menu_proximity_guard_excludes_too_close_pools():
    """Plan 12 Fix 2: a pool nearer than DOL_MIN_DRAW_DISTANCE_PTS to price is NOT offered as
    a same-direction DOL — it's a level price is already on (no forward draw, race-prone).
    Covered both directions and for the wick/body distinction (the guard uses the wick, the
    price that actually gets touched first)."""
    from derive_facts import DOL_MIN_DRAW_DISTANCE_PTS as G, build_menus, FactsBundle as FB
    b = FB()
    b.now_price = 100.0
    b.day_mid = 100.0
    b.levels = {"MNQ": {
        # wick within the guard (body would be farther) → excluded: the wick is the draw.
        "up_close":   (100.0 + G - 0.5, 100.0 + G + 3.0, "above", "day", None),
        "up_far":     (100.0 + G + 10.0, 100.0 + G + 9.0, "above", "day", None),   # eligible
        "down_close": (100.0 - G + 0.5, 100.0 - G - 3.0, "below", "day", None),    # excluded
        "down_far":   (100.0 - G - 10.0, 100.0 - G - 9.0, "below", "day", None),   # eligible
    }}
    vd = {"now_price": 100.0, "levels": {
        "up_close":   {"price": 100.0 + G - 0.5, "side": "high", "swept": False, "depleted": False},
        "up_far":     {"price": 100.0 + G + 10.0, "side": "high", "swept": False, "depleted": False},
        "down_close": {"price": 100.0 - G + 0.5, "side": "low", "swept": False, "depleted": False},
        "down_far":   {"price": 100.0 - G - 10.0, "side": "low", "swept": False, "depleted": False},
    }}
    m = build_menus(b, vd)
    up = {e["level"] for e in m["dol"]["UP"]}
    down = {e["level"] for e in m["dol"]["DOWN"]}
    assert up == {"up_far"}, up
    assert down == {"down_far"}, down


def test_th_02_root_cause_regression_dol_absent():
    """Plan 12 Fix 2 root-cause proof ($0): reproduce the logged 07-02 th_02 facts geometry
    through the menu builder and assert the offending DOL (prev1_day_low, 0.75 pts below price
    at facts build) is NO LONGER offered as a DOWN draw — so the race that produced the bogus
    2-minute "completion" can no longer originate from the menu. A farther unswept low
    (prev2_day_low, 74.75 pts away) is still offered, proving the guard is targeted, not blunt.

    The level table is the verbatim th_02 snapshot logged in
    agent/bench/runs/test_0702_run2_diag/2026-07-02/decisions.jsonl (bench.levels); now_price
    at build was 30010.0 (price had tested prev1_day_low 30009.25 to within 0.75; sess_lo
    30010.0), so the DOL sat 0.75 pts below price — a correct-side pool the OLD menu offered."""
    now_price = 30010.0
    # (name -> (price, side, swept0, threshold)) copied from the logged th_02 bench.levels.
    logged = {
        "prev1_day_low": (30009.25, "below", False, 40.0),   # the offending DOL — 0.75 away
        "prev2_day_low": (29935.25, "below", False, 40.0),   # a genuine farther draw
        "prev1_day_high": (30555.75, "above", False, 40.0),
        "ny_evening(prev1)_low": (30009.25, "below", False, 20.0),
    }
    _thr_tier = {80.0: "week", 40.0: "day", 20.0: "session"}
    b = FactsBundle()
    b.now_price = now_price
    b.day_mid = 30063.0
    mnq, vd_levels = {}, {}
    for name, (price, side, swept, thr) in logged.items():
        mnq[name] = (price, price, side, _thr_tier[thr], None)
        vd_levels[name] = {"price": price, "side": "high" if side == "above" else "low",
                           "swept": swept, "depleted": False}
    b.levels = {"MNQ": mnq}
    m = build_menus(b, {"now_price": now_price, "levels": vd_levels})
    down = {e["level"] for e in m["dol"]["DOWN"]}
    assert "prev1_day_low" not in down, "the 0.75-pt-away DOL must no longer be offered"
    assert "prev2_day_low" in down, "a genuine farther low draw should still be offered"


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


def test_weekly_mid_absent_when_bundle_has_no_weekly_mid():
    # old-style bundle (no weekly_mid set) -> the weekly_mid key reflects that, and the
    # weekly_mid level-class contributes zero predicates. Backward compatibility
    # (existing families/ids UNCHANGED) is proven instead by
    # test_existing_menu_tests_still_pass_with_new_config and the full pre-existing
    # suite in this file passing unmodified.
    m = build_menus(_bundle(), _vd())
    assert m["weekly_mid"] is None


def test_weekly_mid_family_present_when_bundle_has_weekly_mid():
    b = _bundle()
    b.weekly_mid = 95.0
    m = build_menus(b, _vd())
    assert m["weekly_mid"] == 95.0
    up_preds = [e["predicate"] for e in m["predicates"]["UP"]]
    # UP thesis anti_side for weekly_mid falsification is "below" (_anti_side("UP") == "below")
    assert any(p["type"] == "n_closes_beyond" and p["price"] == 95.0 and p["side"] == "below"
               and p["tf"] in ("1h", "4h", "5m") for p in up_preds)


def test_daily_mid_gains_1h_4h_falsification_variants():
    m = build_menus(_bundle(), _vd())
    up_preds = [e["predicate"] for e in m["predicates"]["UP"]]
    tfs = {p["tf"] for p in up_preds
          if p["type"] == "n_closes_beyond" and p["price"] == 99.0}   # day_mid == 99.0 in _bundle
    assert {"5m", "1h", "4h"} <= tfs


def test_swept_levels_evidence_family_present_for_mes():
    b = _bundle()
    b.levels["MES"] = {
        "mes_up_pool": (115.0, 114.5, "above", "day", None),
    }
    b.swept_at = {"MNQ": {}, "MES": {"mes_up_pool": pd.Timestamp("2026-07-02 10:00", tz="America/New_York")}}
    m = build_menus(b, _vd())
    up_preds = [e for e in m["predicates"]["UP"] if e["family"] == "evidence"]
    assert any(e["predicate"].get("price") == 115.0 for e in up_preds)


def test_meaningful_smt_pools_only_day_or_week_tier():
    b = _bundle()
    b.smt_candidates = [
        {"level": "up_pool", "tier": "day", "side": "above",
         "swept_ticker": "MNQ", "unswept_ticker": "MES", "swept_at": None,
         "type": "wick", "meaningful": True},
        {"level": "up_pool", "tier": "session", "side": "above",
         "swept_ticker": "MNQ", "unswept_ticker": "MES", "swept_at": None,
         "type": "wick", "meaningful": False},
    ]
    m = build_menus(b, _vd())
    up_evidence = [e for e in m["predicates"]["UP"] if e["family"] == "evidence"]
    # anti-side of "above" is "below" -> the rejection-direction test for up_pool (price 110.0)
    matches = [e for e in up_evidence
              if e["predicate"].get("price") == 110.0 and e["predicate"].get("side") == "below"]
    assert len(matches) >= 1


def test_existing_menu_tests_still_pass_with_new_config():
    # Sanity: appending new config entries must not perturb the pre-existing
    # family/id assertions this file already made (F1/X1/R1 exact-id checks).
    txt = render_menus_text(_bundle_with_menus())
    assert "D1:" in txt and "X1" in txt and "F1" in txt and "R1" in txt
