# Feature: SMT V2 — SMT & SMT-Fill Detection Redesign

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

**Approved spec:** `docs/superpowers/specs/2026-06-09-smt-v2-detection-design.md` — READ IT FIRST. This plan implements that spec verbatim, including the accepted assumption that **hidden SMTs are anchored to the named per-instrument levels (close-vs-level) on 15m/30m bars**.

---

## Feature Description

Redefine how and when SMTs and SMT-fills are *found* in the SMT V2 pipeline:

1. **Regular (wick) SMT** = on a 1m bar, one instrument's wick touches an existing per-instrument liquidity level (a completed 6hr-session high/low, or the running day/week high/low) while the other instrument has not touched its corresponding level.
2. **Hidden (body) SMT** = the same level model but close-vs-level, evaluated on completed **15m and 30m** bars.
3. **SMT-fill** = against existing per-instrument **1hr FVGs** that exist on the **same 1hr bar in both tickers**: Fill-A (leader entered/passed its FVG, laggard hasn't reached its) and Fill-B (both entered, one passed the far edge, the other still inside).
4. Detection runs **every `on_1m_bar`**, independent of hypothesis state, with newly-found SMTs accumulated into **two buffers** (per-minute + 5m accumulator) served to consumers by a **cadence param** (09:30–10:30 ET → 1m, else 5m). A lean reference consumer (`PendingSmtWatch`) demonstrates copy-preserve-invalidate. Edge/re-arm state and the retained set persist to a new `smts.json`.

This runs **in parallel** with the existing 5m-hypothesis SMT path (`hypothesis._compute_divs`, `strategy_smt.detect_smt_*`), which is **left untouched** (out of scope).

## User Story

As a **strategy developer**
I want **SMTs and SMT-fills detected every minute against per-instrument liquidity levels and 1hr FVGs, accumulated in cadence-aware buffers**
So that **multiple consumers (1m and 5m) can react to fresh divergences, with some preserving them across the buffer drain until a trend confirms or invalidates them**.

## Problem Statement

Current SMT detection (`hypothesis._compute_divs` → `strategy_smt.detect_smt_divergence` / `detect_smt_fill`) fires on a running intraday-extreme sweep over 15m/30m resamples, only on 5m boundaries, only while hypothesis direction is `none`, uses self-computed FVGs (disabled by default), and never accumulates results. None of the four target requirements are met.

## Solution Statement

A new pure-detection module `smt_detect.py` (detection functions + `SmtBuffer` + `PendingSmtWatch`) wired into `SessionPipeline.on_1m_bar`. `daily.json` gains an **additive** `liquidities_mes` block (the MNQ `liquidities` key is unchanged). A new `smts.json` store persists edge/re-arm state and the retained set across live restarts.

## Feature Metadata

**Feature Type**: New Capability (parallel to existing SMT path)
**Complexity**: High
**Primary Systems Affected**: `smt_detect.py` (new), `session_pipeline.py`, `smt_state.py`, `daily.json` schema
**Dependencies**: None external (pandas already used). No new packages.
**Breaking Changes**: No. `daily.json` change is additive (`liquidities` MNQ key preserved); `on_1m_bar` signature unchanged (buffers are internal pipeline state); old persisted `daily.json`/`smts.json` absences fall back to defaults.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `session_pipeline.py` (lines 361–915) — `on_1m_bar`: the dispatch point detection/buffers wire into. Note `today_mnq`/`today_mes` are already parameters; `mnq_bar_row`/`mes_bar_row` are the just-closed bars.
- `session_pipeline.py` (lines 917–1118) — `_update_dynamic_liquidities`: MNQ session/day/week H/L + FVG-prune. The MES pass mirrors this writing `liquidities_mes`.
- `session_pipeline.py` (lines 1120–1177) — `_extend_fvg_frames`: rolling 1hr/4hr FVG frames from live 1m. The MES 1hr FVG frame mirrors the `1h` branch only.
- `session_pipeline.py` (lines 139–267) — `on_daily_or_startup`: seeding of liquidities/FVG frames. Add the MES seed pass here.
- `daily.py` (lines 35–62) — `_session_bars(mnq_1m, session, today)`: instrument-agnostic; reuse for MES with `today_mes`.
- `daily.py` (lines 146–213) — `_detect_fvgs(hourly_bars, mnq_1m)`: instrument-agnostic; reuse for MES. FVG `name` encodes formation timestamp `fvg_{YYYYMMDD_HHMM}_{bull|bear}` — use it to **pair** MNQ↔MES FVGs by timestamp+side.
- `smt_state.py` (lines 112–151, 235–349) — `DEFAULT_DAILY`, `_atomic_write`/`_load`, `load_*`/`save_*`, `_IN_MEMORY` store, `final_snapshot`. Mirror exactly for `smts.json`.
- `strategy_smt.py` (lines 766–904, 1134–1228) — legacy `detect_smt_divergence`/`detect_smt_fill`/`detect_fvg`: reference ONLY for direction conventions (swept high→short, swept low→long; bullish FVG→long, bearish→short). Do NOT modify.
- `tests/test_session_pipeline.py` (lines 1–120, 1160–1260) — fixtures (`SessionPipeline(hist_mnq, hist_mes, emit)`, `_make_1m_bars`, `_bare_fvg_pipeline`) to mirror for new integration tests.
- `tests/test_smt_state.py` — store-test conventions to mirror for `smts.json`.

### New Files to Create

- `smt_detect.py` — pure detection engine + `SmtBuffer` + `PendingSmtWatch`.
- `tests/test_smt_detect.py` — unit tests for detection + buffer + watch.

### Patterns to Follow

**Naming**: snake_case functions; module-level `UPPER_SNAKE` constants for thresholds (e.g. `MIN_REARM_OPP_MOVE_PTS_MNQ`, `..._MES`, `HIDDEN_TFS = ("15min","30min")`). SMT record = plain `dict` (mirrors today's `smt-div` event dicts).
**Error handling**: detection functions are pure and total — return `([], state)` on degenerate inputs (empty frames, missing level), never raise. Mirror `_mmax`/`_mmin` None-tolerance in `session_pipeline.py`.
**State persistence**: follow `smt_state._load`/`_atomic_write` + `_IN_MEMORY` exactly. `smts.json` lives under `paths.state_dir()` like the other four stores.
**Production silence**: no prints in production paths (per CLAUDE.md). Encode anything diagnostic in returned data.
**Time**: ET via `now.tz_convert(_ET)` (already imported in `session_pipeline.py`). 15m/30m boundary = `now.minute % 15 == 0` / `% 30 == 0` with a per-frame "last processed boundary" guard (mirror `_fvg_done_1hr`).

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel — 3 different files)                    │
├─────────────────────────────────────────────────────────────────────┤
│ Task 1.1: smt_detect.py     │ Task 1.2: smt_state.py  │ Task 1.3:    │
│  detection+buffer+watch     │  smts.json + DEFAULT_DAILY│ session_pipe│
│  Agent: backend-core        │  Agent: state           │ MES liq pass │
│                             │                         │ Agent: backend│
└─────────────────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Wiring + Unit tests (Parallel — different files)            │
├─────────────────────────────────────────────────────────────────────┤
│ Task 2.1: on_1m_bar wiring (session_pipeline) — Deps 1.1,1.2,1.3    │
│ Task 2.2: tests/test_smt_detect.py — Deps 1.1,1.2                   │
└─────────────────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────────────────┐
│ WAVE 3: Integration tests (Sequential) — Deps 2.1                   │
├─────────────────────────────────────────────────────────────────────┤
│ Task 3.1: integration tests in tests/test_session_pipeline.py      │
└─────────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2, 1.3 — three distinct files, no shared edits.
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2 — distinct files (`session_pipeline.py` vs `tests/test_smt_detect.py`).
**Wave 3 — Sequential**: Task 3.1 — needs the wired pipeline from 2.1.

6 tasks total; 5 of 6 run in a parallel wave (83% parallelizable).

### Interface Contracts

**Contract 1 — `smt_detect` API (Task 1.1 provides → 2.1, 2.2 consume):**
- `detect_regular_smts(levels_mnq, levels_mes, mnq_bar, mes_bar, state) -> (list[dict], dict)` — wick; per-bar.
- `detect_hidden_smts(levels_mnq, levels_mes, mnq_tf_bar, mes_tf_bar, timeframe, state) -> (list[dict], dict)` — close-vs-level; called per completed 15m/30m bar.
- `detect_fill_smts(paired_fvgs, mnq_bar, mes_bar, state) -> (list[dict], dict)` where `paired_fvgs` = list of `{name, side, mnq:{top,bottom}, mes:{top,bottom}}` (already intersected by timestamp+side).
- SMT record dict shape: `{"kind":"smt"|"fill", "type":"wick"|"body"|"fill_a"|"fill_b", "side":"bullish"|"bearish", "direction":"long"|"short", "timeframe":"1m"|"15m"|"30m", "time":iso, "leader":"mnq"|"mes", "ref_name":str, "mnq_price":float, "mes_price":float}`.
- `class SmtBuffer`: `add(records:list[dict], bar_ts)`, `get_new(cadence:str)->list[dict]` (`"1m"` → last bar's; `"5m"` → accumulated), `drain_if_boundary(now)`.
- `class PendingSmtWatch`: `ingest(records:list[dict])`, `update(now, mnq_price, mes_price)` (invalidates), `retained()->list[dict]`, `to_dict()`, `classmethod from_dict(d)`.
- `state` for detection = a JSON-serializable `dict` keyed by `(ref_name, direction)` carrying `{armed:bool, last_cond:bool, fill_a_fired:bool, mnq_entered/passed, mes_entered/passed, fire_price}`.

**Contract 2 — `smt_state` store (Task 1.2 provides → 2.1, 2.2 consume):**
- `DEFAULT_SMTS = {"detect_state": {}, "watch": {"retained": []}}`; `load_smts()`, `save_smts(d)`; `DEFAULT_DAILY["liquidities_mes"] = []`.

**Contract 3 — MES liquidities (Task 1.3 provides → 2.1 consumes):**
- After each bar, `load_daily()["liquidities_mes"]` holds MES `level` entries (`asia_high`…`week_low`) and `fvg` entries, same structure as MNQ `liquidities`.

**Mock for parallel work**: 2.2 builds synthetic `levels_*`/`paired_fvgs`/bars dicts directly (no pipeline). 2.1 codes against the 1.1 signatures; if 1.1 lands last, 2.1's detection calls are stubbable via the fixed signatures above.

### Synchronization Checkpoints

**After Wave 1**: `python -m pytest tests/test_smt_state.py -q` (store still green) + `python -c "import smt_detect"` (module imports).
**After Wave 2**: `python -m pytest tests/test_smt_detect.py -q`
**After Wave 3**: `python -m pytest tests/test_smt_detect.py tests/test_session_pipeline.py -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (no external services)

No external APIs or new dependencies — Phase 1 is pure code.

#### Task 1.1: CREATE `smt_detect.py`

**Purpose**: Pure, unit-testable detection engine + buffers + reference consumer.
**Dependencies**: none.
**Steps**:
1. Module constants: `MIN_REARM_OPP_MOVE_PTS_MNQ`, `MIN_REARM_OPP_MOVE_PTS_MES` (sensible defaults, e.g. 20.0 / 3.0), `HIDDEN_TFS = ("15min","30min")`, `WATCH_CONFIRM_PTS_MNQ/MES` (trend-confirmation distance for invalidate).
2. Level model: a level dict = `{"name","kind":"level","price","sub":"high"|"low"}` derived from `liquidities`/`liquidities_mes`. Helper `eligible_levels(liqs, now)` returns completed-session highs/lows (asia/london/ny_morning/ny_evening only if the session has closed at `now`) + `day_high/low` + `week_high/low`. Session-closed test from ET clock (asia closed ≥00:00, london ≥06:00, ny_morning ≥12:00, ny_evening ≥17:00, with the running session excluded).
3. `detect_regular_smts(...)`: for each eligible level present in BOTH instruments, compute wick touch (`High≥high`/`Low≤low`) per instrument. Fire on the rising edge where leader touched and laggard didn't, emitting one record; update `state[(name,dir)]`. Direction: high→short/bearish, low→long/bullish. Symmetric (either instrument may lead). Apply the §3.1 dual re-arm: a `(name,dir)` is `armed=False` after firing; re-arm when `fire_price` opposite move ≥ `MIN_REARM_OPP_MOVE_PTS_*` OR an opposite-direction SMT appears in this call's batch; only an armed pair can fire, and only on a fresh rising edge. Running day/week advance: when the level price changes, reset that pair to armed (new level).
4. `detect_hidden_smts(...)`: identical level/divergence/re-arm logic but "touch" uses the completed 15m/30m bar **Close** vs the level; tag `type:"body"`, `timeframe`.
5. `detect_fill_smts(...)`: input already paired (both tickers have the FVG on the same 1hr bar+side). Per instrument compute entered (`wick into [bottom,top]` past near edge, not far) and passed (`wick through far edge`). Fill-A: leader entered-or-passed AND laggard not reached. Fill-B: both entered AND one passed far edge AND other still inside. Fill-B may follow Fill-A on the same FVG without re-arm (track `fill_a_fired` + per-instrument entered/passed in state); otherwise the §3.1 dual re-arm gates re-creation. Direction from side (bull→long, bear→short).
6. `class SmtBuffer`: `_per_minute:list`, `_accum:list`, `_last_drain_5m`. `add` replaces per-minute, appends accum. `get_new("1m")`→per-minute; `get_new("5m")`→accum. `drain_if_boundary(now)`: when `now.floor("5min") != _last_drain_5m`, clear accum and set marker (call AFTER consumers).
7. `class PendingSmtWatch`: `retained:list`. `ingest` shallow-copies records in. `update(now, mnq_price, mes_price)`: drop a retained SMT when price has moved ≥ `WATCH_CONFIRM_PTS_*` in its direction since retention (expected trend happened) OR an opposite-direction SMT is in the latest ingest (contradicted). `to_dict`/`from_dict` for persistence.
**Validation**: `python -c "import smt_detect"`; unit tests in Task 2.2.

#### Task 1.2: UPDATE `smt_state.py` — `smts.json` store + `liquidities_mes` default

**Purpose**: Durable store for edge/re-arm state + retained set; daily default key.
**Dependencies**: none.
**Steps**:
1. Add `DEFAULT_DAILY["liquidities_mes"] = []` (leave `liquidities` untouched).
2. Add `DEFAULT_SMTS = {"detect_state": {}, "watch": {"retained": []}}`, `_smts_path()` (→ `paths.state_dir()/"smts.json"`), `load_smts()`/`save_smts()` mirroring `load_daily`/`save_daily`.
3. If `final_snapshot()` enumerates stores, include `smts.json` so backtest in-memory snapshots flush it.
**Validation**: `python -m pytest tests/test_smt_state.py -q`.

#### Task 1.3: UPDATE `session_pipeline.py` — MES liquidity + 1hr FVG pass

**Purpose**: Populate `liquidities_mes` each bar + seed at startup, mirroring MNQ.
**Dependencies**: 1.2 (`liquidities_mes` default) — soft; code can write the key regardless.
**Steps**:
1. In `__init__`, add MES rolling-FVG frame fields: `_fvg_mes_1hr`, `_fvg_done_mes_1hr`, plus MES dynamic-liquidity caches mirroring `_dyn_*` (or factor the day/week helpers to take an instrument frame).
2. In `on_daily_or_startup`, seed MES session H/L, day/week H/L, and 1hr FVG frame from `_hist_mes_1m` + `today_mes`, writing a `liquidities_mes` list into `daily.json` (reuse `_session_bars`, `_detect_fvgs`, `compute_live_hl_mid` with MES frames).
3. Refactor `_update_dynamic_liquidities` to run for BOTH instruments: extract the existing MNQ body into a helper `_update_instrument_liquidities(now, bar_row, today_df, liq_key, fvg_frame_attr, fvg_done_attr, dyn_caches)` and call it for MNQ (`liquidities`, today_mnq) and MES (`liquidities_mes`, today_mes). Preserve current MNQ behavior exactly (regression-sensitive).
4. Extend `_extend_fvg_frames` (or add an MES variant) to advance the MES 1hr frame and detect MES 1hr FVGs.
**Validation**: `python -m pytest tests/test_session_pipeline.py -q` (existing MNQ behavior unchanged) + new MES assertions in Task 3.1.

### Phase 2: Core wiring

#### Task 2.1: UPDATE `session_pipeline.py` — detection + buffers + cadence + consumer in `on_1m_bar`

**Purpose**: Run detection every bar; populate buffers; serve the cadence-appropriate consumer; persist.
**Dependencies**: 1.1, 1.2, 1.3.
**Steps**:
1. In `__init__`, create `self._smt_buffer = SmtBuffer()` and `self._pending_watch`. In `on_session_start`/`on_daily_or_startup`, load `smts.json` (in live) into `self._detect_state` and `self._pending_watch = PendingSmtWatch.from_dict(load_smts()["watch"])`.
2. In `on_1m_bar`, AFTER `_update_dynamic_liquidities` (so levels/FVGs are current), build inputs from `load_daily()` (`liquidities`, `liquidities_mes`): eligible levels per instrument; paired 1hr FVGs (intersect MNQ↔MES `fvg` entries by name timestamp+side).
3. Run `detect_regular_smts` + `detect_fill_smts` every bar; run `detect_hidden_smts` only when `now` completes a 15m / 30m bar (resample `today_mnq`/`today_mes` to that TF; guard with a per-frame last-boundary marker). Collect all records; `self._smt_buffer.add(records, now)`.
4. Compute cadence: `is_morning = 09:30 ≤ ET(now).time() ≤ 10:30` → `"1m"` else `"5m"`. Run the reference consumer only when **flat** (`not load_position().get("active")`): if `cadence=="1m"` (every bar) or `cadence=="5m"` at the 5m boundary, call `self._pending_watch.ingest(self._smt_buffer.get_new(cadence))`. Always `self._pending_watch.update(now, mnq_close, mes_close)`.
5. At the 5m boundary, AFTER consumers, `self._smt_buffer.drain_if_boundary(now)`.
6. Persist: `save_smts({"detect_state": self._detect_state, "watch": self._pending_watch.to_dict()})` each bar (live + in-memory).
7. `on_1m_bar` signature and existing emit/strategy/trend behavior UNCHANGED — the new block is purely additive and must not alter existing events.
**Validation**: `python -m pytest tests/test_session_pipeline.py -q`; full assertions in Task 3.1.

#### Task 2.2: CREATE `tests/test_smt_detect.py` — unit tests

**Purpose**: Cover the pure engine exhaustively without the pipeline.
**Dependencies**: 1.1, 1.2.
**Steps**: see TESTING STRATEGY → Unit Tests. Build synthetic `levels_*`, `paired_fvgs`, and bar dicts; assert events + returned state.
**Validation**: `python -m pytest tests/test_smt_detect.py -q`.

### Phase 3: Integration

#### Task 3.1: ADD integration tests to `tests/test_session_pipeline.py`

**Purpose**: Verify the wired pipeline end-to-end.
**Dependencies**: 2.1.
**Steps**: see TESTING STRATEGY → Integration Tests. Use the existing `SessionPipeline` fixtures; drive `on_1m_bar` across crafted bars; assert `liquidities_mes`, buffer reads/drain, cadence selection, flat-gating, restart reload, fill pairing.
**Validation**: `python -m pytest tests/test_session_pipeline.py -q`.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: CREATE smt_detect.py
- **WAVE**: 1 · **AGENT_ROLE**: backend-core · **DEPENDS_ON**: [] · **BLOCKS**: [2.1, 2.2]
- **PROVIDES**: detection fns, `SmtBuffer`, `PendingSmtWatch`, record/state schema (Contract 1)
- **IMPLEMENT**: Phase 1 → Task 1.1 steps 1–7.
- **PATTERN**: direction conventions from `strategy_smt.py:836-903,1218-1227`; None-tolerance from `session_pipeline.py:27-42`.
- **VALIDATE**: `python -c "import smt_detect"`
- **INTEGRATION_TEST**: `python -m pytest tests/test_smt_detect.py -q` (after 2.2)

#### Task 1.2: UPDATE smt_state.py (smts.json + liquidities_mes)
- **WAVE**: 1 · **AGENT_ROLE**: state · **DEPENDS_ON**: [] · **BLOCKS**: [2.1, 2.2]
- **PROVIDES**: `DEFAULT_SMTS`, `load_smts`/`save_smts`, `DEFAULT_DAILY["liquidities_mes"]` (Contract 2)
- **IMPLEMENT**: Phase 1 → Task 1.2 steps 1–3.
- **PATTERN**: `smt_state.py:270-283` (mirror `load_daily`/`save_daily`).
- **VALIDATE**: `python -m pytest tests/test_smt_state.py -q`

#### Task 1.3: UPDATE session_pipeline.py (MES liquidity pass)
- **WAVE**: 1 · **AGENT_ROLE**: backend · **DEPENDS_ON**: [] · **BLOCKS**: [2.1]
- **PROVIDES**: `liquidities_mes` populated each bar + at startup (Contract 3)
- **IMPLEMENT**: Phase 1 → Task 1.3 steps 1–4. **Must preserve existing MNQ `liquidities` behavior byte-for-byte.**
- **PATTERN**: `session_pipeline.py:917-1118` (`_update_dynamic_liquidities`), `1120-1177` (`_extend_fvg_frames`), `daily.py:35-62,146-213`.
- **VALIDATE**: `python -m pytest tests/test_session_pipeline.py -q`

**Wave 1 Checkpoint**: `python -c "import smt_detect" && python -m pytest tests/test_smt_state.py tests/test_session_pipeline.py -q`

---

### WAVE 2: Wiring + Unit tests

#### Task 2.1: UPDATE session_pipeline.py (on_1m_bar detection/buffers/cadence/consumer)
- **WAVE**: 2 · **AGENT_ROLE**: integration-specialist · **DEPENDS_ON**: [1.1, 1.2, 1.3] · **BLOCKS**: [3.1]
- **USES_FROM_WAVE_1**: 1.1 detection/buffer/watch; 1.2 `load_smts`/`save_smts`; 1.3 `liquidities_mes`.
- **IMPLEMENT**: Phase 2 → Task 2.1 steps 1–7. Additive only; no change to existing emit/trend/strategy flow.
- **VALIDATE**: `python -m pytest tests/test_session_pipeline.py -q`

#### Task 2.2: CREATE tests/test_smt_detect.py
- **WAVE**: 2 · **AGENT_ROLE**: qa · **DEPENDS_ON**: [1.1, 1.2] · **BLOCKS**: []
- **IMPLEMENT**: all Unit Tests below.
- **VALIDATE**: `python -m pytest tests/test_smt_detect.py -q`

**Wave 2 Checkpoint**: `python -m pytest tests/test_smt_detect.py tests/test_session_pipeline.py -q`

---

### WAVE 3: Integration

#### Task 3.1: ADD integration tests to tests/test_session_pipeline.py
- **WAVE**: 3 · **AGENT_ROLE**: qa · **DEPENDS_ON**: [2.1] · **PROVIDES**: end-to-end verification
- **IMPLEMENT**: all Integration Tests below.
- **VALIDATE**: `python -m pytest tests/test_session_pipeline.py -q`

**Final Checkpoint**: `python -m pytest tests/test_smt_detect.py tests/test_session_pipeline.py tests/test_smt_state.py -q`

---

## DETAILED LOGIC REFERENCE

This section pins the three subtle mechanisms so the executor doesn't have to re-derive them. Pseudocode is illustrative — match surrounding style when implementing.

### A. Per-target state machine (regular + hidden SMT)

State is a JSON-serializable dict keyed by `(ref_name, direction)`:
```
state[(name, dir)] = {
    "armed": True,          # may fire only when armed
    "last_cond": False,     # divergence-true on the previous evaluation (rising-edge detect)
    "fire_price": None,     # leader price at last fire (for opp-move re-arm)
    "level_price": <float>, # value at last evaluation (running level advance → reset)
}
```
Per evaluation for a `(name, dir)`:
```
cond = leader_touched(level) and not laggard_touched(level)   # touch = wick (regular) / close (hidden)
if level_price_changed(name):           # running day/week advanced
    st.armed = True; st.last_cond = False
# re-arm checks (independent, either re-arms):
if not st.armed:
    if opp_move_since_fire(leader_price, st.fire_price) >= MIN_REARM_OPP_MOVE_PTS_<inst>:
        st.armed = True
    if any(rec.direction == opposite(dir) for rec in batch_so_far):
        st.armed = True
fired = None
if cond and not st.last_cond and st.armed:     # rising edge AND armed
    fired = make_record(...)
    st.armed = False
    st.fire_price = leader_price
st.last_cond = cond
st.level_price = level_price
```
`batch_so_far` = records already produced in THIS call (so an opposite SMT detected on the same bar can re-arm a same-bar opposite pair — order levels deterministically, e.g. by name, for reproducibility).

### B. Fill detection + Fill-B follow-on

Fill state per FVG `name` (carries both instruments' progress):
```
fstate[name] = {"armed": True, "fill_a_fired": False,
                "mnq": {"entered": False, "passed": False},
                "mes": {"entered": False, "passed": False}}
```
Per evaluation (only for FVGs in `paired_fvgs`):
```
for inst in (mnq, mes):
    entered[inst] = wick_into_zone(inst_bar, fvg[inst])      # past near edge, not far
    passed[inst]  = wick_through_far_edge(inst_bar, fvg[inst])
    fstate.inst.entered |= entered[inst]; fstate.inst.passed |= passed[inst]
# Fill-A: leader entered-or-passed, laggard not reached
if armed and rising_edge(A_cond):  emit fill_a; fstate.fill_a_fired = True; armed = False
# Fill-B: both entered, one passed far edge, other still inside.
#   Allowed without re-arm if it follows Fill-A on this same FVG (fill_a_fired==True);
#   otherwise gated by `armed`.
if (fill_a_fired or armed) and rising_edge(B_cond):  emit fill_b; armed = False
# Re-arm (dual gate, §3.1): clears `armed` AND resets fill_a_fired when an opp move
# ≥ MIN_REARM_OPP_MOVE_PTS or an opposite-direction SMT occurs, then the FVG must be
# re-approached for a fresh rising edge.
```
FVG pairing (build `paired_fvgs` in `on_1m_bar` before calling `detect_fill_smts`):
```
mnq_fvgs = {(_ts(name), _side(name)): f for f in liquidities      if f["kind"]=="fvg"}
mes_fvgs = {(_ts(name), _side(name)): f for f in liquidities_mes  if f["kind"]=="fvg"}
paired = [ {"name": k, "side": k[1],
            "mnq": {"top": mnq_fvgs[k]["top"], "bottom": mnq_fvgs[k]["bottom"]},
            "mes": {"top": mes_fvgs[k]["top"], "bottom": mes_fvgs[k]["bottom"]}}
           for k in (mnq_fvgs.keys() & mes_fvgs.keys()) ]
```
`_ts`/`_side` parse the `fvg_{YYYYMMDD_HHMM}_{bull|bear}` name produced by `daily._detect_fvgs`.

### C. `on_1m_bar` insertion point & ordering

Insert the new block **after** `self._update_dynamic_liquidities(now, ...)` (currently `session_pipeline.py:788`) and **before** the existing `_this_5m`/`is_5m` strategy work — but it must NOT touch any existing variable or emit. Exact order inside the block:
```
1. daily = load_daily(); levels_mnq/levels_mes = eligible_levels(...); paired = pair_fvgs(...)
2. records  = detect_regular_smts(...); records += detect_fill_smts(...)
3. if completes_tf(now, "15min"): records += detect_hidden_smts(..., "15m")
   if completes_tf(now, "30min"): records += detect_hidden_smts(..., "30m")
4. self._smt_buffer.add(records, now)
5. cadence = "1m" if 09:30<=ET(now).time()<=10:30 else "5m"
   flat = not load_position().get("active")
   if flat and (cadence=="1m" or (cadence=="5m" and is_5m_boundary(now))):
       self._pending_watch.ingest(self._smt_buffer.get_new(cadence))
   self._pending_watch.update(now, float(mnq_bar_row["Close"]), float(mes_bar_row["Close"]))
6. if is_5m_boundary(now): self._smt_buffer.drain_if_boundary(now)   # AFTER step 5
7. save_smts({"detect_state": self._detect_state, "watch": self._pending_watch.to_dict()})
```
`is_5m_boundary(now)` reuses the existing `is_5m` computation (`session_pipeline.py:790-791`); compute it once and share. The reference consumer reads position via `_smt_state.load_position()` (already imported as `_smt_state`).

---

## TESTING STRATEGY

**⚠️ ALL tests automatable here → 100% automated (pure pandas/python, no UI, no network, no hardware).**

| What | Tool |
|---|---|
| Detection engine, buffers, watch | `pytest` (`tests/test_smt_detect.py`) |
| Pipeline wiring | `pytest` (`tests/test_session_pipeline.py`) |
| State store | `pytest` (`tests/test_smt_state.py`, existing) |

### Unit Tests (`tests/test_smt_detect.py`)

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `python -m pytest tests/test_smt_detect.py -q`

Regular (wick) SMT:
- `test_wick_smt_fires_on_divergence` — MNQ wick touches `day_high`, MES doesn't → one `kind:smt,type:wick,side:bearish` record; leader=mnq.
- `test_no_fire_when_both_touch` — both touch their high → no record.
- `test_no_fire_when_neither_touch` — neither reaches → no record.
- `test_symmetric_leader_mes` — MES leads, MNQ laggs → fires with leader=mes.
- `test_low_level_long_direction` — wick below `day_low` → side bullish / direction long.
- `test_edge_fire_once` — persistent divergence across 3 bars → exactly one record.
- `test_rearm_via_opp_move_pts` — after fire, leader retreats ≥ `MIN_REARM_OPP_MOVE_PTS` then re-touches → second record (new event); below-threshold retreat+retouch → no second record.
- `test_rearm_via_opposite_smt` — opposite-direction SMT in the interim re-arms; re-touch fires again.
- `test_running_level_advance_rearms` — `day_high` advances (new price) → pair re-armed against new level.
- `test_completed_session_only` — ny_morning level not eligible before 12:00 ET; eligible after.

Hidden (body) SMT:
- `test_hidden_close_vs_level_15m` — 15m close beyond level (laggard close not) → `type:body,timeframe:15m`.
- `test_hidden_distinct_from_wick` — same level, wick fired earlier; a later 15m close still produces a separate body record.
- `test_hidden_not_on_1m` — `detect_hidden_smts` only invoked per 15m/30m bar (asserted at pipeline level in 3.1; here assert function tags timeframe correctly for 30m).

Fill SMT:
- `test_fill_pairing_one_sided_no_fire` — FVG only in MNQ → `paired_fvgs` empty → no fill.
- `test_fill_a` — both have the FVG; MNQ entered, MES not reached → `type:fill_a`.
- `test_fill_b` — both entered; MNQ passed far edge, MES still inside → `type:fill_b`.
- `test_fill_b_follow_on` — Fill-A fires, then MES enters, then MNQ passes far edge → Fill-B fires without re-arm.
- `test_fill_independent_b` — both enter same bar (no prior A), one passes → Fill-B.
- `test_fill_entered_vs_passed_boundary` — wick exactly at near edge = entered; exactly at far edge = passed (document inclusivity).
- `test_fill_rearm` — after a fill, re-creation gated by dual re-arm.

SmtBuffer:
- `test_buffer_per_minute_overwrite` — add bar A then bar B; `get_new("1m")` returns only B.
- `test_buffer_5m_accumulates` — 3 adds across a 5m block; `get_new("5m")` returns all 3.
- `test_buffer_drain_at_boundary` — `drain_if_boundary` clears accum only when the 5m floor changes; per-minute unaffected.

PendingSmtWatch:
- `test_watch_preserve_through_drain` — ingest a record, drain the source buffer; `retained()` still has it (copy detached).
- `test_watch_invalidate_on_trend` — price moves ≥ `WATCH_CONFIRM_PTS` in the SMT direction → dropped.
- `test_watch_invalidate_on_contradiction` — opposite-direction SMT ingested → dropped.
- `test_watch_roundtrip` — `from_dict(to_dict())` preserves retained set.

### Integration Tests (`tests/test_session_pipeline.py`)

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `python -m pytest tests/test_session_pipeline.py -q`

- `test_liquidities_mes_populated` — after seeding + a few bars, `load_daily()["liquidities_mes"]` has MES `level` + `fvg` entries; `liquidities` (MNQ) unchanged vs baseline.
- `test_mnq_liquidities_unchanged_regression` — snapshot MNQ `liquidities` before/after the refactor on identical bars → identical.
- `test_detection_runs_every_1m` — craft MNQ-only wick divergence on a 1m bar → buffer per-minute non-empty that bar.
- `test_hidden_only_on_tf_boundary` — divergence present on a non-15m minute → no body record; on the 15m boundary → body record.
- `test_cadence_morning_1m` — at 09:45 ET, flat, new SMT → consumer ingests every 1m.
- `test_cadence_offhours_5m` — at 11:00 ET, consumer ingests only on the 5m boundary; the 5m read returns the accumulated window.
- `test_cadence_boundaries` — 09:29→5m, 09:30→1m, 10:30→1m, 10:31→5m.
- `test_flat_gating` — with an active position, the reference consumer does NOT ingest.
- `test_buffer_drains_after_5m_consumer` — accumulator cleared after the 5m-boundary consumer ran.
- `test_fill_pairing_end_to_end` — construct a 1hr FVG in both instruments same bar → fill possible; one-sided → none.
- `test_restart_reload` — populate watch + detect_state, `save_smts`, new `SessionPipeline`, reload → retained set + edge-state restored from `smts.json`.
- `test_on_1m_bar_events_unchanged` — emitted event list for a scenario equals the pre-change baseline (additive-only guarantee).

### Edge Cases
- **Empty session frames** (bar 0): detection returns `([], state)` — ✅ `test_empty_frames_no_crash`.
- **Level present in one instrument only**: skipped (no pair) — ✅ covered by `test_fill_pairing_one_sided_no_fire` + a level-side analog `test_level_one_sided_no_fire`.
- **Backtest in-memory mode**: `save_smts`/`load_smts` use `_STORE` — ✅ `test_smts_inmemory` in `tests/test_smt_state.py`.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend unit (pytest, test_smt_detect.py) | 27 | |
| ✅ Integration (pytest, test_session_pipeline.py) | 12 | |
| ✅ Store (pytest, test_smt_state.py) | 2 | |
| ⚠️ Manual | 0 | |
| **Total** | 41 | 100% |

**Goal**: 100% path coverage — met; no manual tests.

---

## VALIDATION COMMANDS

### Side-effecting test policy (full-suite runs)

The new code is pure (no broker/IB/network, no process management). New tests carry NO side effects and are NOT marked `integration`.

- **Run side-effecting tests during validation?** ☑ No (default)
- **Deselect command (default-skip):** `python -m pytest tests/ -q` — the repo `pyproject.toml` `addopts` already applies `-m 'not integration'`, excluding live-network/process-lifecycle tests (e.g. `test_ib_realtime`, `test_orchestrator_*`). Do NOT pass `-m integration`.
- **If Yes — exact paths/markers + safe command:** N/A — no opt-in needed; confirm no live orchestrator/IB feed is running before any `-m integration` run regardless.

### Level 0: External Service Validation
N/A — no external services.

### Level 1: Syntax & Import
```bash
python -c "import smt_detect, smt_state, session_pipeline"
```

### Level 2: Unit Tests
```bash
python -m pytest tests/test_smt_detect.py tests/test_smt_state.py -q
```

### Level 3: Integration Tests
```bash
python -m pytest tests/test_session_pipeline.py -q
```

### Level 4: Full suite (regression guard, side-effecting excluded by default)
```bash
python -m pytest tests/ -q
```
Baseline first (before any edits) to record pre-existing failures; compare after implementation — no NEW failures permitted.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] A regular (wick) SMT fires once on the 1m bar where one instrument's wick touches a per-instrument eligible level (completed-session high/low, or running day/week high/low) while the other hasn't touched its corresponding level — leader/laggard symmetric; high→short/bearish, low→long/bullish.
- [ ] A hidden (body) SMT uses the same per-instrument level model but close-vs-level, and fires only on completed 15m and 30m bars (never on intermediate 1m bars), tagged `type:"body"` + timeframe.
- [ ] Re-arm (regular + hidden): a fired `(level, direction)` re-fires only after the dual gate clears (leader opposite move ≥ `MIN_REARM_OPP_MOVE_PTS_*` **or** an intervening opposite-direction SMT) **and** a fresh re-touch; a re-fire is a new event, never a revived one; a running day/week level advance re-arms against the new level.
- [ ] SMT-fills fire against per-instrument 1hr FVGs paired by the same 1hr bar (timestamp+side) in both tickers: Fill-A (leader entered-or-passed, laggard not reached) and Fill-B (both entered, one passed far edge, other still inside), direction from FVG side.
- [ ] Fill-B may follow Fill-A on the same FVG in one continuous move without re-arm; a one-sided FVG (present in only one ticker) never produces a fill.
- [ ] Detection runs every `on_1m_bar`, independent of hypothesis direction and position state.

### Buffers, cadence & consumer
- [ ] Per-minute buffer returns only the just-closed bar's records via `get_new("1m")`; the 5m accumulator returns all records since the last drain via `get_new("5m")`; the accumulator drains at the 5m boundary AFTER consumers run.
- [ ] Cadence is 1m during 09:30–10:30 ET and 5m otherwise (boundaries 09:29→5m, 09:30→1m, 10:30→1m, 10:31→5m); the reference consumer is invoked only when flat (no active position).
- [ ] `PendingSmtWatch` copy-preserves ingested SMTs across a buffer drain and invalidates a retained SMT on a confirming trend move or a contradicting opposite SMT.

### Data & persistence
- [ ] `daily.json` gains an additive `liquidities_mes` block (MES session/day/week levels + 1hr FVGs); the MNQ `liquidities` key and all its readers are unchanged.
- [ ] Edge/re-arm state and the retained set persist to `smts.json` and reload correctly on a fresh `SessionPipeline` (live restart continuity); works in both file and in-memory (backtest) modes.

### Non-functional / no-regression
- [ ] MNQ `liquidities` output and the `on_1m_bar` emitted-event list are byte/list-identical to baseline on identical bars (additive-only guarantee).
- [ ] No prints in production paths; detection functions are total (return `([], state)` on degenerate input, never raise).

### Validation
- [ ] All 41 new tests pass — verified by: `python -m pytest tests/test_smt_detect.py tests/test_session_pipeline.py tests/test_smt_state.py -q`
- [ ] Full suite shows no new failures vs baseline — verified by: `python -m pytest tests/ -q`
- [ ] Modules import cleanly — verified by: `python -c "import smt_detect, smt_state, session_pipeline"`

### Out of Scope
- Migrating the existing 5m-hypothesis SMT path (`hypothesis._compute_divs`, `strategy_smt.detect_smt_*`) onto the new buffer.
- Trade/decision/hypothesis logic in the reference consumer (lifecycle bookkeeping only).
- 4hr-FVG fills; MES TDO/TWO/prev-day fixed levels; body-vs-wick configurability.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] Validation levels 1–4 executed
- [ ] All automated tests created and passing (41)
- [ ] Full suite passes (no new failures vs baseline)
- [ ] MNQ `liquidities` + `on_1m_bar` events regression-verified unchanged
- [ ] No linting/type errors
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Design decisions (from the approved spec):**
- Hidden SMTs anchored to named levels (close-vs-level), NOT the legacy running-close-extreme — user-accepted assumption. If revisited, only `detect_hidden_smts` + its tests change.
- `daily.json` change is additive (`liquidities_mes`); the MNQ `liquidities` key is NOT renamed (avoids the 54-reference blast radius).
- The existing 5m-hypothesis SMT path (`hypothesis._compute_divs`, `strategy_smt.detect_smt_*`) is untouched and runs in parallel; migrating it onto this buffer is a separate future effort.

**Risks & mitigations:**
- *Regression in MNQ liquidities* from refactoring `_update_dynamic_liquidities` into a shared helper → mitigated by `test_mnq_liquidities_unchanged_regression` + running the full `test_session_pipeline.py` against baseline.
- *Additive block changing existing events* in `on_1m_bar` → mitigated by `test_on_1m_bar_events_unchanged` (event-list equality) and keeping all new work after the existing flow with no shared mutable state.
- *Per-bar `save_smts` write cost* (~80k bars in backtest) → in-memory `_STORE` write is cheap; if profiling shows cost, batch to 5m-boundary writes (state is reconstructable). Not optimized pre-emptively (YAGNI).
- *Re-arm thresholds* (`MIN_REARM_OPP_MOVE_PTS_*`, `WATCH_CONFIRM_PTS_*`) are first-guess defaults — exposed as module constants for later tuning; correctness tests assert behavior at/above/below threshold, not specific magnitudes.

**Out of scope (do not build):** hypothesis migration, trade/decision logic in the reference consumer, 4hr-FVG fills, body-vs-wick configurability, MES TDO/TWO/prev-day fixed levels (detection only needs session/day/week + 1hr FVGs).
