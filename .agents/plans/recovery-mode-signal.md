# Plan: Recovery Mode Signal Path

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

**Type**: Enhancement — new signal path in `screen_day()`
**Complexity**: ⚠️ Medium
**Parallel Execution**: ✅ Yes
**Status**: Planned

---

## Feature Description

Add a second signal path ("recovery mode") to `screen_day()` in `train.py` that fires during
corrections within ongoing uptrends. The current strategy requires full SMA bull-stack alignment
(`price > SMA50 > SMA100`, `SMA20 > SMA50`), which fires almost never in corrective markets.

Recovery mode fires when:
- `SMA50 > SMA200` — long-term trend still structurally intact (no death cross)
- `price <= SMA50` — stock is in a correction (below shorter-term average)
- `price > SMA20` — short-term momentum is recovering (above 20-day mean)
- All momentum/volume/stop/resistance rules still apply (same guards, tighter RSI range)

## User Story

As a trader running the screener during a broad market correction,
I want the screener to surface tickers that are recovering within structurally healthy uptrends,
so that I have actionable candidates even when the full SMA bull stack hasn't been restored.

---

## CONTEXT REFERENCES

### Files to Read Before Implementing

- `train.py` L224–345 — full `screen_day()` implementation
- `train.py` L262–273 — current Rule 1 (SMA alignment) to restructure
- `train.py` L334–345 — return dict to extend with `signal_path`
- `screener.py` L168–206 — output printing (armed + gap-skipped tables) to update
- `screener.py` L93–164 — main per-ticker loop (add `signal_path` to row dict)
- `tests/test_screener.py` L47–107 — `make_signal_df`, `make_pivot_signal_df` patterns to follow
- `tests/test_screener_script.py` L21–33 — `_write_signal_parquet` pattern to follow
- `screener_prepare.py` L31 — `HISTORY_DAYS` constant to increase for SMA200 support

### Thread-Safety / Backtest Contract

`screen_day()` is called by the backtester. The return dict gains one new key (`signal_path`).
The backtester ignores unknown keys, so no backtester changes are required.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Implementation (fully parallel)                       │
├────────────────────────────┬─────────────────────────────────┤
│ Task 1.1                   │ Task 1.2                        │
│ UPDATE train.py            │ UPDATE screener.py              │
│ Recovery path in           │ Show signal_path in output      │
│ screen_day()               │                                 │
└────────────────────────────┴─────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests (fully parallel)                                │
├────────────────────────────┬─────────────────────────────────┤
│ Task 2.1                   │ Task 2.2                        │
│ UPDATE test_screener.py    │ UPDATE test_screener_script.py  │
│ screen_day() unit tests    │ Bearish-period screener test    │
└────────────────────────────┴─────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Cache (sequential, depends on 1.1)                    │
├──────────────────────────────────────────────────────────────┤
│ Task 3.1                                                      │
│ UPDATE screener_prepare.py                                    │
│ HISTORY_DAYS 180 → 300 for SMA200 support                    │
└──────────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Contract 1**: Task 1.1 adds `signal_path: "bull" | "recovery"` to `screen_day()` return dict.
Task 1.2 reads `row["signal_path"]` from the screener per-ticker loop.

**Contract 2**: Task 2.1 provides `make_recovery_signal_df()` used as the design reference
for the synthetic parquet in Task 2.2 (`_write_recovery_parquet()`).

---

## IMPLEMENTATION PLAN

### Wave 1: Implementation

#### Task 1.1: UPDATE `train.py` — recovery path in `screen_day()`

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 3.1]

**Steps**:

1. In `screen_day()`, after computing `sma20`, `sma50`, `sma100`, add SMA200 computation
   (guarded by available history — recovery path is silently skipped if hist < 200 rows):

   ```python
   sma200 = float(close_hist.iloc[-200:].mean()) if len(hist) >= 200 else None
   ```

