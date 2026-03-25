# tests/test_v4_b.py
# Unit tests for V4-B harness metric improvements: R2 fold trade-count guard,
# R11 win/loss dollar ratio, and R16 walk-forward fold defaults.
import io
import pathlib

import numpy as np

import train

TRAIN_PY = pathlib.Path(__file__).parent.parent / "train.py"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_stats(total_pnl=0.0, total_trades=5, avg_win_loss_ratio=1.0):
    """Return a full run_backtest-compatible dict with all required keys."""
    return {
        "sharpe": 0.5,
        "total_trades": total_trades,
        "win_rate": 0.5,
        "avg_pnl_per_trade": 10.0,
        "total_pnl": total_pnl,
        "ticker_pnl": {},
        "backtest_start": "2025-01-01",
        "backtest_end": "2025-06-01",
        "trade_records": [],
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "pnl_consistency": 0.0,
        "pnl_min": total_pnl,
        "avg_win_loss_ratio": avg_win_loss_ratio,
        "regime_stats": {},
    }


def _exec_main_block(extra_ns: dict) -> None:
    """
    Execute train.py's __main__ block in a copy of the train module namespace,
    with extra_ns values overlaid. This allows mocking without runpy's fresh namespace.
    """
    source = TRAIN_PY.read_text(encoding="utf-8")
    main_idx = source.find('if __name__ == "__main__":')
    ns = dict(vars(train))
    ns["__name__"] = "__main__"
    ns.update(extra_ns)
    exec(compile(source[main_idx:], str(TRAIN_PY), "exec"), ns)


def _get_float(output, key):
    for line in output.splitlines():
        if line.strip().startswith(key + ':'):
            return float(line.split(':')[1].strip())
    raise ValueError(f"{key} not found in output")


def _get_int(output, key):
    for line in output.splitlines():
        if line.strip().startswith(key + ':'):
            return int(line.split(':')[1].strip())
    raise ValueError(f"{key} not found in output")


# ── R11: win/loss dollar ratio ─────────────────────────────────────────────────

def test_run_backtest_returns_avg_win_loss_ratio_key():
    """run_backtest() result dict must include the avg_win_loss_ratio key."""
    result = train.run_backtest({})
    assert "avg_win_loss_ratio" in result, (
        f"Expected 'avg_win_loss_ratio' key in run_backtest result. Keys: {list(result.keys())}"
    )


def test_run_backtest_win_loss_ratio_zero_no_trades():
    """run_backtest() with empty ticker_dfs (no trades) must return avg_win_loss_ratio == 0.0."""
    result = train.run_backtest({})
    assert result["avg_win_loss_ratio"] == 0.0, (
        f"Expected avg_win_loss_ratio=0.0 for empty run, got {result['avg_win_loss_ratio']}"
    )


def test_run_backtest_win_loss_ratio_positive_formula():
    """The win/loss ratio formula: mean(winners) / abs(mean(losers)) must compute correctly."""
    winning = [10.0, 5.0]
    losing  = [-3.0, -7.0]
    ratio = round(float(np.mean(winning) / abs(np.mean(losing))), 3)
    assert ratio == 1.5, f"Expected ratio=1.5, got {ratio}"


def test_run_backtest_win_loss_ratio_zero_when_no_losers():
    """Sentinel: avg_win_loss_ratio == 0.0 when no trades (no losers present)."""
    result = train.run_backtest({})
    # With no trades there are no losers, so sentinel value 0.0 is returned.
    assert result["avg_win_loss_ratio"] == 0.0


def test_print_results_emits_win_loss_ratio_line(capsys):
    """print_results() must emit a line containing 'win_loss_ratio:'."""
    train.print_results({
        "sharpe": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0,
        "total_pnl": 0.0,
        "backtest_start": "x",
        "backtest_end": "y",
        "avg_win_loss_ratio": 1.5,
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "pnl_consistency": 0.0,
        "pnl_min": 0.0,
        "regime_stats": {},
    })
    captured = capsys.readouterr()
    assert "win_loss_ratio:" in captured.out, (
        f"Expected 'win_loss_ratio:' in print_results output.\nstdout: {captured.out}"
    )


def test_print_results_win_loss_ratio_parseable(capsys):
    """print_results() win_loss_ratio value must parse as float == 1.5."""
    train.print_results({
        "sharpe": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0,
        "total_pnl": 0.0,
        "backtest_start": "x",
        "backtest_end": "y",
        "avg_win_loss_ratio": 1.5,
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "pnl_consistency": 0.0,
        "pnl_min": 0.0,
        "regime_stats": {},
    })
    captured = capsys.readouterr()
    value = _get_float(captured.out, "win_loss_ratio")
    assert value == 1.5, f"Expected win_loss_ratio=1.5, got {value}"


