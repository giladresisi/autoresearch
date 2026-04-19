# plan1_experiment_runner.py
# Runs a single fast 1-fold backtest with an optional flag override.
# Usage: uv run python plan1_experiment_runner.py [FLAG=VALUE ...]
# Example: uv run python plan1_experiment_runner.py MIDNIGHT_OPEN_AS_TP=True
#
# Outputs key metrics in "KEY: VALUE" format for easy parsing.
import sys

# ── Patch fast mode before importing backtest_smt ─────────────────────────────
import backtest_smt
import strategy_smt

backtest_smt.WALK_FORWARD_WINDOWS = 1
backtest_smt.FOLD_TEST_DAYS       = 60

# ── Apply flag overrides from command-line args ────────────────────────────────
BOOL_FLAGS = {
    "MIDNIGHT_OPEN_AS_TP", "STRUCTURAL_STOP_MODE", "INVALIDATION_MSS_EXIT",
    "INVALIDATION_CISD_EXIT", "INVALIDATION_SMT_EXIT", "OVERNIGHT_SWEEP_REQUIRED",
    "OVERNIGHT_RANGE_AS_TP", "SILVER_BULLET_WINDOW_ONLY", "HIDDEN_SMT_ENABLED",
}
FLOAT_FLAGS = {"STRUCTURAL_STOP_BUFFER_PTS"}

for arg in sys.argv[1:]:
    if "=" not in arg:
        continue
    key, val = arg.split("=", 1)
    key = key.strip()
    if key in BOOL_FLAGS:
        setattr(strategy_smt, key, val.strip().lower() in ("true", "1", "yes"))
    elif key in FLOAT_FLAGS:
        setattr(strategy_smt, key, float(val.strip()))

# ── Print active non-default flags ────────────────────────────────────────────
active = []
for f in sorted(BOOL_FLAGS | FLOAT_FLAGS):
    v = getattr(strategy_smt, f, None)
    default = False if f in BOOL_FLAGS else 2.0
    if v != default:
        active.append(f"{f}={v}")
print(f"active_flags: {', '.join(active) if active else 'none (baseline)'}")

# ── Load data and run 1-fold backtest ─────────────────────────────────────────
from backtest_smt import run_backtest, _compute_fold_params, print_results, load_futures_data
import datetime, pandas as pd
from pandas.tseries.offsets import BDay

dfs = load_futures_data()
mnq_df = dfs["MNQ"]
mes_df = dfs["MES"]

train_end_ts = pd.Timestamp(backtest_smt.TRAIN_END)
n_folds, fold_days = _compute_fold_params(
    backtest_smt.BACKTEST_START, backtest_smt.TRAIN_END,
    backtest_smt.WALK_FORWARD_WINDOWS, backtest_smt.FOLD_TEST_DAYS
)

# Single fold (i=0 with n_folds=1)
fold_test_end_ts   = train_end_ts
fold_test_start_ts = fold_test_end_ts - BDay(fold_days)

test_stats  = run_backtest(mnq_df, mes_df,
                           start=str(fold_test_start_ts.date()),
                           end=str(fold_test_end_ts.date()))
train_stats = run_backtest(mnq_df, mes_df,
                           start=backtest_smt.BACKTEST_START,
                           end=str(fold_test_start_ts.date()))

print(f"fold_test_start: {fold_test_start_ts.date()}")
print(f"fold_test_end:   {fold_test_end_ts.date()}")
print("--- TRAIN ---")
print_results(train_stats, prefix="train_")
print("--- TEST ---")
print_results(test_stats, prefix="test_")

# ── Summary line ──────────────────────────────────────────────────────────────
print("---")
print(f"mean_test_pnl:   {test_stats['total_pnl']:.2f}")
print(f"total_trades:    {test_stats['total_trades']}")
print(f"win_rate:        {test_stats['win_rate']:.4f}")
print(f"avg_rr:          {test_stats['avg_rr']:.4f}")
print(f"avg_pnl_trade:   {test_stats['avg_pnl_per_trade']:.2f}")
print(f"max_drawdown:    {test_stats['max_drawdown']:.2f}")
exits = test_stats.get("exit_type_breakdown", {})
for k, v in sorted(exits.items()):
    print(f"exit_{k}: {v}")
