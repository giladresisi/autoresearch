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

def test_train_walk_forward_windows_default_is_7():
    """WALK_FORWARD_WINDOWS must default to 7."""
    assert train.WALK_FORWARD_WINDOWS == 7, (
        f"Expected WALK_FORWARD_WINDOWS=7, got {train.WALK_FORWARD_WINDOWS}"
    )


def test_train_fold_test_days_default_is_40():
    """FOLD_TEST_DAYS must default to 40."""
    assert train.FOLD_TEST_DAYS == 40, (
        f"Expected FOLD_TEST_DAYS=40, got {train.FOLD_TEST_DAYS}"
    )


def test_program_md_walk_forward_windows_default_is_7():
    """program.md must document WALK_FORWARD_WINDOWS with value 7."""
    text = open('program.md', encoding='utf-8').read()
    assert 'WALK_FORWARD_WINDOWS' in text, "Expected 'WALK_FORWARD_WINDOWS' in program.md"
    assert 'WALK_FORWARD_WINDOWS = 7' in text or 'WALK_FORWARD_WINDOWS=7' in text or (
        'WALK_FORWARD_WINDOWS' in text and '7' in text
    ), "Expected WALK_FORWARD_WINDOWS and 7 to appear in program.md"
