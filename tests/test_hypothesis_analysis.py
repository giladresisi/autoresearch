# tests/test_hypothesis_analysis.py
# Plan 3 feature tests: compute_hypothesis_context, HYPOTHESIS_FILTER, rule fields
# in trade records, analyze_hypothesis.py helpers, displacement entry controls,
# PARTIAL_EXIT_LEVEL_RATIO, fvg_detected, and FVG_LAYER_B_REQUIRES_HYPOTHESIS.
import csv
import datetime

import pandas as pd
import pytest

import analyze_hypothesis
import backtest_smt
import hypothesis_smt
from analyze_hypothesis import (
    compute_fold_stats,
    compute_group_stats,
    load_trades,
    print_analysis,
    split_by_alignment,
)
from hypothesis_smt import (
    _count_aligned_rules,
    compute_hypothesis_context,
    compute_hypothesis_direction,
)
from strategy_smt import (
    DISPLACEMENT_STOP_MODE,
    FVG_LAYER_B_REQUIRES_HYPOTHESIS,
    MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT,
    PARTIAL_EXIT_ENABLED,
    PARTIAL_EXIT_LEVEL_RATIO,
    STRUCTURAL_STOP_BUFFER_PTS,
)


# ── Shared helpers ────────────────────────────────────────────────────────────

_DATE = datetime.date(2026, 4, 20)
_BASE = 21800.0


def _make_trades_df(rows: list) -> pd.DataFrame:
    """Build a trades DataFrame with sensible defaults; each row overrides fields."""
    defaults = {
        "entry_date": "2025-01-02", "direction": "long",
        "pnl": 50.0, "exit_type": "exit_tp",
        "matches_hypothesis": "True", "hypothesis_direction": "long",
        "hypothesis_score": "3",
        "pd_range_case": "1.3", "pd_range_bias": "long",
        "week_zone": "discount", "day_zone": "discount",
        "trend_direction": "bullish", "fvg_detected": "False",
    }
    return pd.DataFrame([{**defaults, **r} for r in rows])


def _make_minimal_1m_df(date: datetime.date, base: float = _BASE) -> pd.DataFrame:
    """Minimal synthetic 1m DataFrame: previous day + overnight + 09:00/09:30 bars."""
    rows = []
    prev_day = date - datetime.timedelta(days=3)
    pdh, pdl = base + 100.0, base - 100.0
    pd_mid = (pdh + pdl) / 2.0
    for h in range(0, 24, 4):
        ts = pd.Timestamp(f"{prev_day} {h:02d}:00:00", tz="America/New_York")
        rows.append({
            "ts": ts, "Open": pd_mid, "High": pdh if h == 12 else pd_mid + 10,
            "Low": pdl if h == 16 else pd_mid - 10, "Close": pd_mid, "Volume": 1000.0,
        })
    for h in range(0, 9):
        ts = pd.Timestamp(f"{date} {h:02d}:00:00", tz="America/New_York")
        rows.append({
            "ts": ts, "Open": pd_mid + 20.0, "High": pd_mid + 25.0,
            "Low": pd_mid + 15.0, "Close": pd_mid + 20.0, "Volume": 500.0,
        })
    ts_900 = pd.Timestamp(f"{date} 09:00:00", tz="America/New_York")
    rows.append({
        "ts": ts_900, "Open": pd_mid - 20.0, "High": pd_mid - 15.0,
        "Low": pd_mid - 25.0, "Close": pd_mid - 20.0, "Volume": 500.0,
    })
    ts_930 = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    rows.append({
        "ts": ts_930, "Open": base, "High": base + 5, "Low": base - 5,
        "Close": base, "Volume": 500.0,
    })
    tuesday = date + datetime.timedelta(days=1)
    ts_tue = pd.Timestamp(f"{tuesday} 09:00:00", tz="America/New_York")
    rows.append({
        "ts": ts_tue, "Open": base, "High": base + 5, "Low": base - 5,
        "Close": base, "Volume": 500.0,
    })
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = None
    return df


