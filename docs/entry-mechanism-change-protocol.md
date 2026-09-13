# Changing an entry mechanism — the protocol

Written 2026-08-29, LAST in cycle 3 phases 2+3, so it describes what was actually built
rather than what was intended. The two diverged repeatedly during the cycle, and a
protocol that matches the plan instead of the code is worse than none.

A future session arrives with no memory of this one. Everything that made this cycle work
— doc first, walk it through, capture the BEFORE, explain every delta — otherwise lives
only in a conversation. Read this before changing any entry mechanism.

---

## Step 0 — is it a RULE yet? (added 2026-08-30)

The eight steps below adopt a rule. They assume one exists to write down. **Most proposals
do not start there**, and forcing an undecided idea into step 1 writes an ambiguity into
the specification, where the next reader inherits it as settled.

`l2-mechanisms.md` already carries the earlier tier: §11's **CANDIDATE** and **IDEA ONLY**
entries — a proposal recorded with its evidence and its open questions, explicitly not
implemented. Several sit there today with "zero motivating examples" or "zero net
evidence" attached, and §11.0's DO NOT IMPLEMENT list exists so retired ones are not
rebuilt from stale prose.

So the full path is:

**§11 CANDIDATE (evidence + open questions) → resolve the rule-level questions → §§2-8 as
a rule → the eight steps below.**

A proposal is not ready for step 1 while any of these is open:

- **A rule-level ambiguity.** Not a knob — a question about WHAT the mechanism does that
  two implementations would answer differently. §6.1 is the worked precedent: four such
  ambiguities in §6's prose, each hit by an independent simulator, each now pinned
  normatively. If your change has one, pin it before it becomes a rule.
- **A choice between existing rules.** e.g. which stop applies — a §6-shaped
  excursion-anchored cap, or §7's entry-bar wick capped at 15. Both are established rules;
  picking one is a rule decision, not a tuning pass.
- **Evidence that cannot survive step 3.** If the case rests on a simulator that does not
  model the mechanisms already trading those days, step 3 will contradict it — see below.

**Worked example (2026-08-29).** A proposed second trigger branch for §7 arrived with a
sweep over 07-15..08-28 and a real trader's fill behind it. It could not start at step 1:
the stop rule was undecided, and a bar that both makes a new extreme AND closes back
beyond the swept level had no normative reading (its two candidate readings gave entries
09:34 vs 09:36 and risks 21.50 vs 25.00 on the same day). Then step 3 settled the matter
independently — replaying its motivating date showed §5 already trading it, taking two
stop-outs, while the branch's own entry would have been stopped at 10:06:26 against a
low of 29505.50, forty minutes before the DOL was reached at 10:50:26. Its own simulator
could not have shown this: it was unscoped and contained no §5.

The proposal was sound work. It belonged in §11, not §§2-8, and the author said so first.

## The eight steps, in order, and what each one catches

### 1. Update `l2-mechanisms.md` FIRST, and keep it unstaged

Behaviour changed without a spec change is undocumented behaviour. A new rule arrives
carrying the evidence that justifies it — the date, the entry, the P&L — exactly as every
existing rule does.

*Catches:* the change that is really a preference. If you cannot write the evidence
sentence, you do not have a rule yet.

### 2. Walk the document through with an agent

*Catches:* ambiguity, internal contradiction, and above all **rebuilding something already
retired**. §11.0's DO NOT IMPLEMENT list has already stopped four retired mechanisms from
being resurrected from stale prose elsewhere in the same file: §6's RR-to-DOL test, §5's
"already inside" clause, §7's >150-pt distance key, the 6h-session-block anchor, the §8
SL-cap gate extended to §6-proper, and the §5 re-entry churn guard.

The document contradicts itself in at least one live place, so expect it: §8's 08-14 walk
records a "fresh retrace tick 09:30:30", while §11's own §5 resolution says the tick must
be strictly AFTER 09:30:30. When you hit one, implement the more defensible reading and
record the contradiction prominently — do not silently pick one.

### 3. Replay the affected named dates on CURRENT code and record the BEFORE sequence

```bash
python scripts/replay_session.py --dates 2026-08-13
python scripts/replay_session.py --dates 2026-08-18 \
  --thesis '{"bias":"DOWN","regime":"TREND","confidence":"MEDIUM",
             "dol":{"level":"prev1_week_low","price":29533.5},
             "falsified_if":[{"type":"price_beyond","price":29900.0,"side":"above"}],
             "exhausted_if":[{"type":"price_beyond","price":29533.5,"side":"below"}]}'
```

`falsified_if` and `exhausted_if` are REQUIRED, not optional: plan death reads them, and
an empty list means the plan can only die at its DOL — which makes a §10.2 stress run
worthless. The oracle theses for every named case are already in
`agent/trader/named_cases.ORACLE_THESES`; use those rather than inventing new ones.

