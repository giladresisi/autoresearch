# Session Indexing — Design Thoughts

**Created:** 2026-05-10  
**Context:** Came out of strategy update simulation (strategy-update-method.md). Phase 1 auto-selection required manually reading events.jsonl files for 6+ sessions before finding a suitable candidate. That process should be a database query, not a read loop.

---

## The Problem

The current backtest produces two outputs per session: `events.jsonl` and `trades.tsv`. These are correct and complete, but they're not queryable. To answer "find me a session in the last 30 non-ATH days with a net loss, no wholesale direction miss (hypothesis called wrong direction all session — a mechanism failure in hypothesis.py/trend.py, distinct from an execution failure in strategy.py), and a post-cautious re-entry cluster" requires reading every file and running logic in the querier's head.

The index supports diagnosing both failure types. **Execution failures** (bad entry timing, stop too tight, re-entry after cautious reversal) are isolated by filtering for sessions where the direction was right but trades lost. **Mechanism failures** (wrong hypothesis direction, missed sweep signals) are isolated by comparing `dominant_direction` against `session_direction`. Strategy update work should cover both: execution logic lives in `strategy.py`; mechanism logic is spread across `hypothesis.py`, `trend.py`, and `daily.py`.

As the session corpus grows (we're at ~500+ sessions), this gets worse. And the auto-selection logic in strategy-update-method.md Phase 1 is exactly the kind of structured query that a session index would serve instantly.

---

## Three Levels of Indexing

### Level 1 — Session

The highest level. One record per trading date. Should be computable entirely from `events.jsonl` and the 1m bar data. Every field here is a single scalar or a short list.

**Proposed fields:**

| Field | Type | Source | Notes |
|---|---|---|---|
| `date` | str | — | YYYY-MM-DD |
| `n_trades` | int | trades.tsv | Total positions taken |
| `n_stops` | int | trades.tsv | exit_reason == stopped-out |
| `n_market_close` | int | trades.tsv | exit_reason == market-close (cautious) |
| `n_eos` | int | trades.tsv | exit_reason == end-of-session |
| `pnl_dollars` | float | trades.tsv | Sum of pnl_dollars |
| `win_rate` | float | trades.tsv | Fraction of trades with pnl > 0 |
| `max_consecutive_stops` | int | trades.tsv | Longest stop streak |
| `direction_flips` | int | events.jsonl | Count of direction changes across hypothesis updates |
| `dominant_direction` | str \| None | events.jsonl | "up" / "down" / "mixed" (based on direction of most trades or most hypothesis time) |
| `ath_crossed` | bool | events.jsonl | Any event referencing above-ath-reversal or levels crossing ATH |
| `ath_cross_time` | str \| None | events.jsonl | First crossing timestamp if applicable |
| `n_smt_divs` | int | events.jsonl | Total SMT divergence events |
| `n_hypothesis_updates` | int | events.jsonl | new-hypothesis events |
| `had_cautious_reversal` | bool | events.jsonl | Any market-close with reason=cautious-reversal |
| `had_post_cautious_reentry` | bool | derived | cautious-reversal followed by another entry within N min |
| `n_stop_entries_filled` | int | events.jsonl | stop-entry-filled events (tells us how often pending orders execute vs market-entries) |
| `n_stop_entries_cancelled` | int | events.jsonl | stop-entry-cancelled events |
| `rule` | str | events.jsonl | Primary direction rule from first hypothesis (rule2b, etc.) |
| `session_range_pts` | float | bars | session high - session low (NY session window) |
| `session_direction` | str | bars | "up" / "down" / "flat" based on open-to-close price change |
| `bar_atr` | float | bars | Average true range of 1m bars during NY session |

**What this enables:**
- Filter `ath_crossed == False` instantly
- Filter `pnl_dollars < 0 AND n_trades <= 6 AND max_consecutive_stops <= 3 AND direction_flips <= 4` to find contained-loss, non-chaotic sessions
- Find sessions with `had_post_cautious_reentry == True` to identify the specific pattern we just looked at in April 8
- Compare `dominant_direction` vs `session_direction` (bar data) to detect mechanism failures — sessions where hypothesis was wrong from the start; these call for changes in `hypothesis.py`/`trend.py`, not `strategy.py`

The April 8 search that took 45 minutes of manual reading would have been a 3-field filter.

---

### Level 2 — Trade

One record per trade per session. Extends the trades.tsv with derived context fields. The core trades data already exists; this adds fields computed from events and bars.

**Proposed fields (on top of existing trades.tsv columns):**

| Field | Type | Source | Notes |
|---|---|---|---|
| `trade_n` | int | sequence | 1st, 2nd, ... trade of the session |
| `entry_type` | str | events | "market-entry" or "stop-entry-filled" |
| `stop_pts` | float | derived | abs(entry_price - stop_level) |
| `hold_minutes` | float | derived | (exit_time - entry_time).seconds / 60 |
| `hypothesis_at_entry` | int | derived | Index of active hypothesis when entry fired |
| `direction_flips_before_entry` | int | derived | How many direction changes before this trade |
| `mins_since_last_exit` | float | derived | Gap from previous trade's exit to this entry |
| `last_exit_reason` | str \| None | derived | Exit reason of immediately preceding trade |
| `was_after_cautious_reversal` | bool | derived | True if previous exit was cautious-reversal |
| `mins_after_cautious_reversal` | float \| None | derived | Time gap from cautious-reversal to this entry |
| `post_stop_recovery_pts` | float | bars | How far price moved in trade direction after the stop (within 30min) |
| `post_stop_recovery_min` | float | bars | Minutes until price recovered past stop level (or None if didn't) |
| `pre_entry_bar_atr_5` | float | bars | Average true range of last 5 bars before entry (local volatility) |
| `entry_in_range` | bool | derived | Whether entry_price was inside the entry_ranges from hypothesis |
| `matches_hypothesis` | bool \| None | existing | Already computed by hypothesis system |

**What this enables:**
- Find trades where `was_after_cautious_reversal == True AND mins_after_cautious_reversal < 20 AND pnl_dollars < 0` — the exact April 8 pattern
- Find trades where `exit_reason == stopped-out AND post_stop_recovery_pts > 30` — "stopped then the expected move happened" (suggests stop too tight)
- Find trades with `hold_minutes < 3 AND pnl_dollars < 0` — very quick stops, high whipsaw rate
- Sort by `stop_pts` to identify whether tight stops correlate with losses in a given session
- `was_after_cautious_reversal` and `mins_after_cautious_reversal` are the fields that would have identified April 8's 13:00/13:10/13:20 cluster in 1 query

The `post_stop_recovery_pts` field is particularly useful for the "stop too tight" hypothesis category. If price consistently recovers 20+ points in the trade direction after a stop, that's evidence for a wider stop parameter.

---

### Level 3 — Movement

The session is modeled as a sequence of **movement periods** — each a distinct behavioral segment defined by its direction (up/down/sideways) and type (clean/choppy). A new period begins wherever the bars make it clear that either the trend direction or the movement type has changed. Boundaries reflect actual price behavior; they don't need to coincide with liquidity levels.

**What triggers a period boundary:**
- Trend direction changes: up → down, down → sideways, up → sideways, etc.
- Movement type changes: clean → choppy or choppy → clean (direction may stay the same)

**Clean vs. choppy — definition:**  
A movement is **clean** if its `displacement_ratio ≥ 0.65`:
```
displacement_ratio = abs(end_price - start_price) / sum(high - low across all bars in period)
```
A ratio below 0.65 is **choppy**. A clean move arrives close to where it's heading; a choppy move spends most of its energy oscillating. The 0.65 threshold is a starting point and can be calibrated against a labeled set of sessions.

**Endpoint anchoring — no None values:**  
Each period endpoint (start and end) is always expressed relative to the known level structure. Levels considered: ATH, week_high, week_low, tdo_high, tdo_low, two_high, two_low, ny_open, day_high (running), day_low (running).

Representation:
- If between two levels: `"12.5 pts above week_low and 8.0 pts below tdo_low"`
- If beyond all levels in one direction: `"5.0 pts above ATH"`
- If exactly at a level: `"at week_low"`

**Movement period fields:**

| Field | Type | Notes |
|---|---|---|
| `movement_id` | str | `{date}_M{n}` — sequential within session |
| `start_time` | str | HH:MM of first bar in period |
| `end_time` | str | HH:MM of last bar in period |
| `direction` | str | "up" / "down" / "sideways" |
| `type` | str | "clean" / "choppy" |
| `start_price` | float | Price at start of period |
| `end_price` | float | Price at end of period |
| `pts_net` | float | end_price − start_price (signed) |
| `displacement_ratio` | float | abs(pts_net) / sum(bar high-low) |
| `start_anchor` | str | Level context for start_price |
| `end_anchor` | str | Level context for end_price |
| `trade_ids` | list[str] | IDs of trades that occurred during this period |

**Storage:** Per-session YAML at `sessions/{DATE}/movements.yaml` (human-readable, naturally sharded). A centralized `data/movements_index.parquet` holds the same records for bulk querying alongside the session and trade tables. The bulk Parquet also carries a `movement_sequence` string per session (e.g., `"clean_up,choppy,clean_down"`) as a queryable summary.

---

### Trade-Movement Links

Trades and movements have a many-to-many relationship: a trade can span multiple movement periods, and a movement period usually contains multiple trades. A per-session YAML file (`sessions/{DATE}/trade_movement_links.yaml`) stores one record per trade-movement pair with auto-generated narrative notes.

**Example:**
```yaml
- trade_id: "2026-04-08_T2"
  movement_id: "2026-04-08_M3"
  entry_pct_into_movement: 15
  exit_pct_into_movement: 32
  direction_aligned: true
  note: "entered 15% into a clean upward move off week_low, stopped out after 2 min; price continued 45pts further in the expected direction"

- trade_id: "2026-04-08_T3"
  movement_id: "2026-04-08_M3"
  entry_pct_into_movement: 54
  exit_pct_into_movement: 71
  direction_aligned: true
  note: "entered 54% into the same clean upward move, held 8 min, exited cautious reversal near day_high"
```

**Key fields:**
- `entry_pct_into_movement` / `exit_pct_into_movement`: where in the movement the trade began and ended (0% = movement start price, 100% = movement end price)
- `direction_aligned`: whether the trade direction matched the movement direction
- `note`: auto-generated by the indexer from `direction`, `type`, `entry_pct`, `hold_minutes`, `exit_reason`, `post_stop_recovery_pts`

**Bi-directional references:** `trades_index.parquet` carries a `movement_ids` column (list of movement IDs active during the trade). Each movement record in `movements.yaml` carries a `trade_ids` list. The link YAML is the join table — the place with the computed narrative annotation.

---

## Live vs Backtest Feasibility

| Level | Backtest | Live |
|---|---|---|
| Session | ✓ Fully retroactive; run after session closes | ✓ Computable at end of NY session from events written during the day |
| Trade | ✓ Retroactive; `post_stop_recovery_*` fields require knowing future bars | ⚠ Most fields available at trade close; `post_stop_recovery_*` requires waiting 30 min post-exit |
| Movement periods | ✓ Retroactive; final boundaries and types determined when session is complete | ⚠ Boundaries may shift as session progresses (a "clean up" period may be reclassified to "choppy" as bars accumulate) |
| Trade-movement links | ✓ Retroactive; computable after movements and trades are indexed | ✗ Not meaningful until both movements and trades are finalized |

The `post_stop_recovery_pts` and `post_stop_recovery_min` fields in Level 2 are the only ones that strictly require retroactive computation (you need bars after the stop). Everything else can be computed live with a small delay (30 min max). For the primary use case (strategy update auto-selection), retroactive is sufficient.

---

## Storage Approach

**Option 1: Per-session JSON alongside existing files**  
`sessions/{DATE}/index.json` and `data/regression/{DATE}/index.json`. Each contains session-level and trade-level records. Queryable only by reading all files and filtering in Python.

Pros: no new infrastructure, naturally sharded by date, easy to regenerate for individual sessions.  
Cons: querying still requires O(N) file reads. No improvement over current state for bulk queries.

**Option 2: Centralized Parquet/SQLite**  
`data/sessions_index.parquet` (session-level, one row per date) and `data/trades_index.parquet` (trade-level, one row per trade, with date as join key). Optionally a SQLite file (`data/index.db`) with both as tables.

Pros: instant querying with pandas or SQL; all session/trade data in two files; sorted and indexed on date. This is the format that makes the auto-selection a 3-line query.  
Cons: must regenerate or incrementally update when new sessions are added; not human-readable without a tool.

**Option 3: Both**  
Generate both. Write per-session JSON for per-date inspection and debugging; roll up to Parquet for bulk queries.

**My recommendation: Option 2 (centralized Parquet)**  
The per-session JSON files add overhead without much benefit — the events.jsonl already serves as the ground truth for individual session inspection. The centralized Parquet is the high-value artifact. If you want per-session summary for human inspection, that's what the chart is for.

---

## The Indexer

Conceptually, the indexer is a function:

```python
def index_session(date: str, bars: pd.DataFrame, events: list[dict], trades: list[dict]) -> dict:
    """Return {session: {...}, trades: [{...}, ...]}."""
```

And a runner that applies it to all dates:

```python
def build_index(dates: list[str] | None = None) -> None:
    """Build or incrementally update sessions_index.parquet and trades_index.parquet."""
```

The indexer should be:
- **Pure function** (no side effects, no imports from strategy files)
- **Fast** — no bar-by-bar loops where vectorized pandas ops work
- **Additive** — building the index for a new date doesn't require re-reading old dates

Integration points:
- Run automatically after each regression run
- Run manually over the full historical corpus as a one-time backfill
- Optionally: run as part of the live session teardown script after NY close

---

## Relationship to Strategy Update Auto-Selection

The Phase 1 loop in strategy-update-method.md currently asks: "find a session in the last N non-ATH days with a net loss and an isolated mechanism failure."

With the index, this becomes:

```python
idx = pd.read_parquet("data/sessions_index.parquet")
candidates = idx[
    (idx.ath_crossed == False) &
    (idx.pnl_dollars < 0) &
    (idx.direction_flips <= 4) &   # not chaotic
    (idx.dominant_direction != "mixed")  # not wholesale wrong-direction
].sort_values("date", ascending=False).head(30)
```

And for finding the specific trade:

```python
trades = pd.read_parquet("data/trades_index.parquet")
hot = trades[
    (trades.date.isin(candidates.date)) &
    (trades.was_after_cautious_reversal == True) &
    (trades.mins_after_cautious_reversal < 20) &
    (trades.pnl_dollars < 0)
]
```

The output of these two queries IS Phase 1 auto-selection. What took 45 minutes of manual reading would take 5 seconds and 10 lines of code.

For mechanism failure investigation (hypothesis/trend logic calling the wrong direction), the query is different:

```python
mechanism_misses = idx[
    (idx.ath_crossed == False) &
    (idx.dominant_direction != idx.session_direction) &   # hypothesis vs actual
    (idx.n_trades >= 2)                                   # strategy was active, not just inactive
].sort_values("date", ascending=False).head(20)
```

These sessions surface candidates where the fix belongs in `hypothesis.py`, `trend.py`, or `daily.py`, not in `strategy.py`.

---

## Open Questions

**1. ATH detection**  
Currently detected by scanning events for "above-ath-reversal" exit reasons. But what if price crossed ATH during a session but no trade was open? The events log only what the strategy observed. We'd need to compare session high (bars) vs the ATH level (levels.json) directly to detect crossings independent of the strategy state.

**2. Dominant direction vs session direction**  
`dominant_direction` (from hypothesis events) can disagree with `session_direction` (from bars open-to-close). This disagreement IS useful information (directional miss) but needs a clean field name and definition. Consider: `direction_alignment: bool` = whether dominant_direction matches session_direction.

**3. Movement boundary detection sensitivity**  
The `displacement_ratio` threshold (0.65) and the minimum period length needed to declare a type change are empirical choices. Too sensitive and noise bars split clean moves into fragments; too coarse and distinct behavioral phases get merged. The threshold should be validated against a small set of manually labeled sessions (5–10) before the indexer is run over the full corpus.

**4. The missing stop-entry-filled trades**  
While building the index I expect to find cases (like April 8's 11:32–12:58 UP trade) where a stop-entry-filled position is tracked in events but doesn't produce a trades.tsv row. The indexer will encounter these as "orphaned" fills: a stop-entry-filled event with no corresponding exit in trades.tsv. These should be flagged in the session-level index as `has_unrecorded_trades: bool` so they're visible without reading raw events. This is also a cue to investigate whether the backtest accounting is correct.

**5. Incremental updates**  
How to update the centralized Parquet when new sessions are added without rebuilding from scratch. Simple approach: read existing Parquet, check which dates are missing, run indexer for those dates only, append and re-save. Works as long as historical sessions don't change (they don't after baselines are locked).

**6. Movement indexing for live sessions**  
During a live session, the swing structure is incomplete until the session ends. If we want to use movement-level data in real-time signal logic (not just for retrospective analysis), the indexer needs an incremental mode. This is a significant additional complexity — better to leave it backtest-only initially and revisit if there's a concrete use case.
