# l2-targets.md — target selection at the fill

**Status: study output, not implemented.** Nothing in this document runs, and §2's rules did
NOT pass step 0 of `docs/entry-mechanism-change-protocol.md` — see §9. It is the phase-4
write-up of cycle 4, and it exists so that a phase-5 decision to implement any of it can be
taken on evidence rather than on memory.

**Scope, narrowed 2026-09-04: the one-off target decision taken shortly after the entry fill,
and nothing beyond it.** Anything that keeps acting while a position is open — mid-move target
revision, exit ladders, early exit on a counterpart event — is a sustained effort belonging to
a later Executor cycle. See §7.

**This document is a sibling to `l2-mechanisms.md` and must never be merged into it.**
`l2-mechanisms.md` is scoped to *entry* mechanisms; this one is scoped to *where the move is
going once you are in*. They answer different questions and rot differently.

Every figure below is pinned by `agent/study/named_cases.py` and its drift test. A number
edited here alone will fail `agent/study/test_named_cases.py`.

---

## 1. What the cycle asked

Given a fill on the 09:30 move, name the pool the move will be drawn to.

The study assumed the direction and the entry were correct — deliberately, and with total
lookahead — so that target selection could be isolated from everything upstream of it. The
design, the definitions and the discipline are in `.agents/plans/26.cycle4-target-selection.md`;
the corpus is in `.agents/label-corpus/`; the searches are in plans 27, 28 and 29.

The dependent variable is the **draw**: the last eligible pool the move reached before it
turned. Not where it stopped — overshoot past the draw is normal and is recorded separately.

---

## 2. The rules

Two rules, composed. They are **not** equally trustworthy and §6 says so in detail.

### 2.1 B9 — the confidence rule

> **Decline to set a pool-based target unless an eligible pool sits within 2.0 × avg_range_1h
> of the move's origin.**

Eligible means: ahead of the origin in the move's direction, in that instrument's own price
space, and not already swept.

> **NOT A RULE YET — see §9.** "Decline" has three readings (veto the entry, take it with no
> hard TP, take it with a fallback target) and the threshold is measured from an anchor
> production does not have. Step 0 of the change protocol rejects it as written.

### 2.2 B8g — the selection rule

> **Otherwise, walk the eligible pool stack outward from the origin and name the last pool
> before a gap wider than about 1.5 × avg_range_1h.**

Equivalently, as the hazard it was fitted as: the probability that the move stops at a pool it
has reached is governed by the distance to the *next* pool, not by how far it has already
travelled.

| gap to the next pool | P(stop here), MNQ | MES |
|---|---|---|
| ≤ 0.5 × avg_range_1h | 0.10 – 0.42 | 0.16 – 0.42 |
| 0.5 – 1.5 × | 0.15 – 0.62 | 0.17 – 0.62 |
| > 1.5 × | 0.48 – 0.85 | 0.62 – 0.79 |

**Distance from the origin earns no place in the rule.** Adding it changes nothing on MNQ and
costs accuracy on MES. This reverses the framing the study started with: "how far will it go"
is the wrong question, and "where does the structure thin out" is the right one.

---

## 3. Evidence

### 3.1 The holdout — the number to quote

21 sessions were fixed on 2026-09-03 before any rule was searched for, and read once, on
2026-09-04. The hazard table was fitted on the 55 discovery segments and applied blind.

| | coverage | P(a pool-based answer existed) | **act accuracy** |
|---|---|---|---|
| MNQ, rule acts | 12/21 (57%) | **100%** | **50.0%** (6/12) |
| MNQ, act on everything | 21/21 | 76% | 38.1% |
| MES, rule acts | 13/18 (72%) | **100%** | **53.9%** (7/13) |
| MES, act on everything | 18/18 | 83% | 44.4% |

**`act_accuracy` is the honest production number**: of every session the rule acts on, the share
where it names the pool the move was actually drawn to. An unexplained session inside the acted
set counts as a miss, because in production a target would have been named there and been
wrong.

Against the baselines pre-registered in plan 29 §B.1, on the same unseen sessions:

| act-equivalent accuracy | MNQ | MES |
|---|---|---|
| always the nearest pool | 29% | 22% |
| furthest pool within 2.0 × avg_range_1h | 29% | 44% |
| furthest pool within 3.0 × avg_range_1h | 29% | 33% |
| **B9 + B8g composed** | **50%** | **54%** |

