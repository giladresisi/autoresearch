# Test-suite cleanup + perf — findings & recommendations

**Issue:** GIL-28 · **Branch:** `autoresearch/test-suite-cleanup` (off `master` @ ebba717)
**Mode:** originally EXPLORE-AND-RECOMMEND; the clean deletes/merges + perf fixes have now been
**IMPLEMENTED** (per user approval). All changes are **UNSTAGED** (no commit/push). The v1-SMT and
equities tranches (Table 1b) were **NOT** touched — they remain your go/no-go.

---

## ✅ IMPLEMENTATION RESULTS (executed 2026-06-14, IB Gateway active)

### What was done
**Deleted (4 files):** `test_migrate_to_global_paths.py`, `test_selector.py`, `test_fold_auto_detect.py`
(content merged), `test_smt_dispatch_order.py` (content merged/trimmed).
**Merged:** 8 `_compute_fold_params` tests → `test_optimization.py`; dispatch-order *ordering* test
re-homed → `test_session_pipeline.py::test_on_1m_bar_dispatch_order_trend_hypothesis_strategy`
(mocked `on_1m_bar`, no backtest). Dispatch tests 2/3/5 dropped as proven duplicates.
**Perf fixes:**
- `test_ib_realtime.py::test_gap_fill_not_called_from_start` — patched the **public** `gap_fill`
  (start() calls it; the test only stubbed private `_gap_fill`). **60s hang → 0.22s**, and the
  whole suite no longer aborts (`os._exit`) when IB is down.
- `test_pickmytrade_executor.py` — autouse fixture stubs `httpx.Client` (real construction is
  **~1.7s each**, the true cause — *not* the retry sleep) + no-ops the backoff. **47.7s → 1.70s.**
- `test_orchestrator_main.py` — autouse stub of `_check_ib_reachable` (real socket probe ~2s/test).
  **11.6s → ~5s, and 3 of its 4 failures fixed as a side effect.**

### Deviation from the report
The dispatch-order **v2 smoke test was dropped, not re-homed.** With IB active, parquet is present,
so that test runs a real `run_backtest_v2` (~28s) — re-homing it would have reintroduced the #2
bottleneck. Its coverage (the `run_backtest_v2` return-shape) is redundant with the regression path
(`regression.py:136` reads those keys; `test_smt_regression.py` asserts them).

### Before → after (full single-process `pytest tests/`, IB active)
| | Before | After |
|---|---|---|
| Wall-clock | **could not complete** (os._exit hang at `test_ib_realtime`) | **63.7s** (1.06 min) |
| Slowest test | 47.7s file / 60s hang | **4.1s** (`test_prepare::test_main_exits_1`) |
| `test_pickmytrade_executor` | 47.7s | **1.70s** |
| `test_ib_realtime` | ~60s hang → kill | **~2.2s** |
| `test_orchestrator_main` | 11.6s (4 fail) | **~5s (1 fail)** |
| Result | (suite aborted) | **1402 passed, 28 failed, 6 skipped** |

### Failures: zero introduced by these changes
All 28 failures are **pre-existing / environmental** (stale mocks, missing `MNQ_CONID/MES_CONID`,
business-logic assertions). Counts vs the pre-edit baseline are identical per file
(pickmytrade 8, hypothesis_smt 8, check_session_parquets 3, automation_main 2, smt_humanize 2),
**orchestrator_main improved 4→1**, and **test_ib_realtime's 4** were always failing but were
*hidden* by the os._exit hang — they're now simply visible. My added/merged tests all pass.

### Newly-VISIBLE pre-existing failures worth a follow-up (NOT fixed — out of this scope)
- `test_ib_realtime.py` ×4 — stale `FakeIB` stub (missing `errorEvent`) + `MNQ_CONID/MES_CONID`
  not set. These do real-IB-shaped work and should be made hermetic.
- `test_orchestrator_main.py::test_pre_session_init_skips_when_no_api_key` — **stale test**: asserts
  `"DATABENTO_API_KEY not set"` but `_pre_session_init` no longer has any Databento logic
  (docstring: *"Databento disabled… All backfill is IB-only"*). Delete or update the assertion.
- The other pre-existing failures (pickmytrade slippage/modify, hypothesis_smt case-logic,
  smt_humanize slippage, automation_main session-end, check_session_parquets) predate this work.

### Collection hygiene (still recommended, not done)
Root `test_ib_connection.py` / `test_live_run.py` / `test_step5_realtime.py` still abort a bare
`pytest` (they connect/read at import). Use `pytest tests/` or add `testpaths=["tests"]`.

