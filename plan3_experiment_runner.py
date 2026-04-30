# plan3_experiment_runner.py
# Runs a single fast 1-fold backtest with Plan 3 flag overrides.
#
# Plan 3 adds hypothesis alignment infrastructure tested in four runs (A-D):
#   Run A — Hypothesis filter (HYPOTHESIS_FILTER)
#   Run B — Displacement re-test with correct stop + score gate
#   Run C — Partial exit level tuning (PARTIAL_EXIT_LEVEL_RATIO)
#   Run D — Layer B hypothesis gate (FVG_LAYER_B_REQUIRES_HYPOTHESIS)
#
# Usage: uv run python plan3_experiment_runner.py [FLAG=VALUE ...]
#
# Examples:
#   uv run python plan3_experiment_runner.py                                         # baseline
#   uv run python plan3_experiment_runner.py HYPOTHESIS_FILTER=True
#   uv run python plan3_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0 DISPLACEMENT_STOP_MODE=True
#   uv run python plan3_experiment_runner.py PARTIAL_EXIT_LEVEL_RATIO=0.33
#   uv run python plan3_experiment_runner.py TWO_LAYER_POSITION=True FVG_ENABLED=True FVG_LAYER_B_TRIGGER=True FVG_LAYER_B_REQUIRES_HYPOTHESIS=True MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2
#
# IMPORTANT — HYPOTHESIS_FILTER lives in backtest_smt, not strategy_smt.
#   All other Plan 3 flags live in strategy_smt (same as Plan 2 flags).
#
# Inherits all Plan 1 + Plan 2 flags so this runner can reproduce any prior baseline.
#
# ── Patch fast mode before importing ─────────────────────────────────────────
import backtest_smt
import strategy_smt

backtest_smt.WALK_FORWARD_WINDOWS = 1
backtest_smt.FOLD_TEST_DAYS       = 60

# ── Flag registry ─────────────────────────────────────────────────────────────
PLAN1_BOOL_FLAGS = {
    "MIDNIGHT_OPEN_AS_TP", "STRUCTURAL_STOP_MODE", "INVALIDATION_MSS_EXIT",
    "INVALIDATION_CISD_EXIT", "INVALIDATION_SMT_EXIT", "OVERNIGHT_SWEEP_REQUIRED",
    "OVERNIGHT_RANGE_AS_TP", "SILVER_BULLET_WINDOW_ONLY", "HIDDEN_SMT_ENABLED",
}
PLAN1_FLOAT_FLAGS = {"STRUCTURAL_STOP_BUFFER_PTS"}

PLAN2_BOOL_FLAGS = {
    "TWO_LAYER_POSITION", "FVG_ENABLED", "FVG_LAYER_B_TRIGGER",
    "SMT_OPTIONAL", "PARTIAL_EXIT_ENABLED", "SMT_FILL_ENABLED",
}
PLAN2_FLOAT_FLAGS = {
    "LAYER_A_FRACTION", "FVG_MIN_SIZE_PTS", "MIN_DISPLACEMENT_PTS",
    "PARTIAL_EXIT_FRACTION",
}

# Plan 3 flags — HYPOTHESIS_FILTER mirrors into backtest_smt (not strategy_smt).
PLAN3_BOOL_FLAGS = {
    "HYPOTHESIS_FILTER",           # backtest_smt only
    "DISPLACEMENT_STOP_MODE",      # strategy_smt
    "FVG_LAYER_B_REQUIRES_HYPOTHESIS",  # strategy_smt
}
PLAN3_INT_FLAGS  = {"MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT"}  # strategy_smt
PLAN3_FLOAT_FLAGS = {"PARTIAL_EXIT_LEVEL_RATIO"}               # strategy_smt

PLAN3_DEFAULTS: dict = {
    "HYPOTHESIS_FILTER":                   False,
    "DISPLACEMENT_STOP_MODE":              False,
    "FVG_LAYER_B_REQUIRES_HYPOTHESIS":     False,
    "MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT": 0,
    "PARTIAL_EXIT_LEVEL_RATIO":            0.5,
}

