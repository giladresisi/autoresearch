# SMT V2 — SMT & SMT-Fill Detection Redesign

**Date:** 2026-06-09
**Scope:** How and when SMTs and SMT-fills are *defined and found* in the SMT V2 pipeline. Detection + accumulation buffers + cadence plumbing + one reference consumer. Consumer decision logic (hypothesis/trade) is **out of scope**.

---

## 1. Background — current behavior & gaps

The live V2 pipeline is `session_pipeline.py` → `hypothesis.py` / `daily.py` / `trend.py` / `strategy.py`. SMT detection today lives in `strategy_smt.py` (`detect_smt_divergence`, `detect_smt_fill`, `detect_fvg`) and is invoked only from `hypothesis.py::_compute_divs`.

Gaps vs. the target requirements:

1. **Regular SMT reference is wrong.** `detect_smt_divergence` (strategy_smt.py:836-903) fires on a *new running intraday session extreme* sweep, not on a touch of an existing 6hr-session / day / week **liquidity level**. It is also MNQ-vs-MES on the same resampled extreme, not per-instrument named levels.
2. **SMT-fill uses self-computed FVGs and is disabled.** `detect_smt_fill` (strategy_smt.py:1200-1228) builds its own 3-bar FVGs from 15m/30m resamples instead of the existing 1hr FVGs, only implements case (a), has no "entered vs passed" notion, and is off by default (`SMT_FILL_ENABLED=False`, `FVG_ENABLED=False`).
3. **Cadence is wrong.** Detection runs inside `run_hypothesis`, which only scans on 5m boundaries, only inspects a completed 15m/30m bar, and early-exits whenever a hypothesis direction is already set (hypothesis.py:1276-1277). It is not a per-1m detector.
4. **No accumulation.** `divs` are recomputed and consumed inline; there is no buffer that lets a 5m consumer see all SMTs from the last 5 minutes vs. a 1m consumer seeing the last minute's, and no cadence-selection (e.g. 09:30–10:30 ET → 1m else 5m).

Per-instrument MES levels and per-instrument 1hr FVGs do **not** exist today: all `daily.json` liquidities are computed from MNQ only (`session_pipeline._update_dynamic_liquidities`).

The existing `strategy_smt.detect_smt_*` and `hypothesis._compute_divs` (the 5m-hypothesis path) are **left untouched** — this work runs in parallel; migrating the hypothesis path is explicitly out of scope.

---

## 2. Definitions (target)

### 2.1 Regular SMT

A regular SMT fires when, on a 1m bar close, one instrument (the **leader**) *touches* an existing liquidity level while the other instrument (the **laggard**) has **not** touched its corresponding level.

- **Levels are per-instrument.** MNQ has its own set of levels; MES has its own. The SMT compares MNQ's bar vs. MNQ's level and MES's bar vs. MES's level. Either instrument may be the leader (symmetric).
- **Eligible levels** ("a past 6hr session / day / week"):
  - **Completed 6hr sessions** — fixed high/low of already-**closed** sessions: `asia`, `london`, `ny_morning`, `ny_evening` (18:00–00:00 / 00:00–06:00 / 06:00–12:00 / 12:00–17:00 ET). The currently-forming session does **not** qualify until closed.
  - **Day** high/low — the **running** day extreme established **up to the prior bar**.
  - **Week** high/low — the **running** week extreme established **up to the prior bar**.
- **Touch** = **wick** crosses the level: `bar.High ≥ high_level` (for a high) or `bar.Low ≤ low_level` (for a low). This is the **wick** SMT, evaluated on every 1m bar. The body/close variant is the **hidden** SMT (§2.3).
- **Direction:** a swept **high** → `short` (bearish); a swept **low** → `long` (bullish).

### 2.2 Hidden (body) SMT

A **hidden SMT** is the bar-**body** counterpart of the wick SMT. Because a "body" is only meaningful relative to a chosen bar size, hidden SMTs are **interval-dependent** and evaluated on the **15m and 30m** resampled bars (the two intervals used today).

