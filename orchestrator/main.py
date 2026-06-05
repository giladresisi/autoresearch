# Run as: uv run python -m orchestrator.main [--summary] [--force] [--check-parquets] [--create-empty-parquets]
# --force: reset hypothesis direction and position state at session start.
# IMPORTANT: always use 'uv run python' (not bare 'python') so the command resolves to the
# project venv. Bare 'python' may resolve to system Python which lacks project dependencies.
#
# Agent (Claude Code / Bash tool) usage:
#   The .env file is loaded automatically via load_dotenv() below using an explicit path
#   relative to this file, so no manual sourcing is needed. If env vars are still missing
#   (e.g. IB_PORT), run:  set -a && source .env && set +a
#   before invoking uv, or verify that .env exists in the project root.
#
# orchestrator/main.py
# Daemon entry point: waits for trading sessions, runs signal_smt.py, and triggers post-session summarization.
import atexit as _atexit
import datetime
import os as _os
import sys
import time
import traceback as _traceback
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from orchestrator.output import FileSink, OutputChannel, StdoutSink, TimestampedFileSink
from orchestrator.process import ProcessManager
from orchestrator.relay import SessionRelay
from orchestrator.scheduler import get_et_now, is_trading_day, next_session_open
from orchestrator.summarizer import Summarizer
from session_times import SESSION_OPEN as _SESSION_OPEN_V2, SESSION_CLOSE as _SESSION_CLOSE_V2, cme_session_date

import paths

LIVE_TRADING = _os.environ.get("LIVE_TRADING", "false").lower() == "true"

_ET = ZoneInfo("America/New_York")
_SIGNAL_SMT = Path(__file__).parent.parent / "signal_smt.py"
# Live sessions live in the machine-global folder so every worktree can see them.
_SESSIONS_DIR = paths.sessions_dir()


def _make_session_channels(date: datetime.date) -> tuple[OutputChannel, OutputChannel]:
    """Create session directory and return (signal_channel, orch_channel)."""
    session_dir = _SESSIONS_DIR / date.isoformat()
    session_dir.mkdir(parents=True, exist_ok=True)

    signal_ch = OutputChannel()
    signal_ch.add_sink(StdoutSink())
    signal_ch.add_sink(TimestampedFileSink(session_dir / "signals.log"))

    orch_ch = OutputChannel()
    orch_ch.add_sink(StdoutSink())
    orch_ch.add_sink(TimestampedFileSink(session_dir / "orchestrator.log"))

    return signal_ch, orch_ch


def _close_session_position(log_ch: OutputChannel) -> None:
    """Send a market close if position.json shows an active V2 position at session end."""
    try:
        import smt_state as _smt
        _pos = _smt.load_position()
        if not _pos.get("active"):
            return
        import live_orders as _lo
        _fill_price = float(_pos["active"].get("fill_price", 0.0))
        log_ch.writeln(
            f"[ORCH] Active position at session end (fill {_fill_price:.2f}) — sending market close"
        )
        _lo.close_position(_fill_price, reason="session-end")
        log_ch.writeln("[ORCH] Session-end close sent")
    except Exception as _exc:
        log_ch.writeln(f"[ORCH] WARNING: session-end close failed: {_exc}")


def _check_ib_reachable() -> None:
    """TCP-probe IB Gateway. Prints an alert and exits if unreachable."""
    import os
    import socket
    import sys
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError:
        print(
            f"[ORCH] FATAL: IB Gateway not reachable at {host}:{port} — "
            "open TWS / IB Gateway and restart the orchestrator. Exiting.",
            flush=True,
        )
        sys.exit(1)


