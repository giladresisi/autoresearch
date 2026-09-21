# Initial-target lookahead study (30 RTH days, 2026-08-07 .. 2026-09-17)

Measurement only, with DELIBERATE lookahead on direction and entry. The question: with the
correct 09:30 direction and the best entry of the live window given for free, run the
PRODUCTION target selectors at that entry and ask whether the tape agreed with the T2
primary target, with the new initial target, or with neither. No P&L claim is made.

Script: `agent/study/initial_target_lookahead.py` (run from the repo root with
`.venv/Scripts/python`). Raw rows: `agent/study/out/initial_target_lookahead.tsv` and a
JSON sidecar. Runtime ~45 s for 30 days (the selectors build one facts bundle per day).

## Definitions (as specified)

DATA

- 1m MNQ/MES parquets (ET tz index) in `<global>/general/main/<contract>/`, contract folder
  resolved per date by `backtest_smt._main_dir_for_date` (2026-09 for dates <= 2026-09-11,
  2026-12 after; prices back-adjusted per era, never mixed inside one day). Data ends
  2026-09-18 08:29 in main/2026-12; **2026-09-18 is skipped** (its RTH is not promoted).
  Days = the last 30 dates with RTH 1m bars through 13:00 ET (26 from the 2026-09 era, 4
  from 2026-12).

PRODUCTION SELECTORS (called, not re-implemented)

- Primary target T2: `agent/trader/target.py::select_target(bars, now, direction, "MNQ")`.
  `bars` = {"MNQ": df, "MES": df} of 1m bars with 20 calendar days of history up to and
  including `now`, truncated at `now` (asserted: no bar with index > now in the frames).
- Level universe: `agent/trader/target.py::level_universe(bars, now, "MNQ")`.
- Initial target: `agent/trader/initial_target.py::select_initial_target(direction,
  anchor=entry_price, secondary=T2 price, levels, attempts_used=0)`.
- Reached rule: `InitialTargetTracker(direction, initial, level).on_bar_close(bar)` on
  COMPLETED 1m bars: flips once on touch AND close beyond. A touch alone never flips.
  Per the tracker's docstring the fill's own minute is fed too (flagged `flip_on_entry_bar`).

PER-DAY PROCEDURE (ET)

1. O = the 09:30 1m bar open. Over the 09:30..12:59 1m bars: up_ex = max(High) − O,
   down_ex = O − min(Low). Direction = UP if up_ex > down_ex else DOWN.
2. Optimal entry inside the live window, 1m bars labelled 09:30..10:29: DOWN = the highest
   High, UP = the lowest Low. entry_price = that extreme, entry_time = that bar's label.
3. At now = entry_time: select_target → T2; level_universe → select_initial_target → initial
   (or None with a reason).
4. Walk 1m bars strictly after entry_time through 12:59 (the bar closing at 13:00):
   primary_reached (tick touch of T2), MFE/MAE from entry, the 13:00 mark (last close),
   initial_touched (tick), initial_flipped (tracker), kept_at_flip = signed pts from entry
   at the flip bar close, primary_after_flip, post_flip_mae (worst mark from ENTRY after
   the flip through 13:00), stop15_hit_before_initial (15-pt stop from entry, same-bar →
   stop wins; informational).
   Extra: retrace_to_initial (after the flip, price came back to touch the initial before
   the primary / 13:00 = plan §2.5 action A's stop), worst_flip_to_primary (worst mark
   between the flip and the primary touch), cf_opp_close_kept (exit at the first opposite
   1m close after the flip = action B).
5. Class: A primary reached; B primary not reached but initial flipped; C initial touched
   but never flipped; D initial never touched; E no T2 / no initial stage.

## Per-day table

dist/share/MFE/13:00/kept are pts from the entry, favorable-positive. `share` = initial
distance / entry→T2 distance. `retrace` = back-touch of the initial after the flip (before
the primary on A days). `worst flip→T2` = worst mark between the flip and the primary
touch (or 13:00 on B days).

