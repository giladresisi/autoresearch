# Cycle 1 — preflight baselines and acceptance-gate results

Part 1 was recorded before any code change, so "unchanged" has a definition. Part 2
records the gate results afterwards.

- git `HEAD` at baseline: `186af44332899e2ceb166e36c0d36bf7757db547`
- branch: `autoresearch/strategy-agent-poc`
- date recorded: 2026-08-25
- python 3.12; pytest addopts `--timeout=60 -m 'not integration'`

---

# Part 1 — Preflight baseline (before any change)

## Test-suite baseline

| Suite | Command | Result |
|---|---|---|
| `tests/` | `python -m pytest tests/ -q --tb=no` | **2 failed, 1379 passed, 10 skipped, 12 deselected, 16 errors** (59.9 s) |
| `agent/` minus `agent/bench` | `python -m pytest agent/ -q --tb=no --ignore=agent/bench` | **3 failed, 439 passed** (58.1 s) |
| `agent/bench/test_lifecycle.py` | — | 14 passed |
| `agent/bench/test_score.py` | — | 13 passed |
| `agent/bench/test_facts.py` | — | **pre-existing TIMEOUT** (>60 s, pytest-timeout kills the process) |
| `agent/bench/test_run_bench.py` | — | **pre-existing TIMEOUT** (>60 s) |

### Pre-existing failures (NOT caused by cycle 1)

`tests/`:
- `tests/test_smt_fill_plot.py::test_regression_plot_renders_fill_mark`
- `tests/test_smt_fill_plot.py::test_session_plot_renders_fill_mark`
- 16 errors, all in `tests/test_smt_decouple_active.py` (stale patch targets) — exactly
  the "2 failures + 16 errors" the plan documents as known.

`agent/` — **not anticipated by the plan**, which only documented the `tests/` ones:
- `agent/contracts/test_menu_membership.py::test_menu_matching_predicate_passes_and_tagged_menu_hit`
- `agent/contracts/test_menu_membership.py::test_escape_hatch_valid_predicate_passes_with_tag`
- `agent/decisions/test_facts_adapter.py::test_history_supplies_prior_day_levels`

### `agent/bench` timeouts

`agent/bench/test_facts.py` and `test_run_bench.py` construct `ParquetFactsSource`,
which eagerly loads the machine-global 1s parquets (`MNQ_1s.parquet` ~112 MB,
`MES_1s.parquet` ~82 MB). Measured: **6.5 s to load, 24.5 s per `build_facts` call**
(17 days of 1s data through `compute_facts`). A test that does two boundaries exceeds
the project-wide 60 s ceiling. Pre-existing, and it directly constrains gate 1 —
`agent/facts/test_parity_harness.py` therefore carries an explicit
`@pytest.mark.timeout(900)`.

## 07-23 5m reconciliation (Task 2 Step 5)

`l2-mechanisms.md` §11 records an unresolved 07-23 discrepancy: a 1m-resampled 5m
series showed the 09:40–09:44 bar closing **28856**, above the bound gap top
**28827.5**, *before* a validated fill — which close-through eligibility should have
killed.

`agent.facts.bars.resample(win, "5min")` over `MNQ_1m.parquet`, 09:30–10:00 ET:

```
                               Open      High       Low    Close
2026-07-23 09:30:00-04:00  28718.00  28764.75  28656.00  28759.5
2026-07-23 09:35:00-04:00  28759.75  28817.50  28735.50  28797.0
2026-07-23 09:40:00-04:00  28797.50  28883.75  28796.50  28856.0   <-- 28856.0
2026-07-23 09:45:00-04:00  28856.00  28868.25  28748.50  28760.5
2026-07-23 09:50:00-04:00  28760.00  28819.75  28717.25  28816.0
2026-07-23 09:55:00-04:00  28816.00  28835.75  28696.00  28707.5
```

**Verdict.** This module's convention (left-labeled, closed-left, completed bins only,
session-anchored to 18:00 ET) **reproduces the 28856 reading**. The discrepancy is
therefore **not a resampling-convention artifact** — the two "competing readings" were
never in conflict about the bar; 28856.00 is what a 5m bar built from 1m closes at.

The open question is consequently narrower and sharper than §11 states: a close-through
rule applied on the **5m** timeframe would have killed that gap at the 09:40 close,
55.5 pts above the bound gap top of 28827.5, before the validated fill. So either the
recorded fill's eligibility was evaluated on a different timeframe than 5m, or the
bound gap was not a 5m gap. **Recorded finding, not a code change** — the consumer of
this verdict is out of cycle-1 scope.

---

# Part 2 — Acceptance-gate results (after implementation)

## Gate 1 — facts parity vs offline `derive_facts` (Task 19)

```
python scripts/facts_parity.py --dates 2026-07-14,...,2026-07-27      # exit 0
```

| date | hashed S0–S7 identity equal | contract-key diffs | S8/S9 advisory diffs |
|---|---|---|---|
| 2026-07-14 09:20 | YES | none | none |
| 2026-07-15 09:20 | YES | none | none |
| 2026-07-16 09:20 | YES | none | none |
| 2026-07-17 09:20 | YES | none | none |
| 2026-07-20 09:20 | YES | none | none |
| 2026-07-21 09:20 | YES | none | none |
| 2026-07-22 09:20 | YES | none | none |
| 2026-07-23 09:20 | YES | none | none |
| 2026-07-24 09:20 | YES | none | none |
| 2026-07-27 09:20 | YES | none | none |

All ten dates: identical `sha256` of the rendered S0–S7 sheet, identical values for
every one of the 15 `DECIDE_THESIS_KEYS`, identical `evidence_magnitude`, and
identical S8 `menu_text` / S9 `evidence_text` (the deliberately-unhashed advisory
blocks). **No diffs to resolve or document.**

What this gate actually proves, and what it does not: the online assembler routes the
L1 view through `derive_facts.compute_facts` and the shared
`agent.bench.facts.bundle_to_l1_view`, so S0–S9 *content* is equal by construction.
What the gate tests is the **input preparation** — the 17-day slice, the strictly-before
`< boundary` cut, the CME-maintenance filter bounds, and which frame the all-time high
is taken from. That is where a silent divergence would actually live. (An early draft
took the ATH from the maintenance-filtered frame; the reference takes it from the raw
one. Fixed before the gate ran.)

## Gate 2 — store equivalence (Task 20)

`agent/facts/test_gate_store_equivalence.py` — **7 passed**. Cold store vs
twice-populated store produce identical fact ids AND identical states, for both
requirements. Extended beyond the plan to compare every fact FIELD (prices, windows,
names, `state_ts`, `extra`), and to prove a store seeded by the *other* requirement
does not leak into this one.

## Gate 3 — per-second latency budget (Task 21)

`agent/trader/test_gate_latency.py` — **3 passed**, budget 20 ms p95.

Measured directly (300 samples of `Executor.on_bar(..., bar_complete=False)`, against a
WARM store, on the frames the graft actually hands over):

| frame | rows | per-second mean | per-second p95 | bar-close mean | bar-close p95 |
|---|---|---|---|---|---|
| session only | 600 | 2.62 ms | **2.82 ms** | 25.7 ms | 66.8 ms |
| 17-day history spliced | 24,480 | 2.92 ms | **3.10 ms** | 36.0 ms | 93.2 ms |

Per-second cost is ~15 % of budget and essentially flat in frame size. Bar-close work
(once per minute) uses ~4 % of the minute available to it.

Two hot spots were found and fixed while measuring this, both only visible once real
history reached the Executor — on a 24k-row frame the bar-close cascade originally cost
**820 ms mean / 1083 ms p95**, i.e. it overran the 1-second loop cadence:

1. `agent/facts/bars.py::resample` computed the session origin with a Python call per
   row (`[_session_origin(ts) for ts in src.index]`). Vectorised.
2. `Executor._refresh_avg_range` resampled the WHOLE frame to 1h to read the last 20
   bars. Now resamples a 30-hour tail.

After both: 36 ms mean / 93 ms p95 — a 23x improvement.

**Residual, recorded not fixed:** the FIRST bar close after the plan arms costs ~700 ms
on a 17-day frame (the one-time 14-day lookback fill through `ensure_coverage`). That is
a single ~0.7 s stall on the bar loop, once per session, under the 1 s cadence but not
by much.

**Not measured:** the plan's Task 1 Step 2 whole-loop `backtest_smt.run_backtest_v2`
wall-clock baseline. See "Validation gaps" below.

## Gate 4 — legacy undisturbed (Task 22)

### Suites, after implementation

| Suite | Baseline | After | Delta |
|---|---|---|---|
| `tests/` | 2 failed, 1379 passed, 16 errors | **2 failed, 1390 passed, 16 errors** | +11 passing, **0 new failures** |
| `agent/` minus bench | 3 failed, 439 passed | **3 failed, 726 passed** | +287 passing, **0 new failures** |
| `agent/bench/test_score.py` + `test_lifecycle.py` | 27 passed | **27 passed** | unchanged |

The 2 + 16 in `tests/` and the 3 in `agent/` are the same pre-existing items listed in
Part 1, unchanged.

### Flag-off byte identity

The plan asks for `regression.py --dates 2026-08-13 --mode 1s`; **2026-08-13 has no
locked baseline**, so that run would LOCK a new one rather than diff. Ran the two dates
that DO carry locked 1s baselines instead:

```
python regression.py --dates 2026-05-18 --mode 1s --no-plot --skip-lock
2026-05-18: events=PASS trades=PASS n_trades=31 pnl=1572.00
```

```
python regression.py --dates 2026-05-19 --mode 1s --no-plot --skip-lock
2026-05-19: events=PASS trades=PASS n_trades=23 pnl=-367.00
```

