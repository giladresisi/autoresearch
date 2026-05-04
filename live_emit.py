# live_emit.py
# Shared v2 signal emit callback used by all live SMT dispatch paths (signal_smt, automation).
import json


def emit_v2_signal(sig: dict) -> None:
    """Print a v2 signal dict as a JSON line to stdout."""
    print(json.dumps(sig, sort_keys=True), flush=True)
