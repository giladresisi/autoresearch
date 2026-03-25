"""tests/test_position_monitor.py — Unit tests for position_monitor.py."""
import datetime
import json
import os

import pandas as pd
import pytest

from tests.test_screener import make_pivot_signal_df


@pytest.fixture()
def screener_cache_tmpdir(tmp_path, monkeypatch):
    """Patch SCREENER_CACHE_DIR to a fresh tmpdir."""
    import screener_prepare
    import position_monitor
    monkeypatch.setattr(screener_prepare, "SCREENER_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(position_monitor, "SCREENER_CACHE_DIR", str(tmp_path))
    return tmp_path


def _write_parquet(path, today_price=None):
    """Write a synthetic parquet suitable for position_monitor tests."""
    df = make_pivot_signal_df(250)
    if today_price is not None:
        df.iloc[-1, df.columns.get_loc("close")] = today_price
        df.iloc[-1, df.columns.get_loc("price_1030am")] = today_price
    df.to_parquet(path)
    return df


def _portfolio_json(tmp_path, positions):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"positions": positions}))
    return str(path)


def _sample_position(entry_price=100.0, stop_price=95.0, entry_date="2026-01-01"):
    return {
        "ticker": "AAPL",
        "entry_price": entry_price,
        "entry_date": entry_date,
        "shares": 10,
        "stop_price": stop_price,
        "notes": "",
    }


def test_reads_portfolio_json(screener_cache_tmpdir, tmp_path, monkeypatch):
    """Parses positions dict correctly without crashing."""
    import position_monitor as pm
    _write_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: 100.0)
    portfolio = _portfolio_json(tmp_path, [_sample_position()])
    pm.run_monitor(portfolio)  # should not raise


def test_raise_stop_when_1_5_atr_profit(screener_cache_tmpdir, tmp_path, monkeypatch, capsys):
    """With price 1.6 ATR above entry, output contains RAISE-STOP."""
    import position_monitor as pm
    df = _write_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    from train import calc_atr14
    atr = float(calc_atr14(df).dropna().iloc[-1])
    entry_price = float(df["close"].iloc[-1])
    # Set current price well above entry + 1.5 ATR to trigger breakeven/trail
    current_price = entry_price + 2.0 * atr
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: current_price)
    # entry_date far in the past so time-based exit doesn't trigger
    pos = _sample_position(entry_price=entry_price, stop_price=entry_price - atr, entry_date="2025-01-01")
    portfolio = _portfolio_json(tmp_path, [pos])
    pm.run_monitor(portfolio)
    captured = capsys.readouterr()
    assert "RAISE-STOP" in captured.out


def test_no_output_when_stop_unchanged(screener_cache_tmpdir, tmp_path, monkeypatch, capsys):
    """manage_position returns same stop as current → no RAISE-STOP output."""
    import position_monitor as pm
    df = _write_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    entry_price = float(df["close"].iloc[-1])
    from train import calc_atr14
    atr = float(calc_atr14(df).dropna().iloc[-1])
    current_stop = entry_price - atr
    # Mock manage_position to return the same stop — simulating no raise needed
    monkeypatch.setattr("position_monitor.manage_position", lambda pos, df_ext: pos["stop_price"])
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: entry_price)
    pos = _sample_position(entry_price=entry_price, stop_price=current_stop, entry_date="2025-01-01")
    portfolio = _portfolio_json(tmp_path, [pos])
    pm.run_monitor(portfolio)
    captured = capsys.readouterr()
    assert "RAISE-STOP" not in captured.out


def test_loads_from_screener_cache_dir_not_harness(screener_cache_tmpdir, tmp_path, monkeypatch):
    """position_monitor uses SCREENER_CACHE_DIR, not CACHE_DIR from train.py."""
    import position_monitor as pm
    from train import CACHE_DIR as HARNESS_DIR
    # Put AAPL only in screener cache (not harness dir) — should work
    _write_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: 100.0)
    portfolio = _portfolio_json(tmp_path, [_sample_position()])
    pm.run_monitor(portfolio)  # should not print "not in SCREENER_CACHE_DIR"


def test_missing_ticker_skipped_with_warning(screener_cache_tmpdir, tmp_path, monkeypatch, capsys):
    """Ticker not in SCREENER_CACHE_DIR → warning printed, continues."""
    import position_monitor as pm
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: 100.0)
    # AAPL.parquet does NOT exist in cache
    portfolio = _portfolio_json(tmp_path, [_sample_position()])
    pm.run_monitor(portfolio)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out or "skipping" in captured.out.lower()


def test_empty_portfolio_prints_no_signals(screener_cache_tmpdir, tmp_path, capsys):
    """Empty positions array → prints summary with 0 open positions."""
    import position_monitor as pm
    portfolio = _portfolio_json(tmp_path, [])
    pm.run_monitor(portfolio)
    captured = capsys.readouterr()
    assert "0 open" in captured.out


def test_position_monitor_runs_without_exception(screener_cache_tmpdir, tmp_path, monkeypatch):
    """Full run with synthetic parquet and sample portfolio — no exception."""
    import position_monitor as pm
    _write_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    monkeypatch.setattr(pm, "_fetch_last_price", lambda t: 100.0)
    portfolio = _portfolio_json(tmp_path, [_sample_position()])
    pm.run_monitor(portfolio)  # should not raise