The composed rule beat every pre-registered reference on data it had never seen.

### 3.2 Discovery, and the gap between the two

On the 63 discovery sessions, leave-one-session-out with session-clustered intervals:

- B8g named the draw on **30 of 48** labelled MNQ segments (62.5%, Brier 0.1746) and **30 of 44**
  MES (68.2%, Brier 0.1770).
- Against a distance-only hazard: **+20.8 pp [+10.4, +31.2]** on MNQ and **+18.2 pp
  [+9.1, +27.3]** on MES.
- Composed with B9 at the 2.0 threshold, act accuracy was **64.7%** (MNQ) and **67.6%** (MES),
  against 50.8% and 50.0% for acting unconditionally.

**The selection rule was optimistic by roughly 15 points.** Discovery said 65/68, the holdout
said 50/54. The shortfall is *not* statistically distinguishable — 90% binomial intervals of
[25%, 75%] and [31%, 77%] both contain the discovery estimate, because 12 and 13 acted sessions
carry almost no power — but the honest expectation for production is **low-to-mid 50s act
accuracy at roughly 60% coverage**, not the discovery figure.

Any document quoting 62–68% is quoting a fitted number.

### 3.3 Why the chaining effect is believed

The obvious objection to "wide gap ⇒ stop" is that it is mechanical: a move stopping at an
arbitrary distance lands in whichever interval is widest, so a rising hazard in gap width is
what pure chance predicts.

A rank-stratified permutation test settles it. Shuffling the stop/continue labels within each
rank stratum 2000 times holds the rank distribution fixed and breaks only the gap-to-stop link:

| | stoppers' median gap ahead | continuers' | observed − null | p |
|---|---|---|---|---|
| MNQ | 1.72 / 1.45 / 1.77 at ranks 0/1/2 | 0.69 / 0.30 / 0.37 | +1.34 vs +0.13 | **< 0.0005** |
| MES | 1.96 / 1.40 at ranks 0/1 | 0.60 / 0.38 | +1.30 vs +0.10 | **< 0.0005** |

Consistent at every rank, on both instruments.

---

## 4. What was refuted

Seven hypotheses were tested and failed. They are recorded because the two survivors are only
believable in their company, and because each one will otherwise be proposed again.

| hypothesis | verdict |
|---|---|
| **Some pool classes draw harder than others** (week > day > session, etc.) | **Refuted twice.** No marginal effect once distance is held fixed — every class delta inside its interval in the bucket where the data lives. And none in interaction with the gap: adding tier costs 4.2 pp on MNQ and adds nothing on MES. The inequality between pools is real but entirely *geometric* |
| **MES anchors the pair; MNQ's target follows from MES's structure** | **Refuted.** Adding the counterpart's gap at the translated price costs **12.5 pp** on MNQ and 4.5 pp on MES. Its two supports — MES scoring higher, MES halting nearer its own pools — were both statements about MES explaining MES |
| **A halt between two pools is explained by the other asset reaching one of its own** | **Refuted.** Lift of +2, −0, +2 pp at moderate thresholds; only +13 pp in the thinnest cell (n=19). The two instruments' halt-at-pool events are close to independent |
| **The unexplained sessions are explained by the counterpart** | **Refuted, and reversed.** P(MES labelled) is 70% overall but **47%** given MNQ unexplained; P(MNQ labelled) is 76% overall but **58%** given MES unexplained. 8 doubly-unexplained segments against 4.5 under independence. "Unexplained" is a property of the *session*, not the instrument |
| **FVG edges fill the coverage gap** | **Refuted as a fix, confirmed as structure.** FVG edges sit inside **0 of 216** small named gaps and 13% / 8% of large ones — real structure exactly where the named stack thins. But on unexplained sessions the halt is a median 3.15 × avg_1h from the nearest edge, further than the named pool that already failed, and 10 of 15 unexplained MNQ segments have no eligible edge at all |
| **Move-end can be defined by a retracement fraction** | Refuted in phase 1. A tiered give-back rule cut median primary extent from 253 to 185 points and collapsed eleven primaries into a single bar |
| **The draw can be identified by proximity to move-end** | Rejected by design, then confirmed: only 2 of 64 MNQ draws were identified by a near-miss, and the move overshoots its draw by a median 0.7 × avg_range_1h |

