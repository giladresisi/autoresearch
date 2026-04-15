#!/usr/bin/env python
# test_live_run.py
# 5-second smoke test for signal_smt.py.
# Bypasses the IB gap-fill and runs the live tick subscription loop for 5 seconds,
# then calls loop.stop() directly — which triggers the try/finally in main() to
# call _ib.disconnect() and release the clientId in IB Gateway.
import asyncio
import threading
import time

import ib_insync.util as _ibutil
import signal_smt


def _no_op_gap_fill(mnq_df, mes_df):
    print("[test] Gap-fill skipped (test mode) — using cached parquets as-is")
    return mnq_df, mes_df


signal_smt._gap_fill_1m = _no_op_gap_fill

# ── Capture the event loop when util.run() is called with no awaitables ──────
# util.run() is called two ways in ib_insync:
#   util.run(coroutine)  →  loop.run_until_complete()  (inside connect / reqHistoricalData)
#   util.run()           →  loop.run_forever()           (the top-level blocking call in main())
# We only care about the second form — that's where the subscription loop lives.
_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()
_orig_run = _ibutil.run


def _patched_run(*awaitables, **kwargs):
    global _loop
    if not awaitables:
        # Top-level blocking call — capture the loop before blocking
        _loop = asyncio.get_event_loop()
        _loop_ready.set()
    return _orig_run(*awaitables, **kwargs)


_ibutil.run = _patched_run


def _timer_thread(seconds: float) -> None:
    """Wait for the top-level event loop to start, then stop it after `seconds`."""
    if not _loop_ready.wait(timeout=30):
        print("[test] timeout waiting for event loop — is IB Gateway running?")
        return
    print(f"[test] subscriptions live — running for {seconds:.0f}s")
    time.sleep(seconds)
    print(f"\n[test] {seconds:.0f}s elapsed — calling loop.stop() for graceful disconnect")
    # Scheduling loop.stop() via call_soon_threadsafe ensures it runs inside the
    # event loop thread, allowing the try/finally in main() to call _ib.disconnect().
    _loop.call_soon_threadsafe(_loop.stop)


t = threading.Thread(target=_timer_thread, args=(5.0,), daemon=True)
t.start()

signal_smt.main()
print("[test] main() returned cleanly — IB connections released")
