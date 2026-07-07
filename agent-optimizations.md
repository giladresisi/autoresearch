# Agent Optimizations: Decision System Architecture

This document describes the optimized architecture for running the trading decision system. The key principle: **use deterministic code for everything that doesn't require reasoning; use AI/LLM only for the two core decision points (daily-trend and next-move).**

---

## 1. Core Principle

**Deterministic code handles:**
- Facts extraction (market data → structured facts.json)
- Validation (check output schemas)
- Entry gate logic (arm_entry_confirmation + confidence threshold)
- Orchestration & state management
- Audit logging

**AI/LLM handles:**
- Daily-trend decision (evaluate HTF structure, equilibrium, overnight complex, ATH regime, SMT residue, prior context)
- Next-move decision (build bull/bear ledgers, apply vetoes, determine target and flip trigger)

**Why:** Facts extraction is purely mechanical (query data, apply formulas). Entry gate is a simple threshold check. But deciding whether price will trend up or down requires reasoning about many interacting factors and past patterns — that's what the LLM is for. Don't make the LLM do arithmetic; make it do judgment.

---

## 2. System Architecture

```
Market Data Stream
        ↓
Facts Extractor (deterministic code)
        ├─ Compute levels (TDO, TWO, prev-day, prev-week, running extremes, FVGs)
        ├─ Detect sweeps (wick crosses, body crosses, depletion latch)
        ├─ Detect divergences (cross-ticker SMT pairs)
        ├─ Compute equilibrium (daily/weekly mids, acceptance counts, mid crosses)
        ├─ Get structure bars (4hr, 1hr, 15m, 5m)
        ├─ Read ATH, position state, entry counters
        └─ Output: facts.json
        ↓
Decision Orchestrator (deterministic state machine)
        ├─ Check if now is a checkpoint (06:00, 09:20, 13:00, 18:00 ET)
        │
        ├─ If checkpoint:
        │   └─ LLM Call: evaluate_daily_trend(facts.json) → daily_trend.json
        │       (standing decision persists until next checkpoint)
        │
        ├─ Always:
        │   └─ LLM Call: evaluate_next_move(facts.json, daily_trend.json) → next_move.json
        │
        └─ Post-decision:
            ├─ Validation (check schemas, sanity checks)
            ├─ Entry Gate (deterministic code)
            │   └─ If arm_entry_confirmation == TRUE AND confidence >= THRESHOLD:
            │       → emit NEW_ORDER to broker
            │   └─ Else:
            │       → emit NO_NEW_ORDERS (scripts handle baseline)
            │
            └─ Audit Log (deterministic write)
                └─ Store facts, decisions, reasoning for later review
```

---

## 3. Deterministic Components

### 3.1 Facts Extractor

**Purpose:** Ingest market data (OHLC bars, tick data) and produce structured facts.json.

**Inputs:** Market data stream (real-time or historical)  
**Output:** facts.json schema (see 3.1.1)

**Implementation pattern:**