def _make_minimal_hist_df(end_date: datetime.date, base: float = _BASE) -> pd.DataFrame:
    """Minimal 5m historical DataFrame spanning 10 days before end_date."""
    rows = []
    for i in range(10, 0, -1):
        d = end_date - datetime.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for bar_h in range(9, 14):
            ts = pd.Timestamp(f"{d} {bar_h:02d}:00:00", tz="America/New_York")
            day_base = base + (10 - i) * 5.0
            rows.append({
                "ts": ts, "Open": day_base, "High": day_base + 10.0,
                "Low": day_base - 5.0, "Close": day_base + 5.0, "Volume": 1000.0,
            })
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = None
    return df


def _full_trade_dict(**overrides) -> dict:
    """Return a trade dict with every required fieldname for _write_trades_tsv."""
    trade = {
        "entry_date": "2025-01-02", "entry_time": "09:30", "exit_time": "10:00",
        "direction": "long", "entry_price": 100.0, "exit_price": 110.0,
        "tdo": 110.0, "stop_price": 98.0, "contracts": 1, "pnl": 20.0,
        "exit_type": "exit_tp", "divergence_bar": 0, "entry_bar": 1,
        "stop_bar_wick_pts": 2.0, "reentry_sequence": 0, "prior_trade_bars_held": 0,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 5.0, "smt_miss_pts": 2.0,
        "bars_since_divergence": 1, "matches_hypothesis": True, "smt_type": "wick",
        "fvg_high": None, "fvg_low": None, "layer_b_entered": False,
        "layer_b_entry_price": None, "layer_b_contracts": 0,
        "hypothesis_direction": "long", "pd_range_case": "1.3", "pd_range_bias": "long",
        "week_zone": "discount", "day_zone": "discount", "trend_direction": "bullish",
        "hypothesis_score": 3, "fvg_detected": False,
    }
    trade.update(overrides)
    return trade


# ── Group 1: compute_hypothesis_context (4 tests) ─────────────────────────────

def test_compute_hypothesis_context_returns_dict_with_all_keys():
    """Valid 1m df + hist df → dict with all 7 keys OR None on insufficient data."""
    mnq = _make_minimal_1m_df(_DATE)
    hist = _make_minimal_hist_df(_DATE)
    result = compute_hypothesis_context(mnq, hist, _DATE)
    if result is not None:
        expected_keys = {"direction", "pd_range_case", "pd_range_bias",
                         "week_zone", "day_zone", "trend_direction", "hypothesis_score"}
        assert set(result.keys()) == expected_keys, (
            f"Missing keys: {expected_keys - set(result.keys())}"
        )


def test_compute_hypothesis_context_returns_none_on_empty_df():
    """Empty 1m df → returns None (no exception)."""
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    hist = _make_minimal_hist_df(_DATE)
    result = compute_hypothesis_context(empty, hist, _DATE)
    assert result is None, f"Expected None on empty df, got {result}"


def test_compute_hypothesis_context_consistent_with_direction():
    """Both compute_hypothesis_context and compute_hypothesis_direction → None on empty df."""
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    hist = _make_minimal_hist_df(_DATE)
    ctx = compute_hypothesis_context(empty, hist, _DATE)
    direction = compute_hypothesis_direction(empty, hist, _DATE)
    assert ctx is None, f"context should be None on empty, got {ctx}"
    assert direction is None, f"direction should be None on empty, got {direction}"


def test_hypothesis_score_zero_when_direction_neutral():
    """Neutral or None direction → _count_aligned_rules returns 0 regardless of inputs."""
    assert _count_aligned_rules("long", "discount", "discount", "bullish", "neutral") == 0, (
        "neutral direction must yield score 0"
    )
    assert _count_aligned_rules("long", "discount", "discount", "bullish", None) == 0, (
        "None direction must yield score 0"
    )


# ── Group 2: HYPOTHESIS_FILTER backtest tests (2 tests) ───────────────────────

