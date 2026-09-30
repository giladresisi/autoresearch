"""Plan 44: per-timeframe level status ("V1d") in the L1 evidence ledger.

One standing 4h read per suppressed (asset, side) stack, behind ACT_PER_TF_LEVEL_STATUS
(default OFF). Numbered comments refer to plan 44 §6.
"""
import copy
import json
import os
import pickle
import random
import subprocess
import sys

import pandas as pd
import pytest

from agent import run_agent  # noqa: F401  (puts agent/ and agent/contracts on sys.path)
from agent.bench import facts as bench_facts
from agent.facts.assemble import DECIDE_THESIS_KEYS, _empty_contract
from agent.trader import tiebreak

import derive_facts
from validate_contracts import per_tf_level_status, score_thesis_evidence, validate_thesis

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                        "l1_kw_20260928.json")
TZ = "America/New_York"


# --------------------------------------------------------------------------- helpers
def _fixture_kw() -> dict:
    """The 2026-09-28 09:20 score kwargs, with `magnitude` rebuilt to tuple keys."""
    with open(_FIXTURE, encoding="utf-8") as fh:
        kw = json.load(fh)["kw"]
    kw["magnitude"] = {(a, lvl, tf): r for a, lv in kw["magnitude"].items()
                       for lvl, tfs in lv.items() for tf, r in tfs.items()}
    return kw


def _map_of(kw, **extra) -> dict:
    return per_tf_level_status(kw.get("suppressed_p1_levels"), kw.get("p1_stale_levels"),
                               kw.get("suppressed_p2_sites"), kw.get("level_htf_close_status"),
                               kw.get("level_tiers"), kw.get("smt_candidates"), **extra)


def _synthetic_kw(lows=None, highs=None, suppressed=None, stale=None, p2_sites=None,
                  candidates=None, reversal=None) -> dict:
    """MNQ: an un-suppressed prev1_day_high (1h accept, 4h immature) plus a stack of lows,
    each {"1h": reject, "4h": accept} unless overridden. MES empty."""
    lows = lows if lows is not None else {"prev1_day_low": 100.0, "prev2_day_low": 90.0,
                                          "prev3_day_low": 80.0}
    highs = highs if highs is not None else {"prev1_day_high": 300.0}
    tiers = {"MNQ": {}, "MES": {}}
    status = {"MNQ": {}, "MES": {}}
    for name, px in highs.items():
        tiers["MNQ"][name] = {"tier": "day", "price": px}
        status["MNQ"][name] = {"1h": True, "4h": None}
    for name, px in lows.items():
        tiers["MNQ"][name] = {"tier": "day", "price": px}
        status["MNQ"][name] = {"1h": False, "4h": True}
    return {
        "suppressed_p1_levels": {"MNQ": list(suppressed if suppressed is not None else lows),
                                 "MES": []},
        "p1_stale_levels": {"MNQ": list(stale or ()), "MES": []},
        "suppressed_p2_sites": {"MNQ": list(p2_sites or ()), "MES": []},
        "level_htf_close_status": status,
        "level_tiers": tiers,
        "smt_candidates": list(candidates or ()),
        "htf_reversal": reversal if reversal is not None else {"MNQ": {}, "MES": {}},
    }


def _cand(level, tier="day", swept="MNQ", meaningful=True):
    return {"level": level, "tier": tier, "swept_ticker": swept,
            "unswept_ticker": "MES" if swept == "MNQ" else "MNQ", "meaningful": meaningful}


def _items(res, asset, level):
    return [it for it in res["scored_evidence"]
            if it.get("asset") == asset and it.get("level") == level and it["points"]]


def _v1d_kwarg_emulation(kw, m) -> dict:
    """§1 applied by EDITING the kwargs (the study's `_v1` semantics, reimplemented here):
    lift each restored level out of the three sets and blank its 1h status."""
    kw = copy.deepcopy(kw)
    for a, lv in m.items():
        for n in lv:
            for k in ("suppressed_p1_levels", "p1_stale_levels", "suppressed_p2_sites"):
                d = kw.get(k)
                if d is not None and a in d:
                    d[a] = [x for x in (d[a] or ()) if x != n]
            if n in (kw["level_htf_close_status"].get(a) or {}):
                kw["level_htf_close_status"][a][n]["1h"] = None
    return kw


