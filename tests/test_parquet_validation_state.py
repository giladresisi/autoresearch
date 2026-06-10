# tests/test_parquet_validation_state.py
# Tests for scripts/parquet_validation_state.py (Contract A — watermark store + guard).
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# TestLoadState
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_load_state_missing_returns_empty(self, tmp_path):
        from scripts.parquet_validation_state import load_state

        assert load_state(tmp_path / ".validation_state.json") == {}

    def test_load_state_corrupt_returns_empty(self, tmp_path):
        from scripts.parquet_validation_state import load_state

        state_path = tmp_path / ".validation_state.json"
        state_path.write_bytes(b"\x00\x01 not json at all {{{")
        assert load_state(state_path) == {}


# ---------------------------------------------------------------------------
# TestSetWatermark
# ---------------------------------------------------------------------------

class TestSetWatermark:
    def test_set_then_load_roundtrip(self, tmp_path):
        from scripts.parquet_validation_state import (
            VALIDATOR_VERSION,
            get_watermark,
            load_state,
            set_watermark,
        )

        state_path = tmp_path / ".validation_state.json"

        # Seed a pre-existing entry for a SECOND parquet that must survive.
        set_watermark(
            state_path,
            "MES_1m.parquet",
            validated_through="2026-06-08T23:31:00-04:00",
            validated_rows=861098,
            first_bar="2024-01-01T18:00:00-05:00",
        )
        # Now set the first parquet's entry.
        set_watermark(
            state_path,
            "MNQ_1m.parquet",
            validated_through="2026-06-08T23:31:00-04:00",
            validated_rows=861353,
            first_bar="2024-01-01T18:00:00-05:00",
        )

        state = load_state(state_path)
        entry = get_watermark(state, "MNQ_1m.parquet")
        assert entry == {
            "validator_version": VALIDATOR_VERSION,
            "validated_through": "2026-06-08T23:31:00-04:00",
            "validated_rows": 861353,
            "first_bar": "2024-01-01T18:00:00-05:00",
        }

        # The pre-existing second parquet entry must be preserved.
        other = get_watermark(state, "MES_1m.parquet")
        assert other is not None
        assert other["validated_rows"] == 861098

    def test_set_watermark_atomic_no_tmp(self, tmp_path):
        from scripts.parquet_validation_state import set_watermark

        state_path = tmp_path / ".validation_state.json"
        set_watermark(
            state_path,
            "MNQ_1m.parquet",
            validated_through="2026-06-08T23:31:00-04:00",
            validated_rows=861353,
            first_bar="2024-01-01T18:00:00-05:00",
        )
        assert state_path.exists()
        assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# TestNeedsFullValidation
# ---------------------------------------------------------------------------

class TestNeedsFullValidation:
    def test_needs_full_no_entry(self):
        from scripts.parquet_validation_state import needs_full_validation

        assert needs_full_validation(None, first_bar="x", row_count=10) == (
            True,
            "no-watermark",
        )

    def test_needs_full_version_bump(self):
        from scripts.parquet_validation_state import (
            VALIDATOR_VERSION,
            needs_full_validation,
        )

        entry = {
            "validator_version": VALIDATOR_VERSION - 1,
            "validated_through": "2026-06-08T23:31:00-04:00",
            "validated_rows": 100,
            "first_bar": "x",
        }
        assert needs_full_validation(entry, first_bar="x", row_count=100) == (
            True,
            "version-bump",
        )

    def test_needs_full_first_bar_changed(self):
        from scripts.parquet_validation_state import (
            VALIDATOR_VERSION,
            needs_full_validation,
        )

        entry = {
            "validator_version": VALIDATOR_VERSION,
            "validated_through": "2026-06-08T23:31:00-04:00",
            "validated_rows": 100,
            "first_bar": "old-first-bar",
        }
        assert needs_full_validation(
            entry, first_bar="new-first-bar", row_count=100
        ) == (True, "body-rewritten")

    def test_needs_full_truncation(self):
        from scripts.parquet_validation_state import (
            VALIDATOR_VERSION,
            needs_full_validation,
        )

        entry = {
            "validator_version": VALIDATOR_VERSION,
            "validated_through": "2026-06-08T23:31:00-04:00",
            "validated_rows": 100,
            "first_bar": "x",
        }
        assert needs_full_validation(entry, first_bar="x", row_count=99) == (
            True,
            "truncation",
        )

    def test_needs_full_pure_append_false(self):
        from scripts.parquet_validation_state import (
            VALIDATOR_VERSION,
            needs_full_validation,
        )

        entry = {
            "validator_version": VALIDATOR_VERSION,
            "validated_through": "2026-06-08T23:31:00-04:00",
            "validated_rows": 100,
            "first_bar": "x",
        }
        # Same version + first_bar, row_count grew (pure append) → incremental safe.
        assert needs_full_validation(entry, first_bar="x", row_count=130) == (
            False,
            "",
        )
