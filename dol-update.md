# DOL update on a counter-thesis day-extreme sweep

Status: DESIGN — NOT IMPLEMENTED, deliberately deferred (2026-08-29). Nothing here is wired
into `l2-mechanisms.md`, `agent-docs/strategy/decisions/thesis.md`, or any code. This file
records the problem, the evidence, and the proposal so the work can be picked up later
without re-deriving it. **The change lands in L1 (the thesis/recall layer), not in L2.**

---

## 1. The problem

L1 names a DOL once, at the 09:20 arm, from what it can see then. Nothing re-opens that
choice for the rest of the plan's life. When the market's first post-09:30 move is large and
runs *against* the thesis — often crossing the daily mid on the way — the premise the DOL was
chosen under has changed materially, but the target has not.

The consequence is a target that is unreachable within the mechanism window while every L2
entry mechanism is still holding a 25-pt stop for it. Measured distances from the actual entry
to the standing DOL on the days studied:

| date | recorded DOL | distance from entry |
|---|---|---|
| 08-19 | `prev1_day_high` 30,124.25 | **653.25** |
| 08-19 (2nd entry) | same | 641.50 |
| 07-31 | `prev4_day_high` 28,763.75 | 436.75 |
| 08-11 | `prev1_day_high` 29,985.00 | 290.25 |
| 08-26 | `prev1_day_low` 29,016.75 | 270.50 |
| 08-28 | `prev7_day_high` 29,759.00 | 158.50 |

Median favourable excursion on those same entries was ~125 pts within 60 minutes. The entries
were reaching +50 to +155; the targets sat 160 to 650 pts away. The exit was the binding
constraint, not the entry.

