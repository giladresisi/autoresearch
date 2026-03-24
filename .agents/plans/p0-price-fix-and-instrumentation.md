# P0: Price Fix and Trade Instrumentation

## Feature Summary

Four tightly-coupled changes that must ship together before the next optimization run:

- **P0-A** — Fix price extraction in `prepare.py`: use `Close` (not `Open`) of the 9:30 AM yfinance bar → represents ~10:30 AM price after open volatility settles. Rename column `price_10am` → `price_1030am` everywhere.
- **P0-B** — MFE/MAE: track Maximum Favorable/Adverse Excursion in ATR units per trade.
- **P0-C** — Exit-type tagging: add `exit_type` column to all trade records (stop_hit, end_of_backtest; partial already present).
- **P0-D** — R-multiple: add `r_multiple = (exit_price − entry_price) / (entry_price − initial_stop)` per trade.

**User story:** As the optimization agent, I want all 27+ past iterations to be re-run with correct ~10:30 AM prices so metrics are valid, and I want MFE/MAE/exit-type/R-multiple columns in trades.tsv so I can diagnose stop placement and exit quality in subsequent runs.

---

## Affected Files

| File | Change type |
|---|---|
| `prepare.py` | P0-A: 3 line edits (column extraction + rename + warning message) |
| `train.py` (mutable zone — above marker) | P0-A: rename all `price_10am` references in `screen_day()`, `manage_position()`, `detect_regime()`, `_compute_avg_correlation()` |
| `train.py` (immutable zone — below marker) | P0-A rename + P0-B + P0-C + P0-D in `run_backtest()` and `_write_trades_tsv()` → **GOLDEN_HASH update required** |
| `tests/test_prepare.py` | Update column name throughout; update/rename `test_price_10am_is_open_of_10am_bar` to verify Close semantics |
| `tests/test_optimization.py` | Update `_make_rising_dataset()` fixture + all inline `screen_day` helpers; update GOLDEN_HASH constant |
| `tests/test_e2e.py` | Update `EXPECTED_COLUMNS` set |
| `tests/test_v4_a.py` | Scan and rename any `price_10am` references |
| `tests/test_v4_b.py` | Scan and rename; add new tests for P0-B/C/D |
| `tests/test_backtester.py` | Scan and rename any `price_10am` references |

---

## Critical Constraints

1. **P0-A is the strict prerequisite for everything else.** The column rename must be consistent across prepare.py, train.py, and all test fixtures before any P0-B/C/D change runs.
2. **GOLDEN_HASH** is in `tests/test_optimization.py` line 118. It must be recomputed *after* all immutable-zone edits are complete — compute with:
   ```
   python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
   ```
3. **Cache invalidation:** Existing parquet files contain `price_10am` column. After P0-A the column no longer exists in freshly-generated files, causing a KeyError at runtime. The plan explicitly deletes the cache directory contents. Users must re-run `prepare.py` before the next optimization session.
4. **DO NOT EDIT boundary** is at line 358 of `train.py`. All `run_backtest()` and `_write_trades_tsv()` edits are **below** the marker — they require the GOLDEN_HASH update.

---

## Implementation Tasks

All tasks are sequential (P0-A unblocks B/C/D; GOLDEN_HASH update is last).

### WAVE 1 — P0-A: Fix prepare.py (no dependencies)

**Task 1.1 — prepare.py extraction fix**

File: `prepare.py`, function `resample_to_daily()` (~line 92–98)

```python
# BEFORE (lines 95-98):
mask = df.index.time == datetime.time(9, 30)
df_10am = df[mask][["Open"]].copy()
df_10am.index = pd.Index([ts.date() for ts in df_10am.index], name="date")
price_10am_series = df_10am["Open"].rename("price_10am")

# AFTER:
mask = df.index.time == datetime.time(9, 30)
df_10am = df[mask][["Close"]].copy()
df_10am.index = pd.Index([ts.date() for ts in df_10am.index], name="date")
price_1030am_series = df_10am["Close"].rename("price_1030am")
```

Also update line 112:
```python
# BEFORE:
daily = daily.join(price_10am_series, how="left")
# AFTER:
daily = daily.join(price_1030am_series, how="left")
```

Also update `validate_ticker_data()` (~line 126):
```python
# BEFORE:
n_missing = int(backtest_df["price_10am"].isna().sum())
if n_missing > 0:
    print(f"WARNING: {ticker} has {n_missing} backtest days with missing price_10am")
# AFTER:
n_missing = int(backtest_df["price_1030am"].isna().sum())
if n_missing > 0:
    print(f"WARNING: {ticker} has {n_missing} backtest days with missing price_1030am")
```