def _multiset(res):
    return sorted(json.dumps({k: v for k, v in it.items()}, sort_keys=True, default=str)
                  for it in res["scored_evidence"])


# --------------------------------------------------------------------------- 6.1 pure
def test_restores_most_extreme_suppressed_low_on_4h_only():                          # 1
    m = _map_of(_synthetic_kw())
    assert set(m["MNQ"]) == {"prev3_day_low"}
    st = m["MNQ"]["prev3_day_low"]
    assert (st["1h"], st["4h"], st["side"]) == ("retired", "live", "low")


def test_restores_most_extreme_suppressed_high():                                    # 2
    kw = _synthetic_kw(lows={}, highs={"prev1_day_high": 300.0, "prev2_day_high": 320.0,
                                       "prev3_day_high": 310.0},
                       suppressed=["prev1_day_high", "prev2_day_high", "prev3_day_high"])
    for n in ("prev1_day_high", "prev2_day_high", "prev3_day_high"):
        kw["level_htf_close_status"]["MNQ"][n] = {"1h": True, "4h": False}
    m = _map_of(kw)
    assert set(m["MNQ"]) == {"prev2_day_high"}
    assert m["MNQ"]["prev2_day_high"]["side"] == "high"


def test_one_per_asset_and_side():                                                   # 3
    kw = _synthetic_kw(highs={"prev1_day_high": 300.0, "prev2_day_high": 320.0},
                       suppressed=["prev1_day_low", "prev2_day_low", "prev3_day_low",
                                   "prev1_day_high", "prev2_day_high"])
    kw["level_htf_close_status"]["MNQ"]["prev1_day_high"]["4h"] = True
    kw["level_htf_close_status"]["MNQ"]["prev2_day_high"]["4h"] = True
    kw["level_tiers"]["MES"] = copy.deepcopy(kw["level_tiers"]["MNQ"])
    kw["level_htf_close_status"]["MES"] = copy.deepcopy(kw["level_htf_close_status"]["MNQ"])
    kw["suppressed_p1_levels"]["MES"] = list(kw["suppressed_p1_levels"]["MNQ"])
    m = _map_of(kw)
    assert sum(len(v) for v in m.values()) == 4
    for a in ("MNQ", "MES"):
        assert sorted(v["side"] for v in m[a].values()) == ["high", "low"]
        assert set(m[a]) == {"prev3_day_low", "prev2_day_high"}


def test_level_without_mature_4h_is_not_restored():                                 # 4
    kw = _synthetic_kw()
    kw["level_htf_close_status"]["MNQ"]["prev3_day_low"] = {"1h": False, "4h": None}
    m = _map_of(kw)
    assert "prev3_day_low" not in m["MNQ"]
    assert set(m["MNQ"]) == {"prev2_day_low"}


def test_live_p2_site_is_left_alone():                                               # 5
    # prev3_day_low is the most extreme, but it is a live (meaningful, not P2-suppressed)
    # SMT site: it already scores, so the next level down is restored instead.
    kw = _synthetic_kw(candidates=[_cand("prev3_day_low")])
    m = _map_of(kw)
    assert set(m["MNQ"]) == {"prev2_day_low"}
    # the same candidate once P2-suppressed is no longer live -> it is restorable again
    kw = _synthetic_kw(candidates=[_cand("prev3_day_low")], p2_sites=["prev3_day_low"])
    assert set(_map_of(kw)["MNQ"]) == {"prev3_day_low"}


def test_tie_prefers_higher_tier_then_level_order():                                 # 6
    kw = _synthetic_kw(lows={}, highs={"prev6_day_high": 500.0, "prev2_week_high": 500.0},
                       suppressed=["prev6_day_high", "prev2_week_high"])
    kw["level_tiers"]["MNQ"]["prev2_week_high"]["tier"] = "week"
    for n in ("prev6_day_high", "prev2_week_high"):
        kw["level_htf_close_status"]["MNQ"][n] = {"1h": True, "4h": True}
    assert set(_map_of(kw)["MNQ"]) == {"prev2_week_high"}
    # equal tier: the first name in level_tiers insertion order wins, whichever way round
    kw["level_tiers"]["MNQ"]["prev2_week_high"]["tier"] = "day"
    assert set(_map_of(kw)["MNQ"]) == {"prev6_day_high"}
    kw["level_tiers"]["MNQ"] = dict(reversed(list(kw["level_tiers"]["MNQ"].items())))
    assert set(_map_of(kw)["MNQ"]) == {"prev2_week_high"}


