# Post-T2 research brief — does price move past the T2 target, and should we keep part of the position?

Status: **research brief, nothing built.** Written 2026-09-27 for a follow-up agent. Read
`l2-target-selection.md` (esp. §3, §7, §7a) and `docs/entry-mechanism-change-protocol.md`
first — this is a TARGET / position-management question, and any rule it produces enters as
a §11-style CANDIDATE with evidence, not straight into code.

## 1. Why this came up

Plan 42 re-measured the named days (`l2-mechanisms.md` §11.3). Several documented winners are
much smaller today, and the cause is the TARGET, not the entries:

- 08-18: same §5 entry 09:40:58 @ 29763.25. Documented exit at the 09:20 DOL
  (`prev1_week_low` 29533.5, 11:01:26) = **+229.75**. T2 at the fill picked `prev4_day_low`
  29625.0 = +138.25 (plan 16, 000dfb7); since plan 40 (2da4d0b) the nearer
  `htf_week_running_low` 29631.0 = **+132.25** at 09:55:08.
- 08-12 / 08-14: exits earlier and closer than the documented walks.
- The O4 micro-SMT exit (5f71acc) closed **none** of these trades — it is not the driver.

These are hand-picked specification days, i.e. anecdotes, not a sample. They motivate the
question; they must not be used to fit the answer.

## 2. What is already known (do not re-derive)

- **T2 beat T1 on the corpus.** Nearest-first D1 at the fill: 12,966 pts vs T1's 11,814 over
  84 corpus dates (`l2-target-selection.md` §3; commit 000dfb7).
- **"Smarter ranking" was tried and closed.** T3/B8g "ranks better and selects worse"
  (`l2-target-selection.md` §4).
- **Tightening management hurts** (initial-target lookahead study, 2026-09-20,
  `agent/study/initial_target_lookahead.py`, write-up `agent/study/initial-target-lookahead.md`,
  30 days 08-07..09-17, lookahead entry, no stop): the plan-35 flip exit precedes T2 on 20/20
  winners and forfeits 1,184 pts; a stop moved to the initial target (action A) stops out
  before T2 on 11/20 winners. Policy totals: **T2-else-mark 4,023 > hold-to-13:00 mark 3,853**
  > A 2,912 > B 2,885 > flip-exit 2,717. Hence `INITIAL_TARGET_ACTION="record"`.
  Caveats: lookahead entry (MAE 0 on 21/30), no stop, small n.
- **Farther levels are already candidates:** plan 40 put unnested weekly/monthly extremes (and
  the running week/month) into T2 (`target.HTF_EXTREMES_IN_T2`, ON). Plan 41 lets the
  operator override the target live (`trade.py agent-target`).
- Live trades **2 contracts** today (`.env` `TRADING_CONTRACTS=2`), so a partial exit is
  mechanically available without changing size.

## 3. The three options and the current view

1. **A structural rule that selects farther targets "when relevant".** Sceptical: it is the
   question T3/B8g already lost, and the motivating days are selected on hindsight. Only as a
   pre-registered rule tested on the full corpus.
2. **Tighter management on near-target days.** Existing evidence (§2) points against it.
3. **Keep T2, but at T2 move the stop instead of exiting everything, with a follow-up target
   (a runner).** Most promising: it leaves T2's measured edge on the part that exits there.
   **Unknown:** how often and how far price continues past T2 before retracing. The 09-20
   study measured behaviour BEFORE T2, not after. The hold-to-13:00 result (3,853 < 4,023) says
   a FULL hold loses; it does not say whether a PARTIAL runner with a protective stop wins.

**Decision-grade question:** *after T2 is touched, what is the distribution of further move in
the trade's direction before price returns to (a) T2, (b) the entry (breakeven), within the
13:00 window — and which follow-up target, if any, is reached first?*

## 4. Proposed study (pre-register before running)

Corpus: the replay corpus the T1/T2 comparison used (84 dates) or the 66-session rig; state
which, and hold out a split (e.g. chronological 2/3 fit-free measurement, 1/3 confirmation).
No tuning on §11.3's named days.

For every trade that TOUCHES its T2 target (real Executor path, `scripts/replay_session.py` /
`agent/trader/replay.py`, cached theses, `ACT_TRADER_5M` unset):

1. Post-T2 excursion: max favourable excursion after the T2 touch until the first return to
   T2 and until the first return to entry; time to each; 13:00 mark.
2. Follow-up target candidates, each evaluated for "touched before the protective stop":
   next menu row beyond T2 (D2), the 09:20 DOL (now inert, still recorded as
   `would_have_killed`/`dol_reached`), the nearest farther HTF extreme (plan 40).
3. Policies (per 2-contract position; the non-T2-touching trades are unchanged by
   construction — report them separately to prove it):
   - P0 baseline: both contracts exit at T2 (today).
   - P1: 1 at T2, runner stop -> T2, runner target = D2.
   - P2: 1 at T2, runner stop -> entry (breakeven), runner target = D2.
   - P3: as P1/P2 but runner target = DOL / nearest farther HTF extreme.
   - P4: 1 at T2, runner held to 13:00 mark with stop at T2 (upper bound on "let it run").
   Tick-level (1s) fills, the same stop/fill model as the Executor (`agent/trader/order_sim.py`).
4. Report per policy: total pts, per-day distribution (not only totals), worst day, number of
   days better/worse than P0, and the same for the held-out split. Attempts are unaffected
   (the plan dies on T2 today — note that a runner means the plan must keep MANAGING the
   runner after `target_reached` without taking new entries).

Existing tooling to start from (read before writing new code): `agent/study/position_policy.py`,
`policy_baselines.py`, `targets_for_policy.py`, `scripts/report_position_policy.py`,
`agent/study/initial_target_lookahead.py`.

## 5. Adjacent question raised (separate A/B, not part of this study)

Whether O4 (`micro_smt_exit`, 5f71acc) costs profit: it did not fire on the named days; on the
2026-09-24 live replay it closed a short for +93.25 vs +82.75 at the initial target. A
corpus A/B with O4 off exists as `scripts/ab_micro_smt.py`. Run it separately — mixing it with
this study confounds the two exit rules.

## 6. Constraints for whoever picks this up

- Research only: do not change `agent/trader/target.py`, the Executor or live config.
- One heavy replay job at a time on this laptop (see the machine-memory note); a corpus replay
  is ~64 s/date after the 2026-09-27 perf work.
- Run `-m slow` (64 tests, ~21 min, must stay green) before trusting any replay number.
- Output: a write-up next to this file (`post-t2-results.md`) with the tables above and a
  recommendation; if it recommends a rule, draft it as a CANDIDATE for `l2-target-selection.md`.
