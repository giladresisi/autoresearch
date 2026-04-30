# tests/test_eqh_eql_detection.py
# Unit tests for detect_eqh_eql and its helpers, plus integration tests for
# _build_draws_and_select with EQH/EQL candidates. Pure-function tests — no IB,
# no filesystem fixtures.
import pandas as pd
import pytest

import strategy_smt as _strat
from strategy_smt import detect_eqh_eql


def _bars(patterns):
    """patterns: list of (open, high, low, close) tuples. Returns DataFrame with DatetimeIndex."""
    ts_start = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=ts_start, periods=len(patterns), freq="1min")
    return pd.DataFrame(
        patterns, columns=["Open", "High", "Low", "Close"], index=idx,
    ).assign(Volume=100.0)


def _flat(n, price=20000.0):
    return [(price, price, price, price)] * n


def _set_high(rows, i, high, low=None, close=None):
    """Set bar i to a spike-high pattern (higher High than neighbours)."""
    base_l = low if low is not None else rows[i][2]
    base_c = close if close is not None else (high - 1.0)
    rows[i] = (high - 2.0, high, base_l, base_c)
    return rows


def _set_low(rows, i, low, high=None, close=None):
    base_h = high if high is not None else rows[i][1]
    base_c = close if close is not None else (low + 1.0)
    rows[i] = (low + 2.0, base_h, low, base_c)
    return rows


# ─── Basic detection tests ────────────────────────────────────────────────────

def test_disabled_flag_returns_empty(monkeypatch):
    monkeypatch.setattr(_strat, "EQH_ENABLED", False)
    df = _bars(_flat(50))
    # Even with synthetic swings, disabled flag short-circuits.
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 30, 20100.0)
    df = _bars(rows)
    eqh, eql = detect_eqh_eql(df, 50)
    assert eqh == []
    assert eql == []


def test_insufficient_bars_returns_empty():
    df = _bars(_flat(5))
    eqh, eql = detect_eqh_eql(df, 5)
    assert eqh == []
    assert eql == []


def test_flat_data_returns_empty():
    df = _bars(_flat(50))
    eqh, eql = detect_eqh_eql(df, 50)
    assert eqh == []
    assert eql == []


def test_single_swing_not_enough():
    rows = _flat(50)
    _set_high(rows, 20, 20100.0)
    df = _bars(rows)
    eqh, eql = detect_eqh_eql(df, 50)
    # Single swing high doesn't meet min_touches=2.
    assert eqh == []


# ─── Clustering tests ─────────────────────────────────────────────────────────

def test_two_exact_equal_highs_cluster():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 30, 20100.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50)
    assert len(eqh) == 1
    assert eqh[0]["touches"] == 2
    assert eqh[0]["price"] == pytest.approx(20100.0)


def test_two_equal_highs_within_tolerance():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 30, 20102.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50, tolerance=3.0)
    assert len(eqh) == 1
    assert eqh[0]["touches"] == 2
    assert eqh[0]["price"] == pytest.approx(20101.0)


def test_two_equal_highs_outside_tolerance():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 30, 20110.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50, tolerance=3.0, min_touches=2)
    # Two separate single-swing clusters; neither meets min_touches.
    assert eqh == []


def test_three_equal_highs_one_cluster():
    rows = _flat(60)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 25, 20100.0)
    _set_high(rows, 40, 20100.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 60)
    assert len(eqh) == 1
    assert eqh[0]["touches"] == 3


def test_min_touches_filter():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 30, 20100.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50, min_touches=3)
    assert eqh == []


def test_eql_symmetric():
    rows = _flat(50)
    _set_low(rows, 10, 19900.0)
    _set_low(rows, 30, 19900.0)
    df = _bars(rows)
    _, eql = detect_eqh_eql(df, 50)
    assert len(eql) == 1
    assert eql[0]["touches"] == 2
    assert eql[0]["price"] == pytest.approx(19900.0)


# ─── Staleness tests ──────────────────────────────────────────────────────────

