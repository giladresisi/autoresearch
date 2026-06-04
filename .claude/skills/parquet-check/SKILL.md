---
name: parquet-check
description: >
  Use when the trading session ends or the orchestrator is about to start, to validate
  and repair session parquet files. Validates MNQ/MES 1s session files AND the main
  1m parquets. Auto-repairs 1s issues (targeted fill for minor/major gaps, full IB
  rebuild for critical). Repairs corrupt 1m parquets from backup + 1s resample.
  Merges into main parquets and backs up the result. Runs end-to-end without prompts.
  Trigger phrases: "session ended", "run parquet check", "check the session data",
  "parquet health", "check data integrity", "merge session data", "data health check".
---

# Parquet Session Health Check

Validates and repairs 1s session parquet files, merges them into main parquets, and
backs up the result. Runs fully autonomously once invoked — no confirmation prompts.

## Mode Selection

| Context | Mode |
|---|---|
| User/agent says session just ended / it is post-16:00 ET on a trading day | `session-end` |
| User/agent says orchestrator is starting / pre-session | `orchestrator-start` |
| Ambiguous | Ask: "Is this a session-end check or a pre-session orchestrator-start check?" |

## Step 0 — Verify IB is active (REQUIRED in `session-end` mode)

In `session-end` mode the engine fills the small gaps between the prior main 1s
data and the session 1s files **via IB** before merging into the main parquets. If
IB is not active, the merge proceeds WITHOUT that gap-fill and leaves seams baked
into the main parquets. To prevent that, this skill MUST confirm IB is reachable
**before running the engine** — and refuse to run if it is not.

Run the connectivity check:

```powershell
uv run python -c "import os,socket; from dotenv import load_dotenv; load_dotenv(); h=os.getenv('IB_HOST','127.0.0.1'); p=int(os.getenv('IB_PORT','4002')); s=socket.socket(); s.settimeout(3);
try:
    s.connect((h,p)); s.close(); print(f'IB_OK {h}:{p}')
except Exception as e:
    print(f'IB_DOWN {h}:{p} ({e})')"
```

- Output contains **`IB_OK`** → proceed to Step 1.
- Output contains **`IB_DOWN`** → **STOP. Do NOT run the engine.** Notify the user
  and end the skill:
  > "IB Gateway is not active (cannot connect to `<host>:<port>`). The session-end
  > merge needs IB to fill the small 1s gaps before merging into the main parquets,
  > otherwise the seams get baked in permanently. Please activate IB Gateway, then
  > re-run the parquet-check."

This gate applies to **`session-end` mode only**. In `orchestrator-start` mode the
orchestrator brings IB up itself, so skip this check and go straight to Step 1.

## Step 1 — Run the engine

```powershell
cd "C:\Users\gilad\projects\auto-co-trader\live"
uv run python scripts/check_session_parquets.py --mode <MODE> 2>check_session_stderr.log
```

Capture stdout (JSON report). On failure to run: read `check_session_stderr.log` for error.

Use `--dry-run` if the user wants validation only without any data changes.

## Step 2 — Parse and assess

**1s parquets** — for each instrument in `instruments`:
- `severity`: ok / minor / major / critical
- `action`: what was done (merge, targeted_fill_then_merge, rebuild_then_merge, gap_fill_then_merge, skip)
- `merge_success`: true / false / null (null = dry-run)
- `backup_written`: true / false

**If `merge_success = true`**: report what was done in plain English.

**If `merge_success = false`**: escalate — tell the user what manual steps are needed
and why the automatic fix failed (check `reason` field in the JSON).

**1m parquets** — for each instrument in `instruments_1m`:
- `action`: ok / repair_from_backup
- `repair_success`: true / false / null (null = dry-run or healthy)
- `backup_written`: true / false (healthy run writes a fresh .bak; repaired run also writes .bak)
- `backup_used`: path of the backup that was used (only present on repair)
- `gapfill_status`: "ok: appended N 1m bars from M 1s bars" or "failed: <reason>"
- `corrupted_saved_as`: filename of the saved corrupt copy (only present on repair)

**If `repair_success = false`**: escalate — backup was not found or was unreadable, or
the 1s session file had critical quality. Manual intervention required.

**Exit codes** — the script's exit code reflects what happened, not success/failure on its own.
Always judge success from `merge_success` / `repair_success`, not the exit code:
- **`exit_code = 0`**: no action taken (everything already healthy, nothing merged/repaired).
- **`exit_code = 1`**: **benign — a merge/repair action was performed.** This is the normal
  result of a successful session-end merge (any instrument with `action` other than
  `skip`/null bumps the code to 1). NOT an error; treat as success when `merge_success` /
  `repair_success` are `true`.
- **`exit_code = 2`**: a 1s merge or 1m repair **failed** — escalate (see the failure
  guidance above; check the `reason` field).
- **`exit_code = 3`**: script error; read `check_session_stderr.log`, report the raw
  error to the user.

## Step 3 — LLM severity judgment for ambiguous cases

The script classifies mechanically. After reading the JSON, flag these concerns even
if severity was "ok":

- **Low bar count**: fewer than 50% of expected bars for the session period
  (RTH session = ~23,400 bars, full CME session = ~82,800 bars)
- **Stale last bar**: last bar timestamp is much earlier than when this skill was invoked
  (session data stopped mid-session)
- **Anomalous prices**: price levels look far outside the main parquet's recent range
- **Late start > 2h**: `late_start_hours > 2.0` in session-end mode means the session
  file was missing overnight data. If `action = rebuild_then_merge`, confirm the rebuild
  covers from 18:00 ET (CME open). If action was only `merge`, note that
  `merge_session_1s_parquets` gap-fills main[-1]→session[0] from IB, which may or may
  not recover overnight data depending on IB availability.

If any of these concerns are flagged, note them in the final summary and suggest the
user verify manually.

## Step 4 — Final summary

Output 3–5 concise lines:
- What was found per instrument (severity, action, rows merged, gap bars added if any)
- Whether backups were written
- Any items needing manual attention

## HARD RULE — no code changes

**This skill operates on data files only.**

The executing agent MUST NOT use Edit, Write, or any tool that modifies `.py`, `.md`,
or any non-parquet file.

**Allowed writes**: `data/*.parquet`, `data/*.parquet.bak`, `data/*.parquet.tmp` only.

If the LLM determines that a code change would be beneficial (e.g., a recurring gap
pattern suggests a bug in a source file), it MUST:
1. Describe the proposed change in plain text in the terminal output
2. Stop — do not implement it
3. Wait for the user to explicitly request the code change in a new message
