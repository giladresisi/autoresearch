# Session Hypothesis System
**Complexity:** 🔴 Complex
**Status:** Planned
**Date:** 2026-04-19

---

## Overview

Add a session-planning layer to the realtime SMT signalling workflow. At ~9:00am ET,
a `HypothesisManager` computes a directional bias using deterministic ICT/SMT rules
(no LLM), then calls the Claude API for a narrative hypothesis. During the session,
evidence is evaluated per 1m bar and logged. If 3 contradictions accumulate, the
hypothesis is revised via a second Claude call. At session end, a final Claude summary
call compares hypothesis to actual session behaviour.

The hypothesis is **informational only** — it does not gate signal generation. The one
structural addition to both realtime and backtest is a `matches_hypothesis` boolean on
every signal dict for downstream analysis.

---

## Constraints

- `strategy_smt.py` must NOT be modified under any circumstances.
- `hypothesis_smt.py` must not import from `ib_insync` (backtest imports it too).
- No new Python package dependencies — `anthropic>=0.50` is already in `pyproject.toml`.
- All file writes to `data/sessions/YYYY-MM-DD/hypothesis.json` must be atomic (write to `.tmp` then rename,
  or use `pathlib.Path.write_text` on a fully-constructed string) to prevent partial reads
  by the orchestrator or a parallel reader.
- The Claude API key env var is `ANTHROPIC_API_KEY` (standard SDK default). Document in
  a comment inside `hypothesis_smt.py` and in the env var checklist below.
- Maximum 3 Claude API calls per session: generation, optional revision, finalization.

---

## Environment Variable

`ANTHROPIC_API_KEY` — standard Anthropic SDK env var. Already read by the SDK
automatically. No `load_dotenv()` call needed if the shell exports it. Add to
`.env.example` as `ANTHROPIC_API_KEY=<set-a-secret-here>`.

---

## Files Created / Modified

| File | Action |
|---|---|
| `hypothesis_smt.py` | **New** — rule engine + HypothesisManager |
| `signal_smt.py` | **Modified** — 4 integration points (instantiate, generate, evaluate_bar, finalize, matches_hypothesis) |
| `backtest_smt.py` | **Modified** — compute_hypothesis_direction() call per session, matches_hypothesis in trade record + TSV |
| `data/sessions/YYYY-MM-DD/` | **New directory per session** — `hypothesis.json` lives here; future session artifacts follow the same layout |
| `.env.example` | **Modified** — add ANTHROPIC_API_KEY entry |
| `tests/test_hypothesis_smt.py` | **New** — unit + integration tests |

---

## Separation of Concerns

| Module | Hypothesis logic | LLM calls | Used in backtest |
|---|---|---|---|
| `strategy_smt.py` | None — unchanged | No | Yes |
| `hypothesis_smt.py` | All | Yes (realtime only) | `compute_hypothesis_direction()` only |
| `signal_smt.py` | `HypothesisManager` consumer | Via manager | No |
| `backtest_smt.py` | `compute_hypothesis_direction()` only | No | Yes |

---

## Architecture Summary

```
hypothesis_smt.py
├── compute_hypothesis_direction(mnq_1m_df, hist_mnq_df, date) -> str|None
│   ├── _compute_rule5(...)   → tdo, two, day_zone, week_zone
│   ├── _compute_rule1(...)   → pdh, pdl, pd_range_case, pd_range_bias
│   ├── _compute_rule2(...)   → trend_direction, trend_strength
│   ├── _compute_rule3(...)   → deceptions list
│   ├── _compute_rule4(...)   → session extremes, last_extreme_visited
│   ├── _compute_overnight()  → overnight_high, overnight_low
│   ├── _compute_fvgs(...)    → fvgs_toward_tdo list
│   └── weighted_vote(...)    → "long"|"short"|"neutral"|None
└── HypothesisManager
    ├── __init__(mnq_1m_df, hist_mnq_df, date)
    ├── generate()            → Claude Call 1 (blocks ~2s)
    ├── evaluate_bar(bar)     → no LLM; appends evidence_log
    ├── finalize(signal, exit_result) → Claude Call 3
    └── direction_bias        → property; None until generate() runs

signal_smt.py  (4 touch points, no logic changes)
backtest_smt.py (2 touch points: per-session direction call, trade record field)
```

---

## Wave Structure

```
Wave 1 (independent):
  Task A — hypothesis_smt.py: rule engine (compute_hypothesis_direction)
  Task B — hypothesis_smt.py: HypothesisManager class + Claude calls + file I/O

Wave 2 (depends on Wave 1):
  Task C — signal_smt.py integration
  Task D — backtest_smt.py integration

Wave 3 (depends on Wave 2):
  Task E — tests/test_hypothesis_smt.py
```

Tasks A and B are in the same file but sequenced within Wave 1. They may be written
by the same agent in order (A first, B second) since B calls functions defined in A.

---

## Wave 1 — `hypothesis_smt.py`

**WAVE: 1 | DEPENDS_ON: nothing | AGENT_ROLE: implementer**

### Task A — Rule Engine (`compute_hypothesis_direction` + helpers)

**File:** `hypothesis_smt.py`

**Action:**

Create `hypothesis_smt.py` with the following structure. Write functions in this order
so each helper is defined before it is called:

```
Module docstring (includes env var note: ANTHROPIC_API_KEY)
Imports: datetime, json, pathlib, typing, pandas, anthropic (lazy import inside HypothesisManager)
Constants: SESSION_DATA_DIR = Path("data/sessions"), CONTRADICTION_THRESHOLD = 3,
           EVIDENCE_MOVE_PTS = 15.0, EVIDENCE_TOUCH_PTS = 5.0,
           EXTREME_CONTRADICTION_PTS = 20.0, FVG_LOOKBACK_BARS = 20

Helper functions (all private, all pure — no side effects, no API calls):

_compute_rule5(mnq_1m_df, date) -> dict
  - tdo: reuse compute_tdo() from strategy_smt (import it)
    IMPORTANT: import only compute_tdo from strategy_smt, nothing else
  - two: open of the first 1m bar on Tuesday of the current ISO week (Monday=0)
    Filter mnq_1m_df to bars where bar.index.weekday == 1 (Tuesday) and
    bar.index.isocalendar().week == date.isocalendar()[1] and
    bar.index.isocalendar().year == date.isocalendar()[0]; take first bar's Open
  - price_at_900: open of the 09:00 ET bar for `date`
  - day_zone: "premium" if price_at_900 > tdo else "discount" (None if tdo is None)
  - week_zone: "premium" if price_at_900 > two else "discount" (None if two is None)
  - Returns dict with keys: tdo, two, price_at_900, day_zone, week_zone
  - Returns None if tdo is None or price_at_900 is None (insufficient data)

_compute_rule1(mnq_1m_df, date) -> dict
  Previous day high/low: filter to bars where index.date == previous trading day.
  Previous trading day = subtract 1 day repeatedly (up to 7) until weekday in 0-4.
  pdh = max(High), pdl = min(Low) of all bars that previous day (full 24h ET).
  pd_midpoint = (pdh + pdl) / 2.
  Overnight bars: index.date == date AND index.time() < time(9, 0).
  price_at_900: open of 09:00 bar for date (same as rule5 — accept as param or recompute).
  price_in_pd_range: pdl <= price_at_900 <= pdh.

  Case detection (applied to overnight bars):
    crossed_mid = any overnight bar's Low <= pd_midpoint <= High  (i.e., candle body/wick crosses midpoint)
    near_far_extreme = any overnight bar came within 15% of (pdh-pdl) from the FAR extreme:
      For a bar above midpoint: near_far_extreme if (pdh - bar.High) <= 0.15 * (pdh - pdl)
      For a bar below midpoint: near_far_extreme if (bar.Low - pdl) <= 0.15 * (pdh - pdl)
      (Check all overnight bars; True if any qualifies)
    price_now_above_mid = price_at_900 > pd_midpoint
    outside_pd_range = not price_in_pd_range (price_at_900 < pdl or > pdh)

  Case assignment:
    1.5: outside_pd_range → repeat analysis on today's intraday bars (00:00-08:59) as the reference range
         Re-derive pdh/pdl from today's overnight bars; recompute above logic with those as the range.
         Bias for 1.5: same case logic applied to the intraday range.
    1.3: NOT crossed_mid (overnight never crossed pd_midpoint) → bias toward opposite extreme:
         price_now_above_mid → bias "short" (targeting pdl); else → bias "long" (targeting pdh)
    1.2: crossed_mid AND near_far_extreme → bias back to first extreme:
         price_now_above_mid → bias "short" (was below, crossed up, near top, now expect reversal back down)
         else → bias "long"
    1.4: crossed_mid AND price_now_above_mid AND NOT near_far_extreme → continuation toward far extreme: bias "long"
    1.1: crossed_mid AND NOT price_now_above_mid AND NOT near_far_extreme → bias toward last extreme:
         "short" (price crossed mid but settled below, last visited extreme is low end)

  Returns dict: pdh, pdl, pd_midpoint, pd_range_case (str "1.1"-"1.5"), pd_range_bias ("long"|"short"), price_in_pd_range

_compute_rule2(hist_mnq_df, date) -> dict
  Daily OHLCV from 5m historical: resample hist_mnq_df to "1D" using ET timezone.
  Last 5 trading days before `date` (exclusive): filter hist_mnq_df.index.date < date,
  then take last 5 unique dates, sorted ascending.
  For each day: daily_high = max(High), daily_low = min(Low).
  Build sequence of (daily_high, daily_low) across 5 days.
  HH: day[n].high > day[n-1].high; HL: day[n].low > day[n-1].low
  LH: day[n].high < day[n-1].high; LL: day[n].low < day[n-1].low
  Count: bullish_days = HH OR HL days; bearish_days = LH OR LL days.
  trend_direction:
    3+ bullish_days and 0 bearish: "bullish" strong
    3+ bearish_days and 0 bullish: "bearish" strong
    2 bullish 0 bearish: "bullish" moderate; 2 bearish 0 bullish: "bearish" moderate
    1 in either or mixed: "neutral" weak
  Returns dict: trend_direction, trend_strength, days_analyzed (int)

_compute_rule3(mnq_1m_df, hist_mnq_df, date, two, trend_direction) -> list[dict]
  Deceptions: stop-hunt wicks against trend that touch TWO or prior Asia session H/L.
  Scan last 5 trading days (same dates as rule2).
  For each day:
    If trend_direction == "bullish": look for bearish wicks (bar.Low < prior_bar.Low)
    If trend_direction == "bearish": look for bullish wicks (bar.High > prior_bar.High)
    Asia session for that day: bars 20:00-00:00 ET prior day (or use hist 5m)
      asia_high = max(High) of Asia window; asia_low = min(Low)
    A wick qualifies if: (a) wick direction opposes trend AND
      (b) wick.Low (bearish) or wick.High (bullish) is within EVIDENCE_TOUCH_PTS of two
          OR within EVIDENCE_TOUCH_PTS of asia_high/asia_low
    confirms_trend = True (these sweeps refuel the primary move)
  Returns list of: {date (str), direction ("bullish"|"bearish"), touched_level ("two"|"asia_high"|"asia_low"), confirms_trend: True}
  Returns [] if insufficient data or trend_direction == "neutral".

_compute_rule4(mnq_1m_df, hist_mnq_df, date) -> dict
  Session extremes:
    asia_high / asia_low: max/min High/Low of bars 20:00-00:00 ET prior calendar day
      (Use mnq_1m_df filtered to prior_date with hour in [20, 21, 22, 23])
    london_high / london_low: 02:00-05:00 ET today (mnq_1m_df filtered)
    ny_premarket_high / ny_premarket_low: 07:00-08:59 ET today (mnq_1m_df filtered)
    prior_week_high / prior_week_low: from hist_mnq_df, prior Mon-Fri week H/L
      (ISO week of date - 1; aggregate all bars in that week)
    last_extreme_visited: scan overnight bars (00:00-08:59 today).
      Check approach within 10 pts to each of: asia_high, asia_low, london_high, london_low,
      ny_premarket_high, ny_premarket_low, prior_week_high, prior_week_low.
      last_extreme_visited = the level name most recently approached (latest timestamp).
    next_extreme_candidate: the most recent unvisited extreme on the OPPOSITE side of the
      last_extreme_visited (above vs below current price_at_900).
  Returns dict with all named fields; None values for fields with insufficient data.

_compute_overnight(mnq_1m_df, date) -> dict
  overnight_high = max(High) of bars where index.date == date and index.time() < time(9,0)
  overnight_low  = min(Low)  of same slice
  Returns dict: overnight_high, overnight_low

_compute_fvgs(mnq_1m_df, date, price_at_900, tdo) -> list[dict]
  Scan last FVG_LOOKBACK_BARS (20) 1m bars before 09:00 ET on `date`.
  Three-bar FVG pattern: for bars i-1, i, i+1:
    bullish FVG: bar[i+1].Low > bar[i-1].High → gap between bar[i-1].High and bar[i+1].Low
    bearish FVG: bar[i+1].High < bar[i-1].Low → gap between bar[i+1].High and bar[i-1].Low
  Only keep FVGs that lie BETWEEN price_at_900 and tdo (directionally):
    If tdo > price_at_900: keep bullish FVGs with low >= price_at_900 and high <= tdo
    If tdo < price_at_900: keep bearish FVGs with high <= price_at_900 and low >= tdo
  Returns list of: {low: float, high: float}

_weighted_vote(rule1_bias, week_zone, day_zone, trend_direction) -> str
  Weights: rule1=3, week_zone=2, day_zone=1, trend=1
  long_score = 0; short_score = 0
  rule1_bias "long": long_score += 3; "short": short_score += 3
  week_zone "discount": long_score += 2; "premium": short_score += 2
  day_zone "discount": long_score += 1; "premium": short_score += 1
  trend_direction "bullish": long_score += 1; "bearish": short_score += 1
  If long_score >= 2 and long_score > short_score: return "long"
  If short_score >= 2 and short_score > long_score: return "short"
  return "neutral"

Public function:

def compute_hypothesis_direction(mnq_1m_df, hist_mnq_df, date) -> str | None:
  """Deterministic rule engine. No side effects. Safe to call from backtest."""
  try:
    r5 = _compute_rule5(mnq_1m_df, date)
    if r5 is None:
      return None
    r1 = _compute_rule1(mnq_1m_df, date)
    r2 = _compute_rule2(hist_mnq_df, date)
    return _weighted_vote(
      r1["pd_range_bias"],
      r5["week_zone"],
      r5["day_zone"],
      r2["trend_direction"],
    )
  except Exception:
    return None  # insufficient data → caller treats as no hypothesis
```

