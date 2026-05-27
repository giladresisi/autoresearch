# Manual Entry — Forced Direction Hypothesis

When the user opens a position via `trade.py up / down` in the direction **opposite** to
the current hypothesis, two things must happen before the order is placed:

1. Any pending stop entry for the old direction is cancelled.
2. A new hypothesis is written for the forced direction (cautious prices, targets,
   mid-cross guard labels) so the strategy manages the trade correctly and
   `run_trend()` doesn't immediately fire `trend-broken`.

`entry_ranges` are left **empty** intentionally — the position is being entered manually,
so the O5 confirmation-bar path is bypassed. Setting `entry_ranges = []` is the correct
signal and avoids a parquet read.

---

## 1. New function in `hypothesis.py`

Extract steps 7–11 from `run_hypothesis()` (lines 1186–1312) into:

```python
def build_hypothesis_from_direction(
    direction: str,           # "up" | "down" | "none"
    now,
    current_close: float,
    liquidities: list,        # from load_daily(), no live H/L refresh needed
    global_state: dict,       # from load_global()
    old_direction: str,       # current hypothesis["direction"] before this call
    weekly_mid: str,          # pre-computed label: "above" | "below" | "mid" | ""
    daily_mid: str,           # pre-computed label: "above" | "below" | "mid" | ""
    last_liquidity: str,      # carried from existing hypothesis, no recomputation
    divs: list,               # carried from existing hypothesis, or []
    direction_reason: dict,
    *,
    hist_mnq_1m: "pd.DataFrame | None" = None,  # None → entry_ranges = []
    is_fresh_start: bool = False,
    skip_veto: bool = False,
    skip_position_reset: bool = False,
) -> list:
```

### Body

Exactly the current steps 7–11, with two changes:

**entry_ranges (step 9):**
```python
entry_ranges = []
if hist_mnq_1m is not None:
    ts_now = pd.Timestamp(now)
    bar_12hr  = _find_nearest_bar(hist_mnq_1m, ts_now - pd.Timedelta(hours=12))
    bar_1week = _find_nearest_bar(hist_mnq_1m, ts_now - pd.Timedelta(weeks=1))
    if bar_12hr  is not None:
        entry_ranges.append({"source": "12hr",   "low": bar_12hr["Low"],  "high": bar_12hr["High"]})
    if bar_1week is not None:
        entry_ranges.append({"source": "1week",  "low": bar_1week["Low"], "high": bar_1week["High"]})
```

**veto (step 8b):** skip entirely when `skip_veto=True`.

**formed_at:** always `pd.Timestamp(now).isoformat()` when `direction != old_direction`.

### `run_hypothesis()` after the split

Replace the current steps 7–11 block with:

```python
return build_hypothesis_from_direction(
    direction, now, current_close, liquidities, global_state,
    old_direction, weekly_mid, daily_mid, last_liquidity, divs, direction_reason,
    hist_mnq_1m=hist_mnq_1m,
    is_fresh_start=_is_fresh_start,
    skip_position_reset=skip_position_reset,
)
```

`hist_mnq_1m` is already a parameter of `run_hypothesis()`.

---

## 2. Mid-label computation in `live_orders.py`

`_compute_mid_label` is currently a private function in `hypothesis.py`. Either:
- Rename it to `compute_mid_label` (no underscore) and export it, **or**
- Inline the logic in `live_orders.py` (it's small: compare `current_close` to the midpoint
  with a small tolerance and return `"above"` / `"below"` / `"mid"`).

Check the exact tolerance/logic in `hypothesis.py` before choosing — matching it exactly
matters for `_mid_cross_guard` correctness.

---

## 3. New helper in `live_orders.py`

```python
def _force_hypothesis_for_direction(forced_v2: str) -> None:
    """Rewrite hypothesis.json for a manually forced direction.

    Called before place_stop_entry / place_market_entry when forced_v2 differs
    from the current hypothesis direction. Cancels any pending opposite-direction
    stop entry, then writes a fresh hypothesis with correct cautious prices and
    mid-cross guard labels so run_trend() doesn't immediately fire trend-broken.

    entry_ranges is left empty — the entry is manual, O5 doesn't apply.
    """
    import hypothesis as _hyp_mod
    from smt_state import load_daily, load_global, load_hypothesis

    hyp          = load_hypothesis()
    old_direction = hyp.get("direction", "none")

    if forced_v2 == old_direction:
        return  # already aligned, nothing to do

    # Cancel any pending stop for the old direction before overwriting hypothesis.
    cancel_stop_entry(reason="direction-override")

    now           = datetime.datetime.now(_ET)
    global_state  = load_global()
    daily         = load_daily()
    liquidities   = daily.get("liquidities", [])

    # current_close from bar_state midpoint (same as _current_price()).
    current_close = _current_price()

    # Compute mid labels from liquidities (no live H/L refresh — daily.json values
    # are good enough; what matters is the label relative to current price).
    _liq_map = {l["name"]: l["price"] for l in liquidities if l.get("kind") == "level"}
    wh, wl = _liq_map.get("week_high"), _liq_map.get("week_low")
    dh, dl = _liq_map.get("day_high"),  _liq_map.get("day_low")
    weekly_mid = _hyp_mod.compute_mid_label(current_close, wh, wl) if wh and wl else ""
    daily_mid  = _hyp_mod.compute_mid_label(current_close, dh, dl) if dh and dl else ""

    signals = _hyp_mod.build_hypothesis_from_direction(
        forced_v2,
        now,
        current_close,
        liquidities,
        global_state,
        old_direction,
        weekly_mid,
        daily_mid,
        last_liquidity = hyp.get("last_liquidity", ""),
        divs           = hyp.get("divs", []),
        direction_reason = {"rule": "forced_manual"},
        # hist_mnq_1m not passed → entry_ranges = []
        skip_veto          = True,
        skip_position_reset = True,
    )

    now_str = now.isoformat()
    for sig in signals:
        _log(dict(sig, source="manual", time=now_str))
```

