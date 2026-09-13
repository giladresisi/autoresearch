# Entry Confirmation & Hypothesis Lifecycle — Concepts & Facts

> Concept doc: mechanics + lessons; the "when to arm" decision lives in
> `decisions/next-move.md` §7. Doc changes = strategy changes — backtest before merge (rule in
> smt.md header).

## 1. Direction engine precedence (`hypothesis._determine_direction`, first commit wins)

1. `rule1` — fresh sweep of a meaningful level (decisive).
2. `rule2b` — last high-priority sweep + daily-mid position (the workhorse; equilibrium.md §2
   + guards; conviction override + reversal lock apply here).
3. `rule2` — trending toward an unvisited level with momentum.
4. `rule3_4` — blend ≥ 0.35: `0.65*(0.55*premium-discount + 0.45*BOS(0.35×1hr+0.65×4hr)) +
   0.35*smt_score`.
5. `rule5_trend` — persisted `global.trend` fallback.

Standing conviction `|conv| ≥ 0.5` can flip rule2b/rule2/rule3_4 (GIL-32/33); the
same-liquidity reversal lock vetoes mechanical opposite flips (rule2b only).

## 2. From direction to fill

1. **Formation:** committed direction → hypothesis (entry ranges, stop, cautious ladder,
   target).
2. **Confirmation wait:** multi-bar confirmation before entry — a FEATURE; skipping it
   (market entries) failed twice (GIL-31 −$4,900; GIL-41 −$1,294).
3. **Reform:** decisive events reform the hypothesis; entry state resets only on
   none→direction transitions.
4. **Failed-entry persistence:** `failed_entries` + `cautious_dist_shrinks` increment per
   failed attempt and PERSIST across reforms; past `MAX_FAILED_ENTRIES` re-entries are capped
   and the cautious ladder tightens. A churny morning can lock out a clean afternoon
   (deliberate trade-off — the GIL-22 counter-reset made chop worse and was rejected).
5. **In-position:** cautious ladder (recomputed on fill), trailing, trend-broken exits,
   market-close before maintenance. The ladder's tight tiers are load-bearing (GIL-37 F2:
   widening them broke the cautious-exit→reform→re-enter chain, −$1.7k/2d).

## 3. Churn lessons (why agent outputs must be few and stable)

- Phase-3 (−$3,756/5d): ~54 reforms/day wiped multi-bar entry setups — direction was often
  right and money was still lost.
- GIL-21: 2-bar flip hysteresis saves trend/selloff days, not chop.
- GIL-38 (−$273.50): one stale-SMT exit cascaded a whole day.
- Implication: emit a stable bias, a DOL, an occasional "start looking for confirmation" —
  never a new direction every few minutes.