- Same per-instrument liquidity-level model and direction conventions as §2.1, but the **touch is determined by the bar's close** instead of its wick: leader's 15m/30m **close** crosses its level while the laggard's close does not cross its corresponding level.
- Evaluated when a 15m or 30m bar **completes** (within `on_1m_bar`, at the 15m / 30m boundary), not every 1m bar. A hidden SMT is therefore tagged with its `timeframe` (`"15m"` / `"30m"`) and `type: "body"`.
- A close beyond a level is strictly stronger than a wick touch, so a hidden SMT is a higher-conviction signal; it is a distinct event from a wick SMT on the same level.

> **Assumption flagged for review:** this anchors the hidden SMT to the same per-instrument *named levels* as the wick SMT (just close-vs-level instead of wick-vs-level), for consistency with the redesigned regular SMT. The legacy `HIDDEN_SMT` in `strategy_smt.py` instead used a running *close-extreme* of the resampled session. If you prefer to preserve the legacy running-close-extreme definition for hidden SMTs rather than anchoring them to the named levels, say so and §2.2/§3 will be adjusted.

### 2.3 SMT-fill

A fill fires against existing **per-instrument 1hr FVG** zones `[bottom, top]`.

- **Pairing requirement:** a fill SMT is only valid for a 1hr FVG that exists on the **same 1hr bar in both tickers** (same FVG bar timestamp, same kind). If a 1hr FVG exists for a bar in one ticker but not the other, **no SMT-fill** can form on it. (Such a one-sided FVG remains tracked in the per-instrument liquidity block and is available to *other* consumers — e.g. as a cautious target — just not for fill detection.)
- **entered** = wick reaches into the zone (crosses the **near** edge, not the **far** edge).
- **passed** = wick crosses fully through the **far** edge.
- Each ticker is measured against **its own** FVG zone (prices differ per instrument).
- **Case (a) / Fill-A:** the leader *entered-or-passed* its FVG while the laggard *has not reached* its corresponding FVG.
- **Case (b) / Fill-B:** **both** instruments *entered* their FVGs, and one *passed* the far edge while the other is *still inside*.
- **Direction** from FVG kind: bullish FVG fill → `long`; bearish FVG fill → `short`.

---

## 3. Detection lifecycle & re-arm

All detection is **edge-triggered**: an SMT/fill is emitted **once** on the rising edge of its condition (the bar where it first becomes true). It is **not** re-emitted on every subsequent bar while the divergence persists.

### 3.1 Regular SMT re-arm

After a regular SMT fires on a `(level, direction)` pair, that pair becomes **dormant**. Re-arming **re-allows a brand-new SMT to be created** — it does **not** revive or reactivate the prior SMT. To re-fire, **both**:

1. The dormant flag is cleared by **either** of (whichever occurs first):
   - the leader making a **noticeable opposite move of at least `MIN_REARM_OPP_MOVE_PTS`** points away from the level (measured against the price at the fire), **or**
   - an **opposite-direction SMT** being created in the interim;

   **and**
2. the leader must **re-touch** the level (a fresh rising edge) while the laggard still hasn't.

`MIN_REARM_OPP_MOVE_PTS` is a configurable threshold (per instrument scale; MNQ and MES values may differ). Either trigger independently re-arms the level.

### 3.2 SMT-fill re-arm

Same dual gate as regular SMTs (§3.1): re-creating a fill on a given FVG requires the dormant flag cleared (a `MIN_REARM_OPP_MOVE_PTS` opposite move **or** an intervening opposite-direction SMT), then a fresh re-touch.

**Exception — Fill-B follow-on:** Fill-B may fire as a direct continuation of Fill-A on the **same** FVG in one continuous move, **without** the opposite-SMT re-arm. The permitted progression:

1. Leader enters its FVG, laggard hasn't reached → **Fill-A** fires.
2. Laggard then also enters its FVG (both inside) → no fire.
3. One passes the far edge while the other is still inside → **Fill-B** fires.

