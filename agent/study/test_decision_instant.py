import datetime
import types

import pandas as pd
import pytest

from agent.study.decision_instant import (
    INSTANTS, degenerate_at, instant_ts, observations_at)

TZ = "America/New_York"
D = "2026-08-13"


class _Facts:
    """A StudyFacts stand-in: one bundle, hand-made bars."""

    def __init__(self, levels, avg=100.0, swept=None, bars=None):
        self._bundle = types.SimpleNamespace(
            levels={"MNQ": levels}, mid_price={"MNQ": {}},
            swept_at={"MNQ": swept or {}}, avg_range_1h={"MNQ": avg},
            fvg_zones=[])
        idx = pd.date_range(f"{D} 09:30", periods=20, freq="1min", tz=TZ)
        self._bars = bars if bars is not None else pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)

    def bundle_at(self, ts):
        return self._bundle

    def bars_1m(self, tk):
        return self._bars


def _lvl(price, side="above", tier="day"):
    return (price, price, side, tier, None)


def _lab(**kw):
    base = {"date": D, "segment": 0, "ticker": "MNQ", "status": "labelled",
            "direction": "up", "avg_range_1h": {"MNQ": 100.0}}
    base.update(kw)
    return base


def _cand(price, outcome):
    return {"date": D, "segment": 0, "ticker": "MNQ", "price": price, "outcome": outcome}


def test_instants_are_clock_times_a_production_system_can_compute():
    assert INSTANTS == ("09:32", "09:35", "09:40")
    ts = instant_ts(D, "09:35")
    assert (ts.hour, ts.minute) == (9, 35)
    assert instant_ts(datetime.date(2026, 8, 13), "09:35") == ts


def test_the_reference_price_is_now_price_not_the_origin():
    """The whole point of the plan: distances measured the way _dol_menu measures them.
    Price is 100 at the instant, so a pool at 150 is 0.5 x avg away, not 1.5."""
    f = _Facts({"prev1_day_high": _lvl(150.0), "prev2_day_high": _lvl(220.0)})
    obs = observations_at([_lab()], [_cand(150.0, "draw"), _cand(220.0, "x")], f, "09:35")
    assert obs[0].dist == pytest.approx(0.5)


def test_pools_swept_before_the_instant_are_gone_from_the_stack():
    """Not merely re-ranked: 13%/30% of draws are already taken by +2 min, and a swept
    pool is not a candidate."""
    f = _Facts({"prev1_day_high": _lvl(150.0), "prev2_day_high": _lvl(220.0)},
               swept={"prev1_day_high": pd.Timestamp(f"{D} 09:31", tz=TZ)})
    obs = observations_at([_lab()], [_cand(220.0, "draw"), _cand(150.0, "x")], f, "09:35")
    assert [o.rank for o in obs] == [0]
    assert obs[0].dist == pytest.approx(1.2)      # 220 - 100, not measured from 150


def test_the_gap_between_two_pools_is_unchanged_by_the_anchor():
    """B8g's feature must be anchor-invariant. If this moves, the port is wrong and the
    rule was an artefact of the lookahead origin."""
    f = _Facts({"prev1_day_high": _lvl(150.0), "prev2_day_high": _lvl(220.0),
                "prev3_day_high": _lvl(400.0)})
    obs = observations_at([_lab()],
                          [_cand(150.0, "x"), _cand(220.0, "x"), _cand(400.0, "draw")],
                          f, "09:35")
    assert obs[0].gap_ahead == pytest.approx(0.7)      # (220-150)/100
    assert obs[1].gap_ahead == pytest.approx(1.8)      # (400-220)/100


def test_only_pools_up_to_the_draw_become_observations():
    f = _Facts({"prev1_day_high": _lvl(150.0), "prev2_day_high": _lvl(220.0),
                "prev3_day_high": _lvl(400.0)})
    obs = observations_at([_lab()],
                          [_cand(150.0, "x"), _cand(220.0, "draw"), _cand(400.0, "x")],
                          f, "09:35")
    assert [o.stop for o in obs] == [False, True]


def test_a_draw_already_passed_at_the_instant_yields_nothing_and_is_counted():
    """A degenerate session is a coverage fact, not an absence — counting it as missing
    would flatter every accuracy computed afterwards."""
    f = _Facts({"prev1_day_high": _lvl(150.0)},
               swept={"prev1_day_high": pd.Timestamp(f"{D} 09:31", tz=TZ)})
    labels, cands = [_lab()], [_cand(150.0, "draw")]
    assert observations_at(labels, cands, f, "09:35") == []
    d = degenerate_at(labels, cands, f, "09:35", tickers=("MNQ",))
    assert d["MNQ"] == {"total": 1, "usable": 0, "degenerate": 1}


def test_a_pool_behind_the_current_price_is_not_eligible():
    f = _Facts({"prev1_day_low": _lvl(50.0, side="below"), "prev1_day_high": _lvl(150.0)})
    obs = observations_at([_lab()], [_cand(150.0, "draw"), _cand(50.0, "x")], f, "09:35")
    assert len(obs) == 1 and obs[0].dist == pytest.approx(0.5)


def test_a_down_move_measures_pools_below_the_current_price():
    f = _Facts({"prev1_day_low": _lvl(50.0, side="below"),
                "prev2_day_low": _lvl(150.0, side="below")})
    obs = observations_at([_lab(direction="down")],
                          [_cand(50.0, "draw"), _cand(150.0, "x")], f, "09:35")
    assert len(obs) == 1 and obs[0].dist == pytest.approx(0.5)


def test_the_label_is_still_the_phase_two_draw():
    """Only the anchor moves. A change in the label would make the comparison to cycle 4
    meaningless."""
    f = _Facts({"prev1_day_high": _lvl(150.0), "prev2_day_high": _lvl(220.0)})
    obs = observations_at([_lab()], [_cand(150.0, "x"), _cand(220.0, "draw")], f, "09:35")
    assert obs[-1].stop is True and obs[-1].dist == pytest.approx(1.2)


def test_the_module_never_reads_a_wall_clock_or_a_result_field():
    import inspect

    import agent.study.decision_instant as mod
    src = inspect.getsource(mod)
    assert "datetime.now" not in src and "get_et_now" not in src
    for banned in ("overshoot", "lag_min", "own_extreme", "extreme_ts"):
        assert '["%s"]' % banned not in src, banned
