# orchestrator/stop_request.py
# The stop-request sentinel files, defined ONCE. trade.py writes them; the orchestrator and
# automation.main poll them. Both sides import these names, so the writer and the reader
# cannot drift apart again (trade.py used to write a path relative to its current directory
# while the orchestrator read one anchored here — a terminate from any other directory was
# never seen).
#
# Anchored to the worktree root, not the global folder: a stop request must only ever reach
# the processes of THIS worktree (same scoping as the process-kill scans).
#
# Import-cheap by design (pathlib only): trade.py imports this in the CLI process.
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# trade.py terminate -> orchestrator.main (polled between sessions and during one).
ORCH_STOP_FILE = _ROOT / "orchestrator_stop.req"
# orchestrator / trade.py -> automation.main (polled by its stop-request watcher thread).
AUTOMATION_STOP_FILE = _ROOT / "automation_stop.req"

# How long a requester waits for automation.main to stop by itself before killing it.
AUTOMATION_STOP_WAIT_S = 15


def clear(stop_file: Path) -> None:
    """Remove a stop-request file; never raises."""
    try:
        stop_file.unlink()
    except OSError:
        pass
