"""tests/test_e2e.py — Phase 5 end-to-end integration tests. All @pytest.mark.integration."""
import ast, importlib.util, pathlib, subprocess, tempfile
import pandas as pd
import pytest
import train
from prepare import CACHE_DIR, BACKTEST_START

REPO_ROOT = pathlib.Path(__file__).parent.parent
EXPECTED_TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]
EXPECTED_COLUMNS = {"open", "high", "low", "close", "volume", "price_10am"}


@pytest.fixture(scope="module")
def all_parquet_paths():
    p = pathlib.Path(CACHE_DIR)
    return sorted(p.glob("*.parquet")) if p.is_dir() else []


@pytest.fixture(scope="module")
def skip_if_cache_missing(all_parquet_paths):
    if not all_parquet_paths:
        pytest.skip(f"Cache empty at {CACHE_DIR}. Run `uv run prepare.py` first.")


@pytest.fixture(scope="module")
def first_parquet_df(all_parquet_paths, skip_if_cache_missing):
    return pd.read_parquet(all_parquet_paths[0])


@pytest.fixture(scope="module")
def all_ticker_dfs(skip_if_cache_missing):
    return train.load_all_ticker_data()


@pytest.fixture(scope="module")
def backtest_stats(all_ticker_dfs):
    return train.run_backtest(all_ticker_dfs)


@pytest.mark.integration
def test_parquet_files_exist(all_parquet_paths, skip_if_cache_missing):
    """
    Verify that at least the expected tickers have parquet files in CACHE_DIR.
    One file per ticker in EXPECTED_TICKERS must exist.
    """
    present = {p.stem for p in all_parquet_paths}
    missing = [t for t in EXPECTED_TICKERS if t not in present]
    assert not missing, (
        f"Missing parquet files for tickers: {missing}. "
        f"Run `uv run prepare.py` to download them."
    )


@pytest.mark.integration
def test_parquet_schema_has_required_columns(first_parquet_df, skip_if_cache_missing):
    """
    Every parquet file must contain the columns that train.py's screener reads.
    Checks the first available file as representative.
    """
    actual_columns = set(first_parquet_df.columns)
    missing = EXPECTED_COLUMNS - actual_columns
    assert not missing, f"Parquet file is missing columns: {missing}"


@pytest.mark.integration
def test_parquet_index_is_date_objects(first_parquet_df, skip_if_cache_missing):
    """
    train.py slices with df.loc[:today] where today is a datetime.date object.
    The index must be date objects, not pd.Timestamp or strings.
    """
    import datetime
    assert len(first_parquet_df) > 0, "Parquet file is empty"
    sample = first_parquet_df.index[0]
    assert type(sample) is datetime.date, (
        f"Expected datetime.date index, got {type(sample).__name__}. "
        "train.py slicing with df.loc[:today] requires date objects."
    )


@pytest.mark.integration
def test_train_exits_zero_with_pnl_output(skip_if_cache_missing):
    """
    `uv run train.py` must exit with code 0 and print 'train_total_pnl: <float>' to stdout.
    This is the primary integration smoke test — if this passes, the agent loop works.
    """
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"train.py exited with code {result.returncode}.\n"
        f"stderr:\n{result.stderr[-2000:]}\n"
        f"stdout:\n{result.stdout[-500:]}"
    )
    pnl_lines = [
        line for line in result.stdout.splitlines()
        if line.startswith("train_total_pnl:")
    ]
    assert len(pnl_lines) == 1, (
        f"Expected exactly one 'train_total_pnl:' line in output, got {len(pnl_lines)}.\n"
        f"stdout:\n{result.stdout}"
    )
    # P&L value must be parseable as float
    value_str = pnl_lines[0].split(":", 1)[1].strip()
    pnl_value = float(value_str)  # raises ValueError if not parseable
    assert isinstance(pnl_value, float)


@pytest.mark.integration
def test_output_has_all_seven_fields(skip_if_cache_missing):
    """
    The output block parsed by the agent must contain all 7 required fields.
    Runs train.py via subprocess and checks stdout for each field name.
    """
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"train.py crashed: {result.stderr[-1000:]}"
    required_fields = [
        "train_sharpe:", "train_total_trades:", "train_win_rate:",
        "train_avg_pnl_per_trade:", "train_total_pnl:",
        "train_backtest_start:", "train_backtest_end:",
    ]
    for field in required_fields:
        assert any(line.startswith(field) for line in result.stdout.splitlines()), (
            f"Missing output field '{field}' in train.py output.\n"
            f"stdout:\n{result.stdout}"
        )


