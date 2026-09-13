# DECISION: Next Move (direction · target · confidence)

> Pure policy — every change is a strategy change: **backtest before merging**. Numbers are
> v1 seeds pending calibration. Produced on EVERY call; consumes the standing
> [daily-trend](daily-trend.md) output as given (never re-derives it — that layering is the
> evidence dedup).

**Horizon:** "the next move" = the current/imminent leg from *now* until (a) it reaches its
target pool or (b) its flip trigger fires. Minutes-to-hours, sub-half-day. Day draw belongs to
daily-trend.

**Entry gate:** scripts open NEW positions only when this decision's confidence meets a
calibrated threshold. **Neutral/low = scripts behave exactly as today** — the gate must never
become a regime filter (GIL-18: chop is the most profitable regime).

## 1. Procedure (fixed order)

1. **Snapshot:** now (ET), session window, both tickers' zone labels, standing daily-trend
   output, level map + depletion states.
2. **Fresh-evidence ledgers** (§2): bull and bear built SEPARATELY, each argued as strongly
   as the evidence allows (prevents narrative capture — run 1's failure).
3. **Score** (§3), apply **vetoes/caps** (§4).
4. **Emit** (§6). **Churn guard:** no new fresh event and no fired trigger since the last
   call → return the standing answer unchanged. Direction changes only via the flip trigger
   (2-close hysteresis) or a genuinely new event re-scoring past threshold — never by
   re-narrating the same evidence.

## 2. Fresh evidence & scoring

```
item = tier_weight × session_side × alignment × freshness × whipsaw
```

- **tier_weight:** ATH/week 3.0 · day 2.0 · fill 1.5 · session 1.0.
- **session_side:** NY-AM high-sweep bearish **1.5** (best pocket) · NY-AM low-sweep bullish
  **0.4** (44% inversion) · PM (≥13:00) extreme sweep in premium/discount **1.3** · formed
  overnight **0.7** · else 1.0. (Non-divergent continuation sweeps are deliberately 1.0
  regardless of session.)
- **alignment** vs standing daily trend: with **1.0** · counter **0.3** (the 87/35 as a
  weight) · daily neutral → 0.6 both ways.
- **freshness:** `2^(−age/180min)`; items ≳3h old are spent (they were daily-trend food).
- **whipsaw:** inside 09:15–11:30 ET, intraday *structure* items (mid-crossings, hypothesis
  kills) ×0.5; open-window daily-mid crossings ×0. Level sweeps/SMTs are NOT structure items.

**Eligible items:** fresh SMT divergences (engine-fire semantics: one per
level|direction|type; a re-dip of a swept, depleted level is NOT a new fire) · fresh sweeps
read via the grab-vs-continuation table (liquidity-levels.md §2) · failed reclaims of the
daily mid (weight 2 — the strongest intraday tell; requires an actual close beyond the mid
that fails back) · displacement/MSS through structure · the two **continuation items** below.
**Never** weekly-mid touch/cross events (proven noise, GIL-39 B). Do NOT invent item types —
anything not listed here is context, not a ledger item.

**Continuation items (defined; v1 seeds):** with-trend evidence on quiet delivery sessions —
(a) **mid-rejection:** a push of ≥100 MNQ pts toward the daily mid that reverses WITHOUT any
1m close beyond it — day tier 2.0, freshness from the rejection extreme's time; (b)
**sustained acceptance:** ≥90% of 1m closes on one side of the daily mid from the last
checkpoint to now — day tier 2.0, freshness `2^(−(window_age/2)/180)`. Each counts at most
ONCE per call; normal session-side/alignment multipliers apply.

**Event boundary vs daily-trend:** events at/after 06:00 ET belong here; pre-06:00 events
enter only through the standing daily-trend output (citable as context, never scored).

**Per-physical-event dedup (mandatory):** ONE push (one wick/displacement within a few
minutes) counts ONCE at the HIGHEST-tier level name it touched; same-price/same-push extra
names are not items. Mark collapses in the ledger.

**Clusters:** ≥3 **distinct physical events** (post-dedup, separate pushes) converging on one
zone within the decay window → ×1.5 ONCE on the zone total (never for name confluence).

**Cross-ticker rule:** a divergence is fresh only if the relationship CHANGED inside the
decay window; a persistent multi-day RS regime is daily-trend D5 material, not a per-call
item.

**CANDIDATE item — laggard test-and-fail (uncalibrated; label "CANDIDATE (laggard-fail)"):**
the paired ticker reaches within 25% of the tier's depletion threshold of a FIXED level and
FAILS, while the other ticker long ago accepted-beyond its same-name level. Never an engine
fire (the leader's leg is stale/depleted — GIL-25 latch), which is why it needs its own type.
Divergence-flavored evidence AGAINST the tested side, **fresh at the laggard's failure time**,
scored at the fixed level's tier with normal multipliers. Motivating case: 2026-06-25 08:30 —
MES failed 0.5 under its prev1_day_high with MNQ 105+ beyond; −910 crash followed.

**Contested events:** one physical event enters exactly ONE ledger (per the classification
table), but the REJECTED reading must be listed with its would-be score. Never score both.

**Ticker scope:** MNQ is the decision ticker — MNQ equilibrium items full weight; the same
class occurring ONLY on MES counts ×0.5. Cross-ticker divergence items unaffected.

## 3. Direction & confidence

N = Σbull − Σbear. **Direction:** up if N ≥ +3 · down if N ≤ −3 · else neutral.

| Confidence | Requires |
|---|---|
| high | \|N\| ≥ 6 · losing ledger ≤ half the winning · NO §4 cap in effect |
| medium | \|N\| ≥ 3 · no cap-to-LOW in effect (cap-to-MEDIUM doesn't block this tier) |
| low | everything else |

Vetoes are CAPS with explicit strengths — a cap-to-medium day (neutral daily trend) can still
reach medium; otherwise the entry gate would be permanently cold on neutral days.

## 4. Vetoes/caps (checked AFTER scoring; cannot be outvoted)

| # | Veto | Effect |
|---|---|---|
| 1 | Fresh (≤60 min) top-pocket counter-signal: NY-AM high-sweep bearish SMT vs an UP call, or the PM kill-zone mirror | **cap-to-LOW**; if that single item ≥ half the winning ledger → output **neutral** with both-way resolution triggers. *(This alone flips run 1.)* |
| 2 | Unverifiable load-bearing input | one tier down |
| 3 | Standing daily-trend confidence low or direction neutral | **cap-to-MEDIUM** |
| 4 | Range/hybrid regime | **targets only**: stay inside the day's extremes; cross-weekly-mid targets require regime = trend |

## 5. Target / DOL

- **Move target:** the NEAREST meaningful un-swept, un-depleted pool in the chosen direction;
  pools within ~50 MNQ pts = one zone (name its elements); prefer level+FVG confluence. The
  move ENDS at first touch (partial delivery) — holding through needs a fresh decision.
- **Day draw:** daily-trend's output; cite only as context.

## 6. Output schema

```
next_move:
  direction: up | down | neutral
  confidence: high | medium | low
  move_target: <zone + price(s)>                  # neutral: none + resolution block below
  flip_trigger: <close condition; 2 consecutive 1m closes>
  flipped_target: <where the flip goes>
  arm_entry_confirmation: yes | no                # §7
  ledgers: <bull, bear, rejected contested readings, veto checklist>
resolution (neutral form):
  long_if:  <close condition above>  -> target: <buy-side pool>
  short_if: <close condition below>  -> target: <sell-side pool>
  # a fired resolution trigger (2-close hysteresis) converts to that direction at LOW
  # confidence pending the next full pass
```

## 7. arm_entry_confirmation

**yes** only when: the grab signature is complete at a meaningful pool (sweep + divergence +
equilibrium rejection) in the call's direction, OR a pullback to equilibrium/FVG is completing
in a confirmed trend; AND not inside 09:15–11:30 without overnight/HTF backing; AND
`failed_entries` isn't near the cap (a failed low-conviction arm burns a scarce slot).
Neutral/range read → **no** (the baseline machinery already trades mean-reversion; don't
interfere).
