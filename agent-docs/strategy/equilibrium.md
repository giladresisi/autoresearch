# Equilibrium (Premium / Discount) — Concepts & Facts

> Concept doc: definitions + facts only; weighting lives in `decisions/`. Doc changes =
> strategy changes — backtest before merge (rule in smt.md header).

Equilibrium (a range's midpoint) is rule2b's backbone: above = **premium** (favors selling /
sell-side draws), below = **discount** (favors buying) — as a prior, decisive only combined
with sweep events or SMT clusters.

## 1. Definitions (as computed)

- **Daily mid** = (day_high + day_low)/2 on RUNNING day extremes; **weekly mid** likewise on
  running week extremes. Both move as new extremes print; `daily_zone`/`weekly_zone` labels in
  every hypothesis reason.
- **Week anchor (ENGINE convention, `session_pipeline._week_start_ts`):** Sunday 18:00 ET,
  EXTENDED early-week — Monday session → prev **Thursday** 18:00, Tuesday session → prev
  **Friday** 18:00. Use this for week extremes/mid; not the trade-week first bar, not TWO.
- MNQ is the decision ticker; MES zone labels are mirrored context. A persistent zone-label
  disagreement between tickers is a standing RS regime (daily-trend D5).

## 2. Encoded, validated reads (rule2b)

- **Low swept in weekly DISCOUNT → up** (accumulation grab; fires unconditionally in
  discount). **High swept in weekly PREMIUM → down** (distribution), gated by the ATH /
  morning-ambiguity / recovery guards (liquidity-levels.md §2).
- **Daily-mid close-cross logic** for same-side cases: a post-sweep cross that **fails back**
  = strongest signal (go the other way); a committed pre-sweep cross with no opposite-level
  revisit = continuation sweep.
- **Time conditioning is literal in the code:** before 13:00 ET premium high-sweeps are
  ambiguous; at/after 13:00 (PM kill zone) they are decisive distribution. ATH guard AM-only
  (<12:00). Recovery threshold 2% AM / 3% PM.

## 3. Fact status of the broader time-equilibrium claims

| Claim | Status |
|---|---|
| Close vs daily mid decides same-side sweep reads | encoded & live |
| Weekly premium/discount gates accumulation/distribution | encoded & live |
| Time-of-day changes the meaning (13:00 boundary) | encoded & live |
| Failed equilibrium reclaim → move continues away | encoded & live (strongest single intraday tell) |
| **Weekly-mid touches/crossings as trigger events** | **anti-signal** — proven noise (GIL-39 B, +$83 suppressing them); the weekly mid is a zone boundary, not an event level |
| Monthly equilibrium | not computed anywhere; untested — soft prior at most |
| Multi-day persistence | untested here (strategy is intra-session) |
| MES's own zone read by a rule | not encoded; MNQ decides |

## 4. Caveats

- Running mids are volatile early in the session; rule2b's mid logic is fragile at the open —
  the 09:15–11:30 whipsaw window (session-structure.md) exists because the binary
  close-vs-mid invalidation kills fresh hypotheses there (O1 suspend flag, default OFF).
- Equilibrium is context. Promoting a context signal to a direct trigger has always lost
  money (weekly-mid TB, Phase-3). Blend, don't obey.