---

## 4. Call site in `trade.py`

In the `if cmd in ("up", "down"):` block, **before** `place_stop_entry` / `place_market_entry`:

```python
forced_v2 = "up" if direction == "long" else "down"
live_orders._force_hypothesis_for_direction(forced_v2)
```

This covers both the stop-entry path (`len(args) >= 2`) and the market-entry path (`else`).

---

## 5. What does NOT change

- `run_trend()` — no changes; it reads `hypothesis["daily_mid"]` and `hypothesis["direction"]`
  which are now correctly set before the order hits the broker.
- `session_pipeline.py` — calls `run_hypothesis()` as before; the split is internal.
- `strategy.py` O5 check — `entry_ranges = []` causes it to return `None`, which is correct:
  the position was entered manually, not via the confirmation bar path.
- `trend_broken()` in `live_orders.py` — unchanged; still clears direction to "none" and
  lets the next 5m bar form a fresh hypothesis naturally.

---

## 6. Edge cases

**Same direction:** `_force_hypothesis_for_direction` returns early if `forced_v2 == old_direction`.
No hypothesis rewrite, no stop cancellation — falls through to the normal entry path.

**Direction "none":** If current hypothesis has `direction = "none"` and user forces "up",
the mid labels are computed fresh from current price, cautious prices are set, and the new
"up" hypothesis is written. `old_direction == "none"` → `skip_position_reset=True` prevents
`reset_position_for_new_hypothesis()` from running (the caller is about to place the order).

**No bar_state.json:** `_current_price()` returns `0.0`. Cautious prices computed from 0.0
would be nonsense. Guard: if `current_close == 0.0`, abort with a printed error and do not
rewrite hypothesis.

**`_compute_mid_label` / `compute_mid_label` rename:** update any existing call sites and
the one test that patches it if applicable.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `build_hypothesis_from_direction()` exists in `hypothesis.py` with the exact signature specified in the plan (all 14 parameters including `hist_mnq_1m`, `is_fresh_start`, `skip_veto`, `skip_position_reset`)
- [ ] `run_hypothesis()` delegates its steps 7–11 to `build_hypothesis_from_direction()` via the exact call shown in the plan; no duplicate logic remains
- [ ] When `hist_mnq_1m=None`, `entry_ranges` is set to `[]` inside `build_hypothesis_from_direction()`
- [ ] When `skip_veto=True`, the veto step (8b) is skipped entirely
- [ ] `formed_at` is set to `pd.Timestamp(now).isoformat()` when `direction != old_direction`
- [ ] `compute_mid_label` (no leading underscore) is exported from `hypothesis.py` and callable externally
- [ ] `_force_hypothesis_for_direction(forced_v2)` exists in `live_orders.py` matching the docstring and body in the plan
- [ ] `trade.py` calls `live_orders._force_hypothesis_for_direction(forced_v2)` before `place_stop_entry` / `place_market_entry` for both the stop-entry and market-entry paths in the `up`/`down` block

### Error Handling
- [ ] When `forced_v2 == old_direction`, `_force_hypothesis_for_direction` returns early without rewriting the hypothesis or cancelling any stop
- [ ] When `current_close == 0.0` (bar_state unavailable), `_force_hypothesis_for_direction` prints an error and does **not** rewrite `hypothesis.json`

### Integration / E2E
- [ ] After `trade.py up` (when current direction is `"down"`): any pending stop entry is cancelled, `hypothesis.json` is rewritten with direction `"up"`, correct cautious prices, mid-cross labels, and `entry_ranges = []`
- [ ] After `trade.py up` (when current direction is already `"up"`): no hypothesis rewrite, no stop cancellation — falls through to normal entry path

### Validation
- [ ] `python -c "import hypothesis"` completes without error
- [ ] `python -c "import live_orders"` completes without error
- [ ] All existing `session_pipeline.py` call sites that call `run_hypothesis()` are unchanged

### Out of Scope
- `run_trend()` — no changes required
- `session_pipeline.py` — no changes required
- `strategy.py` O5 check — no changes required
- `trend_broken()` in `live_orders.py` — no changes required
- Tests / pytest suite — plan specifies no automated tests
