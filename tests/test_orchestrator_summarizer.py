import datetime
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.summarizer import Summarizer, _load_previous_summaries
from orchestrator.output import OutputChannel


def _make_mock_client(response_text: str = "## METRICS\ntest"):
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


def _make_channel():
    channel = OutputChannel()
    received = []
    mock_sink = MagicMock()
    mock_sink.write.side_effect = lambda text: received.append(text)
    channel.add_sink(mock_sink)
    return channel, received


def test_summarizer_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Summarizer()


def test_summarizer_builds_prompt_with_session_log(tmp_path):
    date = datetime.date(2026, 4, 17)
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / date.isoformat()
    session_dir.mkdir(parents=True)
    session_log = session_dir / "signals.log"
    session_log.write_text("09:15 SMT bullish divergence detected\n10:02 trade entered at 18500\n")

    mock_client = _make_mock_client()
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    assert mock_client.messages.create.call_count == 1
    call_kwargs = mock_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    assert "09:15 SMT bullish divergence detected" in user_content
    assert "10:02 trade entered at 18500" in user_content


def test_summarizer_includes_previous_summaries(tmp_path):
    sessions_dir = tmp_path / "sessions"
    for d in ("2026-04-14", "2026-04-15", "2026-04-16"):
        pdir = sessions_dir / d
        pdir.mkdir(parents=True)
        (pdir / "summary.md").write_text(f"Summary for {d}")

    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"
    session_log.write_text("signal")

    mock_client = _make_mock_client()
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    user_content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Summary for 2026-04-14" in user_content
    assert "Summary for 2026-04-15" in user_content
    assert "Summary for 2026-04-16" in user_content


def test_summarizer_caps_at_5_previous(tmp_path):
    sessions_dir = tmp_path / "sessions"
    prior_dates = [
        "2026-04-08", "2026-04-09", "2026-04-10", "2026-04-11",
        "2026-04-12", "2026-04-13", "2026-04-14",
    ]
    for d in prior_dates:
        pdir = sessions_dir / d
        pdir.mkdir(parents=True)
        (pdir / "summary.md").write_text(f"Summary for {d}")

    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"
    session_log.write_text("signal")

    mock_client = _make_mock_client()
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    user_content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    # Only the 5 most recent should appear
    assert "Summary for 2026-04-14" in user_content
    assert "Summary for 2026-04-13" in user_content
    assert "Summary for 2026-04-12" in user_content
    assert "Summary for 2026-04-11" in user_content
    assert "Summary for 2026-04-10" in user_content
    # The two oldest should NOT appear
    assert "Summary for 2026-04-08" not in user_content
    assert "Summary for 2026-04-09" not in user_content


def test_summarizer_no_previous_summaries(tmp_path):
    sessions_dir = tmp_path / "sessions"
    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"
    session_log.write_text("signal")

    mock_client = _make_mock_client()
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    user_content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "(none — first session)" in user_content


def test_summarizer_writes_summary_md(tmp_path):
    sessions_dir = tmp_path / "sessions"
    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"
    session_log.write_text("signal")

    mock_client = _make_mock_client("## METRICS\ntest")
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    summary_path = cur_dir / "summary.md"
    assert summary_path.exists()
    assert summary_path.read_text(encoding="utf-8") == "## METRICS\ntest"


def test_summarizer_writes_to_channel(tmp_path):
    sessions_dir = tmp_path / "sessions"
    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"
    session_log.write_text("signal")

    mock_client = _make_mock_client("## METRICS\nsummary-body")
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, received = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    joined = "".join(received)
    assert "SESSION SUMMARY — 2026-04-17" in joined
    assert "summary-body" in joined


def test_summarizer_missing_signals_log(tmp_path):
    sessions_dir = tmp_path / "sessions"
    date = datetime.date(2026, 4, 17)
    cur_dir = sessions_dir / date.isoformat()
    cur_dir.mkdir(parents=True)
    session_log = cur_dir / "signals.log"  # deliberately not created

    mock_client = _make_mock_client()
    with patch("anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            s = Summarizer()
            channel, _ = _make_channel()
            s.run(date, session_log, sessions_dir, channel)

    user_content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "(no signals logged)" in user_content


def test_load_previous_summaries_sorted_oldest_first(tmp_path):
    sessions_dir = tmp_path / "sessions"
    for d in ("2026-04-15", "2026-04-14"):  # deliberately out-of-order creation
        pdir = sessions_dir / d
        pdir.mkdir(parents=True)
        (pdir / "summary.md").write_text(f"Summary {d}")

    result = _load_previous_summaries(sessions_dir, datetime.date(2026, 4, 17))
    assert len(result) == 2
    assert "2026-04-14" in result[0]
    assert "2026-04-15" in result[1]


def test_load_previous_summaries_excludes_current_date(tmp_path):
    sessions_dir = tmp_path / "sessions"
    current = "2026-04-17"
    prior = "2026-04-16"
    for d in (current, prior):
        pdir = sessions_dir / d
        pdir.mkdir(parents=True)
        (pdir / "summary.md").write_text(f"Summary {d}")

    result = _load_previous_summaries(sessions_dir, datetime.date(2026, 4, 17))
    assert len(result) == 1
    assert prior in result[0]
    assert current not in result[0]