```python
class FactsExtractor:
    """Computes all facts needed for decisions; no reasoning required."""
    
    def __init__(self, market_db, config):
        self.db = market_db
        self.config = config  # Level definitions, sweep thresholds, etc.
    
    def extract_facts(self, now: datetime) -> dict:
        """Main entry point: given current time, return facts.json."""
        return {
            "timestamp": now.isoformat(),
            "session_info": self._get_session_info(now),
            "levels": self._compute_levels(now),
            "sweeps": self._detect_sweeps(now),
            "divergences": self._detect_divergences(now),
            "equilibrium": self._compute_equilibrium(now),
            "structure": self._get_structure_bars(now),
            "ath": self._get_ath(),
            "position": self._get_position_state(),
            "entry_counters": self._get_entry_counters(),
        }
    
    def _compute_levels(self, now: datetime) -> dict:
        """
        Compute all level definitions:
        - TDO (session open 18:00 ET)
        - TWO (Monday 18:00 ET)
        - prev1/2_day high/low (fixed levels from prior sessions)
        - prev1_week high/low
        - running day/week extremes (updated as new bars arrive)
        - FVGs (unvisited 1hr and 4hr gaps)
        """
        # Query OHLC data for these ranges
        # Return structured dict with all levels and their wick/body prices
        pass
    
    def _detect_sweeps(self, now: datetime) -> dict:
        """
        For each level, determine:
        - Has it been swept? (wick crossed?)
        - When was it swept? (timestamp)
        - How far beyond? (max excursion, for depletion latch)
        - Is it depleted? (max excursion >= DEPLETION_THRESHOLD)
        - Age (time since sweep, for freshness)
        
        Return one entry per level with all these fields.
        """
        pass
    
    def _detect_divergences(self, now: datetime) -> list:
        """
        For each level that could host a divergence (SMT):
        - MNQ swept / not swept
        - MES swept / not swept
        - Type (wick divergence, body divergence, laggard test-and-fail)
        - Lead ticker (which one is ahead)
        
        Return list of divergence objects.
        """
        pass
    
    def _compute_equilibrium(self, now: datetime) -> dict:
        """
        Compute:
        - Daily running mid = (day_high + day_low) / 2
        - Weekly running mid (engine anchor: Sunday 18:00 ET, extended early-week)
        - Acceptance counts: % of 1m closes ABOVE mid in recent windows
          (session→checkpoint, checkpoint→now, full session)
        - Failed mid reclaims: track close-crosses and whether they held
        
        Return structured dict with all acceptance rates and crossing history.
        """
        pass
    
    def _get_structure_bars(self, now: datetime) -> dict:
        """
        Return OHLC bars at multiple timeframes:
        - 4hr bars (midnight-anchored, engine convention): last 24
        - 1hr bars: last 24
        - 15m bars: last 16
        - 5m bars: from checkpoint to now
        
        Return as lists of (open, high, low, close) tuples with timestamps.
        """
        pass
```

**facts.json schema:**

```json
{
  "timestamp": "2026-06-23T12:59:59-04:00",
  "session_info": {
    "trade_date": "2026-06-23",
    "session_phase": "ny_evening",
    "inside_whipsaw_window": false,
    "checkpoint_applied": "2026-06-23T09:20:00-04:00"
  },
  "levels": {
    "mnq": {
      "tdo": {"wick": 30662.0, "body": 30662.0},
      "two": {"wick": 30662.0, "body": 30662.0},
      "prev1_day_high": {"wick": 30965.5, "body": 30965.5},
      "prev1_day_low": {"wick": 30337.0, "body": 30338.0},
      "prev1_week_high": {"wick": 30974.0, "body": 30974.0},
      "prev1_week_low": {"wick": 29923.0, "body": 29923.25},
      "day_high": {"wick": 30699.75, "body": 30699.75},
      "day_low": {"wick": 29616.5, "body": 29616.5},
      "week_high": {"wick": 30965.5, "body": 30965.5},
      "week_low": {"wick": 29616.5, "body": 29616.5}
    },
    "mes": {}
  },
  "sweeps": {
    "mnq": [
      {
        "level_name": "ny_morning(cur)_high",
        "level_price": 30046.75,
        "direction": "above",
        "swept": false,
        "closest_approach_pts": 131.25,
        "closest_approach_time": "2026-06-23T12:15:06-04:00",
        "age_minutes": 45,
        "depleted": false
      }
    ],
    "mes": []
  },
  "divergences": [
    {
      "level_name": "asia(prev1)_high",
      "wick_mnq_swept": true,
      "wick_mes_swept": false,
      "body_mnq_swept": true,
      "body_mes_swept": false,
      "type": "WICK_DIVERGENCE_CANDIDATE",
      "lead": "MNQ",
      "age_minutes": 1140
    }
  ],
  "equilibrium": {
    "mnq": {
      "daily_mid": 30158.125,
      "weekly_mid": 30291.0,
      "acceptance": {
        "session_to_ckpt": {"above_pct": 0.04, "below_pct": 0.96, "n_closes": 921},
        "ckpt_to_now": {"above_pct": 0.0, "below_pct": 1.0, "n_closes": 220}
      },
      "mid_crosses_recent": [
        {"time": "2026-06-22T20:44:00-04:00", "close": 30606.0, "side": "above"},
        {"time": "2026-06-22T21:16:00-04:00", "close": 30575.0, "side": "below"}
      ]
    },
    "mes": {}
  },
  "structure": {
    "bars_4hr": [
      {
        "open_time": "2026-06-22T16:00:00-04:00",
        "open": 30654.5,
        "high": 30699.75,
        "low": 30538.75,
        "close": 30575.5
      }
    ],
    "bars_1hr": [],
    "bars_15m": [],
    "bars_5m": []
  },
  "ath": {
    "mnq": 31100.25,
    "mes": 7695.0
  },
  "position": "flat",
  "entry_counters": {
    "failed_entries": 0,
    "cautious_dist_shrinks": 0
  }
}
```

