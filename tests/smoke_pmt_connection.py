# tests/smoke_pmt_connection.py
# Manual smoke tests for PickMyTrade connectivity.
#
# test_pmt_limit_order_place_and_cancel
#   Sends a stop sell far below the current market price (so it cannot be filled),
#   moves it to a second unrealistic price via modify_stop_entry,
#   pauses for user verification at each step, then cancels it via a close order.
#
# test_pmt_stop_entry_via_strategy_pipeline
#   Runs SessionPipeline with synthetic bars crafted to produce a new-stop-entry
#   (SELL STOP far below current market → safe pending order), then market-closes.
#   The emit_fn mirrors SmtV2Dispatcher._emit() exactly, including the confirmation_bar
#   state read, to verify the full strategy → emit → PMT dispatch path end-to-end.
#   Use this test to diagnose why stop-entry signals appear in events.jsonl but do
#   not reach the PMT executor.
#
# STP order geometry:
#   SELL STOP must be placed BELOW current market (triggers when price falls to it).
#   BUY  STOP must be placed ABOVE current market (triggers when price rises to it).
#   Both tests use direction="short" so the entry far below market is always pending.
#
# Usage:
#   python -m pytest tests/smoke_pmt_connection.py -v -s
#   (the -s flag is required — the tests prompt for user input)
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


# ── Tests ─────────────────────────────────────────────────────────────────────

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
        # Default: 500 pts below a conservative floor — this will never fill in normal conditions
        limit_price = 15000.0

    # direction="short": SELL STOP at limit_price (below market) is a valid pending order.
    # A BUY STOP must be placed ABOVE market — never below — so long would be rejected here.
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
        print(f"[SMOKE] Moving stop entry from {limit_price:.2f} → {moved_price:.2f} via modify_stop_entry...")

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


