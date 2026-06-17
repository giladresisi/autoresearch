"""Unit tests for the regression ATH seed helper (O6 Part B).

`backtest_smt._ath_as_of` seeds a regression run's all-time-high from the FULL pre-session
history, so an ATH older than the 1s-mode 60-day liquidity window is not missed (the GIL-23
windowing flaw that would misfire rule2b's recovery guard).
"""
import pandas as pd

from backtest_smt import _ath_as_of


def _frame(highs, start="2026-01-01"):
    idx = pd.date_range(start, periods=len(highs), freq="D", tz="America/New_York")
    return pd.DataFrame({"High": [float(h) for h in highs]}, index=idx)


def test_ath_as_of_uses_full_history_not_60day_window():
    """The all-time high sits 99 days before the open — far outside any 60-day window — and
    must still be returned (proving full-history lookback, not the windowed max)."""
    highs = [20000.0] * 100
    highs[0] = 31000.0  # the true ATH, 99 days before the end
    df = _frame(highs)
    assert _ath_as_of(df, 100) == 31000.0


def test_ath_as_of_excludes_bars_at_and_after_end_pos():
    """Only history strictly before end_pos counts (the open bar / future is excluded)."""
    highs = [25000.0, 25500.0, 99999.0, 26000.0]  # the 99999 is at index 2
    df = _frame(highs)
    assert _ath_as_of(df, 2) == 25500.0  # bars [0,1] only — the 99999 at idx 2 is excluded


def test_ath_as_of_no_history_returns_zero():
    """First date (no prior history) or an empty slice -> 0.0, never raises."""
    df = _frame([1.0, 2.0])
    assert _ath_as_of(df, 0) == 0.0
    assert _ath_as_of(df.iloc[:0], 5) == 0.0


def test_ath_as_of_all_nan_returns_zero():
    """A NaN-only High column must not propagate NaN into the seed."""
    df = pd.DataFrame(
        {"High": [float("nan"), float("nan")]},
        index=pd.date_range("2026-01-01", periods=2, freq="D", tz="America/New_York"),
    )
    assert _ath_as_of(df, 2) == 0.0