| date | dir | entry | entry px | T2 level | T2 dist | initial level | ini dist | share | cls | T2 hit | MFE | 13:00 | touch | flip | kept@flip | retrace | worst flip→T2 | opp-close kept | stop15 first |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-08-07 | DOWN | 09:45 | 29821.75 | TDO | 333 | synthetic_85pct | 283.05 | 0.85 | D |  | 257.5 | 82 |  |  |  |  |  |  | Y |
| 2026-08-10 | UP | 09:41 | 29719 | TDO | 159.25 | asia(cur)_low | 67 | 0.42 | A | 10:10 | 181 | 60.75 | 09:44 | 09:46 | 69.25 | Y | 35.25 | 86.25 | n |
| 2026-08-11 | DOWN | 09:30 | 29842.75 | london(cur)_low | 125.25 | ny_morning(cur)_low | 91.25 | 0.73 | A | 09:35 | 226.75 | 217 | 09:31 | 09:33 | 106.75 | n | 97.5 | 106.25 | n |
| 2026-08-12 | DOWN | 09:30 | 30001.5 | TDO | 307 | prev6_day_high | 43 | 0.14 | B |  | 182.75 | 96 | 09:31 | 09:31 | 72 | Y | 31.5 | 99 | n |
| 2026-08-13 | UP | 09:32 | 29862.75 | prev1_day_high | 138.75 | prev7_day_high | 91.75 | 0.66 | A | 09:36 | 404.25 | 330.75 | 09:35 | 09:35 | 117 | n | 115.25 | 165 | n |
| 2026-08-14 | DOWN | 09:48 | 30280.75 | TDO | 128 | day_mid | 73 | 0.57 | A | 10:24 | 255.75 | 222.25 | 09:57 | 10:17 | 81.25 | n | 80.25 | 93.5 | n |
| 2026-08-17 | UP | 10:24 | 30156.5 | TDO | 89.75 | london(cur)_low | 71.75 | 0.8 | A | 11:00 | 130.25 | 27 | 10:51 | 10:57 | 75.5 | n | 74.75 | 87 | n |
| 2026-08-18 | DOWN | 09:41 | 29770 | prev4_day_low | 145 | london(cur)_low | 81 | 0.56 | A | 09:55 | 256 | 120 | 09:44 | 09:44 | 82.5 | Y | 69.5 | 73 | n |
| 2026-08-19 | DOWN | 09:33 | 29742 | TDO | 216.5 | london(cur)_high | 98.75 | 0.46 | A | 09:48 | 366.25 | 182.75 | 09:36 | 09:36 | 115.5 | Y | 91.25 | 113 | n |
| 2026-08-20 | UP | 10:20 | 29304.5 | TDO | 314.25 | prev1_day_low | 69.25 | 0.22 | B |  | 152.75 | 75.25 | 10:21 | 10:22 | 84 | Y | -35.25 | 91.75 | n |
| 2026-08-21 | DOWN | 09:30 | 29472.5 | TDO | 119.75 | prev2_day_low | 94.75 | 0.79 | A | 09:35 | 252.25 | 60.25 | 09:32 | 09:32 | 96 | Y | 83.75 | 95 | n |
| 2026-08-24 | DOWN | 09:30 | 29243.75 | london(cur)_low | 127 | prev1_week_low | 39.25 | 0.31 | A | 09:31 | 296 | 73.25 | 09:31 | 09:30 | 112 | n | 84.25 | 93.25 | n |
| 2026-08-25 | DOWN | 09:49 | 29416 | TDO | 252 | prev4_day_low | 38.25 | 0.15 | A | 10:30 | 258.25 | 140.5 | 09:50 | 09:50 | 49 | Y | 27.75 | 38.75 | n |
| 2026-08-26 | UP | 10:28 | 29156.25 | TDO | 109.25 | prev3_day_low | 62 | 0.57 | A | 10:52 | 136 | 60.75 | 10:37 | 10:37 | 73.5 | Y | 49.5 | 59.75 | n |
| 2026-08-27 | UP | 09:57 | 29424 | london(cur)_high | 237.25 | prev2_week_low | 107.5 | 0.45 | B |  | 235 | 227.5 | 10:03 | 10:03 | 124.25 | Y | 77.75 | 112 | n |
| 2026-08-28 | UP | 10:13 | 29505.5 | TDO | 163.25 | asia(cur)_low | 84 | 0.52 | A | 10:22 | 306.25 | -3.5 | 10:17 | 10:17 | 87.75 | Y | 83.5 | 139.25 | n |
| 2026-08-31 | DOWN | 09:31 | 29517 | TDO | 164.5 | day_mid | 106.5 | 0.65 | B |  | 163 | 77.25 | 09:35 | 09:35 | 107 | Y | 37.25 | 98.5 | n |
| 2026-09-01 | UP | 09:41 | 29040 | TDO | 446.25 | prev4_day_low | 53.5 | 0.12 | B |  | 277.25 | 151 | 09:44 | 09:45 | 74 | Y | 45 | 72.75 | n |
| 2026-09-02 | UP | 09:55 | 29017.25 | asia(cur)_high | 154 | london(cur)_high | 101.75 | 0.66 | A | 10:21 | 194.5 | 137.25 | 10:10 | 10:14 | 110.25 | n | 102 | 132.5 | n |
| 2026-09-03 | UP | 09:36 | 29199.25 | projection_up | 167.75 | london(cur)_high | 91.75 | 0.55 | A | 09:49 | 348.75 | 328.5 | 09:38 | 09:39 | 130 | n | 114.5 | 136.25 | n |
| 2026-09-04 | DOWN | 09:58 | 29692 | TDO | 121 | prev1_day_high | 105 | 0.87 | A | 10:26 | 214.25 | 116.25 | 10:20 | 10:25 | 106.5 | Y | 103.75 | 102.5 | n |
| 2026-09-07 | DOWN | 09:33 | 29646 | asia(cur)_low | 115.75 | prev5_day_high | 100.5 | 0.87 | D |  | 82.25 | 41.75 |  |  |  |  |  |  | n |
| 2026-09-08 | DOWN | 09:30 | 29686.75 | london(cur)_low | 208.25 | prev3_day_high | 99.75 | 0.48 | A | 09:44 | 262.25 | 114.25 | 09:33 | 09:33 | 100 | Y | 73.5 | 86.75 | n |
| 2026-09-09 | UP | 09:30 | 29417 | TDO | 144.75 | asia(cur)_low | 70.75 | 0.49 | A | 09:59 | 177 | 40.75 | 09:34 | 09:41 | 77.5 | Y | 39.5 | 70.75 | n |
| 2026-09-10 | UP | 09:33 | 29057.75 | TDO | 369.75 | synthetic_85pct | 314.29 | 0.85 | D |  | 217.5 | 175 |  |  |  |  |  |  | n |
| 2026-09-11 | DOWN | 09:59 | 29475 | TDO | 416 | week_mid | 71.62 | 0.17 | B |  | 141 | 38.75 | 10:04 | 10:04 | 82.5 | Y | -24.5 | 95.5 | n |
| 2026-09-14 | UP | 09:37 | 29167.5 | TDO | 144.25 | asia(cur)_low | 105 | 0.73 | A | 09:48 | 408.75 | 390.75 | 09:46 | 09:47 | 123.25 | n | 121 | 152 | n |
| 2026-09-15 | DOWN | 09:45 | 29452.75 | london(cur)_low | 221.5 | day_mid | 87.25 | 0.39 | A | 10:51 | 237 | 172.5 | 10:10 | 10:10 | 88.75 | n | 87.5 | 110 | n |
| 2026-09-16 | UP | 09:36 | 29373 | prev1_day_high | 122.75 | ny_morning(cur)_high | 64 | 0.52 | A | 10:59 | 179.5 | 95.5 | 09:46 | 09:46 | 71.25 | Y | 45 | 67 | n |
| 2026-09-17 | DOWN | 10:28 | 29741.25 | TDO | 280.75 | prev6_day_low | 107 | 0.38 | D |  | 89.25 | 0.5 |  |  |  |  |  |  | n |

