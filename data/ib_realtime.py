# IB realtime data source: wraps IB connection, tick subscription, and 1m bar
# assembly for MNQ/MES futures. Extracted from signal_smt.py to provide a
# standalone, reusable component that can be imported without triggering an
# IB connection (ib_insync is imported lazily inside start()).
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd


class IbRealtimeSource:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        mnq_conid: str,
        mes_conid: str,
        bar_data_dir: Path,
        on_bar: Callable,
        max_retries: int = 10,
        retry_delay_s: int = 15,
        on_bar_1m_complete: Callable | None = None,
    ) -> None:
        self._host           = host
        self._port           = port
        self._client_id      = client_id
        self._mnq_conid      = mnq_conid
        self._mes_conid      = mes_conid
        self._bar_data_dir   = bar_data_dir
        self._on_bar         = on_bar
        self._max_retries    = max_retries
        self._retry_delay_s  = retry_delay_s
        self._on_bar_1m_complete = on_bar_1m_complete
        self._ib             = None
        self._mnq_1m_df      = self._empty_bar_df()
        self._mes_1m_df      = self._empty_bar_df()
        self._mnq_partial_1m = None
        self._mes_partial_1m = None
        self._mnq_tick_bar   = None

    @property
    def mnq_1m_df(self) -> pd.DataFrame:
        return self._mnq_1m_df

    @property
    def mes_1m_df(self) -> pd.DataFrame:
        return self._mes_1m_df

    def _empty_bar_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([], tz="America/New_York"),
            dtype=float,
        )

    def _load_parquets(self) -> None:
        mnq_path = self._bar_data_dir / "MNQ_1m.parquet"
        mes_path  = self._bar_data_dir / "MES_1m.parquet"
        self._mnq_1m_df = pd.read_parquet(mnq_path) if mnq_path.exists() else self._empty_bar_df()
        self._mes_1m_df = pd.read_parquet(mes_path) if mes_path.exists() else self._empty_bar_df()

    def _gap_fill(self) -> None:
        from data.sources import IBGatewaySource
        MAX_LOOKBACK_DAYS = 30
        GAP_FILL_MAX_DAYS = 14
        now = pd.Timestamp.now(tz="America/New_York")
        today_midnight = now.normalize()  # 00:00 ET today — ensures TDO bar is always fetchable
        def _start_ts_for(df):
            gap_days = MAX_LOOKBACK_DAYS if df.empty else GAP_FILL_MAX_DAYS
            floor = now - pd.Timedelta(days=gap_days)
            if df.empty:
                return floor
            # Go back to the earlier of: last bar or today's midnight, so overnight bars
            # needed for TDO computation are always included in the fetch range.
            return max(min(df.index[-1], today_midnight), floor)
        mnq_start = _start_ts_for(self._mnq_1m_df)
        mes_start = _start_ts_for(self._mes_1m_df)
        mnq_start_str = mnq_start.isoformat()
        mes_start_str = mes_start.isoformat()
        end_str = now.isoformat()
        source = IBGatewaySource(host=self._host, port=self._port, client_id=self._client_id + 1)
        mnq_new = source.fetch(self._mnq_conid, mnq_start_str, end_str, interval="1m", contract_type="future_by_conid")
        mes_new = source.fetch(self._mes_conid, mes_start_str, end_str, interval="1m", contract_type="future_by_conid")
        if mnq_new is None or mnq_new.empty:
            print(f"[gap_fill] MNQ: 0 bars returned for {mnq_start_str} -> {end_str}", flush=True)
        if mes_new is None or mes_new.empty:
            print(f"[gap_fill] MES: 0 bars returned for {mes_start_str} -> {end_str}", flush=True)
        if mnq_new is not None and not mnq_new.empty:
            self._mnq_1m_df = pd.concat([self._mnq_1m_df, mnq_new]).sort_index()
            self._mnq_1m_df = self._mnq_1m_df[~self._mnq_1m_df.index.duplicated(keep="last")]
        if mes_new is not None and not mes_new.empty:
            self._mes_1m_df = pd.concat([self._mes_1m_df, mes_new]).sort_index()
            self._mes_1m_df = self._mes_1m_df[~self._mes_1m_df.index.duplicated(keep="last")]
        self._bar_data_dir.mkdir(parents=True, exist_ok=True)
        self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet")
        self._mes_1m_df.to_parquet(self._bar_data_dir / "MES_1m.parquet")

    def _bar_timestamp(self, bar) -> pd.Timestamp:
        ts = pd.Timestamp(getattr(bar, "date", None) or bar.name)
        if ts.tz is None:
            return ts.tz_localize("America/New_York")
        return ts.tz_convert("America/New_York")

    def _tick_second_ts(self, t) -> pd.Timestamp:
        ts = pd.Timestamp(t.time)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("America/New_York").floor("s")

    def _update_tick_accumulator(self, acc, price, size, second_ts):
        if acc is None or second_ts != acc["second_ts"]:
            finalized = acc
            new_acc = {"open": price, "high": price, "low": price,
                       "close": price, "volume": size, "second_ts": second_ts}
            return new_acc, finalized
        acc["high"]   = max(acc["high"], price)
        acc["low"]    = min(acc["low"], price)
        acc["close"]  = price
        acc["volume"] += size
        return acc, None

    def _update_partial_1m(self, acc, price, size, minute_ts):
        if acc is None or minute_ts != acc["minute_ts"]:
            return {"open": price, "high": price, "low": price, "close": price, "volume": size, "minute_ts": minute_ts}
        acc["high"]   = max(acc["high"], price)
        acc["low"]    = min(acc["low"], price)
        acc["close"]  = price
        acc["volume"] += size
        return acc

    def _partial_1m_to_bar_row(self, acc, ts):
        import strategy_smt
        return strategy_smt._BarRow(acc["open"], acc["high"], acc["low"], acc["close"], acc["volume"], ts)

    def _seed_from_history(self, bars, instrument: str) -> None:
        """Bulk-populate df from IB's initial historical batch (hasNewBar=False)."""
        rows = []
        timestamps = []
        for bar in bars:
            try:
                ts = self._bar_timestamp(bar)
                rows.append([float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)])
                timestamps.append(ts)
            except Exception:
                continue
        if not rows:
            return
        new_df = pd.DataFrame(
            rows,
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex(timestamps),
        )
        if instrument == "MNQ":
            combined = pd.concat([self._mnq_1m_df, new_df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            self._mnq_1m_df = combined
            self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet")
        else:
            combined = pd.concat([self._mes_1m_df, new_df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            self._mes_1m_df = combined
            self._mes_1m_df.to_parquet(self._bar_data_dir / "MES_1m.parquet")

    def _on_mnq_1m_bar(self, bars, hasNewBar) -> None:
        if not hasNewBar:
            self._seed_from_history(bars, "MNQ")
            return
        bar = bars[-1]
        bar_ts = self._bar_timestamp(bar)
        row = pd.DataFrame(
            [[float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([bar_ts]),
        )
        self._mnq_1m_df = pd.concat([self._mnq_1m_df, row])
        self._mnq_1m_df = self._mnq_1m_df[~self._mnq_1m_df.index.duplicated(keep="last")]
        self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet")
        # Reset second accumulator so last second of expiring minute does not bleed into the next
        self._mnq_tick_bar = None
        from strategy_smt import set_bar_data
        set_bar_data(self._mnq_1m_df, self._mes_1m_df)
        if self._on_bar_1m_complete is not None:
            self._on_bar_1m_complete(bars)

    def _on_mes_1m_bar(self, bars, hasNewBar) -> None:
        if not hasNewBar:
            self._seed_from_history(bars, "MES")
            return
        bar = bars[-1]
        bar_ts = self._bar_timestamp(bar)
        row = pd.DataFrame(
            [[float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([bar_ts]),
        )
        self._mes_1m_df = pd.concat([self._mes_1m_df, row])
        self._mes_1m_df = self._mes_1m_df[~self._mes_1m_df.index.duplicated(keep="last")]
        self._mes_1m_df.to_parquet(self._bar_data_dir / "MES_1m.parquet")
        from strategy_smt import set_bar_data
        set_bar_data(self._mnq_1m_df, self._mes_1m_df)

    def _on_mes_tick(self, ticker) -> None:
        if not ticker.tickByTicks:
            return
        t = ticker.tickByTicks[-1]
        second_ts = self._tick_second_ts(t)
        minute_ts = second_ts.floor("min")
        self._mes_partial_1m = self._update_partial_1m(self._mes_partial_1m, t.price, t.size, minute_ts)

    def _on_mnq_tick(self, ticker) -> None:
        if not ticker.tickByTicks:
            return
        t = ticker.tickByTicks[-1]
        second_ts = self._tick_second_ts(t)
        minute_ts = second_ts.floor("min")
        self._mnq_tick_bar, finalized = self._update_tick_accumulator(
            self._mnq_tick_bar, t.price, t.size, second_ts
        )
        if finalized is not None and self._mnq_partial_1m is not None:
            bar_row = self._partial_1m_to_bar_row(self._mnq_partial_1m, finalized["second_ts"])
            self._on_bar(bar_row, self._mes_partial_1m)
        self._mnq_partial_1m = self._update_partial_1m(self._mnq_partial_1m, t.price, t.size, minute_ts)

    def _setup_subscriptions(self, mnq_contract, mes_contract) -> None:
        mnq_1m = self._ib.reqHistoricalData(
            mnq_contract, endDateTime="", durationStr="3 D",
            barSizeSetting="1 min", whatToShow="TRADES",
            useRTH=False, formatDate=2, keepUpToDate=True,
        )
        mes_1m = self._ib.reqHistoricalData(
            mes_contract, endDateTime="", durationStr="3 D",
            barSizeSetting="1 min", whatToShow="TRADES",
            useRTH=False, formatDate=2, keepUpToDate=True,
        )
        mnq_tick = self._ib.reqTickByTickData(mnq_contract, "AllLast", 0, False)
        mes_tick  = self._ib.reqTickByTickData(mes_contract, "AllLast", 0, False)
        mnq_1m.updateEvent   += self._on_mnq_1m_bar
        mes_1m.updateEvent   += self._on_mes_1m_bar
        mnq_tick.updateEvent += self._on_mnq_tick
        mes_tick.updateEvent += self._on_mes_tick

    def start(self) -> None:
        import time
        from ib_insync import IB, Future, util
        self._load_parquets()
        self._gap_fill()
        mnq_contract = Future(conId=int(self._mnq_conid), exchange="CME")
        mes_contract = Future(conId=int(self._mes_conid), exchange="CME")
        for attempt in range(self._max_retries):
            try:
                self._ib = IB()
                self._ib.connect(self._host, self._port, clientId=self._client_id)
                self._setup_subscriptions(mnq_contract, mes_contract)
                util.run()
                if self._ib.isConnected():
                    break
                raise ConnectionError("IB disconnected unexpectedly")
            except Exception as exc:
                print(
                    f"[{attempt + 1}/{self._max_retries}] IB error: {exc}. "
                    f"Retrying in {self._retry_delay_s}s ...",
                    flush=True,
                )
                try:
                    if self._ib and self._ib.isConnected():
                        self._ib.disconnect()
                except Exception:
                    pass
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay_s)
        else:
            raise RuntimeError(f"IB connection failed after {self._max_retries} attempts")
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass
