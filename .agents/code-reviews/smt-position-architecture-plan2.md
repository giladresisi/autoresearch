# Code Review: SMT Position Architecture Plan 2

**Date**: 2026-04-20
**Branch**: master (unstaged changes)
**Plan**: `.agents/plans/smt-position-architecture-plan2.md`

## Stats

- Files Modified: 3 (strategy_smt.py, backtest_smt.py, signal_smt.py)
- Files Added: 1 (tests/test_smt_position_arch.py)
- New lines: +282 (per git diff --stat)
- Pre-existing test failures: 2 (test_orchestrator_integration.py — confirmed pre-existing via git stash)

---

## Issues Found

---

```
severity: medium
file: backtest_smt.py
line: 321-326
issue: partial_exit trade record's exit_price field shows bar Close, not the actual partial fill price
detail: _build_trade_record() is called with exit_result="partial_exit", which hits the fallback
        branch (line 105-107) and sets exit_price = float(exit_bar["Close"]).
        The pnl field is correctly overwritten on line 326 (using partial_exit_price from
        position["partial_price"]), so P&L accounting is correct.
        However, the exit_price column in the trade dict and trades.tsv shows bar Close,
        not the actual partial fill price. Any downstream analysis that reads exit_price
        for partial_exit rows (e.g., to compute slippage or reconstruct equity) will see
        the wrong price.
suggestion: Either pass the partial_price to _build_trade_record via a position copy that
            sets stop_price=partial_exit_price and calls with exit_result="exit_stop", or
            explicitly set partial_trade["exit_price"] = partial_exit_price on the line
            after line 326, alongside the pnl override.
```

---

```
severity: medium
file: backtest_smt.py
line: 314
issue: partial exit with 1 contract and PARTIAL_EXIT_FRACTION >= 0.5 leaves 0 contracts in trade
detail: partial_contracts = max(1, int(position["contracts"] * PARTIAL_EXIT_FRACTION))
        When contracts=1 and PARTIAL_EXIT_FRACTION=0.5: int(1 * 0.5) = 0, max(1, 0) = 1.
        position["contracts"] -= 1 => contracts = 0.
        The state machine stays IN_TRADE with 0 contracts. On the eventual final exit,
        _build_trade_record computes pnl = sign * (exit - entry) * 0 * pnl_per_point = $0,
        producing a ghost trade record with zero P&L. This is silent; no crash or warning.
suggestion: Add a guard after the subtraction:
            if position["contracts"] <= 0:
                # Partial consumed all contracts — treat as full close
                result = exit_type_from_bar (use session_close or time exit logic)
            Alternatively, clamp partial_contracts to min(partial_contracts, position["contracts"] - 1)
            to ensure at least 1 contract remains after a partial.
```

---

```
severity: low
file: strategy_smt.py
line: 491-492
issue: detect_fvg loop starts at bar_idx-2 but the guard immediately skips that iteration, making the range start redundant
detail: The loop is: for i in range(bar_idx - 2, start - 1, -1):
            if i + 2 >= bar_idx: continue
        At i = bar_idx-2: i+2 = bar_idx >= bar_idx -> SKIP (always).
        The first iteration that executes is i = bar_idx-3, making bar3 = bar_idx-1.
        The effective loop start is bar_idx-3 but the written start is bar_idx-2.
        The docstring says "Search backward from bar_idx-2 so bar3 = i+2 < bar_idx"
        which is self-contradicting: starting at bar_idx-2 yields bar3=bar_idx, not bar3<bar_idx.
        The behavior is correct (finds the most recent FVG with bar3 = bar_idx-1 or earlier),
        but the code wastes one range step and the comment is misleading.
suggestion: Simplify to: for i in range(bar_idx - 3, start - 1, -1):
            Remove the guard entirely. Update the docstring to say bar3 = i+2 <= bar_idx-1.
```

---

