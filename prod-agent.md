# prod-agent.md — POC → Production Decision Agent (GIL-44 roadmap, v2)

Scope for taking the agentic strategy layer from the POC setup (Claude Code agents reading
`agent-docs/strategy/` + a facts sheet) to a production decision agent integrated with the
trading scripts.

**v2 ordering (2026-07-07):** build with no delays; the model swap and the docs-weight
recalibration are config/content changes that do not block integration. The full Claude-based
calibration sweep is **withdrawn** — the confidence→hit-rate curve is model-specific, so
calibration runs ONCE, LATER, on the actual production model. The sweep assets (29 cut folders
+ ground-truth TSV + triplicates + scorer + validator) are model-agnostic and stay; they become
the bench/calibration harness of Phase 4.

**The one hard line:** the entry-gate flag flips ON only after Phase 4 has been run on the
exact production configuration (model + docs + facts pipeline). Everything upstream of that
flag moves as fast as possible; nothing with uncalibrated confidence ever decides a real entry.

**Backend decision (2026-07-07):** the production backend is the **Anthropic API (Haiku 4.5)**
— local inference is DEFERRED (this box: 16GB RAM, CPU inference too slow + contends with live
trading; a stopgap small model would need its own throwaway calibration). Economics at event
cadence: ~$0.02/decision with the docs as a cached byte-stable system prompt (writes $1.25/M,
reads $0.10/M), ≈$0.55–0.85/session, <$40/month worst-case. Structured outputs
(`output_config.format`) give schema-guaranteed JSON on the API too. API failure → validator
fail-safe (NEUTRAL/LOW = scripts baseline). Local returns as a config-swap option after the
cloud-machine migration, benched via Phase 4's assets; GPU/VRAM audit matters only then.

---

## Phase 1 — Validator (in progress)

Deterministic trust boundary for every LLM decision, permanent across all phases.

- **Checks:** (1) syntactic — schema/enums/required fields per form; (2) arithmetic/protocol —
  integer votes, contributions = weight×vote (D1 halving only), S = Σ, ledger item score =
  tier×side×alignment×freshness(×whipsaw), N = bull−bear, direction/thresholds consistent,
  confidence tiers respect veto effect-classes, item types ∈ the closed list; (3) semantic —
  target is a real, un-depleted level on the correct side; triggers on correct sides;
  checkpoint matches S0.
- **Failure policy:** offline → flag `protocol_clean=false`; runner/production → re-prompt ≤2×
  quoting exact violations → fail-safe **NEUTRAL/LOW = scripts baseline**.
- **Requirement:** a reusable `validator.py` module (imported by the Phase-2 runner and the
  Phase-3 orchestrator) — not a sweep-only script.
- **Acceptance:** catches seeded fixtures of every known past violation (run-4 fractional
  vote, invented item types, wrong-side target); zero false positives on runs 3/5; <1s.

## Phase 2 — Runner + sanity check

`run_agent.py`, backend-pluggable — this IS the production LLMClient.

- Prompt assembly: system = decision docs VERBATIM + concept docs (single source of truth,
  never paraphrased), rendered **byte-stable** with a cache breakpoint after it (the 90%
  cache-read discount depends on this); user = rendered facts + task. Output enforced via
  **structured outputs** (`output_config.format` JSON schema).
- Two calls matching production shape: `daily_trend(facts_subset)` at the checkpoint, then
  `next_move(facts_subset, standing_daily)` (~10–15K tokens each).
- Backends behind one interface: **Anthropic API (Haiku 4.5) = default**; OpenAI-compatible
  local (Ollama/vLLM, grammar-constrained decoding) kept as a deferred config option — no
  local install now. Validator + retry loop wired in.
- **Sanity check (the ONLY gate for this phase):** run the default backend on the **5 burned
  POC cuts** (known truths, known good reports): ≥95% validator pass within ≤2 retries,
  coherent outputs, cache-read tokens confirmed nonzero. Blindness irrelevant for sanity.

## Phase 3 — Production merge (shadow / gate-OFF)

Full integration, shipped with the repo's standard discipline — this is code touching the live
pipeline, so: **flag-gated default OFF, 1s A/B byte-identical with the flag off, unit tests.**
(This is the normal GIL-39/GIL-42 pattern, not the deferred calibration.)

- **Facts module:** productionize `derive_facts` — same computations fed from the live rolling
  frames (no parquet re-reads), JSON internally (validator/audit), prompt-facing text rendered
  from the JSON. One source, two views.
- **Orchestrator (deterministic state machine):** model calls ONLY on events — daily-trend at
  checkpoints (06:00/09:20/13:00/18:00; dedup; data ≤ checkpoint; early re-eval on standing
  invalidation with 2×5m-close hysteresis); next-move on new engine fire / resolution or flip
  trigger / checkpoint change + coarse heartbeat. Never per bar. Standing decisions persisted
  like other strategy state.
- **Entry gate (deterministic):** permits/steers the EXISTING hypothesis-confirmation pipeline
  only (arm confirmation, direction/target hints) — never places orders (GIL-31/41).
  Neutral / low / validator-fail ⇒ scripts unchanged. Threshold value = Phase 4 output;
  until then the flag stays OFF.
- **Audit log:** every call → facts snapshot + decision + reasoning + (later) outcome. Doubles
  as the seed of the experience/precedent layer, and shadow logs double as extra calibration
  data on the production model.
- **Shadow operation:** decisions computed + logged every session, gate inert. Requires only
  the Phase-2 sanity check — shadow cannot lose money. Respect the contention prerequisite.

## Phase 4 — Bench + calibration on the production model → gate enablement

Now one activity, run on the exact production configuration:

- Point the runner at the **29 calibration cuts + triplicates** (unattended afternoon).
- Read: validator pass-rate (≥95% within ≤2 retries), direction hit-rate per confidence tier
  **vs ground truth**, triplicate judgment variance, latency (≤60s/decision).
- If the production model underperforms, swap the backend config (Sonnet-tier API, or local
  classes after the cloud migration) and re-run — the swap is a config change by design. Model
  market-knowledge is irrelevant (all knowledge injected); what's tested is
  instruction-following, table reading, arithmetic discipline. Any future local-model move
  re-runs this phase on the same assets before its gate enablement.
- Retune docs weights from the measured misses per the rule-change policy (weights →
  precedents → veto caps; never branches). A docs change re-runs the cuts (cheap, push-button).
- **Set the entry-gate threshold from the measured curve**, enable the flag behind its own
  1s A/B on representative regime days (backtest-before-merge applies to the whole agent
  layer), then supervised live sessions before unattended operation.
