# IB realtime data source: wraps IB connection, tick subscription, and 1m bar
# assembly for MNQ/MES futures. Extracted from signal_smt.py to provide a
# standalone, reusable component that can be imported without triggering an
# IB connection (ib_insync is imported lazily inside start()).
from __future__ import annotations

# IB max duration per reqHistoricalData call for 1s bars (seconds)
_IB_1S_CHUNK_SECONDS = 1800
# Earliest timestamp for 1s gap-fill — prevents requesting unbounded historical data
_1S_EARLIEST = "2026-05-01"

from pathlib import Path
from typing import Callable

import pandas as pd


class IbGatewayDisconnectedError(Exception):
    """Raised when IB Gateway closes the connection (not a transient network error)."""


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
        self._stopping       = False
        self._event_loop     = None
        self._mnq_1m_df      = self._empty_bar_df()
        self._mes_1m_df      = self._empty_bar_df()
        self._mnq_partial_1m = None
        self._mes_partial_1m = None
        self._mnq_tick_bar   = None
        self._mes_tick_bar        = None
        self._mnq_1s_df           = self._empty_bar_df()   # historical, loaded from MNQ_1s.parquet
        self._mes_1s_df           = self._empty_bar_df()   # historical, loaded from MES_1s.parquet
        self._mnq_1s_pending: list[dict] = []               # tick bars buffered until next 1m boundary
        self._mes_1s_pending: list[dict] = []
        self._session_date        = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d")
        self._mnq_1s_session_df   = self._empty_bar_df()   # session bars (NOT written to main parquet)
        self._mes_1s_session_df   = self._empty_bar_df()   # session bars (NOT written to main parquet)

    @property
    def mnq_1m_df(self) -> pd.DataFrame:
        return self._mnq_1m_df

    @property
    def mes_1m_df(self) -> pd.DataFrame:
        return self._mes_1m_df

    @property
    def mnq_1s_df(self) -> pd.DataFrame:
        return self._mnq_1s_df

    @property
    def mes_1s_df(self) -> pd.DataFrame:
        return self._mes_1s_df

    def _empty_bar_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([], tz="America/New_York"),
            dtype=float,
        )

    def _load_parquets(self) -> None:
        for attr, filename in [
            ("_mnq_1m_df", "MNQ_1m.parquet"),
            ("_mes_1m_df", "MES_1m.parquet"),
            ("_mnq_1s_df", "MNQ_1s.parquet"),
            ("_mes_1s_df", "MES_1s.parquet"),
        ]:
            path = self._bar_data_dir / filename
            if not path.exists():
                setattr(self, attr, self._empty_bar_df())
                continue
            try:
                setattr(self, attr, pd.read_parquet(path))
            except Exception as exc:
                print(
                    f"[IbRealtimeSource] WARNING: {filename} corrupted ({exc}); recreating empty",
                    flush=True,
                )
                empty = self._empty_bar_df()
                try:
                    empty.to_parquet(path)
                except Exception:
                    pass
                setattr(self, attr, empty)

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

    def _gap_fill_1s_ib(self) -> None:
        """Fill recent 1s bars from IB: covers what Databento can't serve (last few hours).

        Uses a separate IB connection (client_id + 1) so it doesn't interfere with the
        main session connection. Called in start() after _load_parquets(), before the
        main retry loop. Skips instruments with an empty parquet — run seed script first.
        """
        from ib_insync import IB, Contract as _IBContract, util as _util
        now = pd.Timestamp.now(tz="America/New_York")
        end_dt = now - pd.Timedelta(minutes=2)  # avoid requesting in-progress bars

        pairs = [
            ("MNQ", "_mnq_1s_df", "MNQ_1s.parquet", self._mnq_conid),
            ("MES", "_mes_1s_df", "MES_1s.parquet", self._mes_conid),
        ]
        # Check if any fill is needed before opening an IB connection
        needs_fill = any(
            not getattr(self, df_attr).empty and
            (end_dt - getattr(self, df_attr).index[-1]).total_seconds() > 60
            for _, df_attr, _, _ in pairs
        )
        if not needs_fill:
            return

        ib = IB()
        try:
            ib.connect(self._host, self._port, clientId=self._client_id + 1)
            for instrument, df_attr, parquet_name, conid in pairs:
                try:
                    df = getattr(self, df_attr)
                    if df.empty:
                        print(f"[gap_fill_1s_ib] {instrument}: no seed data — skipping", flush=True)
                        continue
                    earliest = pd.Timestamp(_1S_EARLIEST, tz="America/New_York")
                    start_dt = max(df.index[-1], earliest)
                    if (end_dt - start_dt).total_seconds() <= 60:
                        continue
                    contract = _IBContract(conId=int(conid), exchange="CME")
                    all_bars: list = []
                    chunk_end = end_dt
                    while chunk_end > start_dt:
                        if self._stopping:
                            break
                        chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=_IB_1S_CHUNK_SECONDS))
                        chunk_s = max(1, int((chunk_end - chunk_start).total_seconds()))
                        bars = ib.reqHistoricalData(
                            contract,
                            endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                            durationStr=f"{chunk_s} S",
                            barSizeSetting="1 secs",
                            whatToShow="TRADES",
                            useRTH=False,
                            formatDate=2,
                        )
                        if bars:
                            all_bars.extend(bars)
                        chunk_end = chunk_start
                    if not all_bars:
                        print(f"[gap_fill_1s_ib] {instrument}: 0 bars returned", flush=True)
                        continue
                    new_df = _util.df(all_bars).rename(columns={
                        "date": "datetime", "open": "Open", "high": "High",
                        "low": "Low", "close": "Close", "volume": "Volume",
                    }).set_index("datetime")
                    if new_df.index.tzinfo is None:
                        new_df.index = new_df.index.tz_localize("America/New_York")
                    else:
                        new_df.index = new_df.index.tz_convert("America/New_York")
                    combined = pd.concat([df, new_df[["Open", "High", "Low", "Close", "Volume"]]]).sort_index()
                    combined = combined[~combined.index.duplicated(keep="last")]
                    setattr(self, df_attr, combined)
                    self._bar_data_dir.mkdir(parents=True, exist_ok=True)
                    combined.to_parquet(self._bar_data_dir / parquet_name)
                    print(f"[gap_fill_1s_ib] {instrument}: +{len(new_df)} 1s bars", flush=True)
                except Exception as exc:
                    print(f"[gap_fill_1s_ib] {instrument}: error: {exc}", flush=True)
        except Exception as exc:
            print(f"[gap_fill_1s_ib] connect error: {exc}", flush=True)
        finally:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass

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
        if self._mnq_1s_pending:
            rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
                    for p in self._mnq_1s_pending]
            ts_list = [p["second_ts"] for p in self._mnq_1s_pending]
            new_1s = pd.DataFrame(
                rows, columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex(ts_list),
            )
            self._mnq_1s_session_df = pd.concat([self._mnq_1s_session_df, new_1s]).sort_index()
            self._mnq_1s_session_df = self._mnq_1s_session_df[
                ~self._mnq_1s_session_df.index.duplicated(keep="last")
            ]
            session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
            self._mnq_1s_session_df.to_parquet(session_path)
            self._mnq_1s_pending.clear()
        self._mes_tick_bar = None  # reset alongside _mnq_tick_bar (same minute boundary)
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
        if self._mes_1s_pending:
            rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
                    for p in self._mes_1s_pending]
            ts_list = [p["second_ts"] for p in self._mes_1s_pending]
            new_1s = pd.DataFrame(
                rows, columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex(ts_list),
            )
            self._mes_1s_session_df = pd.concat([self._mes_1s_session_df, new_1s]).sort_index()
            self._mes_1s_session_df = self._mes_1s_session_df[
                ~self._mes_1s_session_df.index.duplicated(keep="last")
            ]
            session_path = self._bar_data_dir / f"MES_1s_session_{self._session_date}.parquet"
            self._mes_1s_session_df.to_parquet(session_path)
            self._mes_1s_pending.clear()
        from strategy_smt import set_bar_data
        set_bar_data(self._mnq_1m_df, self._mes_1m_df)

    def _on_mes_tick(self, ticker) -> None:
        if not ticker.tickByTicks:
            return
        t = ticker.tickByTicks[-1]
        second_ts = self._tick_second_ts(t)
        minute_ts = second_ts.floor("min")
        self._mes_tick_bar, mes_finalized = self._update_tick_accumulator(
            self._mes_tick_bar, t.price, t.size, second_ts
        )
        if mes_finalized is not None:
            self._mes_1s_pending.append(mes_finalized)
        self._mes_partial_1m = self._update_partial_1m(
            self._mes_partial_1m, t.price, t.size, minute_ts
        )

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
        if finalized is not None:
            self._mnq_1s_pending.append(finalized)
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
        import asyncio, time
        from ib_insync import IB, Future, util
        # ib_insync requires an asyncio event loop in the calling thread.
        # Daemon threads have none — create one before any IB calls.
        asyncio.set_event_loop(asyncio.new_event_loop())
        self._load_parquets()
        self._gap_fill_1s_ib()
        mnq_contract = Future(conId=int(self._mnq_conid), exchange="CME")
        mes_contract = Future(conId=int(self._mes_conid), exchange="CME")
        for attempt in range(self._max_retries):
            try:
                self._ib = IB()
                self._disconnected_by_gateway = False
                self._event_loop = util.getLoop()

                # Detect gateway-initiated disconnects (not our own stop() call).
                self._ib.disconnectedEvent += self._on_gateway_disconnect
                self._ib.connect(self._host, self._port, clientId=self._client_id)
                self._setup_subscriptions(mnq_contract, mes_contract)
                util.run()
                if self._stopping:
                    break  # deliberate stop() call — exit retry loop without error
                if self._disconnected_by_gateway:
                    raise IbGatewayDisconnectedError("IB Gateway closed the connection")
                raise ConnectionError("IB disconnected unexpectedly")
            except IbGatewayDisconnectedError:
                raise  # propagate immediately — do not retry
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

    def _on_gateway_disconnect(self) -> None:
        """Callback wired to ib.disconnectedEvent — fires when the gateway closes the connection.

        Ignored when _stopping=True because that means we called stop() ourselves.
        Runs inside the ib_insync event loop thread, so loop.stop() is safe here.
        """
        if not self._stopping:
            self._disconnected_by_gateway = True
            self._event_loop.stop()

    def stop(self) -> None:
        self._stopping = True
        # Stop the asyncio event loop from any thread — loop.call_soon_threadsafe is the
        # correct cross-thread API; loop.stop() alone is only safe from inside the loop.
        try:
            if self._event_loop and self._event_loop.is_running():
                self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        except Exception:
            pass
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass


def gap_fill_1m_ib(bar_data_dir: Path) -> None:
    """Standalone 1m bar gap-fill from IB: called at orchestrator startup.

    Reads IB_HOST, IB_PORT, MNQ_CONID, MES_CONID from environment.
    Uses client_id=17 (distinct from all other IB clients in the system).
    Skips gracefully if required env vars are absent or IB is unreachable.
    """
    import os
    from data.sources import IBGatewaySource

    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    mnq_conid = os.environ.get("MNQ_CONID")
    mes_conid = os.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print("[gap_fill_1m_ib] MNQ_CONID/MES_CONID not set — skipping", flush=True)
        return

    MAX_LOOKBACK_DAYS = 30
    GAP_FILL_MAX_DAYS = 14

    now = pd.Timestamp.now(tz="America/New_York")
    today_midnight = now.normalize()

    def _safe_read(path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        try:
            return pd.read_parquet(path)
        except Exception:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def _start_ts_for(df: pd.DataFrame) -> pd.Timestamp:
        gap_days = MAX_LOOKBACK_DAYS if df.empty else GAP_FILL_MAX_DAYS
        floor = now - pd.Timedelta(days=gap_days)
        if df.empty:
            return floor
        return max(min(df.index[-1], today_midnight), floor)

    mnq_df = _safe_read(bar_data_dir / "MNQ_1m.parquet")
    mes_df = _safe_read(bar_data_dir / "MES_1m.parquet")
    mnq_start = _start_ts_for(mnq_df)
    mes_start = _start_ts_for(mes_df)
    end_str = now.isoformat()

    print(f"[gap_fill_1m_ib] MNQ: gap-filling from {mnq_start.isoformat()} ...", flush=True)
    print(f"[gap_fill_1m_ib] MES: gap-filling from {mes_start.isoformat()} ...", flush=True)

    source = IBGatewaySource(host=host, port=port, client_id=17)
    mnq_new = source.fetch(mnq_conid, mnq_start.isoformat(), end_str, interval="1m", contract_type="future_by_conid")
    mes_new = source.fetch(mes_conid, mes_start.isoformat(), end_str, interval="1m", contract_type="future_by_conid")

    bar_data_dir.mkdir(parents=True, exist_ok=True)
    for instrument, df, new_df, fname in [
        ("MNQ", mnq_df, mnq_new, "MNQ_1m.parquet"),
        ("MES", mes_df, mes_new, "MES_1m.parquet"),
    ]:
        if new_df is None or new_df.empty:
            print(f"[gap_fill_1m_ib] {instrument}: 0 new bars", flush=True)
            continue
        combined = pd.concat([df, new_df]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.to_parquet(bar_data_dir / fname)
        print(f"[gap_fill_1m_ib] {instrument}: +{len(new_df)} bars", flush=True)
