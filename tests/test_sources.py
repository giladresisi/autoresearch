# tests/test_sources.py
# Unit tests for DatabentSource 1s interval support.
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch


class TestDatabentSource1s:
    def _make_source(self):
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            return DatabentSource()

    def test_databent_source_1s_calls_ohlcv_1s_schema(self):
        """fetch(..., interval="1s") must pass schema="ohlcv-1s" to get_range."""
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            src = DatabentSource()

        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="UTC")]),
        )
        mock_client.timeseries.get_range.return_value = mock_data

        with patch("databento.Historical", return_value=mock_client):
            src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        call_kwargs = mock_client.timeseries.get_range.call_args.kwargs
        assert call_kwargs.get("schema") == "ohlcv-1s"

    def test_databent_source_1s_returns_et_dataframe(self):
        """fetch(..., interval="1s") must return a DataFrame with America/New_York index."""
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            src = DatabentSource()

        utc_ts = pd.Timestamp("2026-05-01 14:30:00", tz="UTC")
        mock_client = MagicMock()
        mock_data = MagicMock()
        mock_data.to_df.return_value = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
            index=pd.DatetimeIndex([utc_ts]),
        )
        mock_client.timeseries.get_range.return_value = mock_data

        with patch("databento.Historical", return_value=mock_client):
            result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        assert result is not None
        assert str(result.index.tz) == "America/New_York"
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_databent_source_invalid_interval_raises(self):
        """fetch(..., interval="30m") must raise ValueError."""
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            src = DatabentSource()

        with pytest.raises(ValueError, match="30m"):
            src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="30m")

    def test_databent_source_retries_with_available_end_on_422(self):
        """When Databento returns data_end_after_available_end, fetch retries with the
        available end timestamp parsed from the error message."""
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            src = DatabentSource()

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

        with patch("databento.Historical", return_value=mock_client):
            result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-08T08:39:00Z", interval="1s")

        assert result is not None
        assert mock_client.timeseries.get_range.call_count == 2
        # Second call must use the available_end, not the original end
        retry_kwargs = mock_client.timeseries.get_range.call_args_list[1].kwargs
        assert retry_kwargs["end"] == available_end

    def test_databent_source_returns_none_on_unrelated_error(self):
        """Non-422 errors are not retried — returns None."""
        with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
            from data.sources import DatabentSource
            src = DatabentSource()

        mock_client = MagicMock()
        mock_client.timeseries.get_range.side_effect = Exception("network timeout")

        with patch("databento.Historical", return_value=mock_client):
            result = src.fetch("MNQ.v.0", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", interval="1s")

        assert result is None
        assert mock_client.timeseries.get_range.call_count == 1
