import pandas as pd
import pytest
from agent.facts.records import Fact, FactClass, FactState
from agent.facts.ids import fact_id, fact_label

TS = pd.Timestamp("2026-08-18 04:05", tz="America/New_York")


def test_fact_id_is_stable_across_state_changes():
    """The ID hashes identity only — mutating state must not change it."""
    f = Fact(id="", cls=FactClass.FVG, ticker="MNQ", label="", name=None,
             reference_ts=TS, price=None, price_low=29766.25, price_high=29808.75,
             timeframe="5min", resolution="1min", state=FactState.LIVE,
             state_ts=TS, provenance={}, extra={"direction": "bear"})
    a = fact_id(FactClass.FVG, "MNQ", timeframe="5min", reference_ts=TS,
                price_low=29766.25, price_high=29808.75)
    f.state = FactState.INVALIDATED
    b = fact_id(FactClass.FVG, "MNQ", timeframe="5min", reference_ts=TS,
                price_low=29766.25, price_high=29808.75)
    assert a == b


def test_fact_id_differs_across_tickers():
    kw = dict(timeframe="5min", reference_ts=TS, price_low=1.0, price_high=2.0)
    assert fact_id(FactClass.FVG, "MNQ", **kw) != fact_id(FactClass.FVG, "MES", **kw)


def test_fact_id_excludes_the_mutable_name():
    """A level renamed prev1_day_high -> prev2_day_high keeps its ID."""
    kw = dict(tier="day", window_start=TS, window_end=TS, price=29808.75)
    assert fact_id(FactClass.LEVEL, "MNQ", **kw) == fact_id(FactClass.LEVEL, "MNQ", **kw)


def test_fact_id_is_twelve_hex_chars():
    fid = fact_id(FactClass.LEVEL, "MNQ", tier="day", price=1.0)
    assert len(fid) == 12 and all(c in "0123456789abcdef" for c in fid)


def test_fact_label_renders_fvg_readably():
    f = Fact(id="abc123abc123", cls=FactClass.FVG, ticker="MNQ", label="", name=None,
             reference_ts=TS, price=None, price_low=29766.25, price_high=29808.75,
             timeframe="5min", resolution="1min", state=FactState.LIVE,
             state_ts=TS, provenance={}, extra={"direction": "bear"})
    lbl = fact_label(f)
    assert "MNQ" in lbl and "5min" in lbl and "bear" in lbl
    assert "29766.25" in lbl and "29808.75" in lbl
    assert "08-18 04:05" in lbl


def test_fact_label_renders_level_with_current_name():
    f = Fact(id="def456def456", cls=FactClass.LEVEL, ticker="MES", label="",
             name="prev2_day_high", reference_ts=TS, price=5900.0, price_low=None,
             price_high=None, timeframe=None, resolution="1min",
             state=FactState.SWEPT, state_ts=TS, provenance={}, extra={"tier": "day"})
    lbl = fact_label(f)
    assert "prev2_day_high" in lbl and "MES" in lbl and "5900.0" in lbl


def test_state_change_records_a_timestamp():
    f = Fact(id="x", cls=FactClass.LEVEL, ticker="MNQ", label="", name="day_high",
             reference_ts=TS, price=1.0, price_low=None, price_high=None,
             timeframe=None, resolution="1min", state=FactState.LIVE,
             state_ts=TS, provenance={}, extra={})
    later = TS + pd.Timedelta(hours=1)
    f.set_state(FactState.SWEPT, later)
    assert f.state is FactState.SWEPT and f.state_ts == later


# --- added during implementation (not in the plan): round-trip for the journal --- #

def test_fact_round_trips_through_dict():
    f = Fact(id="x", cls=FactClass.FVG, ticker="MNQ", label="l", name=None,
             reference_ts=TS, price=None, price_low=1.0, price_high=2.0,
             timeframe="5min", resolution="1min", state=FactState.LIVE,
             state_ts=TS, provenance={"src": "batch"},
             extra={"direction": "bull", "creating_bar_ts": TS})
    back = Fact.from_dict(f.to_dict())
    assert back.id == f.id and back.cls is f.cls and back.state is f.state
    assert back.reference_ts == f.reference_ts and back.price_high == 2.0
    assert back.extra["direction"] == "bull"