def test_stale_level_filtered():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 25, 20100.0)
    # Bar 35: close above the 20100 level → stale.
    rows[35] = (20050.0, 20160.0, 20050.0, 20150.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50)
    assert eqh == []


def test_wick_through_level_still_active():
    rows = _flat(50)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 25, 20100.0)
    # Bar 35: wicks through high=20105 but closes below at 20099 — level still active.
    rows[35] = (20050.0, 20105.0, 20050.0, 20099.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 50)
    assert len(eqh) == 1


def test_eql_stale_below():
    rows = _flat(50)
    _set_low(rows, 10, 19900.0)
    _set_low(rows, 25, 19900.0)
    # Bar 35: closes through 19900 downward.
    rows[35] = (19950.0, 19950.0, 19800.0, 19850.0)
    df = _bars(rows)
    _, eql = detect_eqh_eql(df, 50)
    assert eql == []


def test_wick_through_eql_still_active():
    rows = _flat(50)
    _set_low(rows, 10, 19900.0)
    _set_low(rows, 25, 19900.0)
    # Bar 35: low wicks through 19895 but close above at 19901.
    rows[35] = (19950.0, 19950.0, 19895.0, 19901.0)
    df = _bars(rows)
    _, eql = detect_eqh_eql(df, 50)
    assert len(eql) == 1


def test_stale_scan_window():
    # Ensure staleness check walks FROM last_bar+1 (not just the last few bars).
    rows = _flat(60)
    _set_high(rows, 5, 20100.0)
    _set_high(rows, 15, 20100.0)
    # Bar 25 closes through (upward); bars 30+ return to baseline.
    rows[25] = (20050.0, 20160.0, 20050.0, 20150.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 60)
    assert eqh == []


# ─── Lookahead tests ──────────────────────────────────────────────────────────

def test_no_lookahead_in_swing_detection():
    # bar_idx=20 → scan must end at 20 - swing_bars - 1 = 16. A swing at bar 25 MUST NOT appear.
    rows = _flat(50)
    _set_high(rows, 25, 20100.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 20, swing_bars=3, min_touches=1)
    # Bar 25 is beyond the scan window — cannot be detected.
    assert all(lvl["last_bar"] <= 16 for lvl in eqh)
    # With min_touches=2 and only bar 10 being a real swing (added below scenario),
    # no cluster qualifies regardless.


def test_swing_at_bar_end_filtered():
    rows = _flat(30)
    _set_high(rows, 10, 20100.0)
    _set_high(rows, 29, 20100.0)  # cannot qualify as swing — no forward window
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 30, swing_bars=3)
    # Bar 29 filtered → only one valid swing at bar 10 → no cluster at min_touches=2.
    assert eqh == []


# ─── Ranking tests ────────────────────────────────────────────────────────────

def test_sort_by_touches_desc():
    # Build two distinct EQH clusters: one with 3 touches, one with 2.
    # Cluster A at 20200 (3 touches), cluster B at 20100 (2 touches).
    # Close the 20200 bars at 20001 (below the 20100 cluster) so neither staleness-fires.
    rows = _flat(80)
    _set_high(rows, 10, 20100.0, close=20050.0)
    _set_high(rows, 25, 20100.0, close=20050.0)
    _set_high(rows, 40, 20200.0, close=20050.0)
    _set_high(rows, 55, 20200.0, close=20050.0)
    _set_high(rows, 70, 20200.0, close=20050.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 80)
    assert len(eqh) == 2
    assert eqh[0]["touches"] == 3
    assert eqh[1]["touches"] == 2


def test_tiebreak_by_recency():
    # Two clusters with the same touches=2; the cluster with later last_bar should come first.
    # Cluster A at 20100 (bars 5, 10), cluster B at 20200 (bars 50, 70).
    # Close 20200 bars below 20100 to avoid staling the A cluster.
    rows = _flat(80)
    _set_high(rows, 5,  20100.0, close=20050.0)
    _set_high(rows, 10, 20100.0, close=20050.0)
    _set_high(rows, 50, 20200.0, close=20050.0)
    _set_high(rows, 70, 20200.0, close=20050.0)
    df = _bars(rows)
    eqh, _ = detect_eqh_eql(df, 80)
    assert len(eqh) == 2
    # Both clusters have touches=2; tie-break by last_bar desc → 20200 (last_bar=70) first.
    assert eqh[0]["last_bar"] > eqh[1]["last_bar"]