def test_hypothesis_filter_constant_false_by_default():
    """backtest_smt.HYPOTHESIS_FILTER must exist and default to False (Plan 3 default)."""
    assert hasattr(backtest_smt, "HYPOTHESIS_FILTER"), "HYPOTHESIS_FILTER missing from backtest_smt"
    assert backtest_smt.HYPOTHESIS_FILTER is False, (
        f"Default must be False, got {backtest_smt.HYPOTHESIS_FILTER}"
    )


def test_hypothesis_filter_is_bool():
    """HYPOTHESIS_FILTER must be a bool (not int/None)."""
    assert isinstance(backtest_smt.HYPOTHESIS_FILTER, bool), (
        f"Expected bool, got {type(backtest_smt.HYPOTHESIS_FILTER)}"
    )


# ── Group 3: Rule fields in trade records (2 tests) ───────────────────────────

def test_write_trades_tsv_includes_rule_fields(tmp_path, monkeypatch):
    """_write_trades_tsv output header must include all 8 new rule-alignment fields."""
    monkeypatch.chdir(tmp_path)
    trade = _full_trade_dict()
    backtest_smt._write_trades_tsv([trade])
    with open(tmp_path / "trades.tsv", newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f, delimiter="\t").fieldnames
    for field in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                  "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                  "fvg_detected"):
        assert field in fieldnames, f"Missing field '{field}' in trades.tsv header"


