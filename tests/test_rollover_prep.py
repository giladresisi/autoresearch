"""tests/test_rollover_prep.py — quarterly contract rollover-prep.

Covers the pure logic and the file-level steps. The IB lookups (resolve_next_quarterly,
measure_gap) need a live gateway and are exercised by `trade.py rollover-prep --dry-run`.

Every test runs under conftest's `_isolate_global_state`, so paths.general_main_dir() /
general_live_dir() point into tmp_path — no test can touch the real ledger or global.json.
"""
import json

import pandas as pd
import pytest

from scripts import rollover_prep as rp


# ---------------------------------------------------------------------------
# next_prep_date — must reproduce the historical ledger rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expiry,expected", [
    ("2026-06-18", "2026-06-13"),   # Thursday expiry -> preceding Saturday (real June row)
    ("2026-09-18", "2026-09-12"),   # Friday expiry   -> preceding Saturday (real Sept row)
    ("2026-12-18", "2026-12-12"),   # the upcoming December roll
])
def test_next_prep_date_is_the_saturday_before_expiry(expiry, expected):
    assert rp.next_prep_date(expiry) == expected
    assert pd.Timestamp(rp.next_prep_date(expiry)).day_name() == "Saturday"


def test_next_prep_date_on_a_saturday_expiry_steps_back_a_full_week():
    """A Saturday expiry must not return itself — the prep date is always strictly before."""
    assert rp.next_prep_date("2026-12-19") == "2026-12-12"


def test_contract_code_and_subfolder():
    assert rp.contract_code("MNQ", "2026-12-18") == "MNQZ6"
    assert rp.contract_code("MES", "2026-09-18") == "MESU6"
    assert rp.subfolder_for("2026-12-18") == "2026-12"


# ---------------------------------------------------------------------------
# rollover_status — the due-check that drives the banner and the preflight
# ---------------------------------------------------------------------------

LEDGER_PRE_ROLL = [
    {"prep_date": "2026-06-13", "subfolder": "2026-09", "expiry": "2026-09-18"},
    {"prep_date": "0001-01-01", "subfolder": "2026-06", "expiry": "2026-06-18"},
]


def test_not_due_before_the_prep_date():
    st = rp.rollover_status(LEDGER_PRE_ROLL, "2026-09-12", today="2026-09-11")
    assert st["due"] is False and st["already_rolled"] is False


def test_due_on_the_prep_date():
    st = rp.rollover_status(LEDGER_PRE_ROLL, "2026-09-12", today="2026-09-12")
    assert st["due"] is True and st["prep_date"] == "2026-09-12"


def test_due_stays_true_after_the_prep_date_until_the_roll_runs():
    """The roll is not time-boxed — a missed Saturday must keep alarming, not go quiet."""
    st = rp.rollover_status(LEDGER_PRE_ROLL, "2026-09-12", today="2026-09-20")
    assert st["due"] is True


def test_not_due_once_the_roll_has_run_and_env_advanced():
    rolled = [{"prep_date": "2026-09-12", "subfolder": "2026-12", "expiry": "2026-12-18"}]
    rolled += LEDGER_PRE_ROLL
    st = rp.rollover_status(rolled, "2026-12-12", today="2026-09-12")
    assert st["due"] is False and st["already_rolled"] is False


def test_already_rolled_when_ledger_advanced_but_env_did_not():
    """Row written, .env not updated — re-running would double-shift, so refuse."""
    rolled = [{"prep_date": "2026-09-12", "subfolder": "2026-12", "expiry": "2026-12-18"}]
    st = rp.rollover_status(rolled, "2026-09-12", today="2026-09-12")
    assert st["already_rolled"] is True and st["due"] is False


def test_missing_env_prep_date_is_not_due():
    assert rp.rollover_status(LEDGER_PRE_ROLL, None, today="2026-09-12")["due"] is False


# ---------------------------------------------------------------------------
# prepend_row — the silent-no-op trap
# ---------------------------------------------------------------------------

def test_prepend_row_puts_the_new_era_first():
    """rows[0] is what _current_main_subdir() reads; appending would change nothing."""
    row = {"prep_date": "2026-09-12", "subfolder": "2026-12"}
    out = rp.prepend_row(LEDGER_PRE_ROLL, row)
    assert out[0] is row
    assert len(out) == len(LEDGER_PRE_ROLL) + 1
    assert out[1:] == LEDGER_PRE_ROLL