@pytest.mark.integration
def test_screen_day_on_real_parquet_data(all_parquet_paths, skip_if_cache_missing):
    """
    Criterion 18 from the screener acceptance criteria: screen_day must run
    without raising an exception on the last 10 days of any real parquet file.

    Does NOT assert that signals are generated — only that no exception is raised.
    """
    import datetime
    df = pd.read_parquet(all_parquet_paths[0])
    backtest_start = datetime.date.fromisoformat(BACKTEST_START)

    # Get last 10 trading days in the backtest window
    backtest_days = [d for d in df.index if d >= backtest_start]
    if not backtest_days:
        pytest.skip("No backtest-window days in the parquet file — date range mismatch.")

    sample_days = sorted(backtest_days)[-10:]
    errors = []
    for today in sample_days:
        hist = df.loc[:today]
        try:
            result = train.screen_day(hist, today)
            assert result is None or (isinstance(result, dict) and "stop" in result), (
                f"screen_day returned unexpected type {type(result)} on {today}"
            )
        except Exception as exc:
            errors.append(f"{today}: {exc}")

    assert not errors, (
        f"screen_day raised exceptions on {len(errors)} days:\n" + "\n".join(errors)
    )


@pytest.mark.integration
def test_pnl_self_consistency(backtest_stats):
    """total_pnl ≈ avg_pnl_per_trade × total_trades (within $0.05/trade rounding tolerance)."""
    total, avg, n = backtest_stats["total_pnl"], backtest_stats["avg_pnl_per_trade"], backtest_stats["total_trades"]
    if n == 0:
        assert total == 0.0 and avg == 0.0
    else:
        assert abs(total - round(avg * n, 2)) <= 0.05 * n, (
            f"P&L inconsistency: total={total}, avg×n={round(avg*n,2)}"
        )


@pytest.mark.integration
def test_agent_loop_two_iterations_multi_ticker(all_ticker_dfs, backtest_stats):
    """
    Simulates two sequential agent loop iterations on all cached tickers (≥ 2 required).

    Iteration 1 — relax volume threshold (vol_ratio 1.0 → 0.8):
      Accepts slightly below-average volume days.
    Iteration 2 — relax RSI upper bound (75 → 80) + resistance gate (2.0 → 0.1 ATR):
      Builds on whichever source was *kept* from iteration 1, exactly as the real
      loop would: if iter 1 improved Sharpe, iter 2 starts from that code; if not,
      iter 2 starts from the original.

    Keep/discard rule: keep the new source only if total_pnl strictly improves.

    Verifies:
    - ≥ 2 tickers participated (multi-ticker requirement)
    - After both iterations, best_pnl == max(baseline, iter1, iter2) — the
      keep/discard accounting is correct and the best is never lost
    - The fully-relaxed screener produces ≥ 1 trade across the ticker universe,
      confirming at least one real position was opened (non-trivial backtest)
    """
    import os as _os

    assert len(all_ticker_dfs) >= 2, (
        f"Multi-ticker test requires ≥ 2 tickers; cache has {len(all_ticker_dfs)}: "
        f"{list(all_ticker_dfs.keys())}"
    )

    train_source = REPO_ROOT.joinpath("train.py").read_text(encoding="utf-8")

    def run_source(source, label):
        """Load modified train.py source as a temp module and run its backtester."""
        with tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(source)
            tmp = f.name
        try:
            spec = importlib.util.spec_from_file_location(label, tmp)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.run_backtest(all_ticker_dfs)
        finally:
            _os.unlink(tmp)

    # Iteration 0 — baseline from fixture (unmodified screener)
    best_pnl = backtest_stats["total_pnl"]
    best_source = train_source
    history = [best_pnl]

    # ── Iteration 1: relax volume threshold ───────────────────────────────────
    assert "vol_ratio < 1.0" in train_source, "Volume pattern changed — update test"
    src1 = train_source.replace("vol_ratio < 1.0", "vol_ratio < 0.8", 1)
    ast.parse(src1)  # mutation must be valid Python

    stats1 = run_source(src1, "train_iter1")
    history.append(stats1["total_pnl"])
    if stats1["total_pnl"] > best_pnl:  # keep
        best_pnl = stats1["total_pnl"]
        best_source = src1

    # ── Iteration 2: relax RSI upper bound + relax resistance ─────────────────
    # Starts from best_source — correctly reflects the keep/discard outcome above.
    assert "rsi <= 75" in best_source, "RSI pattern changed — update test"
    assert "res_atr < 2.0" in best_source, "Resistance pattern changed — update test"
    src2 = best_source.replace("rsi <= 75", "rsi <= 80", 1)
    src2 = src2.replace("res_atr < 2.0", "res_atr < 0.1", 1)
    ast.parse(src2)

    stats2 = run_source(src2, "train_iter2")
    history.append(stats2["total_pnl"])
    if stats2["total_pnl"] > best_pnl:  # keep
        best_pnl = stats2["total_pnl"]

    # ── Invariant: tracked best must equal the max observed across all iterations ─
    assert best_pnl == max(history), (
        f"Keep/discard accounting error: tracked best={best_pnl}, "
        f"max of history={max(history)}, full history={history}"
    )

    # ── Multi-ticker trade check ───────────────────────────────────────────────
    # Apply all mutations from the original source and verify ≥ 1 trade fires
    # across the full ticker set. This confirms the pipeline ran real positions
    # (not just returned empty stats) on a multi-ticker dataset.
    src_full = train_source.replace("vol_ratio < 1.0", "vol_ratio < 0.8", 1)
    src_full = src_full.replace("rsi <= 75", "rsi <= 80", 1)
    src_full = src_full.replace("res_atr < 2.0", "res_atr < 0.1", 1)
    ast.parse(src_full)

    stats_full = run_source(src_full, "train_full")
    assert stats_full["total_trades"] >= 1, (
        f"Full relaxation produced 0 trades across {len(all_ticker_dfs)} tickers "
        f"({list(all_ticker_dfs.keys())}). Extend backtest window or re-run prepare.py."
    )


@pytest.mark.integration
def test_agent_loop_threshold_mutation_no_crash(all_ticker_dfs, backtest_stats):
    """
    Simulates the first agent loop iteration on real data:
      1. Baseline Sharpe from the default (strict) screener — from fixture.
      2. Relaxed screener (vol_ratio 1.0 → 0.8, RSI 75 → 80, res_atr 2.0 → 0.1).
      3. Both runs complete without error.
      4. Relaxed screener must produce ≥ 1 trade — proves agent has a viable path.

    Does NOT assert relaxed Sharpe > strict Sharpe (flaky on real data).
    Asserts viability: at least one threshold mutation generates signal.
    """
    import os as _os

    train_source = REPO_ROOT.joinpath("train.py").read_text(encoding="utf-8")
    assert "vol_ratio < 1.0" in train_source, (
        "Volume threshold 'vol_ratio < 1.0' not found in train.py editable section. "
        "Update this test if the threshold expression was changed."
    )
    assert "rsi <= 75" in train_source, (
        "RSI threshold 'rsi <= 75' not found in train.py editable section. "
        "Update this test if the RSI expression was changed."
    )
    # Simulate an agent loop mutation sweep across two screener parameters:
    #   1. Volume threshold: vol_ratio < 1.0 → 0.8 (accept slightly below-average volume)
    #   2. RSI upper bound: rsi <= 75 → rsi <= 80 (allow more momentum)
    relaxed_source = train_source.replace("vol_ratio < 1.0", "vol_ratio < 0.8", 1)
    relaxed_source = relaxed_source.replace("rsi <= 75", "rsi <= 80", 1)
    assert "res_atr < 2.0" in relaxed_source, (
        "Resistance threshold 'res_atr < 2.0' not found. "
        "Update this test if the resistance check was changed."
    )
    relaxed_source = relaxed_source.replace("res_atr < 2.0", "res_atr < 0.1", 1)
    ast.parse(relaxed_source)  # must be valid Python

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False,
                                     mode="w", encoding="utf-8") as f:
        f.write(relaxed_source)
        tmp_path = f.name

    try:
        spec = importlib.util.spec_from_file_location("train_relaxed", tmp_path)
        relaxed_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relaxed_mod)
        relaxed_stats = relaxed_mod.run_backtest(all_ticker_dfs)
    finally:
        _os.unlink(tmp_path)

    assert "total_pnl" in backtest_stats  # baseline from fixture
    assert "total_pnl" in relaxed_stats

    # Key Phase 5 viability requirement (PRD: "Phase 5 validation explicitly
    # checks for at least 1 trade before handing off to the agent")
    assert relaxed_stats["total_trades"] >= 1, (
        f"Relaxed screener (vol_ratio 0.8, RSI 80, res_atr 0.1) produced 0 trades over "
        f"{BACKTEST_START} to {train.BACKTEST_END} on {len(all_ticker_dfs)} tickers. "
        "Extend BACKTEST_START/BACKTEST_END and re-run `uv run prepare.py`."
    )
