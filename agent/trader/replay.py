"""Trading-session replay: 09:20 -> 13:00 ET, 1s bars, no legacy engine.

**What is under test.** The thesis is served from a recording, so a replay exercises the
PLANNER and the EXECUTOR. The Analyzer is fixed input. Analyzer quality has its own
instrument -- the standalone `manual-l1-thesis/test_l1_thesis_manual.py` harness.

**Why the window always runs to its end.** Stopping at the DOL touch would make run
length data-dependent: change something that moves the touch time and two A/B variants
cover different windows. Instead the Executor records `plan_dead` with reason
`dol_reached` and its timestamp, and analysis truncates.

**Why the end moved from 11:00 to 13:00 (2026-09-09).** 13:00 is the position policy's
own hard-close horizon, and an 11:00 cut does not merely shorten the run -- it BIASES it.
A position still open at the cut used to emit nothing at all, so every fast stop-out
booked in full while every runner contributed zero, in the one direction that flatters a
tight stop. The companion fix is `OrderSim.mark_open`: whatever is open at the end is
MARKED, never called an exit. The fixed-window property is unchanged, only longer; the
cost is ~2.2x the bar loop.

**Why inline, not threaded.** Live runs the Analyzer on a thread so a 40-100 s model call
cannot stall the bar loop. Replay has no wall clock to protect, and a thread would make
the thesis land on an arbitrary bar depending on machine speed. So: inline for
determinism, plus an arrival gate to restore live's timing shape.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import paths                                                   # noqa: E402
from backtest_smt import run_backtest_v2                       # noqa: E402
from agent.trader.cached_backend import (                      # noqa: E402
    CachedThesisBackend, NetworkCallRefused)
from agent.trader.fixed_backend import (                       # noqa: E402
    FixedThesisBackend, OracleThesisError, validate_oracle_thesis)
from agent.trader.thesis_cache import ThesisCache              # noqa: E402
from agent.facts.htf_source import (                           # noqa: E402
    ARTIFACT as HTF_ARTIFACT, load_session_extremes, write_error_artifact)
# The window end has ONE source, and it is the Executor's: live has no replay window, so
# the Executor enforces the same 13:00 in bar time (plan 38 F11). Re-exported here under
# its old name because every caller reads `replay.WINDOW_END_ET`.
from agent.trader.executor import WINDOW_END_ET                # noqa: E402,F401

TZ = "America/New_York"
_ET = ZoneInfo(TZ)
WINDOW_START_ET = (9, 20)
#: The policy's hard-close horizon (plan 33 clause 10). A CONSTANT, not a data-dependent
#: stop -- see the module docstring. Overridable per run for the fidelity fixtures that
#: were calibrated against the old 11:00 cut. Defined in `agent/trader/executor.py`.
ARM_ENV = "ACT_TRADER_ARM_HHMM"

# Measured p50 of ten recorded 09:20 calls (range 16.3-103.9 s, driven almost entirely by
# retry count). Not load-bearing while arming stays at 09:20: no entry is legal until
# 09:30:30, so any latency under ~10 minutes is absorbed by the settle window entirely.
DEFAULT_ARRIVAL_LATENCY_SEC = 40.0


def replay_window_for(date: str, window_end=None):
    """`window_end` is an `(hour, minute)` pair overriding `WINDOW_END_ET`."""
    day = pd.Timestamp(date, tz=TZ).normalize()
    end = tuple(window_end) if window_end else WINDOW_END_ET
    return (day + pd.Timedelta(hours=WINDOW_START_ET[0], minutes=WINDOW_START_ET[1]),
            day + pd.Timedelta(hours=end[0], minutes=end[1]))


# The backend helpers moved to `agent/trader/cached_backend.py` so live
# (`automation/main.py`) and replay build the SAME recording backend. Re-exported under
# the old names: `build_replay_trader` looks `_real_backend` up here at call time, which
# is what the fidelity gate tests monkeypatch.
from agent.trader.cached_backend import (                      # noqa: E402
    backend_name as _backend_name, cached_thesis_backend,
    prompt_parts as _prompt_parts_shared, real_thesis_backend as _real_backend)


def _prompt_parts():
    """Shared with live — see `agent.trader.cached_backend.prompt_parts`."""
    return _prompt_parts_shared()


def build_replay_trader(date, run_dir, *, allow_calls, arrival_latency_sec,
                        arm_hhmm=None, thesis=None, gate_arrival=True,
                        operator_control=None):
    from agent.trader.graft import TraderGraft

    # Plan 41: reproduce a session's OPERATOR OVERRIDES. The graft drains its control
    # file from its own state dir, so a recorded file is COPIED into the run folder
    # before construction -- never read in place, because a replay must not be able to
    # write anything into a live session's directory.
    if operator_control:
        try:
            import shutil
            from agent.trader.operator_control import CONTROL_FILE
            os.makedirs(run_dir, exist_ok=True)
            shutil.copyfile(str(operator_control), os.path.join(run_dir, CONTROL_FILE))
        except Exception as exc:
            raise RuntimeError(f"could not stage the operator control file "
                               f"{operator_control}: {type(exc).__name__}: {exc}")

    if thesis is not None:
        # The oracle path never constructs a cache: the recorder's content key hashes
        # the prompt, so an injected entry is unauthorable and self-invalidating.
        backend = FixedThesisBackend(thesis)
    else:
        inner = _real_backend() if allow_calls else None
        backend = cached_thesis_backend(date, inner=inner, allow_calls=allow_calls)
    if arm_hhmm:
        # Scoped and restored by `run_replay` -- see `_arm_env`.
        os.environ[ARM_ENV] = arm_hhmm

    # A fixed backend returns instantly, so the gate is the ONLY thing keeping the
    # thesis from being visible at the arm instant. Explicit, not inherited.
    latency = arrival_latency_sec if gate_arrival else 0.0

    # Plan 40: the unnested weekly/monthly extremes, from the SAME 1m file family live
    # reads (the per-contract main era for this date), as of the session open. A failure
    # is recorded in the run folder and the run proceeds without them -- exactly the
    # pre-plan-40 selection -- rather than turning an unrelated replay into a crash.
    htf = None
    try:
        htf = load_session_extremes(date, source="replay")
    except Exception as exc:
        try:
            write_error_artifact(run_dir, date, "replay", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass

    # `arrival_latency_sec` goes through the constructor rather than by overwriting
    # `graft._analyzer` afterwards (the plan's shape), which built and threw away a whole
    # Analyzer -- including its state-file load -- on every replayed date.
    return TraderGraft(run_dir, backend, threaded=False, arrival_latency_sec=latency,
                       htf_extremes=htf)


class _arm_env:
    """Restore `ACT_TRADER_ARM_HHMM` after a run.

    The factory has to SET it (the arm minute is read inside the bar loop, at call time),
    but leaving it set would silently re-arm every later run in the same process -- and
    in a pytest session that means every later test. Divergence from the plan, which set
    it and never restored it.
    """

    def __init__(self, arm_hhmm) -> None:
        self._active = bool(arm_hhmm)
        self._prev = None

    def __enter__(self):
        if self._active:
            self._prev = os.environ.get(ARM_ENV)
        return self

    def __exit__(self, *exc):
        if not self._active:
            return False
        if self._prev is None:
            os.environ.pop(ARM_ENV, None)
        else:
            os.environ[ARM_ENV] = self._prev
        return False


#: Artifacts whose presence means another run already owns this directory.
#: `thesis_state.json` is the dangerous one -- see `_refuse_a_dirty_run_dir`.
_RUN_DIR_ARTIFACTS = ("trader_decisions.jsonl", "thesis_state.json", "plans.json",
                      HTF_ARTIFACT)


#: How many one-second bumps `_fresh_started` will try before giving up. A collision
#: needs two runs of the same SESSION date to start at the same TH second, so needing
#: even two bumps is already extraordinary; 120 is a bound, not an expectation.
_MAX_STAMP_BUMPS = 120


def _fresh_started(date: str, started: datetime.datetime) -> datetime.datetime:
    """A run-start instant whose run directory is not already occupied.

    THE COLLISION. `paths.regression_run_dir` names a run
    `<regression>/sessions/<SESSION date>/<HH-MM-SS TH>` — the session date, never the
    calendar day it was run on. So two replays of one session date, started at the same
    wall-clock second on different days, are handed the SAME directory. That is not
    hypothetical: a 2026-08-18 replay run on 2026-09-09 at 12:54:54 landed in a folder
    written on 2026-08-29, and both consequences were silent (see
    `_refuse_a_dirty_run_dir` for what they were).

    The naming scheme itself is shared with the legacy regression and pinned by
    `tests/test_paths.py` and `tests/test_regression_run_dirs.py`, so it is not this
    module's to change. What IS available: `run_backtest_v2` accepts `started` and
    derives the stamp from it. Replay simply picks one that is free.

    Probing calls `regression_run_dir` rather than restating the TH stamp arithmetic
    here, where it could drift from the function that actually names the run. That
    creates the directory as a side effect, which is harmless: a candidate that is
    already OCCUPIED existed before the probe, and the first FREE candidate is the one
    the run then uses — so no orphan directories accumulate.
    """
    for _ in range(_MAX_STAMP_BUMPS):
        candidate = paths.regression_run_dir(str(date), started)
        if not _run_dir_artifacts(str(candidate)):
            return started
        started = started + datetime.timedelta(seconds=1)
    raise RuntimeError(
        f"{date}: could not find a free run directory in {_MAX_STAMP_BUMPS} seconds "
        f"from {started}; {paths.regression_sessions_dir() / str(date)} is saturated")


def _run_dir_artifacts(run_dir: str) -> list:
    return [n for n in _RUN_DIR_ARTIFACTS
            if os.path.exists(os.path.join(run_dir, n))]


def _refuse_a_dirty_run_dir(run_dir: str) -> None:
    """Refuse to run into a directory another run has already written.

    FOUND THE HARD WAY, 2026-09-09. Run directories are named
    `regression/sessions/<session date>/<HH-MM-SS>` with NO day component, so two runs of
    the same session date started at the same wall-clock second on different calendar
    days land in the same folder. A seeded 2026-08-18 replay did exactly that, colliding
    with a run from 2026-08-29, and the consequences were both silent:

      * `DecisionRecorder` APPENDS, so the older run's four records were prepended to
        this run's stream and the session read as two entries instead of one;
      * worse, `Analyzer._load` reads `thesis_state.json` from the state dir, and
        `maybe_run` early-returns when `armed_date` matches the bar date. The stale file
        was that session's thesis, so the Analyzer NEVER CALLED THE MODEL. A `--seed` run
        produced no cache entry, inherited a previous run's (oracle) thesis, and reported
        a +229.75 winner. The real 09:20 call for that date returns NEUTRAL -- a dark day
        with no trade at all.

    That is an unfalsifiable run: the same failure `NetworkCallRefused` exists to stop,
    arriving through a different door. Raising here is the guard; renaming the directory
    scheme would be the cure, but `paths.regression_run_dir` is shared with the legacy
    regression and its locked baselines, so it is not this module's to change.
    """
    present = _run_dir_artifacts(run_dir)
    if present:
        raise RuntimeError(
            f"run directory {run_dir} already holds {', '.join(present)} from an earlier "
            "run. Run-dir names carry no calendar day, so same-second starts on "
            "different days collide; appending to it would splice two runs' artifacts "
            "and could silently reuse the older run's thesis. Move or delete it, or "
            "start the run a second later.")


def run_replay(dates, *, allow_calls=False,
               arrival_latency_sec=DEFAULT_ARRIVAL_LATENCY_SEC, arm_hhmm=None,
               thesis=None, gate_arrival=True, window_end=None,
               operator_control=None):
    """Replay each date's trading session.

    Returns `{date: {"run_dir", "cache", "last_bar", "legacy"}}`:

      run_dir   the per-date output folder holding the EXECUTOR's artifacts
                (`trader_decisions.jsonl`, `plans.json`, `thesis_state.json`).
      cache     `{hits, misses, calls, refusals}` for the thesis backend.
      last_bar  the floored minute of the last bar the graft was handed. The only
                in-memory record of how far the loop actually got: every on-disk
                artifact stops when the Executor stops writing, which is earlier.
      mark      the `mark` event booking a position still open at the window end, or
                None. A MARK IS NOT AN EXIT (`OrderSim.mark_open`); it exists so a
                runner is not silently worth zero.
      legacy    `run_backtest_v2`'s raw return `{trades, events, metrics, stats}`.
                EXPECTED TO BE EMPTY of trades -- see below.

    **The legacy engine does not run.** `trader_only=True` makes `SessionPipeline`
    return immediately after the trader hook, so the whole legacy path -- daily
    recompute, trend, SMT detection, liquidity update, hypothesis, strategy, bar_state --
    never executes. A replay exercises the Analyzer/Planner/Executor and nothing else.

    The key stays named `legacy` (not `result`) because whatever it carries is the LEGACY
    engine's shape, never the Executor's: a caller reading `["trades"]` expecting entry
    intents would be reading the wrong brain. The Executor's output is on disk in
    `run_dir/trader_decisions.jsonl`. Nothing reaches the legacy event stream either way
    (`write_events=False`) and no baseline is ever written.

    `run_backtest_v2` does NOT report the run directory. The factory is handed that
    directory, so we capture it there rather than trying to re-derive it.

    **A refused call fails the run, loudly.** `Analyzer._call` swallows every exception
    (fail dark, never stall the bar loop) and `TraderGraft.on_bar` swallows again, so a
    `NetworkCallRefused` raised inside the bar loop reaches nobody: the replay would
    finish "successfully" as a dark day, and "a warm-cache replay makes ZERO model
    calls" would be unfalsifiable. The counters on the backend survive the swallow, so
    they are checked here, after the run. Divergence from the plan, which assumed the
    exception would propagate out of `run_replay`.

    `thesis=` injects a SYNTHETIC (oracle) thesis instead of reading a recording -- see
    `agent/trader/fixed_backend.py`. It is refused up front rather than at the arm bar,
    because `Analyzer._call` and `TraderGraft.on_bar` both swallow, so a malformed oracle
    would otherwise finish the run silently as a dark day. `gate_arrival=False` makes the
    injected thesis visible at the arm instant.
    """
    if thesis is not None and allow_calls:
        raise ValueError("thesis= (oracle) and allow_calls= (seed) are mutually "
                         "exclusive: an oracle run must not reach the model")
    if thesis is not None:
        errs = validate_oracle_thesis(thesis)
        if errs:
            raise OracleThesisError("; ".join(errs))

    out = {}
    for date in dates:
        window = replay_window_for(date, window_end)
        # Chosen HERE, not left to `run_backtest_v2`'s own `now`, so the run lands in a
        # directory no earlier run occupies. Outside the bar loop, where a wall clock is
        # legitimate (CLAUDE.md's rule bans one INSIDE it).
        started = _fresh_started(date, datetime.datetime.now(_ET))
        captured = {}

        def _factory(d, run_dir, _c=captured):
            _c["run_dir"] = str(run_dir)
            _refuse_a_dirty_run_dir(str(run_dir))
            graft = build_replay_trader(d, run_dir, allow_calls=allow_calls,
                                        arrival_latency_sec=arrival_latency_sec,
                                        arm_hhmm=arm_hhmm, thesis=thesis,
                                        gate_arrival=gate_arrival,
                                        operator_control=operator_control)
            _c["backend"] = graft._analyzer._backend
            _c["graft"] = graft
            return graft

        with _arm_env(arm_hhmm):
            legacy = run_backtest_v2(
                date, date, mode="1s", write_events=False, started=started,
                trader_factory=_factory, replay_window=window,
                trader_only=True)

        run_dir = captured.get("run_dir")
        # The factory is called once per date that actually has bars. If it was never
        # called, `run_backtest_v2` skipped this date entirely -- no 1s coverage in the
        # window, or a non-trading day. Returning an empty-but-successful entry would put
        # a "the replay ran and found nothing" result in front of the caller, which is
        # the silent-empty failure shape this whole cycle exists to stop repeating.
        if run_dir is None:
            w0, w1 = window
            raise RuntimeError(
                f"{date}: no 1s bars in the replay window {w0} -> {w1}; the date was "
                "skipped, not replayed")

        # `run_backtest_v2` degrades a factory failure to no-trader and drops a
        # breadcrumb rather than aborting. Silently returning an empty run would make a
        # broken graft look like a dark day.
        if run_dir and os.path.exists(os.path.join(run_dir, "trader_init_error.txt")):
            with open(os.path.join(run_dir, "trader_init_error.txt"),
                      encoding="utf-8") as fh:
                raise RuntimeError(f"{date}: replay trader failed to build\n{fh.read()}")

        backend = captured.get("backend")
        if thesis is not None:
            # An oracle run has no cache and reaches no model, so every counter is
            # zero BY CONSTRUCTION. Read explicitly rather than off the backend:
            # `FixedThesisBackend.calls` counts how many times the ORACLE was served,
            # which is a different quantity from `calls` here (MODEL calls) and would
            # report 1 for a run that never touched the network.
            stats = {"hits": 0, "misses": 0, "calls": 0, "refusals": 0}
        else:
            stats = {"hits": getattr(backend, "hits", 0),
                     "misses": getattr(backend, "misses", 0),
                     "calls": getattr(backend, "calls", 0),
                     "refusals": getattr(backend, "refusals", 0)}
        if stats["refusals"]:
            raise NetworkCallRefused(
                f"{date}: {stats['refusals']} thesis cache miss(es) with calls "
                f"disallowed (last key {getattr(backend, 'last_key', None)}) -- "
                "seed the cache with `--seed` first")
        graft = captured.get("graft")

        # Whatever is still open when the WINDOW ends, booked at the last bar the
        # Executor saw. After the loop, never inside it, and driven from here rather
        # than from the Executor because the window end is the RUNNER's knowledge.
        mark = graft.mark_open_position() if graft is not None else None

        # Per (class, ticker) coverage as of the last bar. `ensure_coverage` collapses
        # every class and ticker into ONE value, which is why the 08-13 units bug was
        # invisible for a whole cycle; this is the un-collapsed view, on disk.
        if thesis is not None:
            # NOT best-effort: for a §10-style oracle study this file IS the record of
            # what was run. A silently dropped provenance stamp leaves a run dir whose
            # thesis cannot be recovered, in the one module that otherwise raises rather
            # than hand back a silent-empty result.
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "thesis_source.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"thesis_source": "injected", "thesis": thesis,
                           "gate_arrival": bool(gate_arrival)}, fh,
                          default=str, indent=2)

        cov = graft.coverage_report() if graft is not None else {}
        try:
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "coverage_report.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(cov, fh, default=str, indent=2)
        except Exception:
            pass

        out[date] = {"run_dir": run_dir, "legacy": legacy, "cache": stats,
                     "coverage": cov, "mark": mark,
                     # Plan 40: False when the HTF list failed to load (the reason is
                     # in the run folder's htf_extremes.json) -- an A/B must not score
                     # such a date as "flag on, no change".
                     "htf_loaded": bool(graft is not None and graft.htf_loaded()),
                     "last_bar": (graft.last_bar_minute()
                                  if graft is not None else None)}
    return out
