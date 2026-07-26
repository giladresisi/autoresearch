#!/usr/bin/env python
"""trade.py — Manual order CLI for the live session.

Usage:
  python trade.py up                     # Market LONG  (S/L from bar_state.json)
  python trade.py up 27000               # Stop entry LONG at 27000
  python trade.py down                   # Market SHORT (S/L from bar_state.json)
  python trade.py down 27000             # Stop entry SHORT at 27000
  python trade.py cancel                 # Cancel unfilled stop entry
  python trade.py move 28000             # Move unfilled stop entry to 28000
  python trade.py update-sl 19700        # Move stop-loss on active position to 19700
  python trade.py close                  # Market close active position
  python trade.py trend-broken           # Reset hypothesis direction and log trend-broken (releases any manual lock)
  python trade.py hypothesis             # Force a fresh hypothesis evaluation right now (releases any manual lock)
  python trade.py set-direction down     # Force hypothesis direction + cautious ladder and LOCK it (alias: flip)
  python trade.py unlock                 # Release the manual direction lock (direction kept; auto resets resume)
  python trade.py pause                  # Suppress new automatic entries (exits stay active)
  python trade.py resume                 # Re-enable automatic entries
  python trade.py start                  # Start orchestrator (keeps position.json; resumes & reconciles any open position)
  python trade.py start --summary        # Start orchestrator with LLM-based summary enabled
  python trade.py start --force          # Reset hypothesis direction and position state (start fresh)
  python trade.py start --pause          # Start with automatic entries paused (creates data/paused; start continues regardless)
  python trade.py start --resume         # Start with automatic entries enabled (clears data/paused; start continues regardless)
  python trade.py terminate              # Kill orchestrator and automation.main
  python trade.py gap-fill               # IB-backfill main 1s+1m parquets up to now (orchestrator must NOT be running)
  python trade.py promote                # Copy live parquets over main (prior main backed up to .bak) — run after gap-fill

Add --force / -f to bypass position.json state checks and override broker state:
  python trade.py close --force
  python trade.py cancel --force
  python trade.py up --force             # Allow even if position.json shows open position
  python trade.py up 27000 --force
  python trade.py move 28000 --force     # Direction inferred from position.json
  python trade.py move 28000 --force up  # Direction explicit (up=long, down=short)
  python trade.py update-sl 19700 --force     # Direction inferred from position.json
  python trade.py update-sl 19700 --force down
"""
from __future__ import annotations

import sys


def _resolve_direction(pos_dir: str, extra_arg: str | None) -> str | None:
    """Return 'long'/'short' from an explicit arg or a stored direction string, else None."""
    src = extra_arg or pos_dir
    if src in ("up", "long"):
        return "long"
    if src in ("down", "short"):
        return "short"
    return None


def _orchestrator_pid() -> int | None:
    """Return the live orchestrator PID from orchestrator.pid, or None if not running.

    The PID file lives in the shared general live folder (paths.general_live_dir()), the one
    canonical location for the single live orchestrator (alongside global.json / the pause
    sentinel)."""
    import psutil
    import paths

    pid_file = paths.general_live_dir() / "orchestrator.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        psutil.Process(pid)  # raises NoSuchProcess if dead
        return pid
    except (ValueError, OSError, psutil.NoSuchProcess):
        return None


def _proc_in_worktree(proc, root) -> bool:
    """True only if `proc`'s working directory resolves to this worktree's root.

    Returns False when the cwd is unreadable or belongs to another worktree — so the kill scans
    below never terminate a sibling worktree's (possibly live) orchestrator/automation.main. An
    unscoped machine-wide kill here was the root cause of cross-worktree orchestrator deaths."""
    import psutil
    from pathlib import Path
    try:
        return Path(proc.cwd()).resolve() == root
    except (psutil.Error, OSError):
        return False


