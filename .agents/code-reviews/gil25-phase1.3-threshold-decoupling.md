# Code Review — GIL-25 Phase 1.3: decouple FULFILL_PTS into DEPLETE/DEPART knobs

No-op refactor review. Scope: smt_detect.py, tests/test_smt_level_invalidation.py.

**Stats:**
- Files Modified: 2
- Files Added: 0 (plan file `.agents/plans/4.smt-threshold-decoupling-phase1.3.md` is untracked, not part of code change)
- Files Deleted: 0
- New lines: 79
- Deleted lines: 8

## Verdict
Refactor is correct and behavior-preserving. New constant families are byte-identical to FULFILL_PTS and are distinct dict objects (not aliases). New helpers mirror `_fulfill_pts` exactly including the `.get(tier, table["session"])` fallback. All four migrated call sites map to the correct semantic role; the two fulfillment sites (measured from fire close) were correctly left on `_fulfill_pts`. Tests pass (29 in the touched file, 35 in related pending/invalidation suites).

## Findings

### Correctness (verified, no defects)
- Helpers `_deplete_pts` (smt_detect.py:127-129) and `_depart_pts` (smt_detect.py:132-134) are structurally identical to `_fulfill_pts` (smt_detect.py:122-124), including the unknown-tier fallback to the session row.
- Seed values byte-identical AND distinct objects (runtime-verified): `DEPLETE_PTS_MNQ`/`DEPART_PTS_MNQ` == `FULFILL_PTS_MNQ` and `_MES` counterparts == `FULFILL_PTS_MES`, but `is not` the same dict — so future tuning of one knob cannot mutate the others.
- Call-site migrations all correct by semantic role:
  - smt_detect.py:155 (`pre_session_depleted`) → `_deplete_pts(tier, inst)` — depletion, passes variable `inst`; helper accepts arbitrary inst. Correct.
  - smt_detect.py:647 (fixed-level re-arm departure) → `_depart_pts(tier, "mnq")` — departure, mnq-only. Correct.
  - smt_detect.py:713 / :714 (per-ticker `__level_inv__` latch, mnq/mes) → `_deplete_pts(...)` — depletion. Correct.
- Retained on `_fulfill_pts` (correctly NOT migrated): smt_detect.py:187 (`pending_smt_terminal`, measured from fire_price) and smt_detect.py:629 (block (a) fulfillment, measured from fire close). Both are genuine fulfillment semantics. Confirmed via grep that `_fulfill_pts(` now appears at exactly the definition + these two sites.

### Low-severity / informational (not blocking)

severity: low
file: tests/test_smt_level_invalidation.py
line: 7
issue: Stale constant name in the test-file header comment (pre-existing, not touched by this diff).
detail: The file header (L1-8) still describes the depletion latch as "once a ticker runs a confirmed HH/LL FULFILL_PTS[tier] beyond a level". After this refactor the latch reads DEPLETE_PTS via `_deplete_pts`. The three in-code comments at the migrated sites were correctly updated, but this header comment describing the same depletion mechanism still names the old constant.
suggestion: Update "FULFILL_PTS[tier]" → "DEPLETE_PTS[tier]" in the L7 header to match the corrected in-code comments. Cosmetic; no behavior impact.

severity: low
file: tests/test_smt_level_invalidation.py
line: 235
issue: Imports placed mid-file rather than consolidated at the top.
detail: A second `from smt_detect import (...)` block plus `import pytest` is added at L235-242 (with `# noqa: E402`), while the file already imports from smt_detect at L13-18. This is a deliberate self-contained placement for the Phase-1.3 additions and is annotated with noqa, so it is acceptable, but consolidating into the top import block would be cleaner.
suggestion: Optional — fold the new names into the existing top-of-file `from smt_detect import (...)` block and add `pytest` to the top imports; drop the noqa. Not required.

### Note on plan-vs-implementation accounting (not a defect)
- The plan §2.3 heading says "5 sites" but its own table enumerates 4 migrated call lines (129/687/688/621 in pre-edit numbering) and retains 2 (161/603). The implementation migrated exactly those 4 lines and retained the 2 — matching the table, which is the binding spec. The "5" in the prose is a plan-internal miscount, not an implementation error.
- The plan says "3 comments corrected"; the diff corrects 4 comment locations (docstring L146, latch-skip L545, departure L642, latch-update L707). The 4th (L545) accurately describes the depletion-latch skip path that consumes `_deplete_pts`, so the extra correction is accurate and beneficial, not a regression.

## Tests run
- `pytest tests/test_smt_level_invalidation.py -q` → 29 passed (8 new parametrized no-op/fallback tests + existing).
- `pytest tests/test_pending_smts.py tests/test_smt_invalidation.py -q` → 35 passed (confirms `pending_smt_terminal` still resolves via `_fulfill_pts`; adverse-run invalidation untouched).
- Runtime assertion: seed equality + distinct-object identity confirmed.

Test quality: the new tests lock the no-op invariant (parametrized over all tier×inst) and the unknown-tier fallback for both new helpers — exactly the properties this refactor must preserve. Good coverage for a no-op split.