**Notes:**
- All timestamps in ET
- "age_minutes" is computed at fact-extraction time
- Level definitions (which levels are "day tier" vs "session tier" vs "week tier") should be in config, not computed per-call
- Sweep depletion thresholds vary by tier; store in config

---

### 3.2 Validation (deterministic)

```python
class OutputValidator:
    """Post-LLM checks; ensure decision schemas are valid."""
    
    def validate_daily_trend(self, result: dict) -> tuple[bool, list]:
        """
        Check:
        1. direction ∈ {UP, DOWN, NEUTRAL}
        2. confidence ∈ {HIGH, MEDIUM, LOW}
        3. regime ∈ {TREND, RANGE, HYBRID}
        4. If direction != NEUTRAL: day_dol is not None
        5. All driver contributions sum to score (within 0.01 tolerance)
        
        Return (is_valid, error_messages)
        """
        errors = []
        
        if result.get("direction") not in ["UP", "DOWN", "NEUTRAL"]:
            errors.append(f"direction={result.get('direction')} not in allowed set")
        
        if result.get("confidence") not in ["HIGH", "MEDIUM", "LOW"]:
            errors.append(f"confidence={result.get('confidence')} not in allowed set")
        
        if result.get("direction") != "NEUTRAL" and result.get("day_dol") is None:
            errors.append("day_dol must be set when direction is not NEUTRAL")
        
        # Check driver sum
        if "drivers" in result:
            sum_contributions = sum(d.get("contribution", 0) for d in result["drivers"])
            if abs(sum_contributions - result.get("score", 0)) > 0.01:
                errors.append(f"driver contributions {sum_contributions} != score {result.get('score')}")
        
        return (len(errors) == 0, errors)
    
    def validate_next_move(self, result: dict) -> tuple[bool, list]:
        """
        Check:
        1. direction ∈ {UP, DOWN, NEUTRAL}
        2. confidence ∈ {HIGH, MEDIUM, LOW}
        3. If direction != NEUTRAL: move_target is set
        4. ledgers.bull and ledgers.bear are lists
        5. Ledger items have required fields (tier_weight, session_side, etc.)
        6. All veto conditions are boolean or None
        
        Return (is_valid, error_messages)
        """
        errors = []
        
        if result.get("direction") not in ["UP", "DOWN", "NEUTRAL"]:
            errors.append(f"direction={result.get('direction')} invalid")
        
        if result.get("confidence") not in ["HIGH", "MEDIUM", "LOW"]:
            errors.append(f"confidence={result.get('confidence')} invalid")
        
        if result.get("direction") != "NEUTRAL" and result.get("move_target") is None:
            errors.append("move_target required when direction is not NEUTRAL")
        
        # Check ledger structure
        for ledger_name in ["bull", "bear"]:
            ledger = result.get("ledgers", {}).get(ledger_name, [])
            if not isinstance(ledger, list):
                errors.append(f"ledgers.{ledger_name} must be a list")
            else:
                for i, item in enumerate(ledger):
                    required_fields = ["tier_weight", "session_side", "alignment", "freshness", "score"]
                    for field in required_fields:
                        if field not in item:
                            errors.append(f"ledgers.{ledger_name}[{i}] missing field '{field}'")
        
        return (len(errors) == 0, errors)
```

