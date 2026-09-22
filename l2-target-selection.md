# l2-target-selection.md — target selection measured on production's own menu

**Status: T2 SHIPPED (plan 16, 2026-09-13); T1 retired; T3 still closed.** This document
began as measurement output with nothing in production. §3's result was then adopted:
`agent/trader/target.py` runs T2 — `build_menus`'s nearest-first `D1`, re-anchored at the
entry fill — and it is the take-profit. T1 (the 09:20 pick) is gone from that role, and the
09:20 DOL is inert end to end (no take-profit, no entry floor, no plan death). T3/B8g remains
CLOSED on §4's evidence and is still study-only.

**What §3's +1,152 pts does and does not license.** It compares T1 against T2 — the SAME
nearest-first rule at two instants — with total lookahead, no stop and no losing side (§2).
It is therefore evidence for moving the instant, and NOT evidence that nearest-first beats a
hand-picked level. `agent/trader/named_cases.py` carries documented figures whose DOL was
chosen by hand from `l2-mechanisms.md`'s walks; those are a different selector, and the
deltas against them are recorded per case rather than treated as regressions.

**Sibling to `l2-targets.md`, and it must never be merged into it.** `l2-targets.md` is
cycle 4's write-up, whose universe was *every named level measured from the move origin*.
This document is the same question asked on **`build_menus`'s own rows, anchored at a clock
instant** — the universe mismatch `l2-targets.md` records as a MUST-FIX. Same subject,
different universe, and the answers differ enough that keeping them apart matters.

**Figures here are NOT pinned by `agent/study/named_cases.py`.** The per-date harness is
committed (`agent/study/target_offline.py`); the 84-date sweep drivers were deliberately
kept as scratch and are gone. This document is therefore the record, not a derivation — a
re-measurement must rebuild the driver.

---

## 1. Scope, fixed before measuring

Selection is **menu-only** (the pick is always a `build_menus` row) and the latest honest
instant is the **entry fill**. Direction and entry are assumed correct, with total lookahead,
so selection is isolated from everything upstream — the same discipline `l2-targets.md` §1
used.

Three selectors, all emitted from one code path:

| | selector | instant |
|---|---|---|
| **T1** | nearest-first `D1` | 09:20 L1 boundary |
| **T2** | nearest-first `D1` | the entry fill |
| **T3** | cycle-4 B8g hazard argmax over T2's rows | the entry fill |

---

## 2. How it was measured, and what that measurement cannot see

84 corpus dates (`.agents/label-corpus/`), MNQ primary segments, 1m facts. Entry = the best
possible fill in the move's direction before its extreme. Capture is measured **from the
fill**; targets are anchored on `now_price` at the decision instant. Perfect-exit ceiling
= **31,867 pts** (median move 306). B8g is refit per date with that date excluded.

**Four limits, all load-bearing:**

- **No stop and no losing side.** A missed target scores 0, never a loss. Max adverse
  excursion is *identically zero* because a perfect fill has no drawdown by construction —
  so this measurement structurally cannot see stop risk. **None of these numbers is P&L.**
- The optimal-fill assumption flatters every selector equally, but shifts absolute
  distances; a realistic fill would change the magnitudes.
- Direction is assumed correct on all 84 dates.
- Oracle figures are lookahead upper bounds and are unachievable by construction.

`regression.py` is the only thing that can settle any of this.

---

## 3. What each selector is worth

| selector | captured | % of ceiling |
|---|---|---|
| T1 — `D1` at 09:20 | 11,814 | 37.1% |
| **T2 — `D1` at the fill** | **12,966** | **40.7%** |
| T3 — B8g at the fill | 12,553 | 39.4% |
| ORACLE — perfect menu ranking | 16,486 | 51.7% |

Perfect ranking is worth **at most 3,520 pts (11% of the ceiling)**, and all of it sits in
the 56 dates where a menu entry was reachable — the oracle gains **exactly zero** on the
other 28.

---

## 4. B8g — CLOSED

**B8g ranks better and selects worse.** It names the drawn-to pool far more often — draw-hits
**44.0% vs 29.8%** (discordant pairs 18–6, sign test p=0.023) — and is closer to the turn on
21 of the 31 dates where it differs (p=0.071, median gain 28.8 pts). It still captures
**413 fewer points** than plain nearest-first, because it aims farther and therefore fills
less often (60.8% vs 67.1%).

