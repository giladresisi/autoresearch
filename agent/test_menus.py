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


def test_dol_menu_excludes_nested_prev_level_2026_07_13_case():
    """thesis.md §2.1b names DOL as one of the things prev-level nesting must exclude, but
    `_dol_menu` never received the suppression set until now. Verbatim MNQ day-low family from
    the 2026-07-13 18:00 ET case (thesis.md §10): `prev7_day_low` (29329.0) is nested under
    `prev3_day_low` (28910.25, deeper/farther — no more-recent level reaches as low) yet it was
    offered as the #1 DOWN draw and picked as the thesis's DOL before this fix."""
    now_price = 29433.75
    logged = {
        "prev1_day_low": (29677.5, True),
        "prev2_day_low": (29395.0, True),
        "prev3_day_low": (28910.25, False),
        "prev4_day_low": (29209.75, False),
        "prev5_day_low": (29683.25, True),
        "prev6_day_low": (29522.5, True),
        "prev7_day_low": (29329.0, False),
    }
    b = FactsBundle()
    b.now_price = now_price
    b.day_mid = 29714.5
    mnq, vd_levels = {}, {}
    for name, (price, swept) in logged.items():
        mnq[name] = (price, price, "below", "day", None)
        vd_levels[name] = {"price": price, "side": "low", "swept": swept, "depleted": swept}
    b.levels = {"MNQ": mnq}
    b.suppressed_p1_levels = {
        "MNQ": {"prev4_day_low", "prev5_day_low", "prev6_day_low", "prev7_day_low"}}
    m = build_menus(b, {"now_price": now_price, "levels": vd_levels})
    down = {e["level"] for e in m["dol"]["DOWN"]}
    assert "prev7_day_low" not in down, "nested level must not be offered as a DOL draw"
    assert down == {"prev3_day_low"}, down


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


def test_falsifier_ranking_recommended_default():
    # 2026-08-17 (07-15 sampling-stability): exactly one falsification entry per
    # direction carries recommended=True — the FIRST one, so the tight 2x5m daily-mid
    # close form when available, else the nearest anti-pool via the degenerate-mid
    # fallback in _resolve_level_class.
    m = build_menus(_bundle(), _vd())
    for direction in ("UP", "DOWN"):
        falsifiers = [e for e in m["predicates"][direction] if e["family"] == "falsification"]
        rec = [e for e in m["predicates"][direction] if e.get("recommended")]
        assert rec == [falsifiers[0]]
    # UP (now 100 above mid 99): mid form is forward-looking -> it is F1.
    up_rec = next(e for e in m["predicates"]["UP"] if e.get("recommended"))
    assert up_rec["predicate"]["type"] == "n_closes_beyond"
    assert up_rec["predicate"]["price"] == 99.0
    assert up_rec["predicate"]["tf"] == "5m" and up_rec["predicate"]["n"] == 2
    # DOWN: anti side is "above" and price already sits beyond the mid -> degenerate
    # guard drops the mid form; the recommendation falls to the nearest anti-pool.
    dn_rec = next(e for e in m["predicates"]["DOWN"] if e.get("recommended"))
    assert dn_rec["predicate"]["type"] == "price_beyond"


def test_menu_text_tags_recommended_falsifier():
    txt = render_menus_text(_bundle_with_menus())
    assert txt.count("<-- RECOMMENDED default falsifier") == 2   # once per direction
    assert "RANKED" in txt


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


# --------------------------------------------------------------------------- #
# DOL-menu refit (2026-08-16): ATR floor + band tags + stretch-gated projection #
# --------------------------------------------------------------------------- #

def _refit_bundle(ar=20.0, day_hi=104.0, day_lo=60.0, levels=None):
    b = FactsBundle()
    b.now_price = 100.0
    b.day_mid = 99.0
    b.avg_range_1h = {"MNQ": ar}
    b.day_hi = {"MNQ": day_hi}
    b.day_lo = {"MNQ": day_lo}
    b.levels = {"MNQ": levels or {}}
    return b


def _refit_vd(levels):
    return {"now_price": 100.0, "levels": {
        n: {"price": p, "side": "high" if s == "above" else "low",
            "swept": False, "depleted": False}
        for n, (p, _b, s, _t, _a) in levels.items()}}


