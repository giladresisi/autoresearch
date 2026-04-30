# Session Orchestrator Daemon

**Created:** 2026-04-19
**Complexity:** ⚠️ Medium
**Spec:** `docs/superpowers/specs/2026-04-18-session-orchestrator-design.md`
**Primary Files:** `orchestrator/` (new package), `pyproject.toml`, `PROGRESS.md`

---

## Problem Statement

`signal_smt.py` must be manually started each trading day and its output is lost unless the
terminal is watched. There is no post-session summary. This plan builds a daemon that manages
the full session lifecycle automatically.

## User Story

As a trader running the SMT strategy, I want a daemon that starts `signal_smt.py` at market
open, relays its signals, restarts once on crash, stops it at session end, and produces a
Claude-powered summary with metrics + narrative + parameter recommendations — so I can leave
the system running and receive actionable recaps each day.

---

## Execution Agent Rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Baseline

Before starting, run:
```bash
uv run pytest tests/ -x -q 2>&1 | tail -5
```
Document passing/failing count. Pre-existing failures are not in scope.

---

## Signal Line Formats

From `signal_smt.py` `_format_signal_line` / `_format_exit_line`:

```
[09:14:32] SIGNAL    long  | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x
[09:47:11] EXIT      tp    | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract
[09:30:00] STOP_MOVE breakeven   | stop 19848.50 → 19850.75
```

EXIT labels: `tp`, `stop`, `session_end`, `market`. STOP_MOVE contains Unicode `→` — relay passes it through without parsing.

---

## Affected Files

### New Files
- `orchestrator/__init__.py`
- `orchestrator/output.py`
- `orchestrator/scheduler.py`
- `orchestrator/relay.py`
- `orchestrator/process.py`
- `orchestrator/summarizer.py`
- `orchestrator/main.py`
- `tests/test_orchestrator_output.py`
- `tests/test_orchestrator_scheduler.py`
- `tests/test_orchestrator_relay.py`
- `tests/test_orchestrator_process.py`
- `tests/test_orchestrator_summarizer.py`
- `tests/test_orchestrator_main.py`

### Modified Files
- `pyproject.toml` — add `anthropic>=0.50`, `exchange-calendars>=4.5`, `psutil>=5.9`
- `PROGRESS.md` — add session-orchestrator entry

---

## Architecture

```
orchestrator/
  main.py        ← entry point; main daemon loop
  scheduler.py   ← is_trading_day(), next_session_open()
  process.py     ← ProcessManager: spawn/relay/restart/stop signal_smt.py
  relay.py       ← SessionRelay: emit lines, parse SIGNAL/EXIT, accumulate events
  summarizer.py  ← Summarizer: build prompt, call Claude API, write summary.md
  output.py      ← OutputChannel + StdoutSink + FileSink

sessions/
  YYYY-MM-DD/
    signals.log        ← raw stdout from signal_smt.py (immediate flush)
    orchestrator.log   ← start/stop/crash events
    summary.md         ← Claude summary (post-session)
```

**Data flow:**

```
signal_smt.py stdout
    → relay.emit(line)
        → OutputChannel.write(line)       → stdout + signals.log
        → parse SIGNAL/EXIT → events[]
→ (session ends)
→ summarizer.run(events, signals.log, sessions_dir)
        → Claude API call
        → OutputChannel.write(summary)    → stdout + summary.md
```

---

## Environment Variable

`ANTHROPIC_API_KEY` must be set in the shell environment before running the daemon. The
summarizer checks for it at init and raises `RuntimeError` if missing — fail fast rather than
running a 4-hour session and then failing at summarization.

No `python-dotenv` dependency added; consistent with existing project pattern (env vars set
externally or via shell).

---

## Tasks

### WAVE 1 — Foundation (run all three in parallel)

---

### Task 1A — Add dependencies to pyproject.toml
**WAVE:** 1 | **AGENT_ROLE:** dependency-manager

Add to `pyproject.toml` `dependencies` list:
```toml
"anthropic>=0.50",
"exchange-calendars>=4.5",
"psutil>=5.9",
```

`psutil` is used in `process.py` to find and terminate any existing `signal_smt.py` process before spawning a new one — prevents duplicate instances after a daemon restart.