**Task 1.2 — train.py mutable zone rename**

Search-and-replace all occurrences of `"price_10am"` and `price_10am` in the mutable zone (above the `# ── DO NOT EDIT BELOW THIS LINE` marker at line 358).

Occurrences to find/replace:
- Column accesses: `df.loc[..., "price_10am"]` → `df.loc[..., "price_1030am"]`
- Column accesses: `df["price_10am"]` → `df["price_1030am"]`
- Local variable `price_10am =` → `price_1030am =` (rename the variable too for clarity)
- Any other string or attribute access

**Task 1.3 — train.py immutable zone rename (P0-A portion only)**

In `run_backtest()` below the DO NOT EDIT marker, rename all `price_10am` column accesses:

Key locations (approximate lines post-prior edits):
- ~line 490: `price_10am = float(df.loc[today, "price_10am"])` → `price_1030am = float(df.loc[today, "price_1030am"])`
- ~line 491–514: all uses of the local variable `price_10am` → `price_1030am`
- ~line 557: `price = float(df.loc[today, "price_10am"])` → `df.loc[today, "price_1030am"]`
- ~line 568: `last_price = float(df["price_10am"].iloc[-1])` → `df["price_1030am"].iloc[-1]`

**Do not update GOLDEN_HASH yet** — P0-B/C/D changes are still pending.

---

### WAVE 2 — P0-B: MFE/MAE instrumentation

All changes are in `run_backtest()` (immutable zone). No mutable zone changes.

**Task 2.1 — Track high/low since entry**

In the position dict initialization (~line 539), add two new tracking fields:

```python
portfolio[ticker] = {
    ...existing fields...,
    "initial_stop":     stop_raw,           # P0-D: unchanged reference stop
    "high_since_entry": entry_price,        # P0-B: will track max price seen
    "low_since_entry":  entry_price,        # P0-B: will track min price seen
}
```

**Task 2.2 — Update high/low in mark-to-market step**

After the mark-to-market price read (~line 557), add:

```python
if not np.isnan(price):
    portfolio_value += price * pos["shares"]
    # P0-B: track extremes for MFE/MAE
    pos["high_since_entry"] = max(pos.get("high_since_entry", price), price)
    pos["low_since_entry"]  = min(pos.get("low_since_entry",  price), price)
```

**Task 2.3 — Compute MFE/MAE on close**

Helper to compute MFE/MAE (add inline in the two close paths):

```python
def _mfe_mae(pos: dict) -> tuple[float, float]:
    """Returns (mfe_atr, mae_atr). Returns (0.0, 0.0) if atr14 == 0."""
    atr = pos.get("atr14", 0.0)
    if atr == 0.0:
        return 0.0, 0.0
    mfe = (pos.get("high_since_entry", pos["entry_price"]) - pos["entry_price"]) / atr
    mae = (pos["entry_price"] - pos.get("low_since_entry",  pos["entry_price"])) / atr
    return round(mfe, 4), round(mae, 4)
```

Add this as a nested helper at the top of `run_backtest()` (inside the function body, before the main loop).

In the **stop-hit** trade_record (~line 471):
```python
_mfe, _mae = _mfe_mae(pos)
trade_records.append({
    ...existing fields...,
    "exit_type":  "stop_hit",   # P0-C
    "mfe_atr":    _mfe,         # P0-B
    "mae_atr":    _mae,         # P0-B
    "r_multiple": round((pos["stop_price"] - pos["entry_price"]) /
                        (pos["entry_price"] - pos["initial_stop"]), 4)
                  if (pos["entry_price"] - pos["initial_stop"]) > 0 else "",  # P0-D
})
```

In the **end-of-backtest** trade_record (~line 573):
```python
_mfe, _mae = _mfe_mae(pos)
trade_records.append({
    ...existing fields...,
    "exit_type":  "end_of_backtest",   # P0-C
    "mfe_atr":    _mfe,                # P0-B
    "mae_atr":    _mae,                # P0-B
    "r_multiple": round((last_price - pos["entry_price"]) /
                        (pos["entry_price"] - pos["initial_stop"]), 4)
                  if (pos["entry_price"] - pos["initial_stop"]) > 0 else "",  # P0-D
})
```