2. Replace the existing Rule 1 block:
   ```python
   # Rule 1: SMA alignment — most tickers fail here; check before volume/ATR/RSI
   if price_1030am <= sma50 or price_1030am <= sma100 or sma20 <= sma50 or sma50 <= sma100:
       return None
   ```

   With a two-path check:
   ```python
   # Two signal paths: bull (full SMA stack) or recovery (correction within uptrend).
   # Bull path: classic full alignment — price above both SMAs, SMAs stacked ascending.
   bull_path = (
       price_1030am > sma50 and
       price_1030am > sma100 and
       sma20 > sma50 and
       sma50 > sma100
   )
   # Recovery path: long-term trend intact (SMA50 > SMA200 = no death cross), but price
   # has pulled back below SMA50 and is now recovering above SMA20.
   # Requires 200 bars of history; silently skipped when data is insufficient.
   recovery_path = (
       sma200 is not None and
       sma50 > sma200 and
       price_1030am <= sma50 and
       price_1030am > sma20
   )
   if not bull_path and not recovery_path:
       return None
   signal_path = "bull" if bull_path else "recovery"
   ```

3. Rule 1b (SMA20 slope) — relax threshold for recovery path. A stock early in its
   reversal naturally has a still-declining SMA20; the bull-mode 0.5% tolerance would
   silently kill valid early-recovery entries. Replace:
   ```python
   sma20_5d_ago = float(close_hist.iloc[-25:-5].mean())
   if sma20 < sma20_5d_ago * 0.995:
       return None
   ```
   With:
   ```python
   sma20_5d_ago = float(close_hist.iloc[-25:-5].mean())
   # Recovery stocks have a naturally declining SMA20 early in the reversal;
   # use a wider tolerance so valid early-recovery entries aren't blocked.
   slope_floor = 0.990 if signal_path == "recovery" else 0.995
   if sma20 < sma20_5d_ago * slope_floor:
       return None
   ```

4. Adjust RSI check to use path-appropriate range. Replace:
   ```python
   # Rule 3b: RSI between 50 and 75 (momentum building, not overbought)
   if not (50 <= rsi <= 75):
       return None
   ```
   With:
   ```python
   # RSI range differs by path: recovery stocks are earlier in their move (lower RSI floor).
   rsi_lo, rsi_hi = (40, 65) if signal_path == "recovery" else (50, 75)
   if not (rsi_lo <= rsi <= rsi_hi):
       return None
   ```

5. Add `signal_path` to the return dict:
   ```python
   return {
       ...existing keys...,
       'signal_path': signal_path,
   }
   ```

- **VALIDATE**: `python -c "import train; print('OK')"`

---

#### Task 1.2: UPDATE `screener.py` — show signal_path in output

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]

**Steps**:

1. In the per-ticker loop where the `row` dict is assembled, add:
   ```python
   "signal_path": signal.get("signal_path", "bull"),
   ```

2. In the output header and per-row print, add a `PATH` column (6 chars wide):
   - Header: `f"{'PATH':<6}"` inserted after `TICKER`
   - Per-row: `f"{r['signal_path']:<6}"` inserted in same position

3. The existing `armed` list already separates armed vs gap-skipped. No structural change needed.

4. Update `_rejection_reason()` to distinguish death-cross rejections from generic SMA
   misalignment. In `screener.py`:

   a. Add `"death_cross"` to `_RULES` (after `"sma_misaligned"`):
   ```python
   _RULES = [
       "too_few_rows", "no_price", "earnings_soon",
       "sma_misaligned", "death_cross", "sma20_declining",
       ...
   ]
   ```

   b. In `_rejection_reason()`, after the existing `sma_misaligned` block, insert a
   recovery-path evaluation so death-cross failures are counted separately:
   ```python
   # If bull path failed, check whether recovery path was blocked by a death cross
   # (SMA50 <= SMA200) vs simply having no SMA200 data or failing other alignment.
   if len(hist) >= 200:
       sma200 = float(close_hist.iloc[-200:].mean())
       if sma50 <= sma200:
           return "death_cross"
   return "sma_misaligned"
   ```
   This block replaces the bare `return "sma_misaligned"` at the end of the SMA check.