def test_prepend_row_does_not_mutate_the_input():
    before = list(LEDGER_PRE_ROLL)
    rp.prepend_row(LEDGER_PRE_ROLL, {"prep_date": "2026-09-12"})
    assert LEDGER_PRE_ROLL == before


def test_prepended_row_routes_both_readers_to_the_new_era(monkeypatch):
    """End-to-end on the real readers: a prepended row must flip promote's target and
    route post-prep-date backtests to the new subfolder while pre-roll dates stay put."""
    import paths
    from scripts import check_session_parquets as csp
    import backtest_smt

    main = paths.general_main_dir()
    for sub in ("2026-06", "2026-09", "2026-12"):
        (main / sub).mkdir(parents=True, exist_ok=True)
    rp.write_ledger(LEDGER_PRE_ROLL)
    assert csp._current_main_subdir().name == "2026-09"

    row = rp.build_ledger_row("2026-09-12", "2026-12-18",
                              {"mnq": 300.0, "mes": 60.0},
                              {"mnq": 1, "mes": 2}, {"mnq": 3, "mes": 4})
    rp.write_ledger(rp.prepend_row(rp.load_ledger(), row))

    assert csp._current_main_subdir().name == "2026-12"
    assert backtest_smt._main_dir_for_date("2026-09-15").name == "2026-12"
    assert backtest_smt._main_dir_for_date("2026-09-11").name == "2026-09"
    assert backtest_smt._main_dir_for_date("2026-05-01").name == "2026-06"


# ---------------------------------------------------------------------------
# back_adjust — price scale shifts, volume does not
# ---------------------------------------------------------------------------

def _frame():
    idx = pd.date_range("2026-09-11 15:57", periods=3, freq="1min", tz="America/New_York")
    return pd.DataFrame(
        {"Open": [100.0, 101.0, 102.0], "High": [103.0, 104.0, 105.0],
         "Low": [99.0, 100.0, 101.0], "Close": [102.0, 103.0, 104.0],
         "Volume": [10, 20, 30]}, index=idx)


def test_back_adjust_shifts_ohlc_leaves_volume():
    out = rp.back_adjust(_frame(), 293.25)
    assert out["Open"].tolist() == [393.25, 394.25, 395.25]
    assert out["High"].tolist() == [396.25, 397.25, 398.25]
    assert out["Low"].tolist() == [392.25, 393.25, 394.25]
    assert out["Close"].tolist() == [395.25, 396.25, 397.25]
    assert out["Volume"].tolist() == [10, 20, 30]


def test_back_adjust_preserves_ranges():
    """A constant offset must be decision-neutral: every price difference is unchanged."""
    df = _frame()
    out = rp.back_adjust(df, -62.5)
    assert ((out["High"] - out["Low"]) == (df["High"] - df["Low"])).all()


def test_back_adjust_does_not_mutate_the_input():
    df = _frame()
    rp.back_adjust(df, 10.0)
    assert df["Open"].tolist() == [100.0, 101.0, 102.0]


def test_round_to_tick():
    """Measured gaps land on whole ticks — both contracts tick 0.25."""
    assert rp.round_to_tick(293.19) == 293.25
    assert rp.round_to_tick(62.51) == 62.5
    assert rp.round_to_tick(-17.3) == -17.25
    assert rp.round_to_tick(293.25) == 293.25


# ---------------------------------------------------------------------------
# Resume state — the destructive step must never run twice
# ---------------------------------------------------------------------------

def test_state_roundtrip_and_scoping_to_the_prep_date():
    assert rp._load_state("2026-09-12") == set()
    done = rp._mark_step("2026-09-12", "backadjust", set())
    assert rp._load_state("2026-09-12") == {"backadjust"}
    rp._mark_step("2026-09-12", "ledger", done)
    assert rp._load_state("2026-09-12") == {"backadjust", "ledger"}
    # A state file from a PREVIOUS quarter must not suppress this quarter's steps.
    assert rp._load_state("2026-12-12") == set()
    rp._clear_state()
    assert rp._load_state("2026-09-12") == set()


# ---------------------------------------------------------------------------
# .env rewrite
# ---------------------------------------------------------------------------

