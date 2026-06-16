# Code Review — GIL-25 Phase 1.2: cross-session carry of non-invalidated SMTs

Branch: `autoresearch/gil25-phase1.2-pending-smts`
Scope: UNSTAGED changes to `smt_detect.py`, `smt_state.py`, `session_pipeline.py`, `tests/test_smt_state.py`, plus new `tests/test_pending_smts.py`.

## Stats
- Files Modified: 4 (`session_pipeline.py`, `smt_detect.py`, `smt_state.py`, `tests/test_smt_state.py`)
- Files Added: 1 (`tests/test_pending_smts.py`) + 1 plan doc
- New lines: ~461
- Deleted lines: 1
- New tests: 64 pass (full new-file run green)

## Verdict
One **high-severity correctness bug** makes the feature a no-op on its primary (backtest) code path. The pure helpers (`pending_smt_terminal`, `_pending_age_bdays`, `revalidate_and_filter_pending`), the persistence mechanism (`_PENDING_STORE` survive-reset / clear-on-set), exception isolation, and the no-prints convention are all correct and well-tested. Remaining items are minor.

---

## Issues

```
severity: high
file: session_pipeline.py
line: 714 (ingest) vs 717-718 (force_reset wipe)
issue: _ingest_pending_smts merges survivors into hypothesis["smt_active_set"], but the immediately-following `if force_reset: save_hypothesis(DEFAULT_HYPOTHESIS)` overwrites the whole hypothesis (DEFAULT has smt_active_set=[]) and returns — wiping the carry.
detail: on_session_start runs ingest at L714 inside the cold-start block. Right after, L717-718 does
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS)) then L728 return. DEFAULT_HYPOTHESIS["smt_active_set"]
        is []. The backtest driver calls on_session_start(..., force_reset=True) (backtest_smt.py:1327) and the
        live signal path passes force_reset=self._force_reset (signal_smt.py:850), so the carry's entire
        deliverable — survivors seeded into smt_active_set before the first run_hypothesis — is destroyed before
        run_hypothesis runs at L721. Net effect: in backtest (the documented June8->June9 verification path) the
        feature seeds nothing. Empirically confirmed: driving the real on_session_start(force_reset=True) with a
        valid pre-written pending entry yields 0 carried members in smt_active_set (plan §191/§208 expect 1).
        The existing _warmup_replay_smts shadow writes to smt_active_set are also clobbered by this same reset,
        but warm-up's real product (detect_state via smts.json, a different store) survives — so warm-up tolerates
        it; the carry feature does NOT, because smt_active_set IS its only output. The L711 comment ("MERGE into
        its smt_active_set writes") is therefore also inaccurate.
suggestion: Re-seed AFTER the force_reset save_hypothesis. Either (a) move the _ingest_pending_smts call to run
        after the L718 save_hypothesis(DEFAULT) on the force_reset branch (and before the L721 run_hypothesis),
        or (b) have the force_reset reset preserve the just-merged smt_active_set, or (c) make _ingest_pending_smts
        the last cold-start step that runs post-reset on both branches. Then add the test the plan actually
        specified (see next issue).
```

```
severity: high
file: tests/test_pending_smts.py
line: 305 (test_written_pending_ingested_on_cold_start) and TestColdStartGating (stubs _ingest_pending_smts)
issue: The end-to-end cold-start test bypasses on_session_start and calls pipe._ingest_pending_smts(...) directly, so it never exercises the force_reset save_hypothesis(DEFAULT) wipe — the very interaction the plan's verification (§191) was written to catch.
detail: Plan §191 specifies: "call on_session_start(force_reset=True) on a cold start ... assert the entry appears
        in hypothesis.json['smt_active_set'] after the call." The implemented test instead invokes the private
        helper directly. TestColdStartGating DOES call on_session_start but monkeypatches _ingest_pending_smts to a
        no-op recorder, so it only checks gating, never the seed's survival. As a result the green suite hides the
        high bug above. This is a test-coverage gap that weakened the plan's acceptance check.
suggestion: Add an end-to-end test that calls the REAL on_session_start(force_reset=True) on a cold start with a
        pre-written valid pending entry (heavy IO steps stubbed as TestColdStartGating already does, but WITHOUT
        stubbing _ingest_pending_smts) and asserts a carried member is present in smt_active_set afterward. This
        will fail until the L717 wipe is fixed.
```