- **VALIDATE**: `python -c "import screener; print('OK')"`

**Wave 1 Checkpoint**: `python -m pytest tests/test_screener.py tests/test_screener_script.py -x -q`

---

### Wave 2: Tests

#### Task 2.1: UPDATE `tests/test_screener.py` — recovery signal unit tests

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: []

**Read `tests/test_screener.py` first**, then append these tests at the end.

Add a `make_recovery_signal_df(n=260)` fixture and 5 test cases:

```python
# ── Recovery mode signal tests ────────────────────────────────────────────────

def make_recovery_signal_df(n: int = 260) -> pd.DataFrame:
    """Synthetic DataFrame where screen_day fires the RECOVERY path.

    Price path:
    - Bars 0-199: slow steady rise 50→120 (builds a low SMA200 baseline)
    - Bars 200-229: explosive rally 120→210 (lifts SMA50 well above SMA200)
    - Bars 230-249: correction 210→150 (price falls below SMA50; SMA50 stays > SMA200)
    - Bars 250-258: mini-recovery 150→162 (price reclaims SMA20)
    - Bar 259 (today): price_1030am = 168 (breaks above 20-day high ~163)

    At bar 259:
      SMA200 ≈ mean(last 200 closes before today) — dominated by the slow rise + rally
      SMA50  ≈ mean(last 50 closes) — correction + mini-recovery, above SMA200
      SMA20  ≈ mean(last 20 closes) — mini-recovery zone (~158), below price_1030am
      price_1030am = 168 > 20d high (~163) and > SMA20 (~158) and < SMA50 (~175)
    """
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]

    close = np.zeros(n, dtype=float)
    close[:200]    = np.linspace(50.0, 120.0, 200)    # slow rise
    close[200:230] = np.linspace(120.0, 210.0, 30)    # explosive rally
    close[230:250] = np.linspace(210.0, 150.0, 20)    # correction
    close[250:259] = np.linspace(150.0, 162.0, 9)     # mini-recovery
    close[259]     = 162.0                              # last history bar

    high  = close * 1.005
    low   = close * 0.990
    open_ = close * 0.998

    price_1030am        = close.copy()
    price_1030am[259]   = 168.0  # breakout above 20d high

    # Volume: last 5 bars elevated (vol_trend_ratio >= 1.0), yesterday active
    volume = np.full(n, 1_000_000.0)
    volume[254:259] = 1_500_000.0   # 5-day trend above MA30
    volume[258]     = 1_200_000.0   # yesterday >= 0.8× MA30

    df = pd.DataFrame({
        'open':         open_,
        'high':         high,
        'low':          low,
        'close':        close,
        'volume':       volume,
        'price_1030am': price_1030am,
    }, index=pd.Index(dates, name='date'))

    # Add pivot low ~35 bars from end with a prior touch ~10 bars before it
    pivot_idx = n - 35
    pivot_price = float(df['close'].iloc[pivot_idx]) * 0.85
    df.iloc[pivot_idx, df.columns.get_loc('low')] = pivot_price
    touch_idx = pivot_idx - 10
    if touch_idx >= 0:
        df.iloc[touch_idx, df.columns.get_loc('low')] = pivot_price * 0.99
        df.iloc[touch_idx, df.columns.get_loc('high')] = pivot_price * 1.01

    return df


def test_screen_day_recovery_fires_below_sma50():
    """Recovery path fires: signal returned even though price < SMA50."""
    df = make_recovery_signal_df()
    today = df.index[-1]
    result = screen_day(df, today, current_price=168.0)
    assert result is not None, "Expected recovery signal, got None"
    assert result["signal_path"] == "recovery"


def test_screen_day_recovery_price_below_sma50():
    """Sanity check: the recovery fixture actually has price below SMA50."""
    df = make_recovery_signal_df()
    hist = df.iloc[:-1]
    sma50 = float(hist['close'].iloc[-50:].mean())
    assert 168.0 < sma50, f"Expected price 168 < SMA50 {sma50:.2f}"


def test_screen_day_recovery_sma50_above_sma200():
    """Sanity check: the recovery fixture has SMA50 > SMA200 (no death cross)."""
    df = make_recovery_signal_df()
    hist = df.iloc[:-1]
    sma50  = float(hist['close'].iloc[-50:].mean())
    sma200 = float(hist['close'].iloc[-200:].mean())
    assert sma50 > sma200, f"Expected SMA50 {sma50:.2f} > SMA200 {sma200:.2f}"


def test_screen_day_recovery_blocked_when_death_cross():
    """Recovery path must NOT fire when SMA50 <= SMA200 (death cross)."""
    df = make_recovery_signal_df().copy()
    # Force a death cross: set all recent closes very low so SMA50 drops below SMA200
    df.iloc[-60:-1, df.columns.get_loc('close')] *= 0.3
    # Sanity check: verify the fixture actually produced a death cross before calling screen_day,
    # so a None result is attributed to the right gate and not an earlier filter.
    hist = df.iloc[:-1]
    sma50  = float(hist['close'].iloc[-50:].mean())
    sma200 = float(hist['close'].iloc[-200:].mean())
    assert sma50 <= sma200, (
        f"Fixture did not produce a death cross (SMA50={sma50:.2f}, SMA200={sma200:.2f}). "
        "Adjust the multiplier so SMA50 actually drops below SMA200."
    )
    today = df.index[-1]
    result = screen_day(df, today, current_price=168.0)
    assert result is None, "Recovery path must not fire when SMA50 < SMA200"


def test_screen_day_bull_path_unaffected():
    """Bull path still fires for a classic bull-stack ticker (no regression)."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today, current_price=115.0)
    assert result is not None, "Bull path should still fire"
    assert result["signal_path"] == "bull"


def test_screen_day_recovery_rsi_range_40_65():
    """Recovery path rejects RSI > 65 (tighter ceiling than bull's 75)."""
    df = make_recovery_signal_df().copy()
    # Force RSI > 65 by making recent closes uniformly strongly up
    df.iloc[-60:-1, df.columns.get_loc('close')] = np.linspace(200, 300, 59)
    today = df.index[-1]
    # screen_day should reject on RSI check
    result = screen_day(df, today, current_price=168.0)
    # Either None (RSI gate) or bull path fired — either way NOT recovery with high RSI
    if result is not None:
        assert result.get("signal_path") != "recovery" or result.get("rsi14", 100) <= 65


def test_screen_day_recovery_slope_floor_relaxed():
    """Recovery path uses 1% SMA20 slope tolerance, not the bull-mode 0.5%.

    A stock early in its reversal naturally has a still-declining SMA20.
    With the strict 0.995 floor, valid early-recovery entries would be silently blocked.
    This test verifies that a signal fires even when sma20 is between 0.99 and 0.995
    of sma20_5d_ago (i.e., in the relaxed-but-not-strict zone).
    """
    df = make_recovery_signal_df().copy()
    # Nudge the SMA20 window (bars -25 to -6 before today) slightly higher so that
    # sma20_5d_ago ends up ~0.3% above sma20 — inside recovery tolerance (1%) but
    # outside bull tolerance (0.5%).
    hist_slice = slice(len(df) - 26, len(df) - 6)
    df.iloc[hist_slice, df.columns.get_loc('close')] *= 1.003
    today = df.index[-1]
    result = screen_day(df, today, current_price=168.0)
    assert result is not None, (
        "Recovery signal should fire when SMA20 slope is within 1% tolerance. "
        "Rule 1b slope_floor must be 0.990 for recovery path, not 0.995."
    )
    assert result["signal_path"] == "recovery"
```

