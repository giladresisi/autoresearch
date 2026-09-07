# l2-target-selection.md — target selection measured on production's own menu

**Status: measurement output, not implemented.** Nothing here runs in production. It records
what the three candidate selectors are worth when they are restricted to the menu production
actually builds, and it closes one candidate outright.

**Sibling to `l2-targets.md`, and it must never be merged into it.** `l2-targets.md` is
cycle 4's write-up, whose universe was *every named level measured from the move origin*.
This document is the same question asked on **`build_menus`'s own rows, anchored at a clock
instant** — the universe mismatch `l2-targets.md` records as a MUST-FIX. Same subject,
different universe, and the answers differ enough that keeping them apart matters.

**Figures here are NOT pinned by `agent/study/named_cases.py`.** The per-date harness is
committed (`agent/study/target_offline.py`); the 84-date sweep drivers were deliberately
kept as scratch and are gone. This document is therefore the record, not a derivation — a
re-measurement must rebuild the driver.

---

## 1. Scope, fixed before measuring

Selection is **menu-only** (the pick is always a `build_menus` row) and the latest honest
instant is the **entry fill**. Direction and entry are assumed correct, with total lookahead,
so selection is isolated from everything upstream — the same discipline `l2-targets.md` §1
used.

Three selectors, all emitted from one code path:

| | selector | instant |
|---|---|---|
| **T1** | nearest-first `D1` | 09:20 L1 boundary |
| **T2** | nearest-first `D1` | the entry fill |
| **T3** | cycle-4 B8g hazard argmax over T2's rows | the entry fill |

---

## 2. How it was measured, and what that measurement cannot see

84 corpus dates (`.agents/label-corpus/`), MNQ primary segments, 1m facts. Entry = the best
possible fill in the move's direction before its extreme. Capture is measured **from the
fill**; targets are anchored on `now_price` at the decision instant. Perfect-exit ceiling
= **31,867 pts** (median move 306). B8g is refit per date with that date excluded.

**Four limits, all load-bearing:**

- **No stop and no losing side.** A missed target scores 0, never a loss. Max adverse
  excursion is *identically zero* because a perfect fill has no drawdown by construction —
  so this measurement structurally cannot see stop risk. **None of these numbers is P&L.**
- The optimal-fill assumption flatters every selector equally, but shifts absolute
  distances; a realistic fill would change the magnitudes.
- Direction is assumed correct on all 84 dates.
- Oracle figures are lookahead upper bounds and are unachievable by construction.

`regression.py` is the only thing that can settle any of this.

---

## 3. What each selector is worth

| selector | captured | % of ceiling |
|---|---|---|
| T1 — `D1` at 09:20 | 11,814 | 37.1% |
| **T2 — `D1` at the fill** | **12,966** | **40.7%** |
| T3 — B8g at the fill | 12,553 | 39.4% |
| ORACLE — perfect menu ranking | 16,486 | 51.7% |

Perfect ranking is worth **at most 3,520 pts (11% of the ceiling)**, and all of it sits in
the 56 dates where a menu entry was reachable — the oracle gains **exactly zero** on the
other 28.

---

## 4. B8g — CLOSED

**B8g ranks better and selects worse.** It names the drawn-to pool far more often — draw-hits
**44.0% vs 29.8%** (discordant pairs 18–6, sign test p=0.023) — and is closer to the turn on
21 of the 31 dates where it differs (p=0.071, median gain 28.8 pts). It still captures
**413 fewer points** than plain nearest-first, because it aims farther and therefore fills
less often (60.8% vs 67.1%).

Accuracy against the draw label and captured points are different objectives. Where they
conflict, points decide. **Do not implement B8g.** Its implementation is kept in
`agent/study/target_offline.py` so this result stays reproducible.

The draw sits at **rank 0 (44.6%) or rank 1 (44.6%) — 89.3% combined**, mean rank 0.73, on
production's own menu. Cycle 4's all-levels universe reported mean rank 1.30; production's
filters have already done most of the work, and what remains is close to a D1/D2 coin flip.

---

## 5. Coverage — a third of sessions, and it is not a ranking problem

| | dates | production's behaviour |
|---|---|---|
| menu empty at 09:20 | **5 (6.0%)** | **NEUTRAL — no trade** |
| menu valid, `D1` never reached | **23 (27.4%)** | enters with a DOL that never fills |

The empty-menu case is handled deliberately: a directional thesis without a DOL fails
`SYN_DIRECTIONAL_MISSING_DOL`, and the **no-liquidity override** downgrades the bias to
NEUTRAL with a LOW confidence ceiling when the direction's menu is empty
(`validate_contracts.py`, mirrored in `run_agent.py`). All 5 are UP days; 4 sit within
1.21× avg_1h of the all-time high. Reviewed 2026-09-07 and **kept as-is**.

