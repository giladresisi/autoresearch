# Code Review: Stage 2 — PickMyTrade Automation

**Date:** 2026-04-30
**Plan:** `.agents/plans/stage2-pickmytrade-automation.md`
**Reviewer:** Claude Sonnet 4.6

---

## Stats

- Files Modified: 4 (orchestrator/main.py, orchestrator/process.py, .env.example, pyproject.toml)
- Files Added: 5 (execution/pickmytrade.py, automation/__init__.py, automation/main.py, tests/test_pickmytrade_executor.py, tests/test_automation_main.py)
- Files Deleted: 0
- New lines: ~750
- Deleted lines: ~5

**Test Results:** 26/26 new tests passing. All pre-existing failures confirmed pre-existing (from Stage 1 baseline report: 16 pre-existing failures, unchanged).

---

## Issues Found

---

```
severity: medium
file: execution/pickmytrade.py
line: 222
issue: _record_fill() has no mutex — concurrent calls would corrupt fills.jsonl
detail: _record_fill is called from both the poll loop thread and the webhook handler
        thread (in separate modes). Although today only one mode is active at a time,
        the method also uses a read-then-write pattern (lines 239-243: read the existing
        file contents, write them plus the new line into a tmp file, then replace). If
        two fills resolve simultaneously (e.g., future dual-position support, or a rapid
        succession of poll hits), the second caller could overwrite the first caller's
        write, losing a fill record.
suggestion: Acquire self._lock before the tempfile block and release after tmp_path.replace():
        with self._lock:
            with tempfile.NamedTemporaryFile(...) as tmp:
                ...
            tmp_path.replace(self._fills_path)
```

---

```
severity: medium
file: execution/pickmytrade.py
line: 216
issue: Webhook server binds to all interfaces (""), not localhost
detail: http.server.HTTPServer(("", self._fill_webhook_port), _Handler) binds on 0.0.0.0,
        meaning any host on the network can POST spoofed fill confirmations to this port.
        A spoofed fill would clear a pending order from _pending without an actual fill
        occurring, causing the fill to never be recorded, and the trade to be considered
        closed when it is still open.
suggestion: Bind to "127.0.0.1" instead of "": HTTPServer(("127.0.0.1", self._fill_webhook_port), _Handler).
        If the PMT service posts from an external host, use a reverse proxy or add HMAC
        signature verification on the incoming payload.
```

---

```
severity: medium
file: execution/pickmytrade.py
line: 50
issue: Double-call to start() leaks the previous fill thread
detail: start() calls _stop_event.clear() then unconditionally overwrites self._fill_thread
        with a new Thread. If start() is called a second time without stop(), the prior
        thread is still running (waiting on stop_event.wait()), but its reference is
        lost. The old thread will run forever until the process exits.
suggestion: Add a guard at the top of start():
        if self._fill_thread is not None and self._fill_thread.is_alive():
            raise RuntimeError("start() called while fill thread is still running")
```

---

```
severity: medium
file: execution/pickmytrade.py
line: 177
issue: _query_fill silently suppresses all exceptions including programming errors
detail: except Exception: return None on lines 177-178 swallows any exception that occurs
        during response parsing — including KeyError, AttributeError, and other
        programming bugs — making debugging of fill query failures very difficult.
        A parse error is indistinguishable from an HTTP error or a legitimate "not yet
        filled" response.
suggestion: Log the exception at minimum:
        except Exception as exc:
            print(f"[FILL-WARN] _query_fill error for {order_id}: {exc}", flush=True)
            return None
```

---

```
severity: low
file: execution/pickmytrade.py
lines: 133, 209, 244
issue: print() used for production logging in violation of CLAUDE.md standards
detail: CLAUDE.md states "Production code is silent: No print/stdout logging in production
        paths. Capture errors in structured data (e.g., status fields, error columns)
        rather than printing them. Use a logging framework only for critical failures."
        Lines 133, 209, and 244 use raw print() calls. Note: automation/main.py also uses
        print() extensively, but that module appears to be intentionally print-driven (all
        output is consumed by the orchestrator relay). The executor's internal fill
        warnings are different — they are operational noise that should be structured.
suggestion: Replace with logging.warning()/logging.info() calls or write to a structured
        error field in FillRecord. At minimum, emit structured JSON lines consistent with
        the rest of the automation pipeline.
```