_TIE_SCRIPT = r"""
import json, sys
sys.path.insert(0, r"{repo}")
from agent import run_agent  # noqa
from validate_contracts import per_tf_level_status
kw = json.loads(sys.stdin.read())
print(json.dumps(per_tf_level_status(kw["suppressed_p1_levels"], kw["p1_stale_levels"],
      kw["suppressed_p2_sites"], kw["level_htf_close_status"], kw["level_tiers"],
      kw["smt_candidates"]), sort_keys=True))
"""


def test_selection_independent_of_input_order():                                     # 7
    kw = _synthetic_kw(lows={"prev1_day_low": 80.0, "prev2_day_low": 80.0, "asia(cur)_low": 80.0},
                       highs={"prev6_day_high": 500.0, "prev2_week_high": 500.0},
                       suppressed=["prev1_day_low", "prev2_day_low", "asia(cur)_low",
                                   "prev6_day_high", "prev2_week_high"],
                       stale=["prev2_day_low"])
    for n in ("prev6_day_high", "prev2_week_high"):
        kw["level_htf_close_status"]["MNQ"][n] = {"1h": True, "4h": True}
    base = _map_of(kw)
    rng = random.Random(7)
    for _ in range(10):
        k2 = copy.deepcopy(kw)
        for key in ("suppressed_p1_levels", "p1_stale_levels"):
            rng.shuffle(k2[key]["MNQ"])
        k2["suppressed_p1_levels"]["MNQ"] = set(k2["suppressed_p1_levels"]["MNQ"])
        assert _map_of(k2) == base
    outs = set()
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run([sys.executable, "-c", _TIE_SCRIPT.format(repo=_REPO)],
                           input=json.dumps(kw), capture_output=True, text=True,
                           cwd=_REPO, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip().splitlines()[-1])
    assert len(outs) == 1
    assert json.loads(outs.pop()) == json.loads(json.dumps(base))


def test_unsuppressed_and_suffixless_levels_never_listed():                          # 8
    kw = _synthetic_kw(suppressed=["prev2_day_low", "TDO", "daily_mid", "weekly_mid_low",
                                   "fvg_MNQ_1h_bull_x"])
    for n in ("TDO", "daily_mid", "weekly_mid_low", "fvg_MNQ_1h_bull_x"):
        kw["level_tiers"]["MNQ"][n] = {"tier": "day", "price": 1.0}
        kw["level_htf_close_status"]["MNQ"][n] = {"1h": True, "4h": True}
    m = _map_of(kw)
    # prev3_day_low / prev1_day_low are NOT suppressed here: only prev2_day_low can be restored
    assert set(m["MNQ"]) == {"prev2_day_low"}


def test_none_and_empty_inputs_return_empty_map():                                   # 9
    assert per_tf_level_status(None, None, None, None, None, None) == {}
    assert per_tf_level_status({}, {}, {}, {}, {}, []) == {}
    kw = _synthetic_kw()
    kw["level_tiers"].pop("MNQ")
    kw["level_htf_close_status"].pop("MNQ")
    assert _map_of(kw) == {}


def test_reasons_are_audit_only():                                                   # 10
    kw = _synthetic_kw(stale=["prev3_day_low"])
    a = _map_of(kw)
    b = _map_of(kw, p1_reasons={"MNQ": {"prev3_day_low": ["shadowed", "nested"]}})
    assert a["MNQ"]["prev3_day_low"]["was"] == ["suppressed_p1", "stale"]
    assert b["MNQ"]["prev3_day_low"]["was"] == ["nested", "shadowed", "stale"]
    strip = lambda m: {x: {n: {k: v for k, v in s.items() if k != "was"} for n, s in lv.items()}
                       for x, lv in m.items()}
    assert strip(a) == strip(b)
    ra = score_thesis_evidence([], **kw, level_tf_status=a)
    rb = score_thesis_evidence([], **kw, level_tf_status=b)
    assert ra["net_score"] == rb["net_score"]


# --------------------------------------------------------------------------- 6.2 scorer
def _ledgers():
    fx = _fixture_kw()
    return [("fixture", fx),
            ("lows", _synthetic_kw()),
            ("p2", _synthetic_kw(candidates=[_cand("prev3_day_low")], p2_sites=["prev3_day_low"])),
            ("reverse", _synthetic_kw(reversal={"MNQ": {"prev3_day_low": {"4h": "reverse"}},
                                                "MES": {}}))]


@pytest.mark.parametrize("name,kw", _ledgers(), ids=[n for n, _ in _ledgers()])
def test_none_and_empty_map_equal_legacy(name, kw):                                  # 11
    legacy = score_thesis_evidence([], **copy.deepcopy(kw))
    assert score_thesis_evidence([], **copy.deepcopy(kw), level_tf_status=None) == legacy
    assert score_thesis_evidence([], **copy.deepcopy(kw), level_tf_status={}) == legacy


def test_restored_level_scores_4h_only_when_1h_disagrees():                          # 12
    kw = _synthetic_kw()
    assert _items(score_thesis_evidence([], **kw), "MNQ", "prev3_day_low") == []
    res = score_thesis_evidence([], **kw, level_tf_status=_map_of(kw))
    items = _items(res, "MNQ", "prev3_day_low")
    assert [(i["criterion"], i["tf"], i["direction"], i["side"], i["points"]) for i in items] \
        == [("P1", "4h", "accept", "DOWN", 2.25)]
    assert res["net_score"] == pytest.approx(1.5 - 2.25)


def test_restored_p2_site_scores_p2_on_4h_reject():                                  # 13
    kw = _synthetic_kw(candidates=[_cand("prev3_day_low")], p2_sites=["prev3_day_low"])
    kw["level_htf_close_status"]["MNQ"]["prev3_day_low"] = {"1h": False, "4h": False}
    assert _items(score_thesis_evidence([], **kw), "MNQ", "prev3_day_low") == []
    res = score_thesis_evidence([], **kw, level_tf_status=_map_of(kw))
    items = _items(res, "MNQ", "prev3_day_low")
    assert [(i["criterion"], i["tf"], i["side"], i["points"]) for i in items] \
        == [("P2", "4h", "UP", 2.25)]


def test_restored_stale_level_is_not_zeroed():                                       # 14
    kw = _synthetic_kw(suppressed=["prev1_day_low", "prev2_day_low"], stale=["prev3_day_low"])
    assert _items(score_thesis_evidence([], **kw), "MNQ", "prev3_day_low") == []
    m = _map_of(kw)
    assert m["MNQ"]["prev3_day_low"]["was"] == ["stale"]
    items = _items(score_thesis_evidence([], **kw, level_tf_status=m), "MNQ", "prev3_day_low")
    assert [(i["tf"], i["points"]) for i in items] == [("4h", 2.25)]


def _with_rev(tier):
    return _synthetic_kw(reversal={"MNQ": {"prev3_day_low": {"4h": tier}}, "MES": {}})


def test_restored_4h_reverse_still_flips():                                          # 15 (D3)
    kw = _with_rev("reverse")
    items = _items(score_thesis_evidence([], **kw, level_tf_status=_map_of(kw)),
                   "MNQ", "prev3_day_low")
    assert [(i["direction"], i["side"], i["points"]) for i in items] == [("reject", "UP", 2.25)]


def test_restored_4h_omit_scores_nothing():                                          # 15
    kw = _with_rev("omit")
    res = score_thesis_evidence([], **kw, level_tf_status=_map_of(kw))
    assert _items(res, "MNQ", "prev3_day_low") == []


def test_restored_4h_discount_halves():                                              # 15
    kw = _with_rev("discount")
    items = _items(score_thesis_evidence([], **kw, level_tf_status=_map_of(kw)),
                   "MNQ", "prev3_day_low")
    assert [(i["side"], i["points"]) for i in items] == [("DOWN", 1.125)]


def test_scorer_does_not_mutate_inputs():                                            # 16
    kw = _fixture_kw()
    m = _map_of(kw)
    before = copy.deepcopy((kw, m))
    score_thesis_evidence([], **kw, level_tf_status=m)
    assert (kw, m) == before


@pytest.mark.parametrize("name,kw", _ledgers(), ids=[n for n, _ in _ledgers()])
def test_map_path_equals_v1d_kwarg_emulation(name, kw):                              # 17
    m = _map_of(kw)
    assert m, "every parity ledger must restore something"
    got = score_thesis_evidence([], **copy.deepcopy(kw), level_tf_status=m)
    ref = score_thesis_evidence([], **_v1d_kwarg_emulation(kw, m))
    assert got["net_score"] == ref["net_score"]
    assert _multiset(got) == _multiset(ref)


