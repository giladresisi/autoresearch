# plan2_experiment_runner.py
# Runs a single fast 1-fold backtest with Plan 2 flag overrides.
#
# Plan 2 adds four opt-in features tested in four independent runs (A-D):
#   Run A — SMT-optional displacement entries
#   Run B — Partial exit at first liquidity draw
#   Run C — Two-layer position model (FVG-based Layer B) — 3 flags must be set together
#   Run D — SMT fill divergence entries
#
# Usage: uv run python plan2_experiment_runner.py [FLAG=VALUE ...]
#
# Examples:
#   uv run python plan2_experiment_runner.py                           # baseline (all Plan 2 flags off)
#   uv run python plan2_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0
#   uv run python plan2_experiment_runner.py PARTIAL_EXIT_ENABLED=True PARTIAL_EXIT_FRACTION=0.5
#   uv run python plan2_experiment_runner.py FVG_ENABLED=True TWO_LAYER_POSITION=True FVG_LAYER_B_TRIGGER=True LAYER_A_FRACTION=0.5
#   uv run python plan2_experiment_runner.py SMT_FILL_ENABLED=True
#
# Output format: "KEY: VALUE" lines for easy parsing/copying into the experiment log.
# Metrics printed:
#   active_flags — which Plan 2 flags are non-default
#   final_trades — count of FINAL exits only (excludes partial_exit records)
#   partial_trades — count of partial_exit records (0 when PARTIAL_EXIT_ENABLED=False)
#   total_trades — all records including partials (equals final_trades when disabled)
#   mean_test_pnl — sum of ALL pnl records (partials + finals); true net P&L
#   final_pnl — sum of pnl for final exits only; compare to baseline for apples-to-apples
#   win_rate — computed on FINAL exits only
#   avg_rr — computed on FINAL exits only
#   avg_pnl_trade — mean pnl over FINAL exits only
#   max_drawdown, sharpe — from stats dict (equity curve, all exits)
#   exit_* — breakdown by exit_type (session_close, exit_tp, exit_stop, partial_exit, etc.)
#
# IMPORTANT — interpreting partial exit metrics:
#   When PARTIAL_EXIT_ENABLED=True, each trade produces 2 records: a "partial_exit" record
#   and a final record (exit_tp / exit_stop / session_close). The raw total_trades and
#   total_pnl from run_backtest() include both. Use final_pnl and final_trades for
#   apples-to-apples comparison with the baseline (which has no partial records).
#
# IMPORTANT — interpreting two-layer metrics (Run C):
#   When TWO_LAYER_POSITION=True, Layer A enters at LAYER_A_FRACTION of sized contracts.
#   Layer B enters only when price retraces into the FVG zone (requires FVG_LAYER_B_TRIGGER=True).
#   If Layer B never triggers (no FVG retracements in the test window), the run degrades to a
#   reduced-size single-layer strategy — this is a valid result. Look at layer_b_entered in
#   the trade records to count how often Layer B actually fired.
#
# ── Patch fast mode before importing ─────────────────────────────────────────
import backtest_smt
import strategy_smt

backtest_smt.WALK_FORWARD_WINDOWS = 1
backtest_smt.FOLD_TEST_DAYS       = 60

# ── Flag registry ─────────────────────────────────────────────────────────────
# All Plan 1 flags (kept so this runner can also reproduce Plan 1 baselines).
PLAN1_BOOL_FLAGS = {
    "MIDNIGHT_OPEN_AS_TP", "STRUCTURAL_STOP_MODE", "INVALIDATION_MSS_EXIT",
    "INVALIDATION_CISD_EXIT", "INVALIDATION_SMT_EXIT", "OVERNIGHT_SWEEP_REQUIRED",
    "OVERNIGHT_RANGE_AS_TP", "SILVER_BULLET_WINDOW_ONLY", "HIDDEN_SMT_ENABLED",
}
PLAN1_FLOAT_FLAGS = {"STRUCTURAL_STOP_BUFFER_PTS"}

# Plan 2 flags.
PLAN2_BOOL_FLAGS = {
    "TWO_LAYER_POSITION", "FVG_ENABLED", "FVG_LAYER_B_TRIGGER",
    "SMT_OPTIONAL", "PARTIAL_EXIT_ENABLED", "SMT_FILL_ENABLED",
}
PLAN2_FLOAT_FLAGS = {
    "LAYER_A_FRACTION", "FVG_MIN_SIZE_PTS", "MIN_DISPLACEMENT_PTS",
    "PARTIAL_EXIT_FRACTION",
}

