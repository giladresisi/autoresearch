# POC Result 3 — Decision-Agent Dry Run

**Agent:** fresh decision agent, blind protocol run 3.
**"Now":** 2026-06-30 09:27:59 ET (Tuesday). Trade date 2026-06-30. Sub-session: ny_morning;
**inside the 09:15–11:30 whipsaw window.**
**Daily-trend checkpoint used:** 2026-06-30 09:20:00 ET (most recent at/before now).
**Hard state:** MNQ ATH 31100.25 / MES ATH 7695.00 (provided); position flat; `failed_entries = 0`,
`cautious_dist_shrinks = 0`.
**Facts source:** `derive_facts.py --ath-mnq 31100.25 --ath-mes 7695.00` over the two 1s slices
(plus spot-reads of its S4/S5 tables). All prices MNQ unless marked MES.

Tape summary (facts only): prior week 2026-06-22..27 was a down-expansion week
(30965.5 → 29162.5). Monday 06-29 was a strong recovery day (29280.5 → close 29995.5, range pos
0.91). Overnight into Tuesday price continued up, sweeping and DEPLETING `prev1_day_high` 30069
(20:06, +148 beyond) and `asia(cur)_high` 30130.25 (00:01, +86.75), topping at 30217 (01:03,
London). From 06:59–07:45 MES made a new day/week high (7517.5) while MNQ failed (30174 vs
30217). At 08:00–08:31 both tickers displaced down and swept `london(cur)_low` simultaneously;
MNQ broke its overnight 1hr higher-low chain (below 30019.5) to 29997, and sits at 30035 now —
below the daily mid 30076.12, above the weekly mid 29746, essentially flat on the True Day
(TDO 29995.5).

---

## Section 1 — Daily-trend decision (checkpoint 09:20 ET)

### 1.1 Underlying physical events (for the correlation audit)