Two nulls of my own were also discarded mid-study: a length-biased "inspection paradox" test
contaminated by unreachable tail gaps, and Null A, which cannot fail for a monotone move
because the last-reached pool is by construction the reached pool nearest the extreme.

---

## 5. Named-case registry

Configurations too rare to carry a rate. Each is recorded with its n printed beside it, and
must be quoted that way.

### 5.1 The day-then-week chain — 4 of 4

A day- or session-tier pool within 3 × avg_range_1h with a week-tier pool no more than
0.5 × beyond it. The configuration occurs on **4 of 48** MNQ discovery segments, and the move
continued to the week pool on **all four**. MES: 7 of 44, continued on 5.

The mechanism is B8g's, in its sharpest form. The rate is not a rate — n=4.

### 5.2 2026-08-13 — overshoot is not a further target

The move ran 29862.75 → 30267.00. The highest named MNQ pool is 30073.25
(`prev1_week_high` = `prev6_day_high`, one price under two names), reached at 09:46 with
**193.75 points** of overshoot beyond it. MES independently drew to its own `prev1_week_high`
at 7820.25.

This is the case that established two rules: that the draw is where the move was *drawn to*
rather than where it ended, and that each instrument is labelled in its own price space.

### 5.3 2026-08-11 — the cross-instrument asymmetry

MNQ swept `asia(cur)_low` at 29666.00 around 09:46, running **30 points** through it, while MES
fell **5.0 points** short of its own equivalent. Both reversed up from about 09:51.

The measured shortfall is 0.51 × MES's `avg_range_1h`, so the settled tolerance band of 0.15 ×
does not register it. Recorded as a measurement, not as a verdict on the band.

### 5.4 The counterpart-divergence veto — carried out unmeasured

The leading asset wicks past a mid and closes back inside while the lagging asset sweeps, and
the chain does not extend. Proposed 2026-09-04, never built: it is a *switching* rule, its
condition does not exist at the fill, and the cycle was narrowed before it was reached. It
leaves with its evidence, not as a refuted idea.

---

## 6. The two rules deserve different trust

This is the most important operational finding and it is easy to lose in the tables.

**B9 generalised perfectly.** `P(a pool-based answer existed | the rule acted)` was **100% on
both instruments** in the holdout — 12 of 12 and 13 of 13. Trained on discovery, it excluded
every unexplainable held-out session without a single miss, and its lift survived intact
(MNQ 38.1% → 50.0%, MES 44.4% → 53.9%).

**B8g did not.** Its accuracy fell about 15 points out of sample. It still beats every
alternative, but its discovery figure was not its true rate.

**Consequence for a phase-5 decision.** B9 is a gate that declines to act: its failure mode is
a missed opportunity. B8g names a price: its failure mode is taking profit in the wrong place.
**If anything ships, B9 should ship first and alone.** It requires no new detectors — the
candidate universe and `avg_range_1h` already exist — and it improves on today's behaviour by
identifying, before the fact, the roughly quarter of sessions on which no pool-based target is
available at all.

Nothing here has been through `docs/entry-mechanism-change-protocol.md`. Its step 0 — *is this
a RULE yet* — is the gate, and the inverted-thesis check applies to both rules.

---

## 7. Deferred to a later Executor cycle

Carried out of cycle 4 by the 2026-09-04 scope narrowing, with their evidence intact:

- **Mid-move target revision** — the switching rules of `26.cycle4-target-selection.md` §7.
- **The counterpart-divergence veto** — §5.4 above.
- **The tiered give-back exit ladder** — spec §7.2, which is a good exit policy and was a bad
  move-end definition.
- **Early exit when the counterpart reaches a pool.**

All four are continuing efforts while a position is open, which is a different problem from a
one-off decision at the fill.

---

## 8. Reproducing every figure

```bash
python scripts/build_session_skeleton.py        # phase 1: what the 09:30 move was
python scripts/build_label_corpus.py            # phase 2: what it was drawn to
python scripts/report_baseline.py               # phase 3 stage A: the bar
python scripts/report_hazard.py                 # phase 3 stage B: the rules
python scripts/report_holdout.py --spend-the-holdout   # stage C: already spent
```

