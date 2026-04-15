"""data/sources.py — DataSource protocol and concrete implementations.

Abstracts stock data fetching behind a common interface so prepare.py can
switch between yfinance and IB-Gateway without changing any caller logic.
"""
from __future__ import annotations
import warnings
from typing import Protocol, runtime_checkable

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    import types as _types
    yf = _types.SimpleNamespace(Ticker=None)  # type: ignore[assignment]


@runtime_checkable
class DataSource(Protocol):
    """Fetch raw OHLCV bars for a ticker over a date range at a given interval.

    Returns a DataFrame with a tz-aware DatetimeIndex and columns:
        Open, High, Low, Close, Volume  (uppercase, float64)
    Returns None on failure (network error, no data, unknown ticker).
    """
    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str,
    ) -> pd.DataFrame | None: ...


class YFinanceSource:
    """Fetch OHLCV from Yahoo Finance via yfinance."""

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1h",
    ) -> pd.DataFrame | None:
        if yf.Ticker is None:
            return None  # yfinance not installed; caller should handle None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                prepost=False,
            )
        if df.empty:
            return None
        # Explicit rename for the five OHLCV columns — safer than capitalize()
        # which would silently mangle any unexpected multi-word column names.
        _rename = {"open": "Open", "high": "High", "low": "Low",
                   "close": "Close", "volume": "Volume"}
        df = df.rename(columns=_rename)
        return df[["Open", "High", "Low", "Close", "Volume"]]


# Maps yfinance-style interval strings → IB barSizeSetting
_IB_BAR_SIZE: dict[str, str] = {
    "1m":  "1 min",
    "2m":  "2 mins",
    "5m":  "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "2h":  "2 hours",
    "4h":  "4 hours",
    "1d":  "1 day",
}

# Max calendar days for a single ContFuture reqHistoricalData call with endDateTime=''.
# error 10339 blocks explicit endDateTime for ContFuture at ALL intervals — must use ''.
# Empirically tested limits (ib_insync, 2026-04):
#   1m  → ≤14 D before timeout   → _IB_CONTFUTURE_MAX_DAYS
#   5m  → ≤45 D before timeout   → _IB_5M_CONTFUTURE_MAX_DAYS
_IB_CONTFUTURE_MAX_DAYS = 14
_IB_5M_CONTFUTURE_MAX_DAYS = 45

# Max calendar days to request per IB call at each interval
# (conservative — IB limits vary by subscription; these are safe defaults)
_IB_CHUNK_DAYS: dict[str, int] = {
    "1m":  29,
    "2m":  29,
    "5m":  60,
    "15m": 60,
    "30m": 180,
    "1h":  180,
    "2h":  365,
    "4h":  365,
    "1d":  3650,
}


def _third_friday(year: int, month: int) -> pd.Timestamp:
    """Return the 3rd Friday of the given year/month as a tz-naive Timestamp.

    CME equity-index futures (MNQ, MES, ES, NQ) expire on the 3rd Friday of
    their expiry month. IB requires the exact last-trade date in YYYYMMDD format.
    """
    import calendar as _cal
    # monthcalendar returns weeks as lists [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
    fridays = [week[4] for week in _cal.monthcalendar(year, month) if week[4] != 0]
    return pd.Timestamp(year=year, month=month, day=fridays[2])  # index 2 = 3rd Friday