The **partial** record (~line 503) already has `exit_type: "partial"`. Add MFE/MAE and r_multiple to it too:
```python
_mfe, _mae = _mfe_mae(pos)
trade_records.append({
    ...existing fields...,
    "exit_type":   "partial",
    "mfe_atr":     _mfe,
    "mae_atr":     _mae,
    "r_multiple":  round((price_1030am - pos["entry_price"]) /
                         (pos["entry_price"] - pos["initial_stop"]), 4)
                   if (pos["entry_price"] - pos["initial_stop"]) > 0 else "",
})
```

---

### WAVE 3 — Update _write_trades_tsv fieldnames

**Task 3.1 — Expand fieldnames**

In `_write_trades_tsv()` (~line 779):

```python
# BEFORE:
fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
              "stop_type", "regime", "entry_price", "exit_price", "pnl"]

# AFTER:
fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
              "stop_type", "regime", "entry_price", "exit_price", "pnl",
              "exit_type", "mfe_atr", "mae_atr", "r_multiple"]
```

The `restval=""` already present in the DictWriter ensures backward-compatible reads on older trade_records that lack the new keys.

---

### WAVE 4 — Test updates

**Task 4.1 — test_prepare.py: column rename + semantic update**

1. In `_make_daily_df()`: rename `"price_10am"` key → `"price_1030am"` (line ~58).
2. In `test_resample_produces_expected_columns()`: update expected column set from `"price_10am"` → `"price_1030am"`.
3. Rename `test_price_10am_is_open_of_10am_bar()` → `test_price_1030am_is_close_of_930_bar()` and update the assertion:

```python
def test_price_1030am_is_close_of_930_bar():
    """price_1030am should be the Close of the 9:30 AM bar (= ~10:30 AM price)."""
    hourly = _make_hourly_df(5)
    daily = resample_to_daily(hourly)
    hourly_et = hourly.copy()
    hourly_et.index = hourly_et.index.tz_convert("America/New_York")
    for d, row in daily.iterrows():
        mask = (hourly_et.index.date == d) & (hourly_et.index.time == datetime.time(9, 30))
        assert mask.sum() == 1, f"Expected one 9:30am bar for {d}"
        expected = hourly_et.loc[mask, "Close"].iloc[0]   # Close, not Open
        assert row["price_1030am"] == pytest.approx(expected)
```

4. Scan rest of test_prepare.py for any remaining `"price_10am"` strings and rename.

**Task 4.2 — test_e2e.py: EXPECTED_COLUMNS**

Line 10: `"price_10am"` → `"price_1030am"` in `EXPECTED_COLUMNS`.

**Task 4.3 — test_optimization.py: fixture + inline helpers**

1. `_make_rising_dataset()` line 51: `"price_10am"` → `"price_1030am"`.
2. Scan all inline `screen_day` / `manage_position` mock implementations in the file for `"price_10am"` references → rename.
3. Leave `GOLDEN_HASH` constant unchanged for now — it will be updated in Task 5.1 after all code changes are finalized.

**Task 4.4 — test_v4_a.py, test_v4_b.py, test_backtester.py: rename**

Grep each file for `"price_10am"` and rename to `"price_1030am"`. These are fixture-column references in test DataFrames.

**Task 4.5 — New tests for P0-B/C/D**

Add to `tests/test_v4_b.py` (or a new `tests/test_p0_instrumentation.py` if the file is getting large):