def _pre_session_init() -> None:
    """Run at orchestrator startup: merge leftover session 1s parquets; IB gap-fill (signal mode only).

    In LIVE_TRADING mode the IB gap-fill is skipped here — automation.main connects to IB
    immediately on startup and handles its own gap-fill, so running a separate gap-fill first
    would be redundant and could cause a brief IB client-ID conflict.

    All backfill is IB-only. Databento disabled: retroactive roll adjustments cause
    price discontinuities in append-only parquets.
    """
    _check_ib_reachable()
    bar_data_dir = paths.general_live_dir()
    try:
        from data.parquet_maintenance import merge_session_1s_parquets
        merge_session_1s_parquets(bar_data_dir)
    except Exception as exc:
        print(f"[ORCH] WARNING: session 1s merge (crash recovery) failed: {exc}", flush=True)
    if not LIVE_TRADING:
        try:
            from data.ib_realtime import gap_fill_1m_ib
            print("[ORCH] Running IB 1m gap fill ...", flush=True)
            gap_fill_1m_ib(bar_data_dir)
            print("[ORCH] IB 1m gap fill complete", flush=True)
        except Exception as exc:
            print(f"[ORCH] WARNING: IB 1m gap fill failed: {exc}", flush=True)


def _check_parquet_files(bar_data_dir: Path) -> None:
    """Pause and prompt if any required parquet file is missing.

    Interactive (TTY) mode: prompts the operator to copy files or create empty ones.
    Non-interactive mode (no TTY, e.g. launched by a Claude Code agent): exits with
    code 10 so the agent can detect the condition, ask the user via AskUserQuestion,
    and call --create-empty-parquets before restarting.
    """
    import pandas as _pd

    required = ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]
    while True:
        missing = [f for f in required if not (bar_data_dir / f).exists()]
        if not missing:
            return
        if not sys.stdin.isatty():
            # Agent context: signal missing files without blocking on input().
            # The agent should run --check-parquets / --create-empty-parquets first.
            print(
                f"[ORCH] ERROR: Missing parquets: {', '.join(missing)}\n"
                "[ORCH] Run '--check-parquets' to inspect and '--create-empty-parquets' to initialise.",
                flush=True,
            )
            sys.exit(10)
        print(
            f"\n[ORCH] Missing parquet files: {', '.join(missing)}\n"
            "\nOptions:\n"
            "  1) Copy the files from another repo/worktree, then press Enter to retry\n"
            "  2) Create empty parquet files (Databento/IB will fill them at startup)\n",
            flush=True,
        )
        choice = input("Enter 1 or 2: ").strip()
        if choice == "1":
            input("[ORCH] Press Enter when files have been copied... ")
        elif choice == "2":
            empty = _pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=_pd.DatetimeIndex([], tz="America/New_York"),
                dtype=float,
            )
            bar_data_dir.mkdir(parents=True, exist_ok=True)
            for fname in missing:
                empty.to_parquet(bar_data_dir / fname)
                print(f"[ORCH] Created empty {fname}", flush=True)
            return
        else:
            print("[ORCH] Invalid choice — enter 1 or 2.", flush=True)


def _cli_check_parquets() -> None:
    """Print JSON {"missing": [...]} and exit 0 (all present) or 1 (some missing).

    Designed for agent use: the agent runs this, parses the output, and decides
    whether to call --create-empty-parquets or ask the user to copy files.
    """
    import json
    bar_data_dir = paths.general_live_dir()
    required = ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]
    missing = [f for f in required if not (bar_data_dir / f).exists()]
    print(json.dumps({"missing": missing}), flush=True)
    sys.exit(0 if not missing else 1)


def _cli_create_empty_parquets() -> None:
    """Create empty (schema-correct) parquets for any missing files, then exit 0.

    Called by the agent after the user chooses 'create new' in response to
    --check-parquets.  Skips files that already exist.
    """
    import pandas as _pd
    bar_data_dir = paths.general_live_dir()
    required = ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]
    empty = _pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=_pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    bar_data_dir.mkdir(parents=True, exist_ok=True)
    for fname in required:
        path = bar_data_dir / fname
        if not path.exists():
            empty.to_parquet(path)
            print(f"[ORCH] Created empty {fname}", flush=True)
    sys.exit(0)