Verify no conflicts:
```bash
uv add anthropic exchange-calendars --dry-run 2>&1 | head -20
```

If `--dry-run` is unsupported, just add the lines and note that `uv sync` will validate.

---

### Task 1B — orchestrator/output.py + tests
**WAVE:** 1 | **AGENT_ROLE:** implementer

Create `orchestrator/__init__.py` (empty).

Create `orchestrator/output.py` with three classes:
- `StdoutSink` — `write(text)` calls `print(text, end="", flush=True)`
- `FileSink(path)` — `write(text)` opens path in append mode, writes, flushes immediately
- `OutputChannel` — holds a list of sinks; `write(text)` fans out to all; `writeln(text)` appends `\n`

Create `tests/test_orchestrator_output.py`:

Test cases:
1. `test_stdout_sink_writes_to_stdout` — `StdoutSink().write("hello")` → `capsys` captures "hello"
2. `test_file_sink_creates_and_appends` — `FileSink(tmp_path / "test.log").write("line\n")` twice → file contains "line\nline\n"
3. `test_file_sink_immediate_flush` — write and immediately read without closing: content present
4. `test_output_channel_calls_all_sinks` — two mock sinks registered, `write()` calls both
5. `test_output_channel_writeln_adds_newline` — `writeln("x")` → sink receives "x\n"
6. `test_output_channel_empty_sinks_no_error` — `OutputChannel().write("x")` with no sinks

---

### Task 1C — orchestrator/scheduler.py + tests
**WAVE:** 1 | **AGENT_ROLE:** implementer

Create `orchestrator/scheduler.py`:
- Module-level `_CALENDAR = xcals.get_calendar("XNYS")` using `exchange_calendars`
- `is_trading_day(date) -> bool` — `_CALENDAR.is_session(str(date))`
- `next_session_open(now=None) -> datetime` — if trading day and `now < 09:00 ET` today, return today 09:00 ET; else advance via `_CALENDAR.next_session()` and return that date's 09:00 ET
- `get_et_now() -> datetime` — `datetime.datetime.now(tz=ZoneInfo("America/New_York"))`

Create `tests/test_orchestrator_scheduler.py`:

Test cases:
1. `test_is_trading_day_weekday` — Monday 2026-04-20 → True
2. `test_is_trading_day_saturday` — 2026-04-18 (Saturday) → False
3. `test_is_trading_day_sunday` — 2026-04-19 → False
4. `test_is_trading_day_mlk_day_2026` — 2026-01-19 (MLK Day) → False
5. `test_is_trading_day_july4_observed` — 2025-07-04 (Friday) → False
6. `test_next_session_open_before_open_today` — `now` = 2026-04-21 08:00 ET on a trading day → returns 2026-04-21 09:00 ET
7. `test_next_session_open_after_open_today` — `now` = 2026-04-21 10:00 ET → returns next trading day 09:00 ET
8. `test_next_session_open_weekend` — `now` = Saturday 2026-04-18 → returns Monday 09:00 ET

---

### WAVE 2 — Core Pipeline (parallel after Wave 1)

---

### Task 2A — orchestrator/relay.py + tests
**WAVE:** 2 | **DEPENDS_ON:** Task 1B | **AGENT_ROLE:** implementer

Create `orchestrator/relay.py`:

```python
import re
from orchestrator.output import OutputChannel

_SIGNAL_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] SIGNAL\s+(long|short)\s*\|'
    r'\s*entry ~([\d.]+).*?\|\s*stop ([\d.]+)\s*\|\s*TP ([\d.]+)\s*\|\s*RR ~([\d.]+)x'
)
_EXIT_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] EXIT\s+(\S+)\s*\|'
    r'\s*filled ([\d.]+)\s*\|\s*P&L [+\-]\$([\d.]+)\s*\|\s*(\d+) MNQ'
)


class SessionRelay:
    """Relays signal_smt.py stdout lines; parses SIGNAL/EXIT into structured events."""

    def __init__(self, channel: OutputChannel) -> None:
        self._channel = channel
        self._events: list[dict] = []

    def emit(self, line: str) -> None:
        """Write line to output channel and parse if SIGNAL/EXIT."""
        self._channel.write(line if line.endswith("\n") else line + "\n")
        self._try_parse(line)

    def _try_parse(self, line: str) -> None:
        m = _SIGNAL_RE.search(line)
        if m:
            self._events.append({
                "type": "SIGNAL",
                "time": m.group(1),
                "direction": m.group(2),
                "entry": float(m.group(3)),
                "stop": float(m.group(4)),
                "tp": float(m.group(5)),
                "rr": float(m.group(6)),
            })
            return
        m = _EXIT_RE.search(line)
        if m:
            self._events.append({
                "type": "EXIT",
                "time": m.group(1),
                "exit_kind": m.group(2),
                "filled": float(m.group(3)),
                "pnl": float(m.group(4)),
                "contracts": int(m.group(5)),
            })

    def get_events(self) -> list[dict]:
        return list(self._events)

    def reset(self) -> None:
        self._events.clear()
```

Create `tests/test_orchestrator_relay.py`:

Test cases:
1. `test_emit_signal_line_writes_to_channel` — emit a SIGNAL line → channel mock receives it
2. `test_emit_signal_line_parses_long` — emit long SIGNAL line → `get_events()` returns 1 SIGNAL dict with correct direction/entry/stop/tp/rr
3. `test_emit_signal_line_parses_short` — emit short SIGNAL line → direction="short"
4. `test_emit_exit_tp_parses` — emit EXIT tp line → event has exit_kind="tp", correct pnl
5. `test_emit_exit_stop_parses` — emit EXIT stop line → event has exit_kind="stop"
6. `test_emit_exit_negative_pnl_parses` — `P&L -$3.00` → pnl=3.00 (abs value parsed)
7. `test_emit_non_signal_line_no_event` — plain status line → get_events() empty, still relayed
8. `test_emit_stop_move_line_no_event` — STOP_MOVE line → get_events() empty, relayed correctly
9. `test_emit_malformed_signal_no_crash` — partial SIGNAL line → no event, no exception
10. `test_reset_clears_events` — emit two events, reset(), get_events() → empty
11. `test_emit_adds_newline_if_missing` — line without `\n` → channel receives line + `\n`

---

### Task 2B — orchestrator/summarizer.py + tests
**WAVE:** 2 | **DEPENDS_ON:** Task 1B | **AGENT_ROLE:** implementer

Create `orchestrator/summarizer.py`:

```python
import datetime
import os
from pathlib import Path

import anthropic

from orchestrator.output import OutputChannel

_MODEL = "claude-sonnet-4-6"
_MAX_PREVIOUS = 5

_SYSTEM_PROMPT = """You are a trading session analyst for an SMT divergence futures strategy \
(MNQ/MES). The strategy trades one position per session, 09:00–13:30 ET.
Produce output in exactly three sections with these headers:

## METRICS
## NARRATIVE
## RECOMMENDATIONS"""

_USER_TEMPLATE = """\
Session: {date} ({weekday})
Parameters: SESSION_START=09:00 ET, SESSION_END=13:30 ET, ENTRY_SLIPPAGE_TICKS=2

=== SESSION LOG ===
{session_log}

=== PREVIOUS SESSION SUMMARIES (last {n_prev} sessions) ===
{prev_summaries}

Produce:
1. METRICS — signals fired, trade taken (y/n), exit type (tp/stop/session_end/none), \
P&L ($), theoretical R:R vs achieved, time-in-trade.
2. NARRATIVE — what happened; flag anything unusual (stop hit <2 min after entry may indicate \
slippage; no signal in a full session is notable; session_end exit means position held past close).
3. RECOMMENDATIONS — preliminary parameter review triggers based on today + recent session \
patterns. Examples: 3+ consecutive stops → review STOP_RATIO; consistent session_end exits → \
consider earlier SESSION_END. If fewer than 3 sessions of data, state that explicitly."""


class Summarizer:
    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is required")
        self._client = anthropic.Anthropic(api_key=api_key)

    def run(
        self,
        date: datetime.date,
        session_log_path: Path,
        sessions_dir: Path,
        channel: OutputChannel,
    ) -> None:
        """Generate and write post-session summary."""
        session_log = session_log_path.read_text(encoding="utf-8") if session_log_path.exists() else "(no signals logged)"
        prev = _load_previous_summaries(sessions_dir, date)
        user_msg = _USER_TEMPLATE.format(
            date=date.isoformat(),
            weekday=date.strftime("%A"),
            session_log=session_log,
            n_prev=len(prev),
            prev_summaries="\n\n---\n\n".join(prev) if prev else "(none — first session)",
        )
        response = self._client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        summary = response.content[0].text
        summary_path = sessions_dir / date.isoformat() / "summary.md"
        summary_path.write_text(summary, encoding="utf-8")
        channel.writeln("\n" + "=" * 60)
        channel.writeln(f"SESSION SUMMARY — {date.isoformat()}")
        channel.writeln("=" * 60)
        channel.write(summary + "\n")


def _load_previous_summaries(sessions_dir: Path, current_date: datetime.date) -> list[str]:
    """Load up to _MAX_PREVIOUS previous summary.md files, oldest first."""
    summaries = []
    if not sessions_dir.exists():
        return summaries
    dated_dirs = sorted(
        (d for d in sessions_dir.iterdir()
         if d.is_dir() and d.name < current_date.isoformat()),
        key=lambda d: d.name,
    )
    for d in dated_dirs[-_MAX_PREVIOUS:]:
        summary_file = d / "summary.md"
        if summary_file.exists():
            summaries.append(f"### {d.name}\n{summary_file.read_text(encoding='utf-8')}")
    return summaries
```

