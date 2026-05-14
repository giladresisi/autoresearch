# tests/test_sources.py
# Unit tests for DatabentSource 1s interval support.
import sys
import types
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch


def _ensure_databento_stub():
    """Insert a minimal stub for the ``databento`` package into sys.modules.

    DatabentSource does ``import databento as db`` inside __init__.  On
    machines where the real library is not installed we need a lightweight
    stub so that patch("databento.Historical", ...) can resolve the target
    without an ImportError.  The stub is inserted only once; subsequent calls
    are no-ops because the module is already cached.
    """
    if "databento" not in sys.modules:
        stub = types.ModuleType("databento")
        stub.Historical = MagicMock()  # will be overridden by patch() in each test
        sys.modules["databento"] = stub


class TestDatabentSource1s:
    def _make_source(self, mock_client=None):
        """Create a DatabentSource with databento.Historical patched at construction time."""
        _ensure_databento_stub()
        if mock_client is None:
            mock_client = MagicMock()
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}), \
             patch("databento.Historical", return_value=mock_client):
            from data.sources import DatabentSource
            return DatabentSource()

    def test_databent_source_1s_calls_ohlcv_1s_schema(self):
        """fetch(..., interval="1s") must pass schema="ohlcv-1s" to get_range."""
        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="UTC")]),
        )
        mock_client.timeseries.get_range.return_value = mock_data
        src = self._make_source(mock_client)

        src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        call_kwargs = mock_client.timeseries.get_range.call_args.kwargs
        assert call_kwargs.get("schema") == "ohlcv-1s"

    def test_databent_source_1s_returns_et_dataframe(self):
        """fetch(..., interval="1s") must return a DataFrame with America/New_York index."""
        utc_ts = pd.Timestamp("2026-05-01 14:30:00", tz="UTC")
        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([utc_ts]),
        )
        mock_client.timeseries.get_range.return_value = mock_data
        src = self._make_source(mock_client)

        result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        assert result is not None
        assert str(result.index.tz) == "America/New_York"
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_databent_source_invalid_interval_raises(self):
        """fetch(..., interval="30m") must raise ValueError."""
        src = self._make_source()

        with pytest.raises(ValueError, match="30m"):
            src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="30m")

    def test_databent_source_retries_with_available_end_on_422(self):
        """When Databento returns data_end_after_available_end, fetch retries with the
        available end timestamp parsed from the error message."""
        available_end = "2026-05-08 08:30:00+00:00"
        error_msg = (
            f"The dataset GLBX.MDP3 has data available up to '{available_end}'. "
            "The `end` in the query ('2026-05-08 08:39:00+00:00') is after the "
            "available range. [422 data_end_after_available_end]"
        )

        good_df = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-08 09:30:00", tz="UTC")]),
        )
        mock_retry_data = MagicMock()
        mock_retry_data.to_df.return_value = good_df

        mock_client = MagicMock()
        # First call raises; second (retry) returns data
        mock_client.timeseries.get_range.side_effect = [Exception(error_msg), mock_retry_data]
        src = self._make_source(mock_client)

        result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-08T08:39:00Z", interval="1s")

        assert result is not None
        assert mock_client.timeseries.get_range.call_count == 2
        # Second call must use the available_end, not the original end
        retry_kwargs = mock_client.timeseries.get_range.call_args_list[1].kwargs
        assert retry_kwargs["end"] == available_end

    def test_databent_source_returns_none_on_unrelated_error(self):
        """Non-422 errors are not retried — returns None."""
        mock_client = MagicMock()
        mock_client.timeseries.get_range.side_effect = Exception("network timeout")
        src = self._make_source(mock_client)

        result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        assert result is None
        assert mock_client.timeseries.get_range.call_count == 1

    def test_databento_client_instantiated_in_init(self):
        """databento.Historical must be called exactly once, during __init__, not during fetch."""
        _ensure_databento_stub()
        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="UTC")]),
        )
        mock_client.timeseries.get_range.return_value = mock_data

        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}), \
             patch("databento.Historical", return_value=mock_client) as mock_historical:
            from data.sources import DatabentSource
            src = DatabentSource()
            # Historical must have been called exactly once — during __init__
            assert mock_historical.call_count == 1
            # fetch() must not create another client
            src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")
            assert mock_historical.call_count == 1

    def test_databento_client_reused_across_fetch_calls(self):
        """databento.Historical must be called only once even after multiple fetch() calls."""
        _ensure_databento_stub()
        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="UTC")]),
        )
        mock_client.timeseries.get_range.return_value = mock_data

        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}), \
             patch("databento.Historical", return_value=mock_client) as mock_historical:
            from data.sources import DatabentSource
            src = DatabentSource()

            src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")
            src.fetch("MNQ.v.0", "2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z", interval="1s")

            # Client must have been constructed exactly once — the singleton is reused
            assert mock_historical.call_count == 1
