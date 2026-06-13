# GIL-27 — Backtest perf: profiled findings & ranked recommendations

**Status:** Investigation complete **and Wave 1 IMPLEMENTED + verified** (UNSTAGED). See
"Wave 1 — implemented & verified" below for the measured 3-date byte-identical A/B and the
~3.1× speedup. Throwaway artifacts (`_perf_*.py`, `pyspy_1s.folded`) are also UNSTAGED.

## Wave 1 — implemented & verified (2026-06-14, UNSTAGED)

Implemented **#1 + #2 + #4**, one at a time, each verified by a 1s regression diff against the
locked baseline (`trades_match` AND `events_match` must be True). Original-code baselines for
2026-05-20/-22 were locked *before* any edit; 2026-05-18's was already locked.

**Final clean 3-date verification (no contention), full Wave-1 stack:**

| date | baseline | optimized | speedup | trades | diff |
|---|---|---|---|---|---|
| 2026-05-18 | 895.1 s | **289.1 s** | **−67.7%** | 29 / +$2605 | **PASS** |
| 2026-05-20 | (≈895 s) | **296.2 s** | ~−67% | 32 / −$174 | **PASS** |
| 2026-05-22 | (≈895 s) | **274.3 s** | ~−69% | 21 / +$1015 | **PASS** |

**~286 s/date avg vs ~895 s → ~3.1× faster (−68%)**, well beyond the projected ~480 s. (#2's
Path caching turned out bigger than estimated: with the 5 hot state Paths cached, `paths.state_dir()`
/`_ensure` aren't called per-load at all — it removes #1's residual `str()` too.)

