# orchestrator/summarizer.py
# Post-session summarizer: builds a prompt from session log + prior summaries, calls Claude API,
# writes summary.md, and echoes output to the session channel.
import datetime
import os
from pathlib import Path

import anthropic

from orchestrator.output import OutputChannel

_MODEL = "claude-sonnet-4-6"
_MAX_PREVIOUS = 5

_SYSTEM_PROMPT = """You are a trading session analyst for an SMT divergence futures strategy \
(MNQ/MES). The strategy trades one position per session, 09:00–13:30 ET.
Produce output in exactly three sections with these headers:

## METRICS
## NARRATIVE
## RECOMMENDATIONS"""

_USER_TEMPLATE = """\
Session: {date} ({weekday})
Parameters: SESSION_START=09:00 ET, SESSION_END=13:30 ET, ENTRY_SLIPPAGE_TICKS=2

=== SESSION LOG ===
{session_log}

=== PREVIOUS SESSION SUMMARIES (last {n_prev} sessions) ===
{prev_summaries}

Produce:
1. METRICS — signals fired, trade taken (y/n), exit type (tp/stop/session_end/none), \
P&L ($), theoretical R:R vs achieved, time-in-trade.
2. NARRATIVE — what happened; flag anything unusual (stop hit <2 min after entry may indicate \
slippage; no signal in a full session is notable; session_end exit means position held past close).
3. RECOMMENDATIONS — preliminary parameter review triggers based on today + recent session \
patterns. Examples: 3+ consecutive stops → review STOP_RATIO; consistent session_end exits → \
consider earlier SESSION_END. If fewer than 3 sessions of data, state that explicitly."""


class Summarizer:
    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is required")
        self._client = anthropic.Anthropic(api_key=api_key)

    def run(
        self,
        date: datetime.date,
        session_log_path: Path,
        sessions_dir: Path,
        channel: OutputChannel,
    ) -> None:
        """Generate and write post-session summary."""
        session_log = session_log_path.read_text(encoding="utf-8") if session_log_path.exists() else "(no signals logged)"
        prev = _load_previous_summaries(sessions_dir, date)
        user_msg = _USER_TEMPLATE.format(
            date=date.isoformat(),
            weekday=date.strftime("%A"),
            session_log=session_log,
            n_prev=len(prev),
            prev_summaries="\n\n---\n\n".join(prev) if prev else "(none — first session)",
        )
        response = self._client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        summary = response.content[0].text
        summary_path = sessions_dir / date.isoformat() / "summary.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary, encoding="utf-8")
        channel.writeln("\n" + "=" * 60)
        channel.writeln(f"SESSION SUMMARY — {date.isoformat()}")
        channel.writeln("=" * 60)
        channel.write(summary + "\n")


def _load_previous_summaries(sessions_dir: Path, current_date: datetime.date) -> list[str]:
    """Load up to _MAX_PREVIOUS previous summary.md files, oldest first."""
    summaries = []
    if not sessions_dir.exists():
        return summaries
    dated_dirs = sorted(
        (d for d in sessions_dir.iterdir()
         if d.is_dir() and d.name < current_date.isoformat()),
        key=lambda d: d.name,
    )
    for d in dated_dirs[-_MAX_PREVIOUS:]:
        summary_file = d / "summary.md"
        if summary_file.exists():
            summaries.append(f"### {d.name}\n{summary_file.read_text(encoding='utf-8')}")
    return summaries
