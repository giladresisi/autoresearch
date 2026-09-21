---
name: run-orchestrator
description: >
  Use when the user wants to start, run, launch, restart, or revive the auto-co-trader
  orchestrator or trading system — including after a crash. Handles the full startup sequence:
  kills any existing instance, launches the orchestrator with stdout capture, then arms a
  persistent monitor that sends push notifications at each session milestone (gap-fill complete,
  session started, daily.py done, first directed hypothesis, orchestrator death). At the CME
  17:00–18:00 ET maintenance break it runs a post-session cycle (parquet-check, session-analysis,
  pull+rebase onto master) and auto-restarts the orchestrator for the next session once IB
  realtime data is confirmed live again at the reopen. Trigger phrases include "start the
  orchestrator", "run the orchestrator", "kick it off", "bring it back up", "restart after crash",
  or any request to get the trading system running. Does not edit session_times.py at runtime —
  the session window is whatever is configured there.
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
| **Gap-fill NOT done by 09:15 ET** | clock check, not a log line | **WARNING.** The gap-fill BLOCKS the bar feed (`IbRealtimeSource.start`). Since 2026-09-21 the arm has a 7-minute grace window (`analyzer.LATE_ARM_GRACE_MIN`), so a fill finishing by **09:27** still takes the thesis — 09:27 is the last minute that leaves the plan time to derive on the next bar close before entries open at 09:30:30, at the worst recorded call latency. A fill still running at 09:27 IS a dark day: push CRITICAL then |
| **Late arm taken** | `[AGENT-LIVE] LATE ARM` | The gap-fill overran 09:20 and the thesis was taken inside the grace window instead. Not an error — but it means the fill is running close to the wire; tell the user to start earlier tomorrow |
| Agent owns the dispatcher | `[AGENT-LIVE] OK` | Positive confirmation that the agent took the dispatcher and the legacy engine is dark. Its ABSENCE is not proof of failure (it prints once, early) but its presence is proof of success |
| L1 thesis armed | `thesis_state.json` appears in `<global>/sessions/<date>/` | The 09:20 model call landed (16–104 s, median ~40 s). A DARK day writes no such file — report which |
| Plan derived | `plans.json` appears in the same folder | The Planner turned the thesis into a plan; entries become possible after 09:30:30 |
| First agent order | `"source": "agent"` in a signal line, then `[PMT] Order … sent OK` | The first real order of the session. Push it — this is the one the user wants to see |
| Startup fatal | `FATAL` | IB unreachable or other hard failure; monitor exits immediately |
| Agent REFUSED the dispatcher | `[AGENT-LIVE] REFUSED` | A config precondition failed (`FORCE_RESET`, `SMT_PIPELINE` != v2, `ACT_AI_MODE=primary`, or the trader could not be built). **NOTHING will trade all session** and there is no fallback — push immediately and stop |
| IB zombie suspected | `[ib-watchdog] No data for` | Bar feed silent; 30s recovery window open. **Suppressed after `[ORCH] Session ended`** — expected during the maintenance break |
| IB watchdog killed | `[ib-watchdog] No recovery after` | Watchdog killed connection as zombie. **Suppressed after session end** — expected during maintenance |
| Maintenance break started | `Pre-session IB connection ended`, OR orchestrator exit after `[ORCH] Session ended` | Clean shutdown for the CME 17:00–18:00 ET break — NOT a crash. Triggers the post-session cycle (Step 4); monitor exits |
| Orchestrator died | PID snapshotted at startup + `powershell Get-Process` | Checked every iteration from startup. After session end → reported as maintenance, not a death |
| automation.main died | PID parsed from `[ORCH] automation.main started (pid=…)` + `powershell Get-Process` | Checked every iteration after session start |

## Step 1 — Start the orchestrator via trade.py

`trade.py start` handles everything: kills any existing orchestrator and automation.main,
appends the `=== RESTART` marker to `orchestrator_stdout.log`, launches the process with
stdout/stderr captured to the log files, and confirms the PID.

