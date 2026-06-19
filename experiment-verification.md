# Experiment Verification — GIL-39 (SMT conviction-seed-carry + suppress weekly-mid trend-broken)

Worktree: `C:\Users\gilad\projects\auto-co-trader\smt-conv-seed-wkmid-trendbroken`
Run dirs (1s A/B): baseline = `…\regression\sessions\<date>\_baseline_run`; change (both flags ON) = `…\<date>\22-59-04`.

## Verdict summary
| occurrence | date | time (ET) | change | verdict | one-line reason |
|---|---|---|---|---|---|
| #1 | 2026-06-10 | 10:45 | B | INCONCLUSIVE | No `weekly_mid` trend-broken exists anywhere on 06-10 baseline; in-window breaks are `daily_mid`/`day_high`. Premise absent → nothing to suppress; whole day byte-identical. |
| #2 | 2026-05-28 | 09:33 | B | INCONCLUSIVE (mechanism PROVEN elsewhere) | The day's only weekly_mid trend-broken is at **09:07:29** (outside 09:25–09:41) and IS suppressed in change; in-window events byte-identical. Suppression works, just not at the nominal time. |
| #3 | 2026-05-20 | 09:36 | B | INCONCLUSIVE (mechanism PROVEN elsewhere) | No weekly_mid event in 09:28–09:44; the day's weekly_mid trend-broken fires at 03:15–03:45 and 10:12 ARE suppressed in change. In-window stop-exits byte-identical. |
| #4 | 2026-06-10 | 00:00 (open) | A | OFFLINE-NOT-EXERCISABLE | Carry-seed conviction set empty (`[]`) at open in replay; `smt_dominant=null`; session-open hypothesis + full 31-event new-hypothesis direction sequence byte-identical. The documented live-only nuance — not a code failure. |

## Per-occurrence detail

### #1 — 2026-06-10 10:45 ET, Change B (window 10:37–10:53)
Baseline in-window: single `new-hypothesis dir=down rule=rule2b @10:40:00`. Nearby trend-broken activity (10:23:52 `day_high`, 10:24–10:35 `day_high` cooldown) is sourced from `day_high`, **not** weekly_mid. Across the entire 06-10 baseline file there are **0** `weekly_mid` trend-broken events (levels seen: `daily_mid`, `day_low`, `day_high`). Change run: trend-broken 22 = 22, and `events_1s.jsonl` is byte-identical (0 lines differ). The documented "weekly-mid cross emits trend-broken (dir=up)" does not occur in the 1s replay on this date.

### #2 — 2026-05-28 09:33 ET, Change B (window 09:25–09:41)
Baseline in-window: `09:33:40 stopped-out up`, `09:34:00 entry-gated up` — both byte-identical in change. The day's single `weekly_mid` trend-broken is at **`2026-05-28T09:07:29` (dir=up, price 30061.0, level weekly_mid 30061.5)** — present in baseline, **absent in change** (the only event removed all day; trend-broken 15→14). The adjacent `09:44:21`/`09:46:05` `daily_mid` breaks are correctly *retained* (suppression is weekly-mid-specific). Change B does exactly its job, but the suppressed fire is 26 min before the nominal window and whole-day trades stay byte-identical.

### #3 — 2026-05-20 09:36 ET, Change B (window 09:28–09:44)
Baseline in-window: `09:36 new-stop-exit`, `09:37`/`09:39 move-stop-exit`, `09:41 stop-exit (cautious-secondary-break)` — all `down`, byte-identical in change. No weekly_mid event falls in this window. The day's `weekly_mid` trend-broken fires — baseline `03:15:01, 03:33:11, 03:35:13, 03:40:01, 03:45:24` and `10:12:07` (all dir=up, level weekly_mid 29041.375) — are **all suppressed in change** (trend-broken 15→9). The 03:15–03:45 suppression lets the overnight up-hypothesis persist and fill (change adds `03:18:29 stop-entry-filled`, `03:41:08 filled`, …) — the one whole-day trade delta on these three dates. Mechanism proven, not at the nominal 09:36 occurrence.

### #4 — 2026-06-10 session open (~00:00 / overnight 18:10), Change A
Change-run end-of-session `hypothesis.json`: `smt_conviction_set = []`, `smt_dominant = null`, `direction = none`. First new-hypothesis of the session (`2026-06-09T18:10:00`, overnight open) identical on both sides: `dir=up rule=rule2b smt_score=0.0 smt_alignment=None`. The full 31-element new-hypothesis direction sequence is byte-identical baseline vs change. The carried-SMT/fill seed set is empty at open in offline replay, so the rule2b conviction override is never challenged — exactly the live-only carry-seed nuance in feature.md. Offline-not-exercisable (correctly flag-implemented, no input in regression), not a FAIL.

## Whole-day impact (1s, all trades)
| date | baseline n, pnl | change n, pnl | Δpnl | Δtrades | notes |
|---|---|---|---|---|---|
| 2026-06-10 | 20, −$1466.00 | 20, −$1466.00 | $0.00 | 0 | events byte-identical (no weekly_mid fire; empty seed) |
| 2026-05-28 | 16, +$1976.00 | 16, +$1976.00 | $0.00 | 0 | one weekly_mid trend-broken suppressed @09:07, no trade impact |
| 2026-05-20 | 29, −$513.50 | 30, −$430.50 | **+$83.00** | +1 | weekly_mid suppression @03:15–03:45 lets overnight up-hyp fill; +1 trade, +$83 |

## Bottom line
Both changes are implemented and flag-gated correctly, but **none of the four nominal occurrences flips current→desired in the offline 1s replay**. Change B (suppress weekly-mid trend-broken) genuinely works — it removes exactly the `weekly_mid`-sourced trend-broken events and leaves `daily_mid`/`day_high` ones intact — but on these three dates every weekly_mid fire lands *outside* the documented occurrence windows (06-10 has none at all; 05-28's is at 09:07 not 09:33; 05-20's are at 03:15–03:45/10:12 not 09:36). This is a live↔regression divergence: the live sessions evidently saw a weekly-mid cross at the noted times the 1s replay does not reproduce at the same clock. Whole-day cost/benefit of Change B is negligible-to-mildly-positive (06-10 and 05-28 byte-identical; 05-20 +$83/+1 trade from the genuine 03:15 suppression). Change A (#4) is offline-not-exercisable exactly as feature.md predicted — the carry-seed set is empty at the open in replay (`smt_conviction_set=[]`, `smt_dominant=null`), so its effect can only be observed live. Net: no occurrence is a clean PASS; B's mechanism is verified-real but mis-timed vs the table; A is unverifiable offline. Recommend re-deriving the occurrence timestamps from the actual suppressed weekly_mid fires (05-20 03:15–03:45 is the strongest, trade-affecting case) before judging graduation, and treating A's verification as live-session-only.