```python
# ── P0-B: MFE/MAE ─────────────────────────────────────────────────────────────

def test_mfe_atr_positive_for_winning_trade():
    """A trade that went up before exiting should have mfe_atr > 0."""
    from train import run_backtest
    # build a dataset where the position moves up before stop hits
    ticker_dfs = _make_winner_dataset()  # use existing _make_rising_dataset or new helper
    result = run_backtest(ticker_dfs, start=..., end=...)
    records = result["trade_records"]
    wins = [r for r in records if r["pnl"] > 0]
    assert len(wins) > 0
    assert all(r.get("mfe_atr", 0.0) > 0 for r in wins)


def test_mae_atr_non_negative():
    """MAE (adverse excursion) should always be ≥ 0."""
    from train import run_backtest
    ticker_dfs = _make_rising_dataset()
    result = run_backtest(ticker_dfs)
    for r in result["trade_records"]:
        assert r.get("mae_atr", 0.0) >= 0.0, f"Negative MAE for {r}"


# ── P0-C: exit_type ────────────────────────────────────────────────────────────

def test_exit_type_present_in_all_trade_records():
    """Every trade record must have an exit_type field."""
    from train import run_backtest
    ticker_dfs = _make_rising_dataset()
    result = run_backtest(ticker_dfs)
    for r in result["trade_records"]:
        assert "exit_type" in r, f"Missing exit_type in {r}"
        assert r["exit_type"] in {"stop_hit", "end_of_backtest", "partial"}, \
            f"Unknown exit_type: {r['exit_type']}"


def test_partial_exit_type_unchanged():
    """Partial close records must still have exit_type == 'partial'."""
    from train import run_backtest
    ticker_dfs = _make_rising_dataset()
    result = run_backtest(ticker_dfs)
    partials = [r for r in result["trade_records"] if r.get("exit_type") == "partial"]
    # partial fires when price >= entry + 1 ATR; rising dataset should produce at least one
    # (not guaranteed by every synthetic dataset — skip assertion if none found)
    for r in partials:
        assert r["exit_type"] == "partial"


# ── P0-D: R-multiple ──────────────────────────────────────────────────────────

def test_r_multiple_present_in_all_trade_records():
    """Every trade record must have an r_multiple field (empty string if initial risk == 0)."""
    from train import run_backtest
    ticker_dfs = _make_rising_dataset()
    result = run_backtest(ticker_dfs)
    for r in result["trade_records"]:
        assert "r_multiple" in r, f"Missing r_multiple in {r}"


def test_r_multiple_positive_for_winning_trade():
    """Winning trades (pnl > 0) exited above entry → r_multiple should be positive."""
    from train import run_backtest
    ticker_dfs = _make_rising_dataset()
    result = run_backtest(ticker_dfs)
    for r in result["trade_records"]:
        if isinstance(r.get("r_multiple"), float) and r["pnl"] > 0 and r["exit_type"] != "partial":
            assert r["r_multiple"] > 0, f"Expected positive r_multiple for winner: {r}"


def test_r_multiple_negative_for_stop_hit():
    """A stop hit at or below initial stop → r_multiple should be ≤ 0."""
    from train import run_backtest
    # Use a dataset where prices fall and stop hits occur
    ticker_dfs = _make_falling_dataset()   # new helper: prices fall after entry
    result = run_backtest(ticker_dfs)
    stop_hits = [r for r in result["trade_records"] if r.get("exit_type") == "stop_hit"]
    for r in stop_hits:
        if isinstance(r.get("r_multiple"), float):
            assert r["r_multiple"] <= 0, f"Expected non-positive r_multiple for stop hit: {r}"
```

Note: `_make_falling_dataset()` helper needed — returns a single-ticker dataset where prices drop below stop level within a few days. Can be modelled after `_make_rising_dataset()` with `np.linspace(150, 90, 200)`.

**Task 4.6 — test_prepare.py: verify Close semantics (semantic correctness)**

Confirm that the updated `test_price_1030am_is_close_of_930_bar` uses `"Close"` from the hourly fixture, not `"Open"`. The `_make_hourly_df()` helper already sets `"Close": price + 0.1` distinct from `"Open": price`, so the test will catch any regression.

---

### WAVE 5 — GOLDEN_HASH recompute and cache invalidation

**Task 5.1 — Recompute and update GOLDEN_HASH**

After all code changes are in place, run:

```bash
python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
```

Replace the `GOLDEN_HASH` constant in `tests/test_optimization.py` line 118 with the output.

**Task 5.2 — Cache invalidation note**

The parquet files in the `AUTORESEARCH_CACHE_DIR` (default: `stock_data/`) have the old `price_10am` column schema. They are now semantically invalid (wrong price was used).

Add a note in PROGRESS.md under the P0 entry:

> **ACTION REQUIRED before next optimize run:** Delete the parquet cache (`rm -rf stock_data/*.parquet` or equivalent) and re-run `prepare.py`. Old parquet files contain incorrect `price_10am` (9:30 AM open — market open spike); new files will contain `price_1030am` (9:30 bar close — post-open-volatility price). All 27+ optimization iterations were evaluated on the wrong price.

---

## Test Plan

