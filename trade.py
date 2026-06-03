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
  python trade.py trend-broken           # Reset hypothesis direction and log trend-broken
  python trade.py hypothesis             # Force a fresh hypothesis evaluation right now
  python trade.py start                  # Start orchestrator (keeps position.json; resumes & reconciles any open position)
  python trade.py start --summary        # Start orchestrator with LLM-based summary enabled
  python trade.py start --force          # Reset hypothesis direction and position state (start fresh)
  python trade.py terminate              # Kill orchestrator and automation.main

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
    """Return the live orchestrator PID from orchestrator.pid, or None if not running."""
    import psutil
    from pathlib import Path

    pid_file = Path("orchestrator.pid")
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        psutil.Process(pid)  # raises NoSuchProcess if dead
        return pid
    except (ValueError, OSError, psutil.NoSuchProcess):
        return None


def _terminate_all() -> list[str]:
    """Gracefully stop orchestrator (cancelMktData + IB disconnect + parquet flush), then kill
    automation.main. Returns list of killed/stopped descriptions."""
    import psutil
    from pathlib import Path

    killed = []

    pid_file = Path("orchestrator.pid")
    stop_file = Path("orchestrator_stop.req")
    if pid_file.exists():
        try:
            orch_pid = int(pid_file.read_text().strip())
            try:
                p = psutil.Process(orch_pid)
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
            if "orchestrator.main" in cmdline:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                killed.append(f"powershell wrapper pid={proc.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info.get("name", "").lower() not in ("python.exe", "python"):
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("automation.main" in arg for arg in cmdline):
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
            live_orders.place_stop_entry(direction, entry_price, sl_price)
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
            live_orders.place_market_entry(direction, 0.0, float(stop))

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
        live_orders.move_stop_entry(new_price, pending_stop, direction)

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

    elif cmd == "trend-broken":
        live_orders.trend_broken()

    elif cmd == "hypothesis":
        live_orders.hypothesis()

    elif cmd == "start":
        import os
        import subprocess
        import time
        from datetime import datetime
        from pathlib import Path

        summary = "--summary" in raw_args

        # Check if orchestrator is already running
        existing_pid = _orchestrator_pid()
        if existing_pid is not None:
            print(f"Killing existing orchestrator (pid={existing_pid})...")
            killed = _terminate_all()
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
