"""NEUTRAL tie-break (agent/trader/tiebreak.py) and its Analyzer seam."""
import pytest

from agent import run_agent  # noqa: F401  (puts agent/contracts on sys.path)
from agent.trader import tiebreak as tb
from agent.trader.analyzer import Analyzer, stands

F_DOWN = {"type": "price_beyond", "price": 30062.5, "side": "above"}
MENUS = {
    "dol": {"UP": [{"id": "D1", "level": "london(cur)_high", "price": 30062.5, "dist_ratio": 1.33}],
            "DOWN": [{"id": "D1", "level": "asia(cur)_low", "price": 29745.75, "dist_ratio": 2.60}]},
    "predicates": {"UP": [], "DOWN": [{"id": "F1", "family": "falsification",
                                       "predicate": F_DOWN, "recommended": True}]},
}
NOW = "2026-07-15 09:19:00-04:00"          # session opened 2026-07-14 18:00
SMT_DAY_HIGH = {"level": "london(cur)_high", "tier": "day", "meaningful": True, "side": "above",
                "swept_ticker": "MES", "unswept_ticker": "MNQ",
                "promoted_from": "session", "swept_at": "2026-07-15 08:43:00-04:00",
                "suggested_exhausted": False}
SMT_WEEK_HIGH = {"level": "prev2_week_high", "tier": "week", "meaningful": True,
                 "side": "above", "swept_ticker": "MNQ", "unswept_ticker": "MES",
                 "promoted_from": None, "swept_at": "2026-07-14 20:05:00-04:00",
                 "suggested_exhausted": False}
REJECTED_DAY_HIGH = {"MES": {"london(cur)_high": {"1h": False, "4h": None}}}
ACCEPTED_WEEK_HIGH = {"MNQ": {"prev2_week_high": {"1h": True, "4h": True}}}
NEUTRAL = {"bias": "NEUTRAL", "regime": "RANGE", "dol": None, "falsified_if": [],
           "confidence": "LOW", "evidence": []}


@pytest.fixture
def net(monkeypatch):
    def _set(value):
        monkeypatch.setattr(tb, "_net_score", lambda t, f, m: {"net_score": value})
    return _set


def _facts(smt=(), menus=MENUS, closes=None, now=NOW):
    status = {}
    for part in (closes if closes is not None else [REJECTED_DAY_HIGH]):
        for asset, levels in part.items():
            status.setdefault(asset, {}).update(levels)
    return {"menus": menus, "smt_candidates": list(smt), "level_htf_close_status": status,
            "now": now}


def test_rejected_day_extreme_smt_wins_over_net_sign_and_nearer_dol(net):
    """2026-07-15: net and nearer-draw both say UP; the day-high SMT, rejected by the
    lagger's 09:00 1h close, says DOWN."""
    net(0.75)
    t = tb.tiebreak_thesis(NEUTRAL, _facts([SMT_DAY_HIGH]))
    assert (t["bias"], t["tiebreak_rule"], t["model_bias"]) == (
        "DOWN", "rejected_day_extreme_smt", "NEUTRAL")
    assert t["dol"] == {"level": "asia(cur)_low", "price": 29745.75}
    assert t["falsified_if"] == [F_DOWN] and t["thesis_source"] == "tiebreak"
    assert stands(t)


@pytest.mark.parametrize("lagger_1h", [True, None])
def test_a_day_extreme_smt_the_lagger_did_not_reject_does_not_decide(net, lagger_1h):
    net(0.75)
    closes = [{"MES": {"london(cur)_high": {"1h": lagger_1h, "4h": None}}}]
    t = tb.tiebreak_thesis(NEUTRAL, _facts([SMT_DAY_HIGH], closes=closes))
    assert t["tiebreak_rule"] == "net_sign" and t["bias"] == "UP"


def test_the_leaders_close_is_never_read():
    closes = {"MNQ": {"london(cur)_high": {"1h": False}}}            # leader, not lagger
    assert tb.rejected_day_extreme_smt_direction([SMT_DAY_HIGH], closes) is None


@pytest.mark.parametrize("change", [{"promoted_from": None}, {"suggested_exhausted": True},
                                    {"meaningful": False}, {"side": None}])
def test_only_a_live_running_extreme_smt_counts(net, change):
    net(0.75)
    t = tb.tiebreak_thesis(NEUTRAL, _facts([{**SMT_DAY_HIGH, **change}]))
    assert t["tiebreak_rule"] == "net_sign" and t["bias"] == "UP"


def test_the_freshest_rejected_day_extreme_smt_decides():
    low = {**SMT_DAY_HIGH, "level": "asia(cur)_low", "side": "below",
           "swept_at": "2026-07-15 07:00:00-04:00"}
    closes = {"MES": {"london(cur)_high": {"1h": False}, "asia(cur)_low": {"1h": False}}}
    assert tb.rejected_day_extreme_smt_direction([low, SMT_DAY_HIGH], closes) == "DOWN"
    low["swept_at"] = "2026-07-15 09:00:00-04:00"
    assert tb.rejected_day_extreme_smt_direction([low, SMT_DAY_HIGH], closes) == "UP"