ALL_BOOL_FLAGS  = PLAN1_BOOL_FLAGS  | PLAN2_BOOL_FLAGS  | PLAN3_BOOL_FLAGS
ALL_FLOAT_FLAGS = PLAN1_FLOAT_FLAGS | PLAN2_FLOAT_FLAGS | PLAN3_FLOAT_FLAGS
ALL_INT_FLAGS   = PLAN3_INT_FLAGS

import sys

for arg in sys.argv[1:]:
    if "=" not in arg:
        continue
    key, val = arg.split("=", 1)
    key = key.strip(); val = val.strip()
    if key in ALL_BOOL_FLAGS:
        parsed = val.lower() in ("true", "1", "yes")
        if key == "HYPOTHESIS_FILTER":
            # Lives in backtest_smt, not strategy_smt
            setattr(backtest_smt, key, parsed)
        else:
            setattr(strategy_smt, key, parsed)
            if hasattr(backtest_smt, key):
                setattr(backtest_smt, key, parsed)
    elif key in ALL_FLOAT_FLAGS:
        parsed_f = float(val)
        setattr(strategy_smt, key, parsed_f)
        if hasattr(backtest_smt, key):
            setattr(backtest_smt, key, parsed_f)
    elif key in ALL_INT_FLAGS:
        parsed_i = int(val)
        setattr(strategy_smt, key, parsed_i)
        if hasattr(backtest_smt, key):
            setattr(backtest_smt, key, parsed_i)

# ── Print active non-default Plan 3 flags ────────────────────────────────────
active = []
for f in sorted(PLAN3_BOOL_FLAGS | PLAN3_FLOAT_FLAGS | PLAN3_INT_FLAGS):
    if f == "HYPOTHESIS_FILTER":
        v = getattr(backtest_smt, f, None)
    else:
        v = getattr(strategy_smt, f, None)
    default = PLAN3_DEFAULTS.get(f)
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

# ── Split partial vs final metrics (same as Plan 2 runner) ────────────────────
all_trades      = test_stats.get("trade_records", [])
final_trades    = [t for t in all_trades if t.get("exit_type") != "partial_exit"]
partial_records = [t for t in all_trades if t.get("exit_type") == "partial_exit"]

final_pnl      = sum(t["pnl"] for t in final_trades)
final_wins     = [t for t in final_trades if t["pnl"] > 0]
final_losses   = [t for t in final_trades if t["pnl"] <= 0]
final_wr       = len(final_wins) / len(final_trades) if final_trades else 0.0
final_avg_pnl  = final_pnl / len(final_trades) if final_trades else 0.0
avg_win        = sum(t["pnl"] for t in final_wins) / len(final_wins) if final_wins else 0.0
avg_loss       = sum(t["pnl"] for t in final_losses) / len(final_losses) if final_losses else 0.0
final_avg_rr   = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0

layer_b_count      = sum(1 for t in all_trades if t.get("layer_b_entered"))
displacement_count = sum(1 for t in all_trades if t.get("smt_type") == "displacement")
fill_count         = sum(1 for t in all_trades if t.get("smt_type") == "fill")

# Plan 3 — hypothesis alignment breakdown
aligned_finals    = [t for t in final_trades if str(t.get("matches_hypothesis")) == "True"]
misaligned_finals = [t for t in final_trades if str(t.get("matches_hypothesis")) == "False"]
no_hyp_finals     = [t for t in final_trades if t.get("matches_hypothesis") is None]
fvg_detected_count = sum(1 for t in final_trades if str(t.get("fvg_detected")) == "True")

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
print(f"hypothesis_aligned:   {len(aligned_finals)}")
print(f"hypothesis_misaligned:{len(misaligned_finals)}")
print(f"hypothesis_none:      {len(no_hyp_finals)}")
print(f"fvg_detected_count:   {fvg_detected_count}")
exits = test_stats.get("exit_type_breakdown", {})
for k, v in sorted(exits.items()):
    print(f"exit_{k}: {v}")

from backtest_smt import _write_trades_tsv
_write_trades_tsv(all_trades)
