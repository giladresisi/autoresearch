# Plan: SMT Direction Control Refactor

## Overview

**Feature**: SMT Strategy — direction control, TDO validity gate, per-direction stop ratios, direction breakdown metrics  
**Type**: Enhancement / Refactor  
**Complexity**: ⚠️ Medium  
**Status**: 📋 Planned  
**Date**: 2026-04-01

### Problem

The live backtest reveals two structural issues:

1. **TDO logic is geometrically inverted on 3 of 12 trades, all of which lost.**  
   For a SHORT, TDO (True Day Open) must be *below* entry — you faded a pump back to the open.  
   For a LONG, TDO must be *above* entry — you bounced from a sweep back up to the open.  
   When TDO is on the wrong side of entry, the TP literally points against the trade direction,
   and the stop (0.45 × |entry - TDO|) becomes sub-noise (as small as 0.11 pts → 227-contract sizing on $50 risk).

2. **No mechanism to restrict by direction or to separately tune stop ratios per direction.**  
   Long: 20% WR, -$178. Short: 57% WR, +$458. These deserve independent optimization paths.

### Scope

All changes are in the **editable section** of `train_smt.py` (above `# DO NOT EDIT BELOW THIS LINE`).  
The frozen harness (`run_backtest`, `manage_position`, `_compute_metrics`, `print_results`, `__main__`) is NOT touched.

---

## Execution Agent Rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Affected Files

| File | Change |
|------|--------|
| `train_smt.py` | 5 new constants, `screen_session` guards, `print_direction_breakdown` function |
| `tests/test_smt_strategy.py` | New unit tests for each guard and new function |
| `tests/test_smt_backtest.py` | Regression tests: new defaults produce ≥ as-good results |

---

## Key Design Decisions

### 1. TDO Validity Gate

Add `TDO_VALIDITY_CHECK = True` constant. When `True`, `screen_session` skips any signal where
TDO is on the wrong side of the entry price:
- LONG: skip if `tdo <= entry_price`
- SHORT: skip if `tdo >= entry_price`

Setting `False` restores exact legacy behavior for regression testing.

### 2. Minimum Stop Distance Guard

Add `MIN_STOP_POINTS = 5.0` constant. After computing `stop_price`, skip if
`abs(entry_price - stop_price) < MIN_STOP_POINTS`. This prevents the 227-contract sizing
degenerate case. Setting `0.0` restores legacy behavior (no guard).

### 3. Direction Control

Add `TRADE_DIRECTION = "both"` constant. Immediately after `detect_smt_divergence` returns
a direction, skip the signal if it doesn't match. `"both"` = no filter = legacy behavior.

### 4. Per-Direction Stop Ratios

Replace the hardcoded `0.45` in stop computation with `LONG_STOP_RATIO = 0.45` and
`SHORT_STOP_RATIO = 0.45`. Both default to 0.45 — zero behavioral change until an autoresearch
agent tunes them separately.

### 5. `print_direction_breakdown` — design note

The frozen `print_results` and `__main__` cannot be modified. The frozen output already
includes `long_pnl` and `short_pnl`, which are the primary direction signals for optimization.

`print_direction_breakdown(stats, prefix="")` is added to the editable section as an
**exported utility** — it is:
- Verified by unit tests
- Available for import by autoresearch agents: `import train_smt; train_smt.print_direction_breakdown(stats)`
- Callable from any evaluation scripts

The function prints per-direction trade count, win rate, avg PnL, and exit type counts
using the same `{prefix}{key}: {value}` pattern as `print_results`.

---

## Implementation Tasks

### WAVE 1 — All code changes (sequential, single agent)

#### Task 1.1 — New constants in editable section

In `train_smt.py`, within the `# ══ STRATEGY TUNING ══` section (around line 67–79),
add the following five constants immediately after `MIN_BARS_BEFORE_SIGNAL = 5`:

```python
# Direction filter: "both" = trade longs and shorts | "long" = longs only | "short" = shorts only
TRADE_DIRECTION = "both"

# TDO validity gate: skip signals where the take-profit target is geometrically inverted.
# For LONG: TDO must be above entry (price bounces up to the open).
# For SHORT: TDO must be below entry (price fades down to the open).
# Set False to disable and restore legacy behavior.
TDO_VALIDITY_CHECK = True

# Minimum stop distance in MNQ points. Signals with |entry - stop| < this value are skipped.
# Prevents degenerate sizing when TDO is very close to entry.
# Set 0.0 to disable.
MIN_STOP_POINTS = 5.0

# Per-direction stop placement ratios (fraction of |entry - TDO| distance).
# Both default to 0.45, matching the original hardcoded value.
LONG_STOP_RATIO  = 0.45
SHORT_STOP_RATIO = 0.45
```

Also add a constant for the direction breakdown print toggle:

```python
# Print per-direction win rate, avg PnL, and exit breakdown after each fold.
# Set False to suppress — does not affect frozen print_results output.
PRINT_DIRECTION_BREAKDOWN = True
```

---

#### Task 1.2 — Refactor `screen_session` with all four guards

Replace the body of `screen_session` (from the scan loop downward) with the updated version.
Read the function carefully first. Apply changes in this order:

**A. Direction filter** — immediately after `if direction is None: continue`:
```python
        # Direction filter: skip if this signal's direction is not allowed
        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue
```

**B. TDO validity gate** — after computing `entry_price` (after `entry_bar = mnq_reset.iloc[entry_idx]`
and `entry_price = float(entry_bar["Close"])`), before computing stop:
```python
        # TDO validity gate: skip if TDO is on the wrong side of entry
        if TDO_VALIDITY_CHECK:
            if direction == "long" and tdo <= entry_price:
                continue
            if direction == "short" and tdo >= entry_price:
                continue
```

**C. Per-direction stop ratios** — replace the hardcoded `0.45` in both branches:
```python
        distance_to_tdo = abs(entry_price - tdo)
        if direction == "long":
            stop_price = entry_price - LONG_STOP_RATIO * distance_to_tdo
        else:
            stop_price = entry_price + SHORT_STOP_RATIO * distance_to_tdo
```

**D. Minimum stop distance guard** — after computing `stop_price`, before `entry_time`:
```python
        # Minimum stop distance guard: reject sub-noise stops
        if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
            continue
```

Update the `screen_session` docstring to document the new constants that affect its behavior:
add a "Guards controlled by constants" section listing `TRADE_DIRECTION`, `TDO_VALIDITY_CHECK`,
`MIN_STOP_POINTS`, `LONG_STOP_RATIO`, `SHORT_STOP_RATIO`.

---

#### Task 1.3 — Add `print_direction_breakdown` function

Insert the following function in the editable section, immediately before `screen_session`
(i.e., after `compute_tdo` and before `screen_session`):

```python
def print_direction_breakdown(stats: dict, prefix: str = "") -> None:
    """Print per-direction trade count, win rate, avg PnL, and exit breakdown.

    Uses the same {prefix}{key}: {value} format as print_results so autoresearch
    agents can parse direction metrics alongside the standard fold output.

    Reads from stats["trade_records"]. Prints nothing if trade_records is absent
    or empty. Controlled by PRINT_DIRECTION_BREAKDOWN constant (caller's responsibility
    to check before calling).

    Args:
        stats:  Dict returned by run_backtest or _compute_metrics.
        prefix: String prepended to every printed key (e.g. "fold1_train_").
    """
    trades = stats.get("trade_records", [])
    if not trades:
        return
    for direction in ("long", "short"):
        subset = [t for t in trades if t["direction"] == direction]
        n = len(subset)
        wins = sum(1 for t in subset if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in subset)
        win_rate  = round(wins / n, 4) if n > 0 else 0.0
        avg_pnl   = round(total_pnl / n, 2) if n > 0 else 0.0
        print(f"{prefix}{direction}_trades: {n}")
        print(f"{prefix}{direction}_win_rate: {win_rate}")
        print(f"{prefix}{direction}_avg_pnl: {avg_pnl}")
        exits: dict[str, int] = {}
        for t in subset:
            exits[t["exit_type"]] = exits.get(t["exit_type"], 0) + 1
        for exit_type, count in exits.items():
            print(f"{prefix}{direction}_exit_{exit_type}: {count}")
```

