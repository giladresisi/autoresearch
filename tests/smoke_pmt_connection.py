# tests/smoke_pmt_connection.py
# Manual smoke tests for PickMyTrade connectivity.
#
# test_pmt_limit_order_place_and_cancel
#   Sends a stop sell far below the current market price (so it cannot be filled),
#   moves it to a second unrealistic price via modify_stop_entry,
#   pauses for user verification at each step, then cancels it via a close order.
#
# test_pmt_update_sl_after_stop_fill
#   Verifies the post-fill SL-attach flow: MKT SELL with sl placeholder,
#   update_stop_loss replaces the placeholder with the real SL, then closes.
#
# test_pmt_stop_entry_via_strategy_pipeline
#   Drives the REAL strategy through SessionPipeline (pause->resume force-eval, as in
#   tests/test_session_pipeline_resume_entry.py) to a new-stop-entry that is a SELL STOP
#   far below the live market (safe, pending only), then market-closes. The emit_fn
#   builds the same pmt_signal live_orders.place_stop_entry builds, so the full
#   strategy -> emit -> PMT dispatch path is exercised end-to-end.
#
# STP order geometry:
#   SELL STOP must be placed BELOW current market (triggers when price falls to it).
#   BUY  STOP must be placed ABOVE current market (triggers when price rises to it).
#   Both tests use direction="short" so the entry far below market is always pending.
#
# Usage:
#   python -m pytest tests/smoke_pmt_connection.py -v -s
#   (the -s flag is required - the tests prompt for user input)
#
# Prerequisites: PMT_WEBHOOK_URL, PMT_API_KEY, TRADING_ACCOUNT_ID in .env or shell.
# The tests will NOT run unless SMOKE_PMT=1 is also set, to prevent accidental execution.

import os
import time

import pandas as pd
import pytest
from dotenv import load_dotenv

load_dotenv()

SMOKE_GUARD = "SMOKE_PMT"
LIMIT_OFFSET_PTS = 500.0    # place sell stop this many points below current price
LIMIT_MOVE_PTS   = 100.0    # move the stop by this many additional points for the modify step
SMOKE_PIPELINE_MAX_PRICE = 25000.0  # pipeline test: never dispatch an entry above this (must sit far below live MNQ)


def _requires_smoke_env():
    if not os.environ.get(SMOKE_GUARD):
        pytest.skip(f"Set {SMOKE_GUARD}=1 to run PMT connection smoke test")


def _make_executor():
    from execution.pickmytrade import PickMyTradeExecutor
    account_ids = [s.strip() for s in os.environ.get("TRADING_ACCOUNT_IDS", "").split(",") if s.strip()]
    return PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_ids=account_ids,
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


# -- Tests ---------------------------------------------------------------------