```
severity: low
file: session_pipeline.py
line: 2189-2192 (_active_record_to_pending) / smt_detect.py:340 (_pending_entry_to_record)
issue: `timeframe` is dropped on the round-trip. _active_record_to_pending never copies rec["timeframe"], so the rebuilt carried record always has timeframe=None.
detail: to_record (hypothesis.py:384) carries `timeframe`, and dominant()/scoring weight by timeframe
        (hypothesis.py:1438 TF_WEIGHT.get(div.get("timeframe",""),1.0)). _pending_entry_to_record reads
        e.get("timeframe") (smt_detect.py:340) but the pending entry never stored it, so a carried SMT loses its
        timeframe weighting on re-ingest. Shadow-only today (divs doesn't drive direction), so impact is currently
        nil, but it's silent data loss that will matter if Phase 3 wires divs to direction.
suggestion: Add "timeframe": rec.get("timeframe") to the dict returned by _active_record_to_pending.
```

```
severity: low
file: smt_detect.py
line: 318 (_pending_entry_to_record) and pending entry schema
issue: The persisted `valid` field is dead — it is written by _active_record_to_pending (session_pipeline.py:2204 "valid": True) and asserted in tests, but never read anywhere in revalidate_and_filter_pending or ingest.
detail: revalidate_and_filter_pending re-derives validity from the price window every cold start, so `valid`
        carries no behavior. Harmless, but it implies a gate that does not exist; a future reader may assume
        setting valid=False suppresses ingest (it does not).
suggestion: Either honor it (skip entries with valid is False before the age-cap) or drop the field and the test
        assertions on it to avoid a misleading contract.
```

```
severity: low
file: smt_detect.py
line: 218-229 (revalidate_and_filter_pending dedup, price-proximity branch)
issue: Price-proximity dedup compares the carried `price` (swept level price) against the fresh member's mnq_lvl_price, but the logical-key dedup and the price-proximity dedup use different identity notions; a carried entry could legitimately survive against a near, same-direction fresh level that is in fact the same liquidity under a different ref_name only when within 5pt.
detail: Not a bug — the tolerance is the existing DEDUP_TOL_PTS convention (matches _dedup_level_smts). Flagging
        only that the proximity check keys on `direction` alone (not side), so two opposite-`side` levels that map
        to the same `direction` and sit within 5pt would dedup. Given direction is the consumer-relevant axis this
        is defensible; confirm it is intended.
suggestion: No change required; document that proximity dedup is direction-scoped by design, or add `side` to the
        proximity key if side-distinct same-direction levels must both survive.
```

---

## Verified correct (no action)

- **Exception isolation:** write side is inside the existing shadow `try/except Exception: pass` (session_pipeline.py:2112/2194); `_ingest_pending_smts` wraps its whole body in `try/except Exception: pass` (2316/2334). A defect in either cannot break detection or startup. Good.
- **Persistence mechanism:** `_PENDING_STORE` is a module global NOT cleared by `reset_in_memory` (smt_state.py:280-291) and IS reset by `set_in_memory_mode` (smt_state.py:271). load/save route through the dedicated slot in-memory and the on-disk general_live_dir file in live (smt_state.py:442-465). `_fast_copy` prevents shared-mutation aliasing. Tests lock all four behaviors. Correct and matches plan §82.
- **Price-anchored re-validation:** `pending_smt_terminal` (smt_detect.py:146) implements long fulfilled@`wh>=fp+FULFILL` / invalidated@`wl<=fp-INVALIDATE`, short mirror, fulfilled-precedence — matches smt_status precedence and FULFILL_PTS_MNQ/INVALIDATE_PTS_MNQ. Unknown tier falls back via _fulfill_pts/_invalidate_pts (no raise). Empty overnight window keeps the entry (no false take-out). Verified by tests + manual.
- **Business-day age:** `_pending_age_bdays` returns Fri->Mon=1, Mon->Tue=1, Thu->Tue=3, same-day=0, unparseable=None (manually verified). Age-cap uses `age is not None and age > max_age` so an unparseable date is conservatively KEPT, not dropped. Correct.
- **Dedup:** carried-vs-carried collapse keeps newest fire_time; logical-key (ref_name,direction) and price-proximity vs fresh active set both drop carried. Verified by tests.
- **No prints:** no `print(`/stdout in any production path (grep clean). Complies with the project "production code is silent" convention.
- **No hardcoded URLs / secrets / SQL.** N/A to this change.

## Pre-existing failures
None introduced or observed in the new test files (64 passed). Full-suite baseline not run (out of scope); the two new test modules are green.