---

## TL;DR

- **Suite is faster than the issue estimated, and the slowness is I/O/sleep-bound, not CPU.** The
  big high-count SMT files run in ~1–2s (e.g. `test_smt_strategy.py`, **105 tests in 1.20s**).
  Total ≈ **188s isolated** across 60 files (overstated — see note); the real cost is concentrated
  in **4 files** (~162s).
- **The "v1 is dead" premise is only half true.** The v1 SMT stack (engine, `run_backtest`,
  `hypothesis_smt`, signal mode) is **gated OFF in the current deployment** by `.env`
  (`SMT_PIPELINE=v2`, `LIVE_TRADING=true`) **but is still fully wired — one env-flag flip from
  live**, and it **shares low-level primitives** (`detect_smt_divergence`, `detect_smt_fill`,
  `divergence_score`, `compute_tdo`) that the v2 live path *does* use. So v1 tests can't be deleted
  on the filename — most need primitive-test extraction first, and the decision is yours.
- **Only 2 files are clean, low-risk deletes** (`test_migrate_to_global_paths`, `test_selector`),
  2 are clean merges (`test_fold_auto_detect`, `test_smt_dispatch_order`), and the issue's
  "trivial trio" all **survive** scrutiny.
- **Biggest perf wins are 3 bug/mock fixes** worth ~130s combined, plus `pytest-xdist -n auto`
  as the suite-level multiplier.