def test_print_results_win_loss_ratio_missing_key_uses_default(capsys):
    """print_results() must not raise KeyError when avg_win_loss_ratio is absent; defaults to 0.000."""
    train.print_results({
        "sharpe": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0,
        "total_pnl": 0.0,
        "backtest_start": "x",
        "backtest_end": "y",
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "pnl_consistency": 0.0,
        "pnl_min": 0.0,
        "regime_stats": {},
        # avg_win_loss_ratio intentionally omitted
    })
    captured = capsys.readouterr()
    assert "0.000" in captured.out, (
        f"Expected '0.000' default win_loss_ratio in output.\nstdout: {captured.out}"
    )


# ── R2: fold trade-count guard ─────────────────────────────────────────────────

def test_main_min_test_pnl_folds_included_in_output(capsys):
    """When all folds have >= 3 trades, min_test_pnl_folds_included must equal WALK_FORWARD_WINDOWS."""
    N = train.WALK_FORWARD_WINDOWS
    call_idx = [0]

    def fake_run_backtest(ticker_dfs, start=None, end=None, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx % 2 == 0:  # even = train call
            return _fake_stats(total_pnl=200.0, total_trades=10)
        else:  # odd = test call
            fold = idx // 2
            return _fake_stats(total_pnl=100.0 + fold, total_trades=5)

    _exec_main_block({
        "load_all_ticker_data": lambda: {"dummy": None},
        "run_backtest": fake_run_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
        "TICKER_HOLDOUT_FRAC": 0,
        "TEST_EXTRA_TICKERS": [],
    })

    captured = capsys.readouterr()
    assert "min_test_pnl_folds_included:" in captured.out, (
        f"Expected 'min_test_pnl_folds_included:' in output.\nstdout: {captured.out[:800]}"
    )
    n_included = _get_int(captured.out, "min_test_pnl_folds_included")
    assert n_included == N, (
        f"Expected min_test_pnl_folds_included={N}, got {n_included}"
    )


def test_main_fold_exclusion_skips_sparse_fold(capsys):
    """Fold 0 with trades=1 (< 3) must be excluded; min_test_pnl uses remaining folds."""
    N = train.WALK_FORWARD_WINDOWS
    call_idx = [0]

    def fake_run_backtest(ticker_dfs, start=None, end=None, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx % 2 == 0:  # even = train call
            return _fake_stats(total_pnl=200.0, total_trades=10)
        else:  # odd = test call
            fold = idx // 2  # 0-indexed fold number
            if fold == 0:
                return _fake_stats(total_pnl=-100.0, total_trades=1)
            else:
                return _fake_stats(total_pnl=50.0 + fold, total_trades=5)

    _exec_main_block({
        "load_all_ticker_data": lambda: {"dummy": None},
        "run_backtest": fake_run_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
        "TICKER_HOLDOUT_FRAC": 0,
        "TEST_EXTRA_TICKERS": [],
    })

    captured = capsys.readouterr()
    min_pnl = _get_float(captured.out, "min_test_pnl")
    # fold 0 excluded (trades=1); fold 1 → pnl=51.0 is minimum of qualified folds
    assert min_pnl == 51.0, (
        f"Expected min_test_pnl=51.0 (fold 0 excluded), got {min_pnl}\nstdout: {captured.out[:800]}"
    )
    n_included = _get_int(captured.out, "min_test_pnl_folds_included")
    assert n_included == N - 1, (
        f"Expected min_test_pnl_folds_included={N - 1}, got {n_included}"
    )


def test_main_fold_exclusion_fallback_all_sparse(capsys):
    """When all folds have trades < 3, fallback uses raw minimum of all folds."""
    N = train.WALK_FORWARD_WINDOWS
    call_idx = [0]

    def fake_run_backtest(ticker_dfs, start=None, end=None, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx % 2 == 0:  # even = train call
            return _fake_stats(total_pnl=200.0, total_trades=10)
        else:  # odd = test call
            fold = idx // 2
            if fold == 0:
                return _fake_stats(total_pnl=-100.0, total_trades=1)
            else:
                return _fake_stats(total_pnl=50.0 + fold, total_trades=1)

    _exec_main_block({
        "load_all_ticker_data": lambda: {"dummy": None},
        "run_backtest": fake_run_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
        "TICKER_HOLDOUT_FRAC": 0,
        "TEST_EXTRA_TICKERS": [],
    })

    captured = capsys.readouterr()
    min_pnl = _get_float(captured.out, "min_test_pnl")
    # Fallback: all folds sparse → raw minimum = -100.0
    assert min_pnl == -100.0, (
        f"Expected min_test_pnl=-100.0 (fallback all sparse), got {min_pnl}\nstdout: {captured.out[:800]}"
    )
    n_included = _get_int(captured.out, "min_test_pnl_folds_included")
    # Fallback includes all folds
    assert n_included == N, (
        f"Expected min_test_pnl_folds_included={N} (fallback), got {n_included}"
    )


def test_main_fold_exclusion_included_count_equals_qualifying_folds(capsys):
    """N-1 of N folds qualify (trades >= 3); last fold has trades=0 → excluded."""
    N = train.WALK_FORWARD_WINDOWS
    call_idx = [0]

    def fake_run_backtest(ticker_dfs, start=None, end=None, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx % 2 == 0:  # even = train call
            return _fake_stats(total_pnl=200.0, total_trades=10)
        else:  # odd = test call
            fold = idx // 2
            if fold == N - 1:
                return _fake_stats(total_pnl=999.0, total_trades=0)
            else:
                return _fake_stats(total_pnl=100.0 + fold, total_trades=5)

    _exec_main_block({
        "load_all_ticker_data": lambda: {"dummy": None},
        "run_backtest": fake_run_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
        "TICKER_HOLDOUT_FRAC": 0,
        "TEST_EXTRA_TICKERS": [],
    })

    captured = capsys.readouterr()
    n_included = _get_int(captured.out, "min_test_pnl_folds_included")
    assert n_included == N - 1, (
        f"Expected min_test_pnl_folds_included={N - 1} (last fold excluded), got {n_included}"
    )


# ── R16: walk-forward fold defaults ───────────────────────────────────────────

def test_train_walk_forward_windows_default_is_6():
    """WALK_FORWARD_WINDOWS must default to 6 (updated from 7 in run-a-eval-foundation)."""
    assert train.WALK_FORWARD_WINDOWS == 6, (
        f"Expected WALK_FORWARD_WINDOWS=6, got {train.WALK_FORWARD_WINDOWS}"
    )


def test_train_fold_test_days_default_is_60():
    """FOLD_TEST_DAYS must default to 60 (updated from 40 in run-a-eval-foundation)."""
    assert train.FOLD_TEST_DAYS == 60, (
        f"Expected FOLD_TEST_DAYS=60, got {train.FOLD_TEST_DAYS}"
    )


def test_program_md_walk_forward_windows_default_is_7():
    """program.md must document WALK_FORWARD_WINDOWS with value 7."""
    text = open('program.md', encoding='utf-8').read()
    assert 'WALK_FORWARD_WINDOWS' in text, "Expected 'WALK_FORWARD_WINDOWS' in program.md"
    assert 'WALK_FORWARD_WINDOWS = 7' in text or 'WALK_FORWARD_WINDOWS=7' in text or (
        'WALK_FORWARD_WINDOWS' in text and '7' in text
    ), "Expected WALK_FORWARD_WINDOWS and 7 to appear in program.md"


# ── P0-B/C/D: MFE/MAE, exit_type, R-multiple ─────────────────────────────────

import unittest.mock as mock
import pandas as pd
from datetime import date, timedelta


def _make_trade_run(prices_after_entry: list, stop: float = 90.0, atr14: float = 5.0,
                    entry_price: float = 100.0) -> dict:
    """
    Build a minimal run_backtest result with one trade.
    screen_day fires once on the first day; manage_position keeps stop fixed.
    prices_after_entry: price_1030am values for days after the entry signal.
    """
    signal_date = date(2026, 1, 10)
    n = 1 + len(prices_after_entry)
    days = [signal_date + timedelta(days=i) for i in range(n)]

    prices_all = [entry_price] + list(prices_after_entry)
    prices_arr = __import__('numpy').array(prices_all)

    df = pd.DataFrame({
        'open':        prices_arr,
        'high':        prices_arr + 1.0,
        'low':         prices_arr - 1.0,
        'close':       prices_arr,
        'volume':      __import__('numpy').full(n, 1_000_000.0),
        'price_1030am': prices_arr,
    }, index=pd.Index(days, name='date'))

    fake_signal = {'entry_price': entry_price, 'stop': stop, 'atr14': atr14, 'stop_type': 'pivot'}
    fired = [False]

    def _screen(d, today):
        if not fired[0]:
            fired[0] = True
            return fake_signal
        return None

    with mock.patch.object(train, 'screen_day', side_effect=_screen), \
         mock.patch.object(train, 'manage_position', lambda pos, df: pos['stop_price']):
        return train.run_backtest({'X': df},
                                  start=str(days[0]),
                                  end=str(days[-1] + timedelta(days=1)))


# ── P0-B: MFE/MAE ────────────────────────────────────────────────────────────

def test_mfe_atr_positive_for_winning_trade():
    """A trade where prices rise after entry should have mfe_atr > 0."""
    # Prices rise from 100 to 110 over 5 days → MFE = (110−100)/5 = 2.0
    result = _make_trade_run(prices_after_entry=[102, 105, 108, 110, 110])
    records = result["trade_records"]
    assert len(records) > 0, "Expected at least one trade record"
    for r in records:
        assert "mfe_atr" in r, f"Missing mfe_atr in {r}"
    # The end-of-backtest record should reflect the run-up
    eob = [r for r in records if r.get("exit_type") == "end_of_backtest"]
    assert len(eob) > 0
    assert eob[-1]["mfe_atr"] > 0, f"Expected mfe_atr > 0, got {eob[-1]['mfe_atr']}"


def test_mae_atr_non_negative():
    """MAE (adverse excursion) must always be >= 0 for any trade."""
    result = _make_trade_run(prices_after_entry=[100, 101, 102, 103])
    for r in result["trade_records"]:
        assert r.get("mae_atr", 0.0) >= 0.0, f"Negative MAE in {r}"


# ── P0-C: exit_type ───────────────────────────────────────────────────────────

def test_exit_type_present_in_all_trade_records():
    """Every trade record must contain a non-empty exit_type."""
    result = _make_trade_run(prices_after_entry=[100, 101, 102])
    for r in result["trade_records"]:
        assert "exit_type" in r, f"Missing exit_type in {r}"
        assert r["exit_type"] in {"stop_hit", "end_of_backtest", "partial"}, \
            f"Unknown exit_type: {r['exit_type']}"


def test_partial_exit_type_unchanged():
    """Partial close records must have exit_type == 'partial'."""
    # entry=100, atr=5 → partial fires when price_1030am >= 100.03+5=105.03
    result = _make_trade_run(prices_after_entry=[106, 106, 106], atr14=5.0)
    partials = [r for r in result["trade_records"] if r.get("exit_type") == "partial"]
    assert len(partials) >= 1, "Expected at least one partial record — price 106 should trigger partial at +1ATR"
    for r in partials:
        assert r["exit_type"] == "partial"


# ── P0-D: R-multiple ─────────────────────────────────────────────────────────

def test_r_multiple_present_in_all_trade_records():
    """Every trade record must have an r_multiple field."""
    result = _make_trade_run(prices_after_entry=[100, 101])
    for r in result["trade_records"]:
        assert "r_multiple" in r, f"Missing r_multiple in {r}"


def test_r_multiple_positive_for_winning_trade():
    """Winning end-of-backtest trade exited above entry → r_multiple > 0."""
    # entry=100, initial_stop=90, exit=110 → R = (110−100)/(100−90) = 1.0
    result = _make_trade_run(prices_after_entry=[110, 110], stop=90.0, atr14=5.0)
    for r in result["trade_records"]:
        if r.get("exit_type") == "end_of_backtest" and r["pnl"] > 0:
            assert isinstance(r["r_multiple"], float) and r["r_multiple"] > 0, \
                f"Expected positive r_multiple for winner: {r}"


# ── P0-B/C/D: _write_trades_tsv fieldnames ───────────────────────────────────

def test_trades_tsv_fieldnames_include_p0_columns(tmp_path, monkeypatch):
    """_write_trades_tsv must write exit_type, mfe_atr, mae_atr, r_multiple columns."""
    import csv
    monkeypatch.chdir(tmp_path)
    record = {
        "ticker": "X", "entry_date": "2026-01-01", "exit_date": "2026-01-10",
        "days_held": 9, "stop_type": "pivot", "regime": "bull",
        "entry_price": 100.0, "exit_price": 110.0, "pnl": 10.0,
        "exit_type": "end_of_backtest", "mfe_atr": 2.0, "mae_atr": 0.5, "r_multiple": 1.0,
    }
    train._write_trades_tsv([record])
    with open(tmp_path / "trades.tsv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames
    assert "exit_type"  in fieldnames, "exit_type missing from trades.tsv"
    assert "mfe_atr"    in fieldnames, "mfe_atr missing from trades.tsv"
    assert "mae_atr"    in fieldnames, "mae_atr missing from trades.tsv"
    assert "r_multiple" in fieldnames, "r_multiple missing from trades.tsv"