**Start at ~09:00 ET (the operator's routine since 2026-09-21).** That leaves ~20 minutes
for the blocking IB gap-fill before the 09:20 arm. The arm is an EXACT-MINUTE test, so the
bar feed MUST be live before 09:20 or the Analyzer never arms and the session is dark. A
weekend or multi-day gap needs several IB pacing rounds ~11 minutes apart — on a Monday, or
after any long outage, start earlier or run `trade.py gap-fill` beforehand so the
orchestrator's own fill has little left to do.

**Default to resumed mode (automatic entries enabled).** Start with `--resume` unless the
user explicitly asks to start paused — only then use `--pause` instead. `--resume` lifts the
manual entry pause so automatic entries fire; `--pause` suppresses new automatic entries
(exits stay active). **NEVER pass `--force` while the agent brain owns the dispatcher (the default).** It sets
`FORCE_RESET`, which wipes `position.json` at session start — harmless for the legacy brain,
but plan 38's agent uses that file as its single witness: it ACKs every order by reading it and
a per-bar watchdog treats any change it did not cause as a reason to stop. So the agent REFUSES
the dispatcher when `FORCE_RESET` is set, and the session runs with NOTHING trading. `trade.py`
now refuses the combination up front (exit 1), so this is a wrong command rather than a dark
day — but do not reach for `--force` as a habit. It is valid only with `ACT_TRADER=0`.

`--force` restarts without a prompt and resets hypothesis/position state
at session start.

Choose flags based on the user's request:

| User intent | Command |
|---|---|
| Default start (resumed — automatic entries enabled) | `uv run python trade.py start --resume` |
| Start PAUSED (only when explicitly requested) | `uv run python trade.py start --pause` |
| Force-reset state (LEGACY brain only, `ACT_TRADER=0`) | add `--force` |
| Enable LLM session summary | add `--summary` |

```powershell
uv run python trade.py start --resume   # default: resumed. Use --pause only if explicitly requested.
```

### Record the running commit (once, at session start)

Immediately after the start command succeeds, record the running commit into the
current session's `comments.md` so post-session analysis knows which code version
produced the session. The session folder is `paths.sessions_dir()/<date>/` (the
machine-global sessions root, env `ACT_GLOBAL_DIR`). Do this exactly once at startup:

```powershell
uv run python -c "import paths; from scripts.commit_note import write_commit_note; from session_times import session_date_str; write_commit_note(paths.sessions_dir() / session_date_str() / 'comments.md')"
```

`write_commit_note` derives the short SHA, subject, and dirty flag from git and appends
a single `- Running commit: <short-sha> \"<subject>\" (dirty)` line (it never overwrites
existing comments and never raises). Skip it on a `--resume` restart of an already-open
session if the note is already present for the day.

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
# PID file now lives in the shared general live folder (paths.general_live_dir()),
# alongside global.json / the pause sentinel — resolve it via paths.py.
PID_FILE="$(uv run python -c "import paths; print(paths.general_live_dir() / 'orchestrator.pid')" 2>/dev/null)"

# The session folder the agent writes its thesis and plan into.
SESSION="$(uv run python -c "import paths; from session_times import session_date_str; print(paths.sessions_dir() / session_date_str())" 2>/dev/null)"

gap_fill_done=false
gap_deadline_reported=false
agent_ok=false
agent_refused=false
late_arm=false
thesis_done=false
plan_done=false
order_done=false
fill_incomplete_reported=false
gap_check_reported=false
session_started=false
session_ended=false
daily_done=false
ib_zombie_suspected=false
ib_watchdog_killed=false
maint_done=false

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

# True once the orchestrator has logged a clean session close. After this point the IB
# feed dropping out / the orchestrator exiting is the EXPECTED CME maintenance-break
# shutdown, not a crash — used to suppress zombie/death alarms and to recognise the break.
session_is_over() { cur | grep -qF "[ORCH] Session ended"; }

# Maintenance-break trigger. After the clean session close the orchestrator runs
# pre-session accumulate, then IB Gateway closes the feed at the 17:00 ET maintenance
# break and the orchestrator exits cleanly ("Pre-session IB connection ended ... expected
# during the 17:00-18:00 ET maintenance break"). Emit ONE maintenance event and exit so
# the agent runs the Step-4 post-session cycle. NOT a crash.
check_maintenance() {
    if [ "$maint_done" = false ] && cur | grep -qF "Pre-session IB connection ended"; then
        maint_done=true
        echo "[MAINTENANCE] break started — orchestrator shut down cleanly for the CME 17:00-18:00 ET maintenance break; run the post-session cycle"
        exit 0
    fi
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

check_maintenance   # if we armed right at the maintenance break, hand off immediately

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

# The agent refused the dispatcher: no brain trades this session and it never falls back
# to legacy. One log line is not enough — surface it and stop.
if cur | grep -q "\[AGENT-LIVE\] REFUSED"; then
    echo "[KEEPALIVE] AGENT REFUSED THE DISPATCHER: $(cur | grep '\[AGENT-LIVE\] REFUSED' | head -1)"
    exit 0
fi

if cur | grep -q "\[AGENT-LIVE\] OK"; then
    agent_ok=true
    echo "[MONITOR] Agent owns the dispatcher — legacy dark, arm 09:20 ET"
fi

if cur | grep -q "\[ib-watchdog\] No data for" && ! session_is_over; then
    ib_zombie_suspected=true
    echo "[MONITOR] IB watchdog: zombie suspected — no bar data received"
fi

if cur | grep -q "\[ib-watchdog\] No recovery after" && ! session_is_over; then
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

    :
fi

while true; do
    sleep 5

    check_maintenance   # clean shutdown for the CME maintenance break → hand off to Step 4

    # IB watchdog events — only meaningful DURING the active session. After a clean session
    # close they are the expected maintenance-break teardown, so suppress them there.
    if [ "$ib_zombie_suspected" = false ] && cur | grep -q "\[ib-watchdog\] No data for" && ! session_is_over; then
        ib_zombie_suspected=true
        echo "[MONITOR] IB watchdog: zombie suspected — no bar data received"
    fi
    if [ "$ib_watchdog_killed" = false ] && cur | grep -q "\[ib-watchdog\] No recovery after" && ! session_is_over; then
        ib_watchdog_killed=true
        echo "[KEEPALIVE] IB watchdog: connection killed as zombie — orchestrator restarting"
    fi

    if [ "$late_arm" = false ] && cur | grep -q "\[AGENT-LIVE\] LATE ARM"; then
        late_arm=true
        echo "[MONITOR] Late arm taken: $(cur | grep '\[AGENT-LIVE\] LATE ARM' | head -1)"
    fi
    if [ "$agent_ok" = false ] && cur | grep -q "\[AGENT-LIVE\] OK"; then
        agent_ok=true
        echo "[MONITOR] Agent owns the dispatcher — legacy dark, arm 09:20 ET"
    fi
    if [ "$agent_refused" = false ] && cur | grep -q "\[AGENT-LIVE\] REFUSED"; then
        agent_refused=true
        echo "[KEEPALIVE] AGENT REFUSED THE DISPATCHER: $(cur | grep '\[AGENT-LIVE\] REFUSED' | head -1)"
        exit 0
    fi

    # THE 09:00-START DEADLINE. The gap-fill blocks the bar feed and the 09:20 arm is an
    # exact-minute test, so a fill still running at 09:20 costs the whole day. Warn at 09:15
    # while there is still a minute to decide something.
    if [ "$gap_fill_done" = false ] && [ "$gap_deadline_reported" = false ]; then
        et=$(uv run python -c "import datetime,zoneinfo;n=datetime.datetime.now(tz=zoneinfo.ZoneInfo('America/New_York'));print(n.hour*60+n.minute)" 2>/dev/null)
        if [ -n "$et" ] && [ "$et" -ge 555 ] && [ "$et" -lt 560 ]; then
            gap_deadline_reported=true
            echo "[KEEPALIVE] GAP-FILL STILL RUNNING AT 09:15 ET — the thesis can still be taken until 09:27 (grace window); after that the day is DARK"
        fi
    fi

    # Gap-fill and FATAL — both detected from stdout with offset
    if [ "$gap_fill_done" = false ]; then
        if cur | grep -q "FATAL"; then
            fatal_msg=$(cur | grep "FATAL" | head -1)
            echo "[KEEPALIVE] Orchestrator startup FATAL: $fatal_msg"
            exit 0
        fi
        if cur | grep -q "\[AGENT-LIVE\] REFUSED"; then
            echo "[KEEPALIVE] AGENT REFUSED THE DISPATCHER: $(cur | grep '\[AGENT-LIVE\] REFUSED' | head -1)"
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

    # Keepalive — orchestrator: always active from startup. After a clean session close,
    # the orchestrator exiting is the expected CME maintenance-break shutdown → emit the
    # maintenance event (run Step 4), NOT a crash alarm.
    if [ -n "$ORCH_PID" ] && ! is_alive "$ORCH_PID"; then
        if [ "$maint_done" = true ] || session_is_over; then
            echo "[MAINTENANCE] break started — orchestrator exited for the CME 17:00-18:00 ET maintenance break; run the post-session cycle"
        elif [ "$session_started" = true ]; then
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

        # --- the agent's own milestones (files, not log lines: the thesis and the plan
        # --- are written to the session folder, they are never [TRADER] records)
        if [ "$thesis_done" = false ] && [ -f "$SESSION/thesis_state.json" ]; then
            thesis_done=true
            bias=$(grep -oE '"bias"[[:space:]]*:[[:space:]]*"[A-Z]+"' "$SESSION/thesis_state.json" | head -1)
            echo "[MONITOR] L1 thesis armed: $bias"
        fi
        if [ "$plan_done" = false ] && [ -f "$SESSION/plans.json" ]; then
            plan_done=true
            echo "[MONITOR] Plan derived — entries possible from 09:30:30, cutoff 10:30"
        fi
        if [ "$order_done" = false ]; then
            o=$(cur | grep -F '"source": "agent"' | head -1)
            if [ -n "$o" ]; then order_done=true; echo "[MONITOR] First agent order: $o"; fi
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
| `[MONITOR] Agent owns the dispatcher …` | `Agent owns the dispatcher — legacy dark, arm 09:20 ET` |
| `[MONITOR] L1 thesis armed: …` | `L1 thesis armed: <bias> — plan next, entries from 09:30:30` |
| `[MONITOR] Plan derived …` | `Plan derived — entries possible 09:30:30–10:30` |
| `[MONITOR] First agent order: …` | `FIRST ORDER: <direction> @ <price> — check Tradovate` |
| `[KEEPALIVE] GAP-FILL STILL RUNNING AT 09:15 ET …` | `WARNING: gap-fill still running at 09:15 — the thesis can still be taken until 09:27, dark day after that` |
| `[MONITOR] Late arm taken …` | `Late arm: thesis taken after 09:20 — gap-fill ran close to the wire` |
| `[MONITOR] automation.main restarted by orchestrator …` | `automation.main restarted (pid=…) — session resuming` |
| `[MONITOR] IB watchdog: zombie suspected …` | `WARNING: IB zombie suspected — no bar data; 30s recovery window open` |
| `[KEEPALIVE] IB watchdog: connection killed …` | `WARNING: IB watchdog killed zombie connection — orchestrator restarting` |
| `[KEEPALIVE] Orchestrator startup FATAL: …` | `CRITICAL: Orchestrator failed at startup — <first line of FATAL message>` |
| `[KEEPALIVE] AGENT REFUSED THE DISPATCHER: …` | `CRITICAL: agent refused the dispatcher — NOTHING trades this session. <reason>` |
| `[KEEPALIVE] Orchestrator … died before session start …` | `CRITICAL: Orchestrator died before session start — check stdout log` |
| `[KEEPALIVE] Orchestrator … has DIED` | `CRITICAL: Orchestrator died during session` |
| `[KEEPALIVE] automation.main … has DIED` | `WARNING: automation.main died — orchestrator should restart it` |
| `[MAINTENANCE] break started …` | `Maintenance break — running parquet-check, session-analysis, rebase; will auto-restart at the 18:00 ET reopen` — then **run Step 4** |
| `[REOPEN] IB realtime data confirmed …` | `IB live after maintenance — restarting orchestrator for the next session` — then **restart (Step 1) and re-arm this Monitor (Step 2)** |
| `[REOPEN] WARN: no IB realtime data …` | `WARNING: IB data not back after the maintenance break — manual check needed` |

The `[MAINTENANCE]` and `[REOPEN]` events are not just notifications — they drive the
Step-4 cycle below. The keepalive Monitor (this step) exits when it emits `[MAINTENANCE]`.

## Step 4 — Maintenance-break cycle (CME 17:00–18:00 ET) and auto-restart

The keepalive Monitor emits `[MAINTENANCE] break started` once per day, when the
orchestrator shuts down cleanly for the CME daily maintenance break (~17:00 ET): IB
Gateway closes the feed, the watchdog reports "no bar data", and the orchestrator exits.
**This is EXPECTED — not a crash.** The preceding `[ib-watchdog] No data for` /
`No recovery after` lines in this window are normal and are already suppressed by the
Monitor. On the `[MAINTENANCE]` event, run the post-session cycle, then auto-restart for
the next session.

**1. Post-session housekeeping (run now, while the market is closed):**

  a. Invoke the **`parquet-check`** skill (validate/repair the session + main parquets).

  b. Invoke the **`session-analysis`** skill (discrepancies / optimizations / digest for
     the session that just closed).

  c. Pull + rebase the worktree onto remote master so the next session runs the latest
     code:

  ```bash
  git fetch origin && git rebase origin/master
  ```

  If the rebase reports conflicts, run `git rebase --abort`, notify the user, and proceed
  on the CURRENT code — never start the next session on a half-rebased tree, and never
  skip the session over a rebase conflict.

**2. Wait for the reopen, then verify IB realtime data is live again.** IB Gateway
auto-restarts during the break and stays logged in, but **no bars flow until the 18:00 ET
reopen**. Arm a second Monitor (`persistent: true`) that polls the IB realtime
verification script — it returns no-data throughout the break and only succeeds once bars
actually resume, so it pinpoints the reopen without any clock math:

```bash
BASE="$(pwd)"
VERIFY="$BASE/.claude/skills/run-orchestrator/scripts/verify_ib_realtime.py"
echo "[REOPEN-WAIT] polling IB realtime data until the CME maintenance break ends (~18:00 ET)"
for i in $(seq 1 75); do
    # verify_ib_realtime.py: exit 0 = MNQ+MES ticks flowing; 2 = connected, no data;
    # 3 = connect failed (IB mid-restart). Only exit 0 means the feed is live again.
    if uv run python "$VERIFY" --timeout 20 >/dev/null 2>&1; then
        echo "[REOPEN] IB realtime data confirmed — restart the orchestrator for the next session"
        exit 0
    fi
    sleep 40
done
echo "[REOPEN] WARN: no IB realtime data ~75 min after the break started — manual check needed"
exit 0
```

**3. On `[REOPEN] IB realtime data confirmed`:** restart the orchestrator for the next
session by repeating **Step 1** (`uv run python trade.py start --resume`, record
the running-commit note, report the session window) and **re-arm the Step-2 keepalive
Monitor**. The full cycle then repeats automatically each trading day:

```
session running → [ORCH] Session ended (16:55) → pre-session accumulate →
[MAINTENANCE] break started (~17:00) → parquet-check + session-analysis + rebase →
[REOPEN-WAIT] poll verify script → [REOPEN] data confirmed (~18:00) →
trade.py start + re-arm Monitor → session running …
```

On `[REOPEN] WARN`, do NOT auto-restart — alert the user that IB never came back and let
them investigate (Gateway may not have logged back in after the break).