def test_pmt_limit_order_place_and_cancel(capsys):
    """
    Smoke test: place a STP SELL 500 pts below market, verify in Tradovate, then cancel.
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
        # Default: 500 pts below a conservative floor - this will never fill in normal conditions
        limit_price = 15000.0

    # direction="short": SELL STOP at limit_price (below market) is a valid pending order.
    # A BUY STOP must be placed ABOVE market - never below - so long would be rejected here.
    signal = {
        "direction": "short",
        "entry_price": limit_price,
        "stop_price": limit_price + 50.0,
        "take_profit": limit_price - 100.0,
        "stop_fill_bars": 999,   # marks this as a stop entry order
    }

    bar = _fake_bar(limit_price)

    with capsys.disabled():
        print(f"\n[SMOKE] Sending STP SELL @ {limit_price:.2f} to PickMyTrade...")

    rec = ex.place_entry(signal, bar)
    time.sleep(3)  # give the pool thread time to dispatch the HTTP request

    with capsys.disabled():
        print(f"[SMOKE] Order dispatched. FillRecord: order_id={rec.order_id}, "
              f"fill_price={rec.fill_price}, status={rec.status}")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    You should see a pending STP SELL order for {signal['entry_price']:.2f}.")
        print("    ENTER = order visible (pass)  |  'fail' = order not visible  |  'skip' = skip check")
        response = input("    > ").strip().lower()

    if response == "fail":
        pytest.fail("Order placement: order not visible in Tradovate after placement")
    elif response not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response!r}")

    # Move the stop to a second unrealistic price via modify_stop_entry
    moved_price = limit_price - LIMIT_MOVE_PTS
    new_signal = {**signal, "entry_price": moved_price}

    with capsys.disabled():
        print()
        print(f"[SMOKE] Moving stop entry from {limit_price:.2f} -> {moved_price:.2f} via modify_stop_entry...")

    ex.modify_stop_entry(signal, new_signal, bar)
    time.sleep(3)  # close is synchronous; give pool time to dispatch the re-place

    with capsys.disabled():
        print(f"[SMOKE] Modify dispatched.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    The STP SELL order should now show price {moved_price:.2f} (was {limit_price:.2f}).")
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
        print("    The pending STP SELL order should now be gone.")
        print("    ENTER = order gone (pass)  |  'fail' = order still visible  |  'skip' = skip check")
        response2 = input("    > ").strip().lower()

    if response2 == "fail":
        pytest.fail("Order cancellation: order still visible in Tradovate after close")
    elif response2 not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {response2!r}")

    # Market order step: buy 1 contract at market with a stop 100 pts below.
    # SMOKE_MARKET_PRICE can be set to the current MNQ price for an accurate stop;
    # if omitted, we use limit_price + LIMIT_OFFSET_PTS as a rough estimate.
    # The 'price' field on MKT orders is informational - actual fill is at market.
    market_price_env = os.environ.get("SMOKE_MARKET_PRICE")
    market_price = float(market_price_env) if market_price_env else limit_price + LIMIT_OFFSET_PTS

    market_signal = {
        "direction": "long",
        "entry_price": market_price,
        "stop_price": market_price - 100.0,
        "take_profit": market_price + 200.0,
        # no limit_fill_bars -> market order
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


def test_pmt_update_sl_after_stop_fill(capsys):
    """
    Smoke test: verify that update_stop_loss attaches a real SL to a live open position.

    Key findings from investigation:
      - update_stop_loss only works on FILLED (open) positions, not pending orders.
      - The initial entry order (MKT or STP) must include a non-zero sl for Tradovate
        to create a stop-order anchor at fill time; update_stop_loss then replaces it
        with the real SL price.

    Flow:
      1. MKT SELL with initial_sl - opens a short position.
      2. update_stop_loss with updated_sl (different value) - Tradovate should show the change.
      3. MKT close.

    Requires SMOKE_PMT=1 and SMOKE_SL_PRICE set to an SL price above current MNQ
    (e.g. current price + 100). The update uses SMOKE_SL_PRICE - 50 so the change
    is visible in Tradovate.
    """
    _requires_smoke_env()

    sl_price_env = os.environ.get("SMOKE_SL_PRICE")
    if not sl_price_env:
        pytest.skip("Set SMOKE_SL_PRICE to an SL price above current MNQ (e.g. current + 100) to run this test")
    initial_sl = float(sl_price_env)
    updated_sl = initial_sl - 50.0   # tighter stop - visually distinct from initial_sl
    bar = _fake_bar(initial_sl)

    ex = _make_executor()
    ex.start()

    # Step 1: MKT SELL with initial_sl - creates the stop-order anchor Tradovate needs
    mkt_signal = {
        "direction":   "short",
        "entry_price": 0.0,      # ignored by PMT for MKT orders
        "stop_price":  initial_sl,
        # no stop_fill_bars -> MKT order
    }

    with capsys.disabled():
        print(f"\n[SMOKE] Sending MKT SELL (initial_sl={initial_sl:.2f})...")

    ex.place_entry(mkt_signal, bar)
    time.sleep(1)   # let entry HTTP request clear before updating SL

    # Step 2: replace initial_sl with updated_sl - expect Tradovate to show the change
    with capsys.disabled():
        print(f"[SMOKE] Calling update_stop_loss with updated_sl={updated_sl:.2f} (was {initial_sl:.2f})...")

    status, body = ex.update_stop_loss({"direction": "short", "stop_price": updated_sl}, bar)

    with capsys.disabled():
        print(f"[SMOKE] PMT response: HTTP {status} - {body[:300]}")

    time.sleep(1)   # let SL update settle before closing

    # Step 3: close
    with capsys.disabled():
        print("[SMOKE] Sending MKT close...")

    ex.place_close(label="smoke_mkt_close")
    time.sleep(2)

    with capsys.disabled():
        print("[SMOKE] All three orders dispatched.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    SL should have changed from {initial_sl:.2f} -> {updated_sl:.2f} while open.")
        print("    ENTER = SL change visible (pass)  |  'fail' = SL missing or unchanged  |  'skip' = skip check")
        resp = input("    > ").strip().lower()

    if resp == "fail":
        pytest.fail(f"update_stop_loss: SL did not change from {initial_sl:.2f} to {updated_sl:.2f}")
    elif resp not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {resp!r}")

    ex.stop()

    with capsys.disabled():
        print()
        print("[SMOKE] Test complete.")


def test_pmt_stop_entry_via_strategy_pipeline(tmp_path, monkeypatch, capsys):
    """
    E2E smoke: drive the REAL strategy through SessionPipeline to a new-stop-entry
    (SELL STP far below the live market - safe, pending only), then market-close.

    Live path mirrored:
      strategy.run_strategy -> pipeline emit -> live_orders.dispatch -> place_stop_entry
      -> PickMyTradeExecutor.place_entry (SELL STP); place_close then cancels it.

    Harness (short-side mirror of tests/test_session_pipeline_resume_entry.py): the
    pipeline starts paused, a DOWN hypothesis is hand-set flat, a small bullish 5m window
    [09:45, 09:50) is the opposite-direction confirmation bar, and the pause->resume
    transition arms a force-eval so the strategy evaluates on the very next bar.

    Bar geometry (all ~21000, far below live MNQ ~29000+):
      window 09:45..09:49: first open 20990 -> last close 21000 (bullish, body 10 <= 25)
      resume bar 09:50:    open 20998, low 20992 (> entry -> resting STP, not market),
                           close 20993 -> short CPR (21000-20993)/(21000-20992) = 0.875 >= 0.40
      entry = min(body_low 20990, open - MIN_APPROACH_PTS 10 = 20988) = 20988 (SELL STP)
    """
    _requires_smoke_env()

    import copy
    import paths as _paths
    import smt_state as _ss
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    from session_pipeline import SessionPipeline

    # Isolate ALL state (incl. the pause sentinel under general_live_dir()) in tmp_path.
    monkeypatch.setattr(_paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(_ss, "_IN_MEMORY", False)

    # No-op the passes that would overwrite the hand-set state; run_strategy runs FOR REAL.
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    tz = "America/New_York"

    def _bars(start, o, h, lo, c):
        idx = pd.date_range(start, periods=len(o), freq="1min", tz=tz)
        return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c,
                             "Volume": [100.0] * len(o)}, index=idx)

    def _flat(start, n, base):
        return _bars(start, [base] * n, [base + 10.0] * n, [base - 10.0] * n, [base + 2.0] * n)

    ex = _make_executor()
    ex.start()

    emitted: list[dict] = []

    def emit_fn(sig: dict) -> None:
        """Same mapping as live_orders.dispatch -> place_stop_entry for new-stop-entry."""
        emitted.append(copy.copy(sig))
        if sig.get("kind") != "new-stop-entry":
            return
        direction_v2 = sig.get("direction", "none")
        stop = sig.get("stop")
        if direction_v2 == "none" or stop is None:
            print(f"[SMOKE-EMIT] skipped: direction={direction_v2} stop={stop}", flush=True)
            return
        direction = "long" if direction_v2 == "up" else "short"
        price = float(sig["price"])
        # Safety: only a SELL STP far below the live market may reach the broker. A BUY STP
        # below market (or anything near market) would fill immediately.
        if direction != "short" or price > SMOKE_PIPELINE_MAX_PRICE:
            pytest.fail(f"refusing to dispatch unsafe entry: {direction} @ {price:.2f}")
        pmt_signal = {
            "direction":      direction,
            "entry_price":    price,
            "stop_price":     float(stop),
            "stop_fill_bars": 1,
        }
        print(f"[SMOKE-EMIT] new-stop-entry -> place_entry({pmt_signal})", flush=True)
        ex.place_entry(pmt_signal, _fake_bar(price))

    pipeline = SessionPipeline(_flat("2025-11-13 09:20", 5, 21000.0),
                               _flat("2025-11-13 09:20", 5, 3000.0), emit_fn)
    monkeypatch.setattr(pipeline, "_run_smt_v2_detection", lambda *a, **kw: [])

    # Start paused (no late-start arm), then hand-set a DOWN hypothesis, flat, high ATH.
    _ss.pause_path().write_text("paused")
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:20", tz=tz),
                              _flat("2025-11-14 09:20", 1, 21000.0))
    _ss.save_hypothesis({**_ss.DEFAULT_HYPOTHESIS, "direction": "down"})
    _ss.save_position(copy.deepcopy(_ss.DEFAULT_POSITION))
    _ss.save_global({**_ss.DEFAULT_GLOBAL, "all_time_high": 30000.0})

    today_mnq = _bars("2025-11-14 09:45",
                      o=[20990, 20994, 20995, 20996, 20997, 20998],
                      h=[20996, 20998, 20999, 21000, 21002, 21000],
                      lo=[20988, 20990, 20991, 20992, 20993, 20992],
                      c=[20992, 20994, 20995, 20997, 21000, 20993])
    today_mes = _flat("2025-11-14 09:45", 6, 3000.0)
    mnq_bar = pd.Series({"Open": 20998.0, "High": 21000.0, "Low": 20992.0, "Close": 20993.0})
    mes_bar = pd.Series({"Open": 3000.0, "High": 3008.0, "Low": 2998.0, "Close": 3005.0})

    # 09:48, still paused: entry work suppressed (_prev_paused -> True).
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:48", tz=tz), mnq_bar, mes_bar,
                       today_mnq.iloc[:4], today_mes.iloc[:4])
    assert pipeline._prev_paused is True
    # Resume: remove the sentinel; the next bar force-evaluates the last 5m window.
    _ss.pause_path().unlink()
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz=tz), mnq_bar, mes_bar,
                       today_mnq, today_mes)

    time.sleep(3)  # give the pool thread time to dispatch the HTTP request

    stop_signals = [s for s in emitted if s.get("kind") == "new-stop-entry"]

    with capsys.disabled():
        if not stop_signals:
            pytest.fail(
                f"Pipeline never emitted new-stop-entry.\n"
                f"All emitted: {emitted}\n"
                f"position.json: {_ss.load_position()}"
            )
        entry_price = float(stop_signals[0]["price"])
        print(f"\n[SMOKE] SessionPipeline emitted new-stop-entry @ {entry_price:.2f} (SELL STP)")
        print("[SMOKE] This is far below the live market - order is pending only, cannot fill")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print(f"    You should see a pending STP SELL order at {entry_price:.2f}.")
        print("    ENTER = order visible (pass)  |  'fail' = not visible  |  'skip' = skip check")
        resp1 = input("    > ").strip().lower()

    if resp1 == "fail":
        pytest.fail(f"Stop-entry: STP SELL not visible in Tradovate at {entry_price:.2f}")
    elif resp1 not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {resp1!r}")

    with capsys.disabled():
        print("\n[SMOKE] Sending market close to cancel the STP...")

    ex.place_close(label="smoke_pipeline_close")
    time.sleep(2)

    with capsys.disabled():
        print("[SMOKE] Close sent.")
        print()
        print(">>> CHECK YOUR TRADOVATE ACCOUNT NOW <<<")
        print("    The STP SELL order should now be gone.")
        print("    ENTER = order gone (pass)  |  'fail' = order still visible  |  'skip' = skip check")
        resp2 = input("    > ").strip().lower()

    if resp2 == "fail":
        pytest.fail("Close: STP SELL still visible in Tradovate after market close")
    elif resp2 not in ("", "skip"):
        pytest.fail(f"Unrecognised input: {resp2!r}")

    ex.stop()

    with capsys.disabled():
        print()
        print("[SMOKE] Test complete. Strategy -> emit -> PMT dispatch path verified.")
