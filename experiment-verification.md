# GIL-25 (R2) — Per-ticker liquidity-level invalidation: verification

**Date:** 2026-06-14 · **Branch:** `autoresearch/smt-level-invalidation` (base `live` @ f475455)
**Verdict:** ✅ PASS — depletion retirement + fixed re-arm both verified in the SMT-div stream;
trade-level A/B on 2026-06-12 is **net positive** (wired live per the user's decision).

## What was built (3 consumers + re-arm)
- **(1) Detection** (`smt_detect._detect_level_smts`): per-ticker depletion latch in the reserved
  `detect_state["__level_inv__"]`. A level is retired for a ticker once it runs `FULFILL_PTS[tier]`
  beyond it (HH above a `*_high`, LL below a `*_low`, wick pass). The pair comparison is **skipped
  if either ticker** retired it. Audit trail in `__level_retirements__`.
- **(1b) Fixed re-arm** (AC#3 / GIL-25 2026-06-13): fixed levels no longer fire once-ever — they
  re-arm on a fresh re-visit (depart-then-return; departure margin = `FULFILL_PTS[tier]`) and
  re-fire **until invalidated**. Invalidation (the skip above) is the terminal state.
- **(2) Cautious targets** (`hypothesis.compute_cautious_prices`) and **(3) hypothesis targets**
  (`build_hypothesis_from_direction` step 7): drop MNQ-invalidated level names (loaded from
  `__level_inv__` via `_invalidated_target_names()`). Wired into the **live trade path** (user's
  decision); `invalidated_names=None`/empty is a pure no-op for direct callers/tests.

## Tests
- New: `tests/test_smt_level_invalidation.py` (11) — latch thresholds, skip-if-either, asymmetric
  (Q8 shape), body-pass skip, fixed re-arm/no-refire-without-departure/terminal-on-invalidation.
- Updated: `test_smt_detect.py` (`test_rearm_via_opposite_smt`, replaced `…single_smt_ever` →
  `…rearms_until_invalidated`, added `…opposite_smt_does_not_rearm_fixed_without_departure`);
  `test_smt_invalidation.py` (`…precedence_same_bar`); `test_smt_hypothesis.py` (+5 consumer 2/3).
- Suite: SMT/hypothesis/pipeline = **203 pass**. Full suite (skip IB) = **26 failures, ALL
  pre-existing** — identical set reproduced on base source f475455 (test_pickmytrade_executor,
  test_hypothesis_smt LLM, test_check_session_parquets, test_bar_state, …). **Zero new failures.**

## SMT-stream verification (regression replay 2026-06-12, baseline vs change)
The replay runs the CME session (overnight from 2026-06-11 18:00 ET). It does **not** reproduce
the exact live-session occurrence times (the live run used different live level prices / state) —
a known replay-vs-live caveat — but it reproduces the same depleted-pool mechanism and one
occurrence (Q3) exactly. smt-div A/B diff (1s and 1m identical conclusions):

**REMOVED by change (depleted pools retired):**
- `prev2_day_high` @ **00:05 (wick) / 00:06 (body)** — **occurrence Q3 exactly** ✅
- `prev1_day_high` @ 21:22 (wick+body) — same depleted-pool mechanism as Q7 (replay timing) ✅

**ADDED by change (fixed re-arm, AC#3):**
- `ny_evening_high` re-fires @ **18:21 and 18:34** (was single-fire-ever) ✅
- `london_high` @ 07:53 (body) — legitimate re-fire on a fresh re-visit ✅

`asia_high` (Q8) does not occur in the replay environment → covered by the asymmetric unit test
(`test_asymmetric_one_ticker_depleted_skips_pair`).

## Trade A/B (2026-06-12) — P&L + exit timing
| mode | baseline | change | Δ |
|---|---|---|---|
| **1s** | 19 trades, **−$304** | 18 trades, **+$881** | **+$1,185** |
| **1m** | 16 trades, **+$1,005.5** | 12 trades, **+$1,185** | **+$179.5** |

All trades are **long** (uptrend day). The change **captures the 03:00–05:39 morning rally** that
baseline missed entirely — 03:35→04:01 **+$438**, 04:47→05:23 **+$374**, 05:26→05:39 **+$240** —
where baseline instead took two losers (05:55 −$89, 06:07 −$141). Dropping the depleted prev-day
high levels as cautious/hypothesis targets removed premature exit ceilings, so longs rode the
uptrend further (e.g. 19:30→19:52 +$212 vs baseline +$150; 22:36 exit re-timed). Net strongly
positive on this day. NOTE: this is **one day**; before any merge the P&L effect should be
broadened across regimes, and the user may want to tune the cautious-pts threshold for
invalidated-as-target levels (the original exploration intent).

## Late / cold start — pre-startup crossings (verified)
The `__level_inv__` latch is built inside `_run_smt_v2_detection`, which the cold-start **warm-up
replay** (`session_pipeline._warmup_replay_smts`) runs for every pre-startup bar `[session_open,
now)`. So a fixed prev liquidity decisively crossed *before the orchestrator started* is retired
from the first live bar (and `__level_inv__`/`__level_retirements__` are `__`-prefixed, so they
don't mask the cold-start guard). Verified on 3 real late-start probes:
- **06-12 @ 09:30** retires `prev1_day_high{mnq}`, `prev2_day_high{mnq}`, **`asia_high{mes}`**,
  `asia_low{mnq}` — i.e. all three occurrence levels (incl. the Q8 asymmetric MES case the
  replay-from-open didn't surface). 06-08 @ 19:30 retires `prev1_week_low`/`prev2_day_low`;
  06-10 @ 09:30 retires 6 levels. Retirement events stamp the real pre-startup bar.
- Locked in by `tests/test_smt_warmup_startup.py::test_warmup_seeds_level_invalidation`.
- **Residual (minor):** a level crossed in a *prior* session **and** fully retraced below it
  before this session's 18:00 open is not pre-seeded (warm-up only goes back to session open;
  if price is still beyond at open it IS caught on the first bar). Backtests replay from open so
  they're inherently covered.

## Artifacts
- Plots: `regression/sessions/2026-06-12/r2_ab/R2_{change,baseline}_{1s,1m}.html`
- Streams/trades: same folder, `{change,baseline}_{events,trades}[_1s].*`
- Plan: `.agents/plans/1.smt-level-invalidation-r2.md`

## Acceptance criteria
- [x] Per-ticker level invalidation state; skipped in `_detect_level_smts` if either ticker invalidated.
- [x] Fan-out to cautious + hypothesis target lists (invalidated dropped); wired live.
- [x] Fixed-level re-arm until invalidated (no longer single-fire-ever); invalidation terminal.
- [x] 2026-06-12 regression: depleted prev2_day_high @ 00:05/00:06 (Q3) + prev1_day_high no longer
      fire; asia_high (Q8) covered by unit test (absent from replay env).
- [x] Unit tests for invalidation + re-arm; SMT/pipeline suite green except known pre-existing.