---

### 3.3 Entry Gate (deterministic)

```python
class EntryGate:
    """Decides whether to open new positions based on next_move output."""
    
    CONFIDENCE_THRESHOLD = "MEDIUM"  # Configurable
    
    def __init__(self, order_service, config):
        self.order_service = order_service
        self.confidence_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        self.threshold_rank = self.confidence_rank.get(config.get("confidence_threshold", "MEDIUM"))
    
    def execute(self, next_move: dict, facts: dict) -> str:
        """
        Decide whether to place a new order.
        
        Return: "NEW_ORDER" | "HOLD" (no new orders, let existing positions run)
        """
        # Check arm_entry_confirmation flag
        if not next_move.get("arm_entry_confirmation", False):
            return "HOLD"
        
        # Check confidence threshold
        confidence_rank = self.confidence_rank.get(next_move.get("confidence"), -1)
        if confidence_rank < self.threshold_rank:
            return "HOLD"
        
        # If we get here: place order
        direction = next_move["direction"]
        target = next_move.get("move_target", {}).get("price")
        flip_trigger = next_move.get("flip_trigger")
        
        self.order_service.place_entry_order(
            direction=direction,
            target=target,
            flip_trigger=flip_trigger,
            facts_snapshot=facts,
        )
        
        return "NEW_ORDER"
```

---

### 3.4 Decision Orchestrator (deterministic state machine)

```python
class DecisionOrchestrator:
    """Routes facts → daily-trend → next-move; manages state and checkpoints."""
    
    CHECKPOINTS_ET = ["06:00", "09:20", "13:00", "18:00"]
    
    def __init__(self, facts_extractor, llm_client, entry_gate, audit_log):
        self.facts = facts_extractor
        self.llm = llm_client
        self.entry_gate = entry_gate
        self.audit = audit_log
        
        self.standing_daily_trend = None
        self.last_checkpoint_time = None
    
    def on_market_data(self, now: datetime):
        """
        Called on each bar close (or tick, depending on frequency).
        Routes to LLM if checkpoint detected; always evaluates next-move.
        """
        # Extract facts
        facts = self.facts.extract_facts(now)
        
        # Check if this is a checkpoint time
        if self._is_checkpoint(now):
            # Evaluate daily-trend
            self.standing_daily_trend = self.llm.evaluate_daily_trend(
                facts, now
            )
            self.last_checkpoint_time = now
            
            # Validate
            is_valid, errors = validator.validate_daily_trend(self.standing_daily_trend)
            if not is_valid:
                print(f"WARNING: daily_trend validation failed: {errors}")
                # Fallback: use previous standing_daily_trend, or neutral
                # (depends on your recovery strategy)
        
        # Always evaluate next-move (fresh tape belongs here)
        next_move = self.llm.evaluate_next_move(
            facts, self.standing_daily_trend, now
        )
        
        # Validate
        is_valid, errors = validator.validate_next_move(next_move)
        if not is_valid:
            print(f"WARNING: next_move validation failed: {errors}")
            next_move = {"direction": "NEUTRAL", "confidence": "LOW"}  # Fallback
        
        # Execute entry gate
        gate_result = self.entry_gate.execute(next_move, facts)
        
        # Log
        self.audit.log_decision(
            timestamp=now,
            decision_type="next_move",
            facts=facts,
            daily_trend=self.standing_daily_trend,
            next_move=next_move,
            gate_result=gate_result,
        )
    
    def _is_checkpoint(self, now: datetime) -> bool:
        """Is now at one of the checkpoint times? (avoid double-evaluation same checkpoint)"""
        now_et = now.astimezone(pytz.timezone("US/Eastern"))
        checkpoint_str = now_et.strftime("%H:%M")
        
        if checkpoint_str not in self.CHECKPOINTS_ET:
            return False
        
        # Avoid evaluating same checkpoint twice
        if self.last_checkpoint_time and \
           self.last_checkpoint_time.date() == now_et.date() and \
           self.last_checkpoint_time.strftime("%H:%M") == checkpoint_str:
            return False
        
        return True
```

