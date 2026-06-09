# Code Review — SMT V2 Detection Redesign

**Stats:**
- Files Modified: 3 (session_pipeline.py, smt_state.py, tests/test_smt_state.py)
- Files Added: 2 (smt_detect.py, tests/test_smt_detect.py) + integration tests appended to tests/test_session_pipeline.py
- New lines (impl): ~470 (smt_detect.py ~510 incl. tests; session_pipeline.py +402; smt_state.py +21)

## Issues found & fixed

```
severity: high
file: smt_detect.py
line: eligible_levels (~105-116, original)
issue: asia session level eligible while still forming
detail: The original close-hour model used `et.hour >= close_h` with asia close_h=0,
        which is true for every hour → asia_high/low counted as an eligible SMT target
        even during 18:00–23:59 ET while the asia session is still forming. Spec §2.1
        requires the currently-forming session to be excluded.
fix: Replaced _SESSION_CLOSE_HOUR with _SESSION_WINDOW (open,close) forming windows;
     a session level is eligible only when ET hour is OUTSIDE its forming window
     (asia eligible 00:00–18:00). Added test_asia_not_eligible_while_forming.
```

```
severity: medium
file: smt_detect.py
line: detect_fill_smts fire_price (~415, ~436 original)
issue: cross-instrument scale mix in fill re-arm
detail: fire_price was set to mes_close (~3000) when MES led, but the re-arm
        opposite-move gate always computes `mnq_close - fire_price` against the MNQ
        scale (~21000). A MES-led fire then produced a nonsensical ~18000-pt "opposite
        move" that instantly re-armed every bar.
fix: fire_price is now always the MNQ close at fire time (scale-consistent). Re-arm
     measures the opposite move against MNQ uniformly. Covered by test_fill_rearm.
```

```
severity: medium
file: smt_detect.py
line: _detect_level_smts post-pass re-arm (~245 original)
issue: level/fill shared-state cross-contamination
detail: The pipeline shares one self._detect_state dict between detect_regular_smts
        and detect_fill_smts. The batch post-pass that re-arms opposite-direction
        dormant LEVEL pairs iterated ALL state entries, including fill entries (keyed by
        bare FVG name). For a fill key, `skey.rsplit("|")[-1]` returned the whole name,
        `_opposite(name)` defaulted to "long", so a long level SMT in the batch could
        spuriously re-arm a dormant fill.
fix: Guarded the post-pass to only touch keys containing "|" (the level-SMT key format);
     fill entries (no "|") are skipped — they own their re-arm logic in detect_fill_smts.
```

## Verified non-issues
- `on_1m_bar` signature unchanged; all six callers' tests pass (test_session_pipeline,
  test_smt_v2_dispatcher). The new block is additive and emits nothing
  (test_on_1m_bar_events_unchanged).
- MNQ `liquidities` output byte-identical post-refactor
  (test_mnq_liquidities_unchanged_regression + 42 baseline pipeline tests green).
- `save_smts`/`load_smts` honor in-memory mode; per-bar write is the cheap _STORE path
  in backtests (test_smts_inmemory).
- No prints in production paths; detection functions are total (return ([],state) on
  degenerate input — test_empty_frames_no_crash).

## Pre-existing failures (NOT introduced here)
24 full-suite failures pre-date this changeset (hypothesis_smt, pickmytrade_executor,
automation_main, check_session_parquets, orchestrator_main, smt_humanize) — confirmed
identical before and after. test_ib_realtime.py hangs on a 20s sleep (integration-style)
and was excluded from the full-suite run.