PLAN2_DEFAULTS: dict = {
    "TWO_LAYER_POSITION":   False,
    "FVG_ENABLED":          False,
    "FVG_LAYER_B_TRIGGER":  False,
    "LAYER_A_FRACTION":     0.5,
    "FVG_MIN_SIZE_PTS":     2.0,
    "SMT_OPTIONAL":         False,
    "MIN_DISPLACEMENT_PTS": 10.0,
    "PARTIAL_EXIT_ENABLED": False,
    "PARTIAL_EXIT_FRACTION":0.5,
    "SMT_FILL_ENABLED":     False,
}

ALL_BOOL_FLAGS  = PLAN1_BOOL_FLAGS  | PLAN2_BOOL_FLAGS
ALL_FLOAT_FLAGS = PLAN1_FLOAT_FLAGS | PLAN2_FLOAT_FLAGS

import sys

for arg in sys.argv[1:]:
    if "=" not in arg:
        continue
    key, val = arg.split("=", 1)
    key = key.strip(); val = val.strip()
    if key in ALL_BOOL_FLAGS:
        parsed = val.lower() in ("true", "1", "yes")
        setattr(strategy_smt, key, parsed)
        # Mirror Plan 2 constants into backtest_smt (it imports them at load time as module attrs)
        if hasattr(backtest_smt, key):
            setattr(backtest_smt, key, parsed)
    elif key in ALL_FLOAT_FLAGS:
        parsed_f = float(val)
        setattr(strategy_smt, key, parsed_f)
        if hasattr(backtest_smt, key):
            setattr(backtest_smt, key, parsed_f)

# ── Print active non-default Plan 2 flags ────────────────────────────────────
active = []
for f in sorted(PLAN2_BOOL_FLAGS | PLAN2_FLOAT_FLAGS):
    v = getattr(strategy_smt, f, None)
    default = PLAN2_DEFAULTS.get(f)
    if v != default:
        active.append(f"{f}={v}")
print(f"active_flags: {', '.join(active) if active else 'none (baseline)'}")

# ── Load data and run 1-fold backtest ─────────────────────────────────────────
from backtest_smt import run_backtest, _compute_fold_params, print_results, load_futures_data
import pandas as pd
from pandas.tseries.offsets import BDay

dfs = load_futures_data()
mnq_df = dfs["MNQ"]
mes_df = dfs["MES"]

train_end_ts = pd.Timestamp(backtest_smt.TRAIN_END)
n_folds, fold_days = _compute_fold_params(
    backtest_smt.BACKTEST_START, backtest_smt.TRAIN_END,
    backtest_smt.WALK_FORWARD_WINDOWS, backtest_smt.FOLD_TEST_DAYS,
)

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

# ── Plan 2 — split partial vs final metrics ───────────────────────────────────
all_trades   = test_stats.get("trade_records", [])
final_trades = [t for t in all_trades if t.get("exit_type") != "partial_exit"]
partial_records = [t for t in all_trades if t.get("exit_type") == "partial_exit"]

final_pnl      = sum(t["pnl"] for t in final_trades)
final_wins     = [t for t in final_trades if t["pnl"] > 0]
final_losses   = [t for t in final_trades if t["pnl"] <= 0]
final_wr       = len(final_wins) / len(final_trades) if final_trades else 0.0
final_avg_pnl  = final_pnl / len(final_trades) if final_trades else 0.0
avg_win        = sum(t["pnl"] for t in final_wins) / len(final_wins) if final_wins else 0.0
avg_loss       = sum(t["pnl"] for t in final_losses) / len(final_losses) if final_losses else 0.0
final_avg_rr   = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0

# Layer B stats (only meaningful for Run C)
layer_b_count  = sum(1 for t in all_trades if t.get("layer_b_entered"))
displacement_count = sum(1 for t in all_trades if t.get("smt_type") == "displacement")
fill_count         = sum(1 for t in all_trades if t.get("smt_type") == "fill")

print("---")
print(f"mean_test_pnl:     {test_stats['total_pnl']:.2f}")
print(f"total_trades:      {test_stats['total_trades']}")
print(f"partial_trades:    {len(partial_records)}")
print(f"final_trades:      {len(final_trades)}")
print(f"final_pnl:         {final_pnl:.2f}")
print(f"win_rate:          {final_wr:.4f}")
print(f"avg_rr:            {final_avg_rr:.4f}")
print(f"avg_pnl_trade:     {final_avg_pnl:.2f}")
print(f"max_drawdown:      {test_stats['max_drawdown']:.2f}")
print(f"sharpe:            {test_stats['sharpe']:.4f}")
print(f"layer_b_triggers:  {layer_b_count}")
print(f"displacement_entries: {displacement_count}")
print(f"fill_entries:      {fill_count}")
exits = test_stats.get("exit_type_breakdown", {})
for k, v in sorted(exits.items()):
    print(f"exit_{k}: {v}")