This is the same defect family §10.1 of `l2-mechanisms.md` already records from the other
direction ("a DOL within ~60–80 pts at arm time … produces flat days … candidate L2 rule: when
the nearest DOL is within ~80 pts at arm, target the next-deeper pool instead"). That candidate
was never adopted, and it only covers the too-close case. This file covers the too-far case
that a large counter-thesis move creates.

---

## 2. Evidence that L1 never revisits the DOL

Across the 24 recorded 09:20 theses in `manual-l1-thesis/` (07-14 → 08-28), the recall events
L1 actually emitted were:

| recall event type | occurrences |
|---|---|
| `clock_after` | 47 |
| `n_closes_beyond` | 5 |
| `time_elapsed` | 2 |
| **`level_swept`** | **0** |

Every recorded plan recalls on a **clock**, never on a structural event. `level_swept` is
already one of the six legal predicate atoms and is already legal inside `recall.events`
(see `agent/contracts/schemas.py` and the thesis-doc predicate vocabulary) — L1 simply never
reaches for it. **So this is a knowledge-base authoring gap, not a schema change.**

---

## 3. Worked example — 2026-08-19

Recorded 09:20 thesis: **bias UP**, DOL `prev1_day_high` **30,124.25**.

Structure at the arm:

```
pre-09:30 session low   29,430.25  (02:08)      <- counter-thesis day extreme
pre-09:30 session high  29,759.00  (08:52)
price at the 09:20 arm  29,717.50
09:30 open              29,696.25
```

What actually happened:

```
09:33:01   post-09:30 high  29,742.00
10:15:02   post-09:30 low   29,375.75     <- 366 pts DOWN, against an UP thesis,
                                             and through the pre-09:30 session low
```

The counter-thesis day extreme (29,430.25) was swept during that decline. That sweep is the
event this proposal keys on: at that moment the market had made a large, unanticipated move in
the wrong direction, and `prev1_day_high` at 30,124.25 — **653 pts above where L2 was
entering** — was no longer a plausible session target.

Under the current design nothing re-opens it. Both sweep-and-reclaim entries that day
(09:55 @ 29,471.00 and 10:30 @ 29,482.75) ran +125.50 and +149.00 of favourable excursion
inside 60 minutes and were still judged against a target 640+ pts away.

**Candidate replacement target (Gilad's formulation):** the *mid of the biggest post-09:30
move*. Measured on the 09:30–10:30 swing (high 29,742.00 → low 29,375.75) that mid is
**29,558.88**. Evaluated progressively as the swing extends:

| evaluated at | low so far | mid vs the 09:33 high |
|---|---|---|
| 09:51 | 29,455.25 | 29,598.62 |
| 09:55 | 29,407.75 | 29,574.88 |
| 10:15 | 29,377.00 | 29,559.50 |

All three sit 90–130 pts above the entries — reachable, and clear of the §2 60-pt DOL floor.

---

## 4. The proposal

**When L1 names a DOL, it should also arm a recall on the counter-thesis day extreme being
swept**, so that a large unexpected counter-move forces a re-decision rather than leaving a
stale target standing.

Concretely, in the thesis output:

```
recall: {
  events: [
    {"type": "clock_after", "et_time": "<next subsession boundary>"},   # existing cadence
    {"type": "level_swept", "name": "<counter-thesis day extreme level>"}   # NEW
  ],
  max_age_min: <existing>
}
```

`level_swept` references a level **by name**, and the validator requires that name to be a
REAL price level from the enum — never a synthetic mid, FVG-zone id, or the DOL id itself.
For an UP thesis the counter-thesis day extreme is the running/standing day low; for a DOWN
thesis, the day high.

On recall, L1 re-runs with the new facts and picks a DOL appropriate to what the market has
actually done. It may re-affirm the original, pick a nearer pool, or resolve NEUTRAL.

---

## 5. Why this is L1's job, not L2's

`l2-mechanisms.md` §2 is explicit:

> "Re-arming is L1's recall problem — the recall cadence fires at subsession boundaries and can
> arm a fresh plan when liquidity re-forms — **NOT L2's: no fallback DOL, no self-armed
> thesis** (the §10.2 adverse-day bounds rest entirely on the plan/DOL discipline)."

Having L2 substitute its own target would break exactly the discipline that bounds the
wrong-thesis adverse days. Having L1 re-run on a structural trigger is the intended path and
uses machinery that already exists.

---

## 6. Open questions to settle before implementing

- **The replacement target must be a real named level.** "Mid of the biggest post-09:30 move"
  is synthetic and the validator forbids synthetic names in terminal predicates. Either express
  it as the nearest real level to that mid, or deliberately relax the rule for this case and
  record why.
- **Recall cost and frequency.** Each recall is a real model call. Firing on every
  counter-thesis extreme sweep may be frequent; bound it (once per plan, or only when the sweep
  exceeds some size), and check the interaction with `max_age_min` TTL expiry.
- **Floor interaction.** The new DOL must still clear the §2 60-pt floor from where L2 would
  enter, or the fix swaps an unreachable target for a vetoed one.
- **Which extreme.** The 24h session extreme standing at 09:30, or the running day extreme?
  §7's stale-extreme re-key (age > 2h at the plan arm) already had to answer a closely related
  question and its invariant — *staleness decides which pool the market is actually working* —
  is the natural precedent.
- **Does the re-picked DOL actually help?** Not measured. The evidence above shows the standing
  DOL is too far; it does not yet show that a recalled DOL would be better. That needs a run
  with recall actually firing — most cleanly via an injected-thesis replay mode.

---

## 7. Relationship to existing sections

- **§10.1 (`l2-mechanisms.md`)** — records the too-close DOL case and its unadopted candidate
  rule. This file is the too-far counterpart; the two should probably be settled together.
- **§2 DOL-floor veto** — guards the near end only. Nothing bounds the far end, and nothing
  re-evaluates either end after the arm.
- **§6 / §7** — both already key on "a new counter-thesis day extreme printed after 09:30" as a
  precondition, so the trigger this proposal needs is already a first-class concept in L2; here
  it would be consumed by L1 as a recall event instead.