---

### 3.5 Audit Log (deterministic write)

```python
class AuditLog:
    """Persistent store of all decisions for later review and analysis."""
    
    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                timestamp DATETIME,
                decision_type TEXT,
                facts_json TEXT,
                daily_trend_json TEXT,
                next_move_json TEXT,
                gate_result TEXT,
                validation_passed BOOLEAN
            )
        """)
    
    def log_decision(self, timestamp: datetime, decision_type: str, 
                     facts: dict, daily_trend: dict, next_move: dict, 
                     gate_result: str):
        """Store a decision with full context."""
        self.db.execute("""
            INSERT INTO decisions 
            (timestamp, decision_type, facts_json, daily_trend_json, 
             next_move_json, gate_result, validation_passed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp.isoformat(),
            decision_type,
            json.dumps(facts),
            json.dumps(daily_trend),
            json.dumps(next_move),
            gate_result,
            True,  # Already validated before reaching here
        ))
        self.db.commit()
```

---

## 4. AI/LLM Components

### 4.1 Daily-Trend Decision

**Purpose:** Evaluate standing trend bias at designated checkpoints (06:00, 09:20, 13:00, 18:00 ET).

**Input:** facts.json (all data at/before checkpoint timestamp)  
**Output:** daily_trend.json decision (persists until next checkpoint)

**Key principle:** The decision docs (daily-trend-principles.md) should present **principles and examples**, not prescriptive rules.

**Implementation:**

