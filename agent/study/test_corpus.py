"""`agent/study/corpus.py`: the contract-folder lookup, the thesis patch and the two
criteria on synthetic 1s bars. No parquet, no git, no replay."""
import pandas as pd
import pytest

from agent.study import corpus

ET = "America/New_York"


def _bars(path, start="2026-10-01 09:30:00", step_s=1):
    """1s bars from a price path; each bar's High/Low span the move from the previous
    close, so an excursion inside a second is visible to the scorer."""
    idx = pd.date_range(pd.Timestamp(start, tz=ET), periods=len(path), freq=f"{step_s}s")
    rows, prev = [], path[0]
    for p in path:
        rows.append({"Open": prev, "High": max(prev, p), "Low": min(prev, p), "Close": p})
        prev = p
    return pd.DataFrame(rows, index=idx)


def test_contract_folder_picks_latest_prep_date_at_or_before():
    ledger = [{"prep_date": "2026-09-12", "subfolder": "2026-12"},
              {"prep_date": "2026-06-13", "subfolder": "2026-09"},
              {"prep_date": "0001-01-01", "subfolder": "2026-06"}]
    assert corpus.contract_folder("2026-10-01", ledger) == "2026-12"
    assert corpus.contract_folder("2026-09-12", ledger) == "2026-12"
    assert corpus.contract_folder("2026-09-11", ledger) == "2026-09"
    assert corpus.contract_folder("2026-06-09", ledger) == "2026-06"


def test_oracle_thesis_patches_code_built_fields(tmp_path):
    (tmp_path / "thesis_state.json").write_text(
        '{"thesis": {"bias": "UP", "regime": null, "confidence": null, '
        '"dol": {"level": "TDO", "price": 29199.25}, "falsified_if": []}}', encoding="utf-8")
    thesis, patched, errs = corpus.oracle_thesis(tmp_path)
    assert errs == []
    assert patched == ["regime", "confidence", "falsified_if"]
    assert thesis["falsified_if"][0]["side"] == "below"
    assert thesis["falsified_if"][0]["price"] == pytest.approx(29199.25 - corpus.PATCH_FALSIFIER_PTS)


def test_score_entry_short_mfe_ends_at_meaningful_retrace():
    # Short from 100: runs to 40 (+60), then gives back 41 (> the flat 40-pt floor).
    bars = _bars([100, 90, 70, 50, 40, 45, 55, 81, 60])
    s = corpus.score_entry(bars, bars.index[0], 100.0, "DOWN", 15.0)
    assert s["mfe"] == 60.0 and s["ended_by"] == "retrace" and s["stop_survived"]
    assert s["r_multiple"] == 4.0


def test_score_entry_stop_hit_keeps_mfe_before_it():
    bars = _bars([100, 95, 90, 100, 116, 60])         # +10 then the 15-pt stop is hit
    s = corpus.score_entry(bars, bars.index[0], 100.0, "DOWN", 15.0)
    assert s["mfe"] == 10.0 and s["ended_by"] == "stop" and not s["stop_survived"]


def test_ideal_entry_is_the_leg_origin_after_a_meaningful_retrace():
    # Short: 100 -> 40 (leg 60), retrace to 85 (45 pts, meaningful), then down to 60.
    bars = _bars([100, 80, 60, 40, 60, 85, 70, 60])
    ie = corpus.ideal_entry(bars, "DOWN", bars.index[-1])
    assert ie["ideal_entry"] == 85.0 and ie["ideal_stop_needed"] == 0.0


def test_ideal_entry_origin_extends_before_a_leg_forms():
    # Short: price drifts UP 25 pts with no 40-pt down-leg, then falls: origin = the top.
    bars = _bars([100, 110, 120, 125, 110, 90, 70])
    ie = corpus.ideal_entry(bars, "DOWN", bars.index[-1])
    assert ie["ideal_entry"] == 125.0


def test_ideal_entry_long_mirror():
    bars = _bars([100, 120, 140, 160, 140, 115, 130, 140])
    ie = corpus.ideal_entry(bars, "UP", bars.index[-1])
    assert ie["ideal_entry"] == 115.0


def test_a_sub_floor_pullback_does_not_re_anchor():
    # Short: 100 -> 40, a 35-pt bounce (under the 40 floor), then on to 20: the ideal
    # entry stays at 100 and the stop it needed is the bounce above it (none here).
    bars = _bars([100, 70, 40, 60, 75, 50, 20])
    ie = corpus.ideal_entry(bars, "DOWN", bars.index[-1])
    assert ie["ideal_entry"] == 100.0
