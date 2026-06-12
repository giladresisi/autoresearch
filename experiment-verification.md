# Experiment Verification — GIL-23 ATH-seed root fix + direction diagnosability

Worktree: `C:\Users\gilad\projects\auto-co-trader\ath-seed-direction-fix`
Run dirs (1s, 2026-06-11):
- CHANGE: `regression\sessions\2026-06-11\13-25-28`
- BASELINE: `regression\sessions\2026-06-11\_baseline_run`

> **Verification caveat (from GIL-23 / feature.md, confirmed true here):** the 1s backtest baseline
> already seeds the true ATH `30807` (its 60-day parquet window contains the 05-26→06-04 high) and
> already chooses `up` at both occurrences. The standard baseline-vs-change A/B therefore *cannot*
> reproduce the live `down`→`up` flip — it is a no-op by design. The real pass/fail for the seed fix
> lives in GIL-23's Stage-B unit tests (reported as all 3 passed), not in this A/B. What this
> verification asserts is: (a) the change did **not regress** ex1/ex2 to `down`, and (b) the new
> `direction_reason` diagnostic fields are present and sane at both timestamps.

## Verdict summary
| occurrence | date | time (ET) | verdict | one-line reason |
|-----|------------|----------|------------------------------|-----------------|
| ex1 | 2026-06-11 | 13:43:42 | INCONCLUSIVE — no regression | Baseline already `up` (no live `down` to flip); change stays `up` and now logs session_ath=30807, recovery_gap=0.0528, is_false_pos_recovery=true |
| ex2 | 2026-06-11 | 15:10:00 | INCONCLUSIVE — no regression | Baseline already `up`; change stays `up` and now logs session_ath=30807, recovery_gap≈0.049, is_false_pos_recovery=true |

INCONCLUSIVE here is the **expected** outcome per the caveat (premise absent in replay), and is explicitly **not a failure**. Both occurrences also pass the regression-safety bar: direction unchanged + diagnostics added.

## Per-occurrence detail

### ex1 — 2026-06-11, window 13:35–13:52 ET
The documented `current behavior` (live `new-hypothesis down` @13:43:42, recovery guard off because session_ath=29011.25 ≤ price) is **not present in the baseline replay** — the baseline already chooses `up`, so there is no `down` for the change to flip. The actual `new-hypothesis` event fires at 13:43:42 (exact spec match; a second one at 13:40:00 sits in the same window).

BASELINE @13:43:42 (confirms baseline already `up`, diagnostics absent):
```
dir=up  price=29179.5  last_liq=day_high
direction_reason: {rule=rule2b, last_swept_level=day_high, weekly_zone=premium,
                   daily_zone=premium, smt_score=0.0, ...}   # NO session_ath/recovery_gap/is_false_pos_*
```

CHANGE @13:43:42 (still `up`, diagnostics present and sane):
```
dir=up  price=29179.5  last_liq=day_high
direction_reason: {rule=rule2b, last_swept_level=day_high, weekly_zone=premium, daily_zone=premium,
                   all_time_high=30807.0, session_ath=30807.0, recovery_gap=0.0528,
                   is_false_pos_ath=false, is_false_pos_morning=false, is_false_pos_recovery=true,
                   smt_score=0.0, ...}
```
(Identical values on the 13:40:00 hypothesis: session_ath=30807, recovery_gap=0.0528, is_false_pos_recovery=true.)

`recovery_gap` 0.0528 = (30807−29179.5)/30807 ≈ 5.28% > 3% PM threshold → recovery-week continuation → `up`, exactly matching the issue's expected `recovery_gap≈5.3%`. **Verdict: INCONCLUSIVE (no regression).** Change did not regress to `down`; the corrected seed and guard booleans are now reproducible from the event log.

### ex2 — 2026-06-11, window 15:02–15:18 ET
Same situation: baseline already `up`; the near-15:10 hypothesis fires at 15:10:42 (a second at 15:05:00 in-window).

BASELINE @15:10:42:
```
dir=up  price=29304.25  last_liq=day_high
direction_reason: {rule=rule2b, last_swept_level=day_high, weekly_zone=premium, daily_zone=premium, ...}  # no diagnostics
```

CHANGE @15:10:42:
```
dir=up  price=29304.25  last_liq=day_high
direction_reason: {... all_time_high=30807.0, session_ath=30807.0, recovery_gap=0.0488,
                   is_false_pos_ath=false, is_false_pos_morning=false, is_false_pos_recovery=true, ...}
```
(15:05:00 hypothesis: session_ath=30807, recovery_gap=0.0491, is_false_pos_recovery=true.)

`recovery_gap` ≈0.049 ((30807−29304.25)/30807 ≈ 4.88%) > 3% → `up`. **Verdict: INCONCLUSIVE (no regression).** Direction held `up`; diagnostics present and sane.

## Attribution / in-window diff
Diffing the two runs across all 223 events: **0 rows differ in any non-`direction_reason` field**; exactly **26 rows differ in `direction_reason`**, and those are precisely the 26 `new-hypothesis` events. Every diff is purely **additive** — the six new keys (`all_time_high`, `session_ath`, `recovery_gap`, `is_false_pos_ath`, `is_false_pos_morning`, `is_false_pos_recovery`) appear in the change run with all pre-existing keys byte-identical. This matches Fix-2 (diagnosability) exactly and confirms Fix-1's seed is a no-op in backtest (session_ath was already 30807 via the 60-day window; the change run's `global.json` confirms `session_ath=30807.0`, `all_time_high=30807.0`, `trend=up`). No unrelated side effects.

## Whole-day impact
| date | baseline n_trades, pnl | change n_trades, pnl | Δpnl | Δtrades |
|------|------------------------|----------------------|------|---------|
| 2026-06-11 | 34, +$2,536 | 34, +$2,536 | $0 | 0 |

`trades_1s.tsv` is byte-identical between the two runs.

## Bottom line
At both example occurrences the change behaved exactly as the caveat predicted: the backtest baseline was already correct (`up`), so there was no live `down`→`up` flip to reproduce — hence INCONCLUSIVE, which is the expected, non-failure outcome. The verification did confirm the two things it can: (a) **no regression** — direction stays `up` at 13:43:42 and 15:10:42, trades and whole-day P&L are byte-identical (+$2,536, 34 trades, Δ=$0/0); and (b) the **diagnosability fix is live and sane** — every `rule2b` high-sweep hypothesis now carries `session_ath≈30807`, `all_time_high≈30807`, the matching `recovery_gap` (0.0528 at ex1, ~0.049 at ex2, both > the 3% PM threshold → continuation), and the three `is_false_pos_*` booleans (`is_false_pos_recovery=true`, others false). The actual proof that the seed fix corrects the live failure mode (windowed `_hist_mnq_1m` max 29011 < full-parquet 30807, lost prior global) rests on GIL-23's Stage-B unit tests, not this A/B; this A/B stands only as the regression-safety check, and it passes (no change to the already-correct 06-11 backtest).
