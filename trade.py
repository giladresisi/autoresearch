#!/usr/bin/env python
"""trade.py — Manual order CLI for the live session.

Usage:
  python trade.py up                # Market LONG  (S/L from bar_state.json)
  python trade.py up 27000          # Stop entry LONG at 27000
  python trade.py down              # Market SHORT (S/L from bar_state.json)
  python trade.py down 27000        # Stop entry SHORT at 27000
  python trade.py cancel            # Cancel unfilled stop entry
  python trade.py move 28000        # Move unfilled stop entry to 28000
  python trade.py stop 19700        # Move stop-loss on active position to 19700
  python trade.py close             # Market close active position
"""
from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0].lower()

    import live_orders
    import smt_state

    if cmd in ("up", "down"):
        direction = "long" if cmd == "up" else "short"
        if len(args) >= 2:
            # Stop entry at specified price
            entry_price = float(args[1])
            label = "LONG" if direction == "long" else "SHORT"
            print(f"Stop entry {label} at {entry_price} | S/L: to be set at fill")
            live_orders.place_stop_entry(direction, entry_price, 0.0)
        else:
            # Market entry — read stop from bar_state.json
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
        if not pos.get("stop_entry"):
            print("ERROR: no pending stop entry to cancel")
            sys.exit(1)
        print(f"Cancelling stop entry at {pos['stop_entry']}")
        live_orders.cancel_stop_entry("user-requested")

    elif cmd == "move":
        if len(args) < 2:
            print("ERROR: move requires a price argument (e.g. python trade.py move 28000)")
            sys.exit(1)
        pos = live_orders.get_position()
        if not pos.get("stop_entry"):
            print("ERROR: no pending stop entry to move")
            sys.exit(1)
        new_price = float(args[1])
        direction = "long" if pos.get("stop_direction") in ("up", "long") else "short"
        print(f"Moving stop entry {pos['stop_entry']} -> {new_price}")
        live_orders.move_stop_entry(new_price, 0.0, direction)

    elif cmd == "stop":
        if len(args) < 2:
            print("ERROR: stop requires a price argument (e.g. python trade.py stop 19700)")
            sys.exit(1)
        pos = live_orders.get_position()
        if not pos.get("active"):
            print("ERROR: no active position to update stop-loss on")
            sys.exit(1)
        stop_price = float(args[1])
        print(f"Moving stop-loss to {stop_price} | position: {pos['active']['direction']}")
        live_orders.update_stop_loss(stop_price, "user-requested")

    elif cmd == "close":
        pos = live_orders.get_position()
        if not pos.get("active"):
            print("ERROR: no active position to close")
            sys.exit(1)
        print(f"Market close | direction: {pos['active']['direction']}")
        live_orders.close_position(0.0, "user-requested")

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