---

```
severity: low
file: execution/pickmytrade.py
line: 184
issue: Webhook handler reads unbounded Content-Length without a size cap
detail: do_POST reads exactly Content-Length bytes from rfile with no upper bound:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        Since the server binds to all interfaces (see medium issue above), a request with
        a very large Content-Length header could cause the process to allocate and read
        a large amount of memory before the JSON parse fails.
suggestion: Add a size cap: MAX_BODY = 65_536; length = min(int(...), MAX_BODY).
        This is low severity because the webhook port is intended for PMT callbacks only,
        but combined with the all-interface bind, it becomes exploitable.
```

---

```
severity: low
file: automation/main.py
line: 95
issue: _hist_daily_df reuses 5m OHLCV data as a daily data source
detail: Line 928: _hist_daily_df = _hist_mnq_df  # reuse 5m hist
        compute_pdh_pdl (called at line 386) is documented as using index.date to
        identify the previous day. Passing 5m data works because .index.date still
        returns per-date groupings, but PDH/PDL will be computed from 5m bar OHLCV
        rather than from the actual daily OHLCV bars, which may differ (5m High/Low
        within bars may not equal the session High/Low if bars are not full-session).
        In practice the difference is small, but the intent mismatch should be noted.
suggestion: Load a separate daily parquet if available, or add a comment clarifying that
        compute_pdh_pdl is driven by 5m bars and that the resulting PDH/PDL values are
        the running high/low of all 5m bars on the prior day (which approximates the
        daily high/low for liquid futures).
```

---

```
severity: low
file: orchestrator/process.py
line: 112
issue: _kill_existing_signal_smt matches "automation.main" string too broadly for list commands
detail: When script_path is a list (["uv", "run", "python", "-m", "automation.main"]),
        script_name = script_path[-1] = "automation.main". The kill loop then kills any
        process whose cmdline contains "automation.main". This correctly kills the
        automation process, but if any other process happens to have "automation.main"
        in its arguments (e.g., a log grep, a text editor with the file open), it would
        also be terminated.
suggestion: For list commands, use the full module path or a more specific match
        (e.g., require that "-m" precedes "automation.main" in cmdline), or accept this
        as an acceptable approximation given the narrow operational context.
```

---

```
severity: low
file: automation/main.py
line: 129
issue: _smtv2_dispatcher module-level variable is declared but never initialized in main()
detail: Line 129: _smtv2_dispatcher: "SmtV2Dispatcher | None" = None
        main() sets _smtv2_pipeline (line 932) but never instantiates or assigns
        _smtv2_dispatcher. If code in _on_bar or elsewhere later checks _smtv2_pipeline
        == "v2" and tries to call _smtv2_dispatcher.on_1m_bar(...), it will AttributeError
        on None. This is currently safe because no v2 routing code exists in the callback
        path, but it is a latent bug that will be invisible until the v2 path is enabled.
suggestion: Either add an assertion in main() ("v2 pipeline not yet wired in automation/main.py;
        only v1 supported"), or add a runtime guard in any future code that reads
        _smtv2_dispatcher before using it.
```

---

## Summary

The implementation is functionally correct and all 26 tests pass. The most actionable issues are:

1. **_record_fill concurrency** (medium): missing mutex around the read-modify-write file operation.
2. **Webhook server binds to all interfaces** (medium): spoofed fill confirmations are possible from the network.
3. **start() double-call leaks thread** (medium): minor lifecycle correctness issue.
4. **_query_fill exception suppression** (medium): makes fill polling bugs invisible.

The remaining issues are low-severity and do not affect correctness for the current single-contract, single-session use case.
