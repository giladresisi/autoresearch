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

| Milestone | Detected from | Notes |
|-----------|--------------|-------|
| Gap-fill complete | `orchestrator_stdout.log` | Printed before session channels exist, so stdout capture is essential |
| Session started | `sessions/{date}/orchestrator.log` | Line: `automation.main started` |
| daily.py complete | `sessions/{date}/signals.log` | Line: `[EMIT] daily complete` |
| First directed hypothesis | `sessions/{date}/events.jsonl` | `new-hypothesis` event with `direction` = `up` or `down` |
| Startup fatal | `orchestrator_stdout.log` | `FATAL` in log means IB unreachable or other hard failure; monitor exits immediately |
| Orchestrator died | PID snapshotted at startup + `tasklist.exe` | Checked every iteration from startup — covers both pre-session death and mid-session death |

## Step 1 — Kill any existing orchestrator and automation.main

```powershell
$base = "C:\Users\gilad\projects\auto-co-trader\live"
$orchPid = (Get-Content "$base\orchestrator.pid" -Raw -ErrorAction SilentlyContinue).Trim()
if ($orchPid) {
    Stop-Process -Id $orchPid -Force -ErrorAction SilentlyContinue
    Write-Output "Killed orchestrator pid=$orchPid"
}
# Also kill any lingering stdout-capture wrapper from a previous run
Get-Process -Name powershell -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID } | ForEach-Object {
    $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
    if ($cmdLine -like "*orchestrator.main*") {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Write-Output "Killed wrapper pid=$($_.Id)"
    }
}
# Kill any lingering automation.main subprocess (survives orchestrator crashes)
uv run python -c "
import psutil
for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        if proc.info.get('name', '').lower() not in ('python.exe', 'python'):
            continue
        cmdline = proc.info.get('cmdline') or []
        if any('automation.main' in arg for arg in cmdline):
            proc.terminate()
            try: proc.wait(timeout=5)
            except psutil.TimeoutExpired: proc.kill()
            print(f'Killed automation.main pid={proc.pid}')
    except Exception as e:
        print(f'Warning: {e}')
" 2>$null
```

## Step 2 — Append restart marker and start orchestrator

Do NOT clear `orchestrator_stdout.log` — preserving history is essential for diagnosing
crashes where the process exits before producing any output. Instead, append a `=== RESTART`
separator so the monitor can locate the current run's output by offset.

The stdout redirect captures pre-relay messages (gap-fill, IB probe errors) that are
lost when using bare `-WindowStyle Hidden` without redirect.

```powershell
$base = "C:\Users\gilad\projects\auto-co-trader\live"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "$base\orchestrator_stdout.log" -Value "=== RESTART $timestamp ===" -Encoding utf8

Start-Process -FilePath "powershell" `
    -ArgumentList "-NoProfile", "-NonInteractive", "-Command", `
        "Set-Location '$base'; uv run python -m orchestrator.main --no-summary 2>&1 | Out-File -FilePath '$base\orchestrator_stdout.log' -Encoding utf8 -Append" `
    -WindowStyle Hidden

Start-Sleep -Seconds 3

$orchPid = (Get-Content "$base\orchestrator.pid" -Raw -ErrorAction SilentlyContinue).Trim()
if ($orchPid) {
    Write-Output "Orchestrator started pid=$orchPid"
} else {
    Write-Output "WARNING: orchestrator.pid not written — check orchestrator_stdout.log for errors"
}
```

## Step 3 — Arm the persistent Monitor

Use the `Monitor` tool with `persistent: true` and `timeout_ms: 3600000`.

The ET date is computed via Python ZoneInfo to match how the orchestrator names its
session directory — the Windows system clock timezone may differ.

**Critical**: use `tasklist.exe` for process liveness checks. `kill -0` gives false
negatives for Windows processes when called from Git Bash.

**Race-condition fix**: milestones are checked immediately at startup (before the loop)
so events that fired between the state-check and the monitor arm are never missed.
Offsets are set AFTER the immediate check so the loop only watches for truly new lines.

```bash
BASE="/c/Users/gilad/projects/auto-co-trader/live"
TODAY=$(uv run python -c "
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(tz=ZoneInfo('America/New_York')).date())
")
SESSION_DIR="$BASE/sessions/$TODAY"
STARTUP_LOG="$BASE/orchestrator_stdout.log"
PID_FILE="$BASE/orchestrator.pid"
ORCH_LOG="$SESSION_DIR/orchestrator.log"

gap_fill_done=false
session_started=false
daily_done=false
hyp_done=false

# Snapshot PID before the loop — pid file may be deleted on clean exit
ORCH_PID=$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null)

is_alive() {
    tasklist.exe //FI "PID eq $1" //NH 2>/dev/null | grep -qi "python"
}

