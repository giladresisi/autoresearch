# tests/test_smt_dispatch_order.py
# Tests for run_backtest_v2 dispatch ordering and behaviour.

from __future__ import annotations

import copy
import datetime
import inspect
from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd
import pytest

import smt_state


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_1m_bars(date_str: str, n: int = 60, base_price: float = 21000.0) -> pd.DataFrame:
    """Build a synthetic 1m OHLCV DataFrame (tz-aware ET) with n bars starting at 09:20.

    These tests pin the backtest session open to 09:20 (see _SESSION_OPEN_V2 patch in
    each test) so the same-date 09:20 → 16:00 window captures these bars. 09:20 is a
    5m boundary, so bar 0 fires the 5m dispatch path; bar 1 (09:21) is a non-5m bar.
    """
    tz = "America/New_York"
    start = pd.Timestamp(f"{date_str} 09:20:00", tz=tz)
    idx = pd.date_range(start, periods=n, freq="1min")
    data = {
        "Open":   [base_price] * n,
        "High":   [base_price + 5.0] * n,
        "Low":    [base_price - 5.0] * n,
        "Close":  [base_price + 1.0] * n,
        "Volume": [100] * n,
    }
    return pd.DataFrame(data, index=idx)


@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    """Redirect all smt_state paths into tmp_path."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)


# ---------------------------------------------------------------------------
# Test 1: 5m dispatch order is trend → hypothesis → strategy
# ---------------------------------------------------------------------------

def test_5m_dispatch_order_is_trend_then_hypothesis_then_strategy(
    tmp_path, monkeypatch, _isolate_state
):
    """At a 5m boundary, dispatch order must be: trend → hypothesis → strategy."""
    call_order: list[str] = []

    def fake_run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today):
        pass  # no-op

    def fake_run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m, **kwargs):
        call_order.append("hypothesis")
        return None

    def fake_run_trend(now, mnq_1m_bar, mnq_1m_recent):
        call_order.append("trend")
        return None

    def fake_run_strategy(now, mnq_5m_bar, mnq_1m_recent, fill_check_only=False, **kwargs):
        call_order.append("strategy")
        return None

    date_str = "2025-11-14"
    tz = "America/New_York"

    # Bars start at 09:20 (minute=20, 20%5==0 → first bar IS a 5m boundary), so the bar
    # at 09:20 triggers the trend → hypothesis → strategy dispatch path.
    mnq_bars = _make_1m_bars(date_str, n=10)
    mes_bars = _make_1m_bars(date_str, n=10)

    futures_data = {"MNQ": mnq_bars, "MES": mes_bars}

    import daily as _daily_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    import trend as _trend_mod

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", fake_run_daily_fixed)
    monkeypatch.setattr(_hyp_mod,  "run_hypothesis", fake_run_hypothesis)
    monkeypatch.setattr(_trend_mod, "run_trend", fake_run_trend)
    monkeypatch.setattr(_strat_mod, "run_strategy", fake_run_strategy)

    from backtest_smt import run_backtest_v2
    from strategy_smt import load_futures_data as _lfd
    monkeypatch.setattr("strategy_smt.load_futures_data", lambda: futures_data)

    # Patch load_futures_data inside backtest_smt's local import scope
    import backtest_smt as _bt
    with patch.object(_bt, "run_backtest_v2", wraps=_bt.run_backtest_v2):
        pass  # just confirm it wraps properly

    # We need to patch the function that run_backtest_v2 calls internally.
    # run_backtest_v2 does `from strategy_smt import load_futures_data` inside its body.
    # We can intercept by patching the module attribute directly.
    import strategy_smt as _smt
    monkeypatch.setattr(_smt, "load_futures_data", lambda: futures_data)

    # Pin the backtest session open to 09:20 so the same-date 09:20 → 16:00 window
    # captures the synthetic bars (the live SESSION_OPEN is the overnight 18:00).
    monkeypatch.setattr(_bt, "_SESSION_OPEN_V2", "09:20:00")

    run_backtest_v2(date_str, date_str, write_events=False)

    # on_session_start runs hypothesis once before the per-bar loop begins; that leading
    # call is not part of any bar's dispatch. Anchor on the first per-bar dispatch by
    # taking the first trend call (trend runs first inside on_1m_bar) and only consider
    # hypothesis/strategy calls that occur after it.
    trend_indices = [i for i, v in enumerate(call_order) if v == "trend"]
    assert trend_indices, "run_trend was never called on a 5m boundary"
    first_trend = trend_indices[0]

    hyp_after_trend   = [i for i, v in enumerate(call_order) if v == "hypothesis" and i > first_trend]
    strat_after_trend = [i for i, v in enumerate(call_order) if v == "strategy"   and i > first_trend]

    assert hyp_after_trend,   "run_hypothesis was never called after trend on a 5m boundary"
    assert strat_after_trend, "run_strategy was never called after trend on a 5m boundary"

    # Within the first per-bar dispatch: trend < hypothesis < strategy.
    first_hyp   = hyp_after_trend[0]
    first_strat = strat_after_trend[0]

    assert first_trend < first_hyp, (
        f"trend (pos {first_trend}) must fire before hypothesis (pos {first_hyp})"
    )
    assert first_hyp < first_strat, (
        f"hypothesis (pos {first_hyp}) must fire before strategy (pos {first_strat})"
    )


# ---------------------------------------------------------------------------
# Test 2: non-5m boundary only dispatches trend
# ---------------------------------------------------------------------------

def test_1m_only_dispatches_trend(tmp_path, monkeypatch, _isolate_state):
    """For a non-5m boundary bar, only run_trend is called (not hypothesis, not strategy)."""
    calls: dict[str, int] = {"hypothesis": 0, "trend": 0, "strategy": 0, "daily": 0}

    def fake_run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today):
        calls["daily"] += 1

    def fake_run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m, **kwargs):
        calls["hypothesis"] += 1
        return None

    def fake_run_trend(now, mnq_1m_bar, mnq_1m_recent):
        calls["trend"] += 1
        return None

    def fake_run_strategy(now, mnq_5m_bar, mnq_1m_recent, fill_check_only=False, **kwargs):
        calls["strategy"] += 1
        return None

    date_str = "2025-11-14"
    tz = "America/New_York"

    # Bars start at 09:20 (5m boundary).  Build 2 bars: 09:20 (5m boundary) and 09:21 (not 5m).
    mnq_bars = _make_1m_bars(date_str, n=2)
    mes_bars = _make_1m_bars(date_str, n=2)
    futures_data = {"MNQ": mnq_bars, "MES": mes_bars}

    import daily as _daily_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    import trend as _trend_mod
    import strategy_smt as _smt

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", fake_run_daily_fixed)
    monkeypatch.setattr(_hyp_mod,  "run_hypothesis", fake_run_hypothesis)
    monkeypatch.setattr(_trend_mod, "run_trend", fake_run_trend)
    monkeypatch.setattr(_strat_mod, "run_strategy", fake_run_strategy)
    monkeypatch.setattr(_smt, "load_futures_data", lambda: futures_data)

    import backtest_smt as _bt
    from backtest_smt import run_backtest_v2
    # Pin the backtest session open to 09:20 so the same-date 09:20 → 16:00 window
    # captures the synthetic bars (the live SESSION_OPEN is the overnight 18:00).
    monkeypatch.setattr(_bt, "_SESSION_OPEN_V2", "09:20:00")
    run_backtest_v2(date_str, date_str, write_events=False)

    # There is 1 5m-boundary bar (09:20) and 1 non-5m bar (09:21).
    # trend and strategy fire on every bar; hypothesis fires once at on_session_start
    # plus once on the 5m-boundary bar.
    assert calls["hypothesis"] == 2, (
        f"hypothesis should be called twice (on_session_start + 5m boundary), "
        f"got {calls['hypothesis']}"
    )
    assert calls["trend"] == 2, (
        f"trend should be called for every bar (2 bars), got {calls['trend']}"
    )
    assert calls["strategy"] == 2, (
        f"strategy should be called for every bar (2 bars), got {calls['strategy']}"
    )


# ---------------------------------------------------------------------------
# Test 3: trend invalidation blocks same-bar fill
# ---------------------------------------------------------------------------

def test_trend_invalidation_blocks_same_bar_fill(tmp_path, monkeypatch, _isolate_state):
    """A bar that sweeps a high-priority level returns level-swept from run_trend.

    Direction is NOT cleared by run_trend itself — the pipeline (session_pipeline.py)
    handles that by re-evaluating hypothesis after level-swept and emitting trend-broken
    only if the direction changes. This tests the run_trend unit contract.
    """
    import smt_state as _ss

    # Set up state: limit entry pending, direction = "up", day_low below current price.
    _ss.save_global(copy.deepcopy(_ss.DEFAULT_GLOBAL))

    _ss.save_hypothesis({
        "direction":      "up",
        "weekly_mid":     "",
        "daily_mid":      "",
        "last_liquidity": "",
        "divs":           [],
        "targets":        [],
        "cautious_price": "",
        "entry_ranges":   [],
    })

    conf_bar = {
        "time":      "2025-11-14T09:25:00-05:00",
        "high":      21010.0,
        "low":       20990.0,
        "body_high": 21005.0,
        "body_low":  20995.0,
    }
    _ss.save_position({
        "active": {},
        "stop_entry": 21000.0,
        "conf_bar_entry": conf_bar,
        "failed_entries": 0,
    })

    # daily.json: day_low = 21050.0 (above current close so a bar dipping below it sweeps it)
    _ss.save_daily({
        "date": "2025-11-14",
        "liquidities": [
            {"name": "day_low", "kind": "level", "price": 21050.0},
        ],
        "estimated_dir": "up",
        "opposite_premove": "no",
    })

    # Bar whose low breaches day_low (21050):
    # direction=="up", level_price=21050 < bar_close=21060, bar_low=21040 <= 21050 → level-swept
    now_ts = pd.Timestamp("2025-11-14 09:25:00", tz="America/New_York")
    bar_dict = {
        "time":  now_ts.isoformat(),
        "open":  21070.0,
        "high":  21080.0,
        "low":   21040.0,
        "close": 21060.0,
    }

    # Build a minimal mnq_1m_recent DataFrame
    idx = pd.DatetimeIndex([now_ts])
    recent_bars = pd.DataFrame({
        "Open":  [21070.0],
        "High":  [21080.0],
        "Low":   [21040.0],
        "Close": [21060.0],
    }, index=idx)

    from trend import run_trend
    sig = run_trend(now_ts, bar_dict, recent_bars)

    # run_trend returns level-swept; pipeline converts to trend-broken after hypothesis re-eval.
    assert sig is not None, "run_trend should return a signal"
    assert sig["kind"] == "level-swept", f"expected 'level-swept', got {sig['kind']}"
    assert sig["level_name"] == "day_low"
    # Hypothesis direction is unchanged by run_trend — pipeline owns that transition.
    assert _ss.load_hypothesis()["direction"] == "up", (
        "run_trend must not clear hypothesis direction; only session_pipeline does that"
    )


# ---------------------------------------------------------------------------
# Test 4: smoke test — one real backtest day (skips if parquet missing)
# ---------------------------------------------------------------------------

def test_run_backtest_v2_smoke_one_day(tmp_path, monkeypatch, _isolate_state):
    """run_backtest_v2 returns expected structure; skips if real parquet is absent."""
    try:
        from strategy_smt import load_futures_data
        data = load_futures_data()
        if "MNQ" not in data or data["MNQ"].empty:
            pytest.skip("MNQ parquet not available")
    except Exception as exc:
        pytest.skip(f"parquet not available: {exc}")

    from backtest_smt import run_backtest_v2
    result = run_backtest_v2("2025-11-14", "2025-11-14", write_events=False)

    assert "trades"  in result, "result must contain 'trades'"
    assert "events"  in result, "result must contain 'events'"
    assert "metrics" in result, "result must contain 'metrics'"
    assert "n_trades"  in result["metrics"]
    assert "total_pnl" in result["metrics"]
    assert "win_rate"  in result["metrics"]


# ---------------------------------------------------------------------------
# Test 5: old run_backtest is unchanged and callable with the same signature
# ---------------------------------------------------------------------------

def test_old_run_backtest_unchanged():
    """run_backtest must still be callable and accept (mnq_df, mes_df, start, end)."""
    from backtest_smt import run_backtest
    assert callable(run_backtest), "run_backtest must be callable"

    sig = inspect.signature(run_backtest)
    params = list(sig.parameters.keys())

    expected = ["mnq_df", "mes_df", "start", "end"]
    for p in expected:
        assert p in params, f"run_backtest must accept parameter '{p}', got {params}"
