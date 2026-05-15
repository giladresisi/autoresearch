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
        if len(args) >= 2:
            entry_price = float(args[1])
            label = "LONG" if direction == "long" else "SHORT"
            print(f"Stop entry {label} at {entry_price} | S/L: to be set at fill")
            live_orders.place_stop_entry(direction, entry_price, 0.0)
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
        print(f"Moving stop entry -> {new_price} | direction: {direction}")
        live_orders.move_stop_entry(new_price, 0.0, direction)

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

    elif cmd == "terminate":
        import psutil
        from pathlib import Path

        killed = []

        # Kill orchestrator from PID file
        pid_file = Path("orchestrator.pid")
        if pid_file.exists():
            try:
                orch_pid = int(pid_file.read_text().strip())
                try:
                    p = psutil.Process(orch_pid)
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        p.kill()
                    killed.append(f"orchestrator pid={orch_pid}")
                except psutil.NoSuchProcess:
                    killed.append(f"orchestrator pid={orch_pid} (already dead)")
            except (ValueError, OSError):
                pass

        # Kill powershell wrapper running orchestrator.main
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

        # Kill automation.main
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
