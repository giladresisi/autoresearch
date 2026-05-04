# tests/smoke_pmt_connection.py
# Manual smoke test for PickMyTrade connectivity.
# Sends a limit buy far below the current market price (so it cannot be filled),
# moves it to a second unrealistic price via modify_limit_entry,
# pauses for user verification at each step, then cancels it via a close order.
#
# Usage:
#   python -m pytest tests/smoke_pmt_connection.py -v -s
#   (the -s flag is required — the test prompts for user input)
#
# Prerequisites: PMT_WEBHOOK_URL, PMT_API_KEY, TRADING_ACCOUNT_ID in .env or shell.
# The test will NOT run unless SMOKE_PMT=1 is also set, to prevent accidental execution.

import os
import time

import pandas as pd
import pytest
from dotenv import load_dotenv

load_dotenv()

SMOKE_GUARD = "SMOKE_PMT"
LIMIT_OFFSET_PTS = 500.0    # place buy limit this many points below current price
LIMIT_MOVE_PTS   = 100.0    # move the limit by this many additional points for the modify step


def _requires_smoke_env():
    if not os.environ.get(SMOKE_GUARD):
        pytest.skip(f"Set {SMOKE_GUARD}=1 to run PMT connection smoke test")


def _make_executor():
    from execution.pickmytrade import PickMyTradeExecutor
    return PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_id=os.environ.get("TRADING_ACCOUNT_ID", ""),
        contracts=int(os.environ.get("TRADING_CONTRACTS", "1")),
        entry_slip_ticks=0,
    )


def _fake_bar(limit_price: float):
    """Minimal bar with name set so session_date is populated in FillRecord."""
    from strategy_smt import _BarRow
    ts = pd.Timestamp.now(tz="America/New_York").floor("min")
    # Open/High/Low/Close set to limit_price + offset so the bar is far from the limit
    return _BarRow(
        limit_price + LIMIT_OFFSET_PTS,
        limit_price + LIMIT_OFFSET_PTS + 5,
        limit_price + LIMIT_OFFSET_PTS - 5,
        limit_price + LIMIT_OFFSET_PTS,
        0.0,
        ts,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_pmt_limit_order_place_and_cancel(capsys):
    """
    Smoke test: place a limit BUY 500 pts below market, verify in Tradovate, then cancel.
    Requires SMOKE_PMT=1 and valid PMT credentials in the environment.
    """
    _requires_smoke_env()

    ex = _make_executor()
    ex.start()

    # Derive a safe limit price: current MNQ price is typically around 19000-22000.
    # We use a round number 500 pts below a rough current price estimate.
    # The user can override via SMOKE_LIMIT_PRICE env var.
    limit_price_env = os.environ.get("SMOKE_LIMIT_PRICE")
    if limit_price_env:
        limit_price = float(limit_price_env)
    else:
        # Default: 500 pts below a conservative floor — this will never fill in normal conditions
        limit_price = 15000.0

    signal = {
        "direction": "long",
        "entry_price": limit_price,
        "stop_price": limit_price - 50.0,
        "take_profit": limit_price + 100.0,
        "limit_fill_bars": 999,   # marks this as a limit order
    }

    bar = _fake_bar(limit_price)

    with capsys.disabled():
        print(f"\n[SMOKE] Sending LMT BUY @ {limit_price:.2f} to PickMyTrade...")

    rec = ex.place_entry(signal, bar)
    time.sleep(3)  # give the pool thread time to dispatch the HTTP request

    with capsys.disabled():
        print(f"[SMOKE] Order dispatched. FillRecord: order_id={rec.order_id}, "
              f"fill_price={rec.fill_price}, status={rec.status}")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    You should see a pending LMT BUY order for {signal['entry_price']:.2f}.")
        print("    ENTER = order visible (pass)  |  'fail' = order not visible  |  'skip' = skip check")
        response = input("    > ").strip().lower()

    if response == "fail":
        pytest.fail("Order placement: order not visible in Tradovate after placement")
    elif response not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response!r}")

    # Move the limit to a second unrealistic price via modify_limit_entry
    moved_price = limit_price - LIMIT_MOVE_PTS
    new_signal = {**signal, "entry_price": moved_price}

    with capsys.disabled():
        print()
        print(f"[SMOKE] Moving limit from {limit_price:.2f} → {moved_price:.2f} via modify_limit_entry...")

    ex.modify_limit_entry(signal, new_signal, bar)
    time.sleep(3)  # close is synchronous; give pool time to dispatch the re-place

    with capsys.disabled():
        print(f"[SMOKE] Modify dispatched.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    The LMT BUY order should now show price {moved_price:.2f} (was {limit_price:.2f}).")
        print("    ENTER = price updated (pass)  |  'fail' = price unchanged or order missing  |  'skip' = skip check")
        response_move = input("    > ").strip().lower()

    if response_move == "fail":
        pytest.fail(f"Order modify: price not updated to {moved_price:.2f} in Tradovate")
    elif response_move not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response_move!r}")

    # Cancel by sending a close order
    with capsys.disabled():
        print()
        print("[SMOKE] Sending close order to cancel the limit...")

    ex.place_close(label="smoke_cancel")

    with capsys.disabled():
        print("[SMOKE] Close order sent.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT AGAIN <<<")
        print("    The pending LMT BUY order should now be gone.")
        print("    ENTER = order gone (pass)  |  'fail' = order still visible  |  'skip' = skip check")
        response2 = input("    > ").strip().lower()

    if response2 == "fail":
        pytest.fail("Order cancellation: order still visible in Tradovate after close")
    elif response2 not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response2!r}")

    # Market order step: buy 1 contract at market with a stop 100 pts below.
    # SMOKE_MARKET_PRICE can be set to the current MNQ price for an accurate stop;
    # if omitted, we use limit_price + LIMIT_OFFSET_PTS as a rough estimate.
    # The 'price' field on MKT orders is informational — actual fill is at market.
    market_price_env = os.environ.get("SMOKE_MARKET_PRICE")
    market_price = float(market_price_env) if market_price_env else limit_price + LIMIT_OFFSET_PTS

    market_signal = {
        "direction": "long",
        "entry_price": market_price,
        "stop_price": market_price - 100.0,
        "take_profit": market_price + 200.0,
        # no limit_fill_bars → market order
    }

    with capsys.disabled():
        print()
        print(f"[SMOKE] Sending MKT BUY with stop @ {market_signal['stop_price']:.2f}...")

    ex.place_entry(market_signal, bar)
    time.sleep(2)

    with capsys.disabled():
        print("[SMOKE] Sending close order...")

    ex.place_close(label="smoke_market_close")

    with capsys.disabled():
        print("[SMOKE] Close order sent.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print("    You should see a filled MKT BUY and a subsequent close fill in the activity log.")
        print("    ENTER = both fills visible (pass)  |  'fail' = fills missing  |  'skip' = skip check")
        response3 = input("    > ").strip().lower()

    if response3 == "fail":
        pytest.fail("Market order: fills not visible in Tradovate activity log")
    elif response3 not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response3!r}")

    ex.stop()

    with capsys.disabled():
        print()
        print("[SMOKE] Test complete. Connection to PickMyTrade is working.")