def test_pmt_stop_entry_via_strategy_pipeline(tmp_path, monkeypatch, capsys):
    """
    E2E smoke: run SessionPipeline with synthetic bars that produce new-stop-entry
    (SELL STP far below current market — safe, pending only), then market-close.

    Mirrors the exact dispatch path used in live trading:
      strategy.run_strategy → emit_fn reads confirmation_bar from smt_state
      → place_entry sends SELL STP to PMT → place_close cancels it.

    Use this test to diagnose why stop-entry signals appear in events.jsonl but
    do not reach the PMT executor (the emit_fn here is a faithful copy of
    SmtV2Dispatcher._emit() in automation/main.py).

    Bar sequence (direction="down", all prices ~21000, far below live MNQ ~29000+):
      09:20-09:24  neutral bars — strategy blocked before 9:30
      09:25-09:29  bullish 5m window: o=21000→c=21020, body=20pts ≤ 25pt limit
                   → confirmation bar for short; SL = min(high, body_high+15) = 21025
      09:30        5m boundary: open=21018, approach=18≥15 → SELL STP at 21000

    The SELL STP at 21000 is ~8000+ pts below live market and cannot fill.
    """
    _requires_smoke_env()

    import copy
    import smt_state as _ss
    import hypothesis as _hyp_mod
    import trend as _trend_mod
    import daily as _daily_mod

    # Redirect smt_state paths to tmp_path so we don't corrupt the live session state
    monkeypatch.setattr(_ss, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(_ss, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(_ss, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(_ss, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(_ss, "POSITION_PATH",   tmp_path / "position.json")

    # Seed initial state: direction=down, no pending position, high ATH floor
    _ss.save_global({
        **_ss.DEFAULT_GLOBAL,
        "session_ath":   30000.0,
        "all_time_high": 30000.0,
        "confidence":    "medium",
    })
    _ss.save_daily({**_ss.DEFAULT_DAILY, "date": "2025-11-14", "estimated_dir": "down"})
    _ss.save_hypothesis({
        **_ss.DEFAULT_HYPOTHESIS,
        "direction":  "down",
        "formed_at":  "2025-11-14T09:10:00-05:00",
    })
    _ss.save_position(copy.deepcopy(_ss.DEFAULT_POSITION))

    # Suppress hypothesis, trend, daily to isolate the strategy → emit → PMT path
    monkeypatch.setattr(_hyp_mod,   "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_trend_mod, "run_trend",      lambda *a, **kw: None)
    monkeypatch.setattr(_daily_mod, "run_daily",      lambda *a, **kw: None)

    ex = _make_executor()
    ex.start()

    emitted: list[dict] = []

    def emit_fn(sig: dict) -> None:
        """Faithful copy of SmtV2Dispatcher._emit() for the new-stop-entry path."""
        emitted.append(copy.copy(sig))
        if sig.get("kind") != "new-stop-entry":
            return
        direction_v2 = sig.get("direction", "none")
        if direction_v2 == "none":
            print(f"[SMOKE-EMIT] skipped: direction=none", flush=True)
            return
        direction = "long" if direction_v2 == "up" else "short"
        pos = _ss.load_position()
        conf_bar = pos.get("confirmation_bar", {})
        stop_key = "body_low" if direction_v2 == "up" else "body_high"
        stop = conf_bar.get(stop_key)
        if stop is None:
            print(f"[SMOKE-EMIT] skipped: confirmation_bar missing {stop_key} — conf_bar={conf_bar}", flush=True)
            return
        pmt_signal = {
            "direction":      direction,
            "entry_price":    float(sig["price"]),
            "stop_price":     float(stop),
            "stop_fill_bars": 1,
        }
        print(f"[SMOKE-EMIT] new-stop-entry → place_entry({pmt_signal})", flush=True)
        ex.place_entry(pmt_signal, _fake_bar(float(sig["price"])))

    from session_pipeline import SessionPipeline
    empty_1m = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    pipeline = SessionPipeline(empty_1m, empty_1m, emit_fn)
    pipeline._daily_triggered = True  # bypass on_session_start (daily, global, ATH seeding)

    tz = "America/New_York"
    date_str = "2025-11-14"

    # (time_str, open, high, low, close)
    raw_bars = [
        # 09:20-09:24: neutral — run_strategy blocked before 09:30
        (f"{date_str} 09:20", 21015, 21020, 21010, 21015),
        (f"{date_str} 09:21", 21015, 21020, 21010, 21015),
        (f"{date_str} 09:22", 21015, 21020, 21010, 21015),
        (f"{date_str} 09:23", 21015, 21020, 21010, 21015),
        (f"{date_str} 09:24", 21015, 21020, 21010, 21015),
        # 09:25-09:29: bullish 5m window (direction=down → _find_last_bar looks for bullish opp bar)
        #   window[09:25, 09:30): first_open=21000, last_close=21020 → bullish (c > o) ✓
        #   body_high=21020, body_low=21000, high=21025, low=20995
        #   body size = 20 pts ≤ 25 pt MAX_CONFIRMATION_BODY_PTS ✓
        #   SL = min(21025, 21020+15) = 21025
        (f"{date_str} 09:25", 21000, 21010, 20995, 21004),
        (f"{date_str} 09:26", 21004, 21010, 21000, 21008),
        (f"{date_str} 09:27", 21008, 21015, 21005, 21012),
        (f"{date_str} 09:28", 21012, 21020, 21008, 21016),
        (f"{date_str} 09:29", 21016, 21025, 21014, 21020),
        # 09:30: 5m boundary, triggers run_strategy with fill_check_only=False
        #   approach = bar_open - body_low = 21018 - 21000 = 18 ≥ 15 (stop, not market) ✓
        #   bar_mid = (21022+21010)/2 = 21016; SL-bar_mid = 21025-21016 = 9 ≥ 5 ✓
        #   CPR (short) = (high-close)/(high-low) = 7/12 = 0.58 ≥ 0.40 ✓
        #   → new-stop-entry at price=21000 (SELL STP ~8000 pts below live market)
        (f"{date_str} 09:30", 21018, 21022, 21010, 21015),
    ]

    today_rows = [
        {
            "ts":     pd.Timestamp(f"{ts_str}:00", tz=tz),
            "Open":   float(o), "High": float(h),
            "Low":    float(l), "Close": float(c),
            "Volume": 100.0,
        }
        for ts_str, o, h, l, c in raw_bars
    ]
    today_df = (
        pd.DataFrame(today_rows)
        .set_index("ts")
        .rename_axis(None)
    )

    for ts_str, o, h, l, c in raw_bars:
        ts = pd.Timestamp(f"{ts_str}:00", tz=tz)
        bar_row = pd.Series(
            {"Open": float(o), "High": float(h), "Low": float(l), "Close": float(c), "Volume": 100.0},
            name=ts,
        )
        pipeline.on_1m_bar(ts, bar_row, bar_row, today_df, today_df)

    time.sleep(3)

    stop_signals = [s for s in emitted if s.get("kind") == "new-stop-entry"]

    with capsys.disabled():
        if not stop_signals:
            pytest.fail(
                f"Pipeline never emitted new-stop-entry.\n"
                f"All emitted: {emitted}\n"
                f"position.json: {_ss.load_position()}"
            )
        entry_price = stop_signals[0]["price"]
        print(f"\n[SMOKE] SessionPipeline emitted new-stop-entry @ {entry_price:.2f} (SELL STP)")
        print(f"[SMOKE] This is ~8000 pts below live market — order is pending only, cannot fill")
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
        print("[SMOKE] Test complete. Strategy → emit → PMT dispatch path verified.")
