# Liquidity Levels & Sweep Semantics — Concepts & Facts

> Concept doc: definitions + facts only; procedures live in `decisions/`. Doc changes =
> strategy changes — backtest before merge (rule in smt.md header).

Levels are the coordinate system of every decision: direction (which pool was swept), entries
(confirmation at levels), targets (which pool is drawn on next).

## 1. The level map

Built at ~09:20 ET (`daily.py` → `daily.json`) + running intraday extremes. Prices computed on
**MNQ**; MES mirrored for SMT pairing.

- **Fixed daily** (`liquidities`): `TDO` (18:00 ET session open), `TWO` (Monday 18:00 ET open,
  with fallbacks), `prev1/2_day_high/low` (18:00→17:00 windows), unvisited 1hr+4hr FVGs
  (triple-bar gaps; zones, not prices).
- **Additive SMT-only universes** (no trade-side effects): `liquidities_universe` (prev 1–14
  day + 1–2 week extremes, wick `price` + body `close_price`; SMT recency gate admits
  prev1/2_day + prev1_week) and `liquidities_session_prior` (prior session's 6hr sub-session
  extremes).
- **Running:** `day_high/low`, `week_high/low` — the trailing edge of price; anchoring
  decisions on them is treacherous (GIL-16).
- The strategy's own `liquidities` feeds the failed-entry sweep decrement — know which list
  you're reading in state JSONs.

## 2. Sweep semantics: grab vs continuation

A move through a level is a **liquidity grab** (sweep-and-reverse) or **continuation**
(genuine repricing). Evidence table:

| Grab (reversal) | Continuation |
|---|---|
| sweep against weekly zone (high in premium / low in discount) | sweep WITH a committed prior equilibrium cross |
| SMT divergence at the level | both tickers take it cleanly |
| price fails back through the daily mid after | holds beyond; departure > DEPART_PTS |
| PM (≥13:00) sweep of highs in premium | AM sweep of sub-weekly highs during expansion |
| true structural extreme (week_high, prev-week) | the ATH in an ATH-expansion week (price discovery) |

Encoded rule2b guards (`hypothesis.py`): **ATH expansion** — week_high ≈ ATH before 12:00 →
high sweep = discovery, not distribution · **Morning ambiguity** — before 13:00, day_high/
ny_morning_high sweeps in premium are range expansion (only week_high is always valid BSL) ·
**Recovery mode** — >2% (AM) / >3% (PM) below session-open ATH → high sweeps = recovery
continuation · **Overshoot** — "low grab → up" suppressed if price already >50 pts above the
swept low · **Stale anchor** — sweep older than ~1h with >200 pts recovery no longer anchors.

## 3. Level state machine

fresh/unvisited (candidate DOL) → **swept** (first touch = the information event) →
**depleted** (ran DEPLETE_PTS beyond → retired, spent structure) → re-arm (dynamic levels
only). Fixed levels are single-fire per session.

## 4. Observed pool/target behavior (facts; procedure in decisions/next-move.md §5)

- Tier magnetism: week pools > day > session; FVGs are magnets that fill but weak terminal
  targets.
- Pools in a tight zone act as one stronger magnet.
- Depleted levels neither draw nor defend.
- **Partial delivery:** price frequently reacts/reverses AT a pool once swept — a pool is the
  destination of the move, not evidence of continuation through it.
- Cross-weekly-mid draws complete mostly on trend days, not balance days.