def test_dol_floor_scales_with_avg_range():
    # Plan 18 floor raise (0.5 -> 1.0): ar=20 -> floor = max(5, 1.0*20) = 20.
    # (a) a 0.9x-away pool (18 pts) is excluded at the new floor; (b) a 1.1x-away pool
    # (22 pts) is kept. The old 0.5x floor would have kept both.
    levels = {"up_09x": (118.0, 117.5, "above", "day", None),
              "up_11x": (122.0, 121.5, "above", "day", None)}
    b = _refit_bundle(levels=levels)
    m = build_menus(b, _refit_vd(levels))
    up = {e["level"] for e in m["dol"]["UP"] if e["tier"] != "projection"}
    assert up == {"up_11x"}


def test_dol_floor_falls_back_to_flat_guard_without_avg_range():
    # Plan 18 case (c): the flat 5-pt fallback when avg_range is absent is UNCHANGED
    # by the 0.5 -> 1.0 floor raise (the ratio never applies without an ATR).
    levels = {"up_near": (107.0, 106.5, "above", "day", None)}
    b = _refit_bundle(levels=levels)
    b.avg_range_1h = {}                       # no ATR -> flat 5-pt guard, 7-pt pool stays
    m = build_menus(b, _refit_vd(levels))
    assert {e["level"] for e in m["dol"]["UP"]} == {"up_near"}


def test_dol_band_tags_and_ratio():
    # ar=20: 30pts -> 1.5x BAND; 90pts -> 4.5x FAR. (Pool distances sit above the
    # plan-18 floor of 1.0x so both survive the eligibility gate.)
    levels = {"up_band": (130.0, 129.5, "above", "day", None),
              "up_far":  (190.0, 189.0, "above", "week", None)}
    b = _refit_bundle(levels=levels)
    m = build_menus(b, _refit_vd(levels))
    by_name = {e["level"]: e for e in m["dol"]["UP"]}
    assert by_name["up_band"]["band"] == "BAND" and by_name["up_band"]["dist_ratio"] == 1.5
    assert by_name["up_far"]["band"] == "FAR" and by_name["up_far"]["dist_ratio"] == 4.5


def test_dol_projection_offered_when_band_empty():
    # UP has only a FAR pool -> projection_up at day_hi + 1.0*ar (104 + 20 = 124),
    # tick-snapped, tagged PROJECTION. DOWN has a BAND pool (30pts = 1.5x, above the
    # plan-18 floor) -> no projection_down.
    levels = {"up_far":    (190.0, 189.0, "above", "week", None),
              "down_band": (70.0, 70.5, "below", "day", None)}
    b = _refit_bundle(levels=levels)
    m = build_menus(b, _refit_vd(levels))
    up_proj = [e for e in m["dol"]["UP"] if e["tier"] == "projection"]
    assert len(up_proj) == 1
    assert up_proj[0]["level"] == "projection_up" and up_proj[0]["price"] == 124.0
    assert up_proj[0]["band"] == "PROJECTION" and up_proj[0]["body"] is None
    assert not [e for e in m["dol"]["DOWN"] if e["tier"] == "projection"]


def test_dol_floor_raise_frees_band_for_projection():
    # Plan 18 case (d): a direction whose ONLY band pool sat at 0.8x (16 pts at ar=20 —
    # legal under the old 0.5x floor, excluded at 1.0x) now has an empty band, so the
    # stretch-gated projection is offered in its place (gates pass here: day-stretch
    # (100-60)/20 = 2.0x <= 3.0, no weekly mid set).
    levels = {"up_08x": (116.0, 115.5, "above", "day", None)}
    b = _refit_bundle(levels=levels)
    m = build_menus(b, _refit_vd(levels))
    up = m["dol"]["UP"]
    assert not [e for e in up if e["tier"] != "projection"]   # the 0.8x pool is gone
    proj = [e for e in up if e["tier"] == "projection"]
    assert len(proj) == 1 and proj[0]["level"] == "projection_up"
    assert proj[0]["price"] == 124.0                          # day_hi 104 + 1.0*20


def test_dol_projection_stretch_gated():
    # Same geometry as above but price stretched > 3.0x from the opposite-side day
    # extreme (day_lo=20 -> 80pts/20 = 4.0x) -> NO projection anywhere; UP menu keeps
    # only its FAR pool (no-liquidity semantics preserved when that's all there is).
    levels = {"up_far": (190.0, 189.0, "above", "week", None)}
    b = _refit_bundle(day_lo=20.0, levels=levels)
    m = build_menus(b, _refit_vd(levels))
    assert not [e for e in m["dol"]["UP"] if e["tier"] == "projection"]
    assert not [e for e in m["dol"]["DOWN"] if e["tier"] == "projection"]


def test_dol_projection_counts_toward_exhaustion_predicates():
    levels = {"up_far": (190.0, 189.0, "above", "week", None)}
    b = _refit_bundle(levels=levels)
    m = build_menus(b, _refit_vd(levels))
    x_prices = {e["predicate"]["price"] for e in m["predicates"]["UP"]
                if e["family"] == "exhaustion"}
    assert 124.0 in x_prices                   # the projection is a real drawable target


