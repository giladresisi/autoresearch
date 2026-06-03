# Feature: STP→MKT stop-sizing fixes (fixes 1–3)

**Branch:** `autoresearch/stpmkt-fix-jun03` (worktree off `live` @ 389f4b2)
**Status:** spec only — NOT yet implemented. Implement, test, and commit per this file.
**Owner of execution:** a separate agent invoked in THIS worktree.
**Must be backtested before merge to production (`live`/`master`).**

---

## 1. Background — what went wrong live (2026-06-03 overnight session)

During the live session the strategy repeatedly entered with a protective stop placed
far too close to the actual fill, causing immediate stop-outs and a strategy/broker
state desync. The canonical incident:

- `new-stop-entry` @ 00:35 ET: intended entry **30718.25**, stop **30729.25** (~11 pt risk — fine on paper).
- The live executor downgraded the resting stop order to a **market order** (see §2),
  and it filled at **30728.00** — the market had run ~10 pts above the intended entry
  between signal time and fill time.
- The protective stop stayed anchored to the *intended* entry at **30729.25** → only
  **1.25 pts** above the actual fill → it triggered almost immediately.
- Worse: the strategy recorded the position as open (`stop-entry-filled`, `fill_price`
  = intended 30718.25) and never saw the broker stop fill, so `data/position.json`
  showed an **active short** while the broker was **flat** — a phantom position whose
  next strategy-side close would have opened an unintended opposite position.

Full incident log: `sessions/2026-06-03/comments.md` in the `live` worktree (notes at
00:06, 00:10, 00:39, 00:52 ET).

Two earlier symptoms the same session, same root cause:
- A protective Buy Stop @ 30703.25 was **rejected** because the market fill landed above it
  (stop ended up on the wrong side).
- Every 5-min bar the strategy re-issued `close MKT` + `sell STP` while chasing the entry.

---

## 2. Root cause

The bug lives at the boundary between the strategy and the live executor.

**Strategy side** — `strategy.py`, the stop-entry path (≈ lines 400–419). It computes:
- `entry_price = bar_open ∓ MIN_APPROACH_PTS` (10 pts away from the 5m bar open), and
- `stop_loss` from the opposite confirmation bar, then checks `MIN_STOP_DISTANCE` (5.0)
  **relative to `entry_price`** (lines 409/411).

So the strategy guarantees the stop is ≥ 5 pts from the *intended entry*.

**Executor side** — `execution/pickmytrade.py::place_entry` (≈ lines 88–108) downgrades a
resting STP order to a **market** order when the entry is within **5 pts** of the current
market (Tradovate rejects stop orders whose trigger is at/through the market). On that
downgrade it sends the order with `sl = stop_price` **unchanged** — i.e. still anchored to
the intended entry.

When the market drifts between signal time and the (async, fire-and-forget) fill, the
order fills well away from `entry_price`, but the stop is still sized off `entry_price`.
The strategy's "≥ 5 pts from entry" guarantee no longer holds relative to the **actual
fill** → tiny / wrong-side stop.

The backtest does **not** reproduce this: `regression.py` → `backtest_smt.run_backtest_v2`
uses `execution/simulated.py::SimulatedBrokerExecutor`, which fills stop entries at exactly
`entry_price` (no downgrade, no drift). That is why this was invisible until live.

---

## 3. Chosen approach (decided with the user)

**Implement the fixes in the strategy/signal layer (`strategy.py`), NOT in the executors.**
Rationale:
- The strategy runs in **both** live and backtest (`backtest_smt` calls the same
  `strategy.py` per-bar logic), so fixes are automatically backtestable.
- The strategy **owns `position.json`**, so sizing the stop here keeps strategy state and
  the broker order in sync (no new desync).

**Do NOT modify** `execution/pickmytrade.py` or `execution/simulated.py`. The executor's
STP→MKT downgrade stays as-is; we make the strategy emit a stop that *survives* the
downgrade.

### Known limitation (acknowledged, out of scope here)
The strategy only sees bar data, not the executor's send-time price. The freshest price it
has is the current bar mid (`bar_mid`). There remains a residual gap between `bar_mid` at
signal time and the executor's actual async fill. These fixes shrink the damage massively
but do not fully close it. The complete solution is the broker→strategy fill-feedback loop
(reconcile `position.json` against actual fills) — that is **fixes 4–5, deliberately out of
scope** for this branch. Choose `MKT_FILL_MIN_STOP_DISTANCE` (below) large enough to absorb
typical drift; the backtest/live will tune it.

---

## 4. Implementation

All edits are in `strategy.py`, inside the per-bar entry function, in the stop-entry path
that currently reads (≈ 400–419):

