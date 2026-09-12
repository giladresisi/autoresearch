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

Flags:
- `--dry-run` — validation only, no data changes (no parquet, `.bak`, or sidecar writes).
- `--full-validate` — force a full re-scan of the main 1m parquets, bypassing the
  incremental watermark. Use after validator-rule changes or to re-establish trust.
- `--since <iso>` — re-scan the 1m tail from a given timestamp (recovery/debugging aid).

## Step 2 — Parse and assess

**1s parquets** — for each instrument in `instruments`:
- `severity`: ok / minor / major / critical
- `action`: what was done (merge, targeted_fill_then_merge, rebuild_then_merge, gap_fill_then_merge, skip)
- `merge_success`: true / false / null (null = dry-run)
- `backup_written`: true / false

**If `merge_success = true`**: report what was done in plain English.

**If `merge_success = false`**: escalate — tell the user what manual steps are needed
and why the automatic fix failed (check `reason` field in the JSON).

**Promotion (session-end only) — `promotion` block.** After a SUCCESSFUL session-end
merge, the script's FINAL step promotes the validated parquets from the **live production**
dir (`<global>/general/live`, env `ACT_GLOBAL_DIR`; resolved by `paths.general_live_dir()`)
to the **backtest read source** — the **current contract subfolder** under
`<global>/general/main/` (`_current_main_subdir()`: the newest row's `subfolder` in
`rollover_ledger.json`, e.g. `2026-09`; falls back to `general_main_dir()` itself pre-rollover):
for each `<inst>_1m.parquet` / `<inst>_1s.parquet` present in live it backs up the existing main
file to `<name>.parquet.bak`, then atomically copies live → that subfolder. Frozen pre-roll
subfolders are never overwritten. The `promotion` field:
- `promote_success: true` + `promoted: {<name>: "ok", ...}` — live→main promotion done.
- `promote_success: false` + `reason` — promotion failed; main was NOT updated (escalate).
- `null` — no promotion attempted (dry-run, orchestrator-start mode, or no successful merge).

This promotion runs only in `session-end` mode; `orchestrator-start` never promotes.

**Rollover due — `rollover` block.** Present in every run: `{due, already_rolled, prep_date,
reason}`. If `due` is `true`, the quarterly contract roll is outstanding and a
**CONTRACT ROLLOVER DUE** banner is also printed to stderr. Promotion targets the ledger's
newest subfolder, so leaving it undone piles new-contract sessions into the old era's folder.
Go to the rollover-prep section below and run `trade.py rollover-prep` after this check
completes. `already_rolled: true` means the row landed but `.env` was not advanced — do not
re-run the roll; escalate.

**1m parquets** — for each instrument in `instruments_1m`:
- `action`: ok / repair_from_backup
- `repair_success`: true / false / null (null = dry-run or healthy)
- `backup_written`: true / false (healthy run writes a fresh .bak; repaired run also writes .bak)
- `backup_used`: path of the backup that was used (only present on repair)
- `gapfill_status`: "ok: appended N 1m bars from M 1s bars" or "failed: <reason>"
- `corrupted_saved_as`: filename of the saved corrupt copy (only present on repair)
- `validation_scope`: `"incremental"` (only the appended tail + seam were validated) or
  `"full"` (whole body re-scanned)
- `validated_through`: the timestamp up to which the parquet is validated (the watermark)
- `full_reason`: why a full scan ran (present when scope is `full`) — `no-watermark`
  (first run), `version-bump`, `body-rewritten`, `truncation`, or `forced-full`
- `seam_issue`: may appear when the tail's join onto the validated body is anomalous
  (overlap/duplicate or unexpected gap)

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
- **`exit_code = 4`**: **blocked — a quarterly contract roll is pending.** Nothing was read from
  IB and nothing was changed. Go to the rollover-prep section and complete the roll.

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

**Incremental scope note:** when an `instruments_1m` entry has
`validation_scope == "incremental"`, the low-bar-count and stale-last-bar heuristics
above apply to the **tail delta** (the bars appended since the watermark), not the
whole body. A `full` scope run — or an explicit `--full-validate` — is the periodic
safety net that re-checks the entire body.

## Contract rollover-prep (quarterly — gated on `ROLLOVER_PREP_DATE`)

**Trigger: the date, not the mode.** Read `ROLLOVER_PREP_DATE` from `.env`. If the current date
is **on or after** it and no `rollover_ledger.json` row covers it yet, the quarterly contract has
rolled and the data must be migrated — **including when the user only asked for an offline
gap-fill and promote**. It is not session-end-only; a plain `trade.py gap-fill` + `trade.py
promote` on or after the prep date must be followed by the roll, or `main/` keeps accumulating
old-contract data under the stale era.