def test_dol_projection_weekly_extension_gate_direction_aware():
    # Price 5x ABOVE the weekly mid (ar=20, mid=0? use mid such that (100-mid)/20 >= 4):
    # projection_up blocked; projection_down (against the extension) still allowed.
    levels = {"up_far":   (190.0, 189.0, "above", "week", None),
              "down_far": (10.0, 10.5, "below", "week", None)}
    b = _refit_bundle(levels=levels)
    b.weekly_mid = 15.0                        # (100-15)/20 = 4.25x above -> UP gated
    m = build_menus(b, _refit_vd(levels))
    assert not [e for e in m["dol"]["UP"] if e["tier"] == "projection"]
    assert [e for e in m["dol"]["DOWN"] if e["tier"] == "projection"]

    b2 = _refit_bundle(levels=levels)
    b2.weekly_mid = 60.0                       # 2.0x above -> both projections allowed
    m2 = build_menus(b2, _refit_vd(levels))
    assert [e for e in m2["dol"]["UP"] if e["tier"] == "projection"]
    assert [e for e in m2["dol"]["DOWN"] if e["tier"] == "projection"]


# --------------------------------------------------------------------------- #
# plan 34: open-price levels (TDO) as eligible DOL draws, both directions      #
# --------------------------------------------------------------------------- #
# An open price carries no `side`: it is neither resistance nor support, it is a
# reference. It is offered on whichever side of price it sits, and only for the names
# in DOL_OPEN_PRICE_LEVELS -- a generic "any side-less level" rule would re-admit
# `sideless` above, whose exclusion is a pinned contract (a level with no side is not a
# pool). Evidence: 2026-09-09, where an UP thesis had no menu entry nearer than
# london(cur)_high @ 29634.00 (never reached, closest approach 40.00) while the midnight
# open at 29561.75 was crossed at 09:59:20.

def _bundle_with_tdo(tdo_price):
    b = _bundle()
    b.levels["MNQ"]["TDO"] = (tdo_price, None, None, "session", None)
    b.levels["MNQ"]["TWO"] = (105.0, None, None, "session", None)
    return b


def _vd_with_tdo(tdo_price):
    vd = _vd()
    vd["levels"]["TDO"] = {"price": tdo_price, "side": "high", "swept": False,
                           "depleted": False}
    vd["levels"]["TWO"] = {"price": 105.0, "side": "high", "swept": False,
                           "depleted": False}
    return vd


def test_dol_menu_offers_tdo_above_price_as_an_up_draw():
    m = build_menus(_bundle_with_tdo(115.0), _vd_with_tdo(115.0))
    assert "TDO" in {e["level"] for e in m["dol"]["UP"]}
    assert "TDO" not in {e["level"] for e in m["dol"]["DOWN"]}


def test_dol_menu_offers_tdo_below_price_as_a_down_draw():
    m = build_menus(_bundle_with_tdo(85.0), _vd_with_tdo(85.0))
    assert "TDO" in {e["level"] for e in m["dol"]["DOWN"]}
    assert "TDO" not in {e["level"] for e in m["dol"]["UP"]}


def test_dol_menu_applies_the_draw_floor_to_tdo_like_any_other_draw():
    """The open price is not exempt from the proximity guard: 2.0 pts from a 100.0 price
    is inside DOL_MIN_DRAW_DISTANCE_PTS and is a race, not a draw."""
    m = build_menus(_bundle_with_tdo(102.0), _vd_with_tdo(102.0))
    assert "TDO" not in {e["level"] for e in m["dol"]["UP"]}
    assert "TDO" not in {e["level"] for e in m["dol"]["DOWN"]}


def test_dol_menu_does_not_offer_two_scope_limit():
    """TWO is the same shape as TDO and is deliberately NOT admitted: it was not asked
    for and carries no evidence. Widening is a one-tuple change to
    DOL_OPEN_PRICE_LEVELS."""
    m = build_menus(_bundle_with_tdo(115.0), _vd_with_tdo(115.0))
    assert "TWO" not in {e["level"] for e in m["dol"]["UP"]}
    assert "TWO" not in {e["level"] for e in m["dol"]["DOWN"]}


def test_dol_menu_still_excludes_a_generic_sideless_level():
    """The pinned contract from test_dol_menu_only_eligible_pools, restated against the
    open-price change so it cannot be widened by accident."""
    m = build_menus(_bundle_with_tdo(115.0), _vd_with_tdo(115.0))
    assert "sideless" not in {e["level"] for e in m["dol"]["UP"]}
    assert "sideless" not in {e["level"] for e in m["dol"]["DOWN"]}


