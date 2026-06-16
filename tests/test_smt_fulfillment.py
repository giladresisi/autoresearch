# tests/test_smt_fulfillment.py
# Contract C unit tests (SMT V2 Phase 2): smt_detect._record_key + fulfillment_status.
# Pure read-only functions over detect_state; exhaustive per-branch coverage.

from __future__ import annotations

import copy

from smt_detect import _record_key, fulfillment_status, smt_status, detect_regular_smts


# ---------------------------------------------------------------------------
# Builders (mirror tests/test_smt_detect.py)
# ---------------------------------------------------------------------------
def _level(name, price):
    sub = "high" if name.endswith("_high") else "low"
    return {"name": name, "kind": "level", "price": float(price), "sub": sub}


def _levels(**kv):
    return {name: _level(name, price) for name, price in kv.items()}


def _bar(high, low, close=None, time="2026-06-09T10:00:00"):
    if close is None:
        close = (high + low) / 2.0
    return {"time": time, "high": float(high), "low": float(low), "close": float(close)}


# ===========================================================================
# _record_key
# ===========================================================================
def test_record_key_level_wick():
    rec = {"kind": "smt", "ref_name": "day_high", "direction": "short", "type": "wick"}
    assert _record_key(rec) == "day_high|short|wick"


def test_record_key_level_body():
    rec = {"kind": "smt", "ref_name": "week_low", "direction": "long", "type": "body"}
    assert _record_key(rec) == "week_low|long|body"


def test_record_key_fill_bare_name():
    rec = {"kind": "fill", "ref_name": "fvg_123_bull"}
    key = _record_key(rec)
    assert key == "fvg_123_bull"
    assert "|" not in key


def test_record_key_total_on_missing():
    # Partial / empty / non-dict inputs must not raise.
    assert _record_key({}) == "None|None|None"
    assert _record_key({"kind": "fill"}) == "None"
    assert _record_key(None) == ""


# ===========================================================================
# fulfillment_status
# ===========================================================================
def test_fulfillment_unfulfilled():
    ds = {"day_high|short|wick": {"fulfilled": False, "armed": True}}
    assert fulfillment_status(["day_high|short|wick"], ds) == {
        "day_high|short|wick": "unfulfilled"
    }


def test_fulfillment_fulfilled():
    ds = {"day_high|short|wick": {"fulfilled": True}}
    assert fulfillment_status(["day_high|short|wick"], ds) == {
        "day_high|short|wick": "fulfilled"
    }


def test_fulfillment_gone():
    ds = {"day_high|short|wick": {"fulfilled": True}}
    assert fulfillment_status(["not_present|long|wick"], ds) == {
        "not_present|long|wick": "gone"
    }


def test_fulfillment_fill_present_unfulfilled():
    # Fill state dicts have no `fulfilled` field → present fill key is unfulfilled.
    ds = {"fvg_x_bull": {"armed": False, "fill_a_fired": True}}
    assert fulfillment_status(["fvg_x_bull"], ds) == {"fvg_x_bull": "unfulfilled"}


def test_fulfillment_read_only():
    ds = {
        "day_high|short|wick": {"fulfilled": True, "nested": {"a": 1}},
        "fvg_x_bull": {"armed": False},
    }
    before = copy.deepcopy(ds)
    fulfillment_status(["day_high|short|wick", "fvg_x_bull", "absent"], ds)
    assert ds == before  # no mutation


def test_fulfillment_empty_keys():
    ds = {"day_high|short|wick": {"fulfilled": True}}
    assert fulfillment_status([], ds) == {}
    assert fulfillment_status(None, ds) == {}


def test_fulfillment_matches_detection_key():
    # Round-trip: a key produced by _record_key from an emission must match a key
    # actually present in a detect_state built by detect_regular_smts, and report
    # "unfulfilled" immediately after firing.
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=21001.0, low=20990.0, close=20995.0)  # MNQ touches 21000
    mes = _bar(high=2999.0, low=2990.0, close=2995.0)     # MES does NOT touch
    recs, state = detect_regular_smts(lm, le, mnq, mes, {})
    assert len(recs) == 1
    key = _record_key(recs[0])
    assert key == "day_high|short|wick"
    assert key in state
    assert fulfillment_status([key], state) == {key: "unfulfilled"}


# ---------------------------------------------------------------------------
# Phase 1.1.5 (GIL-25 §A.2.6): smt_status honors the new terminal flags
# (superseded / retired_depleted) so the active-set drop removes them.
# ---------------------------------------------------------------------------
def test_smt_status_superseded_is_terminal():
    ds = {"day_high|short|wick": {"superseded": True}}
    assert smt_status(["day_high|short|wick"], ds) == {"day_high|short|wick": "invalidated"}
    # fulfillment_status folds invalidated → unfulfilled (so active-set drop on != unfulfilled).
    assert fulfillment_status(["day_high|short|wick"], ds) == {"day_high|short|wick": "unfulfilled"}


def test_smt_status_retired_depleted_is_terminal():
    ds = {"day_high|short|wick": {"retired_depleted": True}}
    assert smt_status(["day_high|short|wick"], ds) == {"day_high|short|wick": "invalidated"}
    assert fulfillment_status(["day_high|short|wick"], ds) == {"day_high|short|wick": "unfulfilled"}


def test_smt_status_fulfilled_precedence_over_new_flags():
    # fulfilled wins over superseded/retired_depleted (precedence preserved).
    ds = {"k": {"fulfilled": True, "superseded": True, "retired_depleted": True}}
    assert smt_status(["k"], ds) == {"k": "fulfilled"}


def test_smt_status_plain_unfulfilled_unaffected():
    ds = {"k": {"fired": True, "fulfilled": False,
                "superseded": False, "retired_depleted": False}}
    assert smt_status(["k"], ds) == {"k": "unfulfilled"}
