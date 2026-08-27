"""Trading-session replay: 09:20 -> 11:00 ET, 1s bars, no legacy engine.

**What is under test.** The thesis is served from a recording, so a replay exercises the
PLANNER and the EXECUTOR. The Analyzer is fixed input. Analyzer quality has its own
instrument -- the standalone `manual-l1-thesis/test_l1_thesis_manual.py` harness.

**Why the window always runs to 11:00.** Stopping at the DOL touch would make run length
data-dependent: change something that moves the touch time and two A/B variants cover
different windows. Instead the Executor records `plan_dead` with reason `dol_reached` and
its timestamp, and analysis truncates.

**Why inline, not threaded.** Live runs the Analyzer on a thread so a 40-100 s model call
cannot stall the bar loop. Replay has no wall clock to protect, and a thread would make
the thesis land on an arbitrary bar depending on machine speed. So: inline for
determinism, plus an arrival gate to restore live's timing shape.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from backtest_smt import run_backtest_v2                       # noqa: E402
from agent.trader.cached_backend import (                      # noqa: E402
    CachedThesisBackend, NetworkCallRefused)
from agent.trader.thesis_cache import ThesisCache              # noqa: E402

TZ = "America/New_York"
WINDOW_START_ET = (9, 20)
WINDOW_END_ET = (11, 0)
ARM_ENV = "ACT_TRADER_ARM_HHMM"

# Measured p50 of ten recorded 09:20 calls (range 16.3-103.9 s, driven almost entirely by
# retry count). Not load-bearing while arming stays at 09:20: no entry is legal until
# 09:30:30, so any latency under ~10 minutes is absorbed by the settle window entirely.
DEFAULT_ARRIVAL_LATENCY_SEC = 40.0


def replay_window_for(date: str):
    day = pd.Timestamp(date, tz=TZ).normalize()
    return (day + pd.Timedelta(hours=WINDOW_START_ET[0], minutes=WINDOW_START_ET[1]),
            day + pd.Timedelta(hours=WINDOW_END_ET[0], minutes=WINDOW_END_ET[1]))


def _backend_name() -> str:
    return os.environ.get("ACT_TRADER_BACKEND", "openrouter")


def _real_backend():
    """The live thesis backend, imported lazily so a warm-cache replay never touches
    `run_agent` (and therefore never needs an API key).

    Constructed exactly the way `automation/main.py` builds the LIVE trader's backend
    (same env vars, same defaults), so a seeding run records what live would have called.
    """
    from agent.run_agent import make_backend
    from agent.trader.analyzer import thesis_via_decide_thesis
    return thesis_via_decide_thesis(
        make_backend(_backend_name(), os.environ.get("ACT_TRADER_MODEL") or None))


def _prompt_parts():
    """(system_prompt_fn, task_prompt, schema_fn, model_id) for the cache key.

    `build_system_prompt` (the concatenated KB) and `_TASK_THESIS` are the two prompt
    halves `decide_thesis` assembles, so hashing them makes a doc edit or a task-prompt
    edit invalidate every recording.

    `model_id` is `<backend>:<resolved model>` rather than the whole `DEFAULT_MODELS`
    dict: the dict cannot distinguish two backends that happen to share a default, and
    the resolved pair is what actually determines the answer.

    `schema_fn` returns `{}` DELIBERATELY. The real schema is built inside `decide_thesis`
    from `facts["levels"]`, `facts["fvg_zones"]` and the DOL menu -- all of which are
    already rendered into `facts_text`, which IS keyed. Rebuilding the schema here would
    duplicate `decide_thesis`'s derivation in a second place, which is exactly the
    drift this design set out to avoid. See the gap noted in the execution report.
    """
    from agent import run_agent as ra
    backend = _backend_name()
    # Direct attribute access, NOT getattr-with-a-default: a `getattr(ra, "_TASK_THESIS",
    # "")` would silently drop the task prompt out of the key if it were ever renamed,
    # quietly weakening every recording's invalidation instead of failing.
    model = os.environ.get("ACT_TRADER_MODEL") or ra.DEFAULT_MODELS.get(backend, "")
    return (ra.build_system_prompt, ra._TASK_THESIS,
            (lambda facts: {}), "%s:%s" % (backend, model))


def build_replay_trader(date, run_dir, *, allow_calls, arrival_latency_sec,
                        arm_hhmm=None):
    from agent.trader.graft import TraderGraft

    sys_fn, task, schema_fn, model_id = _prompt_parts()
    inner = _real_backend() if allow_calls else None
    backend = CachedThesisBackend(
        inner, cache=ThesisCache(), model_id=model_id,
        system_prompt_fn=sys_fn, task_prompt=task, schema_fn=schema_fn,
        allow_calls=allow_calls, boundary_hint=str(date),
    )
    if arm_hhmm:
        # Scoped and restored by `run_replay` -- see `_arm_env`.
        os.environ[ARM_ENV] = arm_hhmm

    # `arrival_latency_sec` goes through the constructor rather than by overwriting
    # `graft._analyzer` afterwards (the plan's shape), which built and threw away a whole
    # Analyzer -- including its state-file load -- on every replayed date.
    return TraderGraft(run_dir, backend, threaded=False,
                       arrival_latency_sec=arrival_latency_sec)


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


def run_replay(dates, *, allow_calls=False,
               arrival_latency_sec=DEFAULT_ARRIVAL_LATENCY_SEC, arm_hhmm=None):
    """Replay each date's trading session.

    Returns `{date: {"run_dir", "cache", "last_bar", "legacy"}}`:

      run_dir   the per-date output folder holding the EXECUTOR's artifacts
                (`trader_decisions.jsonl`, `plans.json`, `thesis_state.json`).
      cache     `{hits, misses, calls, refusals}` for the thesis backend.
      last_bar  the floored minute of the last bar the graft was handed. The only
                in-memory record of how far the loop actually got: every on-disk
                artifact stops when the Executor stops writing, which is earlier.
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
    """
    out = {}
    for date in dates:
        window = replay_window_for(date)
        captured = {}

        def _factory(d, run_dir, _c=captured):
            _c["run_dir"] = str(run_dir)
            graft = build_replay_trader(d, run_dir, allow_calls=allow_calls,
                                        arrival_latency_sec=arrival_latency_sec,
                                        arm_hhmm=arm_hhmm)
            _c["backend"] = graft._analyzer._backend
            _c["graft"] = graft
            return graft

        with _arm_env(arm_hhmm):
            legacy = run_backtest_v2(
                date, date, mode="1s", write_events=False,
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
        out[date] = {"run_dir": run_dir, "legacy": legacy, "cache": stats,
                     "last_bar": (graft.last_bar_minute()
                                  if graft is not None else None)}
    return out
