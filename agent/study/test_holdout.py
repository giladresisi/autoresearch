import datetime
import json

from agent.study.holdout import ALL_DATES, HOLDOUT_JSON, is_holdout


def test_the_split_is_by_iso_week_one_in_four():
    d = datetime.date(2026, 5, 1)
    assert is_holdout(d) == (d.isocalendar()[1] % 4 == 0)


def test_whole_weeks_move_together():
    """Block-interleaved, not per-session: adjacent sessions share the same prior-week
    pools, the same open gap, often the same unfinished business. A per-day split
    leaks that context into the holdout."""
    by_week = {}
    for d in ALL_DATES:
        by_week.setdefault(d.isocalendar()[:2], set()).add(is_holdout(d))
    assert all(len(v) == 1 for v in by_week.values())


def test_the_held_back_weeks_are_interleaved_across_the_range():
    """One in four, spread -- not a contiguous block at one end of the corpus."""
    weeks = sorted({d.isocalendar()[1] for d in ALL_DATES if is_holdout(d)})
    assert len(weeks) >= 4
    assert all(b - a == 4 for a, b in zip(weeks, weeks[1:]))


def test_roughly_a_quarter_is_held_back():
    held = [d for d in ALL_DATES if is_holdout(d)]
    assert 15 <= len(held) <= 30
    assert len(ALL_DATES) == 87


def test_the_split_is_pure_and_stable():
    assert [is_holdout(d) for d in ALL_DATES] == [is_holdout(d) for d in ALL_DATES]


def test_the_committed_json_matches_the_function():
    """The file is the record; the function is the rule. If they drift, a later phase
    silently searches on sessions it was supposed to hold back."""
    saved = json.load(open(HOLDOUT_JSON, encoding="utf-8"))
    assert saved["holdout"] == [str(d) for d in ALL_DATES if is_holdout(d)]
    assert saved["discovery"] == [str(d) for d in ALL_DATES if not is_holdout(d)]
    assert set(saved["holdout"]) & set(saved["discovery"]) == set()
