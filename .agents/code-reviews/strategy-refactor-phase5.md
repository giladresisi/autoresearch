# Code Review: strategy_refactor Plan — Phase 5

**Date:** 2026-04-22
**Reviewer:** Claude Sonnet 4.6
**Scope:** strategy_smt.py, signal_smt.py, tests/test_signal_smt.py, tests/test_strategy_refactor.py

---

## Stats

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: ~+650 (net, across all 4 files)

---

## Test Run

49 tests in `tests/test_strategy_refactor.py` + `tests/test_signal_smt.py` — all pass.

---

## Issues

---

```
severity: high
file: signal_smt.py
line: 589–633
issue: Running-extremes update double-counts historical bars on first 1s bar after session init
detail: On the very first 1s bar of the day, the session-init block (lines 498–587) pre-loads
  all today's completed 1m bars into _session_mnq_rows and immediately updates _session_smt_cache
  from each of those rows. Then, without any guard, step 5 (lines 589–633) fires and re-reads
  _session_mnq_rows[-1] (the last 1m bar just appended in the init block) and applies its
  extremes to _session_smt_cache a second time. Because the cache uses max/min the double-update
  is idempotent for the cache keys themselves, but _session_run_ses_high/_session_run_ses_low
  are also updated at line 597/603, duplicating the work harmlessly. More critically, the
  smt_cache passed to process_scan_bar for the first bar of a new day already includes the
  last 1m bar's extremes from the init block; step 5 then folds them in again. The result is
  that the smt_cache passed to process_scan_bar for bar_idx=N includes bar N-1's extreme
  twice when N=0 of a new day (no divergence is falsely fired because the values are idempotent,
  but the intent "update from the previous bar" is violated on the session-boundary bar). This
  creates a subtle semantic mismatch between screen_session and live: screen_session updates
  from bar_idx-1 in the for-loop and never double-counts; live double-counts the final historical
  bar. The values remain correct (max/min is idempotent) but correctness is coincidental rather
  than structural — any future mutation to the update pattern (e.g. adding a decay factor) could
  silently produce wrong values on the first live bar.
suggestion: Add a flag set after the init block (e.g. `_session_just_inited = True`) and skip
  step 5 when the flag is True, or restructure step 5 to only run when bar_idx > 0 (which the
  live code cannot detect directly, but the rows list length after init vs. after a normal bar
  gives enough context). Simplest fix: after the init block, track the last-processed bar's
  index so step 5 can skip the rows that were already consumed by the init pass.
```

---

```
severity: high
file: signal_smt.py
line: 785–793
issue: _process_managing resets _scan_state.scan_state to "IDLE" but does not call _scan_state.reset()
detail: On trade exit, _process_managing (line 793) sets `_scan_state.scan_state = "IDLE"` but
  leaves all other ScanState fields (pending_direction, anchor_close, divergence_bar_idx,
  conf_window_start, pending_limit_signal, etc.) at their last-scanned values. If a divergence
  was detected before the trade entry (i.e. _scan_state had WAITING_FOR_ENTRY state when the
  position was opened), those pending fields are stale. When _process_scanning resumes after exit
  and calls process_scan_bar with scan_state="IDLE", the IDLE block in process_scan_bar is
  entered correctly. However, if a replacement divergence triggers while a trade is MANAGING
  (which cannot happen in the live path today because _process_scanning is not called during
  MANAGING), the stale pending_* fields would produce an incorrect continuation. More practically,
  ScanState.reset() is the authoritative way to clear pending state; relying on setting only
  scan_state="IDLE" is fragile against future additions to the ScanState class that add new fields
  needing reset. The existing test `test_process_managing_exit_tp` does not verify that
  pending_direction et al are cleared after exit.
suggestion: Replace `_scan_state.scan_state = "IDLE"` with `_scan_state.reset()` and then
  reassign `_scan_state.prior_trade_bars_held = <computed>` afterward, which is the natural
  read order. This is both correct and resilient to future ScanState field additions.
```

---

```
severity: medium
file: tests/test_signal_smt.py
line: 300, 333, 365
issue: compute_tdo patched via string path "signal_smt.compute_tdo" (correct) vs. object path
  signal_smt.compute_tdo (also correct at line 397) — three tests use the string form and one
  uses the object form; this is not a bug but masks a subtle namespace concern
detail: compute_tdo is imported into signal_smt with `from strategy_smt import (..., compute_tdo)`,
  creating a binding in the signal_smt namespace. The tests that patch "signal_smt.compute_tdo"
  are patching the correct namespace (the one _process_scanning actually calls). The test at
  line 397 uses `monkeypatch.setattr(signal_smt, "compute_tdo", ...)` which is equivalent.
  No functional issue, but the inconsistency could confuse future maintainers about which
  namespace to patch. The three stale-guard / redetection tests patch via string; the valid-signal
  test patches via object reference — both work but the inconsistency is worth standardizing.
suggestion: Standardize all compute_tdo patches to `monkeypatch.setattr(signal_smt, "compute_tdo",
  ...)` (object form) for consistency with how manage_position is patched in the MANAGING tests.
```

---

```
severity: medium
file: signal_smt.py
line: 693–700
issue: _divergence_reentry_count and state.reentry_count in ScanState are maintained as two
  separate, unsynchronized counters tracking the same semantic concept
detail: _process_scanning maintains _divergence_reentry_count (lines 87, 695–716) which counts
  signals emitted per divergence level per session. process_scan_bar inside strategy_smt
  maintains state.reentry_count which counts confirmations emitted from the WAITING/REENTRY
  states. Both are incremented independently and never compared. When a trade exits and
  _scan_state.scan_state is reset to "IDLE" (line 793), state.reentry_count is left at its
  last value, but _divergence_reentry_count continues from its own value. If
  MAX_REENTRY_COUNT is applied via _divergence_reentry_count in signal_smt (line 701) and
  also via state.reentry_count in process_scan_bar (line 1507 of strategy_smt), the gate can
  fire from either counter, making re-entry behavior difficult to reason about. The backtest
  uses only state.reentry_count; live uses both. This creates a semantic divergence between
  backtest and live that the refactor was intended to close.
suggestion: After the refactor settles, remove _divergence_reentry_count from signal_smt and
  rely exclusively on state.reentry_count from ScanState, reading it after process_scan_bar
  returns to decide whether to accept the signal. The MAX_REENTRY_COUNT gate in process_scan_bar
  already blocks signals when the count is exceeded (REENTRY_ELIGIBLE path). The outer gate
  in signal_smt is then redundant and its removal would fully unify the live and backtest paths.
```

---

```
severity: medium
file: signal_smt.py
line: 526–557 (MNQ pre-load) and 558–587 (MES pre-load)
issue: Historical bar pre-load on session init does not filter out bars already in _session_1s_buf
detail: On process restart mid-session, _session_init_date triggers the pre-load loop which reads
  all today's session bars from _mnq_1m_df. These are 1m bars. The live stream immediately appends
  a 1s bar (line 636) for the current second. If a 1m bar for the same minute was already appended
  to _mnq_1m_df by on_mnq_1m_bar during startup, that 1m bar's OHLCV is pre-loaded into
  _session_mnq_rows, and then the 1s sub-bar for the still-in-progress minute is also appended
  at step 6. The result is that _session_mnq_rows contains the completed 1m bar AND the
  in-progress 1s bar of the same clock minute as two separate rows. This inflates bar_idx
  by one vs. the backtest for every historical 1m bar that overlaps with a 1s bar in progress
  at startup time. No divergence is falsely triggered (1s data is more granular than 1m), but
  bar_idx alignment between backtest and live is broken for the in-progress minute at startup.
suggestion: Filter the 1m pre-load to bars strictly before `bar_ts.floor("min")` (i.e., exclude
  the current in-progress minute). This ensures the 1s sub-bars for the current minute are the
  only source of data for the current clock minute, matching the backtest's bar-level accounting.
```

---

```
severity: low
file: tests/test_signal_smt.py
line: 231–243 (test_process_scanning_session_gate_before_start)
      246–259 (test_process_scanning_session_gate_after_end)
      263–284 (test_process_scanning_alignment_gate)
issue: Tests patch strategy_smt.process_scan_bar but do not assert it was not called via the
  calls counter after the gate path returns, leaving the gate test logically incomplete for
  the alignment gate case
detail: The alignment gate test (line 263) correctly sets up misaligned buffers and asserts
  `len(called) == 0`. However the test does not verify that _session_mnq_rows and _session_mes_rows
  were NOT mutated (steps 6 of _process_scanning append to these lists before calling
  process_scan_bar). Looking at the code: the alignment gate returns at line 476 BEFORE step 6,
  so the rows lists are not mutated. But the test does not verify this invariant. If the gate
  position were ever changed (e.g. moved after step 6), the test would still pass because it
  only checks `len(called) == 0`, not that no state was mutated.
suggestion: Add assertions on `len(signal_smt._session_mnq_rows) == 0` and
  `len(signal_smt._session_mes_rows) == 0` to the alignment gate test to make the invariant
  explicit and regression-safe.
```

---

```
severity: low
file: strategy_smt.py
line: 1657–1658
issue: pending_htf_confirmed_tfs is attached to signal only when not None, but the field is
  absent from the signal dict when HTF_VISIBILITY_REQUIRED is False
detail: At line 1657–1658, `signal["htf_confirmed_timeframes"]` is only set when
  `state.pending_htf_confirmed_tfs is not None`. When HTF_VISIBILITY_REQUIRED is False the key
  is absent from the signal dict entirely. Downstream code that calls `signal.get("htf_confirmed_timeframes")`
  handles this safely, but callers doing `signal["htf_confirmed_timeframes"]` would raise KeyError.
  The previous code (before refactor) may have had the same behavior; this is noted for awareness
  rather than as a regression.
suggestion: Normalize to always emit the key: `signal["htf_confirmed_timeframes"] = state.pending_htf_confirmed_tfs`
  (which will be None when HTF is off). This makes the signal dict schema uniform regardless of
  configuration.
```

---

## Pre-existing Issues (Not Introduced by This Changeset)

The execution report for the prior plan (`smt-limit-entry-anchor-close.md`) documents one latent
gap: when `LIMIT_EXPIRY_SECONDS > 0` (forward-limit mode), the fill path in `WAITING_FOR_LIMIT_FILL`
does not apply `secondary_target`, `secondary_target_name`, or `DISPLACEMENT_STOP_MODE` adjustments
to the filled position. This is pre-existing and unrelated to the current refactor.

---

## Summary

All 49 tests pass. No security issues. The two high-severity issues concern correctness of the
stateful scanner: (1) double-application of the last historical bar's extremes on the session-init
boundary, and (2) incomplete reset of ScanState on trade exit. Both are benign under current
conditions (idempotent max/min, IDLE re-entry correct) but are structurally fragile. The two medium
issues document a dual-counter architecture that creates live/backtest semantic divergence around
re-entry counting, and a potential bar-index misalignment on mid-session restart. These are good
targets for the next cleanup pass.