# ─── Integration tests for _build_draws_and_select (Task 3.1) ──────────────────

def test_build_draws_includes_eqh_candidate():
    """EQH is picked as primary TP when it is the nearest valid candidate above entry."""
    from strategy_smt import _build_draws_and_select

    # Entry 100, stop 98 → stop_dist=2; min_dist=max(15, 1.5*2)=15.
    # EQH at 118 (18 pts) beats TDO at 125 (25 pts). run_ses_high=100 silences session_high.
    eqh_levels = [{"price": 118.0, "touches": 3, "last_bar": 50}]
    eql_levels = []

    tp_name, tp_price, _sec_name, _sec_price, _valid = _build_draws_and_select(
        direction="long", ep=100.0, sp=98.0, fvg_zone=None,
        day_tdo=125.0, midnight_open=None,
        run_ses_high=100.0, run_ses_low=95.0,
        overnight={"overnight_high": 140.0, "overnight_low": 95.0},
        pdh=150.0, pdl=90.0,
        eqh_levels=eqh_levels, eql_levels=eql_levels,
    )
    assert tp_name == "eqh"
    assert tp_price == 118.0


def test_build_draws_skips_eqh_when_below_entry_for_long():
    """EQH must be ABOVE entry price for longs to qualify as a target."""
    from strategy_smt import _build_draws_and_select
    eqh_levels = [{"price": 95.0, "touches": 3, "last_bar": 50}]
    tp_name, _tp_price, _, _, _valid = _build_draws_and_select(
        direction="long", ep=100.0, sp=98.0, fvg_zone=None,
        day_tdo=120.0, midnight_open=None,
        run_ses_high=100.0, run_ses_low=95.0,
        overnight={"overnight_high": 140.0, "overnight_low": 95.0},
        pdh=150.0, pdl=90.0,
        eqh_levels=eqh_levels, eql_levels=[],
    )
    # EQH at 95 is below entry 100 — filtered out. TDO at 120 (20pt) is closest remaining.
    assert tp_name == "tdo"


def test_build_draws_short_picks_eql():
    """Mirror test for shorts picking EQL."""
    from strategy_smt import _build_draws_and_select
    # ep=100, sp=102 (stop_dist=2, min_dist=15). EQL at 82 (18pt) beats TDO at 75 (25pt).
    # run_ses_low=100 silences session_low (requires < ep-1=99).
    eql_levels = [{"price": 82.0, "touches": 3, "last_bar": 50}]
    tp_name, tp_price, _, _, _valid = _build_draws_and_select(
        direction="short", ep=100.0, sp=102.0, fvg_zone=None,
        day_tdo=75.0, midnight_open=None,
        run_ses_high=105.0, run_ses_low=100.0,
        overnight={"overnight_high": 105.0, "overnight_low": 60.0},
        pdh=110.0, pdl=50.0,
        eqh_levels=[], eql_levels=eql_levels,
    )
    assert tp_name == "eql"
    assert tp_price == 82.0


def test_build_draws_empty_eqh_degrades_gracefully():
    """With empty lists, behavior is identical to before Gap 1 — TDO is picked."""
    from strategy_smt import _build_draws_and_select
    tp_name, _tp_price, _, _, _valid = _build_draws_and_select(
        direction="long", ep=100.0, sp=98.0, fvg_zone=None,
        day_tdo=120.0, midnight_open=None,
        run_ses_high=100.0, run_ses_low=95.0,
        overnight={"overnight_high": 140.0, "overnight_low": 95.0},
        pdh=150.0, pdl=90.0,
        eqh_levels=[], eql_levels=[],
    )
    # TDO at 120 (20pt) is the nearest valid candidate from the original pool.
    assert tp_name == "tdo"