def _terminate_all() -> list[str]:
    """Gracefully stop orchestrator (cancelMktData + IB disconnect + parquet flush), then kill
    automation.main — **scoped to THIS worktree**. Returns list of killed/stopped descriptions."""
    import psutil
    import paths
    from pathlib import Path

    killed = []
    worktree_root = Path(__file__).resolve().parent

    # PID file lives in the shared general live folder (one canonical live orchestrator).
    pid_file = paths.general_live_dir() / "orchestrator.pid"
    stop_file = Path("orchestrator_stop.req")
    if pid_file.exists():
        try:
            orch_pid = int(pid_file.read_text().strip())
            try:
                p = psutil.Process(orch_pid)
                # The PID file is shared across worktrees; only stop the orchestrator if it
                # belongs to THIS worktree. A sibling worktree's (possibly LIVE) orchestrator
                # must never be stopped from here (same scoping the scans below enforce).
                if _proc_in_worktree(p, worktree_root):
                    # Write stop sentinel so the orchestrator's sleep loop wakes, calls
                    # source.stop() (cancelMktData + IB disconnect + parquet flush), then exits.
                    stop_file.write_text("stop")
                    try:
                        p.wait(timeout=20)
                        killed.append(f"orchestrator pid={orch_pid} (graceful)")
                    except psutil.TimeoutExpired:
                        # Orchestrator didn't exit in time — hard kill as fallback.
                        try:
                            stop_file.unlink()
                        except OSError:
                            pass
                        p.kill()
                        killed.append(f"orchestrator pid={orch_pid} (force-killed after timeout)")
            except psutil.NoSuchProcess:
                killed.append(f"orchestrator pid={orch_pid} (already dead)")
        except (ValueError, OSError):
            pass

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info.get("name", "").lower() != "powershell.exe":
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "orchestrator.main" in cmdline and _proc_in_worktree(proc, worktree_root):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                killed.append(f"powershell wrapper pid={proc.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Orphaned orchestrator.main Python processes scoped to THIS worktree. The pid-file
    # branch above gracefully stops the *tracked* orchestrator; this catches an orphan whose
    # pid is NOT in the file (crash-loop / stale-or-missing pid file) that would otherwise
    # survive a fresh start and run a second live orchestrator. Killed before automation.main
    # so a surviving orchestrator can't respawn a child mid-sweep.
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info.get("name", "").lower() not in ("python.exe", "python"):
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("orchestrator.main" in arg for arg in cmdline) and _proc_in_worktree(proc, worktree_root):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                killed.append(f"orchestrator.main pid={proc.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info.get("name", "").lower() not in ("python.exe", "python"):
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("automation.main" in arg for arg in cmdline) and _proc_in_worktree(proc, worktree_root):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                killed.append(f"automation.main pid={proc.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return killed


def main() -> None:
    raw_args = sys.argv[1:]
    if not raw_args:
        print(__doc__)
        sys.exit(1)

    force = "--force" in raw_args or "-f" in raw_args
    args = [a for a in raw_args if a not in ("--force", "-f")]
    cmd = args[0].lower()

    import live_orders
    import smt_state

    if cmd in ("up", "down"):
        direction = "long" if cmd == "up" else "short"
        pos = live_orders.get_position()
        if not force and (pos.get("active") or pos.get("stop_entry")):
            what = "active position" if pos.get("active") else "pending stop entry"
            print(f"ERROR: position.json already has a {what} — use --force to override")
            sys.exit(1)
        forced_v2 = "up" if direction == "long" else "down"
        live_orders._force_hypothesis_for_direction(forced_v2)
        if len(args) >= 2:
            entry_price = float(args[1])
            if len(args) >= 3:
                sl_price = float(args[2])
            else:
                bar_state = smt_state.load_bar_state()
                if bar_state is None:
                    print("ERROR: bar_state.json not found — provide sl_price explicitly (e.g. trade.py up 20000 19950)")
                    sys.exit(1)
                stop_key = "potential_stop_long" if direction == "long" else "potential_stop_short"
                _sl = bar_state.get(stop_key)
                if _sl is None:
                    print(f"ERROR: {stop_key} is null in bar_state.json — provide sl_price explicitly")
                    sys.exit(1)
                sl_price = float(_sl)
            label = "LONG" if direction == "long" else "SHORT"
            print(f"Stop entry {label} at {entry_price} | S/L: {sl_price}")
            live_orders.place_stop_entry(direction, entry_price, sl_price, source="manual")
        else:
            bar_state = smt_state.load_bar_state()
            if bar_state is None:
                print("ERROR: bar_state.json not found — cannot determine stop price")
                sys.exit(1)
            stop_key = "potential_stop_long" if direction == "long" else "potential_stop_short"
            stop = bar_state.get(stop_key)
            if stop is None:
                print(f"ERROR: {stop_key} is null in bar_state.json — cannot place market entry")
                sys.exit(1)
            label = "LONG" if direction == "long" else "SHORT"
            print(f"Market {label} | S/L: {stop}")
            live_orders.place_market_entry(direction, 0.0, float(stop), source="manual")

    elif cmd == "cancel":
        pos = live_orders.get_position()
        if not force and not pos.get("stop_entry"):
            print("ERROR: no pending stop entry to cancel")
            sys.exit(1)
        entry_price = pos.get("stop_entry") or "unknown"
        print(f"Cancelling stop entry at {entry_price}")
        live_orders.cancel_stop_entry("user-requested", force=force)

    elif cmd == "move":
        if len(args) < 2:
            print("ERROR: move requires a price argument (e.g. python trade.py move 28000)")
            sys.exit(1)
        pos = live_orders.get_position()
        if not force and not pos.get("stop_entry"):
            print("ERROR: no pending stop entry to move")
            sys.exit(1)
        new_price = float(args[1])
        extra = args[2] if len(args) >= 3 else None
        direction = _resolve_direction(pos.get("stop_direction", ""), extra)
        if direction is None:
            print("ERROR: direction unknown — provide it explicitly: trade.py move <price> --force up|down")
            sys.exit(1)
        pending_stop = float(pos.get("pending_stop") or 0.0)
        print(f"Moving stop entry -> {new_price} | direction: {direction} | S/L: {pending_stop}")
        live_orders.move_stop_entry(new_price, pending_stop, direction, force=force)

    elif cmd == "update-sl":
        if len(args) < 2:
            print("ERROR: update-sl requires a price argument (e.g. python trade.py update-sl 19700)")
            sys.exit(1)
        pos = live_orders.get_position()
        if not force and not pos.get("active"):
            print("ERROR: no active position to update stop-loss on")
            sys.exit(1)
        stop_price = float(args[1])
        extra = args[2] if len(args) >= 3 else None
        direction = _resolve_direction(pos.get("active", {}).get("direction", ""), extra)
        print(f"Moving stop-loss to {stop_price} | direction: {direction or 'unknown'}")
        live_orders.update_stop_loss(stop_price, "user-requested", direction=direction)

    elif cmd == "close":
        pos = live_orders.get_position()
        if not force and not pos.get("active"):
            print("ERROR: no active position to close")
            sys.exit(1)
        direction = pos.get("active", {}).get("direction", "unknown")
        print(f"Market close | direction: {direction}")
        live_orders.close_position(0.0, "user-requested")

    elif cmd == "pause":
        if live_orders.pause():
            print("Paused — new automatic entries suppressed (exits still active)")
        else:
            print("Already paused")

    elif cmd == "resume":
        if live_orders.resume():
            print("Resumed — automatic entries re-enabled")
        else:
            print("Already running (not paused)")

    elif cmd == "trend-broken":
        live_orders.trend_broken()

    elif cmd == "hypothesis":
        live_orders.hypothesis()

    elif cmd in ("set-direction", "flip"):
        if len(args) < 2 or args[1].lower() not in ("up", "down"):
            print("ERROR: set-direction requires a direction (e.g. python trade.py set-direction down)")
            sys.exit(1)
        if not live_orders.set_direction(args[1].lower()):
            sys.exit(1)

    elif cmd == "unlock":
        live_orders.unlock_direction()

    elif cmd == "start":
        import os
        import subprocess
        import time
        from datetime import datetime
        from pathlib import Path

        summary = "--summary" in raw_args

        # Optional pause/resume of automatic entries from the moment the orchestrator
        # starts. Independent of --force (different concern). The start proceeds either
        # way; if the data/paused flag already matched the requested mode it just says so.
        start_pause = "--pause" in raw_args
        start_resume = "--resume" in raw_args
        if start_pause and start_resume:
            print("ERROR: --pause and --resume are mutually exclusive")
            sys.exit(1)
        if start_pause or start_resume:
            # Stamp the session date so the paused/resumed event lands in the session
            # folder the orchestrator will use (matches its session_date_str()).
            from session_times import session_date_str
            live_orders.set_session_date(session_date_str())
            if start_pause:
                if live_orders.pause():
                    print("Starting in paused mode — new automatic entries suppressed until resume")
                else:
                    print("Already paused — starting in paused mode")
            else:
                if live_orders.resume():
                    print("Starting in resumed mode — automatic entries enabled")
                else:
                    print("Already resumed — starting normally")

        # Always sweep for an existing/orphaned orchestrator + automation.main in THIS
        # worktree before launching — even when the pid file is missing or stale. A
        # crash-looped orphan whose pid is not in the file must never survive a fresh start
        # (that produced duplicate live orchestrators). _terminate_all is worktree-scoped, so
        # a sibling worktree's live run is never touched.
        existing_pid = _orchestrator_pid()
        if existing_pid is not None:
            print(f"Killing existing orchestrator (pid={existing_pid})...")
        killed = _terminate_all()
        if killed:
            for k in killed:
                print(f"Killed {k}")
            time.sleep(1)

        stdout_log = Path("orchestrator_stdout.log")
        stderr_log = Path("orchestrator_stderr.log")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(stdout_log, "a", encoding="utf-8") as f:
            f.write(f"=== RESTART {timestamp} ===\n")

        orch_cmd = ["uv", "run", "python", "-m", "orchestrator.main"]
        if summary:
            orch_cmd.append("--summary")

        CREATE_NO_WINDOW = 0x08000000
        _popen_env = {**os.environ, "FORCE_RESET": "true"} if force else None
        with open(stdout_log, "a", encoding="utf-8") as out_f, \
             open(stderr_log, "a", encoding="utf-8") as err_f:
            subprocess.Popen(
                orch_cmd,
                stdout=out_f,
                stderr=err_f,
                creationflags=CREATE_NO_WINDOW,
                env=_popen_env,
            )

        time.sleep(3)

        new_pid = _orchestrator_pid()
        if new_pid:
            print(f"Orchestrator started pid={new_pid}")
            if summary:
                print("LLM summary enabled")
        else:
            print("WARNING: orchestrator.pid not written — check orchestrator_stdout.log for errors")

    elif cmd == "gap-fill":
        from pathlib import Path
        from dotenv import load_dotenv

        # trade.py does not load .env elsewhere; gap_fill_until_now reads IB_HOST/IB_PORT/
        # MNQ_CONID/MES_CONID from the environment, so load .env before invoking it.
        load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
        from gap_fill import gap_fill_until_now

        print("Gap-filling main 1s + 1m parquets up to now "
              "(do NOT run while the live orchestrator/session is up — IB client-id conflict)...")
        gap_fill_until_now()
        print("Gap-fill complete")

    elif cmd == "promote":
        # Same promotion parquet-check runs at session end: copy the live parquets
        # into the current main subfolder, backing up each prior main file to .bak.
        # Lets offline work (e.g. after `trade.py gap-fill`) extend backtest main
        # without waiting for a session-end parquet-check.
        from scripts.check_session_parquets import promote_live_to_main

        promoted = promote_live_to_main()
        if promoted:
            print(f"Promoted {len(promoted)} file(s) live -> main: "
                  + ", ".join(sorted(promoted)))
        else:
            print("No live parquets found — nothing promoted")

    elif cmd == "terminate":
        killed = _terminate_all()
        if killed:
            for k in killed:
                print(f"Killed {k}")
        else:
            print("Nothing to terminate")

    else:
        print(f"ERROR: unknown command {cmd!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
