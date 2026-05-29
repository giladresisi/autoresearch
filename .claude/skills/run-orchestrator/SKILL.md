---
name: run-orchestrator
description: >
  Use when the user wants to start, run, launch, restart, or revive the auto-co-trader
  orchestrator or trading system — including after a crash. Handles the full startup sequence:
  kills any existing instance, launches the orchestrator with stdout capture, then arms a
  persistent monitor that sends push notifications at each session milestone (gap-fill complete,
  session started, daily.py done, first directed hypothesis, orchestrator death). Trigger phrases
  include "start the orchestrator", "run the orchestrator", "kick it off", "bring it back up",
  "restart after crash", or any request to get the trading system running. Never touches
  session_times.py — session window is whatever is already configured there.
---

# Run Orchestrator

Starts the orchestrator as a hidden background process with stdout captured, then arms a
persistent Monitor that pushes a notification for each trading session milestone.

## Milestones and how they're detected

All milestones are detected from `orchestrator_stdout.log` using a line offset anchored to
the current run's `=== RESTART` marker. This prevents false-positives from stale session
files left over by a previous run on the same calendar day.

| Milestone | Pattern in `orchestrator_stdout.log` | Notes |
|-----------|--------------------------------------|-------|
| Gap-fill complete | `IB 1m gap fill complete` | Printed before session channels exist |
| Session started | `automation.main started` | Printed by orchestrator when it spawns the session process |
| daily.py complete | `[EMIT] daily complete` | Printed by automation.main after run_daily |
| First directed hypothesis | `"kind": "new-hypothesis"` + `"direction": "up\|down"` | JSON line emitted by automation.main |
| Startup fatal | `FATAL` | IB unreachable or other hard failure; monitor exits immediately |
| IB zombie suspected | `[ib-watchdog] No data for` | Bar feed silent; 30s recovery window open |
| IB watchdog killed | `[ib-watchdog] No recovery after` | Watchdog killed connection as zombie; orchestrator will restart |
| Orchestrator died | PID snapshotted at startup + `powershell Get-Process` | Checked every iteration from startup |
| automation.main died | PID parsed from `[ORCH] automation.main started (pid=…)` + `powershell Get-Process` | Checked every iteration after session start |

## Step 1 — Start the orchestrator via trade.py

`trade.py start` handles everything: kills any existing orchestrator and automation.main,
appends the `=== RESTART` marker to `orchestrator_stdout.log`, launches the process with
stdout/stderr captured to the log files, and confirms the PID.

Choose flags based on the user's request:

| User intent | Command |
|---|---|
| Fresh start (default) | `uv run python trade.py start` |
| Already running, restart without prompt | `uv run python trade.py start --force` |
| Keep position.json as-is | `uv run python trade.py start --resume` |
| Restart without prompt, keep position | `uv run python trade.py start --force --resume` |
| Enable LLM session summary | add `--summary` |

```powershell
uv run python trade.py start --force   # adjust flags per user request
```

After the start command succeeds, report the session window status:

```powershell
$tmp = "$env:TEMP\session_status.py"; @'
from session_times import SESSION_OPEN, SESSION_CLOSE
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now = datetime.now(tz=ZoneInfo("America/New_York"))
t = now.time()
# Overnight session: SESSION_OPEN (18:00) > SESSION_CLOSE (17:00)
# Active when t >= SESSION_OPEN (evening) or t < SESSION_CLOSE (post-midnight)
if t >= SESSION_OPEN or t < SESSION_CLOSE:
    if t < SESSION_CLOSE:
        close_dt = now.replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute, second=0, microsecond=0)
    else:
        close_dt = (now + timedelta(days=1)).replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute, second=0, microsecond=0)
    diff = close_dt - now
    h, m = divmod(int(diff.total_seconds()) // 60, 60)
    print("Session window ACTIVE - closes at %s ET (in %dh %dm)" % (SESSION_CLOSE.strftime("%H:%M"), h, m))
else:
    open_dt = now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    if now >= open_dt:
        open_dt += timedelta(days=1)
    diff = open_dt - now
    h, m = divmod(int(diff.total_seconds()) // 60, 60)
    print("Pre-session - opens at %s ET (in %dh %dm)" % (SESSION_OPEN.strftime("%H:%M"), h, m))
'@ | Out-File -FilePath $tmp -Encoding ascii; uv run python $tmp; Remove-Item $tmp
```

