import json
import os

import pytest

from scripts.build_session_skeleton import build


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("skeleton")
    summary = build(str(out), dates=None)
    return str(out), summary


def test_every_session_appears_including_no_move_ones(built):
    out, summary = built
    rows = [json.loads(l) for l in
            open(os.path.join(out, "session_skeleton.jsonl"), encoding="utf-8")]
    assert len({r["date"] for r in rows}) == summary["sessions"]
    assert summary["sessions"] == 87


def test_no_move_sessions_are_present_as_rows_not_omitted(built):
    out, summary = built
    rows = [json.loads(l) for l in
            open(os.path.join(out, "session_skeleton.jsonl"), encoding="utf-8")]
    no_move = {r["date"] for r in rows if r["status"] == "no_move"}
    assert len(no_move) == summary["no_move"]


def test_every_ok_session_has_exactly_one_primary(built):
    out, _ = built
    rows = [json.loads(l) for l in
            open(os.path.join(out, "session_skeleton.jsonl"), encoding="utf-8")]
    by_date = {}
    for r in rows:
        if r["status"] == "ok":
            by_date.setdefault(r["date"], []).append(r["role"])
    assert by_date, "no ok sessions at all -- the decomposition is broken"
    for date, roles in by_date.items():
        assert roles.count("primary") == 1, (date, roles)


def test_the_summary_file_matches_the_returned_summary(built):
    out, summary = built
    on_disk = json.load(open(os.path.join(out, "session_skeleton_summary.json"),
                             encoding="utf-8"))
    assert on_disk == summary


def test_the_build_is_reproducible(tmp_path):
    a = build(str(tmp_path / "a"), dates=None)
    b = build(str(tmp_path / "b"), dates=None)
    assert a == b
    ra = open(tmp_path / "a" / "session_skeleton.jsonl", encoding="utf-8").read()
    rb = open(tmp_path / "b" / "session_skeleton.jsonl", encoding="utf-8").read()
    assert ra == rb