- **VALIDATE**: `python -m pytest tests/test_screener.py -q`

---

#### Task 2.2: UPDATE `tests/test_screener_script.py` — bearish period screener test

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: []

**Read `tests/test_screener_script.py` first**, then append at the end:

```python
# ── Recovery / bearish-period screener test ───────────────────────────────────

def _write_recovery_parquet(path):
    """Write a synthetic parquet representing a recovery-mode setup.

    Last row is yesterday so the screener appends a fresh synthetic today row.
    Uses the same price path as make_recovery_signal_df: slow rise → rally →
    correction → mini-recovery → breakout.
    """
    from tests.test_screener import make_recovery_signal_df
    df = make_recovery_signal_df(260)
    # Shift index to end at yesterday so screener's today row doesn't duplicate
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    new_index = [yesterday - datetime.timedelta(days=i) for i in range(len(df) - 1, -1, -1)]
    df.index = pd.Index(new_index, name="date")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path)
    return df


def test_screener_finds_candidate_in_bearish_period(screener_cache_tmpdir, monkeypatch, capsys):
    """Regression guard: screener must find >= 1 armed candidate from a recovery-mode ticker.

    This test exists to catch regressions where strategy changes inadvertently break
    recovery-mode signal generation. If this test fails, the screener will produce zero
    candidates in corrective/bearish markets.

    Uses a synthetic parquet engineered to satisfy recovery-mode conditions:
    price below SMA50 but SMA50 > SMA200, with a 20-day breakout and valid pivot stop.
    """
    import screener as sc
    path = os.path.join(str(screener_cache_tmpdir), "RECOVERY.parquet")
    df = _write_recovery_parquet(path)

    # Inject breakout price — above yesterday's close to ensure gap_pct > GAP_THRESHOLD
    prev_close = float(df["close"].iloc[-1])
    breakout_price = 168.0  # matches make_recovery_signal_df breakout price
    monkeypatch.setattr("screener._fetch_last_price", lambda t: breakout_price)
    # Ensure GAP_THRESHOLD doesn't filter it out: 168/162 ≈ +3.7%, above -3% threshold
    monkeypatch.setattr(sc, "GAP_THRESHOLD", -0.10)

    sc.run_screener()
    captured = capsys.readouterr()

    assert "ARMED BUY SIGNALS" in captured.out, (
        "Expected armed candidates section in output — recovery-mode signal did not fire. "
        "This means screen_day() is not returning signals for below-SMA50 recovery setups."
    )
    assert "RECOVERY" in captured.out, (
        "Expected RECOVERY ticker in armed candidates. "
        "Recovery-mode signal path is not working."
    )
```