## Step 2 — Arm the persistent Monitor

Use the `Monitor` tool with `persistent: true` and `timeout_ms: 3600000`.

**All milestones are detected from `orchestrator_stdout.log` only**, using a line offset
anchored to the last `=== RESTART` marker. This guarantees that session files left over
from an earlier run on the same calendar day never trigger false-positive notifications.

**Critical**: use `powershell.exe Get-Process` for process liveness checks. Both `kill -0`
and `tasklist.exe //FI "PID eq …"` give unreliable results for Windows processes when called
from Git Bash — `tasklist.exe` can return exit code 1 even when the process is alive, causing
false "died" notifications. `powershell.exe Get-Process -Id <pid> -ErrorAction SilentlyContinue`
is the only reliable method.

**Race-condition fix**: milestones are checked immediately at startup (before the loop)
so events that fired between the state-check and the monitor arm are never missed.

```bash
BASE="$(pwd)"
STARTUP_LOG="$BASE/orchestrator_stdout.log"
PID_FILE="$BASE/orchestrator.pid"

gap_fill_done=false
fill_incomplete_reported=false
gap_check_reported=false
session_started=false
session_ended=false
daily_done=false
hyp_done=false
ib_zombie_suspected=false
ib_watchdog_killed=false

# Snapshot PID before the loop — pid file may be deleted on clean exit
ORCH_PID=$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null)
AUTO_PID=""
last_reported_dead_auto_pid=""

# Returns 0 if the process with the given PID is alive, 1 if dead.
# Uses powershell.exe Get-Process — the only reliable liveness check for Windows
# processes from Git Bash. tasklist.exe and kill -0 both give false negatives.
# Works for any process name (python.exe, uv.exe, etc.).
is_alive() {
    powershell.exe -NoProfile -Command "if (Get-Process -Id $1 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
}

# Find where the current run starts in the stdout log (after last RESTART marker).
# All milestone detection uses this offset so stale output from prior runs is ignored.
startup_log_offset=0
if [ -f "$STARTUP_LOG" ]; then
    last_restart=$(grep -n "=== RESTART" "$STARTUP_LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    [ -n "$last_restart" ] && startup_log_offset=$last_restart
fi

# Helper: current run's stdout lines only
cur() { tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" 2>/dev/null; }

# --- Immediate catch-up check (before the loop) ---
# Fires notifications for any milestone that already completed before this monitor armed.

if cur | grep -q "IB 1m gap fill complete"; then
    gap_fill_done=true
    echo "[MONITOR] Gap-fill complete"
fi

if cur | grep -q "\[gap_fill_1m_ib\] WARN:"; then
    fill_incomplete_reported=true
    warn_msg=$(cur | grep "\[gap_fill_1m_ib\] WARN:" | tr '\n' ' ')
    echo "[MONITOR] Gap-fill incomplete: $warn_msg"
fi

if cur | grep -q "\[gap_check\] WARN:"; then
    gap_check_reported=true
    warn_msg=$(cur | grep "\[gap_check\] WARN:" | tr '\n' ' ')
    echo "[MONITOR] Parquet gap detected: $warn_msg"
fi

if cur | grep -q "FATAL"; then
    fatal_msg=$(cur | grep "FATAL" | head -1)
    echo "[KEEPALIVE] Orchestrator startup FATAL: $fatal_msg"
    exit 0
fi

if cur | grep -q "\[ib-watchdog\] No data for"; then
    ib_zombie_suspected=true
    echo "[MONITOR] IB watchdog: zombie suspected — no bar data received"
fi

if cur | grep -q "\[ib-watchdog\] No recovery after"; then
    ib_watchdog_killed=true
    echo "[KEEPALIVE] IB watchdog: connection killed as zombie — orchestrator restarting"
fi

if cur | grep -q "automation.main started"; then
    session_started=true
    AUTO_PID=$(cur | grep "automation.main started" | tail -1 | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+')
    echo "[MONITOR] Session started — automation.main is running (pid=$AUTO_PID)"
fi

if [ "$session_started" = true ]; then
    if cur | grep -qF "[ORCH] Session ended"; then
        session_ended=true
    fi

    if cur | grep -qF "[EMIT] daily complete"; then
        daily_done=true
        echo "[MONITOR] daily.py complete"
    fi

    hyp_line=$(cur | grep '"kind": "new-hypothesis"' | grep -E '"direction": "(up|down)"' | head -1)
    if [ -n "$hyp_line" ]; then
        hyp_done=true
        dir_val=$(echo "$hyp_line" | grep -oE '"direction": "[^"]+"')
        echo "[MONITOR] First directed hypothesis: $dir_val"
    fi
fi

while true; do
    sleep 5

    # IB watchdog events — checked every iteration regardless of session state
    if [ "$ib_zombie_suspected" = false ] && cur | grep -q "\[ib-watchdog\] No data for"; then
        ib_zombie_suspected=true
        echo "[MONITOR] IB watchdog: zombie suspected — no bar data received"
    fi
    if [ "$ib_watchdog_killed" = false ] && cur | grep -q "\[ib-watchdog\] No recovery after"; then
        ib_watchdog_killed=true
        echo "[KEEPALIVE] IB watchdog: connection killed as zombie — orchestrator restarting"
    fi

    # Gap-fill and FATAL — both detected from stdout with offset
    if [ "$gap_fill_done" = false ]; then
        if cur | grep -q "FATAL"; then
            fatal_msg=$(cur | grep "FATAL" | head -1)
            echo "[KEEPALIVE] Orchestrator startup FATAL: $fatal_msg"
            exit 0
        fi
        if cur | grep -q "IB 1m gap fill complete"; then
            gap_fill_done=true
            echo "[MONITOR] Gap-fill complete"
        fi
        if [ "$fill_incomplete_reported" = false ] && cur | grep -q "\[gap_fill_1m_ib\] WARN:"; then
            fill_incomplete_reported=true
            warn_msg=$(cur | grep "\[gap_fill_1m_ib\] WARN:" | tr '\n' ' ')
            echo "[MONITOR] Gap-fill incomplete: $warn_msg"
        fi
        if [ "$gap_check_reported" = false ] && cur | grep -q "\[gap_check\] WARN:"; then
            gap_check_reported=true
            warn_msg=$(cur | grep "\[gap_check\] WARN:" | tr '\n' ' ')
            echo "[MONITOR] Parquet gap detected: $warn_msg"
        fi
    fi

    # Session started — detected from stdout with offset
    if [ "$session_started" = false ]; then
        if cur | grep -q "automation.main started"; then
            session_started=true
            AUTO_PID=$(cur | grep "automation.main started" | tail -1 | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+')
            echo "[MONITOR] Session started — automation.main is running (pid=$AUTO_PID)"
        fi
    fi

    # Session ended — suppress automation.main keepalive once orchestrator closes it cleanly
    if [ "$session_started" = true ] && [ "$session_ended" = false ]; then
        if cur | grep -qF "[ORCH] Session ended"; then
            session_ended=true
            echo "[MONITOR] Session ended — automation.main shutdown was intentional"
        fi
    fi

    # Keepalive — orchestrator: always active from startup
    if [ -n "$ORCH_PID" ] && ! is_alive "$ORCH_PID"; then
        if [ "$session_started" = true ]; then
            echo "[KEEPALIVE] Orchestrator (pid=$ORCH_PID) has DIED"
        else
            echo "[KEEPALIVE] Orchestrator (pid=$ORCH_PID) died before session start — check stdout log"
        fi
        exit 0
    fi

    # Track AUTO_PID updates — orchestrator may restart automation.main with a new PID
    if [ "$session_started" = true ]; then
        latest_auto_pid=$(cur | grep "automation.main started" | tail -1 | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+')
        if [ -n "$latest_auto_pid" ] && [ "$latest_auto_pid" != "$AUTO_PID" ]; then
            AUTO_PID=$latest_auto_pid
            echo "[MONITOR] automation.main restarted by orchestrator (pid=$AUTO_PID)"
        fi
    fi

    # Keepalive — automation.main: only while session is active (not after clean session-end)
    # De-duplicate: only report each dead PID once (orchestrator restarts produce a new PID)
    if [ "$session_started" = true ] && [ "$session_ended" = false ] && [ -n "$AUTO_PID" ] && ! is_alive "$AUTO_PID"; then
        if [ "$AUTO_PID" != "$last_reported_dead_auto_pid" ]; then
            last_reported_dead_auto_pid=$AUTO_PID
            echo "[KEEPALIVE] automation.main (pid=$AUTO_PID) has DIED — orchestrator should restart it"
        fi
    fi

    # Downstream milestones — detected from stdout with offset
    if [ "$session_started" = true ]; then
        if [ "$daily_done" = false ]; then
            if cur | grep -qF "[EMIT] daily complete"; then
                daily_done=true
                echo "[MONITOR] daily.py complete"
            fi
        fi

        if [ "$hyp_done" = false ]; then
            hyp_line=$(cur | grep '"kind": "new-hypothesis"' | grep -E '"direction": "(up|down)"' | head -1)
            if [ -n "$hyp_line" ]; then
                hyp_done=true
                dir_val=$(echo "$hyp_line" | grep -oE '"direction": "[^"]+"')
                echo "[MONITOR] First directed hypothesis: $dir_val"
            fi
        fi
    fi
done
```

