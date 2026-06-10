# Feature: SMT V2 — Phase 1: Decouple Active-Position Management from the Live Hypothesis

EXECUTION RULES: implement all changes; delete debug logs you add; leave ALL changes UNSTAGED — no git add/commit; only code changes.

⚠️ Medium. This is **Phase 1 of 3** of the broader SMT-v2 hypothesis redesign. It is the foundational decoupling and **must be independently shippable and testable** — it ships a behavior change (no force-close on mismatch) plus a frozen-snapshot contract that Phase 3 consumes, with **zero behavior drift on the common (non-flipping) trade**.

Validate documentation and codebase patterns before implementing. Match the naming of existing helpers/fields. Verify every quoted line number against the working tree before editing — the tree is the current branch HEAD (`89a5e27`, master + SMT-v2 detection/three-updates) and contains `smt_detect.py` plus the refactored `session_pipeline.py`/`hypothesis.py`/`trend.py`/`strategy.py`. Quoted lines are approximate.

### Line numbers verified against the working tree (HEAD `89a5e27`)

These were read directly and are accurate as of this writing; re-confirm if the tree moves:
- `trend.py`: `direction = hypothesis.get("direction","none")` at **L196**; live cautious ladder reads at **L202-207**; `_ath_secondary` at **L267-272**; `if direction=="none": return None` at **L277-278**; global-trend `trend-broken` reset at **L280-299**; `active = position.get("active", {})` at **L307**; Step-3 `if active:` at **L312**; break checks at **L416 / L524 / L567 / L612**; Step-4 flat scan begins **L629**.
- `strategy.py`: pending-stop-entry cancel at **L300-312**; stop-entry fill builds `active` at **L385-395** + recompute at **L404-409**; market-entry builds `active` at **L484-491** + recompute at **L497-502**; direction-mismatch close at **L575-587** with manual exemption at **L579**.
- `live_orders.py`: `_register_downgraded_fill` builds `active` at **L241-249**, recompute at **L255**; `place_market_entry` builds `active` at **L369-377**; `stop_entry_filled` at **L424-438** (updates `active["stop"]` only — no `active` creation).
- `hypothesis.py`: `recompute_cautious_for_fill` at **L154-181** (no-ops under manual lock at L174; mutates `hypothesis` only).
- `smt_state.py`: `DEFAULT_POSITION` at **L148-158** (`active` defaults to `{}`).
- Existing tests to update: `tests/test_smt_strategy_v2.py` `TestActivePosition` at **L389-441**; `tests/test_smt_trend.py` `_active_position()` at **L63-72**, `redirect_paths` fixture at **L85-90**.

---

## Feature Description

Today the live trading system manages an open position off the **live, mutable** `hypothesis["direction"]` and the **live, mutable** hypothesis cautious ladder (`cautious_price_initial` / `cautious_price_secondary` + their `_level` tags). Two coupling problems follow:

1. **trend.py** reads `direction = hypothesis.get("direction")` (≈L196) and keys *all* of its Step-3 active-position cautious management off it — the `direction=="none"` early return (≈L277), the unarmed scan (3a), the `initial_surpassed`/`secondary_surpassed` arm-confirm (3a2/3b2), the `initial` and `secondary` break checks (≈L416/L524/L567/L612), and the trailing logic — together with the live `cautious_price_*` fields (≈L202-205). If the hypothesis flips to the opposite direction or to `none` while a position is open, trend.py manages the trade against the *wrong* direction (or stops managing it at the `none` gate).
2. **strategy.py** Section 3.1 (≈L575-587) force-closes any open position with a `market-close reason=direction-mismatch` the moment `hypothesis["direction"]` disagrees with the position direction — exempting only **manual** positions (`active.get("source")=="manual"`, ≈L579). An automatic position is flattened on the first mismatched 5m bar, denying cautious targets the chance to manage the exit.

This phase **freezes** the trade's management direction and cautious ladder into `position["active"]` at fill, re-keys trend.py Step-3 to that frozen snapshot, and **removes the automatic-position direction-mismatch market-close** so a hypothesis flip/none no longer force-closes a live trade. Cautious targets (now driven by the frozen ladder) become the sole exit. The flat-scan (trend.py Step 4) and all entry logic keep using the live hypothesis direction. The unfilled-stop-entry **cancel** on a direction change is **kept** (an unfilled entry under the old direction is still cancelled when direction flips).

## User Story

As a **strategy developer**
I want **an open position managed off a frozen direction + cautious ladder captured at fill, immutable for the life of the trade**
So that **the hypothesis is free to evolve, flip, or go `none` while the position rides — with cautious targets, not a force-close, deciding the exit — laying the foundation for Phase 3's evolving-hypothesis active-position model**.

## Problem / Solution Statement

**Problem.** Active-position management is coupled to live hypothesis state. A flip/none either mis-manages the trade (trend.py, wrong direction or `none`-gated) or kills it outright (strategy.py force-close). This blocks the Phase 3 goal of letting the hypothesis evolve during an active position without disturbing the trade.