| ID | Test | Tool | File | Run command | Coverage target |
|----|------|------|------|-------------|-----------------|
| T1 | `test_price_1030am_is_close_of_930_bar` | pytest | test_prepare.py | `pytest tests/test_prepare.py::test_price_1030am_is_close_of_930_bar -v` | P0-A: Close semantics |
| T2 | `test_resample_produces_expected_columns` | pytest | test_prepare.py | `pytest tests/test_prepare.py::test_resample_produces_expected_columns -v` | P0-A: column name |
| T3 | `test_mfe_atr_positive_for_winning_trade` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_mfe_atr_positive_for_winning_trade -v` | P0-B: MFE positive |
| T4 | `test_mae_atr_non_negative` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_mae_atr_non_negative -v` | P0-B: MAE ≥ 0 |
| T5 | `test_exit_type_present_in_all_trade_records` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_exit_type_present_in_all_trade_records -v` | P0-C: all records tagged |
| T6 | `test_partial_exit_type_unchanged` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_partial_exit_type_unchanged -v` | P0-C: partial unchanged |
| T7 | `test_r_multiple_present_in_all_trade_records` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_r_multiple_present_in_all_trade_records -v` | P0-D: column present |
| T8 | `test_r_multiple_positive_for_winning_trade` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_r_multiple_positive_for_winning_trade -v` | P0-D: sign correct for winners |
| T9 | `test_r_multiple_negative_for_stop_hit` | pytest | test_v4_b.py | `pytest tests/test_v4_b.py::test_r_multiple_negative_for_stop_hit -v` | P0-D: sign correct for stops |
| T10 | `test_harness_below_marker_matches_golden_hash` | pytest | test_optimization.py | `pytest tests/test_optimization.py::test_harness_below_marker_matches_golden_hash -v` | GOLDEN_HASH integrity |
| T11 | `_write_trades_tsv` columns check | pytest | test_v4_b.py | existing `test_trades_tsv_*` tests — update expected fieldnames | P0-B/C/D: fieldnames |
| T12 | Full regression suite | pytest | all | `python -m pytest tests/ -q` | No regressions |

---

## Execution Order

```
Task 1.1  prepare.py fix           [P0-A source fix]
Task 1.2  train.py mutable rename  [P0-A, mutable zone]
Task 1.3  train.py immutable rename [P0-A, immutable zone, partial]
Task 2.1  position dict fields     [P0-B/D: new position tracking fields]
Task 2.2  mark-to-market high/low  [P0-B: live tracking]
Task 2.3  MFE/MAE helper + close   [P0-B: compute on close]
       ↓  (also adds exit_type P0-C and r_multiple P0-D in same records)
Task 3.1  _write_trades_tsv fields [P0-B/C/D: fieldnames]
Task 4.1  test_prepare.py          [test update]
Task 4.2  test_e2e.py              [test update]
Task 4.3  test_optimization.py     [test update, skip GOLDEN_HASH]
Task 4.4  test_v4_a/b, backtester  [test update]
Task 4.5  new P0-B/C/D tests       [new tests]
Task 4.6  verify Close semantics   [verification]
Task 5.1  GOLDEN_HASH recompute    [must be last code change]
Task 5.2  cache invalidation note  [PROGRESS.md note]
```

---

## Acceptance Criteria

- [ ] `prepare.py:resample_to_daily()` uses `Close` of 9:30 AM bar and produces a column named `price_1030am`; no `price_10am` column in freshly generated parquet files
- [ ] Zero occurrences of the string `"price_10am"` in `prepare.py`, `train.py`, `tests/test_prepare.py`, `tests/test_optimization.py`, `tests/test_e2e.py`
- [ ] `trades.tsv` schema includes `exit_type`, `mfe_atr`, `mae_atr`, `r_multiple` columns (verified by updating fieldnames list and running a backtest)
- [ ] All trade records (stop_hit, end_of_backtest, partial) have a non-empty `exit_type` value in `{"stop_hit", "end_of_backtest", "partial"}`
- [ ] MFE ≥ 0 and MAE ≥ 0 for all records (by construction); MAE = 0 is valid if price never dipped below entry
- [ ] R-multiple is positive for profitable exits above entry, negative/zero for exits at or below entry
- [ ] `test_harness_below_marker_matches_golden_hash` passes with the new GOLDEN_HASH value
- [ ] Full test suite passes with 0 new failures (`python -m pytest tests/ -q`)
- [ ] PROGRESS.md updated with cache invalidation action note

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Missed `price_10am` occurrence in test fixtures causes silent wrong-column access | Grep all test files before running the suite: `grep -rn "price_10am" tests/` — must return 0 results |
| GOLDEN_HASH updated too early (before all immutable changes) | Task 5.1 is the final task; recompute command is deterministic and idempotent |
| `_make_falling_dataset()` helper doesn't actually trigger stop hits | Verify by checking `len(stop_hits) > 0` in the test — if 0, the dataset needs a lower floor |
| `initial_stop` field missing on old open positions (if running against saved state) | `pos.get("initial_stop", pos["stop_price"])` fallback is safe since stop_price only ever increases |
| Partial close record MFE reflects position state at close (may understate full-position MFE) | Acceptable for P0 — document in PROGRESS.md notes |