def test_update_env_rewrites_conids_and_prep_date_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "IB_PORT=4002\n"
        "MNQ_CONID=793356225   # MNQU6 — September 2026, expires 2026-09-18\n"
        "MES_CONID=793356217   # MESU6 — September 2026, expires 2026-09-18\n"
        "ROLLOVER_PREP_DATE=2026-09-12\n"
        "PMT_FILLS_URL=https://example.invalid/fills\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "env_path", lambda: env)
    rp.update_env({"mnq": 111, "mes": 222}, "2026-12-18", "2026-12-12",
                  {"mnq": 793356225, "mes": 793356217}, "2026-09-18", "2026-09-12")

    text = env.read_text(encoding="utf-8")
    assert "MNQ_CONID=111" in text and "MES_CONID=222" in text
    assert "ROLLOVER_PREP_DATE=2026-12-12" in text
    assert "MNQZ6" in text and "rolled from MNQU6/793356225" in text
    # Untouched lines survive verbatim.
    assert "IB_PORT=4002" in text
    assert "PMT_FILLS_URL=https://example.invalid/fills" in text
    # Old values are gone.
    assert "793356225\n" not in text.replace("rolled from MNQU6/793356225", "")
    assert (tmp_path / ".env.preroll.bak").exists()


# ---------------------------------------------------------------------------
# Preflight — refuses to roll before gap-fill + promote have run
# ---------------------------------------------------------------------------

def _write_parquets(directory, last_ts):
    idx = pd.date_range(end=last_ts, periods=3, freq="1min", tz="America/New_York")
    df = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 1},
                      index=idx)
    directory.mkdir(parents=True, exist_ok=True)
    for name in rp.PARQUET_NAMES:
        df.to_parquet(directory / name)


def _due_status():
    return {"due": True, "already_rolled": False, "prep_date": "2026-09-12", "reason": "due"}


def test_preflight_refuses_when_main_is_behind_live():
    """The promote step has not run, so the old contract's last session is not frozen."""
    import paths
    rp.write_ledger(LEDGER_PRE_ROLL)
    _write_parquets(paths.general_live_dir(), "2026-09-11 16:59")
    _write_parquets(paths.general_main_dir() / "2026-09", "2026-09-04 09:59")

    with pytest.raises(RuntimeError, match="not frozen in its own subfolder"):
        rp._preflight(_due_status())


def test_preflight_passes_when_live_and_main_agree():
    import paths
    rp.write_ledger(LEDGER_PRE_ROLL)
    _write_parquets(paths.general_live_dir(), "2026-09-11 16:59")
    _write_parquets(paths.general_main_dir() / "2026-09", "2026-09-11 16:59")

    rp._preflight(_due_status())   # must not raise


def test_preflight_refuses_when_not_due():
    with pytest.raises(RuntimeError, match="Refusing to roll"):
        rp._preflight({"due": False, "already_rolled": False,
                       "prep_date": "2026-12-12", "reason": "not yet due"})


def test_preflight_refuses_when_already_rolled():
    with pytest.raises(RuntimeError, match="Refusing to roll"):
        rp._preflight({"due": False, "already_rolled": True,
                       "prep_date": "2026-09-12", "reason": "already covered"})


# ---------------------------------------------------------------------------
# Ledger row shape
# ---------------------------------------------------------------------------

def test_build_ledger_row_shape_matches_the_existing_rows():
    row = rp.build_ledger_row("2026-09-12", "2026-12-18",
                              {"mnq": 293.25, "mes": 62.5},
                              {"mnq": 793356225, "mes": 793356217},
                              {"mnq": 111, "mes": 222})
    assert row["prep_date"] == "2026-09-12"
    assert row["subfolder"] == "2026-12"
    assert row["expiry"] == "2026-12-18"
    assert row["mnq"] == {"old_conid": 793356225, "new_conid": 111, "gap": 293.25}
    assert row["mes"] == {"old_conid": 793356217, "new_conid": 222, "gap": 62.5}
    assert "MNQZ6" in row["note"]


def test_write_ledger_backs_up_and_is_valid_json():
    rp.write_ledger(LEDGER_PRE_ROLL)
    rp.write_ledger(rp.prepend_row(LEDGER_PRE_ROLL, {"prep_date": "2026-09-12"}))
    p = rp.ledger_path()
    assert json.loads(p.read_text(encoding="utf-8"))[0]["prep_date"] == "2026-09-12"
    assert p.with_suffix(".json.bak").exists()