import threading as _threading


def _atexit_clean_exit() -> None:
    """Stamp the stdout log on any Python-level exit (distinguishes TerminateProcess)."""
    try:
        _log = Path(__file__).resolve().parent.parent / "orchestrator_stdout.log"
        _ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_log, "a", encoding="utf-8") as _f:
            _f.write(f"=== PYTHON EXIT {_ts} ===\n")
    except Exception:
        pass


_atexit.register(_atexit_clean_exit)

# Prevent Windows Modern Standby / idle sleep from suspending this process or dropping
# network connections while the orchestrator is running.  Without this, the Mediatek WiFi
# driver enters deep sleep on idle-timeout, tears down TCP connections to IB Gateway, and
# kills this process — leaving automation.main as an orphan with no supervision.
# ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001) = 0x80000001
try:
    import ctypes as _ctypes
    _ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
except Exception:
    pass


def _thread_excepthook(args) -> None:
    """Write unhandled thread exceptions to orchestrator_crash.log."""
    try:
        _crash = Path(__file__).resolve().parent.parent / "orchestrator_crash.log"
        _ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_crash, "a", encoding="utf-8") as _f:
            _f.write(f"=== THREAD CRASH {_ts} thread={args.thread.name} ===\n")
            _traceback.print_exception(args.exc_type, args.exc_value, args.exc_tb, file=_f)
            _f.write("\n")
    except Exception:
        pass


_threading.excepthook = _thread_excepthook

_PRE_SESSION_IB_STOP_EARLY_SECS = 60  # signal mode only: stop pre-session IB this many seconds before open

_STOP_FILE = Path(__file__).resolve().parent.parent / "orchestrator_stop.req"


class _GracefulStop(Exception):
    """Raised when trade.py terminate writes the stop-request sentinel file."""


def _check_stop_requested() -> None:
    if _STOP_FILE.exists():
        try:
            _STOP_FILE.unlink()
        except OSError:
            pass
        raise _GracefulStop()


def _start_pre_session_ib(
    bar_data_dir: Path,
) -> "tuple[object, _threading.Thread, list] | tuple[None, None, list]":
    """Start IbRealtimeSource in a daemon thread for pre-market 1m bar accumulation.

    Returns (source, thread, thread_exc) where thread_exc is a one-element list that
    is populated with the exception if the thread exits with an error.
    Returns (None, None, [None]) when MNQ_CONID or MES_CONID is absent.
    """
    import os as _os2
    mnq_conid = _os2.environ.get("MNQ_CONID")
    mes_conid  = _os2.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print(
            "[ORCH] MNQ_CONID/MES_CONID not set — skipping pre-session IB accumulator",
            flush=True,
        )
        return None, None, [None]
    from data.ib_realtime import IbRealtimeSource
    source = IbRealtimeSource(
        host=_os2.environ.get("IB_HOST", "127.0.0.1"),
        port=int(_os2.environ.get("IB_PORT", "4002")),
        client_id=int(_os2.environ.get("PRE_SESSION_IB_CLIENT_ID", "10")),
        mnq_conid=mnq_conid,
        mes_conid=mes_conid,
        bar_data_dir=bar_data_dir,
        on_bar=lambda bar, mes: None,   # accumulate only; strategy runs in session subprocess
    )
    thread_exc: list = [None]

    def _run() -> None:
        try:
            source.start()
        except Exception as exc:
            if not source._stopping:
                thread_exc[0] = exc

    thread = _threading.Thread(target=_run, daemon=True, name="pre-session-ib")
    thread.start()
    print("[ORCH] Pre-session IB accumulator started (client_id="
          f"{_os2.environ.get('PRE_SESSION_IB_CLIENT_ID', '10')})", flush=True)
    return source, thread, thread_exc


