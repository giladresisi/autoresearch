"""tests/test_analyze_gaps.py — Unit tests for analyze_gaps.py."""
import datetime
import io
import os

import pandas as pd
import pytest

from tests.test_screener import make_pivot_signal_df
from analyze_gaps import compute_gaps, print_analysis, load_trades


def _write_parquet(cache_dir, ticker, last_close, last_date=None):
    """Write a minimal parquet for a ticker with a known last close."""
    df = make_pivot_signal_df(250).copy()
    if last_date is None:
        last_date = datetime.date(2024, 12, 1)
    new_index = [last_date - datetime.timedelta(days=i) for i in range(len(df) - 1, -1, -1)]
    df.index = pd.Index(new_index, name="date")
    df.iloc[-1, df.columns.get_loc("close")] = last_close
    os.makedirs(cache_dir, exist_ok=True)
    df.to_parquet(os.path.join(cache_dir, f"{ticker}.parquet"))
    return df


def _make_trades(**kwargs):
    """Build a minimal trades DataFrame with one row."""
    defaults = {
        "ticker": "AAPL",
        "entry_date": pd.Timestamp("2024-12-02"),
        "exit_date": pd.Timestamp("2024-12-10"),
        "entry_price": 100.0,
        "pnl": 5.0,
    }
    defaults.update(kwargs)
    return pd.DataFrame([defaults])


def test_gap_computed_from_prev_close(tmp_path):
    """gap = (entry_price - prev_close) / prev_close: $100 entry, $98 prev_close → ~2.04%."""
    cache_dir = str(tmp_path / "cache")
    prev_close = 98.0
    entry_price = 100.0
    # Write parquet with last row on 2024-12-01 (day before entry 2024-12-02)
    _write_parquet(cache_dir, "AAPL", prev_close, last_date=datetime.date(2024, 12, 1))
    trades = _make_trades(entry_price=entry_price, pnl=5.0)
    df = compute_gaps(trades, cache_dir)
    assert len(df) == 1
    expected = (entry_price - prev_close) / prev_close
    assert abs(df["gap_pct"].iloc[0] - expected) < 1e-6


def test_negative_gap_identified(tmp_path):
    """Gap-down trade (entry < prev_close) gives negative gap_pct."""
    cache_dir = str(tmp_path / "cache")
    prev_close = 100.0
    entry_price = 97.0  # entry below prev close → gap down
    _write_parquet(cache_dir, "AAPL", prev_close, last_date=datetime.date(2024, 12, 1))
    trades = _make_trades(entry_price=entry_price, pnl=-3.0)
    df = compute_gaps(trades, cache_dir)
    assert len(df) == 1
    assert df["gap_pct"].iloc[0] < 0


def test_missing_ticker_gracefully_skipped(tmp_path, capsys):
    """Ticker not in CACHE_DIR → no crash, row skipped."""
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir)
    trades = _make_trades(ticker="NOTEXIST", pnl=1.0)
    df = compute_gaps(trades, cache_dir)
    assert df.empty
    captured = capsys.readouterr()
    assert "WARNING" in captured.out or "not in cache" in captured.out.lower()


def test_winner_loser_split_correct(tmp_path):
    """Winners (pnl > 0) vs losers (pnl <= 0) counted correctly."""
    cache_dir = str(tmp_path / "cache")
    _write_parquet(cache_dir, "AAPL", 98.0, last_date=datetime.date(2024, 12, 1))
    _write_parquet(cache_dir, "MSFT", 198.0, last_date=datetime.date(2024, 12, 1))
    trades = pd.DataFrame([
        {"ticker": "AAPL", "entry_date": pd.Timestamp("2024-12-02"), "exit_date": pd.Timestamp("2024-12-05"), "entry_price": 100.0, "pnl": 5.0},
        {"ticker": "MSFT", "entry_date": pd.Timestamp("2024-12-02"), "exit_date": pd.Timestamp("2024-12-05"), "entry_price": 200.0, "pnl": -3.0},
    ])
    df = compute_gaps(trades, cache_dir)
    assert int(df["winner"].sum()) == 1
    assert int((~df["winner"]).sum()) == 1


def test_threshold_analysis_output(tmp_path, capsys):
    """print_analysis output contains threshold recommendation line."""
    cache_dir = str(tmp_path / "cache")
    _write_parquet(cache_dir, "AAPL", 98.0, last_date=datetime.date(2024, 12, 1))
    trades = _make_trades(entry_price=100.0, pnl=5.0)
    df = compute_gaps(trades, cache_dir)
    print_analysis(df)
    captured = capsys.readouterr()
    assert "Recommended" in captured.out or "GAP_THRESHOLD" in captured.out