```python
                # Push entry away from current price if the natural level is too close.
                if direction == _DIR_UP:
                    entry_price = max(body_end_price, bar_open + MIN_APPROACH_PTS)
                else:
                    entry_price = min(body_end_price, bar_open - MIN_APPROACH_PTS)
                if direction == _DIR_UP:
                    stop_loss = max(float(opp_5m["low"]), float(opp_5m["body_low"]) - _STOP_WICK_CAP)
                else:
                    stop_loss = min(float(opp_5m["high"]), float(opp_5m["body_high"]) + _STOP_WICK_CAP)
                if direction == _DIR_UP and (entry_price - stop_loss) < MIN_STOP_DISTANCE:
                    return None
                if direction == _DIR_DOWN and (stop_loss - entry_price) < MIN_STOP_DISTANCE:
                    return None
                position["conf_bar_entry"] = conf_bar_snap
                kind = "new-stop-entry" if position["stop_entry"] == "" else "move-stop-entry"
                position["stop_entry"]     = entry_price
                position["stop_direction"] = direction
                position["pending_stop"]   = stop_loss
                smt_state.save_position(position)
                return _make_signal(kind, now, entry_price, stop=stop_loss)
```

Reference the existing **market-entry** path (≈ lines 358–398) — it already does the right
thing (anchors the stop to `bar_mid` ± `MIN_STOP_DISTANCE` and fills at `bar_mid`). We are
making the stop-entry path behave correctly *when it will be market-filled*.

### New constants
Add alongside the existing local constants (currently defined ≈ lines 260–267:
`_MARKET_ENTRY_THRESHOLD`, `MIN_STOP_DISTANCE = 5.0`, `MIN_APPROACH_PTS = 10.0`, …):

```python
        # STP→MKT downgrade safety (see feature.md). Keep STP_MKT_PROXIMITY_PTS in sync
        # with the executor's downgrade threshold in execution/pickmytrade.py::place_entry.
        STP_MKT_PROXIMITY_PTS      = 5.0    # entry within this of market → executor sends MKT
        MKT_FILL_MIN_STOP_DISTANCE = 10.0   # min stop distance from the EXPECTED market fill (tune via backtest)
        MAX_ENTRY_CHASE_PTS        = 10.0   # skip entry if market already ran this far past it (tune via backtest)
```
Starting values are deliberate guesses to be tuned by the backtest. `MKT_FILL_MIN_STOP_DISTANCE`
is intentionally larger than the resting `MIN_STOP_DISTANCE` (5.0) to absorb async drift.

### Fix 3 — skip the chase (COMMIT 3, but place the guard first in code flow)
Before building the signal, add a general guard: if the current market has already run past
the intended entry on the trigger side by more than `MAX_ENTRY_CHASE_PTS`, skip — a market
fill would be far worse than planned and the setup has already moved.

```python
                bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                # Fix 3: don't chase — market already ran past the intended entry.
                if direction == _DIR_UP   and bar_mid > entry_price + MAX_ENTRY_CHASE_PTS:
                    return None
                if direction == _DIR_DOWN and bar_mid < entry_price - MAX_ENTRY_CHASE_PTS:
                    return None
```
Note: given `entry_price` is pushed `MIN_APPROACH_PTS` (10) beyond `bar_open`, this guard
rarely fires at *signal* time — it is a safety net for fast markets / `body_end_price`
levels that land near price. The async-drift case it cannot see is the §3 residual.

### Fix 1 — re-anchor the protective stop to the expected market fill (COMMIT 1)
When the entry is within `STP_MKT_PROXIMITY_PTS` of `bar_mid`, the executor will market-fill
it near `bar_mid`. Treat `bar_mid` as the expected fill, anchor the stop to it, and record
`entry_price = bar_mid` so `position.json` matches the market fill and the existing
`MIN_STOP_DISTANCE` check (409/411) is measured against the fill:

```python
                will_market_fill = (
                    (direction == _DIR_UP   and bar_mid >= entry_price - STP_MKT_PROXIMITY_PTS) or
                    (direction == _DIR_DOWN and bar_mid <= entry_price + STP_MKT_PROXIMITY_PTS)
                )
                if will_market_fill:
                    expected_fill = bar_mid
                    if direction == _DIR_UP:
                        stop_loss = min(stop_loss, expected_fill - MKT_FILL_MIN_STOP_DISTANCE)   # Fix 2 floor (commit 2)
                    else:
                        stop_loss = max(stop_loss, expected_fill + MKT_FILL_MIN_STOP_DISTANCE)   # Fix 2 floor (commit 2)
                    entry_price = expected_fill
```
For **Fix 1 alone (commit 1)**, anchor using the trade's intended risk instead of the floor,
so commit 1 is self-contained and commit 2 adds only the floor. Concretely, commit 1:
```python
                    risk = abs(stop_loss - entry_price)
                    if direction == _DIR_UP:
                        stop_loss = expected_fill - risk
                    else:
                        stop_loss = expected_fill + risk
                    entry_price = expected_fill
```
…and **commit 2 (Fix 2)** changes `risk` → `max(risk, MKT_FILL_MIN_STOP_DISTANCE)` (or
equivalently the `min(...)/max(...)` floor form above). Pick whichever reads cleanest; the
end state must apply the floor.

