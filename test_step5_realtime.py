"""
Step 5: Validate IbRealtimeSource realtime path end-to-end without the orchestrator.

Calls src.start() in a background thread (gap-fill patched to no-op so we go
straight to the realtime loop). Runs 75s — long enough to cross at least one
minute boundary — then calls stop() and verifies:

  1. Ticks received for both MNQ and MES (on_bar called)
  2. At least one completed 1m bar flushed (requires crossing minute boundary)
  3. 1m parquet on disk matches in-memory state after flush
  4. Thread exits cleanly after stop()
  5. No runtime errors
"""
import os, threading, time
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

from data.ib_realtime import IbRealtimeSource

HOST    = os.environ.get("IB_HOST", "127.0.0.1")
PORT    = int(os.environ.get("IB_PORT", "4002"))
MNQ_CON = os.environ.get("MNQ_CONID", "770561201")
MES_CON = os.environ.get("MES_CONID", "770561194")
BAR_DIR = Path("data")

on_bar_calls   = []
on_1m_complete = []

def on_bar(bar_row, mes_partial):
    on_bar_calls.append(bar_row)

def on_1m(bars):
    on_1m_complete.append(bars)
    ts = bars[0].date if bars else "?"
    print(f"  [1m callback] bar at {ts}  total={len(on_1m_complete)}", flush=True)

src = IbRealtimeSource(
    host=HOST, port=PORT, client_id=20,
    mnq_conid=MNQ_CON, mes_conid=MES_CON,
    bar_data_dir=BAR_DIR,
    on_bar=on_bar,
    on_bar_1m_complete=on_1m,
)

# Load parquets so we know the before-counts.
src._load_parquets()
mnq_1m_before = len(src._mnq_1m_df)
mes_1m_before = len(src._mes_1m_df)
mnq_1s_before = len(pd.read_parquet(BAR_DIR / "MNQ_1s.parquet")) if (BAR_DIR / "MNQ_1s.parquet").exists() else 0
mes_1s_before = len(pd.read_parquet(BAR_DIR / "MES_1s.parquet")) if (BAR_DIR / "MES_1s.parquet").exists() else 0
print(f"1m parquet before: MNQ={mnq_1m_before}  MES={mes_1m_before}", flush=True)
print(f"1s parquet before: MNQ={mnq_1s_before}  MES={mes_1s_before}", flush=True)

# Patch gap-fill to no-op so start() goes straight to the realtime loop.
src._gap_fill_1s_ib = lambda: print("[test] gap fill skipped", flush=True)

import traceback as _tb
errors = []
def run():
    try:
        src.start()
    except Exception as e:
        errors.append(_tb.format_exc())

t = threading.Thread(target=run, daemon=True)
t.start()

print("Realtime feed running. Waiting 75s for ticks and a minute boundary...", flush=True)
for elapsed in range(75):
    time.sleep(1)
    if elapsed % 15 == 14:
        print(f"  t={elapsed+1}s  on_bar calls={len(on_bar_calls)}  1m flushes={len(on_1m_complete)}", flush=True)

print("\nCalling stop()...", flush=True)
src.stop()
t.join(timeout=10)

src._parquet_executor.shutdown(wait=True)

mnq_1m_after = len(src._mnq_1m_df)
mes_1m_after = len(src._mes_1m_df)
mnq_1m_disk  = len(pd.read_parquet(BAR_DIR / "MNQ_1m.parquet"))
mes_1m_disk  = len(pd.read_parquet(BAR_DIR / "MES_1m.parquet"))
mnq_1s_disk  = len(pd.read_parquet(BAR_DIR / "MNQ_1s.parquet"))
mes_1s_disk  = len(pd.read_parquet(BAR_DIR / "MES_1s.parquet"))

print(f"\n--- Results ---", flush=True)
results = {
    "on_bar called (1s bars)":  len(on_bar_calls) > 0,
    "1m bar flushed":           len(on_1m_complete) > 0,
    "MNQ 1m on-disk matches":   mnq_1m_disk == mnq_1m_after,
    "MES 1m on-disk matches":   mes_1m_disk == mes_1m_after,
    "MNQ 1s on-disk grew":      mnq_1s_disk > mnq_1s_before,
    "MES 1s on-disk grew":      mes_1s_disk > mes_1s_before,
    "no runtime errors":        len(errors) == 0,
    "thread exited cleanly":    not t.is_alive(),
}
all_pass = True
for label, ok in results.items():
    status = "PASS" if ok else "FAIL"
    print(f"  {label}: {status}", flush=True)
    if not ok:
        all_pass = False

if errors:
    print(f"\nErrors: {errors}", flush=True)
print(f"\n  on_bar calls (1s bars): {len(on_bar_calls)}", flush=True)
print(f"  1m bar flushes:         {len(on_1m_complete)}", flush=True)
print(f"\nOverall: {'PASS' if all_pass else 'FAIL'}", flush=True)