# --------------------------------------------------------------------------- 6.3 09-28
_RESTORED_0928 = {"MNQ": {"prev3_day_low", "prev5_day_high"},
                  "MES": {"prev3_week_high", "asia(cur)_low"}}


def test_20260928_v0_ledger_is_plus_10_50_up():                                      # 18
    res = score_thesis_evidence([], **_fixture_kw())
    assert res["net_score"] == pytest.approx(10.5)
    assert res["expected_bias"] == "UP"


def test_20260928_per_tf_ledger_is_plus_5_53_up():                                   # 19
    kw = _fixture_kw()
    m = _map_of(kw)
    assert {a: set(v) for a, v in m.items()} == _RESTORED_0928
    res = score_thesis_evidence([], **kw, level_tf_status=m)
    assert res["net_score"] == pytest.approx(5.53125, abs=1e-4)
    assert res["expected_bias"] == "UP"
    rows = {(i["criterion"], i["asset"], i["level"], i["tf"]): (i["points"], i["side"])
            for i in res["scored_evidence"] if i["points"]}
    assert rows[("P2", "MNQ", "prev3_day_low", "4h")] == (1.6875, "UP")
    assert rows[("P2", "MNQ", "prev5_day_high", "4h")] == (2.8125, "DOWN")
    assert rows[("P1", "MES", "prev3_week_high", "4h")] == (3.0, "DOWN")
    assert rows[("P1", "MES", "asia(cur)_low", "4h")] == (0.8438, "DOWN")   # 0.84375, 4dp
    rev = {(i["asset"], i["level"]): i.get("htf_reversal") for i in res["scored_evidence"]
           if i["points"]}
    assert rev[("MNQ", "prev3_day_low")] == "reverse"
    assert rev[("MES", "asia(cur)_low")] == "discount"
    # the six V0 rows are unchanged (§0 table)
    assert len(rows) == 10


def test_20260928_prev1_day_low_is_not_restored():                                   # 20
    kw = _fixture_kw()
    m = _map_of(kw)
    assert "prev1_day_low" not in m["MNQ"]
    res = score_thesis_evidence([], **kw, level_tf_status=m)
    assert _items(res, "MNQ", "prev1_day_low") == []


# --------------------------------------------------------------------------- 6.4 call sites
def _band_facts():
    """Flag off: +1.5 UP (prev1_day_high 1h accept). Flag on: the restored prev3_day_low 4h
    accept adds -2.25 -> -0.75 DOWN. Opposite sides of a declared UP."""
    kw = _synthetic_kw()
    return {**kw, "levels": {}, "now": None}, _map_of(kw)


def _thesis(bias):
    return {"bias": bias, "regime": "TREND", "confidence": "LOW",
            "dol": {"level": "prev1_day_high", "price": 300.0},
            "falsified_if": [], "evidence": [], "reasoning": "t"}


def test_validate_thesis_threads_level_tf_status():                                  # 21
    facts, m = _band_facts()
    assert "ARI_THESIS_BIAS" not in validate_thesis(_thesis("UP"), facts).codes()
    on = validate_thesis(_thesis("UP"), {**facts, "level_tf_status": m})
    assert "ARI_THESIS_BIAS" in on.codes()
    assert "ARI_THESIS_BIAS" not in validate_thesis(
        _thesis("DOWN"), {**facts, "level_tf_status": m}).codes()


def test_derive_arithmetic_ledger_fallback_and_tiebreak_agree():                     # 22
    kw = _fixture_kw()
    m = _map_of(kw)
    menus = {"dol": {"UP": [{"level": "x", "price": 1.0}], "DOWN": [{"level": "y", "price": 0.5}]}}
    kw["dol_available"] = {"UP": True, "DOWN": True}
    direct = score_thesis_evidence([], **kw, level_tf_status=m)["net_score"]
    assert direct == pytest.approx(5.53125, abs=1e-4)

    score_kw = {**kw, "level_tf_status": m}
    block, _ = run_agent._derive_thesis_arithmetic({"evidence": [], "confidence": "LOW"},
                                                   **score_kw)
    signed = sum(i["points"] * (1 if i["side"] == "UP" else -1 if i["side"] == "DOWN" else 0)
                 for i in block["evidence"])
    assert signed == pytest.approx(direct, abs=1e-4)

    fb = run_agent._ledger_fallback_thesis([{"evidence": [], "bias": "DOWN"}], menus, score_kw)
    assert fb["fallback_net_score"] == pytest.approx(direct)

    facts = {k: v for k, v in kw.items() if k not in ("magnitude", "dol_available")}
    facts.update(menus=menus, level_tf_status=m)
    assert tiebreak._net_score({"evidence": []}, facts, kw["magnitude"])["net_score"] \
        == pytest.approx(direct)