## Aggregates

### Counts per class (n = 30)

| class | n | meaning |
|---|---|---|
| A | 20 | primary (T2) reached |
| B | 6 | primary not reached, initial flipped |
| C | 0 | initial touched, never closed beyond |
| D | 4 | initial never touched |
| E | 0 | no T2 / no initial stage |

T2 was never None and never on the wrong side of the entry. An initial stage existed on all
30 days: 28 structural, 2 synthetic (both when T2 was >330 pts away and no level sat within
110 pts of the fill).

### Headline: days where the primary was NOT reached (n = 10)

The initial flipped on 6 of 10. On the other 4 (class D) it was never touched.

Class B, pts from entry:

| stat | kept at flip | 13:00 mark | MFE |
|---|---|---|---|
| mean | 90.6 | 111.0 | 192.0 |
| median | 83.3 | 86.6 | 172.9 |
| sum | 543.8 | 665.8 | 1151.8 |

- Exiting at the flip kept a median 54% of MFE (range 27%..66%). It did NOT keep "most of
  the profit": on 3 of 6 days the 13:00 mark beat the flip exit (08-27 by 103 pts, 09-01 by
  77, 08-12 by 24), and on 3 it protected (09-11 +44, 08-31 +30, 08-20 +9).
- After the flip, price came back to touch the initial on ALL 6 B days. On 2 of 6 (08-20,
  09-11) it went on to a negative mark from entry (post-flip worst −35 and −25): a hold with
  no stop would have given back everything.
- Class D (never touched): MFE 257 / 82 / 218 / 89 with 13:00 marks 82 / 42 / 175 / 0.5.
  Two of these are the synthetic-85% days (08-07, 09-10) where the initial sat 283 and 314
  pts out and the day made 257 and 218 pts of MFE that no stage could bank. 09-17's entry
  was the 10:28 window high, AFTER the 09:38 low: the post-entry excursion was only 89 pts
  vs an initial 107 pts out.

### Days where the primary WAS reached (n = 20)

- The flip preceded the primary touch on 20 of 20 (never the same minute). Lag flip→primary:
  median 8.5 min, IQR 2.75..15.75, max 73 min (09-16).
- Exiting at the flip would have forfeited (T2 dist − kept at flip): mean 59.2 pts, median
  45.3, total 1,184.5 pts over the 20 days. Kept-at-flip was a median 65% of the T2 distance.
- Between the flip and the primary, price retraced back to the initial on 11 of 20 days
  (worst mark between flip and T2 below the initial). So plan §2.5 action A (stop moved to
  the initial) would have stopped out BEFORE the primary on 11 of the 20 days that did reach
  it, most often 1 minute after the flip.
- By 13:00 the mark was below the flip-exit level on 7 of 20 A days, and below the T2
  distance on 15 of 20: the primary touch was usually the better exit than a hold to 13:00.

### Touch-only days

None. Every one of the 26 touches closed beyond: 14 within the touch bar, 10 within 1–7
minutes, one after 20 min (08-14: touched 09:57, flipped 10:17), and one on the entry bar
itself (08-24). Over 30 days the close-beyond clause never changed an outcome; it only
delayed the flip.

### Policy totals (pts from entry, 30 days, informational, NO stop on non-flip days)

| exit rule | total | mean/day | median | B days | A days |
|---|---|---|---|---|---|
| T2 if reached else 13:00 mark | 4,023 | 134.1 | 133.4 | 666 | 3,058 |
| flip close if flipped else 13:00 mark | 2,717 | 90.6 | 85.9 | 544 | 1,874 |
| §2.5 A: after the flip, stop at the initial | 2,912 | 97.1 | 92.3 | 451 | 2,162 |
| §2.5 B: after the flip, first opposite 1m close | 2,885 | 96.2 | 94.3 | 570 | 2,016 |
| (reference) MFE | 6,889 | 229.6 | | | |
| (reference) 13:00 mark | 3,853 | 128.4 | | | |