```python
class LLMClient:
    """Wraps LLM API (remote or local) for decision making."""
    
    def __init__(self, mode: str = "remote", model: str = "claude-opus-4-8", 
                 principles_dir: str = "./principles/"):
        self.mode = mode
        self.model = model
        self.principles = self._load_principles(principles_dir)
        self.system_prompt = self._build_system_prompt()
    
    def _load_principles(self, principles_dir: str) -> dict:
        """Load principle docs (not prescriptive protocols)."""
        return {
            "daily_trend": read(f"{principles_dir}/daily-trend-principles.md"),
            "next_move": read(f"{principles_dir}/next-move-principles.md"),
        }
    
    def _build_system_prompt(self) -> str:
        """System prompt with principles (not rules)."""
        return f"""You are a trading decision agent making principled judgments about market direction.

Your job is to weigh multiple factors holistically, not to apply a rigid formula.

## Daily Trend Principles

{self.principles['daily_trend']}

## Next-Move Principles

{self.principles['next_move']}

---

When responding:
1. Cite the relevant principles and facts
2. Explain how you weigh them together
3. State your confidence level and why
4. Acknowledge uncertainty and trade-offs

Return JSON matching the schema provided in the user prompt.
"""
    
    def evaluate_daily_trend(self, facts: dict, checkpoint_time: datetime) -> dict:
        """Evaluate trend at checkpoint; output is a standing decision until next checkpoint."""
        
        user_prompt = f"""
## Market Snapshot (Checkpoint {checkpoint_time.isoformat()})

{json.dumps(facts, indent=2)}

---

## Your task

Evaluate the standing trend bias for the next period. Consider:
- HTF structure alignment (4hr and 1hr bars)
- Equilibrium acceptance (which side of daily/weekly mid?)
- Overnight sweep complex (did lows get swept and accepted-beyond?)
- Standing SMT / RS residue (any multi-day conflicts?)
- Recovery regime (how far below ATH?)
- Prior day/week context

Do NOT follow a rigid step-by-step formula. Weigh all factors together, considering how they interact.

Output JSON (no explanation, JSON only):

{{
  "direction": "UP" | "DOWN" | "NEUTRAL",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "regime": "TREND" | "RANGE" | "HYBRID",
  "day_dol": {{"level": str, "price": float}} or null,
  "reasoning": "<<your reasoning in 2-3 sentences, citing facts>>",
  "factors_considered": [
    {{"factor": "HTF structure", "observation": "...", "strength": "high" | "medium" | "low"}},
    ...
  ]
}}
"""
        
        response = self._call_llm(user_prompt)
        return json.loads(response)
    
    def evaluate_next_move(self, facts: dict, daily_trend: dict, now: datetime) -> dict:
        """Evaluate imminent move; driven by fresh evidence since last checkpoint."""
        
        user_prompt = f"""
## Market Snapshot (Now {now.isoformat()})

{json.dumps(facts, indent=2)}

## Standing Daily-Trend Decision

{json.dumps(daily_trend, indent=2)}

---

## Your task

Given the standing daily-trend above, evaluate the next imminent move.

Fresh evidence since the last checkpoint:
- Any new sweeps?
- SMT divergences?
- Failed mid reclaims?
- Equilibrium acceptance changes?

Build separate bull and bear cases. For each item, score it using:
- Tier weight (week-tier 3.0, day 2.0, session 1.0, fill 1.5)
- Session-side (NY-AM high-sweep 1.5, NY-AM low-sweep 0.4, PM extreme 1.3, overnight 0.7, else 1.0)
- Alignment vs daily-trend (with-trend 1.0, counter 0.3, neutral 0.6)
- Freshness decay (2^(-age_minutes/180))

Output JSON:

{{
  "direction": "UP" | "DOWN" | "NEUTRAL",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "move_target": {{"level": str, "price": float}} or null,
  "flip_trigger": "<<condition for direction flip>>",
  "flipped_target": {{"level": str, "price": float}} or null,
  "arm_entry_confirmation": boolean,
  "ledgers": {{
    "bull": [
      {{"item": str, "tier_weight": float, "session_side": float, 
        "alignment": float, "freshness": float, "score": float}},
      ...
    ],
    "bear": [...]
  }},
  "net_score": float,
  "veto_checklist": {{
    "veto_1_top_pocket": {{"applies": bool}},
    "veto_2_unverifiable": {{"applies": bool}},
    "veto_3_daily_trend_low": {{"applies": bool}},
    "veto_4_range_regime": {{"applies": bool}}
  }},
  "reasoning": "<<your reasoning in 2-3 sentences>>"
}}
"""
        
        response = self._call_llm(user_prompt)
        return json.loads(response)
    
    def _call_llm(self, user_prompt: str) -> str:
        """Route to remote (API) or local (Ollama)."""
        if self.mode == "remote":
            return self._call_remote(user_prompt)
        else:
            return self._call_local(user_prompt)
    
    def _call_remote(self, user_prompt: str) -> str:
        """Call Claude API with structured output."""
        import anthropic
        
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        return response.content[0].text
    
    def _call_local(self, user_prompt: str) -> str:
        """Call local LLM (Ollama) with structured output."""
        import requests
        
        OLLAMA_URL = "http://localhost:11434/api/generate"
        
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": self.model,  # e.g., "mistral:7b"
                "prompt": f"{self.system_prompt}\n\n{user_prompt}",
                "stream": False,
                "format": "json",
            },
            timeout=60
        )
        
        return response.json()["response"]
```

---

## 5. Protocol Documentation: Principle-Based (Not Prescriptive)

### Rewrite docs to present principles, not rules

