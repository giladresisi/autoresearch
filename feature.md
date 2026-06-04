# Feature: Gate chop-zone market entries (o5-fallback + STP→MKT) into the mid/equilibrium zone

**Branch:** `autoresearch/o5-gating-jun04` (worktree off `live` @ 56513f5)
**Status:** spec only — NOT yet implemented. Implement, test, and commit per this file.
**Owner of execution:** a separate agent invoked in THIS worktree.
**Must be backtested before merge to production (`live`/`master`).**
**Planning:** before writing any code, turn this spec into a detailed implementation plan
using the `/plan-feature` skill (it does the codebase analysis + execution strategy). This
`feature.md` is the requirements/spec; `/plan-feature` produces the step-by-step plan file.

Source of this spec: the `live` worktree's `sessions/2026-06-04/comments.md`, note
"2026-06-04 03:25 ET — recurring chop-zone market-entry whipsaws (UP)", plus the inline
request to the strategy-feature skill on 2026-06-04.

> Note: this worktree inherited an old `feature.md` (the #55 "stpmkt-fix-jun03" spec, which is
> already merged) because that spec was committed on `live`. This file replaces it; the #55
> work is done and only referenced here for context (the `will_market_fill` re-anchor below).

---

## 1. Background — why this change

On 2026-06-04 the live strategy took **three UP entries that were filled at market in the
tight equilibrium band ~30495–30545** — the band straddling daily mid 30524 / weekly mid
30549 / London high 30544 / TWO 30542.75. Each entered with no genuine breakout and almost
no headroom to the next opposing level, and reversed almost immediately:

1. **~00:30 ET** — `new-stop-entry` 30498.5 was downgraded STP→MKT because market (30495.6)
   was within 5 pts on the **near** side (market *below* the long trigger) → entered the
   breakout *before* it confirmed. (This is the entry from the "00:45 ET" hot-fix note.)
2. **03:05 ET** — **o5-fallback** `market-entry` 30540.75: the hypothesis formed and entered
   on the *same* 1m bar. No real opposite-5m confirmation bar existed (the prior 5m window
   was bullish), so `_o5_fallback` supplied it as a pseudo-conf bar; the bar had opened past
   the pseudo-conf body-high, so the market-entry path fired. Closed ~1 min later at 03:06 by
   a `weekly_mid_cross` exit (scratch, ~−1.5 pts). Entry sat only ~4–9 pts under
   week_mid/London high — no room to run.
3. **03:20 ET** — `new-stop-entry` 30532.25 downgraded STP→MKT (market 30534.0, *above* the
   trigger — a true trigger, but a false breakout), stopped out 32 s later at 30508.0
   (~−24 pts). (A 30540.5 stop-fill at 03:12 also stopped out at 03:15 — same band.)

Two distinct mechanisms — the **STP→MKT downgrade** and the **o5-fallback market entry** —
producing one symptom: *enter at market into the mid/equilibrium zone with no real breakout
and no headroom → immediate reversal.* This change adds gates to stop these whipsaws while
preserving legitimate breakout/origin-coil entries.

---

## 2. Current code (what we're changing)

