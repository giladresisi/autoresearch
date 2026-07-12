"""Phase 2 — offline facts builder tests (plan 10).

Parity against the prepare_cuts path (byte-identical facts content hash on a burned cut)
and the no-lookahead guard. These read the machine-local main parquets; skipped when the
main dir is absent (e.g. CI without data).
"""

import hashlib
import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "contracts"),
           os.path.join(os.path.dirname(os.path.dirname(_HERE)), "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from facts import DEFAULT_MAIN, ParquetFactsSource  # noqa: E402

TZ = "America/New_York"
_REPO = os.path.dirname(os.path.dirname(_HERE))
_BURNED = os.path.join(_REPO, "calibration", "cuts", "2026-06-25_0850", "facts.txt")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(DEFAULT_MAIN) or not os.path.exists(_BURNED),
    reason="main parquets / burned cut not available")


@pytest.fixture(scope="module")
def source():
    return ParquetFactsSource()


def test_facts_parity_with_prepare_cuts(source):
    # The 2026-06-25 08:50 burned cut: bench facts must reproduce prepare_cuts' facts.txt
    # byte-for-byte (same content hash) — the drift guard between bench and calibration.
    boundary = pd.Timestamp("2026-06-25 08:50:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    ref = open(_BURNED, encoding="utf-8").read()
    assert res.content_hash == hashlib.sha256(ref.encode("utf-8")).hexdigest()


def test_lookahead_guard_max_ts_before_boundary(source):
    boundary = pd.Timestamp("2026-06-25 11:00:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    assert res.max_ts is not None
    assert res.max_ts < boundary                      # strictly before — no lookahead
    # every referenced level carries a price/side + birth sweep state + a threshold.
    assert res.levels
    for name, lv in res.levels.items():
        assert lv["side"] in ("above", "below")
        assert isinstance(lv["price"], (int, float))


def test_degraded_before_data_start(source):
    # 1s data starts 2026-05-01; a boundary before it → degraded (failsafe), never raises.
    res = source.build_facts(pd.Timestamp("2026-04-20 12:00:00", tz=TZ))
    assert res.degraded
    assert res.error


def test_evidence_text_populated_and_not_in_content_hash(source):
    boundary = pd.Timestamp("2026-06-25 08:50:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    assert res.evidence_text
    assert res.evidence_text.startswith("## S9 THESIS EVIDENCE")
    # the core content_hash (S0-S7 only) must not change when evidence_text exists.
    assert res.content_hash == hashlib.sha256(res.text.encode("utf-8")).hexdigest()