| ID | Event |
|---|---|
| E1 | Monday 06-29 NY recovery rally (~09:30–16:00): 4hr CHoCH up off 29162.5, reclaimed the weekly mid, closed 0.91 of range |
| E2 | Overnight continuation push (20:06 → 01:03): swept `prev1_day_high` 30069 (+148, DEPLETED) and `asia(cur)_high` 30130.25 (+86.75, DEPLETED); 4hr close chain higher; high 30217 |
| E3 | Overnight stall (01:03 → 06:00): drift 30217 → ~30080, repeated daily-mid crossings 03:50–06:12, held above London low 30019.5 |
| E4 | Morning breakdown 08:00–08:31 + failed bounce (post-06:00 → **belongs to next-move**, not scored here; used only to CLASSIFY E2 per D4's rule) |
| E5 | Prior-week down expansion (30965.5 → 29162.5) |

### 1.2 Driver audit table

| # | Driver (wt) | Vote | Weighted | Evidence (numbers) | Underlying events / sharing |
|---|---|---|---|---|---|
| D1 | HTF structure (3) | **0** | 0 | 4hr chain: up (CHoCH off 29162.5 via E1; BOS above the 30157 pivot during E2; higher closes 29516→30167). 1hr chain: NOT aligned — 01:00–04:00 printed lower highs AND lower lows (30217→30172→30151; lows 30098→30070→30019.5), then only a lower-high bounce to 30174 (07:45). Doc rule: "aligned chain = trend vote; mixed = neutral" → chains disagree → 0. (The fresh 08:31 1hr MSS down is <3h-old tape → next-move item B2, not consumed here.) | E1, E2. E2 also cited by D4 — marked shared; moot since both drivers vote 0. |
| D2 | Weekly equilibrium acceptance (2) | **+1** | +2 | Accepted ABOVE the weekly mid. Trade-week anchor mid = 29746.0: 100% of 1m closes above, session→ckpt (n=914) and last 6h (n=360). TWO-anchor mid = 30076.125 (degenerates to the daily mid on week-day 2): 66% of last-6h closes above at ckpt → still majority-above. No failed reclaim of the weekly mid (never approached). Sign convention: accepted above → up vote. | Acceptance is a state over the overnight window (E2/E3 period). Origin of the position is E1's reclaim — E1 is counted at D3; no event double-counted at full weight (flagged in §3 gaps). |
| D3 | Prior-day / week context (1) | **+1** | +1 | Yesterday closed at 0.91 of its range (29995.5 in 29275–30069) = strong close near highs → up. Prev week was a down-expansion week (E5) — context against, but the close-location criterion is the doc's named test. | E1 (its close). First full-weight counted appearance of E1 (D1 voted 0, consuming nothing). E5 noted, not separately voted. |
| D4 | Overnight sweep complex (2) | **0** | 0 | Pools swept overnight: `prev1_day_high`/`ny_evening(prev1)_high` 30069 (one physical push, 20:06) and `asia(cur)_high` 30130.25 (00:01). Both ran far beyond depletion thresholds (+148 vs 40; +86.75 vs 20) = accepted-beyond → continuation-up. BUT post-06:00 classification (permitted): by the 09:20 checkpoint price (30022) had round-tripped back BELOW every swept pool. Accepted-for-12h-then-fully-given-back fits neither "accepted" nor "rejected" cleanly → mixed → 0. | E2 (+E4 used only as classifier). E2 shared with D1 (both neutral). |
| D5 | Standing SMT residue (1) | **0** | 0 | No standing unfulfilled week/day-tier divergence at the checkpoint: the `prev1_day_high` transient divergence (MNQ swept 20:06, MES lagged to 23:50) resolved when MES confirmed. MES>MNQ relative strength (2.58% vs 3.43% below ATH; MES new week high 07:15 unconfirmed by MNQ) is <1 day old → not a persistent RS regime; its fresh expression is next-move item B1 (must not double-count). | — |
| D6 | ATH / recovery regime (1) | **+1** | +1 | Hard ATH provided: MNQ last close 3.43% below 31100.25 → recovery regime (up-drift with violent shakeouts) → up. MES 2.58% below 7695.00 (inside the 2–3% boundary band — noted). | ATH distance is state; no shared events. |

### 1.3 Score and output

```
S = D1(0×3) + D2(+1×2) + D3(+1×1) + D4(0×2) + D5(0×1) + D6(+1×1) = +4.0
```

Direction: **up** (S ≥ +3). No weight-≥2 driver opposes (none vote down at all).
Confidence: |S| = 4.0 < 6 → **medium** (medium conditions met: |S| ≥ 3, at most one weight-≥2
opposer — actual: zero).

Sensitivity (stated, not scored): under a permissive D1 reading ("4hr dominant carries the vote,
1hr merely modulates" — the 0.65-share language), D1 = +1 → the audit then charges E1 to D1 and
discounts D3 to ×0.5 → S = 3+2+0.5+0+0+1 = +6.5 → high. Direction is **up under both readings**;
only the confidence tier moves. I emit the stricter reading (see Doc gap G2).

```
daily_trend:
  direction: up
  confidence: medium
  regime: hybrid
  day_dol: 30217.0 (day/London high) stacking into the unvisited 1hr bear FVG
           30268.75–30301.5 (gap between them 51.75 pts — marginally beyond the ~50-pt
           one-zone rule, so named as a stack, not one zone). The unvisited 4hr bear FVG
           30474–30534.5 and prev1_week_high 30965.5 are NOT today's draw under a hybrid
           regime (cross-extreme extension requires regime = trend).
  weakens_to_neutral_if: 5m close below 29935.25 (asia(cur)_low = session low; loses the
           overnight accumulation shelf and the TDO/TWO cluster 29995.5–30005.5 with it)
  flips_if: 2 consecutive 5m closes below 29835.0 (ny_evening(prev1)_low, which also means
           acceptance back inside Monday's range and into the unvisited 1hr bull FVGs
           29847.5–29916.0) — subject to the 2×5m-close hysteresis
  drivers: table in §1.2
```

Regime call (§6 tells): trend tells — one-sided overnight expansion (29935 → 30217), sequential
same-side sweeps that DEPLETED (30069, 30130.25); range tells — repeated daily-mid crossings both
ways 03:50–06:12, morning rejection back from the extreme (full round-trip to the True-Day open),
1hr chain mixed. Both columns populated → **hybrid**: expected to mean-revert between 29935–30217
unless one edge is accepted-beyond.

---

## Section 2 — Next-move decision (now = 09:27:59 ET)

Standing daily-trend input (from §1, consumed as given): **up / medium / hybrid**.
Snapshot: MNQ 30035.0 — weekly zone PREMIUM (mid 29746.0), daily zone DISCOUNT (mid 30076.12).
MES 7496.25 — weekly premium (mid 7457.75), daily discount (mid 7499.625). Whipsaw window
09:15–11:30 ACTIVE at call time (items below all pre-date 09:15; penalty shown per item).
Alignment multiplier: daily trend = up → with-trend (bull) 1.0, counter-trend (bear) 0.3.

### 2.1 Bear ledger

| # | Fresh event (post-06:00 only) | tier_w | × sess_side | × align | × fresh | × whip | = score |
|---|---|---|---|---|---|---|---|
| B1 | 06:59–07:15: MES sweeps `london(cur)_high` 7514.5 (06:59:11) and prints a new **day and week high** 7517.5 (07:15:18) while MNQ fails (peak 30174 at 07:45 vs 30217) → fresh bearish cross-ticker divergence, lead MES, NY-AM high-sweep. One physical push; scored ONCE at the highest-tier name touched = week (dynamic week_high; name-collapse performed over london(cur)_high [session] and day_high [day]). | 3.0 | 1.5 (NY-AM high-sweep bearish — best pocket) | 0.3 | 0.563 (age 149 min: 2^(−149/180)) | 1.0 (event pre-09:15) | **0.760** |
| B2 | 08:00–08:31:51: displacement down; BOTH tickers sweep `london(cur)_low` (30019.5 / 7494.0) in the same second; MNQ runs +22.5 beyond (depleted) and breaks its overnight 1hr higher-low chain (MSS below 30019.5, low 29997). Grab-vs-continuation classification → CONTINUATION-down: both tickers took the level cleanly; low-sweep in weekly PREMIUM is not against-context; no reclaim of the daily mid afterwards. (Contested bull reading — sweep-and-reclaim of the level itself — noted in §2.2, not scored: one physical event, one ledger.) Scored at the swept level's tier (session; MSS itself has no tier — gap G5). | 1.0 | 1.0 (low-sweep read bearishly = "anything else") | 0.3 | 0.806 (56 min) | 1.0 | **0.242** |
| B3 | 08:42–08:56: failed reclaim of the daily mid — MES closes above its mid 7499.62 three times (08:42/08:46/08:49) and fails back below each time (last 08:56); MNQ's parallel bounce caps at 30057.75 without ever reaching its mid 30076.12. Item type "failed reclaim of the daily mid", explicit weight 2. (MNQ is the decision ticker and had no reclaim attempt to fail — ticker ambiguity flagged, gap G8.) | 2.0 | 1.0 | 0.3 | 0.884 (32 min) | 1.0 | **0.530** |

Cluster check: three distinct physical events but at three different zones (30174–30217 top /
30019.5 / mid 30076) → **no ×1.5 cluster bonus**.

**Σ bear = 0.760 + 0.242 + 0.530 = 1.532**

### 2.2 Bull ledger

Argued as strongly as the evidence allows — and the honest answer is that no eligible fresh
(post-06:00) bull item exists:

| # | Candidate | Why not scored |
|---|---|---|
| U1 | Overnight buy-side acceptance complex (sweeps of 30069 / 30130.25, depleted) | Pre-06:00 → daily-trend D4 evidence; enters only through the standing daily-trend output. Context, not a ledger item. |
| U2 | `london(cur)_low` sweep-and-reclaim as an SSL grab (price back above 30019.5 now) | Same physical event as B2; classified continuation-down by the grab-vs-continuation table (both tickers swept cleanly, in-premium, no mid reclaim). One event, one ledger. Would anyway score only 1.0 × 0.4 (NY-AM low-sweep bullish — 44% inversion pocket) × 1.0 × 0.81 ≈ 0.32. |
| U3 | Defense of the TDO/TWO cluster 29995.5–30005.5 (lows 30005 at 08:31, 29997 at 09:15 — never crossed TDO 29995.5) | No level was swept; not an eligible item type. Noted as context. |
| U4 | MES relative strength (new week high) | The same physical push as B1, already scored (bearishly, as non-confirmation — that is the framework's reading of it). |

**Σ bull = 0.000**

### 2.3 Score

```
N = Σ bull − Σ bear = 0.000 − 1.532 = −1.532
−3 < N < +3  →  direction NEUTRAL
```

### 2.4 Veto checklist (each checked explicitly)

| # | Veto | Check | Result |
|---|---|---|---|
| 1 | Fresh top-pocket counter-signal (≤60 min NY-AM high-sweep bearish SMT against an UP call, or mirror) | Call is neutral, not up; and B1 is 149 min old (>60 → decayed per the veto's own clock) | **PASS — not triggered** |
| 2 | Unverifiable load-bearing input | ATH provided as hard truth; all levels/mids script-computed; the week-anchor ambiguity does not change any scored item (B1 is a new week high under BOTH anchors) | **PASS — not triggered** |
| 3 | Standing daily-trend confidence low / direction neutral → cap-to-MEDIUM | Standing daily trend = up / **medium** — neither low nor neutral | **PASS — not triggered** |
| 4 | Range/hybrid regime → target constraint only | Regime = **hybrid → TRIGGERED**: targets confined to the day's established extremes 29935.25–30217.0; cross-weekly-mid targets forbidden (weekly mid 29746 is below both edges — no conflict) | **APPLIED (targets only)** |

### 2.5 Confidence

|N| = 1.532 < 3 → **low** (§3 table: "everything else"). No cap needed to get there.

### 2.6 Output

```
next_move:
  direction: neutral
  confidence: low
  move_target: none (neutral). Two-sided resolution pools, per the §4-style both-ways form:
    sell-side: asia(cur)_low 29935.25, then ny_evening(prev1)_low 29835.0 + unvisited 1hr
               bull FVGs 29847.5–29916.0 (one confluence shelf);
    buy-side:  daily mid 30076.12 (running), then day high 30217.0 (hybrid cap).
  flip_trigger: (resolution triggers both ways)
    short if 2 consecutive 1m closes below 29935.25;
    long  if 2 consecutive 1m closes above 30076.12 (running daily mid — recompute as
          extremes move).
  flipped_target:
    short fires → 29835.0–29916.0 shelf (ny_evening(prev1)_low + 1hr bull FVG stack);
    long fires  → 30217.0 (day high; anything beyond needs a fresh decision at the touch).
  arm_entry_confirmation: no
  ledgers: §2.1 (bear, Σ=1.532), §2.2 (bull, Σ=0.000), vetoes §2.4
```

`arm_entry_confirmation: no` — three independent grounds: (1) neutral/range-leaning read → the
doc mandates "no" (baseline machinery already trades mean-reversion profitably); (2) the call
sits inside the 09:15–11:30 whipsaw window and the freshest evidence is counter to the standing
bias, i.e. no completed grab signature in any call direction (the bear evidence lacks a
divergence-at-pool + equilibrium-rejection combo at a sell-side pool; the bull side has no fresh
signature at all); (3) although `failed_entries = 0`, a low-conviction arm burns a scarce
re-entry slot.

Stability contract: this output stands until a genuinely new fresh event re-scores a ledger past
±3 or one of the two flip triggers fires with its 2×1m-close hysteresis.

Sensitivity (stated for the record): had the standing daily trend been NEUTRAL (alignment 0.6
both ways), the same bear ledger would sum to 3.06 → direction "down" exactly at threshold, with
veto 3 capping confidence to medium. The alignment multiplier on the daily call singlehandedly
decides neutral-vs-down at this cut — see gap G9.

---

## Section 3 — Doc gaps

Ambiguities hit, inputs lacked, contradictions found (in rough order of impact on this run):

- **G1 — Week anchor is unspecified.** liquidity-levels/equilibrium define week levels on
  "running week extremes" but never say whether the week starts at the trade-week's first bar
  (Sun 18:00) or at TWO (Mon 18:00). The script prints both because of this. At this cut the
  weekly mid is 29746.0 vs 30076.125 — a 330-pt disagreement; on week-day 2 the TWO-anchor
  weekly mid even degenerates to the daily mid exactly. D2's vote happened to survive both
  anchors (100% vs 66% acceptance above), and B1 is a new week high under both, but early-week
  sessions will not always be this lucky. The docs must pick an anchor.
- **G2 — D1 "aligned chain" is decisive and ambiguous.** Does a trend vote require the 4hr and
  1hr chains to AGREE (strict reading: mixed → 0), or does the 4hr dominate (the "0.65 share"
  language) with 1hr as modulation? At this checkpoint the two readings give S = +4.0 vs +6.5,
  i.e. medium vs high confidence — a full tier hinging on one sentence. I took the strict
  reading; the doc should state which is meant and what "aligned" requires of the 1hr leg.
- **G3 — Correlation-audit mechanics for categorical votes.** The ×0.5 second-appearance rule
  is defined for *events*, but drivers emit categorical −1/0/+1 votes × weight. When a driver's
  vote rests on several events of which one is shared, it is unclear whether the discount
  applies to the whole weighted vote, to nothing (if unshared events alone justify the vote), or
  pro-rata. Related: does a driver that votes 0 "consume" its events? (I assumed no — hence D3
  kept E1 at full weight after D1 voted 0.) The audit needs one worked multi-event example.
- **G4 — D4 has no verdict for "accepted, then fully given back."** The overnight sweeps ran
  148 pts / 86.75 pts beyond (far past depletion = the doc's own continuation criterion), held
  12 hours, and were then round-tripped below before the checkpoint. Binary accepted/rejected
  does not cover this; I voted 0. The doc should say whether depth-and-duration beyond the level
  locks in "accepted" or whether the checkpoint-time position overrides.
- **G5 — No tier weight for displacement/MSS items.** next-move §2 prices items by level tier,
  but "displacement/MSS through structure" touches no pool by definition. I fell back to the
  tier of the level swept in the same push (session, 1.0); an MSS through a 1hr chain arguably
  deserves day-tier authority. Unspecified.
- **G6 — Tier of cross-ticker new-extreme divergences on dynamic levels.** The doc's own example
  ("MES printing a new week high NOW while MNQ lags = fresh, week tier") grants week tier via a
  *dynamic running extreme* — exactly the trailing-edge anchor class GIL-16 warns about, and it
  triples the item's score vs the fixed-level name in the same push (3.0 vs london(cur)_high's
  1.0; 0.76 vs 0.25 in B1). Confirm week tier is intended for running-extreme divergences.
- **G7 — May one contested physical event appear in both ledgers?** The 08:31 London-low push is
  a textbook two-reading event (MSS-down continuation vs SSL-grab-and-reclaim). The dedup rule
  ("one physical push counts ONCE") plus the classification table forced it into one ledger by
  my judgment; the adversarial-two-ledgers idea arguably wants the bull reading priced too.
  Unspecified.
- **G8 — Ticker scope of ledger items.** equilibrium.md: "MNQ is the decision ticker; no encoded
  rule reads MES's own zone label." next-move §2: "failed reclaims of the daily mid" with no
  ticker restriction. The only true failed reclaim at this cut was MES's (B3, 0.53 — a third of
  the bear ledger); MNQ never attempted its mid. Contradiction between the concept doc and the
  decision doc; needs an explicit rule (MNQ-only, either-ticker, or MES at a haircut).
- **G9 — The 87/35 alignment multiplier double-serves as a churn guard and dominates marginal
  calls.** With daily = up, the bear ledger lands at 1.53 (neutral); had daily been neutral, the
  identical evidence sums to 3.06 → "down" exactly at threshold. A single tier of the *other*
  decision's confidence (medium vs low→veto-3) plus its direction fully determines this call.
  Not a bug per se — but the docs nowhere acknowledge that next-move direction is, in marginal
  regimes, an almost pure function of the standing bias. Worth stating, and worth calibrating
  whether counter-trend 0.3 should scale with daily-trend confidence (e.g. 0.3 high / 0.45
  medium).
- **G10 — Veto 1's freshness cliff.** "Undecayed (≤60 min)" sits oddly against the 180-min
  freshness half-life: a 61-min-old top-pocket SMT still carries 79% of its score yet is exempt
  from the veto (B1, at 149 min / 56%, was exempt here). If intended, fine; if not, the veto
  window should reference the decay curve rather than a hard hour.
- **G11 — `move_target` schema for neutral outputs.** §5 defines the target as "the nearest pool
  in the chosen direction," and §6 has no neutral form; only veto 1's severe branch describes a
  "neutral with resolution triggers both ways" output. I reused that form for a scored neutral
  (§2.6). The schema should define it explicitly.
- **G12 — Minor input artifacts.** (a) The slice's first current-week session opens 17:00:52 ET
  (not 18:00), so TDO comes from a 17:00-hour bar and Monday's session spans Sun 18:00→Mon
  17:00 — presumably a slice-boundary artifact; TDO/TWO land 10 pts apart and nothing turned on
  it, but exact-open conventions matter for TDO-anchored rules. (b) prev2_day (2026-06-27) is a
  four-tick stub session (MES: one price for OHLC) — its levels (29290/29285.75) are technically
  eligible day-tier pools but are really Friday-evening remnants; no rule addresses stub
  sessions. (c) MES's london(cur)_high excursion equals its depletion threshold exactly
  (3.00 vs 3.0) — boundary semantics (≥ vs >) decide its depletion state; unstated in the docs.