Create `tests/test_orchestrator_summarizer.py`:

Test cases:
1. `test_summarizer_raises_without_api_key` — no `ANTHROPIC_API_KEY` in env → `RuntimeError`
2. `test_summarizer_builds_prompt_with_session_log` — mock anthropic client; `run()` called with a signals.log → API call includes session log text in user message
3. `test_summarizer_includes_previous_summaries` — create 3 prior session dirs with summary.md; verify prompt contains all 3
4. `test_summarizer_caps_at_5_previous` — create 7 prior session dirs; verify only 5 most recent in prompt
5. `test_summarizer_no_previous_summaries` — no prior sessions; prompt contains "(none — first session)"
6. `test_summarizer_writes_summary_md` — mock API returns "## METRICS\ntest"; `run()` → `summary.md` written
7. `test_summarizer_writes_to_channel` — mock API; `run()` → OutputChannel receives summary text
8. `test_summarizer_missing_signals_log` — signals.log does not exist → prompt contains "(no signals logged)", no crash
9. `test_load_previous_summaries_sorted_oldest_first` — dirs named 2026-04-14 and 2026-04-15 → returns in that order
10. `test_load_previous_summaries_excludes_current_date` — current date dir exists with summary.md → excluded

---

### WAVE 3 — Process Manager (after Wave 2)

---

### Task 3 — orchestrator/process.py + tests
**WAVE:** 3 | **DEPENDS_ON:** Task 2A | **AGENT_ROLE:** implementer

Create `orchestrator/process.py`:

```python
import datetime
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from orchestrator.output import OutputChannel
from orchestrator.relay import SessionRelay

_ET = ZoneInfo("America/New_York")
_SESSION_GRACE_END = datetime.time(13, 35)
_SIGTERM_WAIT_S = 10
_POLL_INTERVAL_S = 0.5


class ProcessManager:
    def __init__(self, script_path: Path, relay: SessionRelay, log_channel: OutputChannel) -> None:
        self._script = script_path
        self._relay = relay
        self._log = log_channel

    def run_session(self, date: datetime.date) -> None:
        """Kill any running signal_smt.py, then spawn fresh; relay output; restart once on unexpected exit; stop at 13:35 ET."""
        _kill_existing_signal_smt(self._script, self._log)
        restarted = False
        while True:
            proc = self._spawn()
            self._log.writeln(f"[ORCH] signal_smt.py started (pid={proc.pid})")
            exit_reason = self._monitor(proc)
            if exit_reason == "scheduled_stop":
                self._log.writeln("[ORCH] Session ended — sending terminate signal")
                self._terminate(proc)
                return
            # Unexpected exit
            if not restarted:
                self._log.writeln(
                    f"[ORCH] *** signal_smt.py exited unexpectedly (code={proc.returncode}) — restarting once ***"
                )
                restarted = True
            else:
                self._log.writeln(
                    f"[ORCH] *** signal_smt.py exited again (code={proc.returncode}) — NOT restarting; waiting for session end ***"
                )
                self._wait_until_grace_end()
                return

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(self._script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

    def _monitor(self, proc: subprocess.Popen) -> str:
        """Read stdout in a thread; poll for exit or 13:35 ET in main thread.

        Returns "scheduled_stop" or "unexpected_exit".
        """
        reader = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
        reader.start()
        while True:
            if proc.poll() is not None:
                reader.join(timeout=2)
                return "unexpected_exit"
            now = datetime.datetime.now(tz=_ET)
            if now.time() >= _SESSION_GRACE_END:
                return "scheduled_stop"
            time.sleep(_POLL_INTERVAL_S)

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self._relay.emit(line.rstrip("\n"))

    def _terminate(self, proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=_SIGTERM_WAIT_S)
        except subprocess.TimeoutExpired:
            self._log.writeln("[ORCH] SIGTERM timeout — killing process")
            proc.kill()

    def _wait_until_grace_end(self) -> None:
        while datetime.datetime.now(tz=_ET).time() < _SESSION_GRACE_END:
            time.sleep(30)


def _kill_existing_signal_smt(script_path: Path, log: OutputChannel) -> None:
    """Terminate any running process whose cmdline contains signal_smt.py."""
    import psutil
    script_name = script_path.name
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(script_name in arg for arg in cmdline):
                log.writeln(f"[ORCH] Killing existing signal_smt.py (pid={proc.pid})")
                proc.terminate()
                proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
```

Add test case to `tests/test_orchestrator_process.py` for the kill-existing behavior:
- `test_kill_existing_terminates_matching_process` — mock `psutil.process_iter` returns a process whose cmdline contains "signal_smt.py"; verify `proc.terminate()` called
- `test_kill_existing_skips_non_matching_process` — mock returns a process with unrelated cmdline; verify `terminate()` NOT called

Create `tests/test_orchestrator_process.py`:

All tests mock `subprocess.Popen` and time. No real subprocesses spawned.

Test cases:
1. `test_run_session_spawns_subprocess` — verify `Popen` called with `sys.executable` and `signal_smt.py` path
2. `test_run_session_relays_stdout_lines` — mock proc stdout yields 2 lines → `relay.emit` called twice
3. `test_run_session_scheduled_stop_terminates` — mock time returns 13:36 ET; verify `proc.terminate()` called
4. `test_run_session_unexpected_exit_restarts_once` — first proc exits immediately (poll=1), second proc runs to 13:36; verify two Popen calls and log contains "restarting once"
5. `test_run_session_second_exit_no_restart` — both procs exit unexpectedly; verify only two Popen calls and log contains "NOT restarting"
6. `test_terminate_calls_kill_on_timeout` — `proc.wait()` raises `TimeoutExpired`; verify `proc.kill()` called
7. `test_terminate_graceful` — `proc.wait()` returns normally; verify `proc.kill()` NOT called

---

### WAVE 4 — Main Loop (after Wave 3)

---

### Task 4 — orchestrator/main.py + tests
**WAVE:** 4 | **DEPENDS_ON:** Task 3 | **AGENT_ROLE:** implementer

Create `orchestrator/main.py` with:

**Constants:** `_SIGNAL_SMT = Path(__file__).parent.parent / "signal_smt.py"`, `_SESSIONS_DIR`, `_SESSION_GRACE_END = time(13, 35)`

**`_make_session_channels(date) -> (signal_ch, orch_ch)`** — creates `sessions/YYYY-MM-DD/`, returns two `OutputChannel` instances each with a `StdoutSink` and `FileSink` (signals.log / orchestrator.log).

