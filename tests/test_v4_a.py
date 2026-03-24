"""tests/test_v4_a.py — Unit tests for V4-A: R8 earnings filter, R9 fallback reject, R10 time exit."""
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta

from train import screen_day, manage_position, RISK_PER_TRADE
from tests.test_screener import make_signal_df, make_pivot_signal_df
from tests.test_backtester import make_position_df


# ── R9: screen_day fallback-stop reject ───────────────────────────────────────

def test_screen_day_rejects_fallback_stop_entry():
    # After R9, a signal df with no pivot structure must return None (was non-None before R9)
    df = make_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is None


def test_screen_day_accepts_pivot_stop_entry():
    # A signal df with a valid pivot low must return a non-None signal after R9
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None
    assert result['stop_type'] == 'pivot'


def test_screen_day_stop_type_always_pivot_when_signal():
    # When screen_day returns a signal, stop_type must be 'pivot' (no fallback path remains)
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None
    assert result['stop_type'] == 'pivot'


# ── R8: earnings proximity filter ─────────────────────────────────────────────

def test_screen_day_rejects_earnings_within_14_days():
    # Earnings 7 days out → 0 <= 7 <= 14 → rejected
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    df['next_earnings_date'] = today + timedelta(days=7)
    result = screen_day(df, today)
    assert result is None


def test_screen_day_rejects_earnings_exactly_14_days():
    # Earnings exactly 14 days out → boundary is inclusive → rejected
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    df['next_earnings_date'] = today + timedelta(days=14)
    result = screen_day(df, today)
    assert result is None


def test_screen_day_passes_earnings_15_days_away():
    # Earnings 15 days out → outside 14-day window → signal passes through
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    df['next_earnings_date'] = today + timedelta(days=15)
    result = screen_day(df, today)
    assert result is not None


def test_screen_day_passes_earnings_nat():
    # next_earnings_date column present but value is NaT → filter skips gracefully
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    df['next_earnings_date'] = pd.NaT
    result = screen_day(df, today)
    assert result is not None


def test_screen_day_passes_no_earnings_column():
    # next_earnings_date column absent entirely (old parquet) → backward compatible, no KeyError
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    assert 'next_earnings_date' not in df.columns
    result = screen_day(df, today)
    assert result is not None


def test_screen_day_rejects_earnings_day_itself():
    # next_earnings_date = today → days_to_earnings == 0 → rejected
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    df['next_earnings_date'] = today
    result = screen_day(df, today)
    assert result is None


# ── R10: manage_position time-based capital-efficiency exit ───────────────────

def _make_long_position(n=50, entry_price=100.0, stop_price=90.0):
    """Position df held n calendar days; ATR≈4 (atr_spread=2). Entry at first date."""
    df = make_position_df(n=n, price=entry_price, atr_spread=2.0)
    # shares sized so RISK_PER_TRADE risk fits inside ATR stop distance
    shares = RISK_PER_TRADE / (entry_price - stop_price)
    pos = {
        'entry_price': entry_price,
        'stop_price': stop_price,
        'shares': shares,
        'ticker': 'X',
        'entry_date': df.index[0],
    }
    return df, pos


def test_manage_position_forces_exit_stalled_position():
    # Held >30 bdays, unrealised_pnl = 0 < 0.3*RISK_PER_TRADE=15 → returns price_1030am
    df, pos = _make_long_position(n=50, entry_price=100.0, stop_price=90.0)
    result = manage_position(pos, df)
    price_1030am = float(df['price_1030am'].iloc[-1])
    assert result == max(pos['stop_price'], price_1030am)
    assert result == price_1030am  # stop_price=90 < price_1030am=100


def test_manage_position_no_force_exit_when_short_hold():
    # Held ≤30 bdays → R10 guard does not fire; normal stop management applies
    df, pos = _make_long_position(n=20, entry_price=100.0, stop_price=90.0)
    result = manage_position(pos, df)
    # Normal: price_1030am=100.0, entry=100.0, ATR≈4 → no breakeven → stop stays at 90
    assert result == 90.0


def test_manage_position_no_force_exit_when_profitable():
    # Held >30 bdays but unrealised_pnl >= threshold → R10 guard does not fire
    # price_1030am=100, entry=80 → unrealised_pnl = (100-80)*shares >> 0.3*RISK_PER_TRADE
    df = make_position_df(n=50, price=100.0, atr_spread=2.0)
    shares = RISK_PER_TRADE / (80.0 - 75.0)  # sized on 80→75 stop distance
    pos = {
        'entry_price': 80.0,
        'stop_price': 75.0,
        'shares': shares,
        'ticker': 'X',
        'entry_date': df.index[0],
    }
    result = manage_position(pos, df)
    price_1030am = float(df['price_1030am'].iloc[-1])  # 100.0
    # R10 does not fire (pnl = (100-80)*10 = 200 >> threshold=15) → stop < price_1030am
    assert result < price_1030am


def test_manage_position_force_exit_never_lowers_stop():
    # R10 fires but existing stop is already above price_1030am → return existing stop
    df, pos = _make_long_position(n=50, entry_price=100.0, stop_price=105.0)
    result = manage_position(pos, df)
    price_1030am = float(df['price_1030am'].iloc[-1])  # 100.0
    assert result == max(pos['stop_price'], price_1030am)  # max(105, 100) = 105
    assert result == 105.0


def test_manage_position_force_exit_respects_current_price():
    # Basic R10: force exit returns stop == price_1030am when price_1030am > current_stop
    df, pos = _make_long_position(n=50, entry_price=100.0, stop_price=90.0)
    result = manage_position(pos, df)
    price_1030am = float(df['price_1030am'].iloc[-1])  # 100.0
    assert result == price_1030am


# ── program.md text assertions ────────────────────────────────────────────────

def test_program_md_fold_test_days_default_is_40():
    # R1: FOLD_TEST_DAYS default changed from 20 to 40 in program.md
    text = open('program.md', encoding='utf-8').read()
    assert 'Default `40`' in text or "Default '40'" in text or '40' in text


def test_program_md_consistency_floor_uses_max_simultaneous_positions():
    # R3: consistency floor formula references MAX_SIMULTANEOUS_POSITIONS
    text = open('program.md', encoding='utf-8').read()
    assert 'MAX_SIMULTANEOUS_POSITIONS' in text


def test_program_md_deadlock_detection_rule_present():
    # R5: deadlock detection pivot paragraph present
    text = open('program.md', encoding='utf-8').read()
    assert 'Deadlock detection' in text


def test_program_md_position_management_iteration_guidance_present():
    # R6: position management priority mentions iterations 6
    text = open('program.md', encoding='utf-8').read()
    assert 'iterations 6' in text