The 27% is a different failure — the target was too far and the move fizzled (median D1
distance 318 pts, median shortfall 109). No ranking rule reaches it. Two examples:
2026-06-23 (every menu row FAR, `D1` 5.57× away, move 368 pts short) and 2026-05-13
(`D1` BAND at 2.44×, missed by **19.00 points**).

`agent/trader/shadow_ledger.py` was built to price the NEUTRAL days empirically and **has
never been wired in** — no callers, no entries on disk.

---

## 6. No level family should be added to the menu

The menu's universe is six families: `asia(cur)_*`, `london(cur)_*`, `prevN_day_*`,
`prevN_week_*`. Structurally absent: `TDO`/`TWO` (side hardcoded `None`), `daily_mid`/
`weekly_mid` (not in `bundle.levels`), FVG edges (a separate object never passed to
`_dol_menu`), and price discovery (only via the gated projection).

Adding each family to the menu, order-independent:

| added | Δ ORACLE | Δ nearest-first |
|---|---|---|
| inside the proximity floor | +59 | **−670** |
| swept / depleted | +2,208 | **−1,856** |
| nested (P1-suppressed) | +5,381 | **−4,694** |
| `TDO` / `TWO` | +1,648 | **−1,185** |
| daily / weekly mid | +2,551 | **−3,251** |
| projection, unconditional | +1,898 | −120 |
| HTF FVG edges | +8,340 | **−7,606** |
| everything | +9,813 | **−9,018** |

**Every family raises the oracle and lowers the rule.** The full-facts oracle reaches 26,299
(82.5%) against the menu's 16,486 (51.7%), but a deterministic rule falls from 40.7% to
12.4% of ceiling. The filters are not a coverage bug — they are what makes nearest-first
work, and P1 suppression is the most load-bearing of them.

`TDO`, `TWO` and the mids supply the oracle's best pick on **zero of 84 dates**. The FVG
figure is a density artifact: ~39 zones × 2 edges per date is near a price continuum, so an
oracle finds an edge near almost any turn. Cycle 4 tested FVG edges and refuted them; this
is the same conclusion by another route.

**The projection's stretch gate was tested separately** — it suppresses a projection no BAND
pool covers on 21 of 84 dates. Un-gating scores +356 for nearest-first, but only **6 of the
21 were ever reached**, one date supplies 479.5 of the gain, and several of the rest are
sessions where production correctly stands NEUTRAL. Rejected: the gain is small, fragile,
and overstated in exactly the direction the no-stop proxy is known to overstate.

---

## 7. CANDIDATE — deterministic selection at the fill

**Not a rule yet.** Step 0 of `docs/entry-mechanism-change-protocol.md` applies.

Two changes, of very different size:

- **(a) deterministic nearest-first replaces L1's DOL choice.** Today the DOL is an LLM pick;
  menu membership is *telemetry only* in the validator and the schema enum admits any named
  level plus the projections, so L1 may pick off-menu — and does: it took the menu's `D1` on
  **15 of 28** recorded theses (54%). (a) therefore *narrows* the reachable universe, and
  what that costs is **unmeasured** — the oracle in §3 is a menu oracle.
- **(b) re-anchor the menu at the entry fill.** A timing change; the rule is byte-identical.
  Worth **+1,152 pts (+8.9%)**, T2 over T1. Needs an explicit rule for an empty re-anchored
  menu — keep the 09:20 DOL rather than voiding a live plan (measured: 1 day gained, 1 lost).

The +1,152 is (b) *given* (a). Production as it stands — L1 picking at 09:20 — was never
scored for capture, so neither figure is "versus production today".

Both need a `regression.py` A/B before implementation. Do not call L1 at the fill: recorded
call latency is 16.3–103.9 s, and `thesis_cache.py` records five identical calls yielding
three theses with two different DOLs.

---

## 8. Two corrections to earlier readings in this line of work

Both came from an 11-date hand-picked sample and did not survive the 84-date sweep:

- *"T2 is dominated by T1; re-anchoring cannot help."* **False.** T1 and T2 agree on 82% of
  dates and T2 is better on both error and capture.
- *"The projection is never offered."* **False.** It is offered on 13.1% of dates; the
  hand-picked days were large movers, which have high `stretch_mult` by construction.

Selecting dates by outcome, or by move size, biases every downstream number. The 84-date
population is the corpus's own primaries, with bucket membership used only as a label.

---

## 9. Running it

```
python -m scripts.report_target_offline --date 2026-08-24 --time 09:30:00 \
    --direction DOWN --ticker MNQ [--source 1s|1m] [--json out.json]

python -m scripts.report_target_offline --date 2026-08-24 --time 09:30:00 \
    --direction DOWN --select-only [--selector nearest|b8g]
```

`--select-only` is the standalone decision path: menu-only, instant as an argument, and it
reads no bar at or after that instant — asserted by
`test_select_target_is_identical_with_the_future_deleted`, which reruns it against a
truncated bar frame and requires identical output. The full report adds a clearly-marked
lookahead block. `build_menus` is called unmodified; there is no second copy of the rule.
MES raises — the DOL menu is MNQ-only.