**`run(summarizer=None)`** — infinite loop:
```
while True:
    now = get_et_now()
    if not is_trading_day(now.date()):   sleep_until(next_session_open(now)); continue
    if now < session_open(09:00 ET):     sleep_until(09:00 ET today); continue
    if now >= grace_end(13:35 ET):       sleep_until(next_session_open(now)); continue
    # run session
    signal_ch, orch_ch = _make_session_channels(date)
    relay = SessionRelay(signal_ch)
    ProcessManager(SIGNAL_SMT, relay, orch_ch).run_session(date)
    summarizer.run(date, sessions/date/signals.log, SESSIONS_DIR, signal_ch)
    sleep_until(next_session_open(get_et_now()))
```

**`_sleep_until(target, label)`** — computes delay, prints `[ORCH] Sleeping Xh until ...`, calls `time.sleep(delay)`.

**`_check_setup()`** — checks `ANTHROPIC_API_KEY` in env (exit 1 if missing), creates `_SESSIONS_DIR`, constructs `Summarizer()`, prints `[ORCH] Setup OK`, exits 0.

**`__main__`** — `if "--check" in sys.argv: _check_setup()` else `run()`.

Create `tests/test_orchestrator_main.py`:

Test cases (all mock `get_et_now`, `is_trading_day`, `next_session_open`, `ProcessManager`, `Summarizer`):

1. `test_main_non_trading_day_sleeps_to_next_open` — `is_trading_day` returns False; `_sleep_until` called with next session open; `ProcessManager` NOT called
2. `test_main_before_session_open_sleeps_to_open` — trading day, `now` = 08:00 ET; `_sleep_until` called with 09:00; `ProcessManager` NOT called
3. `test_main_after_grace_end_skips_to_next_day` — trading day, `now` = 14:00 ET; `_sleep_until` called; `ProcessManager` NOT called
4. `test_main_in_session_runs_session_then_summarizes` — trading day, `now` = 09:15 ET; `ProcessManager.run_session` and `Summarizer.run` both called in order
5. `test_main_session_dirs_created` — `tmp_path` injected; session dir `sessions/YYYY-MM-DD/` created before `ProcessManager` called
6. `test_check_setup_exits_0_with_valid_key` — `ANTHROPIC_API_KEY` set; `_check_setup()` calls `sys.exit(0)` (mock exit)
7. `test_check_setup_exits_1_without_key` — no key; `_check_setup()` calls `sys.exit(1)`

Script deliverables criteria:
- "Running `python orchestrator/main.py --check` completes the setup phase without raising an exception." (runnability — requires `ANTHROPIC_API_KEY` in env)
- "All user-visible output uses ASCII-safe characters." (prices and timestamps are ASCII; the STOP_MOVE Unicode arrow `→` originates in signal_smt.py and is passed through verbatim — relay does not alter it)

---

### WAVE 5 — Integration + Cleanup (after Wave 4)

---

### Task 5 — Integration test + PROGRESS.md update
**WAVE:** 5 | **DEPENDS_ON:** Task 4 | **AGENT_ROLE:** implementer

**Integration test** (`tests/test_orchestrator_integration.py`):

Write a test that uses a real (temporary) mock script instead of mocking at the Python level.
Create a temporary `fake_signal_smt.py` that immediately prints two valid output lines then exits 0:

```python
# fake_signal_smt.py
import time, sys
print("[09:14:32] SIGNAL    long  | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x", flush=True)
time.sleep(0.05)
print("[09:47:11] EXIT      tp    | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract", flush=True)
sys.exit(0)
```

Test cases:
1. `test_integration_relay_captures_events` — run `ProcessManager` with `fake_signal_smt.py` (mock time to return 13:36 ET immediately after proc exits); verify `relay.get_events()` contains 1 SIGNAL + 1 EXIT dict; verify `signals.log` contains both lines
2. `test_integration_signals_log_written` — same setup; verify `signals.log` file exists and content matches

Mark both tests `@pytest.mark.integration` so they are deselectable.

**PROGRESS.md update:**

Add at the top of PROGRESS.md (after the `# PROGRESS` heading), inserting before the first `## Feature:`:

```markdown
## Feature: Session Orchestrator Daemon

**Status**: ✅ Complete
**Plan File**: `.agents/plans/session-orchestrator.md`

### Summary
Python daemon (`orchestrator/main.py`) that manages `signal_smt.py` lifecycle:
starts at 09:00 ET on trading days, relays SIGNAL/EXIT lines to stdout + session log,
restarts once on unexpected crash, terminates at 13:35 ET, and calls Claude API for
post-session summary with metrics, narrative, and parameter recommendations.

---
```

---

## Test Automation Summary

| Module | Test file | Cases | Tool | Run command |
|---|---|---|---|---|
| output.py | tests/test_orchestrator_output.py | 6 | pytest, capsys, tmp_path | `uv run pytest tests/test_orchestrator_output.py -v` |
| scheduler.py | tests/test_orchestrator_scheduler.py | 8 | pytest | `uv run pytest tests/test_orchestrator_scheduler.py -v` |
| relay.py | tests/test_orchestrator_relay.py | 11 | pytest, MagicMock | `uv run pytest tests/test_orchestrator_relay.py -v` |
| summarizer.py | tests/test_orchestrator_summarizer.py | 10 | pytest, MagicMock, tmp_path | `uv run pytest tests/test_orchestrator_summarizer.py -v` |
| process.py | tests/test_orchestrator_process.py | 9 | pytest, MagicMock, psutil mock | `uv run pytest tests/test_orchestrator_process.py -v` |
| main.py | tests/test_orchestrator_main.py | 7 | pytest, MagicMock | `uv run pytest tests/test_orchestrator_main.py -v` |
| integration | tests/test_orchestrator_integration.py | 2 | pytest, subprocess, tmp_path | `uv run pytest tests/test_orchestrator_integration.py -m integration -v` |

**Total new tests:** 53
**Automated:** 51 (100%) — all unit tests use mocks; integration tests use a real fake script
**Manual:** 0
**Live validation required** (not automated): running `python orchestrator/main.py --check` with a real `ANTHROPIC_API_KEY` set; running a real session (requires IB Gateway + market hours)

---

## Coverage Map

| Code path | Covered by |
|---|---|
| StdoutSink, FileSink, OutputChannel | test_orchestrator_output.py |
| is_trading_day (trading day / weekend / holiday) | test_orchestrator_scheduler.py |
| next_session_open (before / after session open, weekend) | test_orchestrator_scheduler.py |
| SessionRelay.emit — SIGNAL parse (long/short) | test_orchestrator_relay.py |
| SessionRelay.emit — EXIT parse (tp/stop, +/- P&L) | test_orchestrator_relay.py |
| SessionRelay.emit — passthrough (STOP_MOVE, unknown) | test_orchestrator_relay.py |
| SessionRelay.emit — malformed line no crash | test_orchestrator_relay.py |
| SessionRelay.reset | test_orchestrator_relay.py |
| Summarizer init — missing API key | test_orchestrator_summarizer.py |
| Summarizer.run — prompt construction | test_orchestrator_summarizer.py |
| Summarizer.run — previous summaries (0, 3, 7) | test_orchestrator_summarizer.py |
| Summarizer.run — missing signals.log | test_orchestrator_summarizer.py |
| Summarizer.run — summary.md written + channel output | test_orchestrator_summarizer.py |
| ProcessManager — spawn + stdout relay | test_orchestrator_process.py |
| ProcessManager — scheduled stop (SIGTERM) | test_orchestrator_process.py |
| ProcessManager — unexpected exit → restart once | test_orchestrator_process.py |
| ProcessManager — second exit → no restart | test_orchestrator_process.py |
| ProcessManager — SIGTERM timeout → SIGKILL | test_orchestrator_process.py |
| main.run — non-trading day sleep | test_orchestrator_main.py |
| main.run — pre-session sleep | test_orchestrator_main.py |
| main.run — post-grace-end skip | test_orchestrator_main.py |
| main.run — full session cycle | test_orchestrator_main.py |
| main.run — session dir creation | test_orchestrator_main.py |
| _check_setup — valid key | test_orchestrator_main.py |
| _check_setup — missing key | test_orchestrator_main.py |
| Integration — real subprocess stdout relay + log file | test_orchestrator_integration.py |

---

## Risks

