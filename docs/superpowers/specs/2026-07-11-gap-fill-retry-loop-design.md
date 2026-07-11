# Gap-fill retry loop: unify production and offline behavior

## Context

`trade.py gap-fill` (offline, run when the orchestrator is NOT running) and
`IbRealtimeSource.start()` (production — invoked by the orchestrator in signal
mode via a background thread, and by `automation.main` synchronously in
LIVE_TRADING mode) both back-fill the 4 main parquets (`MNQ_1s`, `MES_1s`,
`MNQ_1m`, `MES_1m`) from IB before proceeding.

IB enforces a real historical-data pacing limit (~60 requests / rolling 10-min
window). A single `1s` chunk covers 1800s (30 min) of data, so one quota
window can close roughly 30 hours of *trading-time* gap in the best case. A
gap larger than that needs multiple quota windows to close.

This was hit in practice on 2026-07-10: after several non-trading days, MES's
1s gap was 163.4 hours. A single `trade.py gap-fill` invocation could only
close part of it before hitting repeated IB pacing violations (error 162) and
giving up gracefully (existing behavior — it does not crash). Manually
re-invoking `trade.py gap-fill` every ~10-11 minutes (enough for IB's rolling
quota window to clear) closed the gap fully across 6 rounds over ~90 minutes.

Two problems with the current state:

1. **Offline (`trade.py gap-fill`) requires manual re-invocation** to close a
   gap larger than one quota window — there's no internal retry loop.
2. **Production (`IbRealtimeSource.start()`) blocks live-session startup**
   on a single-pass fill attempt. For a large gap this either leaves a large
   silent historical hole (1s fill degrades gracefully and moves on) or,
   worse, the 1m fill (`gap_fill_1m_ib`) can hang for its documented 30-minute
   wall-clock retry deadline and then **raise `RuntimeError`**, crashing
   session startup outright.

## Goal

Production and offline should behave **identically**: both fully close
whatever gap exists, however long that takes, using the same retry loop with
the same IB-pacing-aware spacing — the difference is only *where* the loop
runs (in-process during production startup vs. as a standalone CLI command),
not what it does. Production only gives up if something is genuinely broken
(repeated real errors), never merely because the gap is large.

## Design

### A. Shared gap measurement + failure exception

```python
# data/ib_realtime.py
_GAP_HEADSUP_HOURS = 24    # informational: gap this large likely needs multiple rounds
_GAP_CAUGHT_UP_HOURS = 1   # loop's convergence target (matches what was validated manually)

class GapFillFailedError(Exception):
    """Raised when gap-fill hits 5 consecutive round failures without closing the gap."""
    def __init__(self, gaps_hours: dict[str, float], last_error: Exception):
        self.gaps_hours = gaps_hours
        self.last_error = last_error
        super().__init__(
            f"gap-fill failed 5 consecutive rounds; last error: {last_error}; "
            f"gaps: {gaps_hours}"
        )

def gap_hours_by_file(bar_data_dir: Path) -> dict[str, float]:
    """Hours between now and the last bar in each of the 4 main parquets.

    A missing file returns float('inf') for that entry rather than raising —
    a first-ever run with no existing parquet naturally reads as "needs
    multiple rounds," consistent with how the existing fill logic already
    creates files from scratch when absent.
    """
```

`gap_hours_by_file` reuses the same 4-file set that `check_parquet_gaps`
already scans, and the 24h informational threshold is close to (though
distinct in purpose from) the 48h cutoff `check_parquet_gaps` already uses —
this isn't introducing a foreign concept into the codebase.

### B. One shared retry loop, used by both production and offline

```python
# data/ib_realtime.py
def run_gap_fill_with_retries(
    do_one_round: Callable[[], None],
    bar_data_dir: Path,
    *,
    max_consecutive_failures: int = 5,
    round_spacing_s: float = 650,  # ~10-11 min, matches what was validated manually
) -> None:
    gaps = gap_hours_by_file(bar_data_dir)
    if max(gaps.values(), default=0) > _GAP_HEADSUP_HOURS:
        print(f"[gap_fill] Large gap detected ({gaps}) — this may take several rounds "
              f"spaced ~{round_spacing_s/60:.0f} min apart to respect IB's pacing limit. "
              "Sitting tight...", flush=True)

    consecutive_failures = 0
    last_error: Exception | None = None
    round_num = 1
    while True:
        try:
            do_one_round()
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            last_error = exc
            print(f"[gap_fill] round {round_num} failed ({exc}) — "
                  f"{consecutive_failures}/{max_consecutive_failures} consecutive failures",
                  flush=True)
            if consecutive_failures >= max_consecutive_failures:
                raise GapFillFailedError(gap_hours_by_file(bar_data_dir), last_error)

        gaps = gap_hours_by_file(bar_data_dir)
        if max(gaps.values(), default=0) <= _GAP_CAUGHT_UP_HOURS:
            print(f"[gap_fill] Caught up — all parquets within {_GAP_CAUGHT_UP_HOURS}h of now.",
                  flush=True)
            return

        print(f"[gap_fill] round {round_num} done — gap remaining: {gaps} — "
              f"next round in ~{round_spacing_s/60:.0f} min", flush=True)
        time.sleep(round_spacing_s)
        round_num += 1
```

**Call sites — both funnel through this one function, no subprocess, no
duplicated logic:**

