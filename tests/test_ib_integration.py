# tests/test_ib_integration.py
# Live integration tests — require IB Gateway running at 127.0.0.1:4002.
# Run with: pytest tests/test_ib_integration.py -v -m integration
# These are NOT run in the standard test suite (marked @pytest.mark.integration).
import subprocess
import sys
import threading
import time

import psutil
import pytest

from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource


def _make_source(tmp_path):
    return IbRealtimeSource(
        host="127.0.0.1",
        port=4002,
        client_id=15,
        mnq_conid="770561201",
        mes_conid="770561194",
        bar_data_dir=tmp_path,
        on_bar=lambda *_: None,
        max_retries=1,
        retry_delay_s=0,
    )


@pytest.mark.integration
def test_ib_start_seeds_bars_no_gap_fill(tmp_path, caplog):
    """IbRealtimeSource connects, seeds bars via IB 3-day seed, and [gap_fill] never appears."""
    import logging
    caplog.set_level(logging.DEBUG)

    src = _make_source(tmp_path)
    errors = []

    def run_start():
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            src.start()
        except IbGatewayDisconnectedError:
            pass  # expected if stop() triggers the event
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=run_start, daemon=True)
    t.start()

    # Wait up to 60 s for the IB 3-day seed to populate bars
    deadline = time.time() + 60
    while time.time() < deadline:
        if not src._mnq_1m_df.empty and not src._mes_1m_df.empty:
            break
        time.sleep(1)

    mnq_count = len(src._mnq_1m_df)
    mes_count = len(src._mes_1m_df)

    src.stop()
    t.join(timeout=15)

    if errors:
        raise errors[0]

    assert mnq_count > 0, "MNQ bars not seeded — IB seed did not populate _mnq_1m_df"
    assert mes_count > 0, "MES bars not seeded — IB seed did not populate _mes_1m_df"

    gap_fill_lines = [r.message for r in caplog.records if "[gap_fill]" in r.message]
    assert not gap_fill_lines, f"[gap_fill] log lines found (should be removed): {gap_fill_lines}"

    print(f"\nMNQ bars={mnq_count}  MES bars={mes_count}")


@pytest.mark.integration
def test_ib_gateway_disconnect_raises_and_exits(tmp_path):
    """Closing IB Gateway while connected triggers IbGatewayDisconnectedError within 30 s."""
    src = _make_source(tmp_path)
    errors = []
    raised = threading.Event()

    def run_start():
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            src.start()
        except IbGatewayDisconnectedError:
            raised.set()
        except Exception as e:
            errors.append(e)
            raised.set()

    t = threading.Thread(target=run_start, daemon=True)
    t.start()

    # Wait up to 20 s for the IB seed to confirm connection before killing gateway
    deadline = time.time() + 20
    while time.time() < deadline:
        if not src._mnq_1m_df.empty:
            break
        time.sleep(1)

    assert not src._mnq_1m_df.empty, "IB source never connected — is Gateway running?"

    # Kill IB Gateway process to simulate hardware-level disconnect
    gateway_procs = [
        p for p in psutil.process_iter(["name", "cmdline"])
        if "ibgateway" in (p.info["name"] or "").lower()
        or any("ibgateway" in (c or "").lower() for c in (p.info["cmdline"] or []))
    ]
    assert gateway_procs, (
        "No ibgateway process found — cannot simulate disconnect. "
        "Start IB Gateway before running this test."
    )
    for p in gateway_procs:
        p.kill()

    # IbGatewayDisconnectedError must be raised within 30 s
    fired = raised.wait(timeout=30)
    t.join(timeout=5)

    assert fired, "IbGatewayDisconnectedError was NOT raised within 30 s of gateway kill"
    if errors:
        raise errors[0]
