# Code Review: Session Orchestrator Daemon

**Date:** 2026-04-19
**Plan:** `.agents/plans/session-orchestrator.md`
**Reviewer:** ai-dev-env:code-review

---

## Stats

- Files Modified: 2 (pyproject.toml, PROGRESS.md)
- Files Added: 14 (7 source + 7 test)
- Files Deleted: 0
- New lines: ~1,360
- Deleted lines: 0

---

## Issues Found

---

```
severity: medium
file: orchestrator/relay.py
line: 48
issue: EXIT event dict always stores pnl as a positive magnitude — sign (profit vs loss) is lost
detail: The _EXIT_RE regex uses group(4) = r'[\d.]+' to capture the P&L value. The sign character
        (+/-) is consumed by the non-capturing r'[+\-]' group but never stored. This means
        float(m.group(4)) is always positive: a -$3.00 stop loss and a +$3.00 profit produce
        identical pnl=3.0 in the event dict. The test test_emit_exit_negative_pnl_parses explicitly
        asserts pnl == 3.00 for a -$3.00 line, which passes — but it documents the information loss
        rather than testing correct sign preservation. The summarizer currently sends the raw log
        to Claude (not the events dict), so this bug has no current effect on summary quality.
        However, any future code using events[].pnl for aggregation (win rate, total P&L) will
        compute incorrect results.
suggestion: Change the capturing group to include the sign: r'([+\-][\d.]+)' and store
            float(m.group(4)) which will then be negative for loss lines. Update the test to assert
            pnl == -3.00 for EXIT_NEG and pnl == 78.50 for EXIT_TP.
```

---

```
severity: medium
file: orchestrator/process.py
line: 71-72
issue: Reader thread from crashed proc1 may still be running when proc2 is spawned — concurrent
       relay.emit() calls from two threads with no lock
detail: In _monitor(), when proc.poll() is not None, reader.join(timeout=2) is called. If the
        reader thread takes more than 2 seconds to drain stdout (e.g., large buffered output),
        join() returns but reader1 is still alive. run_session() then calls self._spawn() to create
        proc2 and starts a new _monitor() with a new reader2 thread. Both reader1 and reader2 now
        call relay.emit() concurrently. relay._events is an unsynchronized list; _try_parse() appends
        to it from whichever thread processes a line. In CPython the GIL prevents data structure
        corruption, but there is a logical race: if reset() were ever called between an append from
        reader1 and the subsequent get_events() call, an event could be silently dropped or retained
        incorrectly. More concretely, events from proc1's remaining buffered output could interleave
        with proc2's events, making the session log order ambiguous.
suggestion: After reader.join(timeout=2), close proc.stdout explicitly (proc.stdout.close()) to
            force reader1 to exit the for-loop immediately. This makes the 2-second join effectively
            guaranteed to complete for any normal stdout volume. Alternatively, after join returns,
            check reader.is_alive() and log a warning if the thread is still running.
```

---

```
severity: low
file: orchestrator/summarizer.py
line: 74-75
issue: summary_path.write_text() will raise FileNotFoundError if the session directory does not
       exist at summarization time
detail: summary_path = sessions_dir / date.isoformat() / "summary.md". The parent directory
        (sessions_dir / date.isoformat()) must exist before write_text() is called. In the main.py
        flow this is guaranteed: _make_session_channels() calls session_dir.mkdir(parents=True,
        exist_ok=True) before ProcessManager runs. However, Summarizer.run() does not create the
        directory itself. If summarizer.run() is called in isolation (e.g., to regenerate a missing
        summary, or from a test that forgets to create the dir), it will raise FileNotFoundError.
        The test test_summarizer_writes_summary_md creates cur_dir explicitly, masking this fragility.
suggestion: Add `summary_path.parent.mkdir(parents=True, exist_ok=True)` before the write_text call
            on line 75. This makes the method self-contained and idempotent.
```

---

```
severity: low
file: orchestrator/output.py
line: 4
issue: Unused import — `import sys` is present but `sys` is never referenced
detail: StdoutSink.write() uses print() rather than sys.stdout.write(), so sys is never used.
        The import likely came from an earlier draft and was not cleaned up.
suggestion: Remove line 4 (`import sys`).
```

---

```
severity: low
file: orchestrator/main.py
line: 82
issue: `import os` inside function body — inconsistent with module-level imports elsewhere
detail: _check_setup() has `import os` as the first statement inside the function. All other
        stdlib imports in this module are at the top level. In-function imports work correctly but
        violate PEP 8 and are inconsistent with the rest of the codebase. This was flagged in the
        execution report's CLAUDE.md note about module-level imports being required for mockability.
        While os is not mocked here, it sets a bad precedent.
suggestion: Move `import os` to the top of orchestrator/main.py with the other stdlib imports.
```

---

```
severity: low
file: (missing .gitignore entry)
line: N/A
issue: sessions/ directory (live trading logs with real P&L data) is not gitignored
detail: orchestrator/main.py creates sessions/YYYY-MM-DD/{signals.log,orchestrator.log,summary.md}
        at runtime. These files contain live trading output including real P&L figures. The directory
        is not in .gitignore, so `git add .` or an accidental `git commit -A` could commit live
        trading data to the repo. The execution report noted this risk explicitly.
suggestion: Add `sessions/` to .gitignore. The directory structure should be created at runtime
            by the daemon, not tracked in source control.
```

---

## Non-Issues (Reviewed and Cleared)

- **_sleep_until with past target:** The `if delay > 0` guard prevents negative sleeps. Correct.
- **next_session_open at exactly 09:00 ET:** Returns next trading day. This is fine — run() only calls it post-session (~13:35 ET), not to determine whether to run today's session.
- **Summarizer constructed before while loop:** Fail-fast behavior is correct. ANTHROPIC_API_KEY absence will raise RuntimeError before any sleeping occurs.
- **FileSink open-on-every-write:** Opens in append mode on each write call. Correct — the flush is immediate and the handle is properly closed. No file descriptor leak.
- **_wait_until_grace_end in integration tests:** Correctly patched out via `patch.object(ProcessManager, "_wait_until_grace_end")`. Integration test behavior is sound.
- **OutputChannel._sinks type annotation:** `list` (unparameterized) is acceptable for Python 3.10+ generic. Not a bug.
- **daemon=True on reader thread:** Correct — prevents reader threads from blocking daemon shutdown.

---

## Summary

All 53 tests pass. The implementation is clean and well-structured. The critical path (trading day detection → subprocess spawn → line relay → SIGTERM → Claude summary) is correct. Four issues require fixes before production use:

1. **P&L sign loss** (medium) in `relay.py` — the events API reports losses as positive numbers, which will corrupt any future P&L aggregation.
2. **Reader thread overlap on crash-restart** (medium) in `process.py` — low probability but genuine concurrent-write scenario under the restart path.
3. **Summarizer missing mkdir** (low) in `summarizer.py` — currently protected by main.py flow but fragile as a standalone call.
4. **sessions/ not gitignored** (low) — risk of accidental commit of live trading data.

The unused `import sys` and the in-function `import os` are minor cleanup items.