**THIS IS THE STEP PEOPLE SKIP AND IT IS THE ONE THAT MAKES STEP 7 MEANINGFUL.** Without
a recorded before, a changed number afterwards is unfalsifiable. The 2026-08-28 ladder
defect was found exactly here — the walkthrough had already passed, and recording 08-13's
sequence showed the binding moving backward toward price.

Record the whole sequence, not the P&L: **assert the BOUND ARTIFACT.** That same defect
produced *better* P&L (+130 vs the correct +93) from the wrong gap, and only inspecting
which artifact was bound revealed it.

### 4. Update `agent/trader/named_cases.py`

The registry, with era stamps if a knob moved. Every documented figure lives there once;
`test_named_cases.py` fails when the registry and the document disagree — in either
direction, and including a `pinned_by` test that has been renamed away.

### 5. Write the failing tests, reading expectations FROM the registry

Registry → tests → code, never code → tests. Code first makes the test match the
implementation instead of the specification, and that inversion is invisible afterwards.

### 6. Implement

New code lives in `agent/trader/` or `agent/facts/`. Knobs come from the §9 table at their
starting values: **if a change would alter a NUMBER it is deferred; if it would alter
BEHAVIOUR it is in scope.**

### 7. Re-run the named dates AND the §10.2 inverted-thesis stress. Explain every delta

This is a GATE, not a note. If a documented number moves and you cannot account for it,
**stop**.

- 08-13's +89.25 → +93.25 was acceptable because it is exactly 4.00 pts of entry-buffer
  change. **An unexplained 4.00 pts is a defect wearing the same clothes.**
- 08-12's +46.75 → +56.75 was acceptable because it is 6.00 (the 25-pt SL cap replacing a
  structural −31.00) plus 4.00 (the buffer trim), and nothing else.

The stress run matters because a rule that improves right-thesis days can quietly worsen
wrong-thesis ones. `test_gate_mechanisms.py` runs it and asserts §10.2's adverse band
(0..−70) still holds.

**Attempts: assert, never score.** Record attempts used/remaining and compare against each
case's documented count. They contribute NOTHING to the P&L arithmetic, and attempts-used
is a sharper change detector than P&L — a stopped-out attempt followed by a winner nets
the same as one clean entry, so a row reproducing the right P&L on a different attempt
count is a real discrepancy.

**Two tolerances are pre-accepted and must NOT be chased:** §6.2's ±1 cycle on 08-06, and
07-21's extra 09:38:00 close-verdict cycle. Both are in `ACCEPTED_TOLERANCES`.

### 8. Commit doc + registry + tests + code TOGETHER

A commit where they disagree ships drift by construction.

---

## Standing rules

**Never delete an invalidated case — mark it with its reason.** 07-23's studied entry
(invalid under the pinned 5m convention) and 08-21's +92.75 (look-ahead contaminated) are
both still recorded, marked. Deleting them loses the knowledge that they were considered
and why they failed, which is exactly what stops a future agent re-deriving them.

**Era-stamp on knob changes.** §6.2: "a knob change dated after a validation silently
invalidates that validation's entry set". Every registry row carries its era; a
pre-2026-08-22 P&L cannot be asserted as a target without an explained delta.

**The document's boundary:** WHAT a mechanism does and WHY, never HOW the Executor stores
facts. Implementation changes far faster than rules; that is how a specification rots.

**Identity is not existence.** An FVG's IDENTITY is its MIDDLE bar; its EXISTENCE is the
third bar's COMPLETION (`label(T+1) + timeframe`). Everything time-gated reads existence.
Gating on identity admits a 5m gap ten minutes early — that is look-ahead, and it is the
defect the §11 08-21 erratum documents.

**A validation gap is a finding, not a silence.** When the document does not record enough
to reproduce a figure, put it in `NO_DAY_REPRODUCTION` or `NO_ORACLE_THESIS` with the
reason. A gap nobody wrote down becomes a claim nobody checked.

## Known limits of the current harness

- `replay.py`'s window is **09:20 → 11:00** by construction. 08-18's documented DOL touch
  is at 11:01:26 and therefore cannot be booked; its ENTRY is asserted instead.
- §6, §7 and §8's episode-mode re-entry are implemented and tested as modules
  (`episode.py`, `extreme_reject.py`, `takeover.py`) and validated against the named cases
  over real tape, but are **not wired into the Executor's entry path**. Only §8's
  all-timeframe deeper-penetration scan and its conditional blacklist are live in
  `executor.py`. A day figure that needs a §6/§7 fire cannot be reproduced end to end yet.
- Which mechanism the Planner arms comes from §3's leg segmentation, upstream of every
  §4–§8 rule. On 08-14 it arms `fvg_negation_reversal` where §8's documented walk assumes
  `fvg_return_continuation`; no takeover rule can recover the documented sequence from
  there. If a named day's shape will not reproduce, check the ARMED MECHANISM first.
