# Feature: Gate chop-zone market entries (o5-fallback + STP→MKT) into the mid/equilibrium zone

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

> **Commit sequence note:** `feature.md` §5 defines an intended 6-commit sequence ("suite green
> after each commit"). That sequence is the *target structure* for when the user explicitly
> authorizes commits. Per the global CLAUDE.md rule, commit authorization is per-request and does
> NOT carry over — the execution agent leaves all changes UNSTAGED and does not commit unless the
> user explicitly says so in that request. Do NOT merge to `live`/`master` until the user approves
> the backtest results.

Validate documentation and codebase patterns before implementing. Match naming of existing utils,
types, and models. Import from correct files.

---

## Feature Description

The live SMT strategy repeatedly takes market-filled entries into the tight equilibrium band
around the daily/weekly mid (the band that keeps closing them via `weekly_mid_cross`), with no
genuine breakout and almost no headroom to the next opposing level, then reverses within ~1
minute (scratch or stop-out). Two mechanisms produce this one symptom:

1. **STP→MKT downgrade** firing on the *near* (un-triggered) side — a stop entry is converted to
   a market order merely because price sits within 5 pts of the trigger, even when price has NOT
   reached the trigger (pre-breakout fill).
2. **o5-fallback** supplying a *same-bar* pseudo-confirmation bar that fires a market entry into a
   no-room/mid zone.

This feature adds gates to stop these whipsaws while preserving legitimate breakout / origin-coil
entries:

- **R1** — STP→MKT downgrade only when the trigger is actually reached (live `pickmytrade.py` +
  backtest `strategy.py::will_market_fill`, kept in sync).
- **R2** — o5-fallback headroom gate (don't fire the same-bar pseudo-conf into a no-room zone).
- **R3** — general headroom gate on all entry paths (reward:risk ≥ ~1 to the nearest opposing
  level).
- **R4** (deferred / conditional) — mid/equilibrium suppression, added only if the backtest shows
  it adds value over R3.
- **R6** — tag the entry confirmation path (`conf`: `"o5"`/`"normal"`) and gate reason (`gated`)
  in `events.jsonl` for exact attribution.

## User Story

As the operator of the auto-co-trader live strategy,
I want market entries into the mid/equilibrium zone with no headroom to be gated out,
So that the strategy stops taking immediate-reversal whipsaw trades while still taking real
breakout/origin-coil entries that have room to run.

## Problem Statement

On 2026-06-04 the strategy took three UP entries filled at market in the band ~30495–30545
(straddling daily mid 30524 / weekly mid 30549 / London high 30544 / TWO 30542.75), each with no
real breakout and ~4–17 pts of headroom, and each reversed almost immediately. The two root
mechanisms are the directional-blind STP→MKT downgrade and the o5-fallback same-bar pseudo-conf
market entry. Neither current path checks whether the entry has room to run to the next opposing
level.

## Solution Statement

- Make the STP→MKT downgrade directional: downgrade only when market has reached/passed the
  trigger (R1), in both the live executor and the backtest mirror.
- Add a reusable headroom check: headroom = distance from the prospective entry to the *nearest
  opposing level in the trade direction* (weekly mid, daily mid, or the hypothesis' first target
  ahead); reject the entry when `headroom < max(risk, MIN_HEADROOM_PTS)` where `risk = |entry −
  stop|` (reward:risk < ~1, with a fixed floor) (R3).
- Apply that headroom check as an explicit early gate on the o5-fallback path so the same-bar
  pseudo-conf is rejected before it becomes a `conf_bar` (R2).
- Tag firing entries with `conf` and gated rejections with `gated` so o5 attribution and gate hits
  are exact in `events.jsonl` (R6).
- Backtest before/after over 2026-06-04 + a broader window; tune `MIN_HEADROOM_PTS`; add R4 only if
  it adds value; do not merge until the user approves.

## Feature Metadata

**Feature Type**: Enhancement (entry gating) + Bug Fix (R1 directional downgrade)
**Complexity**: Medium
**Primary Systems Affected**: `strategy.py` (entry logic, backtestable via `run_strategy`),
`execution/pickmytrade.py` (live STP→MKT downgrade), unit tests, regression/backtest.
**Dependencies**: None new. Uses existing `pandas`, `pytest`, `uv`. Backtest via
`regression.py` → `backtest_smt.run_backtest_v2` → `session_pipeline` → `strategy.run_strategy`.
**Breaking Changes**: No new state files. `will_market_fill` and `_too_close` semantics tighten
(intended behavior change, covered by tests). Additive event fields (`conf`/`gated`).

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy.py` (lines 18–24) — constants block (`MAX_FAILED_ENTRIES`, `_O5_FALLBACK_DIST`). New
  module-level gate constants (`MIN_HEADROOM_PTS`) go near here / the in-function block at 260–272.
- `strategy.py` (lines 102–159) — `_o5_fallback`: same-bar pseudo-conf supplier. R2 gate attaches
  here / at its call site.
- `strategy.py` (lines 162–170) — `_make_signal`: `**kwargs` flow straight into the emitted signal
  dict → `events.jsonl`. R6 tags (`conf`, `gated`) are added as kwargs here.
- `strategy.py` (lines 249–272) — in-function constants incl. `STP_MKT_PROXIMITY_PTS = 5.0`,
  `MKT_FILL_MIN_STOP_DISTANCE`, `MAX_ENTRY_CHASE_PTS`. The `:270` comment mandates sync with
  `pickmytrade.py`.
- `strategy.py` (lines 341–456) — the entry emission block: o5 call site (`:348`), market-entry
  path (`:367–407`, emits `market-entry` at `:407`), stop-entry path with #55 `will_market_fill`
  re-anchor (`:418–456`, `will_market_fill` test at `:432–435`, emits `new-stop-entry`/
  `move-stop-entry` at `:456`).
- `strategy.py` (lines 505–521) — daily/weekly mid derivation from `_daily.get("liquidities")`
  (`_liq_map` of `kind=="level"`; `_daily_mid=(day_high+day_low)/2`, `_weekly_mid=(week_high+
  week_low)/2`). **Factor this into a reusable module-level helper** and reuse it for the gate.
- `execution/pickmytrade.py` (lines 88–104) — `place_entry` STP→MKT downgrade `_too_close`
  (R1 live side). `is_stop` derived from `stop_fill_bars`/`limit_fill_bars`; `current_price` read
  from `signal`.
- `execution/pickmytrade.py` (lines 124–148) — `place_entry` tail: entry-window block returns a
  `blocked` FillRecord; otherwise submits `_post_order` and returns `order_type` ("market"/"stop")
  in the FillRecord. The R1 test asserts the returned `order_type`.
- `hypothesis.py` (lines 1142–1162) — `targets` shape: `list[{"name": str, "price": float}]`,
  filtered in-direction from current close. Use `hypothesis["targets"]` for "first target ahead".
- `smt_state.py` (lines 57–66) — `DEFAULT_HYPOTHESIS` carries `targets`, `daily_mid`, `weekly_mid`
  (status strings `"above"`/`"mid"`/`"below"`), `entry_ranges`.
- `session_pipeline.py` (lines 783–807) — `run_strategy` is called here; the returned signal is
  `_emit`-ed and appended to `events`. Arbitrary signal fields propagate to `events.jsonl`.
- `live_orders.py` (lines 414–585) — `dispatch_order`: explicit per-`kind` allowlist; **unknown
  kinds (e.g. `entry-gated`) fall through to line 583–585 `_log(sig)` only — NO broker order**.
  This is what makes the R6 `entry-gated` event safe.
- `tests/test_smt_strategy_v2.py` (lines 24–114) — fixtures: `make_5m_bar`, `make_opp_1m_recent`,
  `write_hypothesis`, `write_position`, `_isolate` (autouse, redirects smt_state paths to tmp).
- `tests/test_smt_strategy_v2.py` (lines 608–725) — #55 Fix1/Fix2/Fix3 tests: the exact template
  for R1 (`will_market_fill`) and R3 tests. **Re-validate these after R1** (the proximity slack
  drops, so the `will_market_fill` trigger condition changes — see Wave 2 note).
- `tests/test_live_orders.py` (lines 53–190) — existing PMT stop/market dispatch tests (mock the
  executor). R1's `_too_close` test exercises the **real** `PickMyTradeExecutor.place_entry`,
  monkeypatching `session_times.is_entry_allowed`→True and `_post_order`/`_order_pool.submit` to
  no-op, asserting the returned FillRecord `order_type`.
- `regression.py` (lines 176–234) — CLI: `--dates DATE_OR_RANGE...`, `--mode {1m,1s}`,
  `--no-plot`, `--update-baseline`, `--skip-lock`.

### New Files to Create

- None required. All production changes are edits to `strategy.py` and `execution/pickmytrade.py`.
- Tests are ADDED to existing files: `tests/test_smt_strategy_v2.py` and `tests/test_live_orders.py`
  (or a new `tests/test_pickmytrade_downgrade.py` if cleaner — see Task 1.1).

### Patterns to Follow

**Naming Conventions**: Module-level tunable constants are UPPER_SNAKE near the top of `strategy.py`
(e.g. `MAX_FAILED_ENTRIES`, `_O5_FALLBACK_DIST`); private helpers are `_snake_case` with a
docstring (e.g. `_find_last_bar`, `_o5_fallback`, `_bar_crosses`). Mid-derivation already exists
inline at `strategy.py:505–521` — mirror its exact `_liq_map`/`kind=="level"` logic in the new
helper.
**Error Handling**: Pure-compute functions return `None` to signal "no entry"; missing
liquidities → mid is `None` → treat as "no opposing level on that axis" (do not crash). Guard all
`.get()` lookups.
**Logging Pattern**: Production code is silent on success. The codebase uses `print(..., flush=True)`
only for genuinely exceptional live paths (e.g. `[PMT] STP->MKT: ...`). Do NOT add new stdout
logging in the gate paths — attribution is via the `conf`/`gated` signal fields, not prints. Keep
the existing `[PMT]` print but update its message to reflect the new directional condition.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: R1 directional fix + shared headroom helper (Parallel)│
├──────────────────────────┬──────────────────────────┬────────┤
│ Task 1.1: R1 pickmytrade │ Task 1.2: R1 strategy    │ Task 1.3│
│ _too_close (live)        │ will_market_fill (bt)    │ headroom│
│ Agent: backend           │ Agent: backend           │ helper  │
└──────────────────────────┴──────────────────────────┴────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: R3 + R2 gates (After Wave 1) — depend on 1.3 helper  │
├──────────────────────────────┬──────────────────────────────┤
│ Task 2.1: R3 general headroom│ Task 2.2: R2 o5-fallback gate │
│ gate at entry emission       │ (reuse helper) + attribution  │
│ Agent: backend               │ Agent: backend                │
└──────────────────────────────┴──────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: R6 event tagging + backtest (Sequential)             │
├─────────────────────────────────────────────────────────────┤
│ Task 3.1: conf/gated tags on all entry + gated signals       │
│ Task 3.2: baseline + before/after regression, tune constants │
│ Task 3.3 (CONDITIONAL): R4 mid-zone gate, only if 3.2 shows  │
│           residual mid-straddle whipsaws after R3            │
└─────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 (pickmytrade), 1.2 (will_market_fill), 1.3 (helper) — touch
disjoint regions. 1.1 and 1.2 must keep the *same* directional rule (interface contract below).
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2 — both consume the Wave-1 helper; 2.1 gates
generic emission, 2.2 gates the o5 path. They edit adjacent regions of `strategy.py:341–456`, so if
run by separate agents, integrate sequentially or have one agent own the whole block.
**Wave 3 — Sequential**: 3.1 (tags) → 3.2 (backtest/tune) → 3.3 (conditional R4).

### Interface Contracts

**Contract 1 (R1 sync)**: Task 1.1 and Task 1.2 MUST implement the identical directional rule:
- long: downgrade/market-fill iff `market >= entry_price` (drop the `− 5.0` / `− STP_MKT_PROXIMITY_PTS`)
- short: downgrade/market-fill iff `market <= entry_price` (drop the `+ 5.0` / `+ STP_MKT_PROXIMITY_PTS`)
The `strategy.py:270` comment mandating sync must be updated to reflect the new equality semantics.

**Contract 2 (headroom helper)**: Task 1.3 provides, in `strategy.py`:
```python
def _session_mids(liquidities: list) -> tuple[float | None, float | None]:
    """Return (daily_mid, weekly_mid) from level liquidities, or None per axis if absent.
    Mirrors strategy.py:505–521."""

def _nearest_opposing_level(entry: float, direction: str, daily_mid, weekly_mid,
                            targets: list) -> float | None:
    """Nearest of {daily_mid, weekly_mid, first target ahead} that lies AHEAD of entry in
    `direction` (up: level > entry; down: level < entry). Returns None if none ahead."""

def _headroom_ok(entry: float, stop: float, direction: str, liquidities: list,
                 targets: list) -> bool:
    """True if there is room to run: headroom = dist(entry → nearest opposing level);
    risk = abs(entry - stop). Pass (room) when there is NO opposing level ahead, else require
    headroom >= max(risk, MIN_HEADROOM_PTS)."""
```
Tasks 2.1 and 2.2 consume `_headroom_ok`. While 1.3 is in progress, 2.1/2.2 authors can code
against this signature (mock it).

**Mock for parallel work**: Wave-2 authors stub `_headroom_ok` returning `True` to develop call-site
wiring, then swap to the real helper at integration.

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q`
(R1 tests pass; Fix1/Fix2/Fix3 re-validated; helper unit tests pass).
**After Wave 2**: `uv run pytest tests/test_smt_strategy_v2.py -q` (R2/R3 tests pass; regression
no-op winner test passes).
**After Wave 3 (3.1)**: full suite green. **3.2**: regression before/after artifacts produced.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (R1 directional fix + shared headroom helper)

No external services. Phase 1 establishes the directional downgrade rule (R1) and the reusable
headroom primitives (R3 foundation) that Wave 2 builds on.

### Phase 2: Core gating (R3 general + R2 o5-specific)

Wire `_headroom_ok` into the entry emission points; add the explicit early o5 gate.

### Phase 3: Attribution + backtest validation (+ conditional R4)

Tag `conf`/`gated`; run baseline + before/after regression; tune; conditionally add R4.

---

## STEP-BY-STEP TASKS

Tasks organized by execution wave. Same wave = safe to run in parallel.

**Task keywords**: CREATE · UPDATE · ADD · REMOVE · REFACTOR · MIRROR

---

### WAVE 1: Foundation

#### Task 1.1: UPDATE `execution/pickmytrade.py` — R1 directional STP→MKT downgrade (live)

- **WAVE**: 1
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: []
- **BLOCKS**: 1.2 (must match rule), 3.2
- **PROVIDES**: Live downgrade fires only when trigger reached.
- **IMPLEMENT**:
  - In `place_entry` (`:94–97`), change `_too_close` to the directional rule:
    `(direction == "long" and _current >= entry_price)` /
    `(direction == "short" and _current <= entry_price)`. Drop the `− 5.0` / `+ 5.0` slack.
  - Keep the `_current > 0` guard. Update the `print("[PMT] STP->MKT: ...")` message to say
    "trigger reached" rather than "within 5pts".
  - Update the explanatory comment at `:90–92` to describe the new rule (downgrade only when the
    stop trigger is at/past market — the case Tradovate rejects as a resting stop).
- **PATTERN**: `execution/pickmytrade.py:88–104`.
- **VALIDATE**: `uv run pytest tests/test_live_orders.py -q`
- **TESTS TO ADD** (real `PickMyTradeExecutor.place_entry`, monkeypatch
  `session_times.is_entry_allowed`→True and `executor._post_order`/`executor._order_pool.submit`
  to no-op; assert returned `FillRecord.order_type`):
  - `test_r1_long_below_trigger_stays_stop`: long stop entry=30498.5, `current_price`=30495.6 →
    `order_type == "stop"` (the 00:30 case — NOT downgraded).
  - `test_r1_long_at_or_above_trigger_downgrades`: long, `current_price`=30499.0 ≥ entry →
    `order_type == "market"`.
  - `test_r1_short_above_trigger_stays_stop`: short, `current_price` above entry+ → `"stop"`.
  - `test_r1_short_at_or_below_trigger_downgrades`: short, `current_price` ≤ entry → `"market"`.

#### Task 1.2: UPDATE `strategy.py` — R1 mirror in `will_market_fill` (backtest)

- **WAVE**: 1
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [] (rule must equal Task 1.1 — Contract 1)
- **BLOCKS**: 3.2
- **PROVIDES**: Backtest reflects the live downgrade timing.
- **IMPLEMENT**:
  - At `strategy.py:432–435`, change `will_market_fill` to drop the `± STP_MKT_PROXIMITY_PTS`
    slack: `(direction == _DIR_UP and bar_mid >= entry_price)` /
    `(direction == _DIR_DOWN and bar_mid <= entry_price)`.
  - Update the `STP_MKT_PROXIMITY_PTS` comment at `:268–270` to reflect that the proximity slack
    is removed and the threshold is now the trigger itself (sync note with `pickmytrade.py`).
    `STP_MKT_PROXIMITY_PTS` may become unused by `will_market_fill`; check whether it is still
    referenced elsewhere — if not, leave the constant (it documents the live↔backtest contract)
    but annotate it as "downgrade now keys off the trigger, not a proximity band".
  - **Re-validate #55 interaction**: the Fix1/Fix2 re-anchor only runs inside the
    `if will_market_fill:` block. With the tighter rule, some prior-proximity cases now fall to the
    resting-stop path. Confirm Fix3 chase-skip (`:428–431`) and the `MIN_STOP_DISTANCE` checks
    (`:446–449`) still behave. Update the existing Fix1/Fix2/Fix3 tests (`tests/...:608–725`) whose
    bars relied on `bar_mid` within ±5 of entry but NOT past it — adjust those fixtures so
    `bar_mid` reaches/passes the entry (so `will_market_fill` still triggers) OR re-assert the
    now-resting-stop outcome. Document each changed assertion in the test docstring.
- **PATTERN**: `strategy.py:418–456`; tests at `tests/test_smt_strategy_v2.py:608–725`.
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q`
- **TESTS TO ADD**:
  - `test_r1_wmf_long_below_trigger_stays_resting_stop`: up entry where `bar_mid < entry_price`
    (no reach) → result is `new-stop-entry`/`move-stop-entry` with `entry_price` unchanged (NOT
    re-anchored to `bar_mid`), `pending_stop` = natural stop.
  - `test_r1_wmf_long_at_trigger_market_fills`: up entry where `bar_mid >= entry_price` →
    re-anchor path still runs (entry == bar_mid).
  - Symmetric short pair.

#### Task 1.3: ADD `strategy.py` — shared headroom/mid helpers (Contract 2)

- **WAVE**: 1
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2
- **PROVIDES**: `_session_mids`, `_nearest_opposing_level`, `_headroom_ok`, `MIN_HEADROOM_PTS`.
- **IMPLEMENT**:
  - ADD module-level constant near `strategy.py:24`: `MIN_HEADROOM_PTS = 10.0  # min room to nearest
    opposing level; entries with less are gated (tune via backtest)`. (Start 10.0; tune in 3.2.)
  - ADD `_session_mids(liquidities)` mirroring `strategy.py:505–513` (`_liq_map` over
    `kind=="level"`, `day_high/day_low`, `week_high/week_low`; each mid `None` if either bound
    missing). REFACTOR `strategy.py:505–513` to call this helper (single source of truth) — verify
    the active-position `reeval_after_stop` logic still produces identical results.
  - ADD `_first_target_ahead(entry, direction, targets)`: from `hypothesis["targets"]`
    (`[{"name","price"}]`), the nearest `price` strictly ahead of `entry` in `direction`
    (up: `price > entry`, take `min`; down: `price < entry`, take `max`). Return `None` if none.
  - ADD `_nearest_opposing_level(entry, direction, daily_mid, weekly_mid, targets)`: collect
    `{daily_mid, weekly_mid, first_target_ahead}` that lie ahead of entry in `direction`; return
    the nearest (smallest `dist`), or `None` if the collection is empty.
  - ADD `_headroom_ok(entry, stop, direction, liquidities, targets)`:
    `daily_mid, weekly_mid = _session_mids(liquidities)`;
    `lvl = _nearest_opposing_level(entry, direction, daily_mid, weekly_mid, targets)`;
    if `lvl is None`: return `True` (no opposing level ahead → room to run);
    `headroom = abs(lvl - entry)`; `risk = abs(entry - stop)`;
    return `headroom >= max(risk, MIN_HEADROOM_PTS)`.
- **PATTERN**: `strategy.py:505–521` (mid derivation); `hypothesis.py:1142–1162` (targets shape).
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q -k headroom`
- **TESTS TO ADD** (pure-function unit tests, no `run_strategy`):
  - `test_session_mids_from_liquidities`: liquidities with day/week H/L → correct mids; missing
    week bound → `weekly_mid is None`.
  - `test_nearest_opposing_level_up`/`_down`: picks nearest of mids+target ahead; ignores levels
    behind; returns None when all behind.
  - `test_headroom_ok_passes_when_no_level_ahead`: `lvl None` → True.
  - `test_headroom_ok_rejects_below_risk`: headroom 8, risk 12 → False (R<1).
  - `test_headroom_ok_rejects_below_floor`: headroom 6, risk 3, floor 10 → False (floor binds).
  - `test_headroom_ok_passes_above_both`: headroom 25, risk 12, floor 10 → True.

**Wave 1 Checkpoint**: `uv run pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q`

---

### WAVE 2: Core gates (After Wave 1)

#### Task 2.1: ADD `strategy.py` — R3 general headroom gate at entry emission

- **WAVE**: 2
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [1.3]
- **BLOCKS**: 3.1, 3.2
- **USES_FROM_WAVE_1**: Task 1.3 `_headroom_ok`.
- **IMPLEMENT**: Gate BOTH emission points using the prospective entry + its computed stop:
  - **Market-entry path** (`strategy.py:367–407`): after `stop` is finalized and the
    `MIN_STOP_DISTANCE`/`_entry_bar_cpr_ok` checks pass, before mutating `position["active"]`
    (i.e. just before `:389`), compute
    `if not _headroom_ok(bar_mid, stop, direction, _daily.get("liquidities", []),
    hypothesis.get("targets", [])): return _GATED(...)` (see Task 3.1 for the gated return; until
    3.1 lands, `return None`).
  - **Stop-entry path** (`strategy.py:409–456`): after `entry_price`/`stop_loss` are finalized
    (post `will_market_fill` re-anchor and the `MIN_STOP_DISTANCE` checks at `:446–449`), before
    `position["conf_bar_entry"] = conf_bar_snap` (`:450`), apply the same `_headroom_ok(entry_price,
    stop_loss, direction, liquidities, targets)` gate.
  - `_daily` is already loaded at `strategy.py:249`. Pass `hypothesis.get("targets", [])`.
  - **Do not** gate fills of already-resting stop entries (the `_entry_reached` fill block at
    `:283–336`) — that order was already validated when placed; gating it would strand a filled
    position. Gate only at *placement/market-entry* emission.
- **PATTERN**: emission points `strategy.py:389, 407, 450, 456`.
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q`
- **TESTS TO ADD**:
  - `test_r3_market_entry_rejected_no_headroom`: up market-entry where the nearest mid/target sits
    just above bar_mid (headroom < risk) → `None`/gated, no `position["active"]`.
  - `test_r3_market_entry_emitted_with_headroom`: same setup but mid far above → `market-entry`
    emitted.
  - `test_r3_stop_entry_rejected_no_headroom` / `_emitted_with_headroom`: stop-entry path, long.
  - `test_r3_short_*`: symmetric short market + stop pair.
  - `test_r3_no_levels_means_room`: liquidities empty / no target ahead → entry still fires
    (helper returns True).

#### Task 2.2: ADD `strategy.py` — R2 explicit o5-fallback headroom gate + o5 marker

- **WAVE**: 2
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [1.3]
- **BLOCKS**: 3.1, 3.2
- **USES_FROM_WAVE_1**: Task 1.3 `_headroom_ok`.
- **IMPLEMENT**:
  - At the o5 call site (`strategy.py:346–348`), track whether the conf bar came from o5:
    set `_conf_is_o5 = (opp_5m_from_find is None and opp_5m is not None)` after the fallback call
    (i.e. `_find_last_bar` returned None but `_o5_fallback` supplied a bar).
  - Add an **explicit early gate** for the o5 path: once the prospective o5 market entry is known
    (the o5 path is same-bar market entry; prospective entry = `bar_mid`), if `_conf_is_o5` and
    `not _headroom_ok(bar_mid, <prospective stop>, direction, liquidities, targets)`, reject before
    writing `conf_bar_snap` — return gated (Task 3.1) / `None`. Compute the prospective stop the
    same way the market-entry path does (`max(opp_5m["low"], opp_5m["body_low"] − _STOP_WICK_CAP)`
    for up / mirror for down) so risk matches what the entry would use.
  - Keep existing `_O5_FALLBACK_DIST` (`:134/:138`) and `MAX_CONFIRMATION_BODY_PTS` (`:349`) guards
    intact — this is an ADDITIONAL gate, not a replacement.
  - `_conf_is_o5` is also consumed by Task 3.1 to tag `conf:"o5"` vs `"normal"`.
  - **Placement decision (resolved)**: implement at the **call site** (`:346–349` region), not
    inside `_o5_fallback`, because the headroom check needs `bar_mid`, the prospective stop, and the
    hypothesis targets/liquidities, which are all available at the call site and keep `_o5_fallback`
    a pure window-finder.
- **PATTERN**: `strategy.py:346–407`.
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q`
- **TESTS TO ADD**:
  - `test_r2_o5_fallback_rejected_no_headroom`: construct the o5-fallback trigger (`_find_last_bar`
    None via a same-direction last 5m window; entry_ranges all `>_O5_FALLBACK_DIST` behind; current
    1m bar agrees) with a mid/target just ahead of `bar_mid` → `None`/gated, no position.
  - `test_r2_o5_fallback_fires_with_headroom`: same o5 trigger but ample room → `market-entry`
    emitted (no regression of legitimate o5).
  - `test_r2_o5_existing_guards_still_respected`: entry_range NOT far enough behind
    (`< _O5_FALLBACK_DIST`) → still `None` (the existing guard, unaffected by the new gate);
    body > `MAX_CONFIRMATION_BODY_PTS` → still filtered.

**Wave 2 Checkpoint**: `uv run pytest tests/test_smt_strategy_v2.py -q`

---

### WAVE 3: Attribution + backtest (Sequential)

#### Task 3.1: ADD `strategy.py` — R6 `conf`/`gated` event tags

- **WAVE**: 3
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: 3.2
- **IMPLEMENT**:
  - On firing entries, add `conf=` kwarg to `_make_signal`:
    - market-entry (`:407`): `conf="o5" if _conf_is_o5 else "normal"`.
    - stop-entry (`:456`): `conf="normal"` (the stop-entry path never uses the o5 pseudo bar for a
      same-bar market entry; confirm and set accordingly).
  - For gated rejections (R2 and R3), define a small local helper / inline:
    `_GATED(reason, conf)` → `return _make_signal("entry-gated", now, <prospective entry price>,
    direction=direction, gated=reason, conf=conf)`. Reasons: `"r3-no-headroom"` (general),
    `"r2-o5-no-headroom"` (o5 path). Include `conf="o5"`/`"normal"` so attribution is exact.
  - **SAFETY (verified)**: `live_orders.dispatch_order` (`:583–585`) log-only's unknown kinds, so
    `entry-gated` triggers NO broker order. `session_pipeline` (`:783–807`) `_emit`s and appends it
    to `events`; it is not in the special-cased kind sets, so no position mutation. Do NOT add
    `entry-gated` to any handler.
  - Confirm `entry-gated` does not break the same-bar-stop-check block (`session_pipeline`): that
    block keys on `kind in {"stop-entry-filled","market-entry"}` — `entry-gated` is excluded. OK.
- **PATTERN**: `_make_signal` `strategy.py:162–170`; signal-field propagation
  `session_pipeline.py:783–807`.
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q`
- **TESTS TO ADD**:
  - `test_r6_market_entry_tagged_normal`: legitimate non-o5 market-entry → `result["conf"]=="normal"`.
  - `test_r6_o5_entry_tagged_o5`: legitimate o5 entry → `result["conf"]=="o5"`.
  - `test_r6_gated_entry_emits_entry_gated`: a headroom-gated entry → `result["kind"]=="entry-gated"`,
    `result["gated"]` in `{"r3-no-headroom","r2-o5-no-headroom"}`, and `position["active"]=={}`.
  - `test_r6_stop_entry_tagged_normal`: stop-entry → `conf=="normal"`.

#### Task 3.2: RUN baseline + before/after regression; tune `MIN_HEADROOM_PTS`

- **WAVE**: 3
- **AGENT_ROLE**: backend / quant
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: 3.3
- **IMPLEMENT** (no production code unless tuning a constant):
  1. **Baseline suite**: `uv run pytest -q` on the unchanged-tree state at branch start was the
     baseline; re-run full suite now and record pass/fail counts in the execution report.
  2. **Backtest before**: stash/checkout the pre-feature `strategy.py`+`pickmytrade.py` is not
     necessary — instead run the regression on the current branch HEAD (pre-change) first IF this
     task runs before edits; since edits are already in, capture the "after" run and compare to the
     committed baseline artifacts in `data/regression/<date>/` (the worktree already has them).
     If no committed baseline exists for the window, run once with the gates effectively disabled
     by setting `MIN_HEADROOM_PTS` very low + temporarily bypassing R1/R2 via a local toggle, to
     establish "before". Prefer comparing against the existing `data/regression/<date>/` baseline.
  3. **Window**: 2026-06-04 (the symptom day) + a broader sample (use the dates already in
     `regression.md`, or a 2–3 week span). Commands:
     - 1m: `uv run python regression.py --dates 2026-06-04 --no-plot`
     - 1s (matches live tick behavior, the symptom resolution): `uv run python regression.py
       --dates 2026-06-04 --mode 1s --no-plot`
     - broader: `uv run python regression.py --dates <range> --mode 1s --no-plot`
     - First `uv run` builds the worktree venv (expected; allow time).
  4. **Compare metrics** before vs after: trade count, total P&L, win rate, avg win, avg loss, and
     **count of <1-min scratch/stop-out entries** (the metric this targets — derive from
     `trades_1s.tsv` hold-time / from `events.jsonl` entry→exit deltas).
  5. **Tune** `MIN_HEADROOM_PTS` (try 8 / 10 / 12) to maximize reduction of <1-min whipsaws while
     **not killing origin-coil winners** (verify a known origin-coil winner still enters). Record
     the chosen value + rationale.
  6. Confirm the three 2026-06-04 cases behave: 00:30 → resting stop (R1); 03:05 o5 → gated (R2);
     03:20 true-trigger false breakout → gated by R3 (headroom ~10–17 < risk/floor).
- **VALIDATE**: regression completes; before/after table produced; full suite still green
  (`uv run pytest -q`).
- **DELIVERABLE**: a before/after comparison table (CLI + execution report) shared with the user.
  **Do NOT merge — user approval gate.**

#### Task 3.3 (CONDITIONAL): ADD R4 mid/equilibrium suppression — only if 3.2 shows residual whipsaws

- **WAVE**: 3
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [3.2]
- **CONDITION**: Implement ONLY if the 3.2 backtest shows mid-straddle whipsaws still occurring
  with R3 in place (R3 may subsume R4). If R3 already eliminates them, SKIP and document that R4
  was unnecessary.
- **IMPLEMENT** (if triggered):
  - ADD `MID_ZONE_PTS = 8.0` constant (tune). In the entry block, before emission, suppress when
    `abs(entry − weekly_mid) <= MID_ZONE_PTS` (and optionally `daily_mid`), OR when
    `hypothesis.get("daily_mid") == "mid"`. Reuse `_session_mids`.
  - Return gated `entry-gated` with `gated="r4-mid-zone"`.
- **VALIDATE**: `uv run pytest tests/test_smt_strategy_v2.py -q`; re-run 3.2 regression to confirm
  net improvement over R3-only.
- **TESTS TO ADD** (if triggered):
  - `test_r4_suppressed_within_mid_zone`: entry within `MID_ZONE_PTS` of weekly mid → gated.
  - `test_r4_suppressed_when_daily_mid_status_mid`: `hypothesis["daily_mid"]=="mid"` → gated.
  - `test_r4_emitted_outside_mid_zone`: outside band + status not "mid" → emitted.

**Final Checkpoint**: `uv run pytest -q` (full suite green) + regression before/after shared.

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.** All tests here are pure-Python `pytest` —
fully automatable (synthetic fixtures, no IB/network, `_isolate` redirects state to tmp). No browser
or hardware involved.

| What you're testing | Tool |
|---|---|
| `strategy.py` gate/helper logic | `pytest` (`tests/test_smt_strategy_v2.py`) |
| `pickmytrade.py` R1 downgrade | `pytest` (`tests/test_live_orders.py` or new `tests/test_pickmytrade_downgrade.py`) |
| End-to-end strategy behavior over real bars | `regression.py` (deterministic backtest) |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy_v2.py`,
`tests/test_live_orders.py` | **Run**: `uv run pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q`

Cases enumerated per task above. Coverage by requirement:
- **R1 live** (1.1): long below/at trigger; short above/at trigger → 4 cases.
- **R1 backtest mirror** (1.2): `will_market_fill` long/short × reach/no-reach → 4 cases + updated
  Fix1/Fix2/Fix3.
- **Helpers** (1.3): mids, nearest level (up/down), headroom (no-level/below-risk/below-floor/pass)
  → 6+ cases.
- **R3** (2.1): market+stop × reject/emit × long/short + no-levels → 7 cases.
- **R2** (2.2): o5 reject / o5 fire / existing-guards-still-fire → 3 cases.
- **R6** (3.1): conf normal / conf o5 / gated emits entry-gated / stop conf normal → 4 cases.
- **R4** (3.3, conditional): within-zone / status-mid / outside → 3 cases.
- **Regression no-op (winners)**: a clear origin-coil breakout with ample headroom still enters
  (assert `market-entry`/`new-stop-entry` emitted) — explicit anti-kill test.

### Integration Tests

**Status**: ✅ Automated | **Tool**: `regression.py` (deterministic) | **Run**:
`uv run python regression.py --dates 2026-06-04 --mode 1s --no-plot`

Validates the gates over real session bars and produces the before/after metrics (Task 3.2).

### End-to-End Tests

Covered by the regression integration run (full pipeline `daily → hypothesis → trend → strategy`
via `session_pipeline`). No UI / Playwright surface in this project.

### Edge Cases

- **Missing mids** (no week H/L): `_session_mids` → `None` axis; `_headroom_ok` must not crash; all
  opposing levels None → returns True (room). — ✅ `-k headroom`
- **Empty targets**: only mids considered. — ✅ `test_nearest_opposing_level_*`.
- **R1 boundary**: `market == entry_price` exactly → downgrades (`>=`/`<=`). — ✅ in 1.1/1.2 tests.
- **Already-resting stop fill not gated**: a previously-placed stop that fills must NOT be gated
  (Task 2.1 excludes the `_entry_reached` fill block). — ✅ `test_r3_resting_fill_not_gated`.
- **#55 re-anchor under R1**: Fix1/Fix2 re-anchor only when `bar_mid` reaches entry now. — ✅
  updated Fix tests.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | ~31 (new) + updated Fix1/2/3 | 100% |
| ✅ Integration (regression) | 1 before/after run (multi-date) | — |
| ⚠️ Manual | 0 | 0% |
| **Total automated** | all | 100% |

**Goal**: 100% path coverage. No manual tests — everything is pure-Python + deterministic backtest.

**Execution agent**: CREATE all automated test files/cases as implementation tasks. RUN after each
wave checkpoint.

---

## VALIDATION COMMANDS

### Level 0: External Service Validation

N/A — no external services. (Regression uses local `data/*.parquet` already present in the worktree.)

### Level 1: Syntax & Style

```bash
uv run python -c "import strategy, execution.pickmytrade"   # import-clean
```
(No linter is configured as a gate in this repo; match existing style.)

### Level 2: Unit Tests

```bash
uv run pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q
```

### Level 3: Integration Tests (regression)

```bash
uv run python regression.py --dates 2026-06-04 --no-plot
uv run python regression.py --dates 2026-06-04 --mode 1s --no-plot
# broader window (example): uv run python regression.py --dates 2026-05-15:2026-06-04 --mode 1s --no-plot
```

### Level 4: Full Suite + before/after

```bash
uv run pytest -q
# compare regression metrics before vs after; verify <1-min whipsaw count dropped, winners intact
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] **R1 live**: `PickMyTradeExecutor.place_entry` returns `order_type=="stop"` when a long stop's
      `current_price < entry` (short's `> entry`), and `"market"` when `current_price >= entry`
      (long) / `<= entry` (short). The 00:30 case (mkt 30495.6 < trigger 30498.5) stays a resting stop.
- [ ] **R1 backtest**: `will_market_fill` triggers the #55 re-anchor only when `bar_mid >= entry`
      (long) / `<= entry` (short); the `±STP_MKT_PROXIMITY_PTS` slack is removed. Both sides
      implement the identical directional rule (Contract 1).
- [ ] **Helpers**: `_session_mids`, `_nearest_opposing_level`, `_headroom_ok` behave per spec;
      `_headroom_ok` returns `True` when no opposing level is ahead, else requires
      `headroom >= max(risk, MIN_HEADROOM_PTS)`.
- [ ] **R3** (scoped to o5-only per 2026-06-04 backtest decision): the headroom gate
      `headroom < max(risk, MIN_HEADROOM_PTS)` is applied **only on the o5 pseudo-conf path**
      (this IS R2). A *general* R3 gate on normal entries was implemented, backtested, and
      **removed** because it over-rejected legitimate breakouts (net −401 on the sample; killed
      winners on 05-18/05-27). Normal (non-o5) entries fire regardless of headroom. The
      `_headroom_ok` helper + its unit tests are retained; the gate call sites are guarded by
      `_conf_is_o5`. See NOTES → "Backtest outcome".
- [ ] **R2**: the o5-fallback path is rejected by an explicit early headroom gate (the pseudo-conf
      never becomes a `conf_bar`) while headroom-having o5 entries still fire; existing
      `_O5_FALLBACK_DIST` / `MAX_CONFIRMATION_BODY_PTS` guards still respected.
- [ ] **R6**: firing entries carry `conf` (`"o5"`/`"normal"`); gated entries emit kind
      `entry-gated` with a `gated` reason (`r2-o5-no-headroom` / `r3-no-headroom`) and leave
      `position["active"] == {}`.
- [ ] **R4** (conditional): implemented only if Task 3.2 backtest shows residual mid-straddle
      whipsaws; otherwise documented as subsumed by R3.

### Error Handling / Edge Cases
- [ ] Missing mids (no week/day H/L) → `_headroom_ok` does not crash; all-None opposing levels →
      `True` (room).
- [ ] Already-resting stop fills (the `_entry_reached` block) are NOT gated.
- [ ] R1 boundary `market == entry_price` downgrades / market-fills (`>=` / `<=`).

### Integration / E2E
- [ ] `live_orders.dispatch_order` treats `entry-gated` as log-only (no broker order) — verified.
- [ ] Regression runs end-to-end through `session_pipeline` → `run_strategy` on 2026-06-04 (1m & 1s).
- [ ] The 2026-06-04 cases resolve under the o5-only scope: 00:30 → resting stop (R1);
      03:05 o5 → gated (R2). NOTE: the 03:20 true-trigger non-o5 false breakout is **no longer
      gated by design** after R3 was scoped to o5-only (the general R3 gate that would have
      caught it killed winners and was removed). Catching true-trigger non-o5 false breakouts is
      now out of scope for this feature — tracked for the separate structure-gate redesign.

### Validation
- [ ] Unit tests pass — verified by: `uv run pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q`
- [ ] Full suite green at each wave checkpoint — verified by: `uv run pytest -q`
- [ ] Fix1/Fix2/Fix3 (#55) tests re-validated under the tightened R1 rule.
- [ ] Origin-coil winner anti-kill test: a clear breakout with ample headroom still enters.
- [ ] Backtest before/after produced (trade count, P&L, win rate, avg win/loss, <1-min whipsaw
      count); `MIN_HEADROOM_PTS` tuned; **origin-coil winners not killed**; shared with user.
- [ ] No regressions vs baseline suite count.

### Out of Scope
- Broader choppy-mode / structure-gate redesign — not required here
- `trade.py pause`/`resume` lever — already on `live`
- Cautious-ladder / `recompute_cautious_for_fill` changes — not touched
- Merging to `live`/`master` — gated on explicit user approval after backtest

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order (1.1–1.3 → 2.1–2.2 → 3.1–3.3)
- [ ] Each task validation passed
- [ ] All validation levels executed (1–4; Level 0 N/A)
- [ ] All automated tests created and passing; Fix1/Fix2/Fix3 re-validated under R1
- [ ] Regression before/after produced and shared with the user
- [ ] `MIN_HEADROOM_PTS` (and `MID_ZONE_PTS` if R4) tuned and documented
- [ ] `STP_MKT_PROXIMITY_PTS` comment sync between `strategy.py` and `pickmytrade.py` updated
- [ ] Full test suite passes (`uv run pytest -q`)
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing `[PMT]` print, updated msg)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed; NOT merged to live/master (user approval gate)**

---

## NOTES

**Design decisions (settled with user, 2026-06-04):**
- **R3 headroom rule**: reward:risk ≥ 1 with a fixed floor — reject when
  `headroom < max(risk, MIN_HEADROOM_PTS)`. `risk = |entry − stop|`. Adapts per-trade; floor
  (start 10.0) catches tiny-stop edge cases. Tune in 3.2.
- **Opposing levels**: nearest of {weekly mid, daily mid, first hypothesis target ahead} in the
  trade direction. (Matches the symptom — mids drive the `weekly_mid_cross` exits — and rewards
  trades with a real target.)
- **R4**: deferred — build R1/R2/R3 + tagging, backtest, add R4 only if residual mid-straddle
  whipsaws remain (R3 likely subsumes it).
- **o5 gate placement (R2)**: at the call site `strategy.py:346–349`, not inside `_o5_fallback`,
  so the headroom check has `bar_mid`/prospective-stop/targets and `_o5_fallback` stays a pure
  window-finder.
- **Gated event safety**: `entry-gated` is an unknown kind to `live_orders.dispatch_order` → it is
  log-only (`live_orders.py:583–585`), so it cannot place a broker order. Verified before relying
  on it for attribution.

**Backtest outcome (2026-06-04, 1m mode, base-code vs final-code on identical data):**
The locked baselines in `data/regression/` were found STALE (data backfilled after locking),
so the valid A/B is a fresh base-code run vs the final code. Isolation diagnostic established
that R1+R2 fix the symptom and are net-positive, while a *general* R3 headroom gate over-rejects
breakouts. Per user decision, **R3 was scoped to o5-only**; final config:
- 05-18: base 28 tr/+2043 → final 27 tr/+2156 (+113, winners intact)
- 05-27: base 17 tr/+1509 → final 17 tr/+1493 (−16, flat)
- 05-29: base 26 tr/−359 → final 26 tr/−359 (unchanged)
- 06-04 (symptom): base 13 tr/−8 → final 12 tr/+100 (+108; 2 o5 whipsaws gated `r2-o5-no-headroom`)
- 12 other dates in 2026-05-18:06-03: unchanged (gate didn't fire)
Net over changed days: **+205**, winners not killed, symptom improved. R4 not implemented
(deferred; R3-general already shown unhelpful, R4 even blunter). Broader-window backtest +
final user merge-approval still pending.

**Trade-offs / risks:**
- R1 tightening changes when the #55 `will_market_fill` re-anchor fires (only at/past trigger now).
  Fix1/Fix2/Fix3 tests MUST be re-validated in Task 1.2 or the suite breaks at the Wave-1 checkpoint.
- R3 risk of over-rejecting breakouts that run *through* a nearby mid. Mitigation: R-multiple rule
  (room ≥ own risk) + origin-coil winner anti-kill test + backtest tuning. If winners are killed,
  relax the rule (e.g. only count mids that are *also* exit levels) before merge.
- R2/R3 overlap (R3 covers the o5 path at emission too). R2 is kept as an explicit, earlier,
  separately-tagged gate so o5 attribution is exact and the pseudo-conf never becomes `conf_bar`.
- `STP_MKT_PROXIMITY_PTS` may become unused by `will_market_fill` after R1; keep it as a documented
  live↔backtest contract anchor, annotated.

**Out of scope (track separately):** broader choppy-mode/structure-gate redesign; `trade.py
pause/resume`; cautious-ladder / `recompute_cautious_for_fill` changes.
