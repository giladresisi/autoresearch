# Code Review: harness-post-eval-foundation-upgrade

**Date:** 2026-03-26
**Branch:** master
**Scope:** train.py changes only (program.md is documentation)

## Stats

- Files Modified: 2 (train.py reviewed; program.md documentation-only, not reviewed for logic)
- Files Added: 0
- Files Deleted: 0
- New lines (train.py): ~16
- Deleted lines (train.py): ~5

---

## Issues Found

---

```
severity: high
file: train.py
line: 26-28
issue: TRAIN_END inline comment says "− 30 calendar days" but the actual value is 14 days before BACKTEST_END; the block comment above still says "14"
detail: BACKTEST_END = "2026-03-20" and TRAIN_END = "2026-03-06". The gap is exactly 14 calendar days
        (confirmed: (date(2026,3,20) - date(2026,3,6)).days == 14). The inline comment was changed from
        "BACKTEST_END − 14 calendar days" to "BACKTEST_END − 30 calendar days" but the date value was
        NOT updated. The block comment on line 26 still reads "last 14 calendar days … are held out as
        test set", creating a direct contradiction. Any future agent or developer reading the inline
        comment will believe the holdout window is 30 days when it is actually 14.
suggestion: Either (a) restore the inline comment to "BACKTEST_END − 14 calendar days" to match the
            actual date and block comment, or (b) if the intent was genuinely to extend the holdout to
            30 days, update TRAIN_END = "2026-02-18" and update the block comment on line 26.
            The block comment and the date value are the ground truth; the inline comment is the outlier.
```

---

## Non-Issues (confirmed correct)

**B1 — R15 window extended from 5 to 7 calendar days (lines 418–425):**
- `_today_date = df.index[-1]` is a `pd.Timestamp`; `position['entry_date']` is also a `pd.Timestamp`
  (set at line 682 from the same `today` loop variable). Subtraction `.days` is valid on the resulting
  `pd.Timedelta`. No type-safety concern.
- The change is a single integer constant bump (`5` → `7`). Logic, condition direction, and the
  `max(current_stop, price_1030am)` guard are unchanged and correct.
- The existing `max()` call ensures the stop is never lowered even under the extended window — the
  invariant holds.

**B2 — SMA50 slope filter for bull path (lines 311–319):**
- Index arithmetic: `close_hist.iloc[-55:-5]` produces exactly 50 elements when `len(close_hist) >= 55`,
  which is always true given the minimum history guard (`len(df) >= 102` → `len(hist) >= 101` →
  `len(close_hist) >= 101 >= 55`). The slice is safe at minimum history.
- The pattern mirrors the existing SMA20 slope check at line 304 (`close_hist.iloc[-25:-5]` for a
  20-bar window offset by 5), offset correctly to 50 bars: `[-55:-5]`. Arithmetic is consistent and
  correct.
- The `signal_path == "bull"` guard correctly exempts the recovery path, matching the stated intent
  (recovery path has a naturally declining SMA50 and is already filtered by its own SMA50 > SMA200
  requirement).
- No NaN guard is needed: `sma50` was already validated as non-NaN at line 273 before `signal_path`
  is set; `sma50_5d_ago` is computed from the same well-bounded slice on a series already confirmed
  to have sufficient length.
- The threshold `0.998` (0.2% tolerance) is numerically reasonable and consistent with the 0.5%
  tolerance used for SMA20 (a stricter filter on the slower SMA50 is appropriate).

**TRAIN_END date value itself:**
- The date `"2026-03-06"` is unchanged from before this diff, so no backtest window was accidentally
  shifted. The only issue is the misleading inline comment.