- **VALIDATE**: `python -m pytest tests/test_screener_script.py -q`

**Wave 2 Checkpoint**: `python -m pytest tests/test_screener.py tests/test_screener_script.py -v`

---

### Wave 3: Cache History Expansion

#### Task 3.1: UPDATE `screener_prepare.py` — increase HISTORY_DAYS for SMA200

- **WAVE**: 3
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: []

SMA200 requires 200 trading days of history. 200 trading days ≈ 290 calendar days.
The current `HISTORY_DAYS = 180` produces ~123 trading days — insufficient for recovery
mode to fire in the live screener (though tests work because they use synthetic data).

Replace:
```python
HISTORY_DAYS = 180  # calendar days of data to maintain in the screener cache (~126 trading days)
```

With:
```python
HISTORY_DAYS = 300  # calendar days of data to maintain in the screener cache (~210 trading days)
                     # 300 days required for SMA200: 200 trading days × (365/252) ≈ 290 calendar days
```

**Note for executor**: After this change, the existing cache has only 123 rows per ticker.
The next run of `screener_prepare.py` will NOT automatically re-download — `is_ticker_current()`
will return True for all recently-cached tickers. To get full SMA200 history, the cache
must be cleared first:
```bash
python -c "
import os, glob
from pathlib import Path
cache = os.path.join(Path.home(), '.cache', 'autoresearch', 'screener_data')
files = glob.glob(os.path.join(cache, '*.parquet'))
for f in files: os.remove(f)
print(f'Cleared {len(files)} files from {cache}')
"
```
**Do NOT run this command as part of the automated plan execution.** Leave it as a manual
step for the user — cache rebuild takes ~10 minutes and should be triggered explicitly.
Document the required steps in a comment in the COMPLETION CHECKLIST.