Accuracy against the draw label and captured points are different objectives. Where they
conflict, points decide. **Do not implement B8g.** Its implementation is kept in
`agent/study/target_offline.py` so this result stays reproducible.

The draw sits at **rank 0 (44.6%) or rank 1 (44.6%) — 89.3% combined**, mean rank 0.73, on
production's own menu. Cycle 4's all-levels universe reported mean rank 1.30; production's
filters have already done most of the work, and what remains is close to a D1/D2 coin flip.

---

## 5. Coverage — a third of sessions, and it is not a ranking problem

| | dates | production's behaviour |
|---|---|---|
| menu empty at 09:20 | **5 (6.0%)** | **NEUTRAL — no trade** |
| menu valid, `D1` never reached | **23 (27.4%)** | enters with a DOL that never fills |

The empty-menu case is handled deliberately: a directional thesis without a DOL fails
`SYN_DIRECTIONAL_MISSING_DOL`, and the **no-liquidity override** downgrades the bias to
NEUTRAL with a LOW confidence ceiling when the direction's menu is empty
(`validate_contracts.py`, mirrored in `run_agent.py`). All 5 are UP days; 4 sit within
1.21× avg_1h of the all-time high. Reviewed 2026-09-07 and **kept as-is**.

The 27% is a different failure — the target was too far and the move fizzled (median D1
distance 318 pts, median shortfall 109). No ranking rule reaches it. Two examples:
2026-06-23 (every menu row FAR, `D1` 5.57× away, move 368 pts short) and 2026-05-13
(`D1` BAND at 2.44×, missed by **19.00 points**).

`agent/trader/shadow_ledger.py` was built to price the NEUTRAL days empirically and **has
never been wired in** — no callers, no entries on disk.

---

## 6. No level family should be added to the menu

The menu's universe is six families: `asia(cur)_*`, `london(cur)_*`, `prevN_day_*`,
`prevN_week_*`. Structurally absent: `TDO`/`TWO` (side hardcoded `None`), `daily_mid`/
`weekly_mid` (not in `bundle.levels`), FVG edges (a separate object never passed to
`_dol_menu`), and price discovery (only via the gated projection).

Adding each family to the menu, order-independent:

| added | Δ ORACLE | Δ nearest-first |
|---|---|---|
| inside the proximity floor | +59 | **−670** |
| swept / depleted | +2,208 | **−1,856** |
| nested (P1-suppressed) | +5,381 | **−4,694** |
| `TDO` / `TWO` | +1,648 | **−1,185** |
| daily / weekly mid | +2,551 | **−3,251** |
| projection, unconditional | +1,898 | −120 |
| HTF FVG edges | +8,340 | **−7,606** |
| everything | +9,813 | **−9,018** |

**Every family raises the oracle and lowers the rule.** The full-facts oracle reaches 26,299
(82.5%) against the menu's 16,486 (51.7%), but a deterministic rule falls from 40.7% to
12.4% of ceiling. The filters are not a coverage bug — they are what makes nearest-first
work, and P1 suppression is the most load-bearing of them.

`TDO`, `TWO` and the mids supply the oracle's best pick on **zero of 84 dates**. The FVG
figure is a density artifact: ~39 zones × 2 edges per date is near a price continuum, so an
oracle finds an edge near almost any turn. Cycle 4 tested FVG edges and refuted them; this
is the same conclusion by another route.

**The projection's stretch gate was tested separately** — it suppresses a projection no BAND
pool covers on 21 of 84 dates. Un-gating scores +356 for nearest-first, but only **6 of the
21 were ever reached**, one date supplies 479.5 of the gain, and several of the rest are
sessions where production correctly stands NEUTRAL. Rejected: the gain is small, fragile,
and overstated in exactly the direction the no-stop proxy is known to overstate.

---

## 7. CANDIDATE — deterministic selection at the fill

**Not a rule yet.** Step 0 of `docs/entry-mechanism-change-protocol.md` applies.

Two changes, of very different size:

- **(a) deterministic nearest-first replaces L1's DOL choice.** Today the DOL is an LLM pick;
  menu membership is *telemetry only* in the validator and the schema enum admits any named
  level plus the projections, so L1 may pick off-menu — and does: it took the menu's `D1` on
  **15 of 28** recorded theses (54%). (a) therefore *narrows* the reachable universe, and
  what that costs is **unmeasured** — the oracle in §3 is a menu oracle.
- **(b) re-anchor the menu at the entry fill.** A timing change; the rule is byte-identical.
  Worth **+1,152 pts (+8.9%)**, T2 over T1. Needs an explicit rule for an empty re-anchored
  menu — keep the 09:20 DOL rather than voiding a live plan (measured: 1 day gained, 1 lost).

The +1,152 is (b) *given* (a). Production as it stands — L1 picking at 09:20 — was never
scored for capture, so neither figure is "versus production today".

Both need a `regression.py` A/B before implementation. Do not call L1 at the fill: recorded
call latency is 16.3–103.9 s, and `thesis_cache.py` records five identical calls yielding
three theses with two different DOLs.

---

## 7a. Unnested weekly / monthly extremes as T2 pools — RULE, flag ON (plan 40)

**Status: implemented; `agent/trader/target.HTF_EXTREMES_IN_T2 = True` by operator
decision (2026-09-22), taken without the full pre-registered adoption rule (see
"Adoption rule") and without the Wave-4 registry era. Q1–Q5 were pinned by the operator
the same day, so this is a rule rather than a CANDIDATE. `False` restores the previous
T2 exactly. Code:
`agent/facts/htf_extremes.py` (pure), `agent/facts/htf_source.py` (disk),
`derive_facts._dol_menu(extra_pools=)`, `target.select_target(htf=)`.

**Motivating case — 2026-09-21, oracle UP.** L1's DOL was `week_high` 30277.25; at the
09:35:01 / 09:37:00 fills the menu held no BAND pool, so T2 was `projection_up` 30338.25,
reached 09:44:59 (+74.50), after which the plan was dead. The day ran to the August high
30639.50 (touched 11:50:49). With the rule on, both fills target that high.

**The rule.**
- **Levels.** For every COMPLETED ISO week and calendar month since the per-asset start
  (MNQ 2026-06-16, MES 2026-06-11 — trade dates), the period's high and low, from the
  1m parquet (both paths read the same file family: live `general/live`, replay the
  per-contract main era). Trade date = ts + 7h; week = ISO week of the trade date; month =
  calendar month of the trade date. Maintenance bars (16:55–18:00) are dropped. Computed
  once, as of the session open; nothing at or after it is read.
- **Unnested.** A level is dropped once any later bar trades STRICTLY beyond it (an equal
  print does not). Intraday, bars in [open, fill) prune the same way.
- **Running week and month (Q5).** Their high and low at the fill = max/min of the part
  before the open (1m parquet) and today's bars up to the fill. They pass the same filters,
  so an extreme price is still making sits inside the draw floor and is filtered out.
- **Dedupe (Q3).** Equal prices on one side are one row: month over week, then most
  recent; the others are aliases. A row equal to a named `bundle.levels` price on the same
  side is dropped — the named level's swept/suppressed treatment governs that price.
- **Eligibility.** Exactly the menu's: correct side, draw floor max(5, 1.0 x avg_1h),
  BAND/FAR tag, nearest-first. Names `htf_week_high_YYYYMMDD` (trade date of the extreme
  bar), `htf_month_high_YYYYMM`, `htf_week_running_high`, `htf_month_running_high`.
- **Projection precedence (Q1 = P1, refined).** The projection draw is offered only when
  a direction has no BAND named pool AND no HTF row on that side that PASSED THE DRAW
  FLOOR. Consequence, measured on 07-31: once the projection is suppressed, D1 is the
  nearest remaining row, which may be a FAR named pool rather than the HTF row.
- **Scope (Q4).** Executor T2 at the fill only. L1's menu, predicates, P1 evidence, S1
  text and the thesis-cache key are unchanged. The initial target (`level_universe`)
  carries the rows flagged `htf: True`, but the selector draws from none of their
  families, so on 09-21 it stays `synthetic_85pct` (30581.30 / 30583.14, record only).
- **Failure.** A list that cannot be loaded (missing or stale 1m file) is non-fatal: the
  run folder's `htf_extremes.json` records why and T2 selects exactly as before.

