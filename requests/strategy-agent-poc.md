# strategy-agent-poc — strategy-knowledge docs + decision-agent POC

## Vision (why this exists)

The user plans to add an **AI decision agent** into the trading strategy. The division of labor:

- **Scripts remain the "hard-truth" providers** — they find and flag SMTs, liquidity levels,
  sweeps, equilibrium touches, TDO/TWO, FVGs, etc. (exactly as today).
- **The AI agent makes the decisions on top** of that flagged data + the latest bars DataFrame:
  direction of the next move, when to start looking for entry confirmation (good setup forming),
  the next move's estimated target (DOL) as of now, when to pause the scripts, etc.
- The scripts then **carry out** the agent's decisions.

This experiment does **NOT** add the agent to the strategy. It prepares the **context/knowledge
documents** the agent will need, and runs a **POC** of the decision-making using Claude Code as
the agent. An evaluation system for decision consistency/coherence comes later — explicitly out
of scope now.

## Deliverables

### 1. `agent-docs/strategy/` folder + `smt.md`

Create `agent-docs/` and `agent-docs/strategy/` in the worktree root. Inside, write `smt.md` —
a comprehensive summary of ALL knowledge gained in this project about SMTs (from the code, the
docs, `.agents/experiment-log.md`, and the knowledge digest below). Requirements:

- Contains **definitions** (what an SMT is here: MNQ/MES divergence at liquidity levels, wick vs
  body, tiers, fulfillment, conviction, invalidation…), but is **mostly about effects**: how SMTs
  affect (and often DON'T affect, or affect only slightly) the graph's movement — reversals,
  liquidity sweeps, draw-on-liquidity (DOL) targets — and how the SMT effect **blends with other
  signals** (daily/weekly equilibrium, daily trend/bias, session-of-day, sweep side, clustering).
- Honest about negative results: the doc must encode what was DISPROVEN as prominently as what
  works, so the decision agent doesn't over-trust SMTs.
- **Structured and clear enough for a human to comprehend and update.** It will be
  version-controlled, and any proposed change to it must be **backtested before merging to
  master** (state this in a "How to update this document" section at the top or bottom).
- Do NOT commit — leave everything unstaged; the user reviews and decides.

### 2. Suggest the next doc topics

After smt.md, propose to the user the next topics for additional `agent-docs/strategy/*.md`
documents the decision agent will need during a live session. Ground the suggestions in what the
scripts actually flag (hard truth) vs what the agent must decide. Candidate seeds (validate
against the code, don't copy blindly): liquidity levels & sweep semantics; FVGs; daily/weekly
equilibrium + TDO/TWO; daily bias / trend determination; session structure & time-of-day windows;
entry-confirmation mechanics (rule1/rule2/rule2b/rule3_4, hypothesis lifecycle); position/exit
management & cautious ladders; regime character (mean-reversion vs trend days). Write the agreed
ones (or at minimum the list with 2-3 bullet scope each) — user decides how many to write now.

### 3. POC of the decision agent (after several important docs exist)

- Find a **recent meaningful trend** in the **MNQ 1s parquet** data (a clear directional move
  worth predicting).
- Copy aside a **slice of the parquet that ENDS BEFORE that trend starts** — the POC test agent
  must have **zero lookahead**. Put the slice in the worktree (e.g. `agent-docs/strategy/poc/`).
- Write `poc.md` describing to a **separate, fresh agent** how to run the test: read the
  `agent-docs/strategy/` documents + the parquet slice, then estimate the graph's **next move —
  direction and where that move is expected to end (target/DOL)** — and report that to the user.
- **Critical:** `poc.md` must not leak ANYTHING about the actual trend that follows the slice end
  (not its direction, not its magnitude, not why that date was chosen). Word it neutrally.
- The experiment agent then reports to the user: slice location, trend ground-truth (kept OUT of
  poc.md — put it in the Linear issue comment / a separate ground-truth file the POC agent is told
  not to read), and how to launch the POC agent.

## Verification for this experiment

Not a classic A/B regression. Success =
1. `agent-docs/strategy/smt.md` exists, is accurate against the code + experiment history, and a
   human can navigate/update it.
2. Additional doc topics proposed (and key ones written).
3. `poc.md` + lookahead-free parquet slice prepared, with ground truth recorded separately.
4. POC dry-run: a fresh agent given only poc.md + docs + slice produces a direction + target
   estimate (its correctness is informative, not pass/fail — this is a POC of the workflow).

## Where the hard-truth code lives (grounding anchors — re-verify in the worktree)

- `smt_detect.py` — SMT detection engine (pairs MNQ/MES at liquidity levels; V2 shadow engine with
  recency gate + divs/smt_score unification shipped via PR #73).
- `smt_conviction.py` — standing-SMT conviction lifecycle (residual + grace + sustain), feeds the
  rule2b/rule2/rule3_4 direction override (`_apply_conviction_override` in `hypothesis.py`).
- `hypothesis.py` — entry rules (rule1/rule2/rule2b/rule3_4), conviction override call sites
  (~:1861 rule2, ~:1898 rule3_4), hypothesis build/reform.
- `trend.py` — trend-broken handling, weekly-mid suppression flag (`SUPPRESS_WEEKLY_MID_TREND_BROKEN`),
  day-extreme SMT exit remnants (GIL-38, flag OFF).
- `session_pipeline.py` — per-bar pipeline, reform gating.
- `daily.py` — daily levels/bias scaffolding (GIL-29 shadow slot ~:337-352).
- Liquidity levels / FVG universe: search for `liquidities`, `liquidities_fvg_edges`,
  `__level_inv__` (GIL-25 depletion latch), `FULFILL_PTS`, `INVALIDATE_PTS`.
- `.agents/experiment-log.md` — canonical experiment history; `plan1-results.tsv` raw data.
- Parquets: `data/MNQ_1s.parquet` (worktree copy) and per-contract `main/<YYYY-MM>/` subfolders
  under the global root (contract rollover architecture; June→Sept roll done 2026-06-15).

## SMT knowledge digest (from the main agent's persisted memory — the handoff agent does NOT have this; treat as source material for smt.md)

**What an SMT is here.** A divergence between MNQ and MES at a shared liquidity level: one ticker
sweeps/exceeds the level while the other fails to (wick or body variants), implying smart-money
manipulation and a possible reversal. Levels have tiers (day/week/session/fixed prev-day/prev-week);
SMTs carry lead-ticker, side, fulfillment state, and (V2) a conviction score.

**Core statistical findings:**
- **SMT predictiveness study, May 1–Jun 16 2026, 33 days, 806 fires** (`_smt_pred_analysis.py` +
  `_smt_pred_out/`): only **9%** of fires precede a big trend; **15% fire wrong-way**. Signal is
  concentrated in **NY-AM session + high-sweep (bearish) + week-tier levels + 3–5-fire clusters**.
  Bullish/low-sweep SMTs **invert 22%** overall (**44% in NY-AM**). `fulfilled` is NOT a prediction
  flag. Top recommendation: **session × side gate** on SMT direction.
- **GIL-24 reframe (16-day correctness study):** raw SMT direction correctness **54%** (coin-flip);
  NO structural feature separates good from bad fires (filtering = DEAD END); the ONLY separator is
  **daily trend**: with-trend SMTs **87%** correct vs counter-trend **35%**. Recall 64% vs 38%
  placebo → a real source exists. Conclusion: **determine the daily trend first, then weight SMT
  fulfillment trend-conditionally — do not filter**.
- **GIL-18 (CANCELED):** the strategy is fundamentally a **mean-reversion/chop trader** — the chop
  tercile is its MOST profitable regime (+$36/trade). Never gate entries by regime/chop. Any SMT
  logic that surrenders the chop edge bleeds (see Phase 3 05-18 −$3,348).
- **GIL-17 (DISPROVEN):** sweep/momentum "relevance" gate on SMTs does not help (−8pp confirmed).

**What shipped (works, in master):**
- **GIL-25 level invalidation (SMT depletion latch):** per-ticker latch retires a level once a
  ticker runs `FULFILL_PTS[tier]` beyond it; skip pair if either side retired. FIXED levels
  (prev-day/week, asia/london/ny_*) are SINGLE-FIRE; only DYNAMIC day_/week_ levels re-arm.
  06-12 A/B: 1s −$304→+$881. Fan-out to cautious + hypothesis targets is wired live.
- **GIL-26 FVG edge levels:** each FVG split into per-ticker `_top`/`_bottom` edge levels in
  additive SMT-only key `liquidities_fvg_edges[_mes]`; per-edge clearing via
  `INVALIDATE_PTS["session"]`; captures first-cross divergences that fill-based detection missed.
- **PR #73 SMT V2 engine unification:** recency gate + divs/smt_score merge; legacy smt_score
  proven ~inert to direction (0 sign-flips/198).
- **GIL-32 (PR #85) + GIL-33 (PR #86) conviction override:** standing SMT conviction
  (|conv| ≥ 0.5) FLIPS rule2b/rule2/rule3_4 hypothesis direction to the SMT side at trend-starts.
  GIL-33 A/B: 05-08 −$859→−$139 (+$720) with rule2b days byte-identical. rule1 override fires 0×
  over May1–Jun16 → left out.
- **GIL-39 B (PR #92, flag-gated OFF):** `SUPPRESS_WEEKLY_MID_TREND_BROKEN` — weekly-mid
  (weekly equilibrium) trend-broken events suppressed; pooled +$83, all from 05-20.
  GIL-39 A (`SMT_CONVICTION_SEED_CARRY`) is inert offline (seed set empty at session open).

**What failed (encode as anti-patterns for the agent):**
- **Phase 3 "SMT-dominant drives direction" (DO NOT SHIP):** NET −$3,756 over 5 regime days.
  Direction was often RIGHT (7/8 biggest missed winners matched) but entry-state churn (~54
  reforms/day) blocked fills; chop days bleed worst. GIL-21 flip-hysteresis (2-bar) salvaged
  trend/selloff days (05-20 +$371, 06-05 +$748) but not chop. GIL-22 re-entry-veto DISPROVEN.
- **GIL-16 flip-fade anchor gate:** +$432/16d, not prod-ready — anchored on trailing-edge
  last_liquidity (bad anchor), UP-day false positives.
- **GIL-19 relevance rules:** +3pp pooled but regime-dependent (deep-V +13 / selloff +15 /
  **trend-up −13**): invalidation-drop + leg-suppress kill correct trend-CONTINUATION longs on
  transient pullbacks. Right on reversal, wrong on continuation.
- **GIL-38 day-extreme opposite-SMT exit:** −$273.50 pooled; root cause = conviction set retains
  residual SMTs → fires on the FIRST standing SMT hours early, cascades. Salvage = fresh-div-only
  / cluster gate (not built).
- **GIL-41 O5 with-trend chase:** −$1,294.50 pooled 4d; chases into exhaustion, perturbation
  cascade; hurts even on strong-down days. Abandoned.
- **GIL-31 displacement-gated fe==2 market entry:** −$4,900/36d; the gate can't distinguish a
  reversing body-poke from a trend-START body-poke → misses the day's big winner re-entries.

**Blending with other signals (the heart of smt.md):**
- **Daily trend/bias is the master key** (GIL-24: 87% vs 35%). GIL-29 designed a deterministic
  weighted-rules daily-bias decider (shadow `estimated_dir/confidence/dol_target` slot in
  daily.json; `bias_brief.py` 7 signal families incl. MSS + standing-unfulfilled-SMT) — designed,
  not yet executed.
- **Session-of-day matters:** NY-AM concentrates both the best signal (bearish/high-sweep) and the
  worst inversions (bullish/low-sweep 44%).
- **Weekly equilibrium (weekly-mid)** is a special level: its trend-broken events are noise
  (GIL-39 B suppression helps). TDO/TWO (true day/week open) are among the maintained levels.
- **Sweep side asymmetry:** high-sweeps (bearish SMTs) are much more reliable than low-sweeps.
- **Clustering:** 3–5 fires at the same zone is signal; a lone fire is mostly noise.
- **Fulfillment ≠ prediction; conviction lifecycle (residual/grace/sustain) can hold STALE SMTs —
  any decision rule reading conviction must prefer fresh divergences (GIL-38 lesson).**
- **Reversal vs continuation:** SMT-driven "price moved against → drop/flip" logic is right on
  reversal days and wrong on continuation days — a regime the agent must estimate before trusting
  SMT direction.

## Constraints

- No strategy-code behavior change in this experiment (docs + POC only; creating folders/files and
  read-only analysis of parquets is fine).
- Leave all changes UNSTAGED; never commit/merge/push.
- Docs are version-controlled going forward; doc changes must be backtested before merge to master
  (a doc change = a prompt/context change for the future agent = a strategy change).
- No evaluation system now.
- Machine timezone is Bangkok (UTC+7); ET is 11h behind — use `get_et_now()` semantics, never the
  machine clock, when reasoning about session times in parquets.