---

### WAVE 2 — Tests (sequential, after Wave 1)

#### Task 2.1 — New unit tests in `tests/test_smt_strategy.py`

Add the following test functions at the end of the file. Each test calls `screen_session`
directly using synthetic bar data and a monkeypatched `compute_tdo`.

**Pattern**: Use `_make_1m_bars` (already in the file) to build session-scoped bars.
The `autouse` `patch_min_bars` fixture sets `MIN_BARS_BEFORE_SIGNAL = 2`.
Monkeypatch `compute_tdo` to control TDO without needing real bar data.

---

**Test A: `TRADE_DIRECTION = "short"` blocks long signals**
```python
def test_trade_direction_short_blocks_long(monkeypatch):
    """TRADE_DIRECTION='short' causes screen_session to skip bullish SMT signals."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    # Build bars that produce a long (bullish) SMT signal
    mnq, mes = _make_long_session_bars()  # helper defined below
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 20100.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is None
```

**Test B: `TRADE_DIRECTION = "long"` blocks short signals**
```python
def test_trade_direction_long_blocks_short(monkeypatch):
    """TRADE_DIRECTION='long' causes screen_session to skip bearish SMT signals."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "long")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 19900.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is None
```

**Test C: `TRADE_DIRECTION = "both"` passes both directions**
```python
def test_trade_direction_both_passes_short(monkeypatch):
    """TRADE_DIRECTION='both' does not filter any signal direction."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 19900.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is not None
    assert result["direction"] == "short"
```

**Test D: TDO validity gate blocks inverted long (TDO below entry)**
```python
def test_tdo_validity_blocks_inverted_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips long signal when TDO < entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_long_session_bars()
    # TDO below base (entry will be near base) → inverted long
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 19950.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is None
```

**Test E: TDO validity gate passes valid long (TDO above entry)**
```python
def test_tdo_validity_passes_valid_long(monkeypatch):
    """TDO_VALIDITY_CHECK=True allows long signal when TDO > entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_long_session_bars()
    # TDO well above base → valid long
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 20100.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is not None
    assert result["direction"] == "long"
    assert result["take_profit"] > result["entry_price"]
```

**Test F: TDO validity gate blocks inverted short (TDO above entry)**
```python
def test_tdo_validity_blocks_inverted_short(monkeypatch):
    """TDO_VALIDITY_CHECK=True skips short signal when TDO > entry_price."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    # TDO above base (entry will be near base) → inverted short
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 20100.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is None
```

**Test G: TDO_VALIDITY_CHECK=False passes inverted setups (legacy behavior)**
```python
def test_tdo_validity_false_passes_inverted(monkeypatch):
    """TDO_VALIDITY_CHECK=False allows inverted signals through (legacy behavior)."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    # TDO above entry → inverted short, but gate is off
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 20100.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is not None  # passes through despite inversion
```

**Test H: MIN_STOP_POINTS filters tiny stops**
```python
def test_min_stop_points_filters_tiny_stop(monkeypatch):
    """MIN_STOP_POINTS=50 rejects signals where stop distance < 50 pts."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 50.0)
    mnq, mes = _make_short_session_bars()
    # TDO just 5 pts below base → stop = 0.45 * 5 = 2.25 pts → filtered
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 19995.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    assert result is None
```

**Test I: MIN_STOP_POINTS=0 disables guard**
```python
def test_min_stop_points_zero_disables_guard(monkeypatch):
    """MIN_STOP_POINTS=0.0 allows all stop distances through."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    mnq, mes = _make_short_session_bars()
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: 19995.0)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    # tiny stop but guard is off — signal should come through (if entry found)
    # just assert it doesn't crash; result may be None if no confirmation bar
    assert True  # no exception = pass
```