def _stop_pre_session_ib(source, thread: "_threading.Thread | None") -> None:
    """Stop the pre-session IB accumulator and wait for its thread to exit."""
    if source is None:
        return
    source.stop()
    if thread is not None and thread.is_alive():
        thread.join(timeout=15.0)
    print("[ORCH] Pre-session IB accumulator stopped", flush=True)


def _sleep_until(target: datetime.datetime, label: str, ib_health_check=None) -> None:
    now = get_et_now()
    delay = (target - now).total_seconds()
    if delay > 0:
        hours = delay / 3600
        print(f"[ORCH] Sleeping {hours:.1f}h until {label}", flush=True)
        while True:
            _check_stop_requested()
            now = get_et_now()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(5.0, remaining))
            if ib_health_check is not None:
                ib_health_check()


def _make_ib_health_check(thread: _threading.Thread, thread_exc: list):
    """Return a callable that terminates the orchestrator if the IB thread died with an error."""
    def check() -> None:
        if not thread.is_alive() and thread_exc[0] is not None:
            print(
                f"\n[ORCH] *** CRITICAL: Pre-session IB connection failed: {thread_exc[0]}\n"
                "[ORCH] *** The strategy cannot run until IB Gateway is active and reachable.\n"
                "[ORCH] *** Fix IB Gateway and restart the orchestrator. Terminating now. ***",
                flush=True,
            )
            sys.exit(4)
    return check


def _pidfile() -> Path:
    """Orchestrator PID file — lives in the shared general live folder (alongside global.json
    and the pause sentinel), NOT the worktree, so there is one canonical location for the
    single live orchestrator across worktrees. Resolved at call time (general_live_dir reads
    ACT_GLOBAL_DIR and mkdir's)."""
    return paths.general_live_dir() / "orchestrator.pid"