> ⚠️ **Note on the numbers.** Timing was done **per-file in isolation** because the suite cannot
> complete a single-process run in this worktree: `test_ib_realtime.py` makes a **real IB
> connection** and on Windows `pytest-timeout` `os._exit()`s the whole process, aborting every
> file after it (see Perf #1). On your live machine (IB Gateway up) it completes. Isolated per-file
> times each re-pay Python/pandas/matplotlib import startup (~0.5–2s/file), so the 188s **sum
> overstates** the real single-process wall-clock (likely ~60–90s with IB up). Per-file ranking is
> still the right signal for *where* the cost is.

**Headline:** ~**20–24 tests safe to drop now** (2 deletes + 2 merges, no coverage loss), a larger
v1/equities tranche pending your go/no-go, and **~130s of perf wins** from fixing one test bug +
two sleep-mock gaps (biggest single win: the `test_ib_realtime` patch-target bug, which also
unblocks IB-less full-suite runs).

---

## How this was measured

```
python -m pytest tests/<file>.py -q -p no:cacheprovider --durations=N -o addopts="--timeout=30 -m 'not integration'"
```
run once per file (60 files), plus targeted `--durations` on the slow survivors. Counts:
**1384 passed / 27 failed / 6 skipped / 10 deselected** (the 27 failures are environment-related —
no IB, no network, no parquet data — matching the known ~24 pre-existing failures; they are *not*
caused by anything here).

---

## Reachability map (the basis for every deletion verdict)

Two disjoint pipelines + a shared primitive layer:

| Layer | Modules | Status in current deployment |
|---|---|---|
| **v2 live + regression** | `strategy.py`, `hypothesis.py`, `trend.py`, `session_pipeline.py`, `daily.py`, `backtest_smt.run_backtest_v2`, `automation/main.py` | **LIVE** (`.env` `SMT_PIPELINE=v2`, `LIVE_TRADING=true`) |
| **Shared primitives** | `strategy_smt.{detect_smt_divergence, detect_smt_fill, divergence_score, compute_tdo, _BarRow, detect_eqh_eql, level helpers}` | **LIVE** — used by v2 via `hypothesis.py:223,1102,1143` |
| **v1 SMT** | `strategy_smt.process_scan_bar`/`ScanState`/`manage_position`, `backtest_smt.run_backtest`, `hypothesis_smt` (`HypothesisManager`), `signal_smt.py` | **GATED OFF** but fully wired; re-activates if `SMT_PIPELINE`≠v2 or `LIVE_TRADING`=false |
| **Equities (legacy)** | `train.py`, `prepare.py`, `screener*.py`, `strategy_selector.py` | **Disjoint** from SMT; manual `uv run` CLIs only; no scheduler/live caller |

Key proofs (file:line):
- `.env:9 SMT_PIPELINE=v2`; `automation/main.py:159` v1 `_process(bar)` is the *fallback* branch; v2 routes via `_smtv2_dispatcher…on_1m_bar` → `SessionPipeline`.
- v2 uses shared primitives: `hypothesis.py:223 from strategy_smt import detect_smt_divergence, detect_smt_fill` (called `:1102`, `:1143`).
- `backtest_smt.run_backtest` (v1) has **no live/regression caller** — only experiment scripts (`plan{1,2,3}_experiment_runner.py`, `diagnose_bar_resolution.py`, `_run_pre_strategy_update.py`) + tests. Regression uses `run_backtest_v2` (`regression.py:136`).
- `hypothesis_smt.compute_hypothesis_context` is called **only** at `backtest_smt.py:521` (inside v1 `run_backtest`); `hypothesis.py` (v2) does **not** import `hypothesis_smt`. → `hypothesis_smt` is NOT on the v2-live/regression path; it's signal-mode + v1-backtest only.
- Equities: no SMT/orchestrator/automation module imports `train`/`prepare`/`screener*`/`strategy_selector`; `strategy_selector.select_strategy` has **zero** callers anywhere.

---

## TABLE 1 — Delete / merge candidates (ranked by confidence)

| # | File / scope | Category | Evidence code is dead/duplicated | Tests removed | Risk |
|---|---|---|---|---|---|
| 1 | **`tests/test_migrate_to_global_paths.py`** | one-time migration | `scripts/migrate_to_global_paths.py` self-documents one-shot ("run ONCE"); migration already executed on live (MEMORY: "migration RUN on live worktree"); zero non-test callers; script idempotent+refuse-overwrite. New layout (`paths.py`) is now the live source of truth. | **4** | **Low** |
| 2 | **`tests/test_selector.py`** | dead code | `strategy_selector.select_strategy` (`strategy_selector.py:158`) has **no caller** in any runnable path (live, regression, or manual CLI) — only self-references + this test. | **15** | **Low** |
| 3 | **`tests/test_fold_auto_detect.py` → merge into `test_optimization.py`** | redundant-as-separate-file | `_compute_fold_params` (`train.py:440`) is live-but-equities; the 8 tests cover real branch logic with **no duplicate**, but belong in the same-module home `test_optimization.py`. *No coverage loss; consolidation only.* ⚠️ moot if the equities tranche (Table 1b) is deleted. | **0** (move 8) | **Low** |
| 4 | **`tests/test_smt_dispatch_order.py` → merge into `test_session_pipeline.py`** | redundant + mis-homed | Dispatch order lives in `session_pipeline.on_1m_bar` (`:778/1135/1191`), not `backtest_smt`. Tests 2/3/5 are **already covered** at the real dispatch site (`test_session_pipeline::test_on_1m_bar_calls_{trend,strategy}_every_bar`, `_hypothesis_only_on_5m`; `_level_sweep_resets_direction_*`; and ~30 `run_backtest` sig call-sites). Tests 1 (ordering) + 4 (v2 smoke) are unique → re-home mocked. **Also the #2 perf offender (27.7s of real in-process backtests).** | **3** (re-home 2) | **Low–Med** |

**Subtotal (clean, no coverage loss): ~22 tests dropped (4 + 15 + 3) + 8 relocated.**

### TABLE 1b — Needs your go/no-go (proof-backed, but a judgment call, not auto-delete)

| Scope | Why it's a candidate | Why it's NOT a clean delete | Tests | Risk |
|---|---|---|---|---|
| **v1 SMT backtest** — `test_smt_backtest.py` (+ `run_backtest`-driven minorities in `test_smt_strategy.py` ~10, `test_smt_position_arch.py` ~3) | `run_backtest` v1 is off-deployment & has no live/regression caller | Each file **mixes** dead-v1-wrapper tests with **live-primitive** unit tests (`divergence_score`/threshold/decay in `test_smt_backtest.py:501-638`; `detect_smt_fill` in `test_smt_position_arch.py:127,146`). Delete **only after extracting** those ~dozen live tests. v1 stack is one env-flag from live. | up to ~70 | **Med** |
| **Equities legacy** — `test_optimization.py` (47), `test_backtester.py` (32), `test_screener.py` (32), `test_screener_prepare.py` (14), `test_screener_script.py` (10), `test_prepare.py` (27) | Entire stack disjoint from SMT live+regression; no scheduler/CI caller | Still reachable as **manual `uv run` workflows** and referenced by the `prepare-optimization` / `fetch-strategies` skills (read `train.py`/`prepare.py`). Delete only if you've retired equities screening/optimization. | ~162 | **Med** |

### TABLE 1c — KEEP (investigated; deletion premise fails)

| File | Tests | Why KEEP |
|---|---|---|
| `test_commit_note.py` | 4 | Prod fn wraps body in `except Exception: pass` (`scripts/commit_note.py:49-69`) + fire-and-forget caller → a break is **silent**, not "caught loudly elsewhere". No duplicate. |
| `test_smt_fill_plot.py` | 2 | Asserts **real logic** (fill_*→'F' label collapse; `fvg_*` name reconstruct) duplicated across `plot_session.py`/`plot_regression.py`; not a "file produced" smoke. Plotters run every analysis. |
| `test_regression_run_dirs.py` | 3 | Sole guardian of run-dir naming / `info.md` contents / location-independence; `test_smt_regression.py` overlaps only partly **and skips** without real parquet. |
| `test_smt_strategy.py` | 105 | Direct unit coverage of **live** `detect_smt_divergence`/`divergence_score` decay (used by v2 via `hypothesis.py`). Only ~10 v1-`run_backtest` tests are droppable. |
| `test_hypothesis_smt.py` | 22 | Guards `hypothesis_smt` rule engine. Live **only** in signal-mode/v1-backtest (NOT v2-live, contrary to first pass) — but signal mode is still wired ⇒ not provably dead. |
| `test_signal_smt.py` | 15 | `signal_smt.py` is the orchestrator's `LIVE_TRADING=false` subprocess (`orchestrator/main.py:475`), still spawned/maintained. Its live twin is covered by `test_automation_main.py`, but its own v1 state machine has no other owner. |

### TABLE 1d — Collection hygiene (not in `tests/`, but worth fixing)

`test_ib_connection.py`, `test_live_run.py`, `test_step5_realtime.py` (repo **root**) are live manual
scripts that connect to IB / read parquet **at import time**. A bare `pytest` collects them
(name matches `test_*`) and **aborts collection** when IB is down. Recommend renaming (drop the
`test_` prefix, e.g. `manual_ib_connection.py`) or adding `testpaths = ["tests"]` to `pyproject.toml`.

---

## TABLE 2 — Performance optimizations (ranked by time saved)

| # | Target | Measured | Root cause (file:line) | Proposed change | Est. saved | Risk |
|---|---|---|---|---|---|---|
| 1 | **`test_ib_realtime.py::test_gap_fill_not_called_from_start`** | **~60s + aborts whole suite** | Test patches **private** `_gap_fill` (`:153`) but `start()` calls **public** `gap_fill()` (`ib_realtime.py:730`) → real IB call → `_time.sleep(20)` retry → 60s `os._exit`. | Patch the **public** `gap_fill` (and/or `gap_fill_1m_ib`). Fixes a **vacuous regression guard** too. | ~60s + unblocks single-process IB-less runs | **Low** (test-only; verify assertion intent) |
| 2 | **`test_pickmytrade_executor.py`** | **47.7s** (50 tests, 8 fail) | Prod `execution/pickmytrade.py:272 time.sleep(2**attempt)` backoff; only *some* tests patch `_mod.time.sleep` (`:315,330,572`). Unpatched/retry tests pay real 2+4+8+16s waits. | Module-scoped **autouse** fixture patching `execution.pickmytrade.time.sleep`; ensure `_http.post` always mocked. | ~40s | **Low** |
| 3 | **`test_smt_dispatch_order.py`** | **27.7s** (5 tests) | 3 tests run real in-process `run_backtest_v2` (loads futures data, full backtest). | **Merge/trim** (Table 1 #4): drop the 3 backtest-driven dups; re-home the 2 unique tests as mocked `on_1m_bar` assertions in `test_session_pipeline.py`. | ~27s | **Low–Med** |
| 4 | **`test_orchestrator_main.py`** (4 tests) | 11.6s (~2s × 4) | `pre_session_init`/`main` tests don't patch `time.sleep` (prod `orchestrator/main.py:320,451`); other tests already do (`:55,81,144`). | Extend the existing `patch("orchestrator.main.time.sleep", …)` to the 4 slow tests. | ~8s | **Low** |
| 5 | **Suite-level: `pytest-xdist`** | n/a | Single-process; cores idle. (xdist not in deps.) | Add `pytest-xdist` to dev deps; run `pytest -n auto`. Distributes 60 files across cores. | **3–4× wall-clock** on multi-core | **Low** (watch for shared-state/`paths.set_state_dir` tests; mark `-p no:xdist` or serialize if needed) |
| 6 | `test_session_pipeline.py` | 9.0s (71 tests) | Death-by-1000-cuts: ~0.13s/test of monkeypatch/state reset. Slowest single test 0.46s. | Promote shared read-only fixtures (loaded DataFrames) to module/session scope where state isn't mutated. | ~3–4s | **Med** (state-reset tests are correctness-sensitive) |
| 7 | `test_prepare.py` | 25.9s first run / ~3.4s calls | yfinance is **mocked**; 25.9s was import/variable cost on that run (`test_main_exits_1` 3.0s). | Low priority; folds away if equities tranche (1b) is dropped. Otherwise investigate `import prepare` cost. | ~variable | **Low** |
| 8 | `test_smt_fill_plot.py` | 4.3s (2 tests) | matplotlib/plotly import + **real** PDF/HTML render — inherent to what it asserts. | Leave as-is (KEEP); import amortizes in full suite. | ~0 | n/a |

---

## Raw evidence — per-file durations (isolated, `--timeout=30`, IB down)

```
47.70s  test_pickmytrade_executor.py   (8 failed, 42 passed)
~60s    test_ib_realtime.py            (hang → os._exit; see Perf #1)
27.71s  test_smt_dispatch_order.py     (5 passed)        ← also merge candidate
25.89s  test_prepare.py                (26 passed, 1 desel)  first-run; ~3.4s steady
11.58s  test_orchestrator_main.py      (4 failed, 19 passed)
 9.02s  test_session_pipeline.py       (71 passed)
 5.15s  test_smt_decouple_active.py    (16 passed)       ~4s import overhead
 4.34s  test_smt_fill_plot.py          (2 passed)        matplotlib render
 3.45s  test_check_session_parquets.py (3 failed, 48 passed)
 3.24s  test_optimization.py           (47 passed)       NOT slow — sklearn import is per-process
 3.08s  test_automation_main.py        (2 failed, 22 passed)
 2.54s  test_hypothesis_smt.py         (8 failed, 14 passed)
 2.37s  test_orchestrator_kill_scope.py
 2.31s  test_live_orders.py            (57 passed)
 2.09s  test_smt_hypothesis.py  · 1.97s test_smt_daily.py · 1.95s test_smt_trend.py
 1.80s  test_parquet_maintenance.py · 1.60s test_smt_humanize.py · 1.54s test_smt_backtest.py (58!)
 1.34s  test_regression_run_dirs.py · 1.20s test_smt_strategy.py (105!) · 1.08s test_position_monitor.py
 1.03s  test_backtester.py · 1.01s test_screener_prepare.py · 1.00s test_smt_regression.py
 0.98s  test_orchestrator_scheduler.py · 0.94s test_orchestrator_summarizer.py · 0.92s ×3 (smt_detect/relevance, check)
 0.91s  test_screener_script.py · 0.84s test_smt_relevance_rules.py · 0.82s test_parquet_tail.py
 0.79s  test_signal_smt.py · 0.78s ×2 (data_sources, smt_position_arch) · 0.77s ×2 (hypothesis_analysis, smt_signal_quality)
 0.75s  test_smt_limit_lifecycle.py (52!) · 0.70s test_screener.py · 0.69s ×3 (fill_executor, gap_fill→0.68, selector)
 0.66s  ×3 (bar_state, eqh_eql, sources) · 0.57s test_fold_auto_detect.py · 0.39s test_smt_state.py (34)
 0.26s  test_migrate_to_global_paths.py · 0.20s test_trade_cli.py (28) · 0.16s test_state_prefix.py
 0.13s  test_paths.py (12) · 0.12s ×2 (orchestrator_relay, rebuild_trades) · 0.10s ×2 (smt_fulfillment, orchestrator_process→0.09)
 0.09s  ×2 (parquet_validation_state) · 0.08s test_commit_note.py · 0.07s test_smt_invalidation.py

SUM ≈ 188.2s isolated / 60 files · 1384 passed, 27 failed (env), 6 skipped, 10 deselected
```

---

## Recommended order of action (once approved)

1. **Perf #1 + #2 + #4** (test-only mock/patch fixes) — ~108s, low risk, no coverage change.
2. **Deletes #1, #2** + **merges #3, #4** (Table 1) — ~22 tests dropped/relocated, no coverage loss; merge #4 also banks ~27s (Perf #3).
3. **Suite lever** (Perf #5): add `pytest-xdist`.
4. **Decide Table 1b** (v1 SMT extraction-then-delete; equities retire?) — needs your call; biggest count reduction but the riskiest.
5. **Collection hygiene** (Table 1d): rename root scripts / add `testpaths`.
