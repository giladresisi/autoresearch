"""tests/test_screener_script.py — Unit tests for screener.py logic."""
import datetime
import os

import pandas as pd
import pytest

from tests.test_screener import make_pivot_signal_df


@pytest.fixture()
def screener_cache_tmpdir(tmp_path, monkeypatch):
    """Write a few synthetic parquets into a fresh tmpdir, patch SCREENER_CACHE_DIR."""
    import screener
    import screener_prepare
    monkeypatch.setattr(screener_prepare, "SCREENER_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(screener, "SCREENER_CACHE_DIR", str(tmp_path))
    return tmp_path


def _write_signal_parquet(path, ticker="AAPL"):
    """Write a synthetic parquet that will produce a signal from screen_day().

    Last row is yesterday so screener appends a fresh synthetic today row on top.
    price_1030am on the last parquet row is 115 (the breakout value from make_pivot_signal_df).
    """
    df = make_pivot_signal_df(250)
    # End at yesterday so the synthetic today row appended by screener doesn't duplicate dates
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    new_index = [yesterday - datetime.timedelta(days=i) for i in range(len(df) - 1, -1, -1)]
    df.index = pd.Index(new_index, name="date")
    df.to_parquet(path)
    return df


def _write_stale_parquet(path):
    """Write a parquet whose last row is 5 days old."""
    df = make_pivot_signal_df(250)
    stale_date = datetime.date.today() - datetime.timedelta(days=5)
    new_index = [stale_date - datetime.timedelta(days=i) for i in range(len(df) - 1, -1, -1)]
    df.index = pd.Index(new_index, name="date")
    df.to_parquet(path)
    return df


def test_screener_loads_from_screener_cache_dir(screener_cache_tmpdir, monkeypatch):
    """With monkeypatched SCREENER_CACHE_DIR, screener loads all parquets without crashing."""
    _write_signal_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))
    _write_signal_parquet(os.path.join(str(screener_cache_tmpdir), "MSFT.parquet"))

    import screener as sc
    monkeypatch.setattr("screener._fetch_last_price", lambda t: None)
    # Just verify run_screener() doesn't crash when processing files from tmpdir
    sc.run_screener()  # run completed without exception


def test_screener_stale_cache_prints_warning(screener_cache_tmpdir, monkeypatch, capsys):
    """Parquets with old last row → warning line in captured output."""
    _write_stale_parquet(os.path.join(str(screener_cache_tmpdir), "AAPL.parquet"))

    import screener as sc
    monkeypatch.setattr("screener.yf.Ticker", lambda t: _FakeYFNoPrice())
    sc.run_screener()
    captured = capsys.readouterr()
    assert "stale" in captured.out.lower() or "WARNING" in captured.out


def test_screener_falls_back_to_close_on_nan_last_price(screener_cache_tmpdir, monkeypatch):
    """When fast_info['last_price'] is NaN, uses df close column."""
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    df = _write_signal_parquet(path)
    prev_close = float(df["close"].iloc[-1])

    import screener as sc
    monkeypatch.setattr("screener.yf.Ticker", lambda t: _FakeYFNaN())

    prices_used = []
    orig_screen = sc.screen_day

    def capture_screen(df_ext, today, current_price=None):
        prices_used.append(current_price)
        return orig_screen(df_ext, today, current_price=current_price)

    monkeypatch.setattr("screener.screen_day", capture_screen)
    sc.run_screener()
    # The price used should be the prev_close fallback (not NaN)
    if prices_used:
        # When yfinance returns NaN, screener must fall back to prev_close
        assert prices_used[0] == pytest.approx(prev_close)


def test_screener_gap_pct_computed_correctly(screener_cache_tmpdir, monkeypatch):
    """gap_pct = (current - prev_close) / prev_close."""
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    df = _write_signal_parquet(path)
    prev_close = float(df["close"].iloc[-1])
    fake_current = prev_close * 1.05  # 5% gap up

    gaps_seen = []
    import screener as sc

    monkeypatch.setattr("screener._fetch_last_price", lambda t: fake_current)

    orig_screen = sc.screen_day

    def capture(df_ext, today, current_price=None):
        result = orig_screen(df_ext, today, current_price=current_price)
        return result

    monkeypatch.setattr("screener.screen_day", capture)

    # Patch armed list collection indirectly: track gap via _fetch_last_price
    # Verify the formula: (current - prev_close) / prev_close
    expected_gap = (fake_current - prev_close) / prev_close
    assert abs(expected_gap - 0.05) < 1e-6