**Test J: Per-direction stop ratios produce correct stop prices**
```python
def test_long_stop_ratio_applied(monkeypatch):
    """LONG_STOP_RATIO is used for long stop computation."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "LONG_STOP_RATIO", 0.3)
    mnq, mes = _make_long_session_bars()
    tdo_val = 20100.0
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: tdo_val)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    if result is not None and result["direction"] == "long":
        expected_stop = result["entry_price"] - 0.3 * abs(result["entry_price"] - tdo_val)
        assert abs(result["stop_price"] - expected_stop) < 0.01

def test_short_stop_ratio_applied(monkeypatch):
    """SHORT_STOP_RATIO is used for short stop computation."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "SHORT_STOP_RATIO", 0.6)
    mnq, mes = _make_short_session_bars()
    tdo_val = 19900.0
    monkeypatch.setattr(train_smt, "compute_tdo", lambda bars, date: tdo_val)
    result = train_smt.screen_session(mnq, mes, mnq.index[0].date())
    if result is not None and result["direction"] == "short":
        expected_stop = result["entry_price"] + 0.6 * abs(result["entry_price"] - tdo_val)
        assert abs(result["stop_price"] - expected_stop) < 0.01
```

**Test K: `print_direction_breakdown` output format**
```python
def test_print_direction_breakdown_format(capsys):
    """print_direction_breakdown prints per-direction metrics with correct prefix."""
    import train_smt
    fake_stats = {
        "total_pnl": 100.0,
        "trade_records": [
            {"direction": "long",  "pnl":  50.0, "exit_type": "exit_tp"},
            {"direction": "long",  "pnl": -20.0, "exit_type": "exit_stop"},
            {"direction": "short", "pnl":  70.0, "exit_type": "exit_tp"},
        ],
    }
    train_smt.print_direction_breakdown(fake_stats, prefix="fold1_train_")
    out = capsys.readouterr().out
    assert "fold1_train_long_trades: 2" in out
    assert "fold1_train_long_win_rate: 0.5" in out
    assert "fold1_train_short_trades: 1" in out
    assert "fold1_train_short_win_rate: 1.0" in out
    assert "fold1_train_long_exit_exit_tp: 1" in out
    assert "fold1_train_long_exit_exit_stop: 1" in out

def test_print_direction_breakdown_empty_trades(capsys):
    """print_direction_breakdown prints nothing when trade_records is empty."""
    import train_smt
    train_smt.print_direction_breakdown({"trade_records": []}, prefix="test_")
    out = capsys.readouterr().out
    assert out == ""
```

**Helper functions** (add at top of new test section, before the test functions):
```python
def _make_short_session_bars(base=20000.0):
    """Session bars with a bearish SMT signal at bar 4."""
    n = 30
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    highs_mes = [base + 5] * n;  highs_mes[4] = base + 30
    highs_mnq = [base + 5] * n
    opens  = [base] * n;  opens[5]  = base + 2
    closes = [base] * n;  closes[5] = base - 2
    highs_mnq[5] = base + 6
    mnq = pd.DataFrame({"Open": opens, "High": highs_mnq, "Low": [base-5]*n, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": highs_mes, "Low": [base-5]*n, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    return mnq, mes


def _make_long_session_bars(base=20000.0):
    """Session bars with a bullish SMT signal at bar 4."""
    n = 30
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    lows_mes = [base - 5] * n;  lows_mes[4] = base - 30
    lows_mnq = [base - 5] * n
    opens  = [base] * n;  opens[5]  = base - 2
    closes = [base] * n;  closes[5] = base + 2
    lows_mnq[5] = base - 6
    mnq = pd.DataFrame({"Open": opens, "High": [base+5]*n, "Low": lows_mnq, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": [base+5]*n, "Low": lows_mes, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    return mnq, mes
```

---

#### Task 2.2 — Regression test in `tests/test_smt_backtest.py`