**Old (prescriptive — WRONG):**
```markdown
# Daily Trend Decision

## Procedure (fixed order)
1. Score drivers D1–D6 with weights 3/2/1/2/1/1
2. Sum S = Σ(weight × vote)
3. If S ≥ +3 → UP; if S ≤ −3 → DOWN; else NEUTRAL
4. Confidence: if |S| ≥ 6 AND no weight-≥2 opposer → HIGH
5. Apply correlation audit (shared events get ×0.5)
```

**New (principle-based — CORRECT):**
```markdown
# Daily Trend Evaluation Principles

## Goal
Estimate the standing bias for the session: is price likely to trend up, trend down, or range?
This decision persists until the next checkpoint (06:00 → 09:20 → 13:00 → 18:00).

## Key factors to weigh (not a rigid checklist)

### HTF Structure (most important)
- Do 4hr and 1hr bars align on direction? Alignment is a strong signal.
- 4hr down + 1hr partial recovery = still downtrend (short-term relief in a bear move)
- 4hr up + 1hr down = potential reversal into chop (watch for failed break)
- Misalignment + no equilibrium consensus = range/chop likely

### Equilibrium Acceptance
- Which side of daily/weekly mid is accepting price?
- >90% of closes below mid = delivery down (strong)
- Even split both ways = chop/range
- One-sided acceptance = confirmed side has conviction

### Overnight Sweep Complex
- Did sweeps CONTINUE or REVERSE?
  - Continued (acceptance-beyond) = genuine move direction
  - Reversed (sweep-then-back) = grab/manipulation, possible reversal
- Failed mid-reclaim (closed above, then failed back) = strongest overnight tell

### Standing SMT / Cross-Ticker Residue
- Are there unresolved week/day-tier divergences from prior days?
- Persistent multi-day RS regime = context for how to read fresh SMTs

### Recovery Regime (ATH context)
- How far below ATH?
- 0–2% = potential exhaustion, breakout territory
- 2–4% = recovery mode (mild up-drift with shakeouts)
- >4% = deep correction; expect mean-reversion attempts

### Prior Day / Week Context
- Yesterday's close in its range (high / mid / low)?
- Week expanding (new highs/lows) or balanced?
- Context for interpreting overnight action

## How to integrate these factors

These factors interact; they're not independent votes to be summed blindly:
- Strong HTF alignment (4hr + 1hr agree) + one-sided acceptance = very high conviction
- HTF conflict + split acceptance = ambiguous; lean neutral or light position
- Overnight grab (sweep-then-reverse) CONTRADICTS a new breakout bias; test the direction
- Standing SMT from yesterday that contradicts today's HTF structure = check if it re-fired or is stale

**Do not simply add weights.** Reason about how the factors together paint a picture.

## Confidence calibration

- HIGH: HTF aligned, acceptance one-sided, overnight tells (failed reclaim or continued sweep), 
  no conflicting standing residue. Regime clear (trend not range).
- MEDIUM: Some factors align, others mixed. HTF conflict resolved by equilibrium acceptance 
  or vice versa. Regime unclear but direction leaning.
- LOW: Factors conflict heavily. HTF misalignment + split acceptance. Unverifiable input 
  (e.g., ATH not provided). Regime ambiguous.

## Common mistakes (anti-patterns)

- **Stale SMT as sole driver:** A multi-day divergence still sitting unfulfilled ≠ fresh signal
- **Ignoring acceptance:** Price moved against mid once; don't assume that erases the 
  overwhelming "below mid" pattern from overnight
- **Recovery mode confusion:** 4% below ATH is NOT a buy signal on its own; combine with 
  equilibrium and HTF
- **Overnight reversal timing:** Lows swept at 03:54; by 09:20 price recovered to mid-range. 
  This is a grab structure, not a breakout

## Example walkthrough

**Example: 2026-06-23 09:20 checkpoint**

Factors:
- 4hr chain: 6 bars of declining closes (18:00 open → 08:00 close). Vote: DOWN.
- Equilibrium: 96% of closes below weekly mid since session open. Vote: DOWN.
- Overnight: Failed mid-reclaim 20:44, then cascaded through 7 lower lows (all depleted). 
  Vote: DOWN.
- Standing SMT: Nothing fresh from prior day. Vote: 0 (neutral).
- Recovery: 4.36% below ATH. Vote: UP (recovery favors up-drift).
- Prior day: Mid-range close; week volatile. Vote: 0 (neutral).

Integration: 4 DOWN votes (some heavier than others) vs 1 UP vote. Equilibrium and overnight 
complex are strong evidence. Recovery mode is opposed by definitive equilibrium rejection.

**Direction: DOWN. Confidence: HIGH** (aligned HTF + strong equilibrium + overnight tells + 
only one weak opposer).

---

## Weakens to neutral if:
(Give a close-based condition, not a prediction.)

## Flips if:
(Give a close-based hysteresis trigger, e.g., "2 consecutive 5m closes above daily mid".)
```