All of the entry logic below lives in `strategy.py::run_strategy` (the Section-2 "no active
position" entry block), which **is exercised by the backtest** — backtests replay bars
through `run_strategy` via `regression.py` → `backtest_smt.run_backtest_v2` using
`execution/simulated.py::SimulatedBrokerExecutor`. So gates added in `strategy.py` are
backtestable.

**Key anchors (current line numbers @ 56513f5):**

- `_O5_FALLBACK_DIST = 100.0` — `strategy.py:24` (o5-fallback "entry range far behind" gate).
- `_find_last_bar(...)` — `strategy.py:30` — finds the most recent completed opposite-direction
  5m confirmation bar; returns `None` if the last 5m bar moved with the hypothesis.
- `_o5_fallback(...)` — `strategy.py:102` — when `_find_last_bar` is `None` AND all entry
  ranges are >`_O5_FALLBACK_DIST` behind price AND the current 1m bar agrees with direction,
  returns the prior 5m window as a **pseudo-confirmation bar**. Called at `strategy.py:348`.
- `_MARKET_ENTRY_THRESHOLD = 0.0` — `strategy.py:260` — market-enter only when the bar opened
  past the entry level (`approach = body_end_price − bar_open < 0`).
- `STP_MKT_PROXIMITY_PTS = 5.0` — `strategy.py:270` — must stay in sync with the executor's
  5-pt downgrade threshold.
- Market-entry path → `_make_signal("market-entry", ...)` at `strategy.py:407` (this is the
  path 03:05 took, using the o5 pseudo-conf bar).
- Stop-entry path with the #55 **`will_market_fill`** re-anchor — `strategy.py:418–445`
  (`will_market_fill` test at `:432–435`, re-anchor + `entry_price = expected_fill` at
  `:437–445`). This is the **backtest-side analog** of the live STP→MKT downgrade.
- Daily/weekly mid derivation from liquidities (pattern to reuse for a mid gate):
  `strategy.py:510–520` (`_daily_mid = (day_high + day_low)/2`, `_weekly_mid =
  (week_high + week_low)/2`, from `_daily.get("liquidities")`). The hypothesis dict also
  carries `daily_mid`/`weekly_mid` **status strings** (`"above"`/`"mid"`/`"below"`) emitted on
  `new-hypothesis`.

**LIVE-only (not exercised by the backtest):**

- STP→MKT downgrade — `execution/pickmytrade.py:88–104`: `_too_close = _current >= entry_price
  − 5.0` (long) / `_current <= entry_price + 5.0` (short) → sends `order_type="MKT"`.
  `execution/simulated.py::place_entry` (`:49`) does **not** downgrade — a stop just records
  `order_type="stop"` with `fill_price=entry_price`; the fill is then decided by
  `run_strategy`'s bar-based logic. ⇒ The directional fix must be made in **both**
  `pickmytrade.py` (live) and the `strategy.py` `will_market_fill` block (backtest), kept in
  sync, or the backtest won't reflect the live behavior.

State files touched: `data/position.json` (active/stop_entry), `data/hypothesis.json`
(cautious ladder via `recompute_cautious_for_fill`). No new state required unless a gate needs
a counter.

---

## 3. Requirements to implement

Primary: **R1 (STP→MKT directional fix)** and **R2 (o5-fallback gate)**. Secondary, to
evaluate against the backtest: **R3 (headroom gate)** and **R4 (mid/equilibrium gate)**.

### Requirement 1 — STP→MKT directional fix (only downgrade when the trigger is actually reached)
A stop entry should become a market order **only when the market has reached/passed the
trigger**, not merely sits within 5 pts on the near (un-triggered) side.

- `execution/pickmytrade.py:94–97`: change `_too_close` from `_current >= entry_price − 5.0`
  (long) to `_current >= entry_price` (long), and `_current <= entry_price + 5.0` (short) to
  `_current <= entry_price` (short). (Keeps the Tradovate constraint — a stop whose trigger is
  at/past market is the case that must go MKT; an un-reached stop above market rests legally.)
- `strategy.py:432–435` (`will_market_fill`): mirror it — `bar_mid >= entry_price` (long) /
  `bar_mid <= entry_price` (short), dropping the `± STP_MKT_PROXIMITY_PTS` near-side slack.
  Keep the two thresholds in sync (the `STP_MKT_PROXIMITY_PTS` comment at `:270` already
  requires this).
- This blocks the 00:30 case (market 30495.6 < trigger 30498.5 → stays a resting STP that only
  fills if price actually rises to 30498.5). The 03:20 case was a *true* trigger and is the
  job of R3/R4.

### Requirement 2 — o5-fallback gate (don't fire the same-bar pseudo-confirmation into a no-room/mid zone)
`_o5_fallback` (`strategy.py:102`) is the most aggressive path: a same-bar-as-formation entry
on a *pseudo*-confirmation bar. Gate it so it does not produce entries with no room or inside
the equilibrium zone. Implement one (decide in §4), additively to the existing guards:
- (preferred) **headroom check inside `_o5_fallback`/its call site (`:348`)**: reject when the
  distance from the prospective entry to the nearest opposing level in the trade direction is
  below a threshold (see R3) — i.e. only allow an o5 entry that has somewhere to go; and/or
- **mid-zone check**: reject o5-fallback when price is within the equilibrium band of the
  weekly/daily mid (see R4), or when the hypothesis `daily_mid` status is `"mid"`.
- Keep the existing `_O5_FALLBACK_DIST` (entry-range-behind) and `MAX_CONFIRMATION_BODY_PTS`
  guards intact; this is an *additional* gate.

### Requirement 3 — Headroom gate (general; applies to all entry paths)
Before emitting any entry (market-entry `:407`, stop-entry `:446`), reject when the distance
from the entry price to the **nearest opposing liquidity level in the trade direction**
(weekly mid, daily mid, and/or the hypothesis' first target) is below a minimum that implies
reward:risk < ~1 (i.e. headroom < the entry's stop distance, or < a fixed `MIN_HEADROOM_PTS`).
Use the mid derivation at `strategy.py:510–520` and `hypothesis["targets"]`. Applies symmetric
for shorts. This catches 03:05 (~4–9 pts headroom) and 03:20 (~10–17 pts), and is the main
defense for *true*-trigger false breakouts that R1 can't see.

### Requirement 4 — Mid/equilibrium gate (suppress entries straddling the mid)
Suppress new entries while price is within `MID_ZONE_PTS` of the weekly mid (the level that
keeps closing these via the `weekly_mid_cross` exit), or when the hypothesis `daily_mid`
status is `"mid"`/straddling. This is the most direct match to the symptom but the bluntest —
evaluate its P&L impact carefully against R3 (R3 may subsume it).

---

## 4. Open design decisions (settle before/while implementing)
- **Headroom threshold (R3):** fixed `MIN_HEADROOM_PTS` vs. R-multiple of the entry's stop
  distance (reward:risk ≥ ~1). Which opposing levels count: weekly mid + daily mid + first
  target, or a configurable set. Start value to be tuned by backtest.
- **Mid-zone width (R4):** `MID_ZONE_PTS` value and whether to key off computed mid distance
  vs. the hypothesis `daily_mid` status string (or both). Whether R4 is even needed if R3 is
  in place.
- **o5-fallback gate form (R2):** headroom-only, mid-only, or both; and whether it lives
  inside `_o5_fallback` (returns `None`) or at the call site `:348`.
- **Constant locations / names:** new constants near the existing block at `strategy.py:24,
  260, 270`. Pick clear names (`MIN_HEADROOM_PTS`, `MID_ZONE_PTS`).
- **New event tag for attribution:** add a field to the emitted entry signals distinguishing
  the confirmation path — e.g. `"conf": "o5"` vs `"normal"`, and/or `"gated": "<reason>"` on a
  suppressed entry — so o5 attribution and gate hits are exact in `events.jsonl` (today the o5
  path is only inferable). Decide the field name/values for plotting + `find_o5_winners.py`.
- **Keep `STP_MKT_PROXIMITY_PTS` semantics** consistent between `strategy.py` and
  `pickmytrade.py` after R1 (the `:270` comment mandates sync).
- **Interaction with #55 Fixes 1–3** (`will_market_fill` re-anchor, `MKT_FILL_MIN_STOP_DISTANCE`
  floor, `MAX_ENTRY_CHASE_PTS` chase skip at `strategy.py:418–445`): R1 narrows when the
  re-anchor fires; confirm the floor/chase logic still behaves.

---

## 5. Commit plan (separate commits)
Suite green after each commit.
1. `fix(execution): STP→MKT downgrade only when trigger reached` — pickmytrade `_too_close`
   directional fix (R1, live side) + unit test.
2. `fix(strategy): mirror STP→MKT directional fix in will_market_fill` — `strategy.py:432`
   (R1, backtest side) + unit test.
3. `feat(strategy): headroom gate on entries` — R3 + constant + unit tests.
4. `feat(strategy): o5-fallback gate (headroom/mid)` — R2 + unit tests.
5. `feat(strategy): mid/equilibrium entry gate` — R4 (only if backtest shows it adds value
   over R3) + unit tests.
6. `feat(events): tag entry confirmation path + gate reason` — emit `conf`/`gated` fields for
   plotting/attribution (final, if introduced).

---

## 6. Testing & backtest validation
- **Unit tests** (`tests/test_smt_strategy_v2.py`, and `tests/test_live_orders.py` /
  a pickmytrade test for R1 live side). Name the cases:
  - R1: long stop, market **below** trigger → stays STP, NOT downgraded (the 00:30 case);
    long stop, market **at/above** trigger → downgrades to MKT; symmetric short cases;
    `will_market_fill` mirror: same two cases at `strategy.py` level.
  - R2: o5-fallback with adequate headroom → still fires (no regression); o5-fallback with
    no headroom / in mid zone → returns `None` (no entry); existing `_O5_FALLBACK_DIST` /
    `MAX_CONFIRMATION_BODY_PTS` guards still respected.
  - R3: entry with headroom ≥ threshold → emitted; entry with headroom < threshold → `None`;
    long and short.
  - R4: price within `MID_ZONE_PTS` of weekly mid / `daily_mid == "mid"` → suppressed; outside
    → emitted.
  - Regression no-op: a clear origin-coil breakout with ample headroom still enters (must not
    kill winners).
- **Backtest / regression:** the worktree already has `data/*.parquet` and
  `data/regression/<date>/` copied. Run `regression.py` BEFORE and AFTER over a window that
  includes 2026-06-04 plus a broader sample; compare **trade count, P&L, win rate, avg win/loss,
  and the count of <1-min scratch/stop-out entries** (the metric this targets). Tune
  `MIN_HEADROOM_PTS` / `MID_ZONE_PTS` from results. First `uv run` builds the worktree venv.
- Establish a baseline: run the full suite BEFORE changes and record pass/fail.
- Do not merge to `live`/`master` until the user approves the backtest results.

---

## 7. Acceptance criteria
- [ ] R1: STP→MKT downgrade (live `pickmytrade` + backtest `will_market_fill`) fires only when
      the trigger is reached; the 00:30-type pre-breakout fill no longer occurs.
- [ ] R2: o5-fallback no longer produces no-room/mid-zone same-bar entries (03:05-type), while
      headroom-having o5 entries still fire.
- [ ] R3: entries with sub-threshold headroom to the nearest opposing level are rejected
      (catches 03:05 and 03:20).
- [ ] R4 (if kept): entries straddling the weekly/daily mid are suppressed.
- [ ] Entry confirmation path / gate reason is tagged in `events.jsonl` (o5 attribution exact).
- [ ] New unit tests pass; full suite green after each commit.
- [ ] Backtest completed and shared (before/after over 2026-06-04 + broader window); constants
      tuned; **origin-coil winners not killed**; user approval before any merge.

---

## 8. Out of scope (track separately, not here)
- The broader **choppy-mode / entry-gate redesign** (gate on structure: objective-reached +
  post-objective stall) — a separate, larger effort already on the radar; this feature is the
  narrow mid-zone/headroom + downgrade-directionality fix, not that redesign.
- The **`trade.py pause`/`resume`** lever and **STP→MKT immediate-fill state recording**
  (already implemented/committed on `live`).
- Any change to the cautious ladder or `recompute_cautious_for_fill` behavior.