Changed files (production): `paths.py` (#1), `smt_state.py` (#2), `session_pipeline.py` (#4).
Unit tests: targeted suites (`test_paths`/`test_smt_state`/`test_state_prefix`/`test_bar_state`/
`test_session_pipeline`) = **129 passed**, 1 pre-existing env failure (`test_bar_state_written_after_1m_bar`
asserts `date.today()` but the file is correctly written under the ET session date — fails only in the
local-vs-ET midnight gap; unrelated to these changes, confirmed by repro).

---

### Original recommendation (pre-implementation) below.

## TL;DR

- **1s regression = ~895 s/day** (23k bars) vs **1m = 30 s/day** (1.4k bars). The slow path is 1s.
- It is **not an algorithmic problem.** ~**40% of runtime is a redundant `mkdir` syscall** fired on
  every single state-file path resolution (dozens/second). The headline fix is a **5-line,
  behavior-neutral memoization** — **A/B-proven: 895.1 s → 556.4 s (−37.8%) with byte-identical
  trades AND events (`trades_match=True, events_match=True`).**
- Recommended first wave (near-zero risk): **#1 + #2 + #4** → projected **895 s → ~450–490 s** with
  byte-identical trades (#1 alone already gets to 556 s). The remaining big lever (#5, per-second
  DataFrame work) is high-risk/high-effort and should be a separate, A/B-gated effort.

## Method

- Fixture: **2026-05-18, 1s mode** (`python regression.py --dates 2026-05-18 --mode 1s --no-plot`),
  29 trades / +$2605. Baseline locked & PASS.
- Profiler: **py-spy** sampling (`--rate 250 --format raw`), **226,799 samples** over the real
  912 s run — near-zero distortion, unlike cProfile's ~3× deterministic overhead on a 15-min run.
  (`line_profiler`/`py-spy` were not installed; py-spy installed for this task.)
- Aggregation: `_perf_fold.py` → self-time (leaf) and total-time (any-frame) rankings.

## Where the time actually goes (self-time, % of 226,799 samples)

| rank | function | self% | what it is |
|---|---|---|---|
| 1 | `mkdir` (pathlib) | **30.6%** | `paths._ensure()` mkdir on every `state_dir()` resolution |
| 2 | `_fast_copy` (smt_state) | **10.1%** | deep-copy of state dict on every `load_*` (cache bypassed in-memory) |
| 3 | `stat` (pathlib) | 4.9% | path existence checks behind the same path machinery |
| 4 | `_extend_instrument_fvg_frames` | 3.4% | per-bar FVG frame extension |
| 5 | `on_1m_bar` | 3.0% | the per-bar dispatcher itself |
| — | `_cmp_method`/`isclose`/`within_tol` | ~3.7% | **`Timestamp.floor()`** internals (pytz + numpy) |
| — | `_parse_path`/`splitroot`/`join`/pathlib `__str__`/`__init__` | ~3.6% | Path-object construction |
| — | pandas frame ops (`iget`/`__getitem__`/`__finalize__`/`__init__`/`_amax`/`_any`) | ~5% | per-second DataFrame rebuild + masks |

**Path machinery (mkdir + stat + Path construction) ≈ 40% of self-time.** Everything the grounding
pass flagged as "hot" (`load_daily` 13.5% total, `load_hypothesis` 12.2%, `load_position` 12.1%,
`_update_instrument_liquidities` 36% total) is hot **only because the mkdir is nested inside it** —
remove the mkdir and those collapse.

## Ranked recommendations

| # | Optimization | Hotspot (measured) | Proposed change | Expected speedup | Behavior risk | Effort |
|---|---|---|---|---|---|---|
| **1** | **Memoize `paths._ensure` (stop the per-call mkdir)** | `mkdir` 30.6% self / path machinery ~36% total | Cache ensured dirs in a module-level `set`; `_ensure` does the mkdir only on first sight of a path. Dir is still created on first use. | **−37.8% A/B-measured (895.1→556.4 s)** | **Negligible** — A/B = byte-identical (`trades_match=events_match=True`). | **Trivial** (~5–10 lines) |
| **2** | **Cache resolved state-file `Path` objects** | `_parse_path`/`splitroot`/`join`/pathlib ctor ~3.6% self | Cache the 5 state Paths keyed by current `_STATE_DIR`; invalidate in `set_state_dir`. Stops rebuilding `state_dir()/"daily.json"` every call. | **~3–5%** | **Negligible** — deterministic paths, behavior-neutral. | Low |
| **3** | **Cut redundant per-bar `load_*` deep-copies** | `_fast_copy` 10.1% self | (3a) De-duplicate the many `load_hypothesis/position/daily` calls within one bar — load once, thread the dict down. (Do **not** share mutable references across save boundaries.) | **~5–8%** | **Medium** — mutation/aliasing hazard; **A/B required.** | Medium–High |
| **4** | **Hoist/cheapen `Timestamp.floor()`** | `isclose`+`within_tol`+`localize` ~5% self | `now` is constant within a bar but floored repeatedly (`:698,:1117,:1589×2,:2004`). Compute each floor once per bar and reuse; optionally integer-ns floor for the tz-stable session. | **~3–5%** | **Low** (cache same value) / **Medium** if switching to integer math — **A/B if so.** | Medium |
| **5** | **Kill per-second pandas on `today_mnq`** | `concat` 5.0% total (`:1492`), frame `__getitem__` 7.8%, per-sec `DataFrame()` (`backtest_smt.py:1430`) | Incrementally track active-session max/min instead of `concat([sliver18, today_mnq])`+max each second; and/or stop rebuilding two DataFrames/sec by passing arrays/views into `on_1m_bar`. | **~8–12%** | **High** — changes the core per-bar data contract; **A/B mandatory, broad surface.** | High |
| 6 | Skip per-second `save_bar_state` write in backtest | `_write_bar_state` 7% total (mostly nested mkdir) | Values change only per-5m (already cached); the per-second write is near-free **after #1**. Re-measure before bothering. | <2% after #1 | Low–Medium (confirm nothing reads it per-second) | Low |

### Suggested adoption order
1. **Wave 1 — do now, near-zero risk:** #1 (already A/B-proven −37.8%), then #2 + #4(cache-only variant).
   Projected **895 s → ~450–490 s** (~48% faster). Each is behavior-neutral; a single confirming 1s A/B
   (trades+events diff = PASS) closes the hard constraint.
2. **Wave 2 — separate, A/B-gated:** #3 (state-copy reduction) and #5 (DataFrame rebuild). These touch
   correctness-sensitive paths; implement one at a time, each with its own 1s A/B on 2026-05-18/-20/-22.

## Behavior-preservation note (hard constraint)

The regression output is the correctness baseline. **#1 and #2 are output-neutral by construction**
(they remove redundant filesystem/allocation work, not logic) and #1 is **already A/B-verified PASS**
below. #3/#4(integer)/#5 can change results if done carelessly → each is flagged **"needs A/B before
adopting"** and must show `events=PASS trades=PASS` on the fixture dates before merge.

## Before/after evidence — Candidate #1 (A/B, throwaway monkeypatch, no prod edit)

_Filled in from `_perf_ab_ensure.py` (memoized `_ensure`, real regression vs locked baseline):_

| run | wall-clock (1s, 2026-05-18) | trades | events/trades diff |
|---|---|---|---|
| baseline | 895.1 s | 29 / +$2605 | PASS (locked) |
| #1 memoized `_ensure` | **556.4 s (−37.8%)** | 29 / +$2605 | **`trades_match=True, events_match=True`** |

Only **6 distinct directories** were ever created across the whole 23k-bar run — i.e. ~1.5M `mkdir`
syscalls were redundant. The monkeypatch lived entirely in throwaway `_perf_ab_ensure.py`; `paths.py`
was not edited.

> Micro-benchmark (this box): `mkdir(parents=True, exist_ok=True)` = **184 µs/call** vs **171 ns**
> memoized → ~1000×. At the profiled call frequency that is ≈ **274 s** of the 895 s.