def _kill_stale_orchestrator() -> None:
    """Kill any stale orchestrator.main Python process FROM THIS WORKTREE, then record our PID.

    Scans Python processes for 'orchestrator.main' in their command line, excluding our own
    process and its direct parent (the background-task wrapper whose cmdline also contains
    'orchestrator.main').

    **Worktree-scoped:** only a process whose working directory is *this* worktree's root is
    terminated. A sibling worktree's (possibly LIVE) orchestrator is never killed — an
    unscoped machine-wide kill here was the root cause of cross-worktree orchestrator deaths
    (e.g. a test that calls run() in another worktree). Candidates whose cwd can't be read are
    left alone rather than risk killing a foreign process. PID file is written last so that a
    competing zombie that races us here will overwrite it.
    """
    import psutil
    current_pid = _os.getpid()
    try:
        parent_pid = psutil.Process(current_pid).ppid()
    except psutil.NoSuchProcess:
        parent_pid = None
    protected = {current_pid, parent_pid} if parent_pid else {current_pid}
    worktree_root = Path(__file__).resolve().parent.parent

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info.get("name", "").lower() not in ("python.exe", "python"):
                continue
            if proc.pid in protected:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not any("orchestrator.main" in arg for arg in cmdline):
                continue
            # Worktree scoping: never terminate an orchestrator from another worktree.
            try:
                if Path(proc.cwd()).resolve() != worktree_root:
                    continue
            except (psutil.Error, OSError):
                continue  # cwd unreadable → don't risk killing a foreign process
            print(f"[orchestrator] Killing stale orchestrator (pid={proc.pid})", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _pidfile().write_text(str(current_pid))


def run(summarizer: Summarizer | None = None, skip_summary: bool = False, force_reset: bool = False) -> None:
    """Main daemon loop. Ctrl+C exits cleanly; subprocess is terminated if active."""
    _kill_stale_orchestrator()
    if not skip_summary and summarizer is None:
        summarizer = Summarizer()
    bar_data_dir = paths.general_live_dir()
    _check_parquet_files(bar_data_dir)
    _pre_src = None
    _pre_thr = None
    try:
        _pre_session_init()
        while True:
            now   = get_et_now()
            today = now.date()
            # `today` (ET calendar date) drives SCHEDULING (session_open_dt, grace_end_dt,
            # is_trading_day). `session_label` (CME trade date = ET-open-date + 1, stable
            # across the midnight roll) names the session FOLDER, so the orchestrator's
            # state JSONs land in the same folder as automation.main's events/bar_state
            # (which use session_date_str() == cme_session_date(now)).
            session_label = cme_session_date(now)

            if not is_trading_day(today):
                _pre_src, _pre_thr, _pre_err = _start_pre_session_ib(bar_data_dir)
                _sleep_until(next_session_open(now), "next trading session",
                             ib_health_check=_make_ib_health_check(_pre_thr, _pre_err))
                _stop_pre_session_ib(_pre_src, _pre_thr)
                continue

            session_open_dt = datetime.datetime.combine(today, _SESSION_OPEN_V2).replace(tzinfo=_ET)
            grace_end_dt    = datetime.datetime.combine(today, _SESSION_CLOSE_V2).replace(tzinfo=_ET)

            # CME session opens at SESSION_OPEN (18:00) and closes at SESSION_CLOSE (17:00)
            # the *next* calendar day.  After 18:00 ET, grace_end_dt must be advanced to
            # tomorrow — otherwise any time between 17:00 and midnight is mis-classified as
            # "session over" and the orchestrator sleeps until the next NYSE open instead of
            # running the live session that already started at 18:00.
            if now >= session_open_dt:
                grace_end_dt = datetime.datetime.combine(
                    today + datetime.timedelta(days=1), _SESSION_CLOSE_V2
                ).replace(tzinfo=_ET)

            if now < session_open_dt:
                if LIVE_TRADING:
                    # automation.main starts immediately: it connects to IB, gap-fills, and
                    # streams bars into the parquets well before session open with no handoff gap.
                    print("[ORCH] LIVE_TRADING: starting automation.main now for early gap-fill", flush=True)
                else:
                    # Signal mode: pre-session IB accumulator runs until 60s before open,
                    # then signal_smt.py takes over the IB connection.
                    _pre_src, _pre_thr, _pre_err = _start_pre_session_ib(bar_data_dir)
                    _ib_check = _make_ib_health_check(_pre_thr, _pre_err)
                    _stop_ts = session_open_dt - datetime.timedelta(seconds=_PRE_SESSION_IB_STOP_EARLY_SECS)
                    if now < _stop_ts:
                        _sleep_until(_stop_ts, "pre-session IB shutdown", ib_health_check=_ib_check)
                    _stop_pre_session_ib(_pre_src, _pre_thr)
                    time.sleep(10)
                # Fall through to session run.

            if now >= grace_end_dt:
                _pre_src, _pre_thr, _pre_err = _start_pre_session_ib(bar_data_dir)
                _sleep_until(next_session_open(now), "next trading session",
                             ib_health_check=_make_ib_health_check(_pre_thr, _pre_err))
                _stop_pre_session_ib(_pre_src, _pre_thr)
                continue

            # Run session (no pre-session IB during session — subprocess owns the IB connection)
            signal_ch, orch_ch = _make_session_channels(session_label)
            relay = SessionRelay(signal_ch)
            # This session's state JSONs live in its session folder. Resolve the dir once
            # here and hand the SAME path to the subprocess via ACT_STATE_DIR so both
            # processes agree by construction — the session-end position check below must
            # read exactly what the subprocess wrote (a date mismatch would miss an open
            # position at close). Folder is named by the CME trade date (session_label),
            # matching automation.main's session_date_str().
            _session_state_dir = _SESSIONS_DIR / session_label.isoformat()
            paths.set_state_dir(_session_state_dir)
            if LIVE_TRADING:
                signal_cmd = ["uv", "run", "python", "-m", "automation.main"]
            else:
                signal_cmd = _SIGNAL_SMT
            print(f"[orchestrator] mode={'LIVE_TRADING' if LIVE_TRADING else 'signal'}", flush=True)
            _extra = {"ACT_STATE_DIR": str(_session_state_dir)}
            if force_reset:
                _extra["FORCE_RESET"] = "true"
            result = ProcessManager(signal_cmd, relay, orch_ch, extra_env=_extra).run_session(today, grace_end_dt=grace_end_dt)
            # Post-session: fill the ~2-min gap (gap-fill end → first session tick) and merge
            # session 1s parquet into main. This runs before pre-session IB restarts so the
            # session file is cleaned up before overnight accumulation begins.
            try:
                from data.parquet_maintenance import merge_session_1s_parquets
                merge_session_1s_parquets(bar_data_dir)
                orch_ch.writeln("[ORCH] 1s session parquets merged into main")
            except Exception as _exc:
                orch_ch.writeln(f"[ORCH] WARNING: 1s session merge failed: {_exc}")
            _close_session_position(orch_ch)
            relay.write_trades_tsv(_SESSIONS_DIR / session_label.isoformat() / "trades.tsv", today)
            # if summarizer is not None:
            #     summarizer.run(today, _SESSIONS_DIR / today.isoformat() / "signals.log", _SESSIONS_DIR, signal_ch)
            if result == "ib_disconnected":
                orch_ch.writeln(
                    "[ORCH] *** IB Gateway disconnected. Restart IB Gateway, then relaunch "
                    "the orchestrator. automation.main attempted a hard close; verify position "
                    "state before restarting. ***"
                )
                sys.exit(3)
            # Post-session: accumulate overnight bars while sleeping until next session
            _pre_src, _pre_thr, _pre_err = _start_pre_session_ib(bar_data_dir)
            _sleep_until(next_session_open(get_et_now()), "next trading session",
                         ib_health_check=_make_ib_health_check(_pre_thr, _pre_err))
            _stop_pre_session_ib(_pre_src, _pre_thr)
    except _GracefulStop:
        print("\n[ORCH] Stop requested — shutting down gracefully.", flush=True)
        _stop_pre_session_ib(_pre_src, _pre_thr)
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n[ORCH] Shutting down.", flush=True)
        _stop_pre_session_ib(_pre_src, _pre_thr)
        sys.exit(0)
    except Exception as _exc:
        # Unhandled exception in the main orchestrator loop — log full traceback before dying.
        try:
            _crash = Path(__file__).resolve().parent.parent / "orchestrator_crash.log"
            _ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(_crash, "a", encoding="utf-8") as _f:
                _f.write(f"=== MAIN THREAD CRASH {_ts} ===\n")
                _traceback.print_exc(file=_f)
                _f.write("\n")
        except Exception:
            pass
        print(
            f"\n[ORCH] *** UNHANDLED EXCEPTION: {_exc!r} ***\n{_traceback.format_exc()}",
            flush=True,
        )
        raise
    finally:
        try:
            _pf = _pidfile()
            if _pf.exists() and _pf.read_text().strip() == str(_os.getpid()):
                _pf.unlink()
        except OSError:
            pass


def _check_setup() -> None:
    """Validate environment and print OK, then exit 0; exit 1 if setup fails."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ORCH] ERROR: ANTHROPIC_API_KEY environment variable is required", flush=True)
        sys.exit(1)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        Summarizer()
    except RuntimeError as e:
        print(f"[ORCH] ERROR: {e}", flush=True)
        sys.exit(1)
    print("[ORCH] Setup OK", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check_setup()
    elif "--check-parquets" in sys.argv:
        _cli_check_parquets()
    elif "--create-empty-parquets" in sys.argv:
        _cli_create_empty_parquets()
    else:
        run(skip_summary="--summary" not in sys.argv, force_reset="--force" in sys.argv)