```
severity: low
file: strategy_smt.py
line: 926-928
issue: Layer B stop tightening for long uses max(), which can move stop above entry_price when fvg_low > entry_price
detail: new_stop = fvg_low - STRUCTURAL_STOP_BUFFER_PTS
        position["stop_price"] = max(position["stop_price"], new_stop)
        For a long trade, if the FVG zone is above the Layer A entry price (e.g., entry=20000,
        fvg_low=20005, buffer=2 -> new_stop=20003 > entry=20000), the stop is placed ABOVE the
        entry price. The position then exits on stop at or above entry on the very next bar that
        dips to 20003. This can cause immediate stop-outs on the Layer B entry bar itself
        if bar.Close lands at 20004 and next bar Low <= 20003.
        The same geometry is possible for shorts if fvg_high < entry_price.
        In normal ICT setups the FVG is above the entry for longs and below for shorts, so this
        edge case may be rare but is unguarded.
suggestion: Add a guard before updating the stop:
            if direction == "long":
                new_stop = fvg_low - STRUCTURAL_STOP_BUFFER_PTS
                if new_stop < position["entry_price"]:  # only tighten below entry
                    position["stop_price"] = max(position["stop_price"], new_stop)
            Equivalent guard for shorts.
```

---

```
severity: low
file: strategy_smt.py
line: 904
issue: Layer B entry is blocked if partial_done=True, which prevents Layer B from entering when partial fires before FVG retracement
detail: Guard: if not position.get("layer_b_entered") and not position.get("partial_done"):
        If PARTIAL_EXIT_ENABLED=True and TWO_LAYER_POSITION=True simultaneously, and price reaches
        the partial_exit_level (midpoint) before retracing to the FVG zone, partial fires first,
        sets partial_done=True, and then Layer B is permanently blocked for this trade.
        This may be intentional (the plan does not specify the interaction between the two features),
        but it is undocumented and will silently prevent Layer B from ever entering after a partial.
suggestion: If the intent is to allow Layer B even after partial, remove "not position.get('partial_done')"
            from the guard. If the intent is to block it, add a comment explaining the design decision:
            # Layer B is intentionally blocked after partial exit: once the midpoint is reached
            # before the FVG retracement, the setup is no longer in a valid retracement structure.
```

---

## Verified Correct (Focus Areas)

**1. Layer B entry mutation edge cases** — Logic is correct for the normal case (FVG zone below entry_price for longs, above for shorts). The `max()`/`min()` direction for stop tightening is correct. The `layer_b_entered` flag reliably prevents double entry. The blended average entry price calculation is arithmetically correct. One edge case (stop above entry when fvg_low > entry) is flagged above as low severity.

**2. partial_exit PnL calculation** — The `pnl` field in the trade record is correct: `partial_pnl` is computed from `partial_exit_price` (the level) and overwrites the value from `_build_trade_record`. The `day_pnl` accumulator is also correct. The `exit_price` field in the trade record is wrong (shows bar Close), flagged as medium severity.

**3. SMT-optional gate** — Correctly never fires when wick SMT is detected. `_smt_fill` and `_displacement_direction` are reset to `None` at the top of each `bar_idx` iteration (lines 667-668 in `screen_session`, lines 509-510 in backtest IDLE), preventing cross-bar state leakage. The guard `if _smt is None:` ensures displacement only runs when SMT detection returned None.

**4. detect_fvg backward search** — Correctly returns the most recent FVG (lowest `bar1` index that still forms a valid triplet with `bar3 < bar_idx`). The backward iteration order (`range(..., -1)`) is correct. The redundant first iteration is a minor code quality issue, not a logic bug.

## Pre-existing Failures

```
file: tests/test_orchestrator_integration.py
tests: test_integration_relay_captures_events, test_integration_signals_log_written
status: pre-existing (confirmed by git stash + rerun — same failures on base commit 2ac433e)
root_cause: orchestrator integration test infrastructure issue unrelated to Plan 2
```
