"""tests/test_registry.py — Registry loading and METADATA validation.

Verifies that the strategies package imports cleanly, that REGISTRY is
populated with correctly structured modules, and that base_indicators
exposes the expected callable interface.
"""
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta


REQUIRED_METADATA_KEYS = {
    "name", "sector", "tickers", "train_start", "train_end",
    "test_start", "test_end", "source_branch", "source_commit",
    "train_pnl", "train_sharpe", "train_trades", "description",
}


# ── Registry structure ────────────────────────────────────────────────────────

def test_registry_is_dict():
    from strategies import REGISTRY
    assert isinstance(REGISTRY, dict)



def test_all_strategies_have_required_metadata():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert hasattr(module, "METADATA"), f"{name} missing METADATA"
        missing = REQUIRED_METADATA_KEYS - set(module.METADATA.keys())
        assert not missing, f"{name} METADATA missing keys: {missing}"


def test_all_strategies_have_screen_day():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert callable(getattr(module, "screen_day", None)), \
            f"{name} missing callable screen_day()"


def test_all_strategies_have_manage_position():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert callable(getattr(module, "manage_position", None)), \
            f"{name} missing callable manage_position()"



# ── base_indicators ───────────────────────────────────────────────────────────

def test_base_indicators_importable():
    from strategies import base_indicators
    assert callable(base_indicators.calc_rsi14)
    assert callable(base_indicators.calc_atr14)
    assert callable(base_indicators.calc_cci)
    assert callable(base_indicators.find_stop_price)
    assert callable(base_indicators.find_pivot_lows)
    assert callable(base_indicators.zone_touch_count)
    assert callable(base_indicators.is_stalling_at_ceiling)
    assert callable(base_indicators.nearest_resistance_atr)


def test_base_indicators_calc_rsi14_range():
    """RSI values (where not NaN) should be in [0, 100] and > 50 on a net-upward series."""
    from strategies.base_indicators import calc_rsi14
    # Use alternating up/down with net upward drift so losses > 0 (avoids all-NaN RSI)
    n = 40
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    # 3 up days for every 1 down day — net rising, but not monotone
    changes = [1.0 if i % 4 != 3 else -0.5 for i in range(n)]
    close = np.array([100.0])
    for c in changes[1:]:
        close = np.append(close, close[-1] + c)
    df = pd.DataFrame({
        "close":  close,
        "open":   close,
        "high":   close * 1.01,
        "low":    close * 0.99,
        "volume": np.full(n, 1e6),
    }, index=pd.Index(dates, name="date"))
    rsi = calc_rsi14(df)
    valid = rsi.dropna()
    assert len(valid) > 0, "RSI should have at least some non-NaN values after warmup"
    assert (valid >= 0).all() and (valid <= 100).all()
    # Net-upward series → final RSI should be bullish (> 50)
    assert float(valid.iloc[-1]) > 50


def test_base_indicators_calc_atr14_non_negative():
    """ATR should always be non-negative after warmup."""
    from strategies.base_indicators import calc_atr14
    n = 30
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    close = np.linspace(100, 110, n)
    df = pd.DataFrame({
        "close":  close,
        "open":   close,
        "high":   close * 1.01,
        "low":    close * 0.99,
        "volume": np.full(n, 1e6),
    }, index=pd.Index(dates, name="date"))
    atr = calc_atr14(df)
    valid = atr.dropna()
    assert (valid >= 0).all()