def test_empty_contract_has_level_tf_status():                                       # 23
    assert "level_tf_status" in DECIDE_THESIS_KEYS
    assert _empty_contract()["level_tf_status"] == {}


# --------------------------------------------------------------------------- 6.5 facts
def _lv(**kw):
    return {name: (px, px, side, tier, None) for name, (px, side, tier) in kw.items()}


def test_p1_suppression_reasons_partition_the_union():                               # 24
    t = pd.Timestamp("2026-09-28 08:00", tz=TZ)
    t_open = pd.Timestamp("2026-09-27 18:00", tz=TZ)
    lv = _lv(prev1_day_low=(100.0, "below", "day"), prev2_day_low=(110.0, "below", "day"),
             prev3_day_low=(90.0, "below", "day"), prev1_week_low=(90.0, "below", "week"),
             **{"asia(cur)_low": (95.0, "below", "session"),
                "london(cur)_low": (94.0, "below", "session"),
                "asia(prev1)_high": (400.0, "above", "session"),
                "london(cur)_high": (401.0, "above", "session")},
             prev1_day_high=(300.0, "above", "day"))
    swept = {"prev1_day_low": t, "prev3_day_low": t, "prev1_week_low": t,
             "asia(cur)_low": t_open, "london(cur)_low": t_open}
    union, reasons = derive_facts._p1_suppression(lv, swept, session_open=t_open)
    assert union == set(reasons)
    helpers = {"nested": derive_facts._nested_prev_levels(lv),
               "nested_session": derive_facts._nested_session_levels(lv),
               "duplicate": derive_facts._duplicate_sweep_losers(lv, swept, session_open=t_open),
               "shadowed": derive_facts._extremity_shadowed_levels(lv, swept)}
    assert union == set().union(*helpers.values())
    for name, rs in reasons.items():
        assert rs == sorted(rs)
        assert set(rs) == {r for r, names in helpers.items() if name in names}
    assert all(helpers.values()), "fixture must exercise every suppressor"


def _info(beyond, close=100.0):
    return {"beyond": beyond, "close": close, "closed_at": "2026-09-28 08:00:00-04:00",
            "n_closed_since": 1}


def _render_bundle():
    b = derive_facts.FactsBundle()
    t = pd.Timestamp("2026-09-28 04:00", tz=TZ)
    b.now = pd.Timestamp("2026-09-28 09:20", tz=TZ)
    b.levels = {"MNQ": {"prev1_day_low": (100.0, 100.0, "below", "day", None),
                        "prev3_day_low": (80.0, 80.0, "below", "day", None)}, "MES": {}}
    b.swept_at = {"MNQ": {"prev1_day_low": t, "prev3_day_low": t}, "MES": {}}
    b.htf_close_status = {"MNQ": {"prev1_day_low": {"1h": _info(False), "4h": _info(True)},
                                  "prev3_day_low": {"1h": _info(False), "4h": _info(True)}},
                          "MES": {}}
    b.suppressed_p1_levels = {"MNQ": {"prev1_day_low", "prev3_day_low"}, "MES": set()}
    b.p1_suppression_reasons = {"MNQ": {"prev1_day_low": ["shadowed"],
                                        "prev3_day_low": ["nested"]}, "MES": {}}
    b.smt_candidates = [
        {"level": name, "tier": "day", "side": "below", "swept_ticker": "MNQ",
         "unswept_ticker": "MES", "swept_at": t, "type": "wick", "meaningful": True,
         "suggested_exhausted": False, "p2_suppressed": True}
        for name in ("prev1_day_low", "prev3_day_low")]
    return b


def test_render_unchanged_when_level_tf_status_is_none():                            # 25
    b = _render_bundle()
    plain = derive_facts.render_evidence_text(b)
    assert derive_facts.render_evidence_text(b, level_tf_status=None) == plain
    assert "PER-TF" not in plain
    assert plain.count("P2-SUPPRESSED (level is nested)") == 2