With the correct direction and a perfect entry, anything that exits at or near the initial
gives up ~1,100–1,300 pts against "T2 or the 13:00 mark", almost all of it on the 20 A days.
On the 6 B days the opposite-close rule (570) beats the raw flip exit (544) and the
initial-stop (451), but none beats the simple 13:00 mark (666) on those same days.

### Initial distance and share of the entry→T2 distance

| initial distance (pts) | n |
|---|---|
| ≤ 40 | 2 (38.25, 39.25: the 40-pt floor is tested on the LEVEL, then the 2-pt offset is applied) |
| 40–60 | 2 |
| 60–80 | 8 |
| 80–100 | 9 |
| 100–120 | 7 |
| > 120 | 2 (synthetic 283, 314) |

Mean 95.8, median 89.3. Share of entry→T2: mean 0.53, median 0.53; bins ≤0.2: 4, 0.2–0.4:
4, 0.4–0.6: 11, 0.6–0.8: 7, 0.8–1.0: 4. T2 distance itself: median 161 pts, mean 201, 8
days > 250 pts, 19 of 30 T2 picks are `TDO`.

## The most instructive days

- **2026-09-11 (B, DOWN, entry 09:59 at 29475, T2 = TDO 416 pts away).** Initial = week_mid
  71.6 pts out, flipped 10:04 keeping 82.5. Price then retraced through the initial, MFE
  141 at 10:48, and closed 13:00 at +38.75 with a −24.5 mark on the way. The stage paid.
- **2026-08-27 (B, UP, entry 09:57, T2 = london high 237 pts).** Initial = prev2_week_low
  107.5 pts out, flipped 10:03 keeping 124. Price kept going: MFE 235 at 12:45, 13:00 mark
  227.5. The stage forfeited 103 pts on a day the tape agreed with T2 to within 2 pts but
  never printed it.
- **2026-08-25 (A, DOWN, entry 09:49, T2 = TDO 252 pts).** Initial = prev4_day_low at
  38.25 pts (the sub-40 case), flipped one minute after the fill keeping 49, retraced to
  27.75 above the initial, then T2 reached 10:30 (MFE 258). A near-anchor initial that fires
  almost immediately, gets re-tested, and precedes a 250-pt move.
- **2026-08-07 / 2026-09-10 (D, synthetic).** T2 = TDO 333 / 370 pts, no structural level
  within 110 pts on the trade side, so the synthetic 85% initial sat 283 / 314 pts out. The
  days made 257 / 218 pts of MFE and marked 82 / 175 at 13:00. The fallback is inert exactly
  when T2 is far; 08-07 is also the only day the 15-pt stop would have hit before any
  initial touch (it was never touched).
- **2026-08-24 (A, flip on the entry bar).** Entry = the 09:30 bar high 29243.75; that same
  bar's low crossed the 39.25-pt initial and closed 112 pts below the entry, so production's
  tracker would have flipped on the fill's own minute, and T2 was touched at 09:31.

## What surprised me

- No class C at all: the touch-AND-close rule was redundant over 30 days at 1m granularity.
- Flip always preceded the primary (by construction the initial is between entry and T2,
  but the close-beyond clause could have lagged past a T2 touch; it never did).
- Retrace to the initial after the flip on 17 of 26 flipped days (11 of 20 A, 6 of 6 B), so
  a stop moved to the initial would exit before T2 on more than half of the winning days.
- select_target returned a pick on every day (the corpus study's ~6% empty-menu rate did not
  show up here), and `TDO` dominated the D1 pick (19 of 30), which makes T2 far (8 days
  > 250 pts) and drives both synthetic fallbacks.
- The sub-40 initials (38.25, 39.25 pts): `MIN_DIST_PTS` is checked on the raw level, then
  the 2-pt offset pulls the initial inside the floor.

## What this does and does not show

- Direction and entry are LOOKAHEAD: the correct 09:30 direction and the window extreme
  are given. MAE is 0 on 21 of 30 days for that reason, and the 15-pt stop is nearly
  never hit. Real fills sit worse and the stop matters; none of the point totals is P&L.
- The entry is the window extreme, not the earliest good fill: on 09-17 (and partly 08-17,
  10:24) the big move came BEFORE the entry, so the post-entry excursion understates the day.
- No stop and no losing side: the "13:00 mark" policies hold through any drawdown, which no
  live position does. B-day comparisons against the 13:00 mark flatter the hold.
- The selectors saw production-shaped frames (20d 1m history truncated at the entry bar's
  label), with `now` = the 1m bar label rather than a 1s fill instant, so the selector's
  view excludes the rest of the entry minute. Level universe differences vs the live graft's
  17d hist splice are possible but the pick rule is the same code.
- 30 days, 26 flips, 6 B days: the B-day medians rest on six observations.
- 2026-09-18 excluded (RTH not promoted into the main parquet at run time).

---

## v1 vs v2 variants (rerun 2026-09-20, after plan 35 §2.2–§2.3 v2)