Fill-B can also fire independently (both entered, one passes, other doesn't) without a preceding Fill-A.

### 3.3 Per-target state

Detection maintains a small state machine per `(instrument-pair, level-or-FVG, direction)`:

- `armed` / `dormant` (re-arm gate).
- last condition value (for rising-edge detection).
- For fills: whether Fill-A has fired and the entered/passed status of each instrument (to allow the Fill-B follow-on).
- Running day/week levels advance: when the running extreme makes a new value, it is treated as a **new level** — reaching the old value no longer counts, and the pair re-evaluates against the new running level.

This state is **persisted** (see §7) so it survives a live restart.

---

## 4. Per-instrument levels & FVGs (`daily.json` schema change)

The `daily.json` liquidity block gains a per-instrument MES counterpart, **additively** — the existing MNQ block keeps its current key:

- **Keep** the existing MNQ block as **`liquidities`** (no rename — all current readers are untouched).
- **Add** a parallel **`liquidities_mes`** block with the same structure (session/day/week levels of kind `level`, plus 1hr FVGs of kind `fvg` with `top`/`bottom`).

`session_pipeline` work:

- `on_daily_or_startup` / `on_session_start` seeding gains an **MES pass** mirroring the MNQ computation (session highs/lows, day/week H/L, 1hr FVGs), using `_hist_mes_1m` + today's MES bars, writing `liquidities_mes`.
- `_update_dynamic_liquidities` gains an MES pass updating `liquidities_mes` each bar from `mes_bar_row` / `today_mes` (both already available in `on_1m_bar`).
- `DEFAULT_DAILY` in `smt_state.py` gains the `liquidities_mes` key (defaulting to `[]`); the existing `liquidities` key is unchanged.

Because the change is purely additive, the ≈54 existing references to the `"liquidities"` key across the codebase are **unaffected**. The `levels.json` snapshot (for plots) gains the MES levels. Old persisted `daily.json` files simply lack `liquidities_mes` (treated as empty) — no migration needed.

---

## 5. Buffers & lifecycle (in `on_1m_bar`)

Two in-memory buffers held on the pipeline (or a dedicated `SmtBuffer`):

- **Per-minute buffer** — list of SMTs/fills found on the just-closed bar. **Overwritten** each `on_1m_bar`. Read by **1m-cadence** consumers.
- **5m accumulator** — list appended every bar. Read by **5m-cadence** consumers. **Cleared at the 5m boundary, after** the 5m consumers have run. Window = the bars since the last drain (≈ the last 5 minutes).

**Per-bar order inside `on_1m_bar`:**

1. Update per-instrument levels/FVGs (§4).
2. **Detect** for this bar → append to per-minute buffer (replace) and 5m accumulator (append):
   - **wick** regular SMTs + fills — every 1m bar;
   - **hidden** (body) SMTs — only when this bar **completes a 15m or 30m** resampled bar (§2.2). The pipeline resamples `today_mnq`/`today_mes` to 15m/30m and evaluates the just-completed bar at the boundary.
3. Run **cadence-appropriate consumers** (§6): 1m consumers every bar (read per-minute buffer); 5m consumers at the 5m boundary (read accumulator).
4. At the 5m boundary, **after** 5m consumers run, **drain** the accumulator.

Note: hidden SMTs fire at most at 15m/30m boundaries, but they land in the same buffers and are read by whichever consumer cadence is active when they appear.

Both buffers are in-memory only — they drain every ≤5 minutes, so losing them on a live restart is harmless.

Each SMT/fill record is a plain dict (shape similar to today's `smt-div` events): `{kind, type, side/direction, time, leader, level_name/fvg_name, level_price/fvg_bounds, mnq_price, mes_price}`. Being plain dicts, a **shallow copy fully detaches** a record (no shared-reference hazard) — important for preserve-by-copy consumers (§6).

---

## 6. Cadence plumbing & reference consumer

### 6.1 Cadence selection

Read API: `get_new_smts(cadence)` with `cadence ∈ {"1m","5m"}`. The invoker computes cadence from the ET clock:

- **09:30–10:30 ET → `"1m"`** (run every 1m bar close).
- **otherwise → `"5m"`** (run on the 5m boundary).

The reference-consumer invocation is additionally gated on **no open position** (flat).

### 6.2 Reference consumer — `PendingSmtWatch` (lifecycle only, no emit)

Demonstrates the **accumulate → preserve → invalidate** lifecycle, nothing more:

- While flat, **copy** (shallow) new SMTs from the buffer into its own **retained set**, so they survive the buffer drain.
- **Invalidate** a retained SMT when a noticeable trend in its direction occurs (the expected move happened) or when it is contradicted.
- Pure state bookkeeping exposed as queryable state. **No events emitted, no trade/hypothesis action.** It exists to prove the retention mechanics and pin the interface future consumers will use.

---

## 7. Persistence

A new **`smts.json`** state store, added to `smt_state.py` following the existing `load_*`/`save_*`/`_IN_MEMORY` pattern (in-memory dict for backtests, file under the session state dir for live). It persists:

- The reference consumer's **retained set**.
- The per-target **edge / re-arm state** (§3.3).

So both survive a live subprocess restart. The 1m/5m buffers stay in-memory. Per-instrument levels/FVGs persist via `daily.json` (already durable; reseeded on restart by `on_daily_or_startup`).

---

## 8. File layout

**New:**

- `smt_detect.py` — pure detection engine (`detect_regular_smts` for wick, `detect_hidden_smts` for 15m/30m body, `detect_fill_smts`) + `SmtBuffer` (per-minute + 5m accumulator) + `PendingSmtWatch` (retained set). Detection functions take prior state and bars, return `(new_events, updated_state)` so they are unit-testable in isolation.
- `smts.json` store helpers in `smt_state.py`.
- `tests/test_smt_detect.py`.

**Edited:**

- `session_pipeline.py` — MES liquidity pass in seeding + `_update_dynamic_liquidities` (writing `liquidities_mes`); 15m/30m resample for hidden SMTs; new per-1m detection + buffers + cadence + reference-consumer wiring in `on_1m_bar`.
- `smt_state.py` — `DEFAULT_DAILY` gains `liquidities_mes`; new `smts.json` store helpers.

The existing `liquidities` (MNQ) key and all its readers are **unchanged** (additive MES block only).

**Untouched (out of scope):** `strategy_smt.detect_smt_*`, `hypothesis._compute_divs` and the 5m-hypothesis SMT/score/direction path.

---

## 9. Testing

Unit (pure functions in `smt_detect.py`):

- Per-instrument touch / no-touch → regular SMT fires only on divergence; both-touch → no fire.
- Edge-fire-once: persistent divergence does not re-emit.
- Re-arm via **both** triggers: (a) `MIN_REARM_OPP_MOVE_PTS` opposite move clears dormancy; (b) an intervening opposite-direction SMT clears dormancy; in each case a fresh re-touch re-fires as a **new** event, not a revived one. Below-threshold opposite move with no opposite SMT → no re-fire.
- Running day/week level advance → re-evaluated against the new extreme.
- Completed-session levels eligible only after close.
- **Hidden SMT:** close-vs-level on 15m and 30m completed bars; fires distinct from the wick SMT; does not fire on intermediate 1m bars.
- Fill **pairing**: no fill when the 1hr FVG exists in only one ticker; fill possible when both tickers have the FVG on the same 1hr bar.
- Fill-A (one reached, other didn't), Fill-B (both entered, one passes far edge), entered-vs-passed boundaries.
- Fill-B follow-on after Fill-A on the same FVG without re-arm; independent Fill-B.

Integration (`session_pipeline`):

- MES `liquidities_mes` populated in parallel with MNQ; existing `liquidities` (MNQ) unchanged.
- Buffer: per-minute read returns only this bar's; 5m read returns the accumulated window; drain occurs at the 5m boundary after 5m consumers.
- Cadence selection across the 09:30 and 10:30 ET boundaries; flat-gating.
- `PendingSmtWatch`: retained SMT survives a buffer drain (preserve-by-copy); invalidate clears it.
- Restart: edge-state + retained set reload from `smts.json`.

---

## 10. Open knobs (deferred, not blocking)

- `MIN_REARM_OPP_MOVE_PTS` values per instrument (MNQ/MES); tuned later.
- Hidden-SMT intervals — currently 15m + 30m (matching today); could be made configurable.
- **Hidden-SMT reference** — anchored to named levels (close-vs-level) per §2.2; flagged for confirmation vs. the legacy running-close-extreme definition.
- Whether the 5m-hypothesis path is later migrated onto this buffer (separate effort).