**Solution.** At every fill site, **freeze** into `position["active"]`: `mgmt_direction` (the trade's own direction) and a copy of the hypothesis cautious ladder (`cautious_initial`, `cautious_initial_level`, `cautious_secondary`, `cautious_secondary_level`), plus a `backing_tier` placeholder (Phase-1 safe default; refined in Phase 3). trend.py Step-3 reads these frozen fields instead of `hypothesis["direction"]` and the live `cautious_price_*` fields. strategy.py drops the automatic-position direction-mismatch close (manual stays exempt as before — now redundant but harmless to keep explicit). The frozen ladder is **immutable**: later `recompute_cautious_for_fill` calls and hypothesis reforms must not overwrite it.

## Feature Metadata

**Feature Type**: Refactor + behavior change (decoupling) — Phase 1 of a 3-phase redesign.
**Complexity**: Medium.
**Primary Systems Affected**: `trend.py` (Step-3 re-key), `strategy.py` (remove mismatch close; freeze at fill paths), `live_orders.py` (freeze at fill paths), `smt_state.py` (`DEFAULT_POSITION` active-dict shape doc), `hypothesis.py` (`recompute_cautious_for_fill` must not clobber the frozen ladder).
**Dependencies**: None external. Pure pandas/python. No new packages. No new state files.
**Breaking Changes**: One intentional behavior change — automatic positions are no longer force-closed on a direction mismatch. The frozen fields are **additive** to `position["active"]`; positions written before this change (lacking the frozen fields) must fall back gracefully (trend.py derives `mgmt_direction` from the legacy `active["direction"]` and the ladder from the live hypothesis — a documented back-compat path). `position.json` on-disk schema is forward/backward compatible.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `trend.py` (L168-196) — `run_trend` entry + `direction = hypothesis.get("direction","none")` (L196) and the live cautious-ladder reads `cautious_initial_raw`/`cautious_secondary_raw`/`_lv1`/`_lv2`/`_cr1`/`_cr2` (L202-207). The frozen-snapshot resolution for Step-3 is layered here.
- `trend.py` (L232-272) — ATH maintenance + `_ath_secondary` (uses `_lv2`, `cautious_secondary_raw`). Must consume the **frozen** `_lv2`/secondary when a position is active (the `_ath_secondary` branch in Step-3 is part of the management path).
- `trend.py` (L277-278) — `if direction == "none": return None`. This early return precedes Step-3; it must NOT short-circuit active-position management once management keys off the frozen direction. Re-key so a `none` live hypothesis still manages an open position.
- `trend.py` (L280-299) — global-trend invalidation `trend-broken`. This runs **before** Step-3 and `save`s `hypothesis["direction"]="none"`; confirm it does NOT clear `position["active"]` (it doesn't) and that, with the mismatch-close removed, it no longer indirectly flattens the trade. The frozen path must keep managing after it fires.
- `trend.py` (L307-627) — **Step-3 active-position block** (`if active:`). Sub-states: 3a unarmed (L334-409), 3a2 `initial_surpassed` (L411-478), 3b `initial` (L481-539), 3b2 `secondary_surpassed` (L541-559), 3c `secondary`/`yes` (L561-627). Every `direction`, `cautious_initial`/`cautious_secondary`, `_lv1`/`_lv2`, `_cr1`/`_cr2`, `_ath_secondary`, `_surpassed`/`_close_beyond`/`_reversal`, and break-check use here must read the **frozen** snapshot.
- `trend.py` (L629-768) — **Step-4 flat scan** (no open position). Keeps using the **live** hypothesis `direction` and live mids/ATH — DO NOT re-key Step 4.
- `trend.py` (L67-80) — `_clear_position_and_hypothesis(clear_active=...)`: every exit path. Unchanged in signature; it already clears `active`, direction, and the manual lock on close.
- `strategy.py` (L569-587) — **Section 3.1 direction-mismatch close** (`if direction == "none" or direction != _pos_hyp_dir:` with the `source=="manual"` exemption at L579). Phase 1 removes the automatic-position close branch (manual stays a no-op return).
- `strategy.py` (L300-312) — **2.1 pending-stop-entry cancel** on direction change (`cancel-stop-entry`, reason `direction-none`/`direction-changed`). **KEEP** — verify and leave intact.
- `strategy.py` (L384-410) — **stop-entry fill** path: builds `position["active"]` (L385-395) then calls `recompute_cautious_for_fill` (L404-409). Freeze the frozen fields into `active` **here**, capturing the ladder **as it is at fill** (capture the snapshot BEFORE/independent of the recompute clobbering live hypothesis fields).
- `strategy.py` (L484-504) — **market-entry** path: builds `active` (L484-491) then `recompute_cautious_for_fill` (L497-502). Freeze the frozen fields **here** too.
- `strategy.py` (L663-692) — `reset_position_for_session` / `reset_position_for_new_hypothesis`: both clear `active` only at session/`active`-untouched-on-new-hyp respectively; confirm neither needs to touch the frozen fields (they live inside `active`, cleared when `active` is cleared).
- `hypothesis.py` (L154-181) — `recompute_cautious_for_fill`: re-anchors the **hypothesis** ladder at fill; no-ops under manual lock (L174). After freezing, this still runs (it keeps `hypothesis.json` current for Step-4/entries), but its output must **not** flow into `position["active"]`'s frozen fields. Confirm it only mutates `hypothesis`, never `position` (it does — leave it; the freeze just captures the ladder independently).
- `live_orders.py` (L226-266) — `_register_downgraded_fill`: STP→MKT immediate-downgrade fill; builds `pos["active"]` (L241-249) then `_recompute_cautious_at_fill` (L255). Freeze the frozen fields here.
- `live_orders.py` (L269-289) — `_recompute_cautious_at_fill`: re-anchors hypothesis.json only; same immutability note as `recompute_cautious_for_fill`.
- `live_orders.py` (L345-383) — `place_market_entry`: builds `pos["active"]` (L369-377). Freeze here (manual entries pass `source="manual"` and `entry==0.0` → fill anchored to current price; mgmt_direction still freezes from `direction`).
- `live_orders.py` (L424-438) — `stop_entry_filled`: only updates `active["stop"]` on an already-active position; it does NOT create `active`, so no freeze here (the freeze happened at strategy fill detection or downgrade).
- `smt_state.py` (L148-158) — `DEFAULT_POSITION`; `active` defaults to `{}`. Document the active-dict shape (the frozen fields) in a comment; no schema field added at the position root.
- `session_pipeline.py` (L1193-1212) — same-bar-stop-check after `stop-entry-filled`/`market-entry`: reads `active["stop"]`/`active["direction"]`; unaffected by the freeze (it runs before any flip). Confirm it still reads `active["direction"]` (the entry direction) — the freeze adds `mgmt_direction` alongside, not replacing `direction`.
- `session_pipeline.py` (L602-720) — hypothesis/active reconciliation + ghost-position handling: reads `active["direction"]`; unaffected.

### New Files to Create

- `tests/test_smt_decouple_active.py` — unit tests for the decoupled active-position management (trend.py off frozen snapshot; strategy.py no force-close; freeze at fill). New file so the new behavior is grouped and the regression guard is explicit.

### Tests to Update (existing)

- `tests/test_smt_strategy_v2.py` — `TestActivePosition`:
  - `test_in_position_direction_mismatch_emits_market_close` (L391-407) and `test_in_position_direction_none_emits_market_close` (L409-424): these assert the OLD force-close behavior for **automatic** positions. They must be **rewritten** to assert the position is now **preserved** (result `None`, `active` intact) — the close path is gone.
  - `test_in_position_manual_entry_exempt_from_direction_mismatch_close` (L426-441): still valid (manual preserved) — keep; optionally fold into the general "preserved" assertion since automatic is now preserved too.
- `tests/test_smt_trend.py` — `_active_position()` helper (L63-72) and any test that sets only `active["direction"]`/relies on the live hypothesis ladder for management: extend the helper to also write the frozen fields (`mgmt_direction` + frozen ladder) so existing trend management tests exercise the frozen path; keep their assertions (behavior must be byte-equivalent when frozen==live).

### Patterns to Follow

**Naming**: `snake_case`; frozen fields use the exact names in the Interface Contract below. Reuse `_make_signal`/`_market_close_signal` and existing signal kinds. No new module constants needed beyond what exists.
**Back-compat**: trend.py must tolerate an `active` dict lacking the frozen fields (positions filled before this change, or hand-written test positions): fall back `mgmt_direction = active.get("mgmt_direction") or _normalize(active.get("direction"))`, and frozen ladder falls back to the live `hypothesis` cautious fields. Use a single resolver helper so the fallback is in one place.
**Production silence**: no `print`/stdout in production paths (per CLAUDE.md). The existing `print` statements in strategy.py L374/L378 and live_orders.py are pre-existing — do NOT add new ones; do NOT remove pre-existing ones in this phase.
**Direction vocabulary**: `active["direction"]` may be `"long"`/`"short"` (live_orders) or `"up"`/`"down"` (strategy). Normalize to `"up"`/`"down"` for `mgmt_direction` (mirror strategy.py L574: `"up" if _pos_dir=="long" else ("down" if _pos_dir=="short" else _pos_dir)`).
**Immutability**: write the frozen fields exactly once (at fill). No code path other than the fill sites may write them; clearing `active` (on exit/reset) drops them implicitly.

---

## INTERFACE CONTRACT THIS PLAN PROVIDES (Phase 3 consumes — names are EXACT)

**Contract A — `position["active"]` gains FROZEN management fields, set once at fill:**

| field | type | meaning | Phase-1 source |
|---|---|---|---|
| `mgmt_direction` | `"up"` \| `"down"` | the trade's own management direction; trend.py Step-3 manages off this | the fill direction, normalized to up/down |
| `cautious_initial` | `float` \| `""` | frozen initial cautious target price | copied from `hypothesis["cautious_price_initial"]` at fill (the value present at fill time) |
| `cautious_initial_level` | `str` | frozen initial level tag (e.g. `day_high`, `synthetic_85pct`) | copied from `hypothesis["cautious_price_initial_level"]` |
| `cautious_secondary` | `float` \| `""` | frozen secondary cautious target price | copied from `hypothesis["cautious_price_secondary"]` |
| `cautious_secondary_level` | `str` | frozen secondary level tag (e.g. `week_high`) | copied from `hypothesis["cautious_price_secondary_level"]` |
| `backing_tier` | `"ATH"`\|`"week"`\|`"day"`\|`"fill"`\|`"session"` | tier of the dominant SMT backing the entry | **Phase-1 safe default** — see below; refined in Phase 3 |

**`backing_tier` Phase-1 derivation (documented, deliberately simple):** derive from the frozen `cautious_secondary_level` tag — `week_*` → `"week"`, `day_*` → `"day"`, else `"day"` as the safe default. (Phase 3 will set this from the dominant relevant SMT in the active SMT set; Phase 1 only needs a stable, non-crashing value that trend.py's `_ath_secondary` logic already approximates via the frozen `_lv2`.) The frozen `cautious_secondary_level` is the single source for the ATH-secondary determination, so deriving `backing_tier` from it keeps the two consistent.

**Capture timing (critical):** the ladder fields are captured from `hypothesis` **at the moment of fill, before that fill's `recompute_cautious_for_fill` mutates the live hypothesis ladder** — so the frozen copy reflects the ladder the trade was placed against. (In the strategy/downgrade paths the recompute re-anchors the *live* hypothesis to the fill; the frozen copy may use either the pre- or post-recompute value as long as it is **deterministic and documented** — Decision: capture the **post-recompute** ladder, i.e. the fill-anchored values, because that is exactly the ladder the trade should be managed against. Capture by reading `hypothesis["cautious_price_*"]` immediately AFTER the recompute call at each fill site. Document this in a comment at each freeze site.)

trend.py Step-3 reads `active["mgmt_direction"]` and the four frozen ladder fields; `hypothesis.json` (direction + cautious_price_*) is free to evolve and is read only by Step-4 and the entry path.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────────────┐
│ WAVE 1: Freeze at fill (parallel — distinct files/functions)         │
├──────────────────────────────────────────────────────────────────────┤
│ Task 1.1: strategy.py freeze     │ Task 1.2: live_orders.py freeze    │
│  (stop-entry + market-entry)     │  (_register_downgraded_fill +      │
│  + remove mismatch close         │   place_market_entry)              │
│  Agent: backend-core             │  Agent: backend-core               │
│                                  │                                    │
│ Task 1.3: smt_state.py doc the active-dict shape (comment only)       │
│  Agent: state                                                          │
└──────────────────────────────────────────────────────────────────────┘
                    ↓ (Contract A: frozen field names)
┌──────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Re-key management (sequential after Contract A is fixed)      │
├──────────────────────────────────────────────────────────────────────┤
│ Task 2.1: trend.py Step-3 reads frozen snapshot (resolver + re-key)   │
│  Deps: 1.1,1.2,1.3 (field names)                                       │
│  Agent: backend-core                                                   │
└──────────────────────────────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (parallel — distinct files)                            │
├──────────────────────────────────────────────────────────────────────┤
│ Task 3.1: tests/test_smt_decouple_active.py (new)  Deps 1.x,2.1       │
│ Task 3.2: update test_smt_strategy_v2.py mismatch tests  Deps 1.1     │
│ Task 3.3: update test_smt_trend.py helper + regression  Deps 2.1      │
│  Agent: qa (all three)                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Interface Contracts (between waves)

- **Wave 1 → Wave 2 (Contract A):** the freeze sites write `mgmt_direction`, `cautious_initial`, `cautious_initial_level`, `cautious_secondary`, `cautious_secondary_level`, `backing_tier` into `active` with the exact names/types above. Wave 2 (trend.py) reads exactly those names with the documented back-compat fallback.
- **Wave 1 → Wave 3:** the removed strategy.py mismatch close is the behavior Task 3.2 asserts (preserved position).
- **Wave 2 → Wave 3:** trend.py's frozen-snapshot reads are what Task 3.1/3.3 exercise.

### Synchronization Checkpoints

- **After Wave 1:** `uv run python -c "import strategy, live_orders, smt_state"` (imports) + `uv run python -m pytest tests/test_smt_strategy_v2.py tests/test_live_orders.py -q` (existing strategy/live_orders tests still load; the two mismatch tests will FAIL here by design until Task 3.2 — note them as expected-red).
- **After Wave 2:** `uv run python -c "import trend"` + `uv run python -m pytest tests/test_smt_trend.py -q` (existing trend behavior preserved once the helper writes frozen fields — but the helper update is Task 3.3; if run before 3.3, the back-compat fallback must keep them green — verify).
- **After Wave 3:** `uv run python -m pytest tests/test_smt_decouple_active.py tests/test_smt_trend.py tests/test_smt_strategy_v2.py tests/test_live_orders.py -q` then full suite.

### Parallelization Summary

7 tasks. Wave 1 = 3 parallel (distinct files). Wave 2 = 1 (depends on Contract A field names). Wave 3 = 3 parallel (distinct test files). 6 of 7 tasks run inside a parallel wave.

---

## STEP-BY-STEP TASKS

### WAVE 1: Freeze at fill + remove the force-close

#### Task 1.1: UPDATE strategy.py — freeze frozen fields at both fill paths; remove the automatic mismatch close
- **WAVE**: 1 · **AGENT_ROLE**: backend-core · **DEPENDS_ON**: [] · **BLOCKS**: [2.1, 3.1, 3.2]
- **PROVIDES**: frozen fields written at strategy fill paths (Contract A); removal of the automatic direction-mismatch close.
- **IMPLEMENT**:
  1. Add a small module-level helper `_freeze_active_mgmt(active: dict, direction: str, hypothesis: dict) -> None` that mutates `active` in place: sets `mgmt_direction` (normalized via the up/down rule), copies the four `cautious_*` fields from `hypothesis["cautious_price_*"]`/`*_level`, and sets `backing_tier` from the frozen `cautious_secondary_level` (`week_*`→`"week"`, `day_*`→`"day"`, else `"day"`). One source of truth for the freeze; reused by the two fill paths. (Place it near `_make_signal`.)
  2. **Stop-entry fill** (L385-410): after the existing `recompute_cautious_for_fill` (L404-409) re-anchors `hypothesis`, call `_freeze_active_mgmt(position["active"], direction, hypothesis)` and re-`save_position(position)` (the active dict is the one written at L385). Add a comment: "freeze captures the fill-anchored (post-recompute) ladder — immutable for the life of the trade".
  3. **Market-entry** (L484-504): same — after the recompute at L497-502, call `_freeze_active_mgmt(position["active"], direction, hypothesis)` and `save_position`.
  4. **Remove the automatic mismatch close** (L575-587): delete the `position["active"] = {} … return _make_signal("market-close", …, reason="direction-mismatch", …)` body for automatic positions. Keep the function returning **`None`** when `direction == "none" or direction != _pos_hyp_dir` (both manual and automatic now fall through to no-op — trend.py + cautious targets manage the exit). Simplest correct form: replace the whole `if direction == "none" or direction != _pos_hyp_dir:` block (L575-587) with an early `return None` guard ONLY for the mismatch case — i.e. a mismatched hypothesis means strategy.py Section 3 does nothing (no stop-cross re-check either, since management is trend.py's job under a mismatch). **Decision (locked):** under a mismatch, `run_strategy` returns `None` immediately (do not run 3.2/3.3 against a mismatched live direction — the frozen-managed trade is trend.py's responsibility; strategy.py's stop-cross at 3.2 uses `active["stop"]`/`active["direction"]` which are still valid, but to avoid double-management keep the single early `return None`). Document why with a comment referencing this plan.
  5. Confirm **2.1 pending-stop-entry cancel** (L300-312) is untouched.
- **PATTERN**: normalize via strategy.py L574; freeze helper mirrors the dict-mutation style of the fill blocks.
- **VALIDATE**: `uv run python -c "import strategy"`; `uv run python -m pytest tests/test_smt_strategy_v2.py -q` (mismatch tests expected-red until 3.2).

#### Task 1.2: UPDATE live_orders.py — freeze frozen fields at downgrade + market-entry fills
- **WAVE**: 1 · **AGENT_ROLE**: backend-core · **DEPENDS_ON**: [] · **BLOCKS**: [2.1, 3.1]
- **PROVIDES**: frozen fields written at the two live_orders fill paths (Contract A).
- **IMPLEMENT**:
  1. `_register_downgraded_fill` (L241-255): after `_recompute_cautious_at_fill(fill)` (L255) re-anchors `hypothesis.json`, reload the hypothesis and freeze the frozen fields into `pos["active"]` (the dict built at L241), then `_save_pos(pos)`. Reuse the SAME freeze logic — to avoid duplicating the helper across modules, define the freeze helper in `strategy.py` (Task 1.1) and import it (`from strategy import _freeze_active_mgmt`), OR if a cross-import is undesirable, place the helper in `smt_state.py` (already imported by both) and have Task 1.1 import it from there. **Decision (locked):** put the helper in `smt_state.py` as `freeze_active_mgmt(active, direction, hypothesis)` (public, no underscore) so both `strategy.py` and `live_orders.py` import it from the shared state module with no circular import. Update Task 1.1 to import it from `smt_state`.
  2. `place_market_entry` (L369-381): after building `pos["active"]` (L369-377), load the current hypothesis (`smt_state.load_hypothesis()`) and call `smt_state.freeze_active_mgmt(pos["active"], direction, hyp)` before `_save_pos(pos)`. (Manual entries pass `source="manual"`; `direction` is still up/down — freeze proceeds. Note this path does NOT recompute cautious for the manual case; the frozen ladder will copy whatever the live hypothesis currently holds — acceptable for a manual trade.)
  3. `stop_entry_filled` (L424-438): no `active` creation → no freeze (documented).
- **PATTERN**: mirror `_register_downgraded_fill`'s `_save_pos` flow.
- **VALIDATE**: `uv run python -c "import live_orders"`; `uv run python -m pytest tests/test_live_orders.py -q`.

#### Task 1.3: UPDATE smt_state.py — `freeze_active_mgmt` helper + document the active-dict shape
- **WAVE**: 1 · **AGENT_ROLE**: state · **DEPENDS_ON**: [] · **BLOCKS**: [1.1, 1.2, 2.1]
- **PROVIDES**: shared `freeze_active_mgmt(active, direction, hypothesis)`; documented active-dict frozen-field shape on `DEFAULT_POSITION`.
- **IMPLEMENT**:
  1. Add `freeze_active_mgmt(active: dict, direction: str, hypothesis: dict) -> None` to `smt_state.py` (mutates `active` in place; pure; no I/O). Normalize direction to up/down; copy the four `cautious_*` fields; set `backing_tier`. Total/None-tolerant: missing hypothesis cautious fields → store `""`.
  2. On `DEFAULT_POSITION` (L148-158), add a comment block documenting the frozen `active` sub-dict shape (the six Contract-A fields + the pre-existing `time`/`fill_price`/`direction`/`stop`/`contracts`/`cautious`/`source`/`cautious_break_price`). `active` default stays `{}`.
- **PATTERN**: pure-function/None-tolerance style of existing `smt_state` helpers.
- **VALIDATE**: `uv run python -c "import smt_state"`; `uv run python -m pytest tests/test_smt_state.py -q`.

**Wave 1 Checkpoint**: `uv run python -c "import smt_state, strategy, live_orders"` && `uv run python -m pytest tests/test_smt_state.py tests/test_live_orders.py -q` (test_smt_strategy_v2 mismatch tests expected-red until Wave 3).

---

### WAVE 2: Re-key trend.py Step-3 to the frozen snapshot

#### Task 2.1: UPDATE trend.py — Step-3 reads the frozen snapshot; Step-4 unchanged
- **WAVE**: 2 · **AGENT_ROLE**: backend-core · **DEPENDS_ON**: [1.1, 1.2, 1.3] · **BLOCKS**: [3.1, 3.3]
- **USES_FROM_WAVE_1**: Contract A frozen field names.
- **IMPLEMENT**:
  1. Add a resolver near the top of `run_trend` (after `active = position.get("active", {})`, L307) that, **when `active` is non-empty**, derives the management snapshot from the frozen fields with back-compat fallback:
     - `mgmt_direction = active.get("mgmt_direction") or _normalize(active.get("direction",""))` (normalize long/short→up/down; `""`→`"none"`).
     - frozen ladder raws: `f_initial_raw = active.get("cautious_initial", hypothesis.get("cautious_price_initial",""))`, `f_secondary_raw = active.get("cautious_secondary", hypothesis.get("cautious_price_secondary",""))`, `f_lv1 = active.get("cautious_initial_level") or hypothesis.get("cautious_price_initial_level","") or ""`, `f_lv2 = active.get("cautious_secondary_level") or hypothesis.get("cautious_price_secondary_level","") or ""`.
     - derive `_cr1`/`_cr2` from `f_lv1`/`f_lv2` (mirror L206-207).
  2. **Re-key Step-3 (L307-627) to use the frozen snapshot**, NOT the live `direction`/`cautious_*_raw`/`_lv1`/`_lv2`/`_cr1`/`_cr2`:
     - Inside `if active:` (L312), use a local `direction = mgmt_direction` so the existing closures `_surpassed`/`_close_beyond`/`_reversal` (L324-331) and every break-check (L416/L524/L567/L612), arm/trail, and the upgrade transitions key off the frozen direction.
     - Use `cautious_initial = float(f_initial_raw) if f_initial_raw != "" else None` and likewise `cautious_secondary` (replacing L321-322).
     - Use the frozen `f_lv1`/`f_lv2` in the `new-stop-exit`/`move-stop-exit` signals' `level_name` (L372/L392/etc.) and `f_cr1`/`f_cr2` in the `close_reason` (L424/L472/L532/L575/L620).
     - `_ath_secondary` (L267-272): recompute it for the active path off the **frozen** `f_lv2` and `f_secondary_raw` (the live one at L267 is fine for Step-4; for Step-3 build an active-scoped `_ath_secondary_frozen`). Keep `_session_ath` from global. Apply the frozen variant inside Step-3's secondary/ath branches (L364/L366/L431/L433/L488/L490/L549/L551/L563/L576).
  3. **The `direction == "none"` early return (L277-278) must not block active-position management.** Move/guard it so that when `active` is non-empty it is **skipped** (management proceeds off `mgmt_direction`, which is the trade's own direction and is never `"none"` for a real fill). Concretely: gate the early return as `if direction == "none" and not active: return None`. Below it, the global-trend `trend-broken` block (L280-299) keys off the **live** `direction` — leave it as-is (it sets hypothesis direction none + clears conf bars/stop_entry but NOT `active`); with the mismatch-close removed it no longer flattens the trade, and Step-3 still runs afterward off the frozen snapshot. Verify ordering: after the `trend-broken` early `return` it returns a signal; ensure that does not pre-empt managing an open position incorrectly — **Decision (locked):** when `active` is non-empty, **skip** the global-trend `trend-broken` reset too (it is a hypothesis-level reset meant for the flat state; an open position is managed by the frozen snapshot + cautious exit). Guard L284 with `and not active`. Document this.
  4. **Step-4 (L629-768) unchanged** — it runs only when `active` is empty (it's after the `if active: … return None` block) and must keep using the live hypothesis `direction`, live mids, live ATH straddles. Do NOT touch.
  5. Add `_normalize` helper (or inline) for long/short→up/down.
  6. Mid-cross invalidations inside Step-3 (3a, L337-354): these use `daily_mid_price`/`weekly_mid_price` + `_mid_cross_guard`/`_weekly_mid_cross_guard` which are derived from the **live** hypothesis (L221-230). **Decision (locked):** for a frozen-managed position, the mid-cross guards should be derived from `mgmt_direction`, not the live hypothesis `direction`, so they remain meaningful after a flip. Recompute `_mid_cross_guard`/`_weekly_mid_cross_guard` for the active path using `mgmt_direction` and the frozen entry-side context (use `hypothesis.get("daily_mid")`/`weekly_mid` only as the side reference — if absent, default to applying the cross check, matching today when frozen==live). Keep behavior identical when frozen==live.
- **PATTERN**: existing Step-3 closures and signal builders; back-compat fallback in ONE resolver.
- **VALIDATE**: `uv run python -c "import trend"`; `uv run python -m pytest tests/test_smt_trend.py -q` (must stay green via back-compat even before the helper update in 3.3).

**Wave 2 Checkpoint**: `uv run python -c "import trend"` && `uv run python -m pytest tests/test_smt_trend.py -q`.

---

## DETAILED LOGIC REFERENCE

This section pins the two subtle mechanisms so the executor does not re-derive them. Pseudocode is illustrative — match the surrounding style when implementing.

### A. `freeze_active_mgmt` (smt_state.py — single source of truth)

```python
def freeze_active_mgmt(active: dict, direction: str, hypothesis: dict) -> None:
    """Freeze the trade's management direction + cautious ladder into `active`.
    Called once at every fill site, AFTER that fill's recompute_cautious_for_fill
    has re-anchored the live hypothesis ladder — so the frozen copy is the
    fill-anchored ladder. Mutates `active` in place. Pure; no I/O; None-tolerant.
    Immutable thereafter: no other code path writes these fields.
    """
    d = "up" if direction == "long" else ("down" if direction == "short" else direction)
    active["mgmt_direction"]          = d  # "up" | "down"
    ci  = hypothesis.get("cautious_price_initial", "")
    ci_l = hypothesis.get("cautious_price_initial_level", "") or ""
    cs  = hypothesis.get("cautious_price_secondary", "")
    cs_l = hypothesis.get("cautious_price_secondary_level", "") or ""
    active["cautious_initial"]          = ci
    active["cautious_initial_level"]    = ci_l
    active["cautious_secondary"]        = cs
    active["cautious_secondary_level"]  = cs_l
    # Phase-1 safe default; Phase 3 refines from the dominant SMT.
    if cs_l.startswith("week"):
        active["backing_tier"] = "week"
    elif cs_l.startswith("day"):
        active["backing_tier"] = "day"
    else:
        active["backing_tier"] = "day"
```

Freeze-site call shape (each of the four fill paths), e.g. strategy stop-entry fill:
```python
# ... position["active"] = {...} built; save_position(position) ...
_hyp_mod.recompute_cautious_for_fill(hypothesis, fill_price, liqs, ath, shrinks)
smt_state.save_hypothesis(hypothesis)
smt_state.freeze_active_mgmt(position["active"], direction, hypothesis)  # POST-recompute
smt_state.save_position(position)
```
For `live_orders.place_market_entry` (no recompute on the manual path) the freeze copies the live hypothesis ladder as-is — load it first: `hyp = smt_state.load_hypothesis(); smt_state.freeze_active_mgmt(pos["active"], direction, hyp)`.

### B. trend.py Step-3 resolver + re-key map

Resolver, placed right after `active = position.get("active", {})` (≈L307), only meaningful when `active` is non-empty:
```python
def _norm(d):  # long/short → up/down; "" → "none"
    return "up" if d == "long" else ("down" if d == "short" else (d or "none"))

if active:
    mgmt_direction = active.get("mgmt_direction") or _norm(active.get("direction", ""))
    f_initial_raw   = active.get("cautious_initial",   hypothesis.get("cautious_price_initial", ""))
    f_secondary_raw = active.get("cautious_secondary", hypothesis.get("cautious_price_secondary", ""))
    f_lv1 = active.get("cautious_initial_level")   or hypothesis.get("cautious_price_initial_level", "")   or ""
    f_lv2 = active.get("cautious_secondary_level") or hypothesis.get("cautious_price_secondary_level", "") or ""
    f_cr1 = f"1st-cautious ({f_lv1})" if f_lv1 else "1st-cautious"
    f_cr2 = f"2nd-cautious ({f_lv2})" if f_lv2 else "2nd-cautious"
    # active-scoped ATH-secondary (mirror L267-272 but off the frozen lv2/raw):
    f_ath_secondary = (f_lv2 in {"day_high", "week_high"} and _session_ath > 0
                       and f_secondary_raw != "" and float(f_secondary_raw) >= _session_ath)
```

Exact substitution map inside Step-3 (`if active:`, L312-627) — replace the live symbol with the frozen one:

| Live symbol (Step-3) | Frozen replacement |
|---|---|
| `direction` (L318+, closures L324-331, breaks, trails, upgrades) | `mgmt_direction` (assign `direction = mgmt_direction` at the top of `if active:`) |
| `cautious_initial_raw` → `cautious_initial` (L321) | `f_initial_raw` → `cautious_initial` |
| `cautious_secondary_raw` → `cautious_secondary` (L322) | `f_secondary_raw` → `cautious_secondary` |
| `_lv1` in `level_name` (L392, L406, L475, L537) | `f_lv1` |
| `_lv2` in `level_name` (L372, L438, L497, L557, L625) | `f_lv2` |
| `_cr1` in `close_reason` (L424, L472, L532) | `f_cr1` |
| `_cr2` in `close_reason` (L575, L591, L620) | `f_cr2` |
| `_ath_secondary` (L364, L366, L431, L433, L488, L490, L549, L551, L563, L576) | `f_ath_secondary` |

Gating edits (active-skip) outside Step-3:
- L277-278 → `if direction == "none" and not active: return None`.
- L284 (global-trend reset) → add `and not active` to the `if` so it is skipped while a position is open.
- L337 / L347 mid-cross guards inside 3a → recompute `_mid_cross_guard`/`_weekly_mid_cross_guard` from `mgmt_direction` (see Task 2.1 step 6).

Behavior invariant: when `mgmt_direction == direction` (frozen == live) and the frozen ladder equals the live ladder, every replacement is value-identical → byte-equivalent management (guards AC8).

### C. Baseline capture for the byte-equivalence regression

Before re-keying trend.py (Task 2.1), capture the signal sequence the current code emits for the representative non-flipping management run (unarmed → initial arm → secondary arm → break) and encode it as the expected list in `test_normal_trade_management_byte_equivalent`. The simplest capture: write the scenario test FIRST against the pre-change `trend.py`, record the emitted dicts, then assert equality after the change (and again with the live hypothesis flipped in `test_flip_does_not_change_normal_management`). This makes the regression a true before/after guard rather than a re-derivation.

---

### WAVE 3: Tests

#### Task 3.1: CREATE tests/test_smt_decouple_active.py — decoupling behavior
- **WAVE**: 3 · **AGENT_ROLE**: qa · **DEPENDS_ON**: [1.1, 1.2, 1.3, 2.1] · **BLOCKS**: []
- **IMPLEMENT**: all "Decoupling unit tests" below. Mirror the path-redirect fixture from `tests/test_smt_trend.py` (L85-90) and the position/hypothesis writers from `tests/test_smt_strategy_v2.py`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_decouple_active.py -q`.

#### Task 3.2: UPDATE tests/test_smt_strategy_v2.py — mismatch tests assert preservation
- **WAVE**: 3 · **AGENT_ROLE**: qa · **DEPENDS_ON**: [1.1] · **BLOCKS**: []
- **IMPLEMENT**: rewrite `test_in_position_direction_mismatch_emits_market_close` and `test_in_position_direction_none_emits_market_close` (L391-424) to assert the automatic position is **preserved** (`result is None`, `pos["active"] != {}`, `active` unchanged). Keep `test_in_position_manual_entry_exempt_from_direction_mismatch_close` (still passes). Add a freeze-at-fill assertion test (`test_fill_freezes_mgmt_fields`) verifying the six Contract-A fields appear in `active` after a stop-entry fill and a market-entry fill, with values copied from the (post-recompute) hypothesis ladder.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_strategy_v2.py -q`.

#### Task 3.3: UPDATE tests/test_smt_trend.py — frozen helper + byte-equivalence regression
- **WAVE**: 3 · **AGENT_ROLE**: qa · **DEPENDS_ON**: [2.1] · **BLOCKS**: []
- **IMPLEMENT**: extend `_active_position()` (L63-72) to also set `mgmt_direction` + the four frozen ladder fields + `backing_tier` (frozen==live ladder), so existing management tests exercise the frozen path. Add `test_normal_trade_management_byte_equivalent` (see below). Add the flip/none management tests (see below). Keep all pre-existing trend assertions green.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_trend.py -q`.

**Final Checkpoint**: `uv run python -m pytest tests/test_smt_decouple_active.py tests/test_smt_trend.py tests/test_smt_strategy_v2.py tests/test_live_orders.py tests/test_smt_state.py -q` then the full suite (`uv run python -m pytest tests/ -q`).

---

## TESTING STRATEGY

All tests are pure pandas/python (no UI, no network, no IB, no orchestrator) → **100% automated via pytest**. State files redirected to `tmp_path` via the existing `redirect_paths` fixture pattern (`tests/test_smt_trend.py` L85-90).

| What | Type | Automation | Tool | File |
|---|---|---|---|---|
| Freeze at fill (strategy paths) | Unit | ✅ | pytest | `tests/test_smt_strategy_v2.py`, `tests/test_smt_decouple_active.py` |
| Freeze at fill (live_orders paths) | Unit | ✅ | pytest | `tests/test_smt_decouple_active.py` |
| No force-close on mismatch (automatic) | Unit | ✅ | pytest | `tests/test_smt_strategy_v2.py` |
| Manual position still untouched | Unit | ✅ | pytest | `tests/test_smt_strategy_v2.py` |
| Pending-stop-entry cancel still fires | Unit | ✅ | pytest | `tests/test_smt_decouple_active.py` |
| trend.py manages off frozen mgmt_direction (flip/none) | Unit | ✅ | pytest | `tests/test_smt_decouple_active.py`, `tests/test_smt_trend.py` |
| Frozen ladder not overwritten by later recompute | Unit | ✅ | pytest | `tests/test_smt_decouple_active.py` |
| Normal-trade byte-equivalence regression | Unit | ✅ | pytest | `tests/test_smt_trend.py` |
| `freeze_active_mgmt` helper + default shape | Unit | ✅ | pytest | `tests/test_smt_state.py` (or `test_smt_decouple_active.py`) |

### Decoupling unit tests (`tests/test_smt_decouple_active.py`)
**Status**: ✅ Automated · **Tool**: pytest · **Run**: `uv run python -m pytest tests/test_smt_decouple_active.py -q`

Freeze at fill:
- `test_stop_entry_fill_freezes_all_six_fields` — drive a stop-entry fill via `run_strategy`; assert `active` has `mgmt_direction` (up/down), the four frozen ladder fields (== fill-anchored hypothesis ladder), and `backing_tier` derived from `cautious_secondary_level`.
- `test_market_entry_fill_freezes_all_six_fields` — same via the market-entry path.
- `test_downgrade_fill_freezes_fields` — call `live_orders._register_downgraded_fill(...)` (paths redirected); assert frozen fields present.
- `test_place_market_entry_freezes_fields` — `live_orders.place_market_entry(...)` with a stubbed executor; assert frozen fields present, `source` honored.
- `test_backing_tier_derivation` — `cautious_secondary_level="week_high"`→`"week"`; `"day_high"`→`"day"`; `""`→`"day"`.

trend.py manages off the frozen snapshot:
- `test_trend_manages_when_hypothesis_flipped_opposite` — frozen `mgmt_direction="up"`, set `hypothesis["direction"]="down"`; an arm/break scenario that should fire for an UP trade still fires correctly (uses frozen up-side `_surpassed`/break math), NOT the down-side.
- `test_trend_manages_when_hypothesis_none` — frozen `mgmt_direction="down"`, `hypothesis["direction"]="none"`; the `direction=="none"` early return is skipped and the DOWN position is managed (e.g. secondary break fires on the correct side).
- `test_trend_break_check_correct_side_after_flip` — initial-cautious break check uses the frozen direction's comparator (long: `bar_low < break`; short: `bar_high > break`) even when the live hypothesis is flipped.
- `test_global_trend_reset_skipped_when_active` — confidence=high + opposing live direction would fire `trend-broken` when flat; with an active frozen position it is skipped and the position keeps being managed.

Immutability:
- `test_frozen_ladder_not_overwritten_by_recompute` — freeze a ladder at fill; then call `hypothesis.recompute_cautious_for_fill(hyp, other_price, ...)` (mutates only `hypothesis`); reload position and assert `active`'s frozen ladder is unchanged.
- `test_frozen_ladder_survives_hypothesis_direction_change` — flip `hypothesis["direction"]` and rewrite live `cautious_price_*`; assert `active` frozen fields unchanged and trend.py still uses them.

Cancel preserved:
- `test_pending_stop_entry_cancel_on_direction_change` — no active position, a resting `stop_entry` under `stop_direction="up"`, hypothesis flips to `down` → `run_strategy` returns `cancel-stop-entry` (reason `direction-changed`) and clears `stop_entry` (verifies strategy.py L300-312 intact).
- `test_pending_stop_entry_cancel_on_direction_none` — same with `direction="none"` → reason `direction-none`.

Back-compat:
- `test_legacy_active_without_frozen_fields_managed_via_fallback` — an `active` dict lacking the frozen fields (only legacy `direction`) is managed using the fallback (mgmt_direction from `direction`, ladder from live hypothesis) — no crash, behavior == today.

### Regression / flip tests in `tests/test_smt_trend.py`
**Status**: ✅ Automated · **Tool**: pytest · **Run**: `uv run python -m pytest tests/test_smt_trend.py -q`

- `test_normal_trade_management_byte_equivalent` — run a representative non-flipping management sequence (unarmed → initial arm → secondary arm → break) with frozen ladder == live ladder and matching direction; assert the emitted signals are identical to a captured baseline (the same sequence on the pre-change behavior, encoded as expected dicts). Guards "no behavior change in the common case".
- `test_flip_does_not_change_normal_management` — same sequence but with the live hypothesis flipped to the opposite direction mid-trade; assert the signals are **identical** to the non-flipped baseline (frozen snapshot insulates management).

### Mismatch tests in `tests/test_smt_strategy_v2.py`
**Status**: ✅ Automated · **Tool**: pytest · **Run**: `uv run python -m pytest tests/test_smt_strategy_v2.py -q`
- `test_in_position_direction_mismatch_preserves_automatic_position` (rewrite) — automatic `active`, hypothesis flipped → `result is None`, `active` preserved.
- `test_in_position_direction_none_preserves_automatic_position` (rewrite) — automatic `active`, hypothesis `none` → `result is None`, `active` preserved.
- `test_in_position_manual_entry_preserved` (keep) — manual `active`, mismatch → preserved.
- `test_fill_freezes_mgmt_fields` (new) — stop + market fills write the six frozen fields.

### `freeze_active_mgmt` helper in `tests/test_smt_state.py`
**Status**: ✅ Automated · **Tool**: pytest · **Run**: `uv run python -m pytest tests/test_smt_state.py -q`
- `test_freeze_active_mgmt_copies_ladder_and_normalizes_direction` — long→up, short→down; copies the four fields; sets backing_tier.
- `test_freeze_active_mgmt_none_tolerant` — missing hypothesis cautious fields → frozen fields default to `""`; no raise.

### Coverage pass (every changed function/branch → a named test)

| Changed surface | Branch | Test | ✅/⚠️ |
|---|---|---|---|
| `smt_state.freeze_active_mgmt` | up/down normalize | `test_freeze_active_mgmt_copies_ladder_and_normalizes_direction` | ✅ |
| `smt_state.freeze_active_mgmt` | missing fields | `test_freeze_active_mgmt_none_tolerant` | ✅ |
| `smt_state.freeze_active_mgmt` | backing_tier derivation | `test_backing_tier_derivation` | ✅ |
| strategy stop-entry fill freeze | freeze written | `test_stop_entry_fill_freezes_all_six_fields` | ✅ |
| strategy market-entry fill freeze | freeze written | `test_market_entry_fill_freezes_all_six_fields` | ✅ |
| strategy 3.1 mismatch removal | automatic mismatch | `test_in_position_direction_mismatch_preserves_automatic_position` | ✅ |
| strategy 3.1 mismatch removal | live direction none | `test_in_position_direction_none_preserves_automatic_position` | ✅ |
| strategy 3.1 (manual unchanged) | manual mismatch | `test_in_position_manual_entry_preserved` | ✅ |
| strategy 2.1 cancel (kept) | direction-changed | `test_pending_stop_entry_cancel_on_direction_change` | ✅ |
| strategy 2.1 cancel (kept) | direction-none | `test_pending_stop_entry_cancel_on_direction_none` | ✅ |
| live_orders `_register_downgraded_fill` freeze | freeze written | `test_downgrade_fill_freezes_fields` | ✅ |
| live_orders `place_market_entry` freeze | freeze written | `test_place_market_entry_freezes_fields` | ✅ |
| live_orders `stop_entry_filled` (no freeze) | active updated only | covered by existing `test_live_orders.py` | ✅ |
| trend resolver back-compat fallback | legacy active | `test_legacy_active_without_frozen_fields_managed_via_fallback` | ✅ |
| trend Step-3 frozen direction | flip opposite | `test_trend_manages_when_hypothesis_flipped_opposite` | ✅ |
| trend Step-3 frozen direction | live none | `test_trend_manages_when_hypothesis_none` | ✅ |
| trend Step-3 break-check side | flipped | `test_trend_break_check_correct_side_after_flip` | ✅ |
| trend `none` early-return guard | active skip | `test_trend_manages_when_hypothesis_none` | ✅ |
| trend global-trend reset guard | active skip | `test_global_trend_reset_skipped_when_active` | ✅ |
| trend frozen `_ath_secondary` | frozen lv2 path | `test_trend_manages_when_hypothesis_flipped_opposite` (ATH-secondary variant) | ⚠️ add explicit `test_ath_secondary_uses_frozen_lv2` if the flip test doesn't cover the `_ath_secondary` branch |
| trend frozen ladder immutability | recompute after freeze | `test_frozen_ladder_not_overwritten_by_recompute` | ✅ |
| trend Step-4 (unchanged) | flat scan | existing `test_smt_trend.py` flat-scan tests | ✅ |
| normal-case regression | no drift | `test_normal_trade_management_byte_equivalent`, `test_flip_does_not_change_normal_management` | ✅ |

**Gap closed:** add `test_ath_secondary_uses_frozen_lv2` (frozen `cautious_secondary_level="week_high"` at ATH while live hypothesis is flipped → the ATH-secondary break-even path uses the frozen lv2) to cover the `_ath_secondary` Step-3 branch explicitly.

---

## VALIDATION COMMANDS

### Side-effecting test policy (full-suite runs)

All new code is pure (no broker/IB/network, no process management). New tests carry NO side effects and are NOT marked `integration`.

- **Run side-effecting tests during validation?** ☑ No (default).
- **Full-suite command:** `uv run python -m pytest tests/ -q` — the repo `pyproject.toml` `addopts` already applies `-m 'not integration'`, which excludes live IB/network and orchestrator/process-lifecycle tests. Do NOT opt them in. **A live trading process may be running** — do not run integration/IB/orchestrator/live tests.
- **If Yes — exact paths/markers + safe command:** N/A — no opt-in needed. If ever required, first confirm no live orchestrator/IB feed is running.

### Level 1: Syntax & Import
```bash
uv run python -c "import smt_state, strategy, live_orders, trend, hypothesis, session_pipeline"
```

### Level 2: Targeted unit tests
```bash
uv run python -m pytest tests/test_smt_decouple_active.py tests/test_smt_state.py -q
uv run python -m pytest tests/test_smt_strategy_v2.py tests/test_smt_trend.py tests/test_live_orders.py -q
```

### Level 3: Full suite (integration excluded by addopts)
```bash
uv run python -m pytest tests/ -q
```

---

## ACCEPTANCE CRITERIA

1. **AC1 — Freeze at fill (all four paths):** every fill that creates `position["active"]` (strategy stop-entry fill, strategy market-entry, `live_orders._register_downgraded_fill`, `live_orders.place_market_entry`) writes the six Contract-A fields — `mgmt_direction` (up/down), `cautious_initial`, `cautious_initial_level`, `cautious_secondary`, `cautious_secondary_level`, `backing_tier` — with the ladder copied from the fill-anchored (post-recompute) hypothesis ladder.
2. **AC2 — trend.py manages off the frozen snapshot:** Step-3 active-position management (arming, trailing, all break checks, ATH-secondary) keys off `active["mgmt_direction"]` and the frozen ladder, never the live `hypothesis["direction"]` or live `cautious_price_*`. The `direction=="none"` early return and the global-trend `trend-broken` reset are skipped while a position is active.
3. **AC3 — No force-close on mismatch (automatic):** with an automatic open position and a flipped or `none` live hypothesis, `run_strategy` returns `None` and the position is preserved (no `market-close reason=direction-mismatch`). Cautious targets are the sole exit.
4. **AC4 — Manual position still untouched:** a `source="manual"` position is preserved on a direction mismatch exactly as before.
5. **AC5 — Pending-stop-entry cancel preserved:** an unfilled stop entry under the old direction is still cancelled (`cancel-stop-entry`, reason `direction-changed`/`direction-none`) when the live direction flips/none while flat.
6. **AC6 — Frozen ladder immutable:** a later `recompute_cautious_for_fill` (or any hypothesis ladder/direction change) does not alter the frozen fields in `position["active"]`.
7. **AC7 — Correct-side management after a flip:** with the live hypothesis flipped to the opposite direction, trend.py's break/arm comparators fire on the frozen direction's side (long vs short), not the flipped side.
8. **AC8 — Normal-case byte-equivalence:** for a non-flipping trade (frozen == live), the emitted management signals are identical to pre-change behavior; flipping the live hypothesis mid-trade produces identical signals to the non-flipped baseline.
9. **AC9 — Back-compat:** an `active` dict lacking the frozen fields is managed via the documented fallback (mgmt_direction from legacy `direction`, ladder from live hypothesis) without crashing.
10. **AC10 — Production silence + scope:** no new `print`/stdout in production paths; no changes outside `trend.py`, `strategy.py`, `live_orders.py`, `smt_state.py`, `hypothesis.py` (doc only), and the named test files; Step-4 flat scan and all entry logic unchanged.
11. **AC11 — Suite green:** `uv run python -m pytest tests/ -q` passes (integration excluded by `addopts`); every changed function/branch has a named test per the coverage table.

---

## COMPLETION CHECKLIST

- [ ] `smt_state.freeze_active_mgmt` added; `DEFAULT_POSITION` active-dict shape documented.
- [ ] strategy.py: freeze at stop-entry fill + market-entry; automatic mismatch close removed (returns `None`); 2.1 cancel intact.
- [ ] live_orders.py: freeze at `_register_downgraded_fill` + `place_market_entry`; `stop_entry_filled` documented as no-freeze.
- [ ] trend.py: Step-3 resolver + re-key to frozen snapshot; `none`/global-trend guards on `active`; Step-4 untouched; back-compat fallback.
- [ ] hypothesis.py: confirmed `recompute_cautious_for_fill` mutates only `hypothesis` (no change needed beyond a clarifying comment if helpful).
- [ ] New tests: `tests/test_smt_decouple_active.py` (all listed cases incl. `test_ath_secondary_uses_frozen_lv2`).
- [ ] Updated tests: `test_smt_strategy_v2.py` mismatch tests → preserved; `test_smt_trend.py` helper + byte-equivalence + flip tests; `test_smt_state.py` helper tests.
- [ ] All debug logs added during execution deleted.
- [ ] `uv run python -m pytest tests/ -q` green.
- [ ] Changes left UNSTAGED — no `git add`/`commit`.

---

## NOTES

- **Phase boundary.** This phase deliberately does NOT introduce the active-SMT set, dominant-SMT direction, event-driven reformation, or a real `backing_tier` from SMTs — those are Phases 2/3. Phase 1 ships only the decoupling + the frozen-snapshot contract, with a safe `backing_tier` default so Phase 3 can refine it without a schema change.
- **Why skip the global-trend reset while active (Decision):** that reset (trend.py L280-299) and the mid-cross resets exist to invalidate a *flat* hypothesis. With force-close removed, letting them keep firing against an open position would mutate hypothesis state under the trade's feet without any management consequence; skipping them while `active` keeps the frozen-managed trade clean and the live hypothesis free to reform on the next flat bar.
- **Capture timing (Decision):** the frozen ladder is the **post-recompute** (fill-anchored) ladder at each fill site — the exact ladder the trade is managed against. Documented at each freeze site.
- **Helper location (Decision):** `freeze_active_mgmt` lives in `smt_state.py` (imported by both `strategy.py` and `live_orders.py`) to avoid a `strategy`↔`live_orders` cross-import.
- **Manual market entry (`place_market_entry`)** does not recompute cautious; its frozen ladder copies whatever the live hypothesis holds at entry — acceptable for a discretionary trade and consistent with today's manual behavior.
- **Regression discipline:** the byte-equivalence test (`test_normal_trade_management_byte_equivalent`) is the gate that proves "no behavior change in the common case"; capture its baseline from the current behavior before re-keying trend.py.