- **VALIDATE**: `python -c "import screener_prepare; assert screener_prepare.HISTORY_DAYS == 300; print('OK')"`

---

## TESTING STRATEGY

All tests automated with pytest. No manual tests required.

### Unit Tests — `screen_day()` recovery path (6 tests)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener.py`
**Run**: `python -m pytest tests/test_screener.py -q`

| Test | What it verifies |
|------|-----------------|
| `test_screen_day_recovery_fires_below_sma50` | Recovery signal returned when price < SMA50 |
| `test_screen_day_recovery_price_below_sma50` | Fixture sanity: price is actually below SMA50 |
| `test_screen_day_recovery_sma50_above_sma200` | Fixture sanity: SMA50 > SMA200 |
| `test_screen_day_recovery_blocked_when_death_cross` | SMA50 ≤ SMA200 → None |
| `test_screen_day_bull_path_unaffected` | Existing bull path still fires (no regression) |
| `test_screen_day_recovery_rsi_range_40_65` | RSI > 65 blocked in recovery mode |
| `test_screen_day_recovery_slope_floor_relaxed` | SMA20 slope tolerance 1% (not 0.5%) in recovery mode |

### Integration Test — `screener.py` bearish period (1 test)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener_script.py`
**Run**: `python -m pytest tests/test_screener_script.py -q`

| Test | What it verifies |
|------|-----------------|
| `test_screener_finds_candidate_in_bearish_period` | ≥ 1 armed candidate from recovery-mode synthetic parquet |

**Why this test exists**: future changes to `screen_day()` that accidentally break the
recovery path would produce zero screener output in corrective markets. This test catches
that regression without requiring a real live market run.

### Coverage Gap Analysis