`trade.py gap-fill` and `trade.py promote` print a **CONTRACT ROLLOVER DUE** banner in this
situation. If you see it, do not stop at promote.

### The gate — IB fetching stops until the roll completes

Once the prep date has passed **and** the old contract's final session is gap-filled and
promoted, every IB-fetch path refuses to run:

- `gap_fill.gap_fill_until_now` — covers `trade.py gap-fill` **and** `orchestrator.main`'s
  pre-session backfill, so production startup stops too (and with it the live realtime
  subscription, which only opens after startup passes this point). Exits 1.
- `scripts/check_session_parquets.py` — exits **4** with `rollover_blocked: true` in the report.
  `--dry-run` is exempt: it reads nothing from IB and is the safe way to inspect state.

The window this closes: between the operator editing `.env` and the roll finishing, `.env` names
the new contract while the parquets are still on the old scale. Any fetch in that window splices
new-contract prices onto old-contract history. The gate means nothing *acts* in that window —
notably the Sunday 18:00 ET orchestrator restart.

The gate keys off the same preflight the roll uses, so it has two phases: **not** blocked while
the old-conid gap-fill is still the required next step, blocked once live and main agree.
Completing the roll writes the ledger row, which makes the status not-due — so the gate reopens
by construction, with no separate approval flag that can go stale.

### Do it with the CLI

```powershell
uv run python trade.py rollover-prep --dry-run   # prints the new conids + gaps, changes nothing
# operator edits the two conid lines in .env      (see below)
uv run python trade.py rollover-prep             # verifies .env, then executes
```

**Who owns what in `.env`:** the operator sets `MNQ_CONID` / `MES_CONID`; the code advances
`ROLLOVER_PREP_DATE` on completion, so that date can never claim a roll that did not finish.
Do not edit `ROLLOVER_PREP_DATE` by hand, and do not edit the conids for the agent.

`rollover-prep` **verifies** `.env` names the expected new front month before anything is
shifted, and refuses with the exact lines to set if not. This is deliberate: an operator's "yes
I updated it" that did not land would shift the parquets onto the new scale while every later
fetch still returned old-contract prices — the same discontinuity, mirrored. Asking is the
workflow; verifying is what makes it safe.

It also rejects an implausible measured gap (zero, or more than 3% of the price level) before
applying it — the shift touches every bar ever recorded, and nobody is reading the number when
the roll runs unattended.

`scripts/rollover_prep.py` performs every step below in the correct order, refuses to run when
the preconditions are not met, and records per-step state in `<main>/.rollover_prep_state.json`
so a crash midway never re-applies the destructive in-place back-adjustment. Prefer it over
doing the steps by hand.

### The required order (and why each step depends on the one before)

```
trade.py gap-fill  ->  trade.py promote  ->  trade.py rollover-prep
(OLD conids)           (freezes old era)     (shifts live, opens new era)
```

- **Gap-fill before touching `.env` conids.** Swap the conids first and IB returns new-contract
  prices appended onto old-contract history — a ~300-point discontinuity baked into the live
  parquets with no seam marker.
- **Promote before the ledger row, never after.** `_current_main_subdir()` reads `rows[0]`, so
  the moment the new row exists, promote targets the **new** subfolder. Promote after the roll
  and the old contract's final session lands in the new era's folder as raw, un-back-adjusted
  bars on a back-adjusted scale. The old subfolder is never written again after the roll, so
  it must be complete through the old contract's last session first.

Steps, in execution order:

1. **Look up the new front-month conids and expiry** for MNQ + MES. Enumerate `reqContractDetails`
   and take the first expiry after the current one — **not `ContFuture`**: IB rolls its continuous
   contract at/near expiry, but the prep date is the Saturday *before* expiry, so on prep day
   `ContFuture` still resolves to the contract you are rolling away from. Next prep date = the
   **Saturday before** the new expiry.
2. **Measure the per-symbol gap** = (new-front close − current-conid close) at the data's
   **seam** — the last bar in the live 1m parquet, i.e. the old contract's final session close.
   Anchor there, NOT at the latest bar the two contracts happen to share: the old contract keeps
   trading until its own expiry (a week past the prep date), so both legs resume quoting at the
   next session open while the gate holds the parquets frozen. A roll delayed even one session
   would otherwise anchor the shift away from the join and bake in the carry drift between those
   two moments. The code takes the latest common bar at or before the seam and refuses beyond a
   1-day tolerance. Fetch with `endDateTime=''` — IB rejects an explicit `endDateTime` for CME
   equity-index futures 1m bars (error 162 / 10339) — widening the lookback for a delayed roll,
   capped at IB's 14-day 1m limit.
