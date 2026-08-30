"""Task 1: eligibility reads EXISTENCE, never identity (l2-mechanisms.md §2).

`reference_ts` is the gap's IDENTITY — the MIDDLE bar. EXISTENCE is the THIRD bar's
COMPLETION (`label(T+1) + timeframe`). Gating on identity admits a 5m gap TEN MINUTES
early, which is the look-ahead the §11 08-21 erratum documents (a 09:36:04 fill recorded
on a gap that only existed at 09:40:00).
"""
import pandas as pd
import pytest

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import Executor

TZ = "America/New_York"


def _gap(tf="5min", ref="2026-08-21 09:30", exists="2026-08-21 09:40",
         direction="bear"):
    return Fact(id=f"g-{tf}-{ref}", cls=FactClass.FVG, ticker="MNQ",
                label="g", name=None,
                reference_ts=pd.Timestamp(ref, tz=TZ), price=None,
                price_low=29472.25, price_high=29485.0,
                timeframe=tf, resolution=tf, state=FactState.LIVE,
                state_ts=pd.Timestamp(ref, tz=TZ), provenance={},
                extra={"direction": direction, "max_anti_excursion": 0.0,
                       "exists_from": pd.Timestamp(exists, tz=TZ)})


@pytest.fixture
def ex(tmp_path):
    """A SHORT plan, so the bear gaps above are the trade direction."""
    return Executor(str(tmp_path),
                    {"plan_id": "t", "direction": "DOWN",
                     "dol": {"price": 29000.0},
                     "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-21 09:20", tz=TZ))


def test_a_gap_is_invisible_before_its_existence_instant(ex):
    """The 08-21 gap is identified 09:30 but exists only at 09:40:00. At 09:35 it must
    not be eligible — admitting it is the look-ahead the §11 erratum documents."""
    ex._store.upsert(_gap())
    assert ex._eligible_gaps(pd.Timestamp("2026-08-21 09:35", tz=TZ)) == []


def test_the_same_gap_is_eligible_at_its_existence_instant(ex):
    ex._store.upsert(_gap())
    got = ex._eligible_gaps(pd.Timestamp("2026-08-21 09:40", tz=TZ))
    assert [g.id for g in got] == ["g-5min-2026-08-21 09:30"]


def test_identity_alone_never_admits_a_gap(ex):
    """reference_ts (the MIDDLE bar) must not be the gate. A 5m gap is admitted two bar
    widths — ten minutes — early if it is."""
    ex._store.upsert(_gap())
    at_identity = pd.Timestamp("2026-08-21 09:30", tz=TZ)
    assert ex._eligible_gaps(at_identity) == []


def test_a_1m_gap_is_gated_two_minutes_after_its_identity(ex):
    ex._store.upsert(_gap(tf="1min", ref="2026-08-21 09:23", exists="2026-08-21 09:25"))
    assert ex._gaps_on("1min", pd.Timestamp("2026-08-21 09:24", tz=TZ)) == []


def test_a_fact_without_exists_from_falls_back_to_identity(ex):
    """Legacy/synthetic facts must not vanish. The fallback keeps older snapshots
    readable; it is not a licence to omit the field."""
    g = _gap()
    del g.extra["exists_from"]
    ex._store.upsert(g)
    assert len(ex._eligible_gaps(pd.Timestamp("2026-08-21 09:31", tz=TZ))) == 1


def test_exists_by_is_total_when_now_is_unknown(ex):
    """Direct-construction callers pass now=None; the gate must not swallow the fact."""
    assert ex._exists_by(_gap(), None) is True