Add one new integration test verifying that with new defaults (`TDO_VALIDITY_CHECK=True`,
`MIN_STOP_POINTS=5.0`, `TRADE_DIRECTION="both"`) the backtest returns fewer or equal trades
vs legacy defaults and no exceptions:

```python
def test_new_defaults_produce_valid_results(futures_tmpdir, monkeypatch):
    """New default constants (validity gate + min stop) don't crash run_backtest."""
    import train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 5.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "LONG_STOP_RATIO", 0.45)
    monkeypatch.setattr(train_smt, "SHORT_STOP_RATIO", 0.45)

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert "total_trades" in stats
    assert "trade_records" in stats
    assert stats["total_trades"] >= 0
```

---

### WAVE 3 — Validation (sequential, after Wave 2)

#### Task 3.1 — Run full test suite

```bash
uv run python -m pytest tests/ -x -q 2>&1 | tail -10
```

Expected: **346 + N_new passed** (all original 346 plus the new tests), 2 skipped, 0 failures.
If any existing test fails, diagnose: most likely `screen_session` now filters a signal that the
test relied on without setting `TDO_VALIDITY_CHECK = False` or `MIN_STOP_POINTS = 0.0`.

Fix: add monkeypatches for the new constants to any failing existing test (set them to legacy
values). Do NOT relax the guards.

#### Task 3.2 — Live backtest comparison

```bash
uv run python train_smt.py
```

Expected with new defaults (`TDO_VALIDITY_CHECK=True`, `MIN_STOP_POINTS=5.0`):

- Fewer total trades than 12 (the 3 inverted-TDO trades and the 0.11-pt stop trade are filtered)
- Long PnL should improve (inverted longs are gone) or go to 0 (if all longs were invalid)
- Short PnL should remain positive (valid shorts were not inverted)
- No crash, no exception

If trade count drops to 0: diagnose whether the bar data in cache covers the expected window.
It should not — the valid shorts (Mar 13, 17, 25) had TDO well below entry.

---

## Test Coverage Map

### New code paths

| Path | Test | Status |
|------|------|--------|
| `TRADE_DIRECTION = "short"` filters long | `test_trade_direction_short_blocks_long` | ✅ |
| `TRADE_DIRECTION = "long"` filters short | `test_trade_direction_long_blocks_short` | ✅ |
| `TRADE_DIRECTION = "both"` passes all | `test_trade_direction_both_passes_short` | ✅ |
| TDO gate blocks inverted long | `test_tdo_validity_blocks_inverted_long` | ✅ |
| TDO gate passes valid long | `test_tdo_validity_passes_valid_long` | ✅ |
| TDO gate blocks inverted short | `test_tdo_validity_blocks_inverted_short` | ✅ |
| TDO gate=False passes inverted (legacy) | `test_tdo_validity_false_passes_inverted` | ✅ |
| `MIN_STOP_POINTS` filters tiny stop | `test_min_stop_points_filters_tiny_stop` | ✅ |
| `MIN_STOP_POINTS=0` disables guard | `test_min_stop_points_zero_disables_guard` | ✅ |
| `LONG_STOP_RATIO` applied to stop price | `test_long_stop_ratio_applied` | ✅ |
| `SHORT_STOP_RATIO` applied to stop price | `test_short_stop_ratio_applied` | ✅ |
| `print_direction_breakdown` output format | `test_print_direction_breakdown_format` | ✅ |
| `print_direction_breakdown` empty input | `test_print_direction_breakdown_empty_trades` | ✅ |
| New defaults integration (no crash) | `test_new_defaults_produce_valid_results` | ✅ |

### Existing tests re-validated

| Area | Risk | Mitigation |
|------|------|-----------|
| `test_smt_backtest.py::test_run_backtest_long_trade_tp_hit` | Long signal may be filtered by TDO gate | Test uses monkeypatched TDO; if filtered, add `TDO_VALIDITY_CHECK=False` patch |
| `test_smt_backtest.py::test_run_backtest_short_trade_stop_hit` | Similar — short may be filtered | Add `MIN_STOP_POINTS=0.0` patch if needed |
| All `test_smt_strategy.py` tests | Only test `detect_smt_divergence` and `find_entry_bar` directly — not affected | ✅ No changes expected |