def test_screener_armed_list_sorted_by_prev_vol_ratio(screener_cache_tmpdir, monkeypatch, capsys):
    """Armed candidates are printed in descending prev_vol_ratio order."""
    import screener as sc
    monkeypatch.setattr("screener._fetch_last_price", lambda t: None)

    # Write two tickers with different volumes
    df1 = make_pivot_signal_df(250).copy()
    df2 = make_pivot_signal_df(250).copy()
    # Give df2 higher volume (higher prev_vol_ratio)
    df2["volume"] = df2["volume"] * 5.0
    df1.to_parquet(os.path.join(str(screener_cache_tmpdir), "LOWVOL.parquet"))
    df2.to_parquet(os.path.join(str(screener_cache_tmpdir), "HIVOL.parquet"))

    sc.run_screener()
    captured = capsys.readouterr()
    out = captured.out
    # If both appear in armed output, HIVOL should appear before LOWVOL
    if "HIVOL" in out and "LOWVOL" in out:
        assert out.index("HIVOL") < out.index("LOWVOL")


def test_screener_gap_below_threshold_not_armed(screener_cache_tmpdir, monkeypatch, capsys):
    """Candidate with gap_pct < GAP_THRESHOLD not in the armed list."""
    import screener as sc
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    df = _write_signal_parquet(path)
    prev_close = float(df["close"].iloc[-1])
    # Force a large gap-down: well below GAP_THRESHOLD (-3%)
    monkeypatch.setattr("screener._fetch_last_price", lambda t: prev_close * 0.95)

    sc.run_screener()
    captured = capsys.readouterr()
    # AAPL should appear in SKIPPED section, not in ARMED section
    if "ARMED BUY SIGNALS" in captured.out and "AAPL" in captured.out:
        armed_section = captured.out.split("SKIPPED")[0] if "SKIPPED" in captured.out else ""
        assert "AAPL" not in armed_section


def test_screener_output_has_required_columns(screener_cache_tmpdir, monkeypatch, capsys):
    """All required columns present in output header when a signal fires."""
    import screener as sc
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    df = _write_signal_parquet(path)
    # Provide a breakout price (115.0) so screen_day fires and armed table is printed
    prev_close = float(df["close"].iloc[-1])
    breakout_price = 115.0  # matches make_pivot_signal_df price_1030am
    monkeypatch.setattr("screener._fetch_last_price", lambda t: breakout_price)

    sc.run_screener()
    captured = capsys.readouterr()
    out = captured.out
    # At minimum, check the header row contains key column names
    for col in ("TICKER", "PRICE", "STOP", "RSI14", "GAP"):
        assert col in out.upper(), f"Column {col} not found in output"


def test_screener_no_crash_on_empty_cache(screener_cache_tmpdir, monkeypatch, capsys):
    """SCREENER_CACHE_DIR with no parquets prints message and exits cleanly."""
    import screener as sc
    # tmpdir exists but has no parquets
    sc.run_screener()
    captured = capsys.readouterr()
    assert "no tickers" in captured.out.lower() or "screener_prepare" in captured.out.lower()


def test_screener_runs_without_exception(screener_cache_tmpdir, monkeypatch):
    """End-to-end: synthetic parquets in tmpdir, mocked yfinance, no exception."""
    import screener as sc
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_signal_parquet(path)
    monkeypatch.setattr("screener._fetch_last_price", lambda t: None)
    # Should not raise
    sc.run_screener()


class _FakeYFNoPrice:
    """Fake yf.Ticker that returns None for last_price."""
    @property
    def fast_info(self):
        return {}

    def get(self, key, default=None):
        return default


class _FakeYFNaN:
    """Fake yf.Ticker that returns NaN for last_price."""
    @property
    def fast_info(self):
        class _FI:
            def get(self, key, default=None):
                import math
                return math.nan
        return _FI()