# --------------------------------------------------------------------------- #
# Plan 40: unnested HTF extremes as `extra_pools` (T2 only)                     #
# --------------------------------------------------------------------------- #

def _htf(name, price, side="above", tier="htf_week"):
    return (name, price, None, tier, side)


def test_build_menus_without_extra_pools_is_byte_identical():
    levels = {"up_far": (190.0, 189.0, "above", "week", None),
              "down_band": (70.0, 70.5, "below", "day", None)}
    for extra in (None, []):
        a = build_menus(_refit_bundle(levels=levels), _refit_vd(levels))
        b = build_menus(_refit_bundle(levels=levels), _refit_vd(levels),
                        extra_pools=extra)
        assert json.dumps(a, sort_keys=True, default=str) == \
            json.dumps(b, sort_keys=True, default=str)
    b1, b2 = _bundle_with_menus(), _bundle_with_menus()
    assert render_menus_text(b1) == render_menus_text(b2)


def test_extra_pool_joins_menu_nearest_first_with_band_tag():
    levels = {"up_far": (190.0, 189.0, "above", "week", None)}
    m = build_menus(_refit_bundle(levels=levels), _refit_vd(levels),
                    extra_pools=[_htf("htf_week_high_x", 170.0),
                                 _htf("htf_month_high_y", 130.0, tier="htf_month"),
                                 _htf("htf_week_low_z", 40.0, side="below")])
    up = m["dol"]["UP"]
    assert [e["level"] for e in up] == ["htf_month_high_y", "htf_week_high_x", "up_far"]
    assert up[0]["id"] == "D1" and up[0]["band"] == "BAND" and up[0]["dist_ratio"] == 1.5
    assert up[1]["band"] == "FAR" and up[1]["tier"] == "htf_week"
    assert [e["level"] for e in m["dol"]["DOWN"]][0] == "htf_week_low_z"


def test_extra_pool_inside_draw_floor_excluded():
    m = build_menus(_refit_bundle(), _refit_vd({}),
                    extra_pools=[_htf("htf_week_high_near", 115.0)])   # 15 < floor 20
    assert "htf_week_high_near" not in {e["level"] for e in m["dol"]["UP"]}


def test_extra_pool_equal_to_named_level_is_deduped():
    levels = {"prev1_week_high": (150.0, 149.0, "above", "week", None)}
    m = build_menus(_refit_bundle(levels=levels), _refit_vd(levels),
                    extra_pools=[_htf("htf_week_high_dup", 150.0),
                                 _htf("htf_week_low_other_side", 150.0, side="below")])
    up = [e["level"] for e in m["dol"]["UP"]]
    assert "prev1_week_high" in up and "htf_week_high_dup" not in up
    # a swept named level at that price keeps the price OUT, the HTF row cannot revive it
    vd = _refit_vd(levels)
    vd["levels"]["prev1_week_high"]["swept"] = True
    m2 = build_menus(_refit_bundle(levels=levels), vd,
                     extra_pools=[_htf("htf_week_high_dup", 150.0)])
    assert 150.0 not in {e["price"] for e in m2["dol"]["UP"]}


def test_projection_suppressed_by_htf_pool():
    # Q1 = P1: a FAR htf row (9x) suppresses the projection a FAR named pool would not.
    m = build_menus(_refit_bundle(), _refit_vd({}),
                    extra_pools=[_htf("htf_month_high_far", 280.0, tier="htf_month")])
    up = m["dol"]["UP"]
    assert [e["level"] for e in up] == ["htf_month_high_far"]
    assert up[0]["band"] == "FAR"


def test_htf_row_inside_the_draw_floor_does_not_suppress_the_projection():
    m = build_menus(_refit_bundle(), _refit_vd({}),
                    extra_pools=[_htf("htf_week_running_high", 102.0,
                                      tier="htf_week_running")])
    assert [e["level"] for e in m["dol"]["UP"]] == ["projection_up"]


def test_projection_kept_when_suppression_off(monkeypatch):
    import derive_facts as df_mod
    monkeypatch.setattr(df_mod, "DOL_HTF_SUPPRESSES_PROJECTION", False)
    m = build_menus(_refit_bundle(), _refit_vd({}),
                    extra_pools=[_htf("htf_month_high_far", 280.0, tier="htf_month")])
    assert [e["level"] for e in m["dol"]["UP"]] == ["projection_up", "htf_month_high_far"]