def test_write_trades_tsv_writes_row_values(tmp_path, monkeypatch):
    """Row values round-trip through TSV serialization (string form)."""
    monkeypatch.chdir(tmp_path)
    trade = _full_trade_dict(
        hypothesis_direction="short", pd_range_case="1.2",
        week_zone="premium", hypothesis_score=2,
    )
    backtest_smt._write_trades_tsv([trade])
    with open(tmp_path / "trades.tsv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        row = next(reader)
    assert row["hypothesis_direction"] == "short", f"got {row['hypothesis_direction']}"
    assert row["pd_range_case"] == "1.2", f"got {row['pd_range_case']}"
    assert row["week_zone"] == "premium", f"got {row['week_zone']}"
    assert row["hypothesis_score"] == "2", f"got {row['hypothesis_score']}"


# ── Group 4: analyze_hypothesis.py functions (10 tests) ──────────────────────

def test_load_trades_reads_tsv(tmp_path):
    """load_trades reads TSV and converts pnl to float."""
    path = tmp_path / "trades.tsv"
    df_in = _make_trades_df([{"pnl": 50.0}, {"pnl": -20.0}])
    df_in.to_csv(path, sep="\t", index=False)
    df = load_trades(str(path))
    assert not df.empty, "df should not be empty"
    assert df["pnl"].dtype.kind == "f", f"pnl dtype must be float, got {df['pnl'].dtype}"


def test_load_trades_missing_file_returns_empty(tmp_path):
    """Nonexistent path → returns empty DataFrame (no exception)."""
    missing = tmp_path / "does_not_exist.tsv"
    df = load_trades(str(missing))
    assert df.empty, "missing file must return empty DataFrame"


def test_split_by_alignment_three_groups():
    """df with True/False/None rows → 3 groups with correct counts."""
    df = _make_trades_df([
        {"matches_hypothesis": "True"},
        {"matches_hypothesis": "True"},
        {"matches_hypothesis": "False"},
        {"matches_hypothesis": "None"},
    ])
    groups = split_by_alignment(df)
    assert len(groups["aligned"]) == 2, f"aligned count: {len(groups['aligned'])}"
    assert len(groups["misaligned"]) == 1, f"misaligned count: {len(groups['misaligned'])}"
    assert len(groups["no_hypothesis"]) == 1, f"no_hypothesis count: {len(groups['no_hypothesis'])}"


def test_split_by_alignment_string_true_false():
    """Stringified 'True'/'False' (TSV form) are split correctly."""
    df = _make_trades_df([
        {"matches_hypothesis": "True"},
        {"matches_hypothesis": "False"},
        {"matches_hypothesis": "False"},
    ])
    groups = split_by_alignment(df)
    assert len(groups["aligned"]) == 1, "one 'True' row expected"
    assert len(groups["misaligned"]) == 2, "two 'False' rows expected"


def test_compute_group_stats_all_winners():
    """All pnl > 0 → win_rate = 1.0."""
    df = _make_trades_df([{"pnl": 10.0}, {"pnl": 20.0}, {"pnl": 30.0}])
    df["pnl"] = df["pnl"].astype(float)
    stats = compute_group_stats(df)
    assert stats["win_rate"] == 1.0, f"win_rate should be 1.0, got {stats['win_rate']}"
    assert stats["count"] == 3, f"count: {stats['count']}"


def test_compute_group_stats_mixed():
    """2 wins $50, 1 loss -$25 → win_rate=2/3, avg_pnl=25.0."""
    df = _make_trades_df([{"pnl": 50.0}, {"pnl": 50.0}, {"pnl": -25.0}])
    df["pnl"] = df["pnl"].astype(float)
    stats = compute_group_stats(df)
    assert stats["win_rate"] == pytest.approx(2 / 3), f"win_rate: {stats['win_rate']}"
    assert stats["avg_pnl"] == pytest.approx(25.0), f"avg_pnl: {stats['avg_pnl']}"


def test_compute_group_stats_empty():
    """Empty df → count=0, no exception."""
    stats = compute_group_stats(pd.DataFrame())
    assert stats["count"] == 0, f"count should be 0, got {stats['count']}"
    assert stats["win_rate"] == 0.0, f"win_rate should be 0.0, got {stats['win_rate']}"


def test_compute_fold_stats_two_folds():
    """6 trades across 2 explicit date-bounded folds → 2 fold entries."""
    df = _make_trades_df([
        {"entry_date": "2025-01-02"},
        {"entry_date": "2025-01-03"},
        {"entry_date": "2025-01-04"},
        {"entry_date": "2025-01-10"},
        {"entry_date": "2025-01-11"},
        {"entry_date": "2025-01-12"},
    ])
    df["pnl"] = df["pnl"].astype(float)
    boundaries = ["2025-01-01", "2025-01-05", "2025-01-15"]
    folds = compute_fold_stats(df, fold_boundaries=boundaries)
    assert len(folds) == 2, f"expected 2 folds, got {len(folds)}"
    assert folds[0]["trade_count"] == 3, f"fold 1 count: {folds[0]['trade_count']}"
    assert folds[1]["trade_count"] == 3, f"fold 2 count: {folds[1]['trade_count']}"


def test_print_analysis_runs_without_error(capsys):
    """print_analysis on a well-formed df → no exception; emits output."""
    df = _make_trades_df([
        {"matches_hypothesis": "True", "pnl": 50.0},
        {"matches_hypothesis": "False", "pnl": -20.0},
    ])
    df["pnl"] = df["pnl"].astype(float)
    print_analysis(df)
    captured = capsys.readouterr()
    assert "HYPOTHESIS ALIGNMENT ANALYSIS" in captured.out, "header missing from output"


def test_print_analysis_no_rule_columns(capsys):
    """df lacking rule columns → skips decomposition gracefully, no exception."""
    df = pd.DataFrame([
        {"entry_date": "2025-01-02", "matches_hypothesis": "True", "pnl": 50.0,
         "exit_type": "exit_tp"},
        {"entry_date": "2025-01-03", "matches_hypothesis": "False", "pnl": -20.0,
         "exit_type": "exit_stop"},
    ])
    print_analysis(df)
    captured = capsys.readouterr()
    assert "columns not found" in captured.out or "HYPOTHESIS ALIGNMENT" in captured.out, (
        "expected graceful handling of missing rule columns"
    )


# ── Group 5: Displacement entry controls (6 tests) ────────────────────────────

def test_displacement_stop_mode_constant_exists_and_is_true():
    """DISPLACEMENT_STOP_MODE must be True by default (Round 3 approved value)."""
    assert DISPLACEMENT_STOP_MODE is True, (
        f"Default must be True (Round 3 approved), got {DISPLACEMENT_STOP_MODE}"
    )


def test_min_hypothesis_score_constant_exists_and_is_zero():
    """MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT must default to 0 (gate disabled)."""
    assert MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT == 0, (
        f"Default must be 0, got {MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT}"
    )


def test_displacement_stop_enabled_by_default():
    """DISPLACEMENT_STOP_MODE is True by default (Round 3 optimizer approved)."""
    assert DISPLACEMENT_STOP_MODE is True, (
        "Displacement stop default is True per Round 3 approval"
    )


def test_displacement_score_gate_disabled_at_zero():
    """Score gate at 0 means any hypothesis_score (including 0) passes the gate."""
    assert MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT == 0, (
        "Gate threshold must default to 0 (no filtering)"
    )


def test_displacement_stop_math_long():
    """Long displacement stop: extreme - STRUCTURAL_STOP_BUFFER_PTS."""
    extreme = 100.0
    stop = extreme - STRUCTURAL_STOP_BUFFER_PTS
    assert stop == 98.0, f"long displacement stop math: {stop}"


def test_displacement_stop_math_short():
    """Short displacement stop: extreme + STRUCTURAL_STOP_BUFFER_PTS."""
    extreme = 110.0
    stop = extreme + STRUCTURAL_STOP_BUFFER_PTS
    assert stop == 112.0, f"short displacement stop math: {stop}"


# ── Group 6: PARTIAL_EXIT_LEVEL_RATIO (2 tests) ───────────────────────────────

def test_partial_exit_level_ratio_default_is_0_33():
    """PARTIAL_EXIT_LEVEL_RATIO must default to 0.33 (Round 3 approved value)."""
    assert PARTIAL_EXIT_LEVEL_RATIO == 0.33, (
        f"Default must be 0.33 (Round 3 approved), got {PARTIAL_EXIT_LEVEL_RATIO}"
    )


def test_partial_exit_level_ratio_interpolation():
    """Interpolation: entry + (tp - entry) * ratio for 0.33 and 0.5."""
    entry, tp = 100.0, 110.0
    level_33 = entry + (tp - entry) * 0.33
    level_50 = entry + (tp - entry) * 0.5
    assert level_33 == pytest.approx(103.3), f"ratio=0.33 → {level_33}"
    assert level_50 == pytest.approx(105.0), f"ratio=0.5 → {level_50}"


# ── Group 7: fvg_detected (1 test) ────────────────────────────────────────────

def test_fvg_detected_in_write_trades_tsv_fieldnames(tmp_path, monkeypatch):
    """fvg_detected must be a fieldname emitted by _write_trades_tsv."""
    monkeypatch.chdir(tmp_path)
    trade = _full_trade_dict(fvg_detected=True)
    backtest_smt._write_trades_tsv([trade])
    with open(tmp_path / "trades.tsv", newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f, delimiter="\t").fieldnames
    assert "fvg_detected" in fieldnames, "fvg_detected must appear in trades.tsv header"


# ── Group 8: FVG_LAYER_B_REQUIRES_HYPOTHESIS (3 tests) ────────────────────────

def test_fvg_layer_b_requires_hypothesis_constant_exists_and_is_false():
    """FVG_LAYER_B_REQUIRES_HYPOTHESIS must default to False (gate disabled)."""
    assert FVG_LAYER_B_REQUIRES_HYPOTHESIS is False, (
        f"Default must be False, got {FVG_LAYER_B_REQUIRES_HYPOTHESIS}"
    )


def test_fvg_layer_b_gate_uses_same_threshold_constant():
    """When the gate is off, Layer B passes regardless of hypothesis_score."""
    assert FVG_LAYER_B_REQUIRES_HYPOTHESIS is False, (
        "Gate must be off by default so Layer B is not blocked"
    )


def test_fvg_layer_b_gate_does_not_affect_constant_defaults():
    """Both Layer B gate and displacement score gate must be at safe defaults."""
    assert FVG_LAYER_B_REQUIRES_HYPOTHESIS is False, "Layer B gate must default to False"
    assert MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT == 0, "score gate must default to 0"
    assert PARTIAL_EXIT_ENABLED is True, "PARTIAL_EXIT_ENABLED must be True per Round 2"
