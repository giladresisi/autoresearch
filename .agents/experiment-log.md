# SMT Strategy — Experiment Log

This file is the canonical record of completed optimization experiments across all plan rounds.
A future agent implementing the next optimization round should read this first to understand
what has been tested, what the verdicts were, and what conditions must be met before retesting
rejected or neutral flags.

---

## Round 1 — Plan 1 Signal Quality Flags (2026-04-20)

**Test window:** 2026-01-19 → 2026-04-12 (60-day 1-fold fast mode via `plan1_experiment_runner.py`)
**Baseline:** trades=36, pnl=$3,885.80, wr=55.6%, avg_rr=7.47, appt=$107.94, max_dd=$130.20
**Raw data:** `plan1-results.tsv`

### Results

| Flag | Delta PnL | Verdict | Mechanistic Reason |
|------|-----------|---------|-------------------|
| `MIDNIGHT_OPEN_AS_TP=True` | $0 | NEUTRAL | Midnight open ≈ TDO in Jan–Apr 2026; trailing stop equalizes exit regardless of TP level |
| `STRUCTURAL_STOP_MODE=True` (2pt) | −$1,179 | REJECT | Wider stop moves trail anchor further from entry → smaller wins per trade; RR collapses 7.47→2.08 |
| `STRUCTURAL_STOP_MODE=True` (3pt) | −$1,119 | REJECT | Same pattern at larger buffer; confirms mode is incompatible with trailing-stop exits |
| `SILVER_BULLET_WINDOW_ONLY=True` | $0 | NEUTRAL | All 36 baseline trades already fall within 09:50–10:10 ET window; no filtering effect |
| `OVERNIGHT_SWEEP_REQUIRED=True` | $0 | NEUTRAL | All 36 baseline trades already had overnight sweeps; gate has no bite in this period |
| `OVERNIGHT_RANGE_AS_TP=True` | $0 | NEUTRAL | Overnight range equals TDO in this period; same trailing-stop equalization as midnight TP |
| **`HIDDEN_SMT_ENABLED=True`** | **+$1,189 (+30.6%)** | **APPROVE** | Close-based (body) SMT captures 9 extra trades; same WR/RR quality, lower drawdown |
| `INVALIDATION_MSS_EXIT=True` | −$2,757 | REJECT | 22/36 trades exit early via close-below-div-bar-low; drawdown 5.5×; condition fires too close to entry |
| `INVALIDATION_SMT_EXIT=True` | −$2,551 | REJECT | 20/36 early exits via SMT-defended-level breach; same destruction; session extreme is too close to entry |
| `INVALIDATION_CISD_EXIT=True` | $0 | NEUTRAL | Midnight open never breached on close after entry in this period; condition inactive |

### Approved Flag

`HIDDEN_SMT_ENABLED = True` — set as default in `strategy_smt.py` after Round 1.
New effective baseline: trades=45, pnl=$5,075, wr=57.8%, avg_rr=7.25, appt=$112.78, max_dd=$108.

### Re-test Conditions (before including in future optimization)

**`INVALIDATION_MSS_EXIT` / `INVALIDATION_SMT_EXIT`** — re-test when a `MIN_FAVORABLE_MOVE_PTS`
guard is added: condition should only activate after price has moved at least X pts in-favor first.
Without that guard, it fires immediately on any adverse close near entry, cutting every winner short.
Suggested guard: 5–10 MNQ pts of favorable move before exit condition arms.

**`STRUCTURAL_STOP_MODE`** — do not re-test until exit mechanism changes away from trailing-stop.
The incompatibility is structural: wider stop = larger trail anchor offset = smaller profit-lock.
Would only make sense with a fixed-target exit (`TRAIL_AFTER_TP_PTS = 0.0`).

**`MIDNIGHT_OPEN_AS_TP` / `OVERNIGHT_RANGE_AS_TP` / `OVERNIGHT_SWEEP_REQUIRED`** — neutral in
this 60-day window because overnight structure was homogeneous. Re-test after extending data window
or in a period with strong overnight directional moves. Note: `compute_overnight_range()` has a
performance issue — per-day `.index.date` filter on 807K-row DataFrame. Pre-compute as a
day-indexed dict at load time before running these in any batch experiment.

**`SILVER_BULLET_WINDOW_ONLY`** — neutral because all baseline setups cluster in the 09:50–10:10
window already. Useful as a noise filter in expanded-universe or longer-window tests where
off-window signals may become more common.

**`INVALIDATION_CISD_EXIT`** — only meaningful when midnight open diverges significantly from
entry zone. Re-test alongside `MIDNIGHT_OPEN_AS_TP=True` in a period with large overnight gaps.

---

## Round 2 — Plan 2 Position Architecture (pending)

_To be filled after Plan 2 experiments._

## Round 3 — Plan 3 Hypothesis Alignment (pending)

_To be filled after Plan 3 experiments._
