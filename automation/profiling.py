"""Memory profiling for a live run, enabled only by `trade.py start --profile`.

The flag travels as a command-line argument: trade.py -> `orchestrator.main --profile` ->
`automation.main --profile`. Each process then:

- samples RSS / commit / CPU every 30s, on the wall-clock :00/:30 marks, all session;
- traces allocations (tracemalloc) from process start and snapshots twice:
    BASELINE at 09:15:30 ET (before the 09:20 arm), tracing continues;
    FINAL at 13:05:30 ET (after the 13:00 window-end close) OR when `trade.py terminate`
    / `start` asks for it first -- whichever comes first, exactly once -- with the growth
    per source line since the baseline. Tracing then stops.

COST, measured on a 1s replay: tracing roughly doubles CPU time (no freezes), and a
snapshot holds the GIL ~5s. So no snapshot is ever taken inside the trading window
except the one a terminate asks for. A process that starts inside 09:15-13:05 ET never
traces (samples only); tracing without a baseline by 09:19 stops.

Terminate handshake: trade.py writes `profile_snapshot.req` in the worktree root and
waits for `profile_snapshot_<label>.done` from each profiled process before stopping it.
The orchestrator ends automation.main with TerminateProcess (no atexit/finally runs), so
this handshake -- not an exit hook -- is what captures the final state.

It can never raise into trading: everything runs in one daemon thread, every failure
disables the profiler silently, and output goes only to `<out_dir>/profile/` plus the two
handshake files. The sampler reads the wall clock in its own thread, outside any bar loop.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

SAMPLE_SEC = 30
POLL_SEC = 1.0
TOP_N = 30
BASELINE_AT = datetime.time(9, 15, 30)
BASELINE_DEADLINE = datetime.time(9, 19)
FINAL_AT = datetime.time(13, 5, 30)
NO_TRACE_START = datetime.time(9, 15)      # the arm (09:20) through window end (13:00),
NO_TRACE_END = datetime.time(13, 5)        # with a margin either side

ROOT = Path(__file__).resolve().parent.parent
REQUEST_FILE = ROOT / "profile_snapshot.req"


def done_file(label: str) -> Path:
    return ROOT / f"profile_snapshot_{label}.done"


_ET = ZoneInfo("America/New_York")
_started = False
_stop = threading.Event()


def _clock() -> datetime.datetime:
    return datetime.datetime.now(_ET)


def _in_no_trace_window(t: datetime.time) -> bool:
    return NO_TRACE_START <= t < NO_TRACE_END


def start(label: str, out_dir) -> "Path | None":
    """Start profiling this process. Returns the output folder, or None when it was
    already started or anything about starting failed."""
    global _started
    if _started:
        return None
    try:
        import psutil
        folder = Path(out_dir) / "profile"
        folder.mkdir(parents=True, exist_ok=True)
        prof = _Profiler(label, folder, psutil.Process())
        if not _in_no_trace_window(_clock().time()):
            import tracemalloc
            tracemalloc.start(1)
            prof.phase = "pre"
        else:
            prof.note("not traced: started inside the 09:15-13:05 ET window")
        _started = True
        threading.Thread(target=prof.run, name=f"act-profile-{label}", daemon=True).start()
        return folder
    except Exception:
        return None


def _stamp(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _to_next_mark(now: datetime.datetime) -> float:
    """Seconds to the next wall-clock :00/:30 mark (for SAMPLE_SEC=30)."""
    s = now.second + now.microsecond / 1e6
    return SAMPLE_SEC - (s % SAMPLE_SEC) or SAMPLE_SEC


class _Profiler:
    def __init__(self, label: str, folder: Path, proc):
        self.label, self.folder, self.proc = label, folder, proc
        self.stem = f"{label}_{os.getpid()}"
        self.phase = "off"            # off | pre | window | done
        self.baseline: "dict | None" = None
        self.started_at = time.time()

    # -- the loop ----------------------------------------------------------------- #

    def run(self) -> None:
        try:
            import psutil
            mem_path = self.folder / f"mem_{self.stem}.tsv"
            new = not mem_path.exists()
            with open(mem_path, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("time_et\trss_mb\tprivate_mb\tcpu_pct\tthreads\tsys_avail_mb\n")
                self.proc.cpu_percent(None)
                next_sample = 0.0
                while not _stop.is_set():
                    if self._requested():
                        self.on_request()
                    if time.monotonic() >= next_sample:
                        now = _clock()
                        mi = self.proc.memory_info()
                        fh.write(f"{_stamp(now)}\t{mi.rss / 2**20:.0f}\t"
                                 f"{getattr(mi, 'private', 0) / 2**20:.0f}\t"
                                 f"{self.proc.cpu_percent(None):.0f}\t"
                                 f"{self.proc.num_threads()}\t"
                                 f"{psutil.virtual_memory().available / 2**20:.0f}\n")
                        fh.flush()
                        self.on_sample(now.time())
                        next_sample = time.monotonic() + _to_next_mark(_clock())
                    _stop.wait(min(POLL_SEC, max(0.0, next_sample - time.monotonic())))
        except Exception:
            return
        finally:
            _stop_tracing()

    def _requested(self) -> bool:
        """A request file written after this process started (a stale one is ignored)."""
        try:
            return REQUEST_FILE.stat().st_mtime >= self.started_at
        except OSError:
            return False

    # -- the schedule --------------------------------------------------------------- #

    def on_sample(self, t: datetime.time) -> None:
        if self.phase == "pre":
            if BASELINE_AT <= t < BASELINE_DEADLINE:
                self.baseline = self._snapshot("0915", "baseline")
                self.phase = "window" if self.baseline is not None else self._finish()
            elif BASELINE_DEADLINE <= t < NO_TRACE_END:
                self.note("no snapshot: no baseline by the 09:19 deadline")
                self._finish()
        elif self.phase == "window" and t >= FINAL_AT:
            self._final("scheduled 13:05:30")

    def on_request(self) -> None:
        try:
            if self.phase == "window":
                self._final("terminate requested")
            elif self.phase == "pre":
                self._snapshot("final", "terminate requested (before the baseline)")
                self._finish()
        finally:
            try:
                done_file(self.label).write_text(_stamp(_clock()), encoding="utf-8")
            except OSError:
                pass

    def _final(self, why: str) -> None:
        try:
            self._snapshot("final", why, baseline=self.baseline)
        finally:
            self._finish()

    def _finish(self) -> str:
        self.phase = "done"
        _stop_tracing()
        return "done"

    # -- output ----------------------------------------------------------------------- #

    def note(self, text: str) -> None:
        try:
            with open(self.folder / f"tracemalloc_{self.stem}_note.txt", "a",
                      encoding="utf-8") as fh:
                fh.write(f"# {_stamp(_clock())} ET  {text}\n")
        except OSError:
            pass

    def _snapshot(self, tag: str, why: str, baseline: "dict | None" = None) -> "dict | None":
        """Write the top allocation sites (and growth vs `baseline`); return this
        snapshot's per-line totals, or None when tracing is off or it failed."""
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                return None
            t0 = time.perf_counter()
            snap = tracemalloc.take_snapshot().filter_traces(
                (tracemalloc.Filter(False, tracemalloc.__file__),
                 tracemalloc.Filter(False, __file__)))
            cur, peak = tracemalloc.get_traced_memory()
            stats = snap.statistics("lineno")
            took = time.perf_counter() - t0
            totals = {(s.traceback[0].filename, s.traceback[0].lineno): s.size for s in stats}
            with open(self.folder / f"tracemalloc_{self.stem}_{tag}.txt", "w",
                      encoding="utf-8") as fh:
                fh.write(f"# {_stamp(_clock())} ET  {why}  traced={cur / 2**20:.1f} MB  "
                         f"peak={peak / 2**20:.1f} MB  snapshot_took={took:.2f}s\n")
                if baseline is not None:
                    growth = sorted(((size - baseline.get(key, 0), size, key)
                                     for key, size in totals.items()), reverse=True)
                    fh.write(f"\n## top {TOP_N} growth since the 09:15:30 baseline\n")
                    for diff, size, (fname, line) in growth[:TOP_N]:
                        fh.write(f"{fname}:{line}: {diff / 2**20:+.2f} MiB "
                                 f"(now {size / 2**20:.2f} MiB)\n")
                fh.write(f"\n## top {TOP_N} by size\n")
                for s in stats[:TOP_N]:
                    fh.write(f"{s}\n")
            return totals
        except Exception:
            return None


def _stop_tracing() -> None:
    try:
        import tracemalloc
        if tracemalloc.is_tracing():
            tracemalloc.stop()
    except Exception:
        pass