Everything above is the v1 selector (fixed 110-pt cap, no side filter, closest-to-T2). Its rows
are now `out/initial_target_lookahead_v1.tsv`. The v2 selector was rerun on the SAME 30 days, the
same lookahead entries and the same T2 picks (the T2 stage is untouched); what changed is the
level universe (`rth(cur)_high/low` over completed post-09:30 bars before the entry bar,
`ny_morning(cur)_mid`, and the bundle's swept / depleted / nested flags now applied) and the
rule: correct side only, band 20–80 % of D, floor 42 pts before the 2-pt offset, tiers session
extreme → mid/open → prevN, 85 % fallback. Grid = EXTREME_MIN {A: ≥150 pts, F: ≥0.65·D} ×
MID_PREFERENCE {nearest, farthest, session_mid_first}; one file per variant
`out/initial_target_lookahead_<A|F>_<pref>.tsv`, tables in `out/variants_summary.md`
(`--all-variants`, 53 s for 30 days × 6 selectors).

### Class counts

| variant | A (T2 hit) | B (flipped) | C (touch only) | D (never touched) | E | structural | synthetic |
|---|---|---|---|---|---|---|---|
| v1 | 20 | 6 | 0 | 4 | 0 | 28 | 2 |
| A_nearest | 20 | 7 | 0 | 3 | 0 | 24 | 6 |
| A_farthest | 20 | 5 | 1 | 4 | 0 | 24 | 6 |
| **A_session_mid_first** | 20 | 7 | 0 | 3 | 0 | 24 | 6 |
| F_nearest | 20 | 8 | 0 | 2 | 0 | 26 | 4 |
| F_farthest | 20 | 5 | 1 | 4 | 0 | 26 | 4 |
| F_session_mid_first | 20 | 8 | 0 | 2 | 0 | 26 | 4 |

No wrong-side pick remains in any v2 variant (v1 had 8 of 28 structural picks on the wrong side;
all six hand-reviewed days included). The synthetic fallback fires more often under v2 (4–6 vs 2)
because the band and the floor exclude near levels that v1 accepted (08-13, 08-17, 08-24, 09-16
and, under A only, 09-02 and 09-14); its distance is now bounded by 0.85·D instead of sitting
280–320 pts out.

### Failed-primary days (n = 10; T2 never touched)

Pts from the entry; mean / median over the flipped days. "Total banked" = kept at the flip on
flipped days + the 13:00 mark on the days the initial was never reached (a hold, not an exit).

| variant | initial reached | kept at flip | 13:00 mark | MFE | kept / MFE (median) | 13:00 beat the flip | negative mark after the flip | total banked |
|---|---|---|---|---|---|---|---|---|
| v1 | 6/10 | 90.6 / 83.2 | 111.0 / 86.6 | 192.0 / 172.9 | 0.54 | 3/6 | 2/6 | 544 + 299 = 843 |
| A_nearest | 7/10 | 158.6 / 180.8 | 121.5 / 96.0 | 202.2 / 217.5 | 0.79 | 1/7 | 1/7 | 1,110 + 115 = 1,225 |
| A_farthest | 5/10 | 160.6 / 187.2 | 115.9 / 82.0 | 203.0 / 235.0 | 0.79 | 1/5 | 1/5 | 803 + 386 = 1,188 |
| **A_session_mid_first** | 7/10 | 158.6 / 180.8 | 121.5 / 96.0 | 202.2 / 217.5 | 0.79 | 1/7 | 1/7 | 1,110 + 115 = 1,225 |
| F_nearest | 8/10 | 138.4 / 120.6 | 115.7 / 89.0 | 196.0 / 200.1 | 0.72 | 2/8 | 2/8 | 1,108 + 39 = 1,147 |
| F_farthest | 5/10 | 171.2 / 187.2 | 115.9 / 82.0 | 203.0 / 235.0 | 0.80 | 1/5 | 1/5 | 856 + 386 = 1,242 |
| F_session_mid_first | 8/10 | 138.4 / 120.6 | 115.7 / 89.0 | 196.0 / 200.1 | 0.72 | 2/8 | 2/8 | 1,108 + 39 = 1,147 |

v2 doubles what the flip keeps on the days that matter (v1 kept a median 54 % of MFE, v2 A keeps
79 %) and the 13:00 mark beats the flip exit on 1 of 7 days instead of 3 of 6. The three A-variant
misses are 08-20 (rth high 164 pts out, MFE 153), 09-11 (NY-morning low 268 pts out, MFE 141) and
09-17 (NY-morning low 184 pts out, MFE 89: the 10:28 entry came after the move). F recovers 08-20
(NY-morning mid at 105 pts, flipped 10:39 keeping 108.5) but loses 09-01 (NY-morning mid 107 pts
instead of the NY-morning high 216: kept 107.5 instead of 218.75), which is why F banks LESS in
total despite the higher coverage. "farthest" reaches only 5 of 10 (08-12 week_mid at 77 % and
09-10 day_mid at 58 % of D were never closed beyond).

### Winners (n = 20; T2 touched)

| variant | flipped before T2 | minutes flip → T2 (mean / median) | forfeited pts by exiting at the flip (mean / median) | forfeited total | retrace to the initial before T2 |
|---|---|---|---|---|---|
| v1 | 20/20 | 14.3 / 8.5 | 59.2 / 45.2 | 1,184 | 11/20 |
| A_nearest | 20/20 | 14.2 / 7.5 | 56.1 / 50.1 | 1,122 | 10/20 |
| A_farthest | 20/20 | 10.7 / 5.5 | 43.3 / 45.2 | 866 | 10/20 |
| **A_session_mid_first** | 20/20 | 13.9 / 7.5 | 54.6 / 46.2 | 1,093 | 11/20 |
| F_nearest | 20/20 | 14.4 / 9.5 | 57.2 / 50.9 | 1,144 | 10/20 |
| F_farthest | 20/20 | 10.9 / 7.0 | 44.7 / 47.8 | 894 | 9/20 |
| F_session_mid_first | 20/20 | 14.2 / 9.5 | 56.0 / 49.8 | 1,121 | 10/20 |

The flip still precedes every T2 touch (by construction the initial is inside the band). The
early-flip penalty barely moves between v1 and the v2 session-mid variants (−90 pts over 20 days
for A_session_mid_first); only "farthest" cuts it (−300) and it pays for that with half the
failed-day coverage.

### Initial distance from the entry

| variant | ≤40 | 40–60 | 60–80 | 80–120 | 120–200 | >200 | mean / median pts | mean / median share of D | tier 1 / 2 / 3 / synthetic |
|---|---|---|---|---|---|---|---|---|---|
| v1 | 2 | 2 | 8 | 16 | 0 | 2 | 95.8 / 89.2 | 0.53 / 0.53 | – |
| A_nearest | 0 | 6 | 7 | 6 | 8 | 3 | 113.1 / 106.1 | 0.58 / 0.53 | 7 / 17 / 0 / 6 |
| A_farthest | 0 | 3 | 6 | 8 | 8 | 5 | 129.1 / 113.8 | 0.66 / 0.68 | 7 / 17 / 0 / 6 |
| **A_session_mid_first** | 0 | 5 | 8 | 6 | 8 | 3 | 114.1 / 106.1 | 0.59 / 0.57 | 7 / 17 / 0 / 6 |
| F_nearest | 0 | 6 | 6 | 10 | 6 | 2 | 106.6 / 99.4 | 0.57 / 0.55 | 9 / 17 / 0 / 4 |
| F_farthest | 0 | 3 | 5 | 10 | 7 | 5 | 129.4 / 107.2 | 0.64 / 0.68 | 9 / 17 / 0 / 4 |
| F_session_mid_first | 0 | 5 | 7 | 10 | 6 | 2 | 107.2 / 99.4 | 0.58 / 0.57 | 9 / 17 / 0 / 4 |

No sub-40 initial remains (the floor is now tested after the offset). Tier 3 (prevN levels) was
never the pick on these 30 days: every eligible prevN level was outranked by a session extreme or
a mid, or excluded as nested/swept — the correct-side + not-nested rules remove most of them.

### The six reviewed days

level / pts from the entry / share of D / tier; `*` = flipped, `T2` = primary reached.

| variant | 09-01 UP | 08-27 UP | 08-12 DOWN | 09-08 DOWN | 09-15 DOWN | 08-25 DOWN |
|---|---|---|---|---|---|---|
| v1 | prev4_day_low 54 12 % * | prev2_week_low 108 45 % * | prev6_day_high 43 14 % * | prev3_day_high 100 48 % * T2 | day_mid 87 39 % * T2 | prev4_day_low 38 15 % * T2 |
| **A_session_mid_first** | ny_morning(cur)_high 216 48 % t1 * (10:48) | rth(cur)_high 179 75 % t1 * (10:27) | ny_morning(cur)_mid 126 41 % t2 * (09:40) | ny_morning(cur)_low 151 72 % t1 * T2 (09:41) | ny_morning(cur)_mid 43 19 % t2 * T2 (10:03) | ny_morning(cur)_mid 66 26 % t2 * T2 (09:54) |
| A_nearest | same | same | same | same | same | same |
| A_farthest | same | same | week_mid 237 77 % t2 (never) | same | day_mid 87 39 % t2 * T2 | day_mid 196 78 % t2 * T2 (10:23) |
| F_session_mid_first | ny_morning(cur)_mid 107 24 % t2 * (10:06) | same | same | same | same | same |
| F_nearest | ny_morning(cur)_mid 107 24 % t2 * | same | same | same | same | same |
| F_farthest | day_mid 271 61 % t2 * | same | week_mid 237 77 % t2 (never) | same | day_mid 87 39 % t2 * T2 | day_mid 196 78 % t2 * T2 |

Under A + session_mid_first the six days select exactly as the plan's §2.3 table (the operator's
reading on five; 08-25 takes the NY-morning mid at 26 % where the operator preferred day_mid at
78 % — the "farthest" preference, which loses 08-12 and half the failed-day coverage elsewhere).
09-15's `ny_morning(cur)_mid` sits at 42.88 pts / 19.4 % of D: it passes the 20 % band on the raw
level (44.9 pts = 20.3 %) and the 42-pt floor, so it is the pick rather than the 85 % fallback
the plan listed as the alternative.

