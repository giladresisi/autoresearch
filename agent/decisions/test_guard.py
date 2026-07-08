"""No-lookahead guard tests (Wave 2.2)."""

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pytest

from guard import LookaheadError, assert_no_lookahead


@dataclass
class _Snap:
    max_ts: Optional[pd.Timestamp]


def _ts(s):
    return pd.Timestamp(s, tz="America/New_York")


def test_clean_snapshot_passes():
    trigger = _ts("2026-05-19 09:27:59")
    assert_no_lookahead(_Snap(max_ts=trigger - pd.Timedelta(seconds=5)), trigger)  # no raise


def test_poisoned_future_bar_raises():
    trigger = _ts("2026-05-19 09:27:59")
    poisoned = _Snap(max_ts=trigger + pd.Timedelta(seconds=1))
    with pytest.raises(LookaheadError):
        assert_no_lookahead(poisoned, trigger)


def test_exact_boundary_bar_passes():
    trigger = _ts("2026-05-19 09:27:59")
    assert_no_lookahead(_Snap(max_ts=trigger), trigger)  # <= boundary is inclusive