## Step 3 — Push a notification for each Monitor event

As each line arrives from the Monitor, call `PushNotification` for EVERY milestone:

| Monitor output | Push message |
|----------------|--------------|
| `[MONITOR] Gap-fill complete` | `Gap-fill complete — pre-session data ready` |
| `[MONITOR] Gap-fill incomplete: …` | `WARNING: Gap-fill incomplete — <coverage details from message>` |
| `[MONITOR] Parquet gap detected: …` | `WARNING: Parquet gap(s) found — <details from message>` |
| `[MONITOR] Session started …` | `Session started — automation.main running` |
| `[MONITOR] daily.py complete` | `daily.py complete — liquidities computed` |
| `[MONITOR] First directed hypothesis: …` | `First hypothesis: <direction> — strategy is live` |
| `[MONITOR] automation.main restarted by orchestrator …` | `automation.main restarted (pid=…) — session resuming` |
| `[MONITOR] IB watchdog: zombie suspected …` | `WARNING: IB zombie suspected — no bar data; 30s recovery window open` |
| `[KEEPALIVE] IB watchdog: connection killed …` | `WARNING: IB watchdog killed zombie connection — orchestrator restarting` |
| `[KEEPALIVE] Orchestrator startup FATAL: …` | `CRITICAL: Orchestrator failed at startup — <first line of FATAL message>` |
| `[KEEPALIVE] Orchestrator … died before session start …` | `CRITICAL: Orchestrator died before session start — check stdout log` |
| `[KEEPALIVE] Orchestrator … has DIED` | `CRITICAL: Orchestrator died during session` |
| `[KEEPALIVE] automation.main … has DIED` | `WARNING: automation.main died — orchestrator should restart it` |