`events.jsonl` and `trades.tsv` both diff **clean** against the locked baselines with
`ACT_TRADER` unset, on both dates.

**Harness note, worth knowing before anyone repeats this.** Running the two dates in ONE
invocation (`--dates 2026-05-18 2026-05-19`) makes 2026-05-19 report `events=FAIL`. The
diff is exactly two extra lines:

```
> {"kind": "smt-carry", "reason": "drop_fulfilled", "ref_name": "fvg_20260515_1600_bear", ...}
> {"kind": "smt-carry", "reason": "drop_fulfilled", "ref_name": "fvg_20260518_0400_bull", ...}
```

— carry-over state from the 2026-05-18 run in the same process, emitted at the
2026-05-19 session open. `n_trades` and `pnl` are identical either way. The locked
baselines were captured from single-date runs, so a multi-date invocation is NOT
comparable against them. Nothing to do with cycle 1; each date run separately passes.

Supporting evidence: `git diff 186af44 -- session_pipeline.py` is **16 insertions, 0
deletions** — one `trader=None` kwarg plus one `self._trader = trader` assignment in
`__init__`, and one additive `if self._trader is not None:` block in `on_1m_bar` that
does not early-return. With the flag off the entire runtime cost is one `is not None`
comparison per bar and zero state is touched.

### No forbidden writes / no `live_orders`

The plan specifies a `grep`. A grep cannot tell CODE from a DOCSTRING that names the
forbidden thing, and every new module documents what it must not touch — so the grep
reports its own warning labels as violations (7 hits, all prose). Replaced with an
AST-based gate, `agent/trader/test_gate_no_legacy_writes.py` (**71 passed**), which
parses every non-test module in `agent/facts/` and `agent/trader/` and asserts:

- no `import live_orders` / `from live_orders import …` (direct or transitive-by-name),
- no call to `save_daily` / `save_position` / `save_hypothesis` / `save_smts` /
  `save_global` / `freeze_active_mgmt` / `set_state_dir`,
- no string literal equal to `events.jsonl` / `daily.json` / `hypothesis.json` /
  `position.json` / `smts.json` anywhere outside a docstring,
- the `session_pipeline` trader hook does not early-return and the legacy daily block
  still follows it.

### `agent/bench/facts.py` refactor is behavior-preserving

Task 10 extracted the S8/S9 overlay block out of `ParquetFactsSource.build_facts` into
a module-level `bundle_to_l1_view(bundle)` so the online assembler and the offline
bench share one copy. Verified against the pre-change file (`git show 186af44:`) on two
independent dates — `text`, `content_hash`, `validator_dict`, `menu_text`,
`evidence_text`, `evidence_magnitude`, `levels`, `now_price`, day extremes and the
degraded/error flags are all **equal**:

```
2026-07-22: ALL EQUAL = True  (vd keys=21, hash=4e126c254ff1)
2026-07-16: ALL EQUAL = True  (vd keys=21, hash=c78cb6f1351b)
```

---

## Validation gaps

0. **A post-review fix round changed several of these numbers.** Two blocking findings
   (the chain being inert on the live `bar_complete=False` cadence, and the Analyzer
   receiving session-only bars instead of 17 days of history) were fixed after the
   gates first ran; every gate above was then re-run. The suite/gate numbers here are
   the POST-fix ones.

1. **Whole-loop wall-clock baseline not measured.** The plan's Task 1 Step 2 asks for
   `backtest_smt.run_backtest_v2("2026-08-13", …, mode="1s")` timing before and after
   (Task 21 Step 3). The isolated per-second measurement above is the number the 20 ms
   budget is actually about and it is unambiguous; the whole-loop figure would add a
   combined-cost check that the flag-off byte-identity result already makes moot (with
   `ACT_TRADER` unset the trader contributes exactly one `is not None` per bar).
   **Unverified:** the combined per-second cost with the trader ARMED inside a real
   1s backtest loop.
2. **No live/shadow session has been run.** `docs/cycle1-shadow-runbook.md` describes
   the configuration and `scripts/compare_analyzer_vs_legacy.py` is tested against
   synthetic and real-shaped records, but the end-to-end shadow session
   (`LIVE_TRADING=true` + `DISCONNECTED=true` + `ACT_TRADER=1`) has **not** been
   executed, and no real `trader_decisions.jsonl` exists.
3. **No real LLM call.** Every Analyzer test uses a stub backend. The
   `thesis_via_decide_thesis` adapter and the `automation/main.py` `_build_trader`
   wiring are exercised only for their flag-off / failure-degradation paths.
4. **The Executor's mechanism binding is one generic FVG binder**, not four
   per-mechanism implementations — see the execution report's "autonomous decisions".
   Its behavior is verified against synthetic tapes only; no real-tape case from
   `l2-mechanisms.md` §11 has been replayed (that is explicitly cycle-2 work).
