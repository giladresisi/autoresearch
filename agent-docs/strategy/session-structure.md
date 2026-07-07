# Session Structure & Time-of-Day — Concepts & Facts

> Concept doc: facts only; time-of-day weighting lives in `decisions/`. Doc changes =
> strategy changes — backtest before merge (rule in smt.md header). All times **ET** (machine
> clock is Asia/Bangkok — never read it as ET).

## 1. The CME True Day

18:00 ET open → 16:55 ET close (maintenance 16:55–18:00); **trade date = close date**
(`session_times.py`). `TDO` = 18:00 open; `TWO` = Monday 18:00 open. Fixed daily levels are
computed at ~09:20; rule2b scans the FULL True Day so overnight sweeps are visible at the NY
open.

## 2. Sub-sessions & encoded boundaries

| Window (ET) | Name | Character / encoded meaning |
|---|---|---|
| 18:00–00:00 | asia | range formation; first pools of the day |
| 00:00–06:00 | london | first manipulation leg; its sweeps often set (or fake) the day |
| 06:00–12:00 | ny_morning | AMD window: highest SMT signal density AND worst bullish inversions (44%) |
| 12:00–17:00 | ny_evening | delivery; **13:00** = PM kill zone boundary (premium high-sweeps become decisive; recovery threshold 2%→3%) |

Other encoded times: 09:20 daily levels · **09:15–11:30 open-window whipsaw** (daily-mid
crossings + fresh-hypothesis kills there are mechanical chop; O1 suspend flag default OFF; O2
09:30–09:45 TP-on-touch flag OFF) · 09:50–10:10 old-era "silver bullet" clustering · <12:00
ATH-expansion guard · 6hr closes (00/06/12) mint new session-tier levels (eligible as SMT
targets only after their window closes).

## 3. Session-conditioned signal quality (measured)

NY-AM hosts the best bearish/high-sweep pocket AND the worst bullish/low-sweep inversions.
Overnight fires are sparser — overnight levels matter mostly as the pools NY trades against.
PM events are fewer and more decisive; late-day trend legs start there.

## 4. Patterns & calendar caveats

- **AMD arc:** Asia range → London/NY-AM manipulation (obvious pool swept, often the WRONG
  way first) → NY delivery. A morning sweep against prevailing structure is often the
  manipulation leg *confirming* it.
- PM events outweigh AM events for reversal reads at extremes (encoded).
- **Holidays/early closes** (e.g. July-4th observed): thin tape, plausibly range-leaning and
  extension-hostile — UNTESTED here; soft prior only.