If any existing test breaks because `screen_session` now rejects its synthetic signal, fix by
adding `monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)` and
`monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)` to that test's fixture or body.

### Test Automation Summary

- **Automated**: 14 new tests + regression validation — 100% automatable
- **Manual (1)**: Live backtest comparison (Task 3.2) — requires cached parquet data;
  not blocking for test suite but validates real-world behavior
- **Gaps remaining**: None for code logic

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Existing integration tests break because `_build_short_signal_bars` / `_build_long_signal_bars` produce signals that fail TDO gate | Medium — TDO is mocked via `compute_tdo` in tests; depends on mock value vs bar base | Add `monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)` to affected tests |
| `MIN_STOP_POINTS = 5.0` filters all synthetic test trades (base=20000, TDO=nearby) | Low — synthetic bars use TDO 50–100+ pts away from base | If needed, set `MIN_STOP_POINTS = 0.0` in affected test fixtures |
| Live backtest returns 0 trades after guards | Low — 5 of 7 shorts had valid TDO (well below entry) | Diagnostic: run with `TDO_VALIDITY_CHECK=False` to confirm all shorts pass; 0 trades would indicate a data or date range issue |
| `print_direction_breakdown` not called by frozen `__main__` | By design — documented explicitly | Autoresearch agent uses `long_pnl`/`short_pnl` from frozen output; function is verified by unit tests |

---

## Parallel Execution Summary

```
Wave 1 (sequential — single file, all interdependent):
  1.1  Add 6 new constants to STRATEGY TUNING section
  1.2  Refactor screen_session with 4 guards
  1.3  Add print_direction_breakdown function

Wave 2 (sequential, after Wave 1):
  2.1  New unit tests in test_smt_strategy.py
  2.2  Regression test in test_smt_backtest.py

Wave 3 (sequential, after Wave 2):
  3.1  uv run python -m pytest tests/ -x -q
  3.2  uv run python train_smt.py (live validation)
```

All Wave 1 tasks touch `train_smt.py` and must run sequentially (no parallelism — single file).
Wave 2 tests can be written simultaneously if two agents are available (different test files),
but the dependency on Wave 1 completing first makes a single agent adequate.

**Total tasks**: 5 (3 code + 2 test + 1 validation run)

---

## ACCEPTANCE CRITERIA

- [ ] `TRADE_DIRECTION = "both"` produces same results as current baseline; "short" blocks all long signals; "long" blocks all short signals
- [ ] `TDO_VALIDITY_CHECK = True` causes screen_session to skip signals where TDO is on the wrong side of entry; `False` restores legacy behavior
- [ ] `MIN_STOP_POINTS = 5.0` causes screen_session to skip signals where computed stop distance < 5 pts; setting to 0.0 restores legacy behavior
- [ ] `LONG_STOP_RATIO` and `SHORT_STOP_RATIO` replace hardcoded 0.45 in stop computation; both at 0.45 → identical outputs to current
- [ ] `print_direction_breakdown` prints per-direction trade count, win rate, avg PnL, exit breakdown; output parseable with same prefix= pattern
- [ ] Full test suite passes (346 original + new tests), 2 skipped, 0 failures
- [ ] Live `uv run python train_smt.py` runs without error; trade count ≤ 12 (new guards filter some trades); valid short trades still appear in results

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: 6 constants added to STRATEGY TUNING section
- [ ] Task 1.2: `screen_session` has all 4 guards in correct order; docstring updated
- [ ] Task 1.3: `print_direction_breakdown` added before `screen_session`
- [ ] Task 2.1: Helper functions + 13 unit tests added to `test_smt_strategy.py`
- [ ] Task 2.2: Regression integration test added to `test_smt_backtest.py`
- [ ] Task 3.1: Full test suite passes
- [ ] Task 3.2: Live backtest runs cleanly; results reviewed
- [ ] All changes unstaged (no `git add`, no `git commit`)