def test_render_mislabel_names_the_real_reason():                                    # 26
    b = _render_bundle()
    off = derive_facts.render_evidence_text(b)
    on = derive_facts.render_evidence_text(b, level_tf_status={})
    assert "prev1_day_low [below, day]" in off
    line_off = next(l for l in off.splitlines() if l.strip().startswith("prev1_day_low ["))
    line_on = next(l for l in on.splitlines() if l.strip().startswith("prev1_day_low ["))
    assert "P2-SUPPRESSED (level is nested)" in line_off
    assert "P2-SUPPRESSED (level is shadowed by a more extreme swept same-side level)" in line_on
    line3 = next(l for l in on.splitlines() if l.strip().startswith("prev3_day_low ["))
    assert "P2-SUPPRESSED (level is nested)" in line3


def test_render_restored_level_rows():                                               # 27
    b = _render_bundle()
    m = {"MNQ": {"prev3_day_low": {"1h": "retired", "4h": "live", "side": "low",
                                   "was": ["nested", "p2_site"]}}}
    off = derive_facts.render_evidence_text(b)
    assert "MNQ prev3_day_low [4h]" not in off              # suppressed + P2-suppressed: hidden
    on = derive_facts.render_evidence_text(b, level_tf_status=m)
    row4 = next(l for l in on.splitlines() if l.startswith("  MNQ prev3_day_low [4h]"))
    row1 = next(l for l in on.splitlines() if l.startswith("  MNQ prev3_day_low [1h]"))
    assert "[PER-TF: standing 4h read -- most extreme suppressed low (was: nested, p2_site)" in row4
    assert "[PER-TF: 1h retired" in row1
    assert "nested/duplicate" not in row4 and "nested/duplicate" not in row1
    cand = next(l for l in on.splitlines() if l.strip().startswith("prev3_day_low ["))
    assert "P2-SUPPRESSED" not in cand
    # the candidate line names what the restored 4h ACTUALLY scores (the scorer's dispatch):
    # a raw 4h accept with no partial-bar tag scores P1, not P2
    assert "PER-TF (thesis.md §2.1g): 1h retired; 4h scores as P1 (effective accept" in cand
    assert "MNQ prev1_day_low [4h]" not in on               # un-restored stays hidden

    def cand_line(rev):
        b.htf_reversal = {"MNQ": {"prev3_day_low": {"4h": rev}}, "MES": {}}
        txt = derive_facts.render_evidence_text(b, level_tf_status=m)
        return next(l for l in txt.splitlines() if l.strip().startswith("prev3_day_low ["))
    assert "4h scores as P2 (effective reject)" in cand_line("reverse")
    assert "4h read OMITTED by the partial bar -- scores nothing" in cand_line("omit")
    assert "4h scores as P1 (effective accept" in cand_line("discount")


@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), ("1", True), ("true", True), ("yes", True), ("on", True),
    (" ON ", True), ("garbage", True),
    ("0", False), ("false", False), ("no", False), ("off", False), (" OFF ", False),
    ("False", False)])
def test_flag_parsing(monkeypatch, value, expected):                                  # 28
    """ON by default (adoption, 2026-09-30): unset = ON; only an explicit off-spelling
    disables it -- the ACT_TRADER convention."""
    if value is None:
        monkeypatch.delenv("ACT_PER_TF_LEVEL_STATUS", raising=False)
    else:
        monkeypatch.setenv("ACT_PER_TF_LEVEL_STATUS", value)
    assert bench_facts.per_tf_level_status_enabled() is expected


# --------------------------------------------------------------------------- 6.6 slow
def _global():
    import paths
    return str(paths.global_root())


def _study_dir():
    return os.path.join(_global(), "studies", "l1_ledger_variants")


@pytest.mark.slow
def test_no_lookahead_level_tf_status(monkeypatch):                                  # 29
    from agent.facts.assemble import assemble_facts
    from agent.facts.store import FactStore
    from agent.facts.test_assemble import NOW, _synthetic_bars
    monkeypatch.setenv("ACT_PER_TF_LEVEL_STATUS", "1")
    bars = {"MNQ": _synthetic_bars(), "MES": _synthetic_bars()}
    ta, _, a, _ = assemble_facts(FactStore(), bars, NOW)
    truncated = {tk: df[df.index < NOW] for tk, df in bars.items()}
    tb, _, b, _ = assemble_facts(FactStore(), truncated, NOW)
    assert "level_tf_status" in a
    assert a["level_tf_status"] == b["level_tf_status"]
    assert ta == tb


