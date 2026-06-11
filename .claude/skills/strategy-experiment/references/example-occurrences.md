# `## Example Occurrences` — schema

This table lives in the worktree's `feature.md` and is the contract between the person specifying
the experiment and the `experiment-verifier` agent (Stage 5). It must be machine-parseable: one row
per concrete, replayable occurrence, with these exact columns.

```markdown
## Example Occurrences

| id  | date       | time (ET) | source                       | window      | current behavior | desired behavior |
|-----|------------|-----------|------------------------------|-------------|------------------|------------------|
| ex1 | 2026-06-10 | 14:50     | live-session:2026-06-10      | 14:45-15:05 | <observed now>   | <wanted after>   |
| ex2 | 2026-06-10 | 13:34     | live-session:2026-06-10      |             | <observed now>   | <wanted after>   |
```

## Column meaning

- **id** — short stable handle (`ex1`, `ex2`, …) used in the verifier's report.
- **date** — `YYYY-MM-DD`, the session to replay. Distinct dates across all rows become the A/B
  regression dates (Stage 4).
- **time (ET)** — `HH:MM` or `HH:MM:SS`, the clock time of the occurrence in US/Eastern.
- **source** — where the occurrence was observed; one of:
  - `live-session:<YYYY-MM-DD>` → `<global>/sessions/<date>/` (must contain `events.jsonl`).
  - `regression-run:<absolute-path>` → an existing regression run dir (must contain
    `events_1s.jsonl`/`events.jsonl` + a trades file).
  The source is what makes the occurrence verifiable; it must exist on disk before the experiment
  runs (Stage 0 enforces this).
- **window** — optional inspection window `HH:MM-HH:MM` ET. If blank, the verifier uses
  `time ± 8 min`.
- **current behavior** — what the strategy does today at that moment, in concrete, checkable terms
  (name the event kinds / trades involved, e.g. "armed `new-stop-entry` long @28630 into the
  down-leg, filled then stopped"). The verifier expects to find this in the **baseline** run.
- **desired behavior** — what should happen after the change, equally concrete (e.g. "no
  counter-trend `new-stop-entry` long armed in this window; a veto event is emitted instead"). The
  verifier checks the **change** run for this.

## Why concreteness matters

The verifier diffs the baseline vs change runs inside each window and asserts the change *flipped*
the behavior from `current` → `desired` at that occurrence. Vague descriptions ("trades better")
can't be checked. Anchor each side to specific event kinds (`new-stop-entry`, `trend-broken`,
`smt-div`, `market-entry`, `stop-exit`), levels, or trade fills that actually appear in
`events*.jsonl` / `trades*.tsv`.