3. **Back-adjust the LIVE parquets** in place: `OHLC += gap` per symbol (volume untouched),
   shifting old-contract history onto the new contract's price scale. Leave all `main/`
   subfolders raw/as-is — only the *live* parquets shift.
4. **Re-seed `global.json`**: set `all_time_high` to the back-adjusted live `MNQ_1m` `High`
   max (= old ATH + gap) so rule2b's ATH / recovery guard stays on the new price scale. The
   seed's corruption guard (`session_pipeline.on_session_start`) would re-anchor a stale value
   on the next restart anyway, but set it explicitly here; `session_ath` re-derives from it on
   the next restart. (This is the GIL-23 recurrence fix — a stale ATH silently disables the
   recovery guard and the strategy fades the trend.)
5. **Create `<main>/<new YYYY-MM>/` and copy the back-adjusted live parquets into it** — before
   the ledger row names it. Both readers fall back to the flat `general_main_dir()` (which holds
   stale pre-restructure data) when a row points at a missing subfolder, and `_current_main_subdir`
   accepts an *empty* directory as valid — so a row written ahead of the data silently routes
   backtests and promotion at nothing.
6. **Prepend a new newest row** to `<main>/rollover_ledger.json` — at **index 0**, not appended:
   `_current_main_subdir()` reads `rows[0]` and `_main_dir_for_date` takes the first row with
   `prep_date <= date`, both treating the array as newest-first. **Appending is a silent no-op** —
   no error, and every reader keeps resolving to the previous era.
   `{"prep_date":"<this prep date>","subfolder":"<new YYYY-MM>","expiry":"<new expiry>",`
   `"mnq":{old_conid,new_conid,gap},"mes":{...}}`.
7. **Advance `ROLLOVER_PREP_DATE` last** — the completion marker, written only after the
   parquets and ledger are in place. The conid lines were the operator's edit and were verified
   before step 3. Note the old conids are read from the **ledger's newest row** (`<sym>.new_conid`),
   not `.env`: by the time the roll runs, `.env` names the new contract and the old numbers are gone.
8. **Notify the user**: what rolled, gaps, new conids, new subfolder, next prep date. The next
   orchestrator restart gap-fills forward with the new conids; `daily` re-derives levels from
   the now-shifted data.

Backtests then route per date via `backtest_smt._main_dir_for_date` (pre-roll dates → the frozen
raw subfolder; on/after → the new back-adjusted one). **This rollover-prep is the one exception
to the no-code-changes HARD RULE below** — running `trade.py rollover-prep` rewrites `.env` +
`rollover_ledger.json` and creates a subfolder. Invoke it as a command; do not hand-edit those
files, and do not edit any `.py`.

## Step 4 — Final summary

Output 3–5 concise lines:
- What was found per instrument (severity, action, rows merged, gap bars added if any)
- Whether backups were written
- Any items needing manual attention

## HARD RULE — no code changes

**This skill operates on data files only.**

The executing agent MUST NOT use Edit, Write, or any tool that modifies `.py`, `.md`,
or any non-parquet file. **One exception:** the quarterly rollover-prep above, which edits
`.env` (conids + `ROLLOVER_PREP_DATE`) and `<main>/rollover_ledger.json` and creates a new
`main/` subfolder — and only when `ROLLOVER_PREP_DATE` has arrived.

**Allowed writes**: parquet files only — the **live production** parquets + session 1s
files under `<global>/general/live/` (`*.parquet`, `*.parquet.bak`, `*.parquet.tmp`, and
the `backup_parquets_until_*/` dirs) and, on session-end promotion, the **backtest-main**
parquets under `<global>/general/main/`. (Resolved via `paths.general_live_dir()` /
`paths.general_main_dir()` — never the legacy worktree-local `data/`.)

The script (NOT the agent) also writes the validation-state sidecar
`.validation_state.json` under the general live dir (`paths.general_live_dir()`), which
tracks the per-parquet incremental-validation watermark. It sits alongside `global.json`
and the pause sentinel as a script-managed allowed write.

If the LLM determines that a code change would be beneficial (e.g., a recurring gap
pattern suggests a bug in a source file), it MUST:
1. Describe the proposed change in plain text in the terminal output
2. Stop — do not implement it
3. Wait for the user to explicitly request the code change in a new message