### Keep the existing relative-to-entry MIN_STOP_DISTANCE checks (409/411)
After the block above, leave the existing checks. With `entry_price` re-anchored to
`expected_fill`, they now measure stop distance from the fill — exactly what we want. For
non-proximity (far) resting stop entries, behavior is unchanged.

### Do NOT
- Do not modify `execution/pickmytrade.py` or `execution/simulated.py`.
- Do not change `MIN_APPROACH_PTS`, `_MARKET_ENTRY_THRESHOLD`, or the market-entry path.
- Do not alter behavior for stop entries that are NOT within proximity (far resting STPs).
- Do not add print/stdout logging in the production path beyond what already exists.

---

## 5. Commit plan (three separate commits, in this order)

1. `fix(strategy): re-anchor STP→MKT protective stop to expected market fill`
   — proximity detection (`will_market_fill`), compute `bar_mid`/`expected_fill`, anchor
   stop to fill using intended `risk`, set `entry_price = expected_fill`.
2. `fix(strategy): enforce minimum stop distance from market fill`
   — introduce `MKT_FILL_MIN_STOP_DISTANCE` and apply `max(risk, floor)` in the re-anchor.
3. `fix(strategy): skip entry when market ran too far past intended entry`
   — introduce `MAX_ENTRY_CHASE_PTS` and the §Fix-3 skip guard.

Each commit must keep the test suite green (see §6). Co-author trailer as per repo
convention if committing on the user's behalf.

---

## 6. Testing & backtest validation

### Unit tests (add to `tests/test_smt_strategy_v2.py`)
Add cases that drive the stop-entry path with a `bar_mid` near `entry_price`:
1. **Fix 1 happy path** — short, `bar_mid` within proximity above the sell entry; assert the
   emitted signal's `stop` is anchored to `bar_mid + risk` and `entry_price ≈ bar_mid`
   (and the long mirror).
2. **Fix 2 floor** — intended `risk` < `MKT_FILL_MIN_STOP_DISTANCE`; assert stop distance
   from `bar_mid` equals `MKT_FILL_MIN_STOP_DISTANCE`.
3. **Fix 3 skip** — `bar_mid` past `entry_price` by > `MAX_ENTRY_CHASE_PTS`; assert the
   function returns `None` (no signal) for both directions.
4. **Regression / no-op** — a far resting stop entry (`bar_mid` well outside proximity);
   assert stop and entry are unchanged vs current behavior.
5. **Min-distance still rejects** — confirm 409/411 still return `None` when the anchored
   stop is too close.

Run: `uv run pytest tests/test_smt_strategy_v2.py -q` and the full executor/order suites
(`tests/test_pickmytrade_executor.py`, `tests/test_live_orders.py`) to confirm no
regressions. Establish the baseline first: run the suite BEFORE changes and record
pass/fail so you can distinguish pre-existing breakage from new.

### Backtest / regression (the gate before merge)
The user will direct exact dates. The general procedure:
- Run `regression.py` (1s regression) over a representative window that includes the
  2026-06-03 overnight session and a broader sample, **before** and **after** the change.
- Compare trade count, P&L, and win rate. Expectations:
  - Fixes 1–2 are ~no-ops on idealized backtest fills *except* for proximity entries, where
    stops become slightly wider; net P&L should be neutral-to-better.
  - Fix 3 removes some chase entries — verify it does not kill profitable origin entries.
- Tune `MKT_FILL_MIN_STOP_DISTANCE` and `MAX_ENTRY_CHASE_PTS` from the results.

Do not merge to `live`/`master` until the user approves the backtest results.

---

## 7. Acceptance criteria

- [ ] Three separate commits as in §5, suite green after each.
- [ ] In the stop-entry path, when an entry would be market-filled (within
      `STP_MKT_PROXIMITY_PTS` of `bar_mid`), the protective stop is anchored to the expected
      fill with at least `MKT_FILL_MIN_STOP_DISTANCE` of room, and `entry_price` reflects the
      expected fill.
- [ ] Entries whose market is already past the intended entry by > `MAX_ENTRY_CHASE_PTS`
      emit no signal.
- [ ] Far (non-proximity) resting stop entries are unchanged.
- [ ] Executors (`pickmytrade.py`, `simulated.py`) are untouched.
- [ ] New unit tests in §6 pass; full suite green.
- [ ] Backtest run completed and shared with the user; constants tuned; user approval
      obtained before any merge.

---

## 8. Out of scope (do NOT implement here)
- Fix 4 (capture real broker fill price into `position.json`).
- Fix 5 (broker→strategy fill-feedback loop / per-bar reconciliation of `position.json`
  against the broker position).
- `modify_stop_entry` sending a market `close` in parallel with a stop entry (separate item;
  see the 00:10 ET note in the live session `comments.md`).
These are the complete cure for the residual async-drift desync and will be handled later.