- **`gap_fill.py`** (`trade.py gap-fill`, offline): `gap_fill_until_now()`
  builds its fill-only `IbRealtimeSource` exactly as today, then calls
  `run_gap_fill_with_retries(source.gap_fill, bar_data_dir)` instead of a
  single `source.gap_fill()` call. `trade.py`'s CLI handler itself is
  unchanged — it already just calls `gap_fill_until_now()`.

- **`IbRealtimeSource.start()`** (production — both orchestrator signal-mode
  and `automation.main` LIVE_TRADING mode): replaces its current
  `self.gap_fill()` call with
  `run_gap_fill_with_retries(self.gap_fill, self._bar_data_dir)`.

`IbRealtimeSource.gap_fill()` itself (the single-pass primitive: one 1s fill
attempt + one 1m fill attempt) is **unchanged** — it's passed as the
`do_one_round` callable into the shared retry wrapper, not modified. Existing
tests that call `source.gap_fill()` directly continue to test the single-pass
primitive unmodified.

### C. Termination on repeated real failure

On 5 consecutive round failures, `GapFillFailedError` propagates to the two
production call sites, mirroring the existing `IbGatewayDisconnectedError` →
`sys.exit(2)` pattern already in `automation/main.py`:

- **`automation/main.py`**: add `except GapFillFailedError as exc:` alongside
  the existing `except IbGatewayDisconnectedError:` around
  `_ib_source.start()`. Prints a loud block (last error, per-file gap hours,
  exact command `python trade.py gap-fill` to resolve manually) and
  `sys.exit(11)` — a new dedicated code, distinct from `2` (IB disconnect)
  and `10` (missing parquets).

- **`orchestrator/main.py`**: the exception propagates out of
  `source.start()` into `thread_exc[0]` (existing mechanism, unchanged).
  `_make_ib_health_check()`'s `check()` currently treats *any* thread death
  as the routine "maintenance break" case and calls generic `_GracefulStop()`
  → always `sys.exit(0)`. `_GracefulStop` gains an optional
  `exit_code: int = 0` attribute; `check()` special-cases
  `isinstance(thread_exc[0], GapFillFailedError)`: prints the same loud
  actionable block, then `raise _GracefulStop(exit_code=11)`. The existing
  `except _GracefulStop as exc: sys.exit(exc.exit_code)` then exits 11
  instead of silently exiting 0 — so this is never confused with a routine
  maintenance-break shutdown. The routine case (generic thread death, no
  `GapFillFailedError`) is unchanged and still exits 0.

Both paths converge on the same exit code (`11`) and the same message
format, so from the outside ("why didn't trading start today") the situation
looks identical regardless of which mode was running.

**IB genuinely unreachable** (not just paced) is already gated *before* this
logic runs in both paths: offline's `gap_fill_until_now()` calls
`check_ib_reachable()` up front (existing code, fails fast, never enters the
retry loop); production's `_pre_session_init()` / `automation.main` already
TCP-probe IB before starting. So the 5-consecutive-failures counter is
specifically for failures *during* the fill process (transient IB errors,
unexpected exceptions inside a chunk request) — not "IB was never up," which
already fails fast separately.

## Testing plan

- **`gap_hours_by_file()`**: all-current → near-zero hours each; missing
  file → `inf` for that entry, others unaffected.
- **`run_gap_fill_with_retries()`** (mock `time.sleep` and the
  `do_one_round` callable):
  1. Small gap, single round closes it → returns immediately, `time.sleep`
     never called.
  2. Large gap needing 3 successful-but-incomplete rounds → loops correctly,
     headsup message printed exactly once (only when initial gap >
     `_GAP_HEADSUP_HOURS`), `time.sleep(650)` called between each round.
  3. One round raises, next round succeeds → failure counter resets to 0
     (not global across the whole run) — verifies transient errors don't
     quietly erode toward the cap.
  4. 5 consecutive raising rounds → raises `GapFillFailedError` with correct
     `gaps_hours`/`last_error`, no 6th attempt made.
- **Production integration** (mirrors the existing
  `IbGatewayDisconnectedError → exit(2)` test pattern):
  5. `automation/main.py`: `_ib_source.start()` raises `GapFillFailedError`
     → caught, loud message printed, `sys.exit(11)`.
  6. `orchestrator/main.py`: `thread_exc[0]` set to `GapFillFailedError` →
     health-check prints loud message, `_GracefulStop(exit_code=11)` →
     `run()` exits 11 (not the routine maintenance-break 0).
  7. **Regression**: the existing routine "IB dropped during maintenance
     break" path (generic thread death, no `GapFillFailedError`) still
     exits 0 with the original message — unchanged.
- **`IbRealtimeSource.gap_fill()`** (single-pass primitive): existing tests
  (`tests/test_gap_fill.py` lines 144, 164 calling `src.gap_fill()` directly)
  continue to test single-pass behavior unmodified — no signature change to
  this method.

## Out of scope

- No new external notification channel (Slack/email/push) — this repo has
  none today; the log output + distinct exit code is the full extent of
  "notification," matching the existing `--check-parquets` → `sys.exit(10)`
  precedent.
- No change to the 1s fill's existing graceful-degradation behavior within a
  single round (chunk-level pacing retry/backoff in `_gap_fill_1s_ib`) or to
  the 1m fill's existing per-round retry logic in `gap_fill_1m_ib` — those
  are unchanged; this design only adds a retry loop *around* whole rounds.