@pytest.mark.slow
def test_20260928_real_bundle(monkeypatch):                                          # 30
    main = os.path.join(_global(), "general", "main", "2026-12")
    if not os.path.exists(os.path.join(main, "MNQ_1m.parquet")):
        pytest.skip(f"no 2026-12 main parquets at {main}")
    from agent.study.facts_source import StudyFacts
    from agent.study.target_offline import instant_ts
    b = StudyFacts(main_dir=main).bundle_at(instant_ts("2026-09-28", "09:20"))

    monkeypatch.setenv("ACT_PER_TF_LEVEL_STATUS", "0")
    vd, _, ev_off, mag = bench_facts.bundle_to_l1_view(b)
    assert "level_tf_status" not in vd
    off = score_thesis_evidence([], **tiebreak._score_kwargs(vd, mag))
    assert off["net_score"] == pytest.approx(10.5)

    monkeypatch.setenv("ACT_PER_TF_LEVEL_STATUS", "1")
    vd, _, ev_on, mag = bench_facts.bundle_to_l1_view(b)
    kw = tiebreak._score_kwargs(vd, mag)
    on = score_thesis_evidence([], **kw)
    assert on["net_score"] == pytest.approx(5.53125, abs=1e-4)
    assert {a: set(v) for a, v in vd["level_tf_status"].items()} == _RESTORED_0928
    strip = lambda m: {x: {n: {k: v for k, v in s.items() if k != "was"} for n, s in lv.items()}
                       for x, lv in m.items()}
    assert strip(vd["level_tf_status"]) == strip(_map_of(_fixture_kw()))
    # the §0 correction: MNQ prev1_day_low is shadowed, not nested, and not restored
    assert "P2-SUPPRESSED (level is shadowed" in ev_on
    assert "PER-TF: standing 4h read" in ev_on and "PER-TF" not in ev_off


def _load_cache():
    cache = os.path.join(_study_dir(), "cache")
    ref = os.path.join(_study_dir(), "impl_parity", "per_date_v1dp.jsonl")
    study = os.path.join(_study_dir(), "per_date.jsonl")
    if not (os.path.isdir(cache) and os.path.exists(ref) and os.path.exists(study)):
        pytest.skip(f"study cache / pinned reference absent under {_study_dir()}")
    recs = {}
    for fn in sorted(os.listdir(cache)):
        if fn.endswith(".pkl"):
            with open(os.path.join(cache, fn), "rb") as fh:
                r = pickle.load(fh)
            if "skip" not in r:
                recs[r["date"]] = r["kw"]

    def load(p):
        with open(p, encoding="utf-8") as fh:
            return {json.loads(l)["date"]: json.loads(l) for l in fh}
    return recs, load(ref), load(study)


def _call(r):
    net = r["net_score"]
    return r["expected_bias"] if abs(net) >= 1.0 and r["expected_bias"] != "NEUTRAL" else "NEUTRAL"


@pytest.mark.slow
def test_corpus_parity_v1d_pinned():                                                 # 31
    recs, ref, study = _load_cache()
    assert len(study) == 685
    net_ref, net_study, call_study = [], [], []
    for d in study:
        kw = recs[d]
        r = score_thesis_evidence([], **kw, level_tf_status=_map_of(kw))
        if abs(r["net_score"] - ref[d]["net"]) > 1e-4:
            net_ref.append(d)
        if abs(r["net_score"] - study[d]["V1d"]["net"]) > 1e-4:
            net_study.append(d)
        if _call(r) != study[d]["V1d"]["call"]:
            call_study.append(d)
    assert net_ref == []
    assert net_study == ["2024-07-29", "2025-03-17", "2026-06-03"]
    assert call_study == []


@pytest.mark.slow
def test_corpus_flag_off_reproduces_v0():                                            # 32
    recs, _ref, study = _load_cache()
    bad = [d for d in study
           if abs(score_thesis_evidence([], **recs[d], level_tf_status=None)["net_score"]
                  - study[d]["V0"]["net"]) > 1e-4]
    assert bad == []
