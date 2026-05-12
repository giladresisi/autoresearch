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
| Orchestrator died | `orchestrator.pid` + `tasklist.exe` | Only checked after session starts — orchestrator sleeps for hours before the session window and that is normal |

## Step 1 — Kill any existing orchestrator

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
```

## Step 2 — Clear old startup log and start orchestrator

Clearing `orchestrator_stdout.log` before starting is important: gap-fill detection
searches that file for `IB 1m gap fill complete`. Without a clear, a stale file from a
previous run would trigger a false-positive immediately.

The stdout redirect captures pre-relay messages (gap-fill, IB probe errors) that are
lost when using bare `-WindowStyle Hidden` without redirect.

```powershell
$base = "C:\Users\gilad\projects\auto-co-trader\live"
Remove-Item "$base\orchestrator_stdout.log" -ErrorAction SilentlyContinue

Start-Process -FilePath "powershell" `
    -ArgumentList "-NoProfile", "-NonInteractive", "-Command", `
        "Set-Location '$base'; uv run python -m orchestrator.main 2>&1 | Out-File -FilePath '$base\orchestrator_stdout.log' -Encoding utf8 -Append" `
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

The script computes log offsets at startup (before the loop) so it only watches lines
written after this restart — important because `signals.log` and `events.jsonl` accumulate
across the entire trading day and may already contain events from earlier runs.

The ET date is computed via Python ZoneInfo to match how the orchestrator names its
session directory — the Windows system clock timezone may differ.

**Critical**: use `tasklist.exe` for process liveness checks. `kill -0` gives false
negatives for Windows processes when called from Git Bash.

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

# Snapshot line counts before the loop — only watch NEW content from this point on
sig_offset=0; evt_offset=0; orch_offset=0
[ -f "$SESSION_DIR/signals.log"      ] && sig_offset=$(wc -l  < "$SESSION_DIR/signals.log"      2>/dev/null || echo 0)
[ -f "$SESSION_DIR/events.jsonl"     ] && evt_offset=$(wc -l  < "$SESSION_DIR/events.jsonl"     2>/dev/null || echo 0)
[ -f "$ORCH_LOG"                     ] && orch_offset=$(wc -l < "$ORCH_LOG"                     2>/dev/null || echo 0)

is_alive() {
    tasklist.exe //FI "PID eq $1" //NH 2>/dev/null | grep -qi "python"
}

while true; do
    sleep 5

    # Gap-fill
    if [ "$gap_fill_done" = false ] && [ -f "$STARTUP_LOG" ]; then
        if grep -q "IB 1m gap fill complete" "$STARTUP_LOG" 2>/dev/null; then
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

    # Keepalive + downstream milestones only once session is active
    if [ "$session_started" = true ]; then
        ORCH_PID=$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null)
        if [ -n "$ORCH_PID" ] && ! is_alive "$ORCH_PID"; then
            echo "[KEEPALIVE] Orchestrator (pid=$ORCH_PID) has DIED"
            exit 0
        fi

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
| `[KEEPALIVE] Orchestrator … has DIED` | `CRITICAL: Orchestrator died during session` |