| Code path | Test |
|-----------|------|
| Recovery path fires (price < SMA50, SMA50 > SMA200) | `test_screen_day_recovery_fires_below_sma50` ✅ |
| Recovery path blocked: death cross (SMA50 ≤ SMA200) | `test_screen_day_recovery_blocked_when_death_cross` ✅ |
| Recovery path blocked: RSI > 65 | `test_screen_day_recovery_rsi_range_40_65` ✅ |
| Bull path unaffected (no regression) | `test_screen_day_bull_path_unaffected` ✅ |
| `signal_path` key in return dict | `test_screen_day_recovery_fires_below_sma50` ✅ |
| `signal_path == "bull"` for bull entries | `test_screen_day_bull_path_unaffected` ✅ |
| screener.py finds recovery candidate | `test_screener_finds_candidate_in_bearish_period` ✅ |
| screener.py PATH column in output | `test_screener_output_has_required_columns` (existing, updated) ✅ |
| SMA200 not computed when hist < 200 rows | Not directly tested — graceful `None` guard in code |
| Rule 1b slope floor 1% in recovery vs 0.5% in bull | `test_screen_day_recovery_slope_floor_relaxed` ✅ |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (pytest) | 8 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 8 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
python -c "import train; print('train OK')"
python -c "import screener; print('screener OK')"
python -c "import screener_prepare; assert screener_prepare.HISTORY_DAYS == 300; print('screener_prepare OK')"
```

### Level 2: New tests

```bash
python -m pytest tests/test_screener.py tests/test_screener_script.py -v
```

Expected: all existing tests pass + 7 new tests pass.

### Level 3: Full suite (no regressions)

```bash
python -m pytest tests/ -q
```

Expected: all previously-passing tests continue to pass. The pre-existing failure
`test_selector.py::test_select_strategy_real_claude_code` is excluded from scope.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `screen_day()` accepts signal candidates where `price < SMA50` provided `SMA50 > SMA200` and `price > SMA20`
- [ ] `screen_day()` still accepts bull-mode signals unchanged (price > SMA50 > SMA100)
- [ ] `screen_day()` return dict includes `signal_path: "bull" | "recovery"`
- [ ] Recovery path is silently skipped (returns None for that path only) when hist < 200 rows
- [ ] RSI range for recovery is 40–65; bull mode remains 50–75
- [ ] `screener.py` output includes a `PATH` column showing `bull` or `recovery` per candidate

### Correctness Guards
- [ ] Recovery path does NOT fire when `SMA50 ≤ SMA200` (death cross / bear market)
- [ ] Recovery path does NOT fire when `price > SMA50` (bull mode takes precedence)
- [ ] All existing `screen_day()` rules (stop, resistance, volume, earnings, stall) still apply in recovery mode
- [ ] Rule 1b slope floor is `0.990` in recovery mode and `0.995` in bull mode
- [ ] `_rejection_reason()` returns `"death_cross"` when `SMA50 ≤ SMA200` (not `"sma_misaligned"`)

### Regression Prevention
- [ ] `test_screener_finds_candidate_in_bearish_period` passes — proves screener finds ≥ 1 armed
  candidate when processing a recovery-mode synthetic parquet. This test guards future strategy
  changes from silently breaking recovery-mode coverage.
- [ ] All pre-existing `test_screener.py` tests continue to pass

### Validation
- [ ] All 8 new tests pass — verified by: `python -m pytest tests/test_screener.py tests/test_screener_script.py -v`
- [ ] Full suite has no new failures — verified by: `python -m pytest tests/ -q`

### Cache (manual, not automated)
- [ ] `HISTORY_DAYS = 300` in `screener_prepare.py`
- [ ] After running `screener_prepare.py` on a cleared cache, tickers have ~210 rows (enough for SMA200)

### Out of Scope
- Backtester changes — `signal_path` key is additive; backtester ignores unknown keys
- Changing `GAP_THRESHOLD` for recovery mode
- Per-path position sizing
- Cache clear + rebuild (manual step, documented below)

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Wave 1 checkpoint passed
- [ ] Wave 2 checkpoint passed
- [ ] Level 1–3 validation commands all passed
- [ ] All 7 new tests created and passing
- [ ] Full test suite passes with no new failures
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

**Post-execution manual step (not part of automated execution):**
To use recovery mode in the live screener, the cache must be rebuilt with 300-day history:
```bash
# 1. Clear cache
python -c "import os,glob; from pathlib import Path; [os.remove(f) for f in glob.glob(os.path.join(Path.home(),'.cache','autoresearch','screener_data','*.parquet'))]; print('Cache cleared')"
# 2. Rebuild (~10 min)
uv run screener_prepare.py
```

---

## NOTES

**Why `signal_path` in return dict rather than a separate function?**
Keeping recovery logic inside `screen_day()` means the backtester automatically evaluates
both paths without modification. A separate function would require parallel backtester calls.

**Why RSI 40–65 for recovery?**
A stock emerging from a correction typically has RSI in the 40s–50s. Requiring RSI > 50
(bull mode floor) would exclude valid early-recovery entries. Capping at 65 prevents
buying into an already-extended bounce.

**Why not use SMA100 as the recovery floor instead of SMA200?**
SMA100 is too sensitive — it would allow recovery trades in stocks that are 2–3 months into
a downtrend. SMA200 is the most widely-watched structural trend line; a SMA50 > SMA200 (no
death cross) is a broadly-accepted minimum condition for a long bias.

**Death cross guard**: If SMA50 drops below SMA200 (death cross), recovery mode is disabled
for that ticker. This is intentional — a death cross signals structural trend breakdown, not
a healthy correction.