**Verify:** `python -c "import hypothesis_smt; print('import ok')"` passes.
`python -c "from hypothesis_smt import compute_hypothesis_direction; print(compute_hypothesis_direction.__doc__)"` prints docstring.

**Done:** `hypothesis_smt.py` exists, imports cleanly, `compute_hypothesis_direction` is importable and returns `str|None` without raising.

---

### Task B — `HypothesisManager` class + Claude calls + file I/O

**File:** `hypothesis_smt.py` (append to Task A's file)

**Action:**

Append the `HypothesisManager` class and the `_build_session_context()` internal helper
to `hypothesis_smt.py` after the public `compute_hypothesis_direction` function.

```python
def _build_session_context(mnq_1m_df, hist_mnq_df, date) -> dict | None:
    """Assemble ALL rule outputs into the session_context dict for Claude."""
    r5 = _compute_rule5(mnq_1m_df, date)
    if r5 is None:
        return None
    r1 = _compute_rule1(mnq_1m_df, date)
    r2 = _compute_rule2(hist_mnq_df, date)
    r3 = _compute_rule3(mnq_1m_df, hist_mnq_df, date, r5.get("two"), r2["trend_direction"])
    r4 = _compute_rule4(mnq_1m_df, hist_mnq_df, date)
    overnight = _compute_overnight(mnq_1m_df, date)
    fvgs = _compute_fvgs(mnq_1m_df, date, r5["price_at_900"], r5["tdo"])
    return {**r5, **r1, **r2, "deceptions": r3, **r4, **overnight, "fvgs_toward_tdo": fvgs}
```

`HypothesisManager` class:

```python
class HypothesisManager:
    """Realtime hypothesis lifecycle manager. Not safe for backtest use (makes API calls)."""

    GENERATION_SYSTEM_PROMPT = """
You are an ICT/SMT market analyst. You receive a session_context dict containing
computed values for 5 rules: Rule 1 (Previous Day Range), Rule 2 (Multi-day Trend),
Rule 3 (Deceptions/Liquidity Sweeps), Rule 4 (Session Extremes), Rule 5 (Premium/Discount vs TDO/TWO).

Your task: produce a directional hypothesis for the NY session (9:30am ET open).

Rules:
- Rule 1 (pd_range_case + pd_range_bias): PRIMARY signal, weight 3. Mandatory.
- Rule 5 week_zone: weight 2. Discount = expect long; Premium = expect short.
- Rule 5 day_zone: weight 1. Yields to week_zone on conflict.
- Rule 2 trend: weight 1. Weakest; yields to Rules 1+5 on conflict.
- Rule 3 deceptions: informational. Trend-confirming sweep history.
- Rule 4 session extremes: supportive hint. Not decisive alone.

The overnight_high / overnight_low are the most likely 9:30am liquidity grab targets.
Always describe the expected sweep direction in expected_scenario before the real move.

Respond ONLY with valid JSON matching this schema exactly:
{
  "direction_bias": "long | short | neutral",
  "confidence": "low | medium | high",
  "narrative": "...",
  "primary_reason": "...",
  "supporting_factors": ["..."],
  "contradicting_factors": ["..."],
  "key_levels_to_watch": [<floats>],
  "expected_scenario": "..."
}
""".strip()

    REVISION_SYSTEM_PROMPT = """
You are reviewing a session hypothesis that has accumulated contradicting evidence.
Given the original hypothesis, evidence log, and current market context, revise the hypothesis.
Respond with valid JSON matching this schema exactly:
{
  "direction_bias": "long | short | neutral",
  "confidence": "low | medium | high",
  "narrative": "...",
  "primary_reason": "...",
  "supporting_factors": ["..."],
  "contradicting_factors": ["..."],
  "key_levels_to_watch": [<floats>],
  "expected_scenario": "...",
  "revision_reason": "...",
  "replaces_hypothesis": true
}
""".strip()

    SUMMARY_SYSTEM_PROMPT = """
You are summarizing a completed trading session hypothesis vs actual outcome.
Given the full hypothesis JSON (including evidence_log, any revisions, signals_fired),
produce a concise session summary.
Respond with valid JSON matching this schema exactly:
{
  "summary": "...",
  "hypothesis_accuracy": "correct | incorrect | partial | no_signal",
  "learnings": ["...", "..."]
}
""".strip()

    def __init__(self, mnq_1m_df: pd.DataFrame, hist_mnq_df: pd.DataFrame, date: datetime.date):
        self._mnq_1m_df   = mnq_1m_df
        self._hist_mnq_df = hist_mnq_df
        self._date        = date
        self._direction_bias: str | None = None
        self._session_data: dict | None  = None
        self._hypothesis_file: Path | None = None
        self._contradiction_count = 0
        self._revision_triggered  = False
        self._call_count          = 0  # max 3 per session
        self._key_levels: list[float] = []

    @property
    def direction_bias(self) -> str | None:
        return self._direction_bias

    def generate(self) -> None:
        """Run rule engine + Claude Call 1. Writes hypothesis file. Prints to terminal."""
        import anthropic  # lazy import — not needed in backtest path

        context = _build_session_context(self._mnq_1m_df, self._hist_mnq_df, self._date)
        if context is None:
            print("[HYPOTHESIS] Insufficient data — skipping hypothesis generation.", flush=True)
            return

        direction = _weighted_vote(
            context.get("pd_range_bias", "neutral"),
            context.get("week_zone", "neutral"),
            context.get("day_zone", "neutral"),
            context.get("trend_direction", "neutral"),
        )
        self._direction_bias = direction

        hypothesis_json = None
        if self._call_count < 3:
            try:
                client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
                msg = client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=1024,
                    system=self.GENERATION_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": json.dumps(context)}],
                )
                self._call_count += 1
                raw = msg.content[0].text.strip()
                hypothesis_json = json.loads(raw)
                self._direction_bias = hypothesis_json.get("direction_bias", direction)
                self._key_levels = hypothesis_json.get("key_levels_to_watch", [])
            except Exception as e:
                # API failure is non-fatal — deterministic direction still used
                print(f"[HYPOTHESIS] Claude call failed: {e}", flush=True)
                hypothesis_json = {
                    "direction_bias": direction,
                    "confidence": "low",
                    "narrative": f"Rule engine only (API error: {e})",
                    "primary_reason": context.get("pd_range_case", "unknown"),
                    "supporting_factors": [],
                    "contradicting_factors": [],
                    "key_levels_to_watch": [],
                    "expected_scenario": "",
                }

        now_et = pd.Timestamp.now(tz="America/New_York")
        self._session_data = {
            "date": str(self._date),
            "generated_at": now_et.isoformat(),
            "session_context": context,
            "hypothesis": hypothesis_json,
            "contradiction_count": 0,
            "evidence_log": [],
            "revisions": [],
            "signals_fired": [],
            "summary": None,
        }

        session_dir = SESSION_DATA_DIR / str(self._date)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._hypothesis_file = session_dir / "hypothesis.json"
        self._write_file()
        self._print_hypothesis(now_et, hypothesis_json)

    def evaluate_bar(self, bar) -> None:
        """Per 1m bar evidence check. No LLM. May trigger revision if threshold reached."""
        if self._session_data is None or self._direction_bias is None:
            return

        bar_ts  = pd.Timestamp(bar.date if hasattr(bar, "date") else bar.name)
        if bar_ts.tz is None:
            bar_ts = bar_ts.tz_localize("America/New_York")
        close = float(bar.close if hasattr(bar, "close") else bar["Close"])
        high  = float(bar.high  if hasattr(bar, "high")  else bar["High"])
        low   = float(bar.low   if hasattr(bar, "low")   else bar["Low"])

        tdo   = self._session_data["session_context"].get("tdo")
        bias  = self._direction_bias
        findings: list[dict] = []

        # Check 1: key level touch (within EVIDENCE_TOUCH_PTS)
        for level in self._key_levels:
            if abs(close - level) <= EVIDENCE_TOUCH_PTS or abs(low - level) <= EVIDENCE_TOUCH_PTS or abs(high - level) <= EVIDENCE_TOUCH_PTS:
                findings.append({
                    "time": bar_ts.isoformat(),
                    "event": f"Price touched key level {level:.2f}",
                    "classification": "neutral",
                    "level_touched": str(level),
                    "bar_close": close,
                })

        # Check 2: >15 pts move in hypothesis direction
        prev_close = self._session_data["evidence_log"][-1]["bar_close"] if self._session_data["evidence_log"] else None
        if prev_close is not None:
            move = (close - prev_close) if bias == "long" else (prev_close - close)
            if move > EVIDENCE_MOVE_PTS:
                findings.append({
                    "time": bar_ts.isoformat(),
                    "event": f"Price moved {move:.1f} pts in hypothesis direction",
                    "classification": "supports",
                    "level_touched": None,
                    "bar_close": close,
                })
            elif -move > EVIDENCE_MOVE_PTS:
                findings.append({
                    "time": bar_ts.isoformat(),
                    "event": f"Price moved {-move:.1f} pts against hypothesis",
                    "classification": "contradicts",
                    "level_touched": None,
                    "bar_close": close,
                })

        # Check 5: extreme contradiction — close >20 pts beyond TDO in wrong direction
        if tdo is not None:
            if bias == "long" and close < tdo - EXTREME_CONTRADICTION_PTS:
                findings.append({
                    "time": bar_ts.isoformat(),
                    "event": f"EXTREME: closed {close:.2f}, {tdo - close:.1f} pts below TDO ({tdo:.2f})",
                    "classification": "contradicts",
                    "level_touched": "tdo",
                    "bar_close": close,
                    "extreme": True,
                })
                self._contradiction_count = CONTRADICTION_THRESHOLD  # instant trigger
            elif bias == "short" and close > tdo + EXTREME_CONTRADICTION_PTS:
                findings.append({
                    "time": bar_ts.isoformat(),
                    "event": f"EXTREME: closed {close:.2f}, {close - tdo:.1f} pts above TDO ({tdo:.2f})",
                    "classification": "contradicts",
                    "level_touched": "tdo",
                    "bar_close": close,
                    "extreme": True,
                })
                self._contradiction_count = CONTRADICTION_THRESHOLD

        for f in findings:
            if f["classification"] == "contradicts" and not f.get("extreme"):
                self._contradiction_count += 1
            self._session_data["evidence_log"].append(f)
            self._session_data["contradiction_count"] = self._contradiction_count
            self._print_evidence(bar_ts, f)

        self._write_file()

        # Trigger revision if threshold reached and not already revised
        if self._contradiction_count >= CONTRADICTION_THRESHOLD and not self._revision_triggered:
            self._revision_triggered = True
            self._revise(bar_ts)

    def _revise(self, bar_ts: pd.Timestamp) -> None:
        """Claude Call 2 — revision on contradiction threshold."""
        import anthropic
        if self._call_count >= 3:
            return
        try:
            client = anthropic.Anthropic()
            payload = {
                "original_hypothesis": self._session_data["hypothesis"],
                "evidence_log": self._session_data["evidence_log"],
                "session_context": self._session_data["session_context"],
            }
            msg = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                system=self.REVISION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            self._call_count += 1
            raw = msg.content[0].text.strip()
            revision = json.loads(raw)
            self._direction_bias = revision.get("direction_bias", self._direction_bias)
            self._key_levels = revision.get("key_levels_to_watch", self._key_levels)
            self._session_data["revisions"].append(revision)
            self._write_file()
            self._print_revision(bar_ts, revision)
        except Exception as e:
            print(f"[HYPOTHESIS] Revision call failed: {e}", flush=True)

    def finalize(self, signal: dict | None, exit_result: dict | None) -> None:
        """Claude Call 3 — session summary. Called at session end."""
        import anthropic
        if self._session_data is None:
            return

        if signal is not None:
            fired_entry = {
                "direction": signal.get("direction"),
                "entry_time": str(signal.get("entry_time", "")),
                "entry_price": signal.get("entry_price"),
                "matches_hypothesis": signal.get("matches_hypothesis"),
                "exit_type": exit_result.get("exit_type") if exit_result else None,
                "pnl": exit_result.get("pnl") if exit_result else None,
            }
            self._session_data["signals_fired"].append(fired_entry)
            self._write_file()

        if self._call_count >= 3:
            print("[HYPOTHESIS] Max API calls reached — skipping summary.", flush=True)
            return

        now_et = pd.Timestamp.now(tz="America/New_York")
        try:
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                system=self.SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(self._session_data)}],
            )
            self._call_count += 1
            raw = msg.content[0].text.strip()
            summary_json = json.loads(raw)
            self._session_data["summary"] = summary_json
            self._write_file()
            print(f"[{now_et.strftime('%H:%M:%S')}] SUMMARY     {summary_json.get('summary', '')}", flush=True)
        except Exception as e:
            print(f"[HYPOTHESIS] Summary call failed: {e}", flush=True)

    def _write_file(self) -> None:
        """Atomic file write — write to .tmp then rename."""
        if self._hypothesis_file is None or self._session_data is None:
            return
        tmp = self._hypothesis_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._session_data, indent=2, default=str))
        tmp.replace(self._hypothesis_file)

    def _print_hypothesis(self, ts: pd.Timestamp, h: dict) -> None:
        bias       = h.get("direction_bias", "?")
        confidence = h.get("confidence", "?")
        narrative  = h.get("narrative", "")
        expected   = h.get("expected_scenario", "")
        levels     = h.get("key_levels_to_watch", [])
        contradict = h.get("contradicting_factors", [])
        print(f"[{ts.strftime('%H:%M:%S')}] HYPOTHESIS  {bias} ({confidence} confidence)", flush=True)
        if narrative:
            for line in narrative.split("\n"):
                print(f"           {line}", flush=True)
        if contradict:
            print(f"           Contradicts: {'; '.join(contradict)}", flush=True)
        if expected:
            print(f"           Expected: {expected}", flush=True)
        if levels:
            print(f"           Levels: {' → '.join(str(l) for l in levels)}", flush=True)

    def _print_evidence(self, ts: pd.Timestamp, finding: dict) -> None:
        cls = finding["classification"]
        count_str = f"({self._contradiction_count})" if cls == "contradicts" else ""
        print(
            f"[{ts.strftime('%H:%M:%S')}] EVIDENCE    {cls}{count_str:<15} | {finding['event']}",
            flush=True,
        )

    def _print_revision(self, ts: pd.Timestamp, revision: dict) -> None:
        bias   = revision.get("direction_bias", "?")
        conf   = revision.get("confidence", "?")
        reason = revision.get("revision_reason", "")
        print(f"[{ts.strftime('%H:%M:%S')}] HYPOTHESIS REVISED → {bias} ({conf} confidence)", flush=True)
        if reason:
            print(f"           {reason}", flush=True)
```

**Verify:**
```
python -c "
from hypothesis_smt import HypothesisManager, compute_hypothesis_direction
print('HypothesisManager:', HypothesisManager)
print('direction fn:', compute_hypothesis_direction)
"
```

**Done:** Both `compute_hypothesis_direction` and `HypothesisManager` are importable.
`HypothesisManager` has `generate`, `evaluate_bar`, `finalize`, `direction_bias`.

---

## Wave 2A — `signal_smt.py` Integration

**WAVE: 2 | DEPENDS_ON: Wave 1 (hypothesis_smt.py complete) | AGENT_ROLE: integrator**

### Task C — Wire `HypothesisManager` into `signal_smt.py`

**File:** `signal_smt.py`

**Action:**

Make exactly 4 targeted modifications. Do NOT restructure or rename any existing logic.

**Modification 1 — Import (top of file, after existing strategy_smt import):**
```python
from hypothesis_smt import HypothesisManager
```

**Modification 2 — Module-level state variable (after `_last_exit_ts` declaration ~line 68):**
```python
_hypothesis_manager: "HypothesisManager | None" = None
_hypothesis_generated: bool = False
```

**Modification 3 — `main()` function, after `_mnq_1m_df, _mes_1m_df = _gap_fill_1m(...)` (~line 563):**
```python
# Load historical 5m data for hypothesis rule engine
_hist_mnq_df = _load_hist_mnq()   # define this helper (see below)
today = pd.Timestamp.now(tz="America/New_York").date()
_hypothesis_manager = HypothesisManager(_mnq_1m_df, _hist_mnq_df, today)
_hypothesis_generated = False
```

Add `_load_hist_mnq()` helper near `_load_parquets()`:
```python
def _load_hist_mnq() -> pd.DataFrame:
    """Load the Databento 5m historical parquet for hypothesis rule engine."""
    import pandas as pd
    hist_path = Path("data/historical/MNQ.parquet")
    if hist_path.exists():
        return pd.read_parquet(hist_path)
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
```

**Modification 4 — `on_mnq_1m_bar()`, after the existing parquet write + set_bar_data call:**
```python
# Hypothesis: generate on first 1m bar at/after 09:00 ET; evaluate every bar
global _hypothesis_manager, _hypothesis_generated
if _hypothesis_manager is not None:
    bar_time = _bar_timestamp(bars[-1]).time()
    _session_start_time_local = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    if not _hypothesis_generated and bar_time >= _session_start_time_local:
        _hypothesis_manager.generate()
        _hypothesis_generated = True
    _hypothesis_manager.evaluate_bar(bars[-1])
```

**Modification 5 — `_process_scanning()`, after `signal = screen_session(...)` is confirmed non-None (before step 7 stale guard, ~line 435):**
```python
# Annotate signal with hypothesis alignment
if _hypothesis_manager is not None and _hypothesis_manager.direction_bias is not None:
    signal["matches_hypothesis"] = (
        signal.get("direction") == _hypothesis_manager.direction_bias
    )
else:
    signal["matches_hypothesis"] = None
```

**Modification 6 — Session end in `_process_managing()`, after the `print(_format_exit_line(...))` call:**
```python
# Finalize hypothesis at session end
if _hypothesis_manager is not None and result != "hold":
    exit_result_dict = {"exit_type": result, "pnl": pnl}
    _hypothesis_manager.finalize(_position, exit_result_dict)
```

Note: `_position` at this point in the code still holds the closed position dict (before
`_position = None`). Place the `finalize` call before `_position = None`.

**Verify:**
```
python -c "import signal_smt; print('import ok')"
```
(No IB connection needed; import should succeed without error.)

**Done:** `signal_smt.py` imports cleanly. `HypothesisManager` is instantiated in `main()`.
`on_mnq_1m_bar` calls `evaluate_bar`. `_process_scanning` sets `matches_hypothesis`. Session end calls `finalize`.

---

## Wave 2B — `backtest_smt.py` Integration

**WAVE: 2 | DEPENDS_ON: Wave 1 | AGENT_ROLE: integrator**

### Task D — Add `compute_hypothesis_direction` + `matches_hypothesis` to backtest

**File:** `backtest_smt.py`

**Action:**

Make minimal targeted changes. Do NOT modify `run_backtest()`'s signal generation logic,
position sizing, or exit logic. Do NOT change the frozen `_build_trade_record` function signature.

**Modification 1 — Import (top of file, after existing strategy_smt imports):**
```python
from hypothesis_smt import compute_hypothesis_direction
```

**Modification 2 — Load historical MNQ in `run_backtest()`, just before the `for day in trading_days:` loop (~line 217):**

First, verify that `run_backtest()` receives `mnq_df` and `mes_df`. Read `run_backtest`'s
signature to confirm parameter names before editing.

Add:
```python
# Load 5m historical for hypothesis direction (deterministic, no API calls)
_hist_mnq_path = Path("data/historical/MNQ.parquet")
_hist_mnq_df = pd.read_parquet(_hist_mnq_path) if _hist_mnq_path.exists() else pd.DataFrame(
    columns=["Open", "High", "Low", "Close", "Volume"]
)
```

**Modification 3 — Inside `for day in trading_days:` loop, after `day_tdo = compute_tdo(...)` and before the bar loop:**
```python
# Hypothesis direction for this session (deterministic, no LLM)
_session_hyp_dir = compute_hypothesis_direction(mnq_df, _hist_mnq_df, day)
```

**Modification 4 — After `signal = _build_signal_from_bar(...)` is confirmed non-None (in both WAITING_FOR_ENTRY and REENTRY_ELIGIBLE states), add:**
```python
signal["matches_hypothesis"] = (
    (signal.get("direction") == _session_hyp_dir)
    if _session_hyp_dir is not None else None
)
```

There are TWO places where `_build_signal_from_bar` is called and `signal is not None` is checked — one in the `WAITING_FOR_ENTRY` block and one in the `REENTRY_ELIGIBLE` block. Add the line in both.

**Modification 5 — `_build_trade_record()` call sites: add `matches_hypothesis` to the trade dict.**

`_build_trade_record` is a frozen function — do NOT modify it. Instead, after the
`trade, day_pnl_delta = _build_trade_record(...)` call, add:
```python
trade["matches_hypothesis"] = position.get("matches_hypothesis")
```
(position dict holds signal fields at entry; signal was spread into position via `{**signal, ...}`)

**Modification 6 — TSV output: locate where the TSV header row and data rows are written.**

Read the TSV output section of `backtest_smt.py` to find exact column list and writing logic.
Add `"matches_hypothesis"` to the column list in the same position as other boolean quality fields.
The value will be `True`, `False`, or `None` (string-formatted as-is via `str()`).

**Verify:**
```
python -c "import backtest_smt; print('import ok')"
```

**Done:** `backtest_smt.py` imports cleanly. `compute_hypothesis_direction` called once per
session day. `matches_hypothesis` present in trade dicts and TSV output. `strategy_smt.py`
untouched.

---

## Wave 3 — Tests

**WAVE: 3 | DEPENDS_ON: Waves 1 + 2 | AGENT_ROLE: tester**

### Task E — `tests/test_hypothesis_smt.py`

**File:** `tests/test_hypothesis_smt.py`

**Test framework:** pytest (existing; see `pyproject.toml` and `tests/conftest.py`).
Use `unittest.mock` for Claude client mocking (same pattern as `test_signal_smt.py`).
Use `pd.DataFrame` with synthetic data — no real parquet files needed.

**Action:**

Create `tests/test_hypothesis_smt.py` with the following test cases:

#### Fixture helpers (module-level, not pytest fixtures)

```python
def _make_1m_df(date, bars_per_day=100, base=21800.0) -> pd.DataFrame:
    """Build a synthetic 30-bar 1m DataFrame for a single date.
    Includes overnight bars (00:00-08:59) and session bars (09:00-13:30).
    """

def _make_hist_5m_df(end_date, n_days=10, base=21800.0) -> pd.DataFrame:
    """Build a synthetic 5m historical DataFrame spanning n_days before end_date."""
```

#### Test cases to implement:

**compute_hypothesis_direction — rule 1 cases:**

```python
def test_compute_direction_case_1_3_long():
    """Case 1.3: overnight never crosses pd_midpoint; price below mid → bias long."""
    # Build mnq_1m_df where previous day has pdh=21900, pdl=21700 (midpoint=21800)
    # Overnight bars all stay above midpoint (no crossing), price_at_900 = 21820 (above mid)
    # Expected: pd_range_bias = "long" (opposite extreme = pdl, but we're above mid... wait)
    # Re-read spec: 1.3 NOT crossed_mid → bias toward OPPOSITE extreme
    # price_now_above_mid=True → opposite is pdl (below) → bias "short"
    # So: price above mid + no crossing → short bias
    # Test with price below mid → long bias

def test_compute_direction_case_1_3_short():
    """Case 1.3: price above pd_midpoint → bias short (toward opposite low extreme)."""

def test_compute_direction_case_1_2():
    """Case 1.2: crossed_mid AND near_far_extreme → bias back to first extreme."""

def test_compute_direction_case_1_4():
    """Case 1.4: crossed_mid AND price_now_above_mid AND NOT near_far_extreme → long."""

def test_compute_direction_case_1_1():
    """Case 1.1: crossed_mid AND NOT price_now_above_mid AND NOT near_far_extreme → short."""

def test_compute_direction_case_1_5():
    """Case 1.5: price_at_900 outside [pdl, pdh] → re-analyze on intraday range."""
```

**compute_hypothesis_direction — weighted vote:**

```python
def test_weighted_vote_long_wins():
    """Rule1=long(3) + discount_week(2) + discount_day(1) → long_score=6 → long."""

def test_weighted_vote_neutral():
    """Conflicting signals below threshold → neutral."""

def test_weighted_vote_rule1_overrides_trend():
    """Rule1 long (weight 3) overrides bearish trend (weight 1)."""
```

**compute_hypothesis_direction — edge cases:**

```python
def test_compute_direction_no_overnight_bars():
    """Missing overnight bars → returns None (insufficient data)."""

def test_compute_direction_no_prior_day():
    """mnq_1m_df has no data for previous day → returns None."""

def test_compute_direction_returns_string_or_none():
    """Return type is always str or None; never raises."""
```

**evaluate_bar — evidence classification:**

```python
def test_evaluate_bar_supports_long():
    """Bar closes 16 pts above previous close on long hypothesis → supports."""

def test_evaluate_bar_contradicts_long():
    """Bar closes 16 pts below previous close on long hypothesis → contradicts."""

def test_evaluate_bar_neutral_level_touch():
    """Bar touches key_level within 5 pts → neutral evidence entry."""

def test_evaluate_bar_extreme_contradiction_sets_threshold():
    """Price closes >20 pts below TDO on long → contradiction_count = THRESHOLD immediately."""

def test_evaluate_bar_revision_triggered_on_threshold():
    """3 contradicts entries → _revision_triggered = True and _revise() called."""
    # Mock the _revise method to avoid API call
```

**HypothesisManager.generate() — mocked Claude:**

```python
def test_generate_writes_hypothesis_file(tmp_path, monkeypatch):
    """generate() writes hypothesis.json inside data/sessions/YYYY-MM-DD/."""
    monkeypatch.setattr("hypothesis_smt.SESSION_DATA_DIR", tmp_path)
    # Mock anthropic.Anthropic().messages.create to return a valid hypothesis JSON
    with mock.patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value.content = [
            mock.MagicMock(text=json.dumps({
                "direction_bias": "long",
                "confidence": "medium",
                "narrative": "Test narrative.",
                "primary_reason": "Rule 1.3",
                "supporting_factors": [],
                "contradicting_factors": [],
                "key_levels_to_watch": [21800.0],
                "expected_scenario": "Test scenario.",
            }))
        ]
        manager = HypothesisManager(_make_1m_df(date), _make_hist_5m_df(date), date)
        manager.generate()
    file = tmp_path / str(date) / "hypothesis.json"
    assert file.exists()
    data = json.loads(file.read_text())
    assert data["hypothesis"]["direction_bias"] == "long"

def test_generate_api_failure_falls_back_to_rule_engine(tmp_path, monkeypatch):
    """If Claude API raises, generate() still writes file using rule engine direction."""
    monkeypatch.setattr("hypothesis_smt.SESSION_DATA_DIR", tmp_path)
    with mock.patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.messages.create.side_effect = Exception("API error")
        manager = HypothesisManager(_make_1m_df(date), _make_hist_5m_df(date), date)
        manager.generate()
    file = tmp_path / str(date) / "hypothesis.json"
    assert file.exists()
    # direction_bias will be the rule-engine fallback — not None
    data = json.loads(file.read_text())
    assert data["hypothesis"]["direction_bias"] in ("long", "short", "neutral")
```

**matches_hypothesis in backtest signal flow:**

```python
def test_backtest_signal_matches_hypothesis_true(tmp_path, monkeypatch):
    """Signal direction == hypothesis direction → matches_hypothesis = True."""
    # Build minimal run_backtest() scenario using existing pattern from test_smt_backtest.py
    # Monkeypatch compute_hypothesis_direction to return "short"
    # Run backtest with bars that produce a short signal
    # Assert trade["matches_hypothesis"] == True

def test_backtest_signal_matches_hypothesis_false(tmp_path, monkeypatch):
    """Signal direction != hypothesis direction → matches_hypothesis = False."""

def test_backtest_matches_hypothesis_when_no_data(tmp_path, monkeypatch):
    """compute_hypothesis_direction returns None → matches_hypothesis = False."""
```

**Verify:**
```
uv run pytest tests/test_hypothesis_smt.py -v
```
All tests pass. No IB connection required. No real API calls made.

**Done:** `tests/test_hypothesis_smt.py` exists, all test cases pass, coverage includes
rule1 cases 1.1-1.5, weighted vote, evaluate_bar classification, generate() with mocked Claude,
and backtest matches_hypothesis flow.

---

## `.env.example` Update

**WAVE: 1 | DEPENDS_ON: nothing | AGENT_ROLE: integrator**

Add the following line to `.env.example` (read the file first to place it after existing
API key entries):
```
ANTHROPIC_API_KEY=<set-a-secret-here>
```

---

## File Format — `data/sessions/YYYY-MM-DD/hypothesis.json`

Written atomically on each evidence entry and at generation/finalization. Accumulates throughout the session. `summary` is filled at session end by Call 3.

```json
{
  "date": "2026-04-19",
  "generated_at": "2026-04-19T09:02:15-04:00",
  "session_context": { "...all rule engine outputs..." },
  "hypothesis": {
    "direction_bias": "long",
    "confidence": "medium",
    "narrative": "...",
    "primary_reason": "...",
    "supporting_factors": ["..."],
    "contradicting_factors": ["..."],
    "key_levels_to_watch": [21710, 21865, 21890, 22050],
    "expected_scenario": "..."
  },
  "contradiction_count": 0,
  "evidence_log": [
    {
      "time": "2026-04-19T09:15:00-04:00",
      "event": "Price closed 21898 — crossed TDO (21890)",
      "classification": "supports",
      "level_touched": "tdo",
      "bar_close": 21898.0
    }
  ],
  "revisions": [],
  "signals_fired": [
    {
      "direction": "long",
      "entry_time": "2026-04-19T09:35:00-04:00",
      "entry_price": 21850.75,
      "matches_hypothesis": true,
      "exit_type": "exit_tp",
      "pnl": 78.50
    }
  ],
  "summary": null
}
```

---

## Terminal Output Format

```
[09:02:15] HYPOTHESIS  long (medium confidence)
           Price in discount vs TDO (21890) and TWO (21950). PDH/PDL pattern 1.3:
           below midpoint (21865), targeting PDH (22050). London swept low at 21740.
           Contradicts: 5-day trend bearish (weaker signal, overridden by Rule 1 + Rule 5).
           Expected: 9:30 liquidity grab to overnight low (21710) -> reversal long.
           Levels: 21710 -> 21865 -> 21890 (TDO) -> 22050 (PDH)

[09:15:00] EVIDENCE    supports        | Price closed 21898, crossed TDO (21890)
[09:30:05] EVIDENCE    neutral         | 9:30 bar spiked to overnight low 21707 (expected sweep)

[10:45:00] EVIDENCE    contradicts(1)  | Price failed TDO hold, closed 21875
[10:50:00] EVIDENCE    contradicts(2)  | Reversed 18pts against hypothesis direction

[10:55:00] HYPOTHESIS REVISED -> short (low confidence)
           3 contradictions: failed TDO hold x2, sustained move against hypothesis.

[13:30:00] SUMMARY     Hypothesis: long -> revised to short. Signal fired long (matches
           original). Exit TP. P&L +$78.50. Hypothesis partially correct.
```

---

## Pre-execution Baseline

Before starting Wave 1, run:
```
uv run pytest --tb=no -q 2>&1 | tail -5
```
Document the baseline passing count. Confirm zero regressions after each wave completes.

---

## Post-implementation Smoke Test (Manual — no live IB)

After all waves complete:

1. Confirm `data/historical/MNQ.parquet` exists:
   ```
   python -c "import pandas as pd; df = pd.read_parquet('data/historical/MNQ.parquet'); print(df.shape)"
   ```

2. Run rule engine with real data for a recent date:
   ```python
   python -c "
   import pandas as pd
   from hypothesis_smt import compute_hypothesis_direction
   mnq_1m = pd.read_parquet('data/realtime/MNQ_1m.parquet')
   hist   = pd.read_parquet('data/historical/MNQ.parquet')
   import datetime
   d = datetime.date(2026, 4, 17)  # adjust to a date with realtime data
   result = compute_hypothesis_direction(mnq_1m, hist, d)
   print('Direction:', result)
   "
   ```
   Should print `"long"`, `"short"`, `"neutral"`, or `None` without raising.

3. Verify backtest imports cleanly:
   ```
   python -c "import backtest_smt; print('ok')"
   ```

4. Verify signal_smt imports cleanly:
   ```
   python -c "import signal_smt; print('ok')"
   ```

5. Run full test suite — confirm zero regressions vs baseline:
   ```
   uv run pytest --tb=short -q
   ```

---

## Execution Order Summary

| Wave | Task | File | Depends On |
|---|---|---|---|
| 1 | A — rule engine functions | `hypothesis_smt.py` | — |
| 1 | B — HypothesisManager class | `hypothesis_smt.py` | Task A |
| 1 | env.example update | `.env.example` | — |
| 2 | C — signal_smt integration | `signal_smt.py` | Wave 1 |
| 2 | D — backtest_smt integration | `backtest_smt.py` | Wave 1 |
| 3 | E — tests | `tests/test_hypothesis_smt.py` | Waves 1+2 |

Tasks C and D (Wave 2) can run in parallel since they touch different files and both
depend only on `hypothesis_smt.py` being complete.

---

## Validation Checklist

- [ ] `hypothesis_smt.py` imports cleanly in both realtime and backtest contexts
- [ ] `compute_hypothesis_direction` never raises; returns `str|None`
- [ ] `HypothesisManager.generate()` writes atomic JSON file before returning
- [ ] `HypothesisManager.evaluate_bar()` never calls the API
- [ ] Max 3 Claude API calls per session enforced by `_call_count` guard
- [ ] `strategy_smt.py` is unmodified (`git diff strategy_smt.py` shows no changes)
- [ ] `matches_hypothesis` present in realtime `position.json`
- [ ] `matches_hypothesis` column in backtest TSV output
- [ ] `data/sessions/YYYY-MM-DD/` subdirectory created automatically on each session start if missing
- [ ] All `test_hypothesis_smt.py` tests pass with zero regressions to existing suite
- [ ] `.env.example` has `ANTHROPIC_API_KEY=<set-a-secret-here>`

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `compute_hypothesis_direction()` returns `"long"`, `"short"`, `"neutral"`, or `None` for any valid session date; never raises an exception
- [ ] All 5 Rule 1 cases (1.1–1.5) are detected correctly from bar data, producing the correct directional bias
- [ ] The weighted vote returns `"long"` or `"short"` only when a direction reaches score ≥ 2; otherwise returns `"neutral"`
- [ ] `HypothesisManager.generate()` writes `data/sessions/YYYY-MM-DD/hypothesis.json` atomically before returning, even when the Claude API call fails (falls back to rule-engine direction)
- [ ] `HypothesisManager.evaluate_bar()` classifies evidence as `supports`, `contradicts`, or `neutral` and appends it to the evidence log in the JSON file on every call
- [ ] When `contradiction_count` reaches 3, a revision Claude call fires exactly once and updates `direction_bias` and `key_levels_to_watch`
- [ ] An extreme contradiction (close >20 pts beyond TDO in wrong direction) sets `contradiction_count` to threshold immediately, triggering revision
- [ ] `HypothesisManager.finalize()` writes a `summary` block to the hypothesis JSON and prints a `SUMMARY` line to terminal
- [ ] Maximum 3 Claude API calls fire per session regardless of how many contradictions accumulate (`_call_count` guard enforced)
- [ ] `signal["matches_hypothesis"]` is `True` when signal direction equals `direction_bias`, `False` when it differs, and `None` when no hypothesis was generated
- [ ] `matches_hypothesis` appears as a column in the backtest TSV output

### Error Handling
- [ ] If `ANTHROPIC_API_KEY` is missing or invalid, `generate()` prints a clear error message and continues with the rule-engine direction (`confidence: "low"`) rather than crashing
- [ ] If `data/historical/MNQ.parquet` is absent, `compute_hypothesis_direction()` returns `None` rather than raising
- [ ] If the 09:00 ET bar is missing for the session date, rule engine returns `None` gracefully
- [ ] A partial or corrupted `.tmp` file from a previous crash does not prevent the next atomic write from succeeding

### Integration / E2E
- [ ] `signal_smt.py` imports cleanly without an IB connection (`python -c "import signal_smt"` succeeds)
- [ ] `backtest_smt.py` imports cleanly and runs a fold without errors after the integration (`python -c "import backtest_smt"` succeeds)
- [ ] `strategy_smt.py` is byte-for-byte unmodified after all changes (`git diff strategy_smt.py` shows nothing)
- [ ] In realtime mode, SCANNING state does not begin until `manager.generate()` returns (hypothesis file exists before first signal scan)
- [ ] `on_mnq_1m_bar` calls `manager.evaluate_bar()` on every 1m bar, including bars during MANAGING state
- [ ] The hypothesis JSON file is updated on each 1m bar, not only at generation and finalization

### Validation
- [ ] All 17 unit tests in `tests/test_hypothesis_smt.py` pass — verified by `uv run pytest tests/test_hypothesis_smt.py -v`
- [ ] Zero regressions in existing test suite — verified by `uv run pytest --tb=short -q`
- [ ] Rule engine produces a direction (not `None`) when run against real `MNQ_1m.parquet` and `MNQ.parquet` for a date within the 30-day rolling window — verified by the smoke test command in the plan
- [ ] `data/sessions/YYYY-MM-DD/` subdirectory is created automatically if absent (no manual setup required)

### Out of Scope
- Hypothesis direction gating signal generation — not required; hypothesis is informational only
- `HYPOTHESIS_FILTER` flag in backtest — deferred
- MES bars used in hypothesis computation — MNQ only in this version
- Aggregated session-level `matches_hypothesis` statistics — deferred to post-analysis
- Automatic order placement — existing out-of-scope from `signal_smt.md`