1. **Windows signal handling** — `proc.terminate()` on Windows calls `TerminateProcess` (immediate kill, no graceful shutdown). `signal_smt.py` may not flush its state before termination. Mitigation: the 5-minute buffer (13:30→13:35) ensures position force-close fires before terminate; parquet files are written on each 1m bar so data loss is at most one minute.

2. **exchange-calendars version drift** — calendar data must be kept current; the library bundles holiday schedules that may lag real exchange changes. Mitigation: pin `>=4.5`, and verify manually on known holidays before relying on the daemon for live trading.

3. **Claude API latency at session end** — summarization call at 13:35 ET blocks the daemon from sleeping. Max 2048 output tokens should complete in <30s. Non-blocking for Phase 1 (next session is ~19h away). Mitigation: none needed yet.

4. **ANTHROPIC_API_KEY absent at summarization time** — if the key was set after the daemon started but later cleared, `Summarizer.__init__` would have already succeeded but the client would fail on the actual API call. Mitigation: `Summarizer` is constructed once at daemon startup; if key disappears mid-run, `anthropic` raises `AuthenticationError` which propagates as an error log, not a crash.

---

## Acceptance Criteria

### Functional
- [ ] `python orchestrator/main.py --check` exits 0 when `ANTHROPIC_API_KEY` is set
- [ ] `python orchestrator/main.py --check` exits 1 and prints an error when `ANTHROPIC_API_KEY` is missing
- [ ] Daemon does not start `signal_smt.py` on weekends or NYSE holidays
- [ ] Daemon sleeps until 09:00 ET when started on a trading day before market open
- [ ] Daemon skips to the next trading day if started after 13:35 ET
- [ ] Before spawning `signal_smt.py`, the daemon terminates any existing process whose cmdline contains `signal_smt.py` — preventing duplicate instances after a restart
- [ ] SIGNAL and EXIT lines are written to stdout and `signals.log` with immediate flush
- [ ] SIGNAL lines are parsed into structured dicts with keys: type, time, direction, entry, stop, tp, rr
- [ ] EXIT lines are parsed into structured dicts with keys: type, time, exit_kind, filled, pnl, contracts
- [ ] Non-SIGNAL/EXIT lines (e.g., STOP_MOVE) pass through to output without being parsed into events
- [ ] Malformed lines do not crash the relay
- [ ] On unexpected `signal_smt.py` exit before 13:35: daemon restarts once and logs "restarting once"
- [ ] On a second unexpected exit: daemon does NOT restart and logs "NOT restarting"
- [ ] At 13:35 ET: calls `terminate()` on `signal_smt.py`; if still alive after 10s, calls `kill()`
- [ ] `summary.md` after session contains `## METRICS`, `## NARRATIVE`, `## RECOMMENDATIONS` sections
- [ ] Summarizer prompt includes up to 5 most recent prior session `summary.md` files, oldest first
- [ ] If `signals.log` is absent at summarization time: prompt contains "(no signals logged)", no exception raised

### Error Handling
- [ ] `Summarizer.__init__` raises `RuntimeError` immediately if `ANTHROPIC_API_KEY` is not set — daemon fails at startup, not after a 4-hour session

### Crash Recovery
- [ ] All session files use immediate flush — data written up to a crash point is preserved
- [ ] On daemon restart within 09:00–13:35 ET: kills any running `signal_smt.py` via psutil, then starts fresh

### Session Files
- [ ] `sessions/YYYY-MM-DD/` directory created before `signal_smt.py` is spawned
- [ ] `sessions/YYYY-MM-DD/signals.log` contains all relayed lines after a session
- [ ] `sessions/YYYY-MM-DD/orchestrator.log` contains start, stop, and crash events
- [ ] `sessions/YYYY-MM-DD/summary.md` exists after session end

### Validation
- [ ] `uv run pytest tests/test_orchestrator_*.py -v` → all 53 tests pass
- [ ] `uv run pytest tests/ -x -q` → no new failures vs pre-implementation baseline

### Out of Scope
- IB Gateway auto-start or lifecycle management
- Auto-recovery of a missed `summary.md` after a mid-session crash (re-run manually)
- External channel forwarding (Telegram, Discord, etc.)
- Automatic order placement
- VPS deployment or systemd unit file
- MES1! as the traded instrument