# Find where the current run starts in the startup log (after last RESTART marker).
# This prevents gap-fill detection from matching output from a previous run when the
# log is preserved across restarts.
startup_log_offset=0
if [ -f "$STARTUP_LOG" ]; then
    last_restart=$(grep -n "=== RESTART" "$STARTUP_LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    [ -n "$last_restart" ] && startup_log_offset=$last_restart
fi

# --- Immediate catch-up check (before setting offsets) ---
# Fires notifications for any milestone that already occurred before this monitor armed.
# This prevents the race where events fire between the caller's state-check and here.

if tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" 2>/dev/null | grep -q "IB 1m gap fill complete"; then
    gap_fill_done=true
    echo "[MONITOR] Gap-fill complete"
fi

if tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" 2>/dev/null | grep -q "FATAL"; then
    fatal_msg=$(tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" | grep "FATAL" | head -1)
    echo "[KEEPALIVE] Orchestrator startup FATAL: $fatal_msg"
    exit 0
fi

if [ -f "$ORCH_LOG" ] && grep -q "automation.main started" "$ORCH_LOG" 2>/dev/null; then
    session_started=true
    echo "[MONITOR] Session started — automation.main is running"
fi

if [ "$session_started" = true ]; then
    if [ -f "$SESSION_DIR/signals.log" ] && grep -q "daily complete" "$SESSION_DIR/signals.log" 2>/dev/null; then
        daily_done=true
        echo "[MONITOR] daily.py complete"
    fi

    if [ -f "$SESSION_DIR/events.jsonl" ]; then
        hyp_line=$(grep '"kind": "new-hypothesis"' "$SESSION_DIR/events.jsonl" 2>/dev/null \
            | grep -E '"direction": "(up|down)"' | head -1)
        if [ -n "$hyp_line" ]; then
            hyp_done=true
            dir_val=$(echo "$hyp_line" | grep -oE '"direction": "[^"]+"')
            echo "[MONITOR] First directed hypothesis: $dir_val"
        fi
    fi
fi

# Now snapshot offsets — loop only watches lines written after this point
sig_offset=0; evt_offset=0; orch_offset=0
[ -f "$SESSION_DIR/signals.log"  ] && sig_offset=$(wc -l  < "$SESSION_DIR/signals.log"  2>/dev/null || echo 0)
[ -f "$SESSION_DIR/events.jsonl" ] && evt_offset=$(wc -l  < "$SESSION_DIR/events.jsonl" 2>/dev/null || echo 0)
[ -f "$ORCH_LOG"                 ] && orch_offset=$(wc -l < "$ORCH_LOG"                 2>/dev/null || echo 0)

while true; do
    sleep 5

    # Gap-fill — also detect FATAL startup errors (IB unreachable, etc.)
    if [ "$gap_fill_done" = false ] && [ -f "$STARTUP_LOG" ]; then
        if tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" 2>/dev/null | grep -q "FATAL"; then
            fatal_msg=$(tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" | grep "FATAL" | head -1)
            echo "[KEEPALIVE] Orchestrator startup FATAL: $fatal_msg"
            exit 0
        fi
        if tail -n "+$((startup_log_offset+1))" "$STARTUP_LOG" 2>/dev/null | grep -q "IB 1m gap fill complete"; then
            gap_fill_done=true
            echo "[MONITOR] Gap-fill complete"
        fi
    fi

    # Session started
    if [ "$session_started" = false ] && [ -f "$ORCH_LOG" ]; then
        if tail -n "+$((orch_offset+1))" "$ORCH_LOG" 2>/dev/null | grep -q "automation.main started"; then
            session_started=true
            echo "[MONITOR] Session started — automation.main is running"
        fi
    fi

    # Keepalive — always active from startup, not just after session begins
    if [ -n "$ORCH_PID" ] && ! is_alive "$ORCH_PID"; then
        if [ "$session_started" = true ]; then
            echo "[KEEPALIVE] Orchestrator (pid=$ORCH_PID) has DIED"
        else
            echo "[KEEPALIVE] Orchestrator (pid=$ORCH_PID) died before session start — check stdout log"
        fi
        exit 0
    fi

    # Downstream milestones only once session is active
    if [ "$session_started" = true ]; then
        SIGNALS_LOG="$SESSION_DIR/signals.log"
        if [ "$daily_done" = false ] && [ -f "$SIGNALS_LOG" ]; then
            if tail -n "+$((sig_offset+1))" "$SIGNALS_LOG" 2>/dev/null | grep -q "daily complete"; then
                daily_done=true
                echo "[MONITOR] daily.py complete"
            fi
        fi

        EVENTS_LOG="$SESSION_DIR/events.jsonl"
        if [ "$hyp_done" = false ] && [ -f "$EVENTS_LOG" ]; then
            hyp_line=$(tail -n "+$((evt_offset+1))" "$EVENTS_LOG" 2>/dev/null \
                | grep '"kind": "new-hypothesis"' \
                | grep -E '"direction": "(up|down)"' \
                | head -1)
            if [ -n "$hyp_line" ]; then
                hyp_done=true
                dir_val=$(echo "$hyp_line" | grep -oE '"direction": "[^"]+"')
                echo "[MONITOR] First directed hypothesis: $dir_val"
            fi
        fi
    fi
done
```

## Step 4 — Push a notification for each Monitor event

As each line arrives from the Monitor, call `PushNotification`:

| Monitor output | Push message |
|----------------|--------------|
| `[MONITOR] Gap-fill complete` | `Gap-fill complete — pre-session data ready` |
| `[MONITOR] Session started …` | `Session started — automation.main running` |
| `[MONITOR] daily.py complete` | `daily.py complete — liquidities computed` |
| `[MONITOR] First directed hypothesis: …` | `First hypothesis: <direction> — strategy is live` |
| `[KEEPALIVE] Orchestrator startup FATAL: …` | `CRITICAL: Orchestrator failed at startup — <first line of FATAL message>` |
| `[KEEPALIVE] Orchestrator … died before session start …` | `CRITICAL: Orchestrator died before session start — check stdout log` |
| `[KEEPALIVE] Orchestrator … has DIED` | `CRITICAL: Orchestrator died during session` |
