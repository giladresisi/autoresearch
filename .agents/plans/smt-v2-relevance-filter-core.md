# Feature: SMT V2 Relevance-Filter Core (Phase 2 of 3)

EXECUTION RULES: implement all changes; delete debug logs you add; leave ALL changes UNSTAGED — no git add/commit; only code changes.

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files. Production code is silent (no `print`/stdout in production paths).

---

## Feature Description

Builds the **relevance-filter infrastructure** for the SMT V2 redesign as a set of pure, exhaustively-unit-tested functions plus a read-only fulfillment-query API, and wires the **active-set computation in SHADOW** inside `session_pipeline.on_1m_bar`. The hypothesis module gains the ability to maintain an "active SMT set" — the unfulfilled, relevant SMTs sourced from `smt_detect.py` — and to select a single **dominant** SMT from that set via a fixed authority ordering. In Phase 2 this active set + dominant are **computed but DO NOT drive direction**: the existing direction engine (`run_hypothesis` / `_determine_direction`) is entirely unchanged. Phase 3 (a separate plan) wires the dominant as the direction source.

This is **PHASE 2 of 3** in the shared SMT V2 redesign:
- Phase 1 (independent): SMT producer / record-emission work in `smt_detect.py`.
- **Phase 2 (this plan): relevance-filter infrastructure + shadow active-set, zero behavior change.**
- Phase 3 (consumes this plan's Contracts B & C): wire dominant → hypothesis direction.

Phase 2 is independent of Phase 1.

## User Story

As a trader running the SMT V2 pipeline,
I want the unfulfilled, relevant SMTs maintained as a single authoritative "active set" with a deterministic dominant selection and a fulfillment-query API,
So that a later phase can drive hypothesis direction from the dominant relevant SMT — and so that in this phase I can observe (in shadow) what that set would be without any risk to live behavior.

## Problem Statement

Today the hypothesis module computes `divs` per 5m via `_compute_divs` (a transient list rebuilt each call from `strategy_smt.detect_smt_*`) and persists them into `hypothesis.json["divs"]` only as a debug payload; they feed `_compute_smt_score` (Rules 3+4) but are never maintained as a *stateful, fulfillment-aware, relevance-filtered* set. Meanwhile `smt_detect.py` is the real per-1m SMT producer (regular/hidden/fill) with rich records (kind/type/side/direction/timeframe/leader/ref_name) and per-key fire/fulfilled/armed state in `smts.json["detect_state"]` — but it exposes **no public fulfillment-query API**, and the hypothesis module has no notion of an "active set", no authority ordering, and no ingest gate. There is therefore no single, testable place that answers "which SMTs are currently relevant, and which one dominates?".

## Solution Statement

- Add **Contract B** pure functions in `hypothesis.py`: `to_record`, `smt_authority`, `dominant`, `ingest_smts`, and a documented **divs record schema** (the persisted active set, migrated to `smt_detect`'s record fields + `tier` + `key` + `fulfilled`).
- Add **Contract C** in `smt_detect.py`: `fulfillment_status(keys, detect_state) -> dict[str, "unfulfilled"|"fulfilled"|"gone"]` — read-only over `detect_state`, deriving the per-key state key exactly as the detection engine does.
- Wire the active-set computation in **SHADOW** inside `session_pipeline.on_1m_bar` / `_run_smt_v2_detection`: each 1m bar, convert the freshly-detected records via `to_record`, drop fulfilled/contradicted/ineligible, `ingest_smts` into the persisted active set, recompute `dominant`, and **store under a debug key in `hypothesis.json`** (`smt_active_set` / `smt_dominant`). **Direction determination is UNCHANGED.**
- Exhaustive unit tests for every authority-ordering case, the ingest gate (flat vs active; proximity-OR-tier with exact-boundary cases), `to_record` schema round-trips across kinds/types/timeframes, `fulfillment_status` (unfulfilled/fulfilled/gone), and invalidation (drop fulfilled / drop contradicted / dominant-dropped re-derive). Plus a shadow no-behavior-change assertion (existing suite green).

## Feature Metadata

**Feature Type**: Enhancement (new infrastructure) + Shadow wiring
**Complexity**: 🔴 Complex (pure-function-heavy, exhaustive testing is the deliverable)
**Primary Systems Affected**: `hypothesis.py` (new pure functions + schema), `smt_detect.py` (new read-only query API), `session_pipeline.py` (shadow wiring only), `smt_state.py` (new `divs` schema is forward-compatible — no signature change), `tests/`
**Dependencies**: None external. Consumes the existing `smt_detect` record shape + `detect_state` key convention.
**Breaking Changes**: None. `hypothesis.json["divs"]` schema is migrated to the new record shape but it is currently a debug-only payload (not read back to drive behavior); `_load`'s forward-compatible merge tolerates the change. No public signature changes.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `smt_detect.py:185–354` — `_detect_level_smts`: the shared engine. **Line 224**: the per-key state key is `f"{_skey(name, direction)}|{rec_type}"` → `"{name}|{direction}|{wick|body}"`. Line 304–320: the emitted record shape (`kind/type/side/direction/timeframe/time/leader/ref_name/mnq_price/mes_price/mnq_lvl_price/mes_lvl_price`). Line 281–290: fulfillment is set on `st["fulfilled"]` when MNQ close follows through `FULFILL_PTS[tier]` past the fire close.
- `smt_detect.py:95–98` — `_skey(name, direction)` = `f"{name}|{direction}"`. **Contract C must reconstruct keys identically.**
- `smt_detect.py:62–83` — `_level_class(name) -> (kind, tier)`: tier is `"week"|"day"|"session"`. **Fills are NOT covered here** — fill records use FVG names and key = the bare FVG name (smt_detect.py:443 `skey = str(name)`); the relevance tier for a fill is `"fill"` (1hr-FVG-fill).
- `smt_detect.py:415–524` — `detect_fill_smts`: fill records (`kind:"fill"`, `type:"fill_a"|"fill_b"`, `timeframe:"1h"`, `ref_name`=FVG name). State key = bare FVG name (no `|`). `st["fulfilled"]` is NOT tracked for fills (only `fill_a_fired`/`fill_b_fired`/`armed`) → Contract C maps a present fill key with no `fulfilled` field to `"unfulfilled"` unless armed cleared by an opposite SMT (see Task 2.1 rules).
- `hypothesis.py:324–425` — `_compute_divs`: current transient divs producer (the OLD `smt-div` record shape: `kind:"smt-div"`, `mnq_div_price`/`mes_div_price`). **Phase 2 does NOT touch `_compute_divs`** — the new active set is sourced from `smt_detect` records, not these.
- `hypothesis.py:663–685` — `_compute_smt_score`: consumes `divs`; **unchanged** (Phase 2 leaves direction scoring intact).
- `hypothesis.py:1229–1244` — `build_hypothesis_from_direction` writes `new_hypothesis` incl. `"divs": divs`. **Phase 2 shadow store** adds sibling debug keys; does not change `divs` semantics here.
- `hypothesis.py:17–22` — cautious-target constants; the ingest proximity gate references cautious targets (`cautious_price_initial`/`cautious_price_secondary`) — see Task 1.3.
- `smt_state.py:127–146` — `DEFAULT_SMTS` (`detect_state`/`watch`) and `DEFAULT_HYPOTHESIS` (`divs:[]`). `_load` (lines 260–279) does a forward-compatible merge so new keys (`smt_active_set`, `smt_dominant`) are tolerated; add them to `DEFAULT_HYPOTHESIS`.
- `session_pipeline.py:1610–1744` — `_run_smt_v2_detection`: the per-1m detection path. `records` (line 1665–1696) is the deduped list of fresh SMT/fill records. `self._detect_state` (line 1666+) is the live `detect_state`. **Shadow wiring inserts here**, after dedup (line 1696), before/around the persist at 1739.
- `session_pipeline.py:1728` — `_flat = not _smt_state.load_position().get("active")` — the flat/active discriminator the ingest gate needs.
- `session_pipeline.py:580–581` — `on_session_start` loads `detect_state` from `smts.json`; the active set lives in `hypothesis.json` (not `smts.json`).
- `tests/test_smt_detect.py:1–80` — builder helpers (`_level`, `_levels`, `_bar`) + the pure-function test style to mirror. `detect_*` are called with plain dicts and an empty `{}` state.
- `pyproject.toml:31–35` — `addopts = "--timeout=60 -m 'not integration'"`; `integration` marker. Full-suite runs deselect integration automatically.

### New Files to Create

- `tests/test_smt_relevance.py` — exhaustive unit tests for Contract B pure functions + invalidation + shadow no-behavior assertion.
- `tests/test_smt_fulfillment.py` — unit tests for Contract C (`fulfillment_status`). (May be merged into `test_smt_detect.py` if preferred; keep separate for clarity.)

### Patterns to Follow

**Pure-function purity**: like `smt_detect.detect_*` — total (never raise on degenerate input; return a safe default), no IO, JSON-serializable inputs/outputs. `smt_authority`/`dominant`/`ingest_smts`/`to_record` take plain dicts/lists and return plain dicts/lists.
**Authority as a sortable tuple**: `smt_authority(record) -> tuple` where a **larger** tuple = more authoritative; `dominant = max(active_set, key=smt_authority)` (None on empty). Mirror `_level_class` ordering semantics.
**Key reconstruction (Contract C)**: reuse the exact convention — level key = `f"{ref_name}|{direction}|{type}"` (type ∈ {wick,body}); fill key = `ref_name`. Centralize in a small helper `_record_key(record)` in `smt_detect.py` so detection and query agree by construction.
**Forward-compatible state**: add `smt_active_set: []` and `smt_dominant: None` (or `""`) to `DEFAULT_HYPOTHESIS`; never read them back to drive behavior in Phase 2.
**Test builders**: reuse/adapt `tests/test_smt_detect.py` `_level`/`_bar` helpers; add a `_record(**overrides)` builder for active-set records.
**Silent production**: no prints anywhere; shadow failures must be swallowed (the shadow block is wrapped so it can never break the live path) — capture nothing to stdout.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Pure infrastructure (Parallel — different files/fns) │
├──────────────────────────────────────────────────────────────┤
│ Task 1.1: hypothesis.py — divs record SCHEMA + to_record()   │
│ Task 1.2: hypothesis.py — smt_authority() + dominant()       │
│ Task 1.3: hypothesis.py — ingest_smts()                      │
│ Task 1.4: smt_detect.py — _record_key() + fulfillment_status │
│           (Contract C)                                        │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Shadow wiring (After Wave 1 — needs B + C)           │
├──────────────────────────────────────────────────────────────┤
│ Task 2.1: session_pipeline._run_smt_v2_detection — shadow    │
│           active-set compute + store (NO direction change)   │
│ Task 2.2: smt_state.DEFAULT_HYPOTHESIS — add debug keys      │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Exhaustive tests (After Wave 2, parallel within)     │
├──────────────────────────────────────────────────────────────┤
│ Task 3.1: tests/test_smt_relevance.py (Contract B + invalid.)│
│ Task 3.2: tests/test_smt_fulfillment.py (Contract C)         │
│ Task 3.3: shadow no-behavior-change assertion + full suite   │
│ Task 3.4: coverage pass — every fn/branch has a named test   │
└──────────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Contract B** (in `hypothesis.py`) — Phase 3 consumes; name EXACTLY:
- `smt_authority(record) -> tuple` — sortable tuple; **larger = more authoritative**, implementing the LOCKED ordering.
- `dominant(active_set: list[dict]) -> dict | None` — `max(active_set, key=smt_authority)`; `None` on empty.
- `ingest_smts(new_records, active_set, *, flat: bool, cautious_targets: dict | None, backing_tier: str | None, x_pts: float) -> list[dict]` — returns the updated active set.
- `to_record(smt_detect_emission: dict) -> dict` — maps a `smt_detect` emission to the **divs record schema** (incl. `tier`, `key`, `fulfilled`, prices).
- The **divs record schema** IS the persisted active set (`hypothesis.json` `divs`, migrated).

**Contract C** (in `smt_detect.py`) — Phase 3 + Task 2.1 consume:
- `fulfillment_status(keys: list[str], detect_state: dict) -> dict[str, str]` — values ∈ `{"unfulfilled","fulfilled","gone"}`; read-only over `detect_state`; `"gone"` = key absent/expired.

**Internal helper** (in `smt_detect.py`): `_record_key(record: dict) -> str` — the single source of truth for the per-key state key; used by `fulfillment_status` and re-used by Task 1.1 `to_record` (import it) so `record["key"]` always matches `detect_state`.

### Checkpoints

- **CP1 (end of Wave 1)**: `uv run python -c "from hypothesis import smt_authority, dominant, ingest_smts, to_record; from smt_detect import fulfillment_status, _record_key; print('B+C OK')"`.
- **CP2 (end of Wave 2)**: `uv run python -c "from session_pipeline import SessionPipeline; print('pipeline OK')"`; existing suite still green (no behavior change).
- **CP3 (end of Wave 3)**: every Contract B/C function + branch has a named passing test; full suite green vs baseline.

---

## IMPLEMENTATION PLAN

### Phase 1: Pure infrastructure (Wave 1)

Tasks 1.1–1.3 are all in `hypothesis.py` but touch **disjoint new functions** — implement as separate function bodies appended in one region to avoid edit collisions; if executed by parallel agents, serialize the final edit. Task 1.4 is in `smt_detect.py` (fully independent).

### Phase 2: Shadow wiring (Wave 2)

Depends on Wave 1 (needs `to_record`/`ingest_smts`/`dominant`/`fulfillment_status`). Task 2.2 (state default keys) is trivial and independent of 2.1.

### Phase 3: Exhaustive tests (Wave 3)

Depends on Wave 2. 3.1/3.2 parallel; 3.3 runs the suite; 3.4 is the coverage gate.

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Pure infrastructure

#### Task 1.1: ADD divs record SCHEMA + `to_record()` in `hypothesis.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.4 for `_record_key` import — implement 1.4 first or stub the key inline then swap]
- **BLOCKS**: [1.3, 2.1, 3.1]
- **PROVIDES**: the **divs record schema** (documented constant/docstring) + `to_record(emission) -> dict`
- **IMPLEMENT**:
  1. Add a module-level docstring block documenting the **divs record schema** (the persisted active-set record). Fields (all JSON-serializable):
     - `kind`: `"smt"` | `"fill"`
     - `type`: `"wick"` | `"body"` | `"fill_a"` | `"fill_b"`
     - `side`: `"bullish"` | `"bearish"`
     - `direction`: `"long"` | `"short"`
     - `timeframe`: `"1m"` | `"15m"` | `"30m"` | `"1h"`
     - `time`: ISO string (fire time)
     - `leader`: `"mnq"` | `"mes"`
     - `ref_name`: level name (e.g. `day_high`) or FVG name (e.g. `fvg_<ts>_<side>`)
     - `tier`: `"ATH"` | `"week"` | `"day"` | `"fill"` | `"session"`
     - `key`: the `smt_detect` `detect_state` key (for fulfillment queries) — produced by `smt_detect._record_key`
     - `fulfilled`: bool
     - prices: `mnq_price`, `mes_price`, and the level/zone comparison price `mnq_lvl_price` (carry `mes_lvl_price` when present). Fills carry the FVG-derived ref price where available; otherwise `mnq_lvl_price=None`.
  2. Implement `to_record(emission: dict) -> dict`. Map from a `smt_detect` emission (the dict appended in `_detect_level_smts` / `detect_fill_smts`, OR the `smt-div` shadow event built in `_run_smt_v2_detection`). Derive:
     - Copy `kind/type/side/direction/timeframe/time/leader/ref_name/mnq_price/mes_price/mnq_lvl_price` (use `.get`).
     - `tier`: for `kind=="smt"` use `smt_detect._level_class(ref_name)[1]` (`week`/`day`/`session`). **ATH special-case**: if `ref_name` resolves to the ATH level (ref_name in {`week_high`,`week_low`} AND the caller flags it as ATH, OR `ref_name == "ATH"`), set `tier="ATH"`. Phase 2: keep ATH detection simple — accept an optional `is_ath: bool` kwarg (default False) OR map `ref_name=="ATH"` → `tier="ATH"`; document that Phase 3 supplies ATH context. For `kind=="fill"` → `tier="fill"`.
     - `key`: `smt_detect._record_key(emission)`.
     - `fulfilled`: default `False` (freshly-emitted SMTs are unfulfilled; invalidation updates this via Contract C).
  3. Keep it total: missing fields default to `None`/`False`; never raise.
- **VALIDATE**: `uv run python -c "from hypothesis import to_record; r=to_record({'kind':'smt','type':'wick','side':'bearish','direction':'short','timeframe':'1m','time':'t','leader':'mnq','ref_name':'day_high','mnq_price':1.0,'mes_price':2.0,'mnq_lvl_price':1.0}); assert r['tier']=='day' and r['key']=='day_high|short|wick' and r['fulfilled'] is False; print(r)"`
- **PATTERN**: import `from smt_detect import _level_class as _smt_level_class, _record_key as _smt_record_key` at top of `hypothesis.py` (alongside existing `strategy_smt` import). Note existing module already imports `smt_state` symbols — follow that import grouping.

---

#### Task 1.2: ADD `smt_authority()` + `dominant()` in `hypothesis.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 3.1]
- **PROVIDES**: `smt_authority(record) -> tuple`, `dominant(active_set) -> dict | None`
- **IMPLEMENT** the LOCKED authority ordering (higher = more authoritative). `smt_authority` returns a tuple compared lexicographically; build it so `max(...)` selects per the rules:
  1. **Tier rank** (primary, high→low): `ATH` and `week` are the top rank (treat ATH≥week as the same top bucket), then `day`, then `fill` (1hr-FVG-fill), then `session` (6hr). Map: `{"ATH": 4, "week": 4, "day": 3, "fill": 2, "session": 1}` → `tier_rank` (unknown → 0).
  2. **Same-level kind** (only meaningful within the SAME tier AND SAME `ref_name`): `wick` (regular) > `body` (hidden). Encode `kind_rank = {"wick": 1, "body": 0}` (fills have no wick/body distinction → treat as 1 so a fill isn't penalized below a hidden SMT of the *same* tier; tier already dominates across levels).
  3. **Recency** (more recent fire wins): parse `time` to a sortable value (ISO → `pandas.Timestamp`; on parse failure use `0`/epoch). Larger = more recent.
  4. **30m > 15m minor sub-tiebreak on hidden SMTs**: `tf_rank = {"30m": 1, "15m": 0}` (others → 0), applied AFTER recency as the lowest-significance element.
  - Tuple order (most-significant first): `(tier_rank, kind_rank, recency_value, tf_rank)`.
  - **Concrete required behaviors** (must hold by construction — assert in tests):
    - `day_high wick > day_high body` (same level → kind_rank breaks tie).
    - any `day` SMT > any `fill` > any `session` SMT (tier dominates regardless of kind/recency).
    - `day_low wick` can outrank `day_high wick` by recency (different levels, same tier+kind → recency decides).
    - a `fill` outranks `session` but NOT `day` (tier_rank: fill=2, session=1, day=3).
  5. `dominant(active_set)`: `return max(active_set, key=smt_authority) if active_set else None`. Total: empty/None → `None`.
- **VALIDATE**: `uv run python -c "from hypothesis import smt_authority, dominant; a={'tier':'day','type':'wick','time':'2026-06-09T10:00:00','timeframe':'1m','ref_name':'day_high'}; b=dict(a, type='body'); assert smt_authority(a) > smt_authority(b); assert dominant([b,a])['type']=='wick'; print('auth OK')"`
- **PATTERN**: parse `time` with `pd.Timestamp(...)` inside a try/except returning a fixed epoch on failure (totality). Keep recency as `int(ts.value)` (nanoseconds) or `0`.

---

#### Task 1.3: ADD `ingest_smts()` in `hypothesis.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.1 (`to_record`), 1.2 (authority for stable ordering — optional)]
- **BLOCKS**: [2.1, 3.1]
- **PROVIDES**: `ingest_smts(new_records, active_set, *, flat, cautious_targets, backing_tier, x_pts) -> list[dict]`
- **IMPLEMENT** the LOCKED ingest pipeline. `new_records` are already in divs-record schema (callers run `to_record` first; defensively run `to_record` again if a record lacks `key`/`tier`). `active_set` is the current persisted list.
  1. **Drop fulfilled/ineligible first** from the *incoming* `new_records`: skip any record with `fulfilled is True` or missing a usable `key`/`direction`.
  2. **Gate by position state**:
     - **FLAT** (`flat=True`): any tier may enter the set. Add the record (subject to dedup, below).
     - **ACTIVE** (`flat=False`): a new SMT enters only if **either**:
       - (proximity) its ref level price is within `x_pts` of a cautious target (initial **or** secondary), **OR**
       - (tier) `tier_rank(record.tier) >= tier_rank(backing_tier)`.
       - **Boundary semantics (test exactly)**: distance `== x_pts` **passes** (inclusive `<=`); `tier == backing_tier` **passes** (inclusive `>=` on tier_rank). Distance `> x_pts` with `tier_rank < backing_tier` → reject.
     - The cautious targets come from `cautious_targets` dict: read `cautious_price_initial` and `cautious_price_secondary` (the hypothesis fields; either may be `""`/absent → that target contributes no proximity). The record's "ref level price" = `mnq_lvl_price` if present else `mnq_price`.
  3. **Dedup / supersede**: maintain at most one active record per `key`. If an incoming record shares a `key` with an existing active record, replace the older with the newer (more-recent `time`). Preserve insertion of genuinely new keys.
  4. **Return** the updated active set (a new list; do not mutate the input list in place — copy). Keep it total: `new_records=None` → return `list(active_set or [])`.
  - Add a module constant `RELEVANCE_X_PTS = 25.0` (first-guess; tunable) so the pipeline has a default `x_pts`. Document it as tunable next to the cautious constants (`hypothesis.py:17–26`).
- **VALIDATE**: `uv run python -c "from hypothesis import ingest_smts; rec={'key':'day_high|short|wick','tier':'day','direction':'short','time':'t','mnq_lvl_price':100.0,'fulfilled':False}; out=ingest_smts([rec], [], flat=True, cautious_targets=None, backing_tier=None, x_pts=25.0); assert len(out)==1; print('ingest OK')"`
- **PATTERN**: tier-rank helper shared with `smt_authority` — factor a private `_tier_rank(tier) -> int` so both use the same mapping. Proximity uses `abs(level_price - target_price) <= x_pts`.

---

#### Task 1.4: ADD `_record_key()` + `fulfillment_status()` (Contract C) in `smt_detect.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [1.1, 2.1, 3.2]
- **PROVIDES**: `_record_key(record) -> str`, `fulfillment_status(keys, detect_state) -> dict[str,str]`
- **IMPLEMENT**:
  1. `_record_key(record: dict) -> str` — single source of truth, matching the detection engine EXACTLY:
     - For level SMTs (`kind=="smt"`): `f"{_skey(ref_name, direction)}|{type}"` = `f"{ref_name}|{direction}|{type}"` where `type ∈ {"wick","body"}`. (Mirror `_detect_level_smts` line 224.)
     - For fills (`kind=="fill"`): `str(ref_name)` (the bare FVG name; mirror `detect_fill_smts` line 443).
     - Total: missing fields → best-effort string; never raise.
  2. `fulfillment_status(keys: list[str], detect_state: dict) -> dict[str,str]` — read-only:
     - For each key: if `key not in detect_state` → `"gone"`.
     - Else read `st = detect_state[key]`. If `st.get("fulfilled") is True` → `"fulfilled"`. Otherwise → `"unfulfilled"`.
     - **Fills**: fill state dicts (keyed by bare FVG name) have NO `fulfilled` field — `st.get("fulfilled")` is falsy → `"unfulfilled"` (a present, non-fulfilled fill key is unfulfilled by definition in Phase 2). Document this; Phase 3 may extend fill fulfillment.
     - Do NOT mutate `detect_state` (read-only). Total: `keys=None` → `{}`; never raise.
  3. Export both in the module's public surface (they are importable; `_record_key` is "internal" by underscore convention but imported by `hypothesis.to_record` — acceptable, document the cross-module reuse).
- **VALIDATE**: `uv run python -c "from smt_detect import _record_key, fulfillment_status; assert _record_key({'kind':'smt','ref_name':'day_high','direction':'short','type':'wick'})=='day_high|short|wick'; assert _record_key({'kind':'fill','ref_name':'fvg_x_bull'})=='fvg_x_bull'; ds={'day_high|short|wick':{'fulfilled':True},'fvg_x_bull':{'armed':False}}; assert fulfillment_status(['day_high|short|wick','fvg_x_bull','nope'], ds)=={'day_high|short|wick':'fulfilled','fvg_x_bull':'unfulfilled','nope':'gone'}; print('C OK')"`
- **PATTERN**: reuse `_skey` (smt_detect.py:95). Keep `fulfillment_status` a pure read — copy nothing, mutate nothing.

---

### WAVE 2: Shadow wiring

#### Task 2.1: SHADOW active-set compute in `session_pipeline._run_smt_v2_detection`

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.1, 1.2, 1.3, 1.4]
- **BLOCKS**: [3.3]
- **PROVIDES**: per-1m shadow active-set + dominant stored in `hypothesis.json` debug keys; **ZERO direction change**
- **IMPLEMENT** (insert AFTER the dedup at `session_pipeline.py:1696` and BEFORE `self._smt_buffer.add(records, now)` at 1720, OR right before the `save_smts` persist at 1739 — choose a spot where `records`, `self._detect_state`, and `_flat` are all available):
  1. Wrap the entire shadow block in `try/except Exception: pass` so it can NEVER affect the live path (silent — no prints, no re-raise). This is the SHADOW safety guarantee.
  2. Compute `flat = not _smt_state.load_position().get("active")` (the same value computed at line 1728 — reuse if already computed, else compute locally).
  3. Determine `backing_tier`: from the active position if present (`_smt_state.load_position().get("active", {}).get("backing_tier")`), else `None`. **Note**: `backing_tier` is a NEW field not yet written by strategy — in Phase 2 it will be `None` (FLAT) or absent (ACTIVE) → ingest tier-gate falls back to proximity-only when `backing_tier` is None. Document that Phase 3 / strategy populates it.
  4. Load the current active set + cautious targets from `hypothesis.json`:
     ```python
     _hyp = _smt_state.load_hypothesis()
     _active = _hyp.get("smt_active_set", []) or []
     _ctargets = {
         "cautious_price_initial":   _hyp.get("cautious_price_initial", ""),
         "cautious_price_secondary": _hyp.get("cautious_price_secondary", ""),
     }
     ```
  5. Convert this bar's fresh `records` to divs-record schema: `new_recs = [_hyp_mod.to_record(r) for r in records]`.
  6. **Invalidation BEFORE ingest** — drop active-set records that are fulfilled/gone via Contract C, then drop contradicted:
     ```python
     _status = _smt_detect.fulfillment_status([r["key"] for r in _active], self._detect_state)
     _active = [r for r in _active if _status.get(r.get("key")) == "unfulfilled" and not r.get("fulfilled")]
     ```
     Mark/drop records whose status is `"fulfilled"`/`"gone"`. (Contradiction is handled inside `ingest_smts` dedup-by-key + the opposite-direction rule; if a lighter contradiction drop is desired, also drop active records whose `direction` is opposite a *new* dominant — but keep Phase 2 minimal: rely on `ingest_smts`.)
  7. `_active = _hyp_mod.ingest_smts(new_recs, _active, flat=flat, cautious_targets=_ctargets, backing_tier=backing_tier, x_pts=_hyp_mod.RELEVANCE_X_PTS)`.
  8. `_dom = _hyp_mod.dominant(_active)`.
  9. **Store under debug keys only** — re-load hypothesis, set `smt_active_set`/`smt_dominant`, save. **DO NOT touch `direction`** or any field the strategy/executor reads:
     ```python
     _hyp2 = _smt_state.load_hypothesis()
     _hyp2["smt_active_set"] = _active
     _hyp2["smt_dominant"]   = _dom
     _smt_state.save_hypothesis(_hyp2)
     ```
  10. Confirm `import session_pipeline as` already binds `_hyp_mod` (hypothesis) and `_smt_state`; add `import smt_detect as _smt_detect` (or `from smt_detect import fulfillment_status`) at the top of the method's existing local import (line 1630) if not already module-imported.
- **VALIDATE**:
  - `uv run python -c "from session_pipeline import SessionPipeline; print('pipeline imports OK')"`.
  - Run the existing pipeline/backtest tests — they MUST stay green (shadow is inert). `uv run python -m pytest tests/test_session_pipeline.py tests/test_smt_backtest.py -q` (skip if those names differ; use the actual pipeline test module).
- **PATTERN**: `direction` and every existing `hypothesis.json` field are untouched; only `smt_active_set`/`smt_dominant` are added. The whole block is exception-isolated.

---

#### Task 2.2: ADD shadow debug keys to `DEFAULT_HYPOTHESIS`

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.3]
- **PROVIDES**: forward-compatible defaults so `load_hypothesis` always returns the debug keys
- **IMPLEMENT**: in `smt_state.py:132–146` `DEFAULT_HYPOTHESIS`, add:
  ```python
  "smt_active_set": [],
  "smt_dominant":   None,
  ```
  Do NOT change any existing key. `_load`'s forward-compatible merge (lines 277–279) already preserves file values and back-fills new defaults.
- **VALIDATE**: `uv run python -c "from smt_state import DEFAULT_HYPOTHESIS; assert DEFAULT_HYPOTHESIS['smt_active_set']==[] and DEFAULT_HYPOTHESIS['smt_dominant'] is None; print('defaults OK')"`
- **PATTERN**: mirror the additive `liquidities_mes` precedent in `DEFAULT_DAILY` (smt_state.py:118–122).

---

### WAVE 3: Exhaustive tests

#### Task 3.1: CREATE `tests/test_smt_relevance.py` — Contract B + invalidation

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1, 1.2, 1.3]
- **BLOCKS**: [3.4]
- **PROVIDES**: exhaustive Contract B coverage
- **IMPLEMENT** (named tests — minimum set, all ✅ required):
  - **Authority ordering** (`smt_authority`/`dominant`):
    - ✅ `test_day_high_wick_beats_day_high_body` — same level, wick > body.
    - ✅ `test_any_day_beats_any_fill` — `day` SMT > `fill` regardless of kind/recency.
    - ✅ `test_any_fill_beats_any_session` — `fill` > `session`.
    - ✅ `test_fill_does_not_beat_day` — `fill` < `day`.
    - ✅ `test_day_low_wick_outranks_day_high_wick_by_recency` — different levels, same tier+kind, newer time wins.
    - ✅ `test_ath_and_week_top_bucket` — `ATH` and `week` share the top tier rank (neither auto-dominates the other on tier alone; recency/kind decides).
    - ✅ `test_30m_outranks_15m_hidden_subtiebreak` — same tier/kind/recency, 30m > 15m (minor sub-tiebreak).
    - ✅ `test_dominant_empty_returns_none` — `dominant([]) is None`.
    - ✅ `test_dominant_picks_highest_authority` — mixed set, returns the expected record.
  - **`to_record` schema round-trip** (smt/fill × wick/body × timeframes):
    - ✅ `test_to_record_wick_1m` — tier=`day`, key=`day_high|short|wick`, fulfilled False, all schema fields present.
    - ✅ `test_to_record_body_15m` and ✅ `test_to_record_body_30m` — type=body, tier from level, key suffix `|body`, timeframe preserved.
    - ✅ `test_to_record_fill_a_1h` and ✅ `test_to_record_fill_b_1h` — kind=fill, tier=`fill`, key=bare FVG name.
    - ✅ `test_to_record_week_tier` — `week_high` → tier `week`.
    - ✅ `test_to_record_session_tier` — `ny_morning_high` → tier `session`.
    - ✅ `test_to_record_ath_tier` — ATH path (`ref_name=="ATH"` or `is_ath=True`) → tier `ATH`.
    - ✅ `test_to_record_total_on_missing_fields` — partial emission doesn't raise; defaults applied.
  - **`ingest_smts` gate**:
    - ✅ `test_ingest_flat_any_tier_enters` — FLAT: a `session` SMT enters.
    - ✅ `test_ingest_active_proximity_enters` — ACTIVE: level within `x_pts` of a cautious target enters even when tier < backing.
    - ✅ `test_ingest_active_tier_enters` — ACTIVE: tier >= backing enters even when far from targets.
    - ✅ `test_ingest_active_rejects_far_low_tier` — ACTIVE: far AND tier < backing → rejected.
    - ✅ `test_ingest_boundary_exact_x_pts_passes` — distance exactly `== x_pts` passes (inclusive).
    - ✅ `test_ingest_boundary_exact_backing_tier_passes` — `tier == backing_tier` passes (inclusive `>=`).
    - ✅ `test_ingest_drops_incoming_fulfilled` — incoming `fulfilled=True` never enters.
    - ✅ `test_ingest_dedup_by_key_supersede` — same key, newer time replaces older.
    - ✅ `test_ingest_none_records_returns_copy` — `new_records=None` returns the (copied) active set.
  - **Invalidation** (active-set lifecycle; simulate Task 2.1's drop logic against Contract C in a small helper or via direct list filtering):
    - ✅ `test_invalidation_drops_fulfilled` — a record whose key is `fulfilled` in detect_state is removed.
    - ✅ `test_invalidation_drops_gone` — a record whose key is absent (`gone`) is removed.
    - ✅ `test_invalidation_drops_contradicted` — an opposite-direction record is dropped (via ingest dedup/contradiction rule).
    - ✅ `test_invalidation_dominant_redrives_after_drop` — after the current dominant is dropped, `dominant` re-derives the next-highest.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_relevance.py -v`
- **PATTERN**: mirror `tests/test_smt_detect.py` builders. Add a `_rec(**kw)` helper defaulting a full valid divs-record, overridable per test.

---

#### Task 3.2: CREATE `tests/test_smt_fulfillment.py` — Contract C

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.4]
- **BLOCKS**: [3.4]
- **PROVIDES**: exhaustive Contract C coverage
- **IMPLEMENT** (all ✅ required):
  - ✅ `test_record_key_level_wick` — `day_high|short|wick`.
  - ✅ `test_record_key_level_body` — `week_low|long|body`.
  - ✅ `test_record_key_fill_bare_name` — fill → bare FVG name (no `|`).
  - ✅ `test_record_key_total_on_missing` — partial record doesn't raise.
  - ✅ `test_fulfillment_unfulfilled` — present key, `fulfilled` falsy → `"unfulfilled"`.
  - ✅ `test_fulfillment_fulfilled` — present key, `fulfilled=True` → `"fulfilled"`.
  - ✅ `test_fulfillment_gone` — absent key → `"gone"`.
  - ✅ `test_fulfillment_fill_present_unfulfilled` — fill key present (no `fulfilled` field) → `"unfulfilled"`.
  - ✅ `test_fulfillment_read_only` — `detect_state` is NOT mutated (deep-compare before/after).
  - ✅ `test_fulfillment_empty_keys` — `keys=[]`/`None` → `{}`.
  - ✅ `test_fulfillment_matches_detection_key` — a key produced by `_record_key` from an emission matches a key actually present in a `detect_state` built by `detect_regular_smts` (integration-of-pure-funcs: fire a wick SMT, assert the record's key is in the returned state and `fulfillment_status` reports `"unfulfilled"`).
- **VALIDATE**: `uv run python -m pytest tests/test_smt_fulfillment.py -v`
- **PATTERN**: build `detect_state` both by hand and via `detect_regular_smts(...)` (reuse `tests/test_smt_detect.py` helpers) for the round-trip test.

---

#### Task 3.3: SHADOW no-behavior-change assertion + full suite

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: [3.4]
- **PROVIDES**: proof the shadow wiring changes nothing observable to direction/strategy
- **IMPLEMENT**:
  1. Add a test (in `tests/test_smt_relevance.py` or the pipeline test module) that drives `SessionPipeline` over a small synthetic bar sequence (reuse the existing pipeline test fixtures/builders if present) and asserts:
     - ✅ `test_shadow_does_not_change_direction` — `hypothesis.json["direction"]` after running with the shadow block is **identical** to a baseline where the shadow block is a no-op. Practically: assert direction is driven only by the existing engine — e.g. run the pipeline and assert `smt_active_set`/`smt_dominant` are populated (shadow ran) while `direction` equals what `run_hypothesis` alone produced. If a true A/B is impractical, assert: (a) `smt_active_set` key exists and is a list, (b) `smt_dominant` is a record-or-None, (c) `direction` equals the value `run_hypothesis` returns when called directly on the same inputs.
     - ✅ `test_shadow_block_exception_is_swallowed` — monkeypatch `to_record` (or `ingest_smts`) to raise, run one bar, assert no exception propagates and the live path still emits its normal events / writes `direction`.
  2. **Parity note**: document in the test module docstring that Phase 2 is shadow-only; existing suite green == parity.
- **VALIDATE**:
  - Baseline first: `uv run python -m pytest tests/ -q` BEFORE Wave 2 wiring is enabled (or stash the shadow block) → record pass/fail/skip counts.
  - After: `uv run python -m pytest tests/ -q` → **zero new failures**. (pyproject `addopts` applies `-m 'not integration'` automatically.)
- **PATTERN**: monkeypatch via `monkeypatch.setattr("hypothesis.to_record", _raiser)`.

---

#### Task 3.4: COVERAGE PASS — every function/branch named

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.1, 3.2, 3.3]
- **BLOCKS**: []
- **PROVIDES**: a coverage matrix mapping each Contract B/C function + branch to a named test; ✅/⚠️ marks; gaps filled
- **IMPLEMENT**:
  1. Enumerate: `to_record` (smt/fill, each tier branch, ATH branch, total branch), `smt_authority` (each tuple element's tiebreak), `dominant` (empty/non-empty), `ingest_smts` (flat, active-proximity, active-tier, reject, both boundaries, fulfilled-drop, dedup, none), `_tier_rank` (every tier + unknown), `_record_key` (level/fill/total), `fulfillment_status` (unfulfilled/fulfilled/gone/fill/read-only/empty).
  2. For each, confirm a named test exists; mark ✅. Any ⚠️ (uncovered branch) → add a test until ✅.
  3. Record the matrix in the **TESTING STRATEGY → Coverage Matrix** section of this plan as the deliverable's checklist (or in the test module docstring).
- **VALIDATE**: `uv run python -m pytest tests/test_smt_relevance.py tests/test_smt_fulfillment.py -q` → all pass; then full suite green.
- **PATTERN**: no `coverage.py` requirement; the matrix is the named-test enumeration.

---

## TESTING STRATEGY

| What | Tool | Location | Run command |
|---|---|---|---|
| Contract B (authority/dominant/ingest/to_record) + invalidation | pytest | `tests/test_smt_relevance.py` | `uv run python -m pytest tests/test_smt_relevance.py -v` |
| Contract C (`_record_key`/`fulfillment_status`) | pytest | `tests/test_smt_fulfillment.py` | `uv run python -m pytest tests/test_smt_fulfillment.py -v` |
| Existing SMT detection (must stay green) | pytest | `tests/test_smt_detect.py` | `uv run python -m pytest tests/test_smt_detect.py -q` |
| Pipeline shadow (no-behavior-change) | pytest | pipeline test module | `uv run python -m pytest tests/test_session_pipeline.py -q` (use actual module name) |
| Full suite | pytest | `tests/` | `uv run python -m pytest tests/ -q` |

### Coverage Matrix (fill during Task 3.4)

| Function | Branch / case | Named test | Mark |
|---|---|---|---|
| `to_record` | wick/1m → tier day, key | `test_to_record_wick_1m` | ✅ |
| `to_record` | body/15m, body/30m | `test_to_record_body_15m`/`_30m` | ✅ |
| `to_record` | fill_a/fill_b → tier fill | `test_to_record_fill_a_1h`/`_b_1h` | ✅ |
| `to_record` | week/session/ATH tier | `test_to_record_week_tier`/`_session_tier`/`_ath_tier` | ✅ |
| `to_record` | total on missing | `test_to_record_total_on_missing_fields` | ✅ |
| `smt_authority` | tier rank | `test_any_day_beats_any_fill`/`test_any_fill_beats_any_session`/`test_fill_does_not_beat_day` | ✅ |
| `smt_authority` | kind (wick>body) | `test_day_high_wick_beats_day_high_body` | ✅ |
| `smt_authority` | recency | `test_day_low_wick_outranks_day_high_wick_by_recency` | ✅ |
| `smt_authority` | 30m>15m subtiebreak | `test_30m_outranks_15m_hidden_subtiebreak` | ✅ |
| `smt_authority` | ATH≡week top bucket | `test_ath_and_week_top_bucket` | ✅ |
| `dominant` | empty / non-empty | `test_dominant_empty_returns_none`/`test_dominant_picks_highest_authority` | ✅ |
| `ingest_smts` | flat any tier | `test_ingest_flat_any_tier_enters` | ✅ |
| `ingest_smts` | active proximity | `test_ingest_active_proximity_enters` | ✅ |
| `ingest_smts` | active tier | `test_ingest_active_tier_enters` | ✅ |
| `ingest_smts` | active reject | `test_ingest_active_rejects_far_low_tier` | ✅ |
| `ingest_smts` | boundary == x_pts | `test_ingest_boundary_exact_x_pts_passes` | ✅ |
| `ingest_smts` | boundary == backing_tier | `test_ingest_boundary_exact_backing_tier_passes` | ✅ |
| `ingest_smts` | drop incoming fulfilled | `test_ingest_drops_incoming_fulfilled` | ✅ |
| `ingest_smts` | dedup supersede | `test_ingest_dedup_by_key_supersede` | ✅ |
| `ingest_smts` | none records | `test_ingest_none_records_returns_copy` | ✅ |
| invalidation | drop fulfilled/gone/contradicted/redrive | `test_invalidation_*` (4) | ✅ |
| `_record_key` | level/fill/total | `test_record_key_*` (4) | ✅ |
| `fulfillment_status` | unfulfilled/fulfilled/gone/fill/read-only/empty/round-trip | `test_fulfillment_*` (7) | ✅ |
| shadow | no direction change / exception swallowed | `test_shadow_does_not_change_direction`/`test_shadow_block_exception_is_swallowed` | ✅ |

### Edge Cases

- Distance **exactly** `x_pts` and tier **exactly** `backing_tier` (inclusive boundaries) — explicit named tests.
- `cautious_targets` with `""`/absent fields → that target contributes no proximity (no crash).
- `backing_tier=None` while ACTIVE → tier gate falls back to proximity-only.
- `time` unparseable → recency = epoch (totality), authority still well-defined.
- Empty/`None` inputs to every Contract B/C function → safe defaults, never raise.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | ~45 | 100% |
| ⚠️ Manual | 0 | 0% |

Manual tests: none. All deliverables are pure functions or shadow state — fully unit-testable.

---

## VALIDATION COMMANDS

**Side-effecting policy**: Do NOT run integration/IB/orchestrator/live tests. The full suite is run via `uv run python -m pytest tests/ -q` — `pyproject.toml` `addopts` applies `-m 'not integration'` automatically. Never invoke `-m integration`, the orchestrator, `trade.py start`, or any live/IB-network path. Windows host.

### Level 1: Imports OK (CP1)

```bash
uv run python -c "from hypothesis import smt_authority, dominant, ingest_smts, to_record, RELEVANCE_X_PTS; print('B OK')"
uv run python -c "from smt_detect import fulfillment_status, _record_key; print('C OK')"
uv run python -c "from session_pipeline import SessionPipeline; print('pipeline OK')"
uv run python -c "from smt_state import DEFAULT_HYPOTHESIS; assert 'smt_active_set' in DEFAULT_HYPOTHESIS; print('defaults OK')"
```

### Level 2: Unit Tests (CP3)

```bash
uv run python -m pytest tests/test_smt_relevance.py -v
uv run python -m pytest tests/test_smt_fulfillment.py -v
uv run python -m pytest tests/test_smt_detect.py -q
```

### Level 3: Full Suite (no regressions)

```bash
uv run python -m pytest tests/ -q
```

Expected: zero new failures vs the pre-change baseline (record the baseline pass/fail/skip BEFORE enabling Wave 2 wiring).

---

## ACCEPTANCE CRITERIA

- [ ] `hypothesis.to_record(emission)` returns the documented **divs record schema** (kind/type/side/direction/timeframe/time/leader/ref_name/tier/key/fulfilled + prices); tier derived via `_level_class` (smt→week/day/session, fill→fill, ATH special-cased); `key` derived via `smt_detect._record_key`; `fulfilled` defaults False.
- [ ] `hypothesis.smt_authority(record)` returns a sortable tuple implementing the LOCKED ordering (tier ATH≡week > day > fill > session; same level wick>body; then recency; then 30m>15m). All four concrete required behaviors hold: day_high wick > day_high body; any day > any fill > any session; day_low wick can outrank day_high wick by recency; fill > session but fill < day.
- [ ] `hypothesis.dominant(active_set)` returns the top-authority record, or `None` on empty.
- [ ] `hypothesis.ingest_smts(new_records, active_set, *, flat, cautious_targets, backing_tier, x_pts)` implements: drop fulfilled/ineligible first; FLAT → any tier enters; ACTIVE → enter only if within `x_pts` of a cautious target (initial OR secondary) OR tier_rank >= backing tier_rank; inclusive boundaries at exactly `x_pts` and exactly `backing_tier`; dedup/supersede by key; returns a new list (no input mutation).
- [ ] `RELEVANCE_X_PTS` tunable constant exists in `hypothesis.py`, documented next to the cautious constants.
- [ ] `smt_detect._record_key(record)` reconstructs the `detect_state` key EXACTLY (level → `name|direction|type`; fill → bare FVG name).
- [ ] `smt_detect.fulfillment_status(keys, detect_state)` returns per-key `"unfulfilled"|"fulfilled"|"gone"`, read-only (no mutation of `detect_state`), total on empty/None.
- [ ] `session_pipeline._run_smt_v2_detection` computes the active set + dominant in SHADOW each 1m bar (invalidate via Contract C → `ingest_smts` → `dominant`), stores them under `hypothesis.json["smt_active_set"]`/`["smt_dominant"]` ONLY, wraps the whole block in exception isolation, and **does NOT change `direction` or any field the strategy/executor reads**.
- [ ] `smt_state.DEFAULT_HYPOTHESIS` includes `smt_active_set: []` and `smt_dominant: None` (forward-compatible; no existing key changed).
- [ ] Direction determination is UNCHANGED — existing `run_hypothesis`/`_determine_direction`/`_compute_smt_score`/`_compute_divs` untouched in behavior.
- [ ] Exhaustive tests exist and pass: every authority case; ingest gate incl. both exact boundaries; `to_record` round-trips for smt/fill × wick/body × timeframes; `fulfillment_status` unfulfilled/fulfilled/gone; invalidation (drop fulfilled, drop contradicted, dominant-dropped re-derive); shadow no-behavior-change + exception-swallowed.
- [ ] Coverage matrix complete — every Contract B/C function and branch has a named test, all ✅.
- [ ] Production code is silent (no prints). Existing full suite green (zero regressions). All changes UNSTAGED.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed (CP1, CP2, CP3)
- [ ] All validation levels executed (1–3)
- [ ] All automated tests created and passing
- [ ] Full test suite passes (no regressions vs recorded baseline)
- [ ] No linting/type errors (imports clean, no undefined names)
- [ ] Coverage matrix filled — every function/branch ✅
- [ ] Shadow wiring confirmed inert (direction unchanged; exception-isolated)
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed; no `git add`/`git commit`**

---

## NOTES

**ATH tier in Phase 2**: `_level_class` only returns `week`/`day`/`session` — it has no `ATH` notion (ATH is a dynamic running value carried in `global.json["all_time_high"]`, surfaced in hypothesis as the synthetic `"ATH"` cautious-target name at `hypothesis.py:74–75`). For Phase 2, `to_record` treats `tier="ATH"` only when explicitly flagged (`ref_name=="ATH"` or an `is_ath=True` kwarg). Since `smt_detect` never emits an `"ATH"` ref_name today, in practice the shadow active set will see week/day/fill/session tiers; the ATH branch is tested but not yet exercised by the live producer. Phase 3 is responsible for supplying ATH context (e.g. marking a `week_high` SMT as ATH when `week_high ≈ all_time_high`). Authority maps ATH and week to the same top bucket so this is forward-safe.

**`backing_tier` is a new field**: no current strategy/position code writes `backing_tier` into `position.json["active"]`. In Phase 2 it is therefore `None`/absent → the ACTIVE tier gate degrades to proximity-only. This is intentional: Phase 2 must not change strategy. Phase 3 (or a strategy task) populates `backing_tier` when a position opens (the tier of the SMT that backed the trade). Tests cover the gate with explicit `backing_tier` values so the logic is verified independently of the producer.

**`divs` field reuse**: the LOCKED design says the active set is persisted in `hypothesis.json` `divs` "migrated to smt_detect's record schema". In Phase 2, to guarantee zero behavior change, the shadow active set is stored under NEW keys (`smt_active_set`/`smt_dominant`) rather than overwriting `divs` — because `divs` is currently written by `build_hypothesis_from_direction` (hypothesis.py:1235) from `_compute_divs` and embedded in `hyp_event` (line 1278–1280) for logging/plotting. Overwriting `divs` now would change that payload (a visible/observable change). Phase 3 — when the dominant drives direction — migrates `divs` to the new schema and retires the old `_compute_divs` payload. Document this divergence so Phase 3 knows the migration is deferred. (If the executor prefers to store into `divs` immediately, it MUST first confirm nothing reads the old `divs` shape for behavior — `_compute_smt_score` reads the *in-memory* `divs` list inside `run_hypothesis`, not the persisted one, so the persisted `divs` is debug-only — but keeping a separate key is the safer shadow choice.)

**Key agreement is load-bearing**: `to_record` MUST derive `record["key"]` from `smt_detect._record_key`, and `_record_key` MUST mirror the detection engine's key construction (level `name|direction|type` at smt_detect.py:224; fill bare name at smt_detect.py:443). The round-trip test (`test_fulfillment_matches_detection_key`) guards this — if the detection engine's key convention ever changes, that test fails loudly. Centralizing both detection and query on `_record_key` (refactor optional in Phase 2; at minimum re-use `_skey`) is the durable fix.

**Shadow safety**: the entire shadow block in `_run_smt_v2_detection` is wrapped in `try/except Exception: pass` with no logging — a defect in the relevance infrastructure can never break live detection/emission or direction. This is acceptable precisely because Phase 2 is shadow; Phase 3 will remove the blanket swallow once the path is load-bearing and add structured error capture instead.