def _quarterly_future_ranges(
    ticker: str, start_dt: pd.Timestamp, end_dt: pd.Timestamp
) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Return (YYYYMMDD, period_start, period_end) for CME quarterly futures covering [start_dt, end_dt].

    CME equity-index futures (MNQ, MES, ES, NQ) expire on the 3rd Friday of
    Mar/Jun/Sep/Dec. Each tuple's period runs from the previous expiry to the
    current one so the full date range is covered without gaps or double-fetching.
    IB requires the exact expiry date (YYYYMMDD) for historical data on specific
    Future contracts.
    """
    QUARTER_MONTHS = [3, 6, 9, 12]
    result: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    prev_boundary = start_dt

    for year in range(start_dt.year - 1, end_dt.year + 2):
        for month in QUARTER_MONTHS:
            expiry_naive = _third_friday(year, month)
            expiry = expiry_naive.tz_localize(start_dt.tzinfo) if start_dt.tzinfo else expiry_naive
            if expiry <= start_dt:
                prev_boundary = expiry
                continue
            period_start = max(prev_boundary, start_dt)
            period_end = min(expiry, end_dt)
            if period_start < period_end:
                result.append((expiry_naive.strftime("%Y%m%d"), period_start, period_end))
            prev_boundary = expiry
            if expiry >= end_dt:
                return result

    return result


def _to_et(ts_str: str) -> pd.Timestamp:
    """Return a tz-aware America/New_York Timestamp from a date or datetime string.

    Handles both naive strings ("2024-01-01") and tz-aware ISO strings
    ("2024-01-01T00:00:00+00:00") so callers don't need to normalize first.
    """
    ts = pd.Timestamp(ts_str)
    if ts.tzinfo is not None:
        return ts.tz_convert("America/New_York")
    return ts.tz_localize("America/New_York")


class DatabentSource:
    """Fetch OHLCV from Databento for CME Globex futures (GLBX.MDP3).

    Requires DATABENTO_API_KEY environment variable.
    Downloads 1m bars and optionally resamples to 5m.
    Returns tz-aware (America/New_York) DataFrame or None on failure.
    """

    def __init__(self) -> None:
        import os
        api_key = os.environ.get("DATABENTO_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DATABENTO_API_KEY environment variable is required for DatabentSource"
            )
        self._api_key = api_key

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "5m",
        **kwargs,
    ) -> pd.DataFrame | None:
        if interval not in ("1m", "5m"):
            raise ValueError(
                f"DatabentSource only supports 1m and 5m intervals, got {interval!r}"
            )
        import databento as db
        try:
            client = db.Historical(key=self._api_key)
            data = client.timeseries.get_range(
                dataset="GLBX.MDP3",
                symbols=[ticker],
                schema="ohlcv-1m",
                start=start,
                end=end,
                stype_in="continuous",
            )
            df = data.to_df()
        except Exception as exc:
            import sys
            print(f"DatabentSource: error fetching {ticker}: {exc}", file=sys.stderr)
            return None

        if df.empty:
            return None

        # Rename to standard uppercase OHLCV columns
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        df = df[["Open", "High", "Low", "Close", "Volume"]]

        # Resample 1m → 5m if requested
        if interval == "5m":
            df = df.resample("5min").agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }).dropna()

        # Convert UTC → America/New_York
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("America/New_York")

        return df if not df.empty else None


class IBGatewaySource:
    """Fetch OHLCV from Interactive Brokers via ib_insync.

    Requires IB Gateway (or TWS) running at host:port with API enabled.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id

    def fetch(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1h",
        contract_type: str = "stock",
    ) -> pd.DataFrame | None:
        # contract_type accepted values:
        #   "stock"            — equity via Stock(ticker, "SMART", "USD") with useRTH=True
        #   "contfuture"       — continuous CME future via ContFuture + endDateTime='' (most recent ≤45d)
        #   "future_by_conid"  — specific CME quarterly future identified by conId (ticker arg = conId string)
        from ib_insync import IB, Stock, Future, util

        if interval not in _IB_BAR_SIZE:
            print(f"  IBGatewaySource: unsupported interval '{interval}'")
            return None

        bar_size = _IB_BAR_SIZE[interval]
        chunk_days = _IB_CHUNK_DAYS[interval]

        ib = IB()
        try:
            ib.connect(self.host, self.port, clientId=self.client_id)

            # Normalize to ET regardless of whether the caller passed a tz-aware string
            start_dt = _to_et(start)
            end_dt   = _to_et(end)
            all_bars: list = []

            if contract_type == "contfuture":
                # IB rejects all explicit endDateTime for CME equity-index futures 1m bars
                # (error 10339 for ContFuture; error 162 / silent for specific contracts).
                # Only endDateTime='' (most recent data) works reliably.
                # IB limits 1m bar history to ≤30 calendar days per request.
                # We therefore fetch the most recent window bounded by:
                #   min(requested_window_days, chunk_days)
                from ib_insync import ContFuture
                contract = ContFuture(ticker, "CME", "USD")
                ib.qualifyContracts(contract)
                requested_days = max(1, (end_dt - start_dt).days)
                # Each interval has its own empirical cap with endDateTime=''.
                # Caps prevent timeouts; values verified against live IB-Gateway (2026-04).
                if interval == "1m":
                    duration_days = min(requested_days, _IB_CONTFUTURE_MAX_DAYS)
                elif interval == "5m":
                    duration_days = min(requested_days, _IB_5M_CONTFUTURE_MAX_DAYS)
                else:
                    duration_days = requested_days
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime="",  # '' = most recent; any explicit date is rejected by IB
                    durationStr=f"{duration_days} D",
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                )
                if bars:
                    all_bars.extend(bars)
            elif contract_type == "future_by_conid":
                # Fetch a specific futures contract by conId with explicit endDateTime
                # pagination. Avoids error 10339 that blocks ContFuture from using
                # explicit endDateTime — only works for non-ContFuture specific contracts.
                # `ticker` carries the conId string; exchange is always CME for MNQ/MES.
                from ib_insync import Contract as _IBContract
                contract = _IBContract(conId=int(ticker), exchange="CME")
                # No qualifyContracts: conId uniquely identifies the contract;
                # qualification is unnecessary and fails for expired contracts.
                chunk_end = end_dt
                while chunk_end > start_dt:
                    chunk_start = max(start_dt, chunk_end - pd.Timedelta(days=chunk_days))
                    duration_days = max(1, (chunk_end - chunk_start).days)
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                        durationStr=f"{duration_days} D",
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    )
                    if bars:
                        all_bars.extend(bars)
                    chunk_end = chunk_start
            else:
                contract = Stock(ticker, "SMART", "USD")
                ib.qualifyContracts(contract)

                # Paginate backwards from end_dt to start_dt in chunk_days windows.
                chunk_end = end_dt
                while chunk_end > start_dt:
                    chunk_start = max(start_dt, chunk_end - pd.Timedelta(days=chunk_days))
                    duration_days = max(1, (chunk_end - chunk_start).days)
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                        durationStr=f"{duration_days} D",
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=2,
                    )
                    if bars:
                        all_bars.extend(bars)
                    chunk_end = chunk_start

            if not all_bars:
                return None

            df = util.df(all_bars)
            df = df.rename(columns={
                "date":   "datetime",
                "open":   "Open",
                "high":   "High",
                "low":    "Low",
                "close":  "Close",
                "volume": "Volume",
            })
            df = df.set_index("datetime")
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("America/New_York")
            else:
                df.index = df.index.tz_convert("America/New_York")
            df = df.sort_index()
            # Deduplicate bars that appear in multiple pagination windows
            df = df[~df.index.duplicated(keep="last")]
            # Trim to requested window (start_dt / end_dt already ET-aware)
            df = df[(df.index >= start_dt) & (df.index < end_dt)]
            return df[["Open", "High", "Low", "Close", "Volume"]]

        except Exception as e:
            print(f"  IBGatewaySource: error fetching {ticker}: {e}")
            return None
        finally:
            if ib.isConnected():
                ib.disconnect()
