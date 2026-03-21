# Code Review — Strategy Registry and LLM Selector (Enhancement 6a + 6b)

**Date:** 2026-03-21
**Branch:** autoresearch/mar20
**Reviewer:** Claude Code (automated)

---

## Stats

- Files Modified: 1 (`pyproject.toml`)
- Files Added: 8
- Files Deleted: 0
- New lines: ~530
- Deleted lines: 0

---

## Test Results

- `tests/test_registry.py`: **24 passed** (all new tests)
- `tests/test_selector.py`: **24 passed** (all new tests)
- `tests/test_e2e.py`, `tests/test_prepare.py`: collection errors (pre-existing — missing `yfinance` dependency, unrelated to this changeset)

---

## Issues Found

```
severity: medium
file: scripts/extract_strategy.py
line: 73
issue: Path traversal: --name argument written directly into output path without sanitization
detail: STRATEGIES_DIR / f"{name}.py" will resolve outside the strategies/ directory if
        name contains "../" sequences (e.g. name="../../etc/hosts" writes to /etc/hosts.py
        relative to project root). This is a CLI tool run manually by a researcher, not
        an API surface, so exploitability is low. However, accidental misuse (e.g. a typo
        like "--name ../train_backup") could overwrite files outside the intended directory.
suggestion: Add a guard after argument parsing:
        if "/" in args.name or "\\" in args.name:
            print(f"ERROR: --name must not contain path separators: {args.name}", file=sys.stderr)
            sys.exit(1)
        This is a single-line check before calling extract().
```

```
severity: low
file: strategy_selector.py
line: 33
issue: _API_KEY captured at module import time — late env injection silently uses empty key
detail: _API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") is evaluated once at import.
        If a caller sets os.environ["ANTHROPIC_API_KEY"] after importing strategy_selector
        (e.g. in a script that loads .env after imports), select_strategy() will silently
        use an empty string as the API key, causing an auth error at the Anthropic API call
        rather than at import time. The existing RuntimeError guard catches the no-key case
        at import time (correct), but _API_KEY itself is not re-read at call time.
        Combined with no load_dotenv() call anywhere in the module, callers must set the
        env var before import — this is documented in the docstring but easy to miss.
suggestion: Either (a) read os.environ.get("ANTHROPIC_API_KEY") inside select_strategy()
        rather than at module level, or (b) add a note in the module docstring explicitly
        warning that the env var must be set before `import strategy_selector`.
        Option (a) is safer: client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        inside select_strategy(), and keep the import-time check as a convenience guard only.
```

```
severity: low
file: strategy_selector.py
line: 66-67
issue: vol_avg30 label is misleading when recent_df has fewer than 30 rows
detail: _compute_ticker_snapshot does df = recent_df.tail(30), then computes
        vol_avg30 = float(volume.mean()). If recent_df has, say, 20 rows, vol_avg30 is
        actually the 20-bar average, not 30-bar. The result key "vol_ratio_5d_30d" then
        misrepresents the denominator period. Not a crash, and unlikely in production
        (callers are expected to provide >= 30 rows), but the naming implies a guarantee
        the code doesn't enforce.
suggestion: Either enforce a minimum row count check (similar to the 14-bar check on line 47),
        or rename the variable to vol_avg_window to reflect that it covers whatever rows
        are available in the tail.
```

---

## Pre-existing Failures

- `tests/test_e2e.py` — `ModuleNotFoundError: No module named 'yfinance'` (pre-existing, unrelated to this changeset)
- `tests/test_prepare.py` — same root cause

---

## Observations (No Action Required)

**Indicator fidelity:** `energy_momentum_v1.py` indicators (`calc_rsi14`, `calc_atr14`, `calc_cci`, all pivot/stop functions) are verbatim copies of the same functions in `train.py` at commit `e9886df`. `base_indicators.py` is identical. Numeric behavior is preserved exactly.

**RSI label "Wilder smoothing":** Both `train.py` and the extracted strategy use `rolling(14).mean()` (simple moving average of gains/losses), not a true EMA-based Wilder smoothing (which would use `ewm(com=13)`). The comment saying "Wilder smoothing" is inaccurate but is present in `train.py` itself — this is a pre-existing documentation inconsistency, not introduced by this changeset, and since both implementations are identical, live backtest results are unaffected.

**subprocess safety:** `extract_strategy.py` calls `subprocess.run(["git", "show", f"{tag}:train.py"], ...)` with a list (not `shell=True`). The `tag` argument is safe from shell injection.

**Test mock correctness:** `test_selector.py` patches `anthropic.Anthropic` at the top-level module scope. Since `strategy_selector.py` defers `import anthropic` to inside `select_strategy()` and then calls `anthropic.Anthropic(...)` by name, the patch at `anthropic.Anthropic` correctly intercepts the constructor. The pattern is sound.

**Model name:** `claude-opus-4-6` appears in the installed Anthropic SDK's model references and is a valid identifier.

**Registry type annotation:** `REGISTRY: dict` in `strategies/__init__.py` is unparameterized. This is minor and consistent with the codebase's light use of type hints.