### Days where the v2 variants disagree (pick / pts / class / kept at the flip)

- **08-20 UP (B under v1, MFE 153, 13:00 +75):** A → rth(cur)_high 164 pts, never touched (D);
  F → ny_morning(cur)_mid 105 pts, flipped 10:39 keeping 108.5. The one failed day A misses and
  F banks.
- **09-01 UP (MFE 277, 13:00 +151):** A → ny_morning(cur)_high 216 pts, flipped 10:48 keeping
  218.75; F → ny_morning(cur)_mid 107 pts, flipped 10:06 keeping 107.5 (F_farthest: day_mid 271
  pts, kept 273). The operator's reading was the NY-morning high.
- **08-12 DOWN (MFE 183, 13:00 +96):** session-mid variants → ny_morning(cur)_mid 126 pts,
  flipped 09:40 keeping 132.75; farthest → week_mid 237 pts, never closed beyond (D).
- **08-25 DOWN (T2 10:30):** session-mid → ny_morning(cur)_mid 66 pts (flip 09:54, kept 73.5);
  farthest → day_mid 196 pts (flip 10:23, kept 201.75). Both precede T2.
- **09-02 / 09-14 UP (both T2 days):** A → synthetic 85 % (131 / 123 pts); F → rth(cur)_high
  100 pts / ny_morning(cur)_high 97 pts (61 % / 67 % of D, under A's 150-pt bar). Kept-at-flip
  differs by 34 and 20 pts in A's favour.
- **08-11 DOWN (T2 09:35):** A → TWO 76.5 pts; F → ny_morning(cur)_low 91 pts (73 % of D).

### Recommendation: variant A (EXTREME_MIN = 150 pts) + session_mid_first — kept as the code default

By the agreed criteria: (1) coverage of failed-primary days — F_session_mid_first 8/10 vs
A_session_mid_first 7/10 vs farthest 5/10; (2) share of the move captured on those days —
A 79 % of MFE (median), F 72 %, and in total A banks 1,225 pts vs F 1,147 because 09-01 (+111
for A) outweighs 08-20 (+33 for F, net of the 13:00 hold); (3) early-flip penalty on winners —
A_session_mid_first 1,093 forfeited vs F 1,121 vs farthest 866–894. A_session_mid_first is
best or tied on (2) and (3) and one day short of F on (1); the only variant that beats it on
the penalty gives up half the failed-day coverage and the operator's picks on 08-12 and 09-15.
The A/F difference rests on two days (08-20, 09-01) and the 150-pt bar was fitted on a 16-pt
margin, so this is a default, not a finding: re-measure once the recorded
`initial_target_selected.tier/variant` accumulate on live fills.

Per-day rows for the chosen default (pts from the entry; `kept` = at the flip close, `retr` =
back-touch of the initial after the flip, `B-exit` = first opposite 1m close):

| date | dir | entry | entry px | T2 level | T2 dist | initial level | dist | share | tier | cls | T2 hit | MFE | 13:00 | flip | kept | retr | B-exit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-08-07 | DOWN | 09:45 | 29821.75 | TDO | 333.00 | ny_morning(cur)_low | 227.50 | 0.68 | 1 | B |  | 257.50 | 82.00 | 10:12 | 228.75 | Y | 217.75 |
| 2026-08-10 | UP | 09:41 | 29719.00 | TDO | 159.25 | ny_morning(cur)_mid | 118.12 | 0.74 | 2 | A | 10:10 | 181.00 | 60.75 | 10:06 | 139.00 | n | 137.75 |
| 2026-08-11 | DOWN | 09:30 | 29842.75 | london(cur)_low | 125.25 | TWO | 76.50 | 0.61 | 2 | A | 09:35 | 226.75 | 217.00 | 09:32 | 81.50 | Y | 106.25 |
| 2026-08-12 | DOWN | 09:30 | 30001.50 | TDO | 307.00 | ny_morning(cur)_mid | 126.25 | 0.41 | 2 | B |  | 182.75 | 96.00 | 09:40 | 132.75 | Y | 113.25 |
| 2026-08-13 | UP | 09:32 | 29862.75 | prev1_day_high | 138.75 | synthetic_85pct | 117.94 | 0.85 | 0 | A | 09:36 | 404.25 | 330.75 | 09:36 | 162.00 | n | 165.00 |
| 2026-08-14 | DOWN | 09:48 | 30280.75 | TDO | 128.00 | ny_morning(cur)_mid | 50.12 | 0.39 | 2 | A | 10:24 | 255.75 | 222.25 | 09:57 | 61.75 | Y | 55.25 |
| 2026-08-17 | UP | 10:24 | 30156.50 | TDO | 89.75 | synthetic_85pct | 76.29 | 0.85 | 0 | A | 11:00 | 130.25 | 27.00 | 10:58 | 87.50 | Y | 87.00 |
| 2026-08-18 | DOWN | 09:41 | 29770.00 | prev4_day_low | 145.00 | ny_morning(cur)_mid | 59.75 | 0.41 | 2 | A | 09:55 | 256.00 | 120.00 | 09:44 | 82.50 | n | 73.00 |
| 2026-08-19 | DOWN | 09:33 | 29742.00 | TDO | 216.50 | ny_morning(cur)_mid | 108.38 | 0.50 | 2 | A | 09:48 | 366.25 | 182.75 | 09:36 | 115.50 | Y | 113.00 |
| 2026-08-20 | UP | 10:20 | 29304.50 | TDO | 314.25 | rth(cur)_high | 163.75 | 0.52 | 1 | D |  | 152.75 | 75.25 |  |  |  |  |
| 2026-08-21 | DOWN | 09:30 | 29472.50 | TDO | 119.75 | day_mid | 63.88 | 0.53 | 2 | A | 09:35 | 252.25 | 60.25 | 09:31 | 71.00 | n | 95.00 |
| 2026-08-24 | DOWN | 09:30 | 29243.75 | london(cur)_low | 127.00 | synthetic_85pct | 107.95 | 0.85 | 0 | A | 09:31 | 296.00 | 73.25 | 09:30 | 112.00 | Y | 93.25 |
| 2026-08-25 | DOWN | 09:49 | 29416.00 | TDO | 252.00 | ny_morning(cur)_mid | 66.38 | 0.26 | 2 | A | 10:30 | 258.25 | 140.50 | 09:54 | 73.50 | n | 106.50 |
| 2026-08-26 | UP | 10:28 | 29156.25 | TDO | 109.25 | ny_morning(cur)_mid | 74.88 | 0.69 | 2 | A | 10:52 | 136.00 | 60.75 | 10:40 | 81.50 | Y | 72.00 |
| 2026-08-27 | UP | 09:57 | 29424.00 | london(cur)_high | 237.25 | rth(cur)_high | 179.00 | 0.75 | 1 | B |  | 235.00 | 227.50 | 10:27 | 187.25 | Y | 177.50 |
| 2026-08-28 | UP | 10:13 | 29505.50 | TDO | 163.25 | ny_morning(cur)_mid | 99.00 | 0.61 | 2 | A | 10:22 | 306.25 | -3.50 | 10:20 | 112.50 | n | 139.25 |
| 2026-08-31 | DOWN | 09:31 | 29517.00 | TDO | 164.50 | ny_morning(cur)_mid | 69.38 | 0.42 | 2 | B |  | 163.00 | 77.25 | 09:35 | 107.00 | Y | 98.50 |
| 2026-09-01 | UP | 09:41 | 29040.00 | TDO | 446.25 | ny_morning(cur)_high | 216.00 | 0.48 | 1 | B |  | 277.25 | 151.00 | 10:48 | 218.75 | Y | 205.75 |
| 2026-09-02 | UP | 09:55 | 29017.25 | asia(cur)_high | 154.00 | synthetic_85pct | 130.90 | 0.85 | 0 | A | 10:21 | 194.50 | 137.25 | 10:18 | 133.75 | n | 132.50 |
| 2026-09-03 | UP | 09:36 | 29199.25 | projection_up | 167.75 | week_mid | 47.87 | 0.28 | 2 | A | 09:49 | 348.75 | 328.50 | 09:37 | 61.50 | n | 136.25 |
| 2026-09-04 | DOWN | 09:58 | 29692.00 | TDO | 121.00 | ny_morning(cur)_mid | 79.00 | 0.65 | 2 | A | 10:26 | 214.25 | 116.25 | 10:12 | 85.75 | Y | 69.50 |
| 2026-09-07 | DOWN | 09:33 | 29646.00 | asia(cur)_low | 115.75 | ny_morning(cur)_mid | 54.00 | 0.47 | 2 | B |  | 82.25 | 41.75 | 11:08 | 55.00 | Y | 51.00 |
| 2026-09-08 | DOWN | 09:30 | 29686.75 | london(cur)_low | 208.25 | ny_morning(cur)_low | 150.75 | 0.72 | 1 | A | 09:44 | 262.25 | 114.25 | 09:41 | 153.50 | n | 187.75 |
| 2026-09-09 | UP | 09:30 | 29417.00 | TDO | 144.75 | day_mid | 64.25 | 0.44 | 2 | A | 09:59 | 177.00 | 40.75 | 09:35 | 70.00 | Y | 63.75 |
| 2026-09-10 | UP | 09:33 | 29057.75 | TDO | 369.75 | ny_morning(cur)_mid | 173.50 | 0.47 | 2 | B |  | 217.50 | 175.00 | 10:25 | 180.75 | Y | 147.50 |
| 2026-09-11 | DOWN | 09:59 | 29475.00 | TDO | 416.00 | ny_morning(cur)_low | 268.50 | 0.65 | 1 | D |  | 141.00 | 38.75 |  |  |  |  |
| 2026-09-14 | UP | 09:37 | 29167.50 | TDO | 144.25 | synthetic_85pct | 122.61 | 0.85 | 0 | A | 09:48 | 408.75 | 390.75 | 09:47 | 123.25 | Y | 152.00 |
| 2026-09-15 | DOWN | 09:45 | 29452.75 | london(cur)_low | 221.50 | ny_morning(cur)_mid | 42.88 | 0.19 | 2 | A | 10:51 | 237.00 | 172.50 | 10:03 | 51.25 | Y | 68.25 |
| 2026-09-16 | UP | 09:36 | 29373.00 | prev1_day_high | 122.75 | synthetic_85pct | 104.34 | 0.85 | 0 | A | 10:59 | 179.50 | 95.50 | 09:59 | 106.00 | Y | 102.75 |
| 2026-09-17 | DOWN | 10:28 | 29741.25 | TDO | 280.75 | ny_morning(cur)_low | 184.25 | 0.66 | 1 | D |  | 89.25 | 0.50 |  |  |  |  |

Caveats carried over from the v1 write-up: lookahead direction and entry, no stop, the 13:00
mark is a hold, 30 days / 10 failed-primary days. One v2-specific caveat: the selectors run at
`now` = the entry bar's 1m label on 1m frames (`index <= now`), so `ny_morning(cur)_high/low/mid`
and the running 6h block include the entry minute's FULL range — the fixtures show
`ny_morning(cur)_low == anchor` on 09-01 / 08-27 and `..._high == anchor` on 08-12 / 09-08 — where
a live 1s fill sees only the seconds up to the fill. `rth(cur)_*` excludes that minute in both.
The three `ny_morning(cur)_mid` picks among the reviewed days (08-12, 09-15, 08-25) embed it;
the trade-side extreme is the fill itself, so the effect is confined to the entry bar's
opposite wick. The `initial_target_selected` record now
carries `tier` and `variant`, so live fills accumulate the same view without a rerun.
