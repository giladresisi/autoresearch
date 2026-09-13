import datetime
import os

from scripts.plot_session_skeleton import plot_sessions


def test_one_png_is_written_per_requested_date(tmp_path):
    dates = [datetime.date(2026, 8, 13), datetime.date(2026, 8, 12)]
    written = plot_sessions(dates, str(tmp_path))
    assert len(written) == 2
    for p in written:
        assert os.path.exists(p) and os.path.getsize(p) > 0


def test_a_date_outside_the_study_range_is_skipped_not_crashed(tmp_path):
    written = plot_sessions([datetime.date(1999, 1, 4)], str(tmp_path))
    assert written == []