The corpus is committed under `.agents/label-corpus/`, the results under
`.agents/rule-search/`. The holdout split is `holdout.json`, fixed before any search and
enforced by `agent/study/test_holdout.py`.

**The holdout is spent.** Any further tuning needs a new one, and there is no more
2026-05 → 08 data to take it from.

---

## 9. CANDIDATE — B9 as an upstream proposal, not an L2 rule

**Status: CANDIDATE. Not implemented, and must not be implemented from this text.**

Recorded 2026-09-05 after running step 0 of `docs/entry-mechanism-change-protocol.md` against
B9. Step 0 asks whether a proposal is a RULE yet; B9 is not, on four counts. This section is
the protocol's prescribed home for a proposal carrying its evidence and its open questions —
the tier before §§2-8, not a weaker version of them.

### 9.1 What is proposed

That when no eligible named pool sits within a small multiple of `avg_range_1h`, the system
should recognise that **no pool-based target is available** rather than naming one anyway.

### 9.2 The evidence, which is good

On the 21 held-out sessions, the gate admitted only sessions that had a pool-based answer:
`P(labelled | acted)` = **100% on both instruments**, 12 of 12 and 13 of 13, trained on
discovery and applied blind. Acting only on admitted sessions lifted act accuracy from 38.1%
to 50.0% (MNQ) and 44.4% to 53.9% (MES). On roughly a quarter of sessions no named pool is
drawn to at all, and those sessions are identifiable in advance from one forward-computable
number.

### 9.3 Why it is not a rule yet

**(a) The threshold does not transfer — units mismatch.** The study measured `d0` from the
**move origin**, a lookahead-derived construct. `derive_facts._dol_menu` measures
`abs(price - now_price)`, from the price at the call. The study measured the difference
directly: MNQ's median distance to the draw falls from **210.5 points at the origin to 127.8 at
+2 minutes**. So "2.0 × avg_range_1h from the origin" is roughly 1.2× from the fill. Shipping
2.0 would be a different rule at an unvalidated threshold. **This is the blocking one**, and it
is a defect in how the study was framed, not in the finding.

**(b) It is a choice between existing rules.** `DOL_PROJECTION_RATIO` already detects B9's
exact condition — *"when a direction has NO named pool inside the band"* — and already answers
it, by emitting a stretch-gated synthetic `projection_up` / `projection_down` draw at the day
extreme, unswept by construction, expressly so that L2 has a real TP. B9 proposes declining
where the system currently projects. Choosing between those is a rule decision.

**(c) Hard exclusion was already considered and rejected.** `DOL_BAND_MAX_RATIO`'s own note:
entries beyond the band are *"tagged FAR (rendered + audit-warned) …, NOT excluded: a far pool
is sometimes the honest answer, and hard exclusion would silently rewrite no-liquidity
semantics."* B9's decline is that exclusion. Not on the DO NOT IMPLEMENT list, but rejected in
the same spirit, for a reason this study never addressed.

**(d) Wrong owner.** `l2-mechanisms.md` §11.0 is explicit that the draw floor is *"Not an L2
change at all — it lands in `derive_facts.DOL_MIN_DRAW_RATIO`, shared with the L1/thesis
pipeline. Different owner; propose upstream rather than editing from L2."*

**And an ambiguity this document previously glossed:** "decline to set a pool-based target" has
three readings — veto the entry, take it with no hard TP, or take it with a fallback target.
Three mechanisms, three P&L profiles. §2.1 does not say which, and two implementers would not
agree.

### 9.4 What would resolve it

| blocker | what it needs |
|---|---|
| (a) units | Stage B re-derived with `d0` measured from the entry instant rather than the origin. Cannot be validated the way the original was — the holdout is spent |
| (b) projection vs decline | A decision, informed by how the projection draw actually performs on the sessions B9 would have declined |
| (c) hard exclusion | An answer to the no-liquidity-semantics objection, which this study did not consider |
| (d) owner | Route it as a proposal to the L1/thesis pipeline, not an L2 edit |

### 9.5 The honest framing

B9 is **evidence bearing on an already-parameterised decision**, not a new mechanism. The
system has already chosen what to do when no pool is near: project. This study says that
choice is being made on roughly a quarter of sessions, and that those sessions are predictable.
That belongs upstream as input to the projection policy.

**DO NOT** implement a hard decline from this text.