def test_an_accepted_week_smt_means_continuation_and_outranks_rule_2(net):
    """2026-09-21 shape: a bearish week-high SMT the lagger closed beyond on 1h and 4h is
    ignored by the market -> UP, even with a rejected day-high SMT pointing DOWN."""
    net(-2.0)
    t = tb.tiebreak_thesis(NEUTRAL, _facts([SMT_WEEK_HIGH, SMT_DAY_HIGH],
                                           closes=[REJECTED_DAY_HIGH, ACCEPTED_WEEK_HIGH]))
    assert (t["bias"], t["tiebreak_rule"]) == ("UP", "accepted_week_smt")


def test_an_accepted_week_low_smt_means_down():
    """2026-08-18 shape: bullish SMT at the 08-11 low, MES closed below it -> DOWN."""
    low = {**SMT_WEEK_HIGH, "level": "prev1_week_low", "side": "below", "swept_ticker": "MES"}
    closes = {"MES": {"prev1_week_low": {"1h": True, "4h": True}}}
    assert tb.accepted_week_smt_direction([low], closes, NOW) == "DOWN"


@pytest.mark.parametrize("change,closes", [
    ({"swept_at": "2026-07-14 10:34:00-04:00"}, ACCEPTED_WEEK_HIGH),   # previous session (07-15)
    ({}, {"MNQ": {"prev2_week_high": {"1h": True, "4h": False}}}),     # 4h rejected
    ({}, {"MNQ": {"prev2_week_high": {"1h": True, "4h": None}}}),      # 4h immature
    ({"tier": "day"}, ACCEPTED_WEEK_HIGH),                              # not week tier
])
def test_rule_1_needs_a_current_session_week_smt_accepted_on_both_tfs(change, closes):
    assert tb.accepted_week_smt_direction([{**SMT_WEEK_HIGH, **change}], closes, NOW) is None


def test_the_session_opens_at_1800_et():
    assert str(tb.session_start("2026-07-15 09:19:00-04:00")) == "2026-07-14 18:00:00-04:00"
    assert str(tb.session_start("2026-07-15 19:00:00-04:00")) == "2026-07-15 18:00:00-04:00"
    assert tb.session_start(None) is None


def test_exact_zero_net_takes_the_nearer_draw(net):
    net(0.0)
    t = tb.tiebreak_thesis(NEUTRAL, _facts())
    assert (t["bias"], t["tiebreak_rule"]) == ("UP", "nearer_dol")


def test_a_rule_whose_side_has_no_dol_falls_through(net):
    net(-2.0)
    menus = {"dol": {"UP": MENUS["dol"]["UP"], "DOWN": []}, "predicates": {}}
    t = tb.tiebreak_thesis(NEUTRAL, _facts([SMT_DAY_HIGH], menus=menus))
    assert (t["bias"], t["tiebreak_rule"]) == ("UP", "nearer_dol")


def test_no_menu_at_all_leaves_the_thesis_alone(net):
    net(1.0)
    assert tb.tiebreak_thesis(NEUTRAL, _facts(menus={})) is None


def test_a_directional_thesis_is_never_tie_broken():
    assert tb.tiebreak_thesis({**NEUTRAL, "bias": "UP"}, _facts([SMT_DAY_HIGH])) is None


@pytest.mark.parametrize("value,enabled", [(None, True), ("", True), ("1", True),
                                           ("0", False), ("false", False), ("OFF", False)])
def test_flag_defaults_to_on(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("ACT_THESIS_TIEBREAK", raising=False)
    else:
        monkeypatch.setenv("ACT_THESIS_TIEBREAK", value)
    assert tb.tiebreak_enabled() is enabled


# -- the Analyzer seam ---------------------------------------------------------------- #

@pytest.fixture
def analyzer(tmp_path, net, monkeypatch):
    net(0.75)
    monkeypatch.delenv("ACT_THESIS_TIEBREAK", raising=False)
    monkeypatch.delenv("ACT_THESIS_FAILSAFE", raising=False)
    return Analyzer(tmp_path, backend=lambda *a, **k: None)


def _seam(a, thesis=NEUTRAL, meta=None, health=None):
    return a._tiebreak(thesis, meta or {"verdict": "clean"}, _facts([SMT_DAY_HIGH]), None,
                       health or {"degraded": False})


def test_seam_tie_breaks_a_clean_neutral(analyzer):
    assert _seam(analyzer)["bias"] == "DOWN"


def test_seam_skips_a_standing_thesis(analyzer):
    standing = {**NEUTRAL, "bias": "UP", "dol": {"level": "x", "price": 1.0}}
    assert _seam(analyzer, thesis=standing) is None


def test_seam_skips_a_degraded_view(analyzer):
    assert _seam(analyzer, health={"degraded": True}) is None


def test_seam_respects_the_flag(analyzer, monkeypatch):
    monkeypatch.setenv("ACT_THESIS_TIEBREAK", "0")
    assert _seam(analyzer) is None


def test_seam_keeps_an_operator_requested_failsafe_dark(analyzer, monkeypatch):
    meta = {"verdict": "failsafe"}
    assert _seam(analyzer, meta=meta)["bias"] == "DOWN"      # failsafe flag off
    monkeypatch.setenv("ACT_THESIS_FAILSAFE", "true")
    assert _seam(analyzer, meta=meta) is None


def test_seam_is_total(analyzer, monkeypatch):
    monkeypatch.setattr(tb, "tiebreak_thesis", lambda *a, **k: 1 / 0)
    assert _seam(analyzer) is None