**Verified during execution.** The existing `week_high` / `week_low` are NOT DOL-menu
pools: they are `bundle.week_hi` / `week_lo`, outside `bundle.levels`, so `_dol_menu`
could never pick them; only `level_universe` lists them. The running week rows are
therefore new T2 candidates, not duplicates of an existing one.

**The 09-21 list (MNQ, as of the 2026-09-20 18:00 open, 2026-12 era).** Highs 31273.75
(June month, alias the Jun 16 week), 31267.50, 30899.75, 30855.50 (July month, formed
Jun 30 20:00 = trade date Jul 1), 30639.50 (August month, alias the Aug 17 week),
30111.50, 30064.50, 29993.50; lows 27499.75 (July month), 28612.75 (August month),
29052.75. The three highs under 30277.25 are pruned by 09-21's own overnight bars before
the first fill. Pinned in `named_cases.HTF_0921_MNQ` / `HTF_0921_MES`.

**Measured (2026-09-22 A/B, 13:00 window; A = flag off, B1 = flag on + P1).**
- 09-21 oracle UP: **+61.50 -> +362.75 (+301.25)**; T2 `projection_up` 30338.25 ->
  `htf_month_high_202608` 30639.50 at both fills; same fills, same stop-out, 1 attempt.
- Oracle theses (`named_cases.ORACLE_THESES`, 18 cases): Δ 0.00 on every case. One pick
  changed without a P&L change: `sec8-0731-deepest`'s 2nd/3rd fills took
  `prev6_day_high` 29283.0 instead of `projection_up` (suppressed by FAR HTF highs); all
  three attempts stopped out within six minutes either way.
- Real-L1 cached theses: 14 warm dates since 2026-05-18, Δ 0.00 on all, picks identical.
  15 dates had stale caches (not re-seeded: no model calls).
- §10.2 inverted stress (4 cases): identical in both arms (−21.75, +69.00, −23.25,
  +38.50); the 0..−70 band holds.
- Every delta traces to a changed pick; zero unexplained.

**Adoption rule (pre-registered) and verdict.** Adopt only if summed Δ >= 0 on the real-L1
dates AND on the oracle dates, the §10.2 band holds, zero unexplained deltas, and `-m slow`
is green. On the A/B above the first three hold (real Δ 0.00, oracle Δ +301.25). `-m slow`
is NOT green at HEAD `e65e4fa` itself (24 failed / 13 errors on an untouched export of
HEAD — pre-existing, not caused by this change), and the post-change slow run was stopped
by operator instruction before it finished. The operator then flipped the flag ON
directly; the Wave-4 registry era and named-case pins were NOT added. The A/B figures
are from runs completed before those instructions and are not re-verified.

**Counter-evidence on file.** §6 found that every family added to the menu lowered
nearest-first capture. HTF unnested extremes were not among them and are sparse (≤ 8 per
side here), which is why they were measured rather than rejected on §6's prior; the
offline corpus measurement (`target_offline.htf_extra_pools`) has not been run.

---

## 8. Two corrections to earlier readings in this line of work

Both came from an 11-date hand-picked sample and did not survive the 84-date sweep:

- *"T2 is dominated by T1; re-anchoring cannot help."* **False.** T1 and T2 agree on 82% of
  dates and T2 is better on both error and capture.
- *"The projection is never offered."* **False.** It is offered on 13.1% of dates; the
  hand-picked days were large movers, which have high `stretch_mult` by construction.

Selecting dates by outcome, or by move size, biases every downstream number. The 84-date
population is the corpus's own primaries, with bucket membership used only as a label.

---

## 9. Running it

```
python -m scripts.report_target_offline --date 2026-08-24 --time 09:30:00 \
    --direction DOWN --ticker MNQ [--source 1s|1m] [--json out.json]

python -m scripts.report_target_offline --date 2026-08-24 --time 09:30:00 \
    --direction DOWN --select-only [--selector nearest|b8g]
```

`--select-only` is the standalone decision path: menu-only, instant as an argument, and it
reads no bar at or after that instant — asserted by
`test_select_target_is_identical_with_the_future_deleted`, which reruns it against a
truncated bar frame and requires identical output. The full report adds a clearly-marked
lookahead block. `build_menus` is called unmodified; there is no second copy of the rule.
MES raises — the DOL menu is MNQ-only.
