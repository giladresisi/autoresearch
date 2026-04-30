# orchestrator/main.py
# Daemon entry point: waits for trading sessions, runs signal_smt.py, and triggers post-session summarization.
import datetime
import os as _os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from orchestrator.output import FileSink, JsonlFileSink, OutputChannel, StdoutSink
from orchestrator.process import ProcessManager
from orchestrator.relay import SessionRelay
from orchestrator.scheduler import get_et_now, is_trading_day, next_session_open
from orchestrator.summarizer import Summarizer

LIVE_TRADING = _os.environ.get("LIVE_TRADING", "false").lower() == "true"

_ET = ZoneInfo("America/New_York")
_SIGNAL_SMT = Path(__file__).parent.parent / "signal_smt.py"
_SESSIONS_DIR = Path(__file__).parent.parent / "sessions"
_SESSION_OPEN = datetime.time(9, 0)
_SESSION_GRACE_END = datetime.time(13, 35)


def _make_session_channels(date: datetime.date) -> tuple[OutputChannel, OutputChannel]:
    """Create session directory and return (signal_channel, orch_channel)."""
    session_dir = _SESSIONS_DIR / date.isoformat()
    session_dir.mkdir(parents=True, exist_ok=True)

    signal_ch = OutputChannel()
    signal_ch.add_sink(StdoutSink())
    signal_ch.add_sink(FileSink(session_dir / "signals.log"))
    signal_ch.add_sink(JsonlFileSink(session_dir / "events.jsonl"))

    orch_ch = OutputChannel()
    orch_ch.add_sink(StdoutSink())
    orch_ch.add_sink(FileSink(session_dir / "orchestrator.log"))

    return signal_ch, orch_ch


def _sleep_until(target: datetime.datetime, label: str) -> None:
    now = get_et_now()
    delay = (target - now).total_seconds()
    if delay > 0:
        hours = delay / 3600
        print(f"[ORCH] Sleeping {hours:.1f}h until {label}", flush=True)
        time.sleep(delay)


def run(summarizer: Summarizer | None = None) -> None:
    """Main daemon loop. Ctrl+C exits cleanly; signal_smt.py is terminated if active."""
    if summarizer is None:
        summarizer = Summarizer()
    try:
        while True:
            now = get_et_now()
            today = now.date()

            if not is_trading_day(today):
                _sleep_until(next_session_open(now), "next trading session")
                continue

            session_open_dt = datetime.datetime(today.year, today.month, today.day, 9, 0, tzinfo=_ET)
            grace_end_dt = datetime.datetime(today.year, today.month, today.day, 13, 35, tzinfo=_ET)

            if now < session_open_dt:
                _sleep_until(session_open_dt, "session open 09:00 ET")
                continue

            if now >= grace_end_dt:
                _sleep_until(next_session_open(now), "next trading session")
                continue

            # Run session
            signal_ch, orch_ch = _make_session_channels(today)
            relay = SessionRelay(signal_ch)
            if LIVE_TRADING:
                signal_cmd = ["uv", "run", "python", "-m", "automation.main"]
            else:
                signal_cmd = _SIGNAL_SMT
            print(f"[orchestrator] mode={'LIVE_TRADING' if LIVE_TRADING else 'signal'}", flush=True)
            ProcessManager(signal_cmd, relay, orch_ch).run_session(today)
            relay.write_trades_tsv(_SESSIONS_DIR / today.isoformat() / "trades.tsv", today)
            summarizer.run(today, _SESSIONS_DIR / today.isoformat() / "signals.log", _SESSIONS_DIR, signal_ch)
            _sleep_until(next_session_open(get_et_now()), "next trading session")
    except KeyboardInterrupt:
        print("\n[ORCH] Shutting down.", flush=True)
        sys.exit(0)


def _check_setup() -> None:
    """Validate environment and print OK, then exit 0; exit 1 if setup fails."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ORCH] ERROR: ANTHROPIC_API_KEY environment variable is required", flush=True)
        sys.exit(1)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        Summarizer()
    except RuntimeError as e:
        print(f"[ORCH] ERROR: {e}", flush=True)
        sys.exit(1)
    print("[ORCH] Setup OK", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check_setup()
    else:
        run()