---

## 6. Full Implementation Example

```python
#!/usr/bin/env python3
"""
Main trading decision loop.
Run continuously; processes each bar and emits decisions.
"""

import json
import pytz
from datetime import datetime

# Import components
from facts_extractor import FactsExtractor
from llm_client import LLMClient
from orchestrator import DecisionOrchestrator
from entry_gate import EntryGate
from audit_log import AuditLog
from order_service import OrderService
from validators import OutputValidator

def main():
    # Config
    config = {
        "market_db": "./market_data.db",
        "principles_dir": "./docs/principles/",
        "llm_mode": "remote",
        "llm_model": "claude-opus-4-8",
        "confidence_threshold": "MEDIUM",
        "audit_db": "./decisions.db",
    }
    
    # Initialize components
    facts_extractor = FactsExtractor(config["market_db"])
    llm_client = LLMClient(mode=config["llm_mode"], model=config["llm_model"],
                           principles_dir=config["principles_dir"])
    order_service = OrderService(broker_api=..., config=config)
    entry_gate = EntryGate(order_service, config)
    audit_log = AuditLog(config["audit_db"])
    validator = OutputValidator()
    
    orchestrator = DecisionOrchestrator(facts_extractor, llm_client, entry_gate, 
                                        audit_log)
    
    # Main loop (connect to market data feed)
    for bar in market_data_stream:
        now = bar.timestamp
        
        print(f"[{now}] Processing bar...")
        
        try:
            orchestrator.on_market_data(now)
            print(f"[{now}] ✓ Decision processed")
        except Exception as e:
            print(f"[{now}] ✗ Error: {e}")
            # Implement recovery strategy (skip this bar, log error, etc.)

if __name__ == "__main__":
    main()
```

---

## 7. Checklist for Implementation

- [ ] Facts extractor computes all levels, sweeps, divergences, equilibrium (deterministic code)
- [ ] facts.json schema defined and validated
- [ ] Principle-based docs written (daily-trend-principles.md, next-move-principles.md)
- [ ] LLM client wraps remote API (Claude) or local inference (Ollama)
- [ ] Output validators check daily_trend and next_move schemas
- [ ] Orchestrator routes facts → daily-trend (at checkpoints) → next-move (always)
- [ ] Entry gate applies arm_entry_confirmation + confidence threshold
- [ ] Audit log stores every decision with facts and reasoning
- [ ] Error handling and fallbacks defined (what if LLM fails? validation fails?)
- [ ] Tested on historical data (backtest)
- [ ] Monitored for decision quality in live trading

---

## 8. Notes on Future Enhancements

The following are NOT included in this document but can be added later:

- **Historical decision database:** Store every decision + its market outcome (correctness, P&L)
- **RAG (retrieval-augmented generation):** Retrieve similar past decisions to feed into the LLM prompt
- **Fine-tuning:** Collect decision data, fine-tune the LLM on your trading patterns
- **Backtesting harness:** Run the full system on historical data to measure decision accuracy

For now, focus on building the core system (facts → decisions → entry gate) with principle-based docs and LLM reasoning.
