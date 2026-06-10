# IB realtime data source: wraps IB connection, tick subscription, and 1m bar
# assembly for MNQ/MES futures. Extracted from signal_smt.py to provide a
# standalone, reusable component that can be imported without triggering an
# IB connection (ib_insync is imported lazily inside start()).
from __future__ import annotations

# IB max duration per reqHistoricalData call for 1s bars (seconds)
_IB_1S_CHUNK_SECONDS = 1800
# Earliest timestamp for 1s gap-fill — prevents requesting unbounded historical data
_1S_EARLIEST = "2026-05-01"

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pandas as pd

from strategy_smt import set_bar_data as _set_bar_data


def _next_trading_open(ts: "pd.Timestamp") -> "pd.Timestamp":
    """Return ts if it falls within CME Globex trading hours for MNQ/MES, else the
    next session-open timestamp (skipping weekends and the daily maintenance break).

    Used by the 1s gap-fill to advance over non-trading windows WITHOUT issuing IB
    requests. Without this, a forward fill that starts before a weekend walks ~49h of
    closed market 30-min-chunk-by-chunk; IB returns stale prior-session bars for each
    closed-window endDateTime, so the request count blows past IB's 60-per-10-min
    pacing limit before the real post-reopen data is ever reached.

    CME Globex schedule (ET):
      Opens:       Sunday 18:00
      Daily break: 17:00-18:00 Mon-Thu
      Weekend:     Friday 17:00 through Sunday 18:00
    """
    ts_et = ts.tz_convert("America/New_York")
    dow = ts_et.weekday()  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun
    t = ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second

    CLOSE = 17 * 3600
    OPEN = 18 * 3600

    in_wknd = (dow == 4 and t >= CLOSE) or dow == 5 or (dow == 6 and t < OPEN)
    in_break = (dow <= 3) and (CLOSE <= t < OPEN)  # Mon-Thu maintenance hour

    if in_wknd:
        days_to_sun = (6 - dow) % 7
        sun = (ts_et + pd.Timedelta(days=days_to_sun)).normalize()
        return sun + pd.Timedelta(hours=18)
    if in_break:
        return ts_et.normalize() + pd.Timedelta(hours=18)
    return ts_et


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
        watchdog_timeout_s: int = 120,
        watchdog_termination_grace_s: int = 30,
        comments_path: Path | None = None,
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
        self._mnq_contract   = None  # saved in _setup_subscriptions for cancelMktData
        self._mes_contract   = None
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
        # Single-worker executor serializes all parquet writes off the IB event loop thread.
        # max_workers=1 prevents concurrent writes to the same file.
        self._parquet_executor    = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parq")
        self._last_seed_count: dict[str, int] = {"MNQ": 0, "MES": 0}
        self._watchdog_timeout_s: int = watchdog_timeout_s
        self._watchdog_termination_grace_s: int = watchdog_termination_grace_s
        self._comments_path: Path | None = comments_path
        self._last_data_ts: float = 0.0
        self._watchdog_thread: threading.Thread | None = None
        self._ib_connectivity_lost: bool = False  # tracks 1100/1102 cycle for comment dedup

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

    def _submit_parquet_write(self, df: pd.DataFrame, path: Path) -> None:
        """Submit an atomic parquet write to the executor (write-to-tmp then os.replace)."""
        def _write(snap=df, dst=path):
            tmp = dst.with_name(f"{dst.stem}.{uuid4().hex}.parquet.tmp")
            try:
                snap.to_parquet(tmp, use_dictionary=False)
                os.replace(tmp, dst)
            except Exception as exc:
                print(f"[parquet_write] ERROR writing {dst.name}: {exc}", flush=True)
                raise
        self._parquet_executor.submit(_write)

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
                    tmp = path.with_name(f"{path.stem}.{uuid4().hex}.parquet.tmp")
                    empty.to_parquet(tmp, use_dictionary=False)
                    os.replace(tmp, path)
                except Exception:
                    pass
                setattr(self, attr, empty)

        # Re-hydrate session DFs from any session file written by a prior run this day.
        # Without this, each automation.main restart starts _mnq/mes_1s_session_df empty,
        # and the first live flush overwrites the existing session file with only the new
        # run's bars — silently discarding bars written by all prior runs.
        # Concretely: on the 2026-05-29 session, multiple restarts caused each new run to
        # overwrite the MNQ session file.  The last short-lived run wrote nothing (no 1m
        # boundary crossed before session end), leaving no session file for merge.
        for sess_attr, inst in (("_mnq_1s_session_df", "MNQ"), ("_mes_1s_session_df", "MES")):
            session_path = self._bar_data_dir / f"{inst}_1s_session_{self._session_date}.parquet"
            if not session_path.exists():
                continue
            try:
                existing = pd.read_parquet(session_path)
                if not existing.empty:
                    setattr(self, sess_attr, existing)
                    print(
                        f"[IbRealtimeSource] Resumed session file {session_path.name} "
                        f"({len(existing)} bars)",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[IbRealtimeSource] WARNING: could not resume {session_path.name}: {exc}",
                    flush=True,
                )

    def _gap_fill(self) -> None:
        from data.sources import IBGatewaySource
        MAX_LOOKBACK_DAYS = 30
        GAP_FILL_MAX_DAYS = 14
        now = pd.Timestamp.now(tz="America/New_York")
        def _start_ts_for(df):
            gap_days = MAX_LOOKBACK_DAYS if df.empty else GAP_FILL_MAX_DAYS
            floor = now - pd.Timedelta(days=gap_days)
            if df.empty:
                return floor
            return max(df.index[-1], floor)
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
        self._submit_parquet_write(self._mnq_1m_df.copy(), self._bar_data_dir / "MNQ_1m.parquet")
        self._submit_parquet_write(self._mes_1m_df.copy(), self._bar_data_dir / "MES_1m.parquet")

    def _gap_fill_1s_ib(self) -> bool:
        """Fill recent 1s bars from IB (forward iteration, best-effort).

        Iterates forward in 1800-second chunks from main[-1] toward now.
        Stops naturally when IB returns 0 bars (IB's recency boundary).
        The remaining tail gap is filled by merge_session_1s_parquets at session end.

        Returns True if all instruments that needed filling received ≥1 bar
        (or no fill was needed). Returns False if any instrument received 0 bars,
        indicating IB is unreachable or has no data for this period.
        """
        from ib_insync import IB, Contract as _IBContract, util as _util
        now = pd.Timestamp.now(tz="America/New_York")

        pairs = [
            ("MNQ", "_mnq_1s_df", "MNQ_1s.parquet", self._mnq_conid),
            ("MES", "_mes_1s_df", "MES_1s.parquet", self._mes_conid),
        ]
        needs_fill = any(
            not getattr(self, df_attr).empty and
            (now - getattr(self, df_attr).index[-1]).total_seconds() > 60
            for _, df_attr, _, _ in pairs
        )
        if not needs_fill:
            return True

        all_filled = True
        ib = IB()
        try:
            ib.connect(self._host, self._port, clientId=self._client_id + 1)
            for instrument, df_attr, parquet_name, conid in pairs:
                if self._stopping:
                    return True  # clean stop, not a failure
                try:
                    df = getattr(self, df_attr)
                    earliest = pd.Timestamp(_1S_EARLIEST, tz="America/New_York")
                    start_dt = earliest if df.empty else max(df.index[-1], earliest)
                    if (now - start_dt).total_seconds() <= 60:
                        continue
                    contract = _IBContract(conId=int(conid), exchange="CME")
                    all_bars: list = []
                    chunk_start = start_dt
                    consecutive_skips = 0
                    requested_any = False
                    while chunk_start < now:
                        if self._stopping:
                            break
                        # Skip weekends and the daily maintenance break without issuing
                        # IB requests — closed-window endDateTimes return stale prior-session
                        # bars that waste the pacing budget (see _next_trading_open).
                        adj = _next_trading_open(chunk_start)
                        if adj > chunk_start:
                            chunk_start = adj
                            continue
                        chunk_end = min(chunk_start + pd.Timedelta(seconds=_IB_1S_CHUNK_SECONDS), now)
                        chunk_s = max(1, int((chunk_end - chunk_start).total_seconds()))
                        requested_any = True
                        bars = ib.reqHistoricalData(
                            contract,
                            endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                            durationStr=f"{chunk_s} S",
                            barSizeSetting="1 secs",
                            whatToShow="TRADES",
                            useRTH=False,
                            formatDate=2,
                            keepUpToDate=False,
                        )
                        if not bars:
                            lag_min = int((now - chunk_end).total_seconds() / 60)
                            if lag_min > 65:
                                # Far from now: expected gap (maintenance window, holiday).
                                # Skip and continue — don't treat this as recency boundary.
                                consecutive_skips += 1
                                if consecutive_skips > 3:
                                    print(
                                        f"[gap_fill_1s_ib] {instrument}: >3 consecutive zero-bar chunks "
                                        f"far from now — stopping at {chunk_end.strftime('%H:%M ET')}",
                                        flush=True,
                                    )
                                    break
                                chunk_start = chunk_end
                                continue
                            print(
                                f"[gap_fill_1s_ib] {instrument}: IB recency boundary "
                                f"~{lag_min} min before now — stopping forward fill",
                                flush=True,
                            )
                            break
                        consecutive_skips = 0
                        all_bars.extend(bars)
                        chunk_start = chunk_end

                    if not all_bars:
                        if not requested_any:
                            # Entire fill window was non-trading (e.g. started over a
                            # weekend) — nothing to fill, not an IB failure.
                            print(f"[gap_fill_1s_ib] {instrument}: market closed across fill window — nothing to fill", flush=True)
                        else:
                            print(f"[gap_fill_1s_ib] {instrument}: 0 bars returned — IB unavailable", flush=True)
                            all_filled = False
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
                    self._submit_parquet_write(combined.copy(), self._bar_data_dir / parquet_name)
                    print(f"[gap_fill_1s_ib] {instrument}: +{len(new_df)} 1s bars", flush=True)
                except Exception as exc:
                    print(f"[gap_fill_1s_ib] {instrument}: error: {exc}", flush=True)
                    all_filled = False
        except Exception as exc:
            print(f"[gap_fill_1s_ib] connect error: {exc}", flush=True)
            all_filled = False
        finally:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass
        return all_filled

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
        if len(bars) == self._last_seed_count[instrument]:
            return  # callback fired with no new bars — skip redundant dedup work
        self._last_seed_count[instrument] = len(bars)
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
            self._submit_parquet_write(combined.copy(), self._bar_data_dir / "MNQ_1m.parquet")
        else:
            combined = pd.concat([self._mes_1m_df, new_df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            self._mes_1m_df = combined
            self._submit_parquet_write(combined.copy(), self._bar_data_dir / "MES_1m.parquet")

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
        _mnq_snap = self._mnq_1m_df  # capture reference before possible trim
        _mnq_path = self._bar_data_dir / "MNQ_1m.parquet"
        self._submit_parquet_write(_mnq_snap, _mnq_path)
        # Trim to 14-day window after write is submitted; full history preserved on disk via _mnq_snap
        _cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
        if (not self._mnq_1m_df.empty
                and self._mnq_1m_df.index.tz is not None
                and self._mnq_1m_df.index[0] < _cutoff):
            self._mnq_1m_df = self._mnq_1m_df[self._mnq_1m_df.index >= _cutoff].copy()
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
            _ses_snap = self._mnq_1s_session_df  # capture before clear
            self._parquet_executor.submit(_ses_snap.to_parquet, session_path)
            self._mnq_1s_session_df = self._empty_bar_df()  # clear after submission; parquet is durable record
            self._mnq_1s_pending.clear()
        self._mes_tick_bar = None  # reset alongside _mnq_tick_bar (same minute boundary)
        # Reset second accumulator so last second of expiring minute does not bleed into the next
        self._mnq_tick_bar = None
        _set_bar_data(self._mnq_1m_df, self._mes_1m_df)
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
        _mes_snap = self._mes_1m_df  # capture reference before possible trim
        _mes_path = self._bar_data_dir / "MES_1m.parquet"
        self._submit_parquet_write(_mes_snap, _mes_path)
        # Trim to 14-day window after write is submitted; .copy() breaks view chain so old array is GC'd
        _cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
        if (not self._mes_1m_df.empty
                and self._mes_1m_df.index.tz is not None
                and self._mes_1m_df.index[0] < _cutoff):
            self._mes_1m_df = self._mes_1m_df[self._mes_1m_df.index >= _cutoff].copy()
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
            _ses_snap = self._mes_1s_session_df  # capture before clear
            self._parquet_executor.submit(_ses_snap.to_parquet, session_path)
            self._mes_1s_session_df = self._empty_bar_df()  # clear after submission; parquet is durable record
            self._mes_1s_pending.clear()
        _set_bar_data(self._mnq_1m_df, self._mes_1m_df)

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

    def _on_mnq_mkt_data(self, ticker) -> None:
        self._last_data_ts = time.monotonic()
        import math as _math
        for tick in ticker.ticks:
            if tick.tickType != 4:
                continue
            price = tick.price
            if not price or _math.isnan(price) or price <= 0:
                continue
            size = ticker.lastSize or 0
            ts = pd.Timestamp(tick.time) if tick.time else pd.Timestamp.now(tz="UTC")
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            second_ts = ts.tz_convert("America/New_York").floor("s")
            minute_ts = second_ts.floor("min")
            old_minute_ts = self._mnq_partial_1m["minute_ts"] if self._mnq_partial_1m else None
            self._mnq_tick_bar, finalized = self._update_tick_accumulator(
                self._mnq_tick_bar, price, size, second_ts
            )
            if finalized is not None and self._mnq_partial_1m is not None:
                bar_row = self._partial_1m_to_bar_row(self._mnq_partial_1m, finalized["second_ts"])
                self._on_bar(bar_row, self._mes_partial_1m)
            if finalized is not None:
                self._mnq_1s_pending.append(finalized)
            if old_minute_ts is not None and minute_ts != old_minute_ts:
                self._flush_completed_1m_bar("MNQ", self._mnq_partial_1m, old_minute_ts)
            self._mnq_partial_1m = self._update_partial_1m(
                self._mnq_partial_1m, price, size, minute_ts
            )

    def _on_mes_mkt_data(self, ticker) -> None:
        import math as _math
        for tick in ticker.ticks:
            if tick.tickType != 4:
                continue
            price = tick.price
            if not price or _math.isnan(price) or price <= 0:
                continue
            size = ticker.lastSize or 0
            ts = pd.Timestamp(tick.time) if tick.time else pd.Timestamp.now(tz="UTC")
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            second_ts = ts.tz_convert("America/New_York").floor("s")
            minute_ts = second_ts.floor("min")
            old_minute_ts = self._mes_partial_1m["minute_ts"] if self._mes_partial_1m else None
            self._mes_tick_bar, mes_finalized = self._update_tick_accumulator(
                self._mes_tick_bar, price, size, second_ts
            )
            if mes_finalized is not None:
                self._mes_1s_pending.append(mes_finalized)
            if old_minute_ts is not None and minute_ts != old_minute_ts:
                self._flush_completed_1m_bar("MES", self._mes_partial_1m, old_minute_ts)
            self._mes_partial_1m = self._update_partial_1m(
                self._mes_partial_1m, price, size, minute_ts
            )

    def _flush_1s_pending_to_session_file(self, instrument: str) -> None:
        """Flush pending 1s bars to the session parquet (not the main parquet).

        Session DF accumulates all bars for this session in memory; the session
        parquet is a crash-durable snapshot overwritten atomically each flush.
        merge_session_1s_parquets() merges it into the main parquet at session end.
        """
        pending = self._mnq_1s_pending if instrument == "MNQ" else self._mes_1s_pending
        if not pending:
            return
        snap = pending[:]
        pending.clear()
        new_df = pd.DataFrame(
            [[p["open"], p["high"], p["low"], p["close"], p["volume"]] for p in snap],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([p["second_ts"] for p in snap]),
        )
        if instrument == "MNQ":
            self._mnq_1s_session_df = pd.concat([self._mnq_1s_session_df, new_df]).sort_index()
            self._mnq_1s_session_df = self._mnq_1s_session_df[
                ~self._mnq_1s_session_df.index.duplicated(keep="last")
            ]
            session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
            ses_snap = self._mnq_1s_session_df.copy()
        else:
            self._mes_1s_session_df = pd.concat([self._mes_1s_session_df, new_df]).sort_index()
            self._mes_1s_session_df = self._mes_1s_session_df[
                ~self._mes_1s_session_df.index.duplicated(keep="last")
            ]
            session_path = self._bar_data_dir / f"MES_1s_session_{self._session_date}.parquet"
            ses_snap = self._mes_1s_session_df.copy()
        self._submit_parquet_write(ses_snap, session_path)

    def _flush_completed_1m_bar(self, instrument: str, partial_1m, bar_ts) -> None:
        row = pd.DataFrame(
            [[partial_1m["open"], partial_1m["high"], partial_1m["low"],
              partial_1m["close"], partial_1m["volume"]]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([bar_ts]),
        )
        if instrument == "MNQ":
            self._mnq_1m_df = pd.concat([self._mnq_1m_df, row])
            self._mnq_1m_df = self._mnq_1m_df[~self._mnq_1m_df.index.duplicated(keep="last")]
            self._submit_parquet_write(self._mnq_1m_df.copy(), self._bar_data_dir / "MNQ_1m.parquet")
        else:
            self._mes_1m_df = pd.concat([self._mes_1m_df, row])
            self._mes_1m_df = self._mes_1m_df[~self._mes_1m_df.index.duplicated(keep="last")]
            self._submit_parquet_write(self._mes_1m_df.copy(), self._bar_data_dir / "MES_1m.parquet")
        self._flush_1s_pending_to_session_file(instrument)
        # Cross-flush: also flush the other instrument's pending at every 1m boundary.
        # MES ticks (reqMktData) can arrive in large batches whose timestamps collapse to
        # the same wall-clock second, preventing _update_tick_accumulator from ever
        # finalising a bar. Piggybacking on MNQ boundaries (which always fire) ensures
        # the MES session parquet is written even when MES tick timing is unfavourable.
        other = "MES" if instrument == "MNQ" else "MNQ"
        # Finalize the other instrument's in-progress tick bar before flushing pending.
        # Without this, the partial-second bar in _mes_tick_bar/_mnq_tick_bar is never
        # appended to pending when tick timestamps collapse (all ticks share same lastTime),
        # so only bars that happened to cross a second boundary naturally are ever written.
        other_tick_attr    = "_mes_tick_bar"    if instrument == "MNQ" else "_mnq_tick_bar"
        other_pending_attr = "_mes_1s_pending"  if instrument == "MNQ" else "_mnq_1s_pending"
        other_tick_bar = getattr(self, other_tick_attr)
        if other_tick_bar is not None:
            getattr(self, other_pending_attr).append(other_tick_bar)
            setattr(self, other_tick_attr, None)
        self._flush_1s_pending_to_session_file(other)
        _set_bar_data(self._mnq_1m_df, self._mes_1m_df)
        if instrument == "MNQ" and self._on_bar_1m_complete is not None:
            from types import SimpleNamespace
            self._on_bar_1m_complete([SimpleNamespace(date=bar_ts)])

    def _setup_subscriptions(self, mnq_contract, mes_contract) -> None:
        self._mnq_contract = mnq_contract
        self._mes_contract = mes_contract
        mnq_t = self._ib.reqMktData(mnq_contract, "", False, False)
        mes_t = self._ib.reqMktData(mes_contract, "", False, False)
        mnq_t.updateEvent += self._on_mnq_mkt_data
        mes_t.updateEvent += self._on_mes_mkt_data

        # Parquets are already loaded — fire _on_bar_1m_complete after 30s so
        # session init (daily.py, hypothesis) doesn't wait for a minute boundary.
        if self._on_bar_1m_complete is not None and not self._mnq_1m_df.empty:
            import threading as _threading
            def _parquet_fallback():
                from types import SimpleNamespace
                self._on_bar_1m_complete([SimpleNamespace(
                    date=pd.Timestamp.now(tz="America/New_York")
                )])
            _threading.Timer(30.0, _parquet_fallback).start()

    def gap_fill(self) -> None:
        """Backfill the main 1s then 1m parquets from IB up to now (no live streaming).

        This is the fill prologue start() runs at orchestrator startup, factored out so it
        can also be invoked standalone (e.g. gap_fill.gap_fill_until_now / `trade.py gap-fill`)
        for a fill-only pass without opening real-time subscriptions. Raises RuntimeError if
        the 1s fill returns no bars (IB unreachable / no data), which blocks the 1m fill.
        """
        self._load_parquets()
        if not self._gap_fill_1s_ib():
            raise RuntimeError(
                "[gap_fill_1s_ib] failed to fill any 1s bars — "
                "IB unreachable or returned no data; 1m gap-fill blocked"
            )
        print("[gap_fill_1s_ib] complete", flush=True)
        gap_fill_1m_ib(self._bar_data_dir)  # calls check_parquet_gaps internally
        print("[gap_fill_1m_ib] IB 1m gap fill complete", flush=True)
        # Reload 1m dfs so the in-memory state matches the gap-filled parquet files.
        # Without this, the first live 1m bar write would overwrite any bars added by
        # gap_fill_1m_ib with the stale df that was loaded before gap-fill ran.
        self._load_parquets()

    def start(self) -> None:
        import asyncio, time
        # eventkit (ib_insync dependency) calls get_event_loop() at module import time;
        # non-main threads have no loop, so create one before the import.
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB, Future, util
        self.gap_fill()
        # Release ~70 MB: history only needed by _gap_fill_1s_ib; live signal path reads parquet
        self._mnq_1s_df = self._empty_bar_df()
        self._mes_1s_df = self._empty_bar_df()
        mnq_contract = Future(conId=int(self._mnq_conid), exchange="CME")
        mes_contract = Future(conId=int(self._mes_conid), exchange="CME")
        for attempt in range(self._max_retries):
            try:
                self._ib = IB()
                self._disconnected_by_gateway = False
                self._event_loop = util.getLoop()

                # Detect gateway-initiated disconnects (not our own stop() call).
                self._ib.disconnectedEvent += self._on_gateway_disconnect
                self._ib.errorEvent += self._on_ib_error
                self._ib.connect(self._host, self._port, clientId=self._client_id)
                self._setup_subscriptions(mnq_contract, mes_contract)
                self._last_data_ts = time.monotonic()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop, daemon=True, name="ib-watchdog"
                )
                self._watchdog_thread.start()
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

    def _write_session_comment(self, text: str) -> None:
        if self._comments_path is None:
            return
        ts = pd.Timestamp.now(tz="America/New_York")
        needs_newline = self._comments_path.exists() and self._comments_path.stat().st_size > 0
        prefix = "\n" if needs_newline else ""
        entry = f"{prefix}## {ts.strftime('%Y-%m-%d %H:%M:%S ET')}\n{text}\n"
        try:
            self._comments_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._comments_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as exc:
            print(f"[ib-watchdog] WARNING: could not write comment: {exc}", flush=True)

    def _watchdog_loop(self) -> None:
        while not self._stopping:
            time.sleep(5)
            if self._stopping:
                break
            elapsed = time.monotonic() - self._last_data_ts
            if elapsed < self._watchdog_timeout_s:
                continue

            # Phase 1: zombie detected — log and enter recovery window
            now_et = pd.Timestamp.now(tz="America/New_York")
            last_data_et = now_et - pd.Timedelta(seconds=elapsed)
            print(
                f"[ib-watchdog] No data for {elapsed:.0f}s -- "
                f"zombie suspected, waiting {self._watchdog_termination_grace_s}s for recovery",
                flush=True,
            )
            self._write_session_comment(
                f"IB zombie suspected -- no data for {elapsed:.0f}s "
                f"(last data ~{last_data_et.strftime('%H:%M:%S ET')}). "
                f"Waiting {self._watchdog_termination_grace_s}s for IB to recover."
            )

            # Phase 2: recovery window
            grace_start = time.monotonic()
            recovered = False
            while time.monotonic() - grace_start < self._watchdog_termination_grace_s:
                time.sleep(1)
                if self._stopping:
                    return
                if time.monotonic() - self._last_data_ts < self._watchdog_timeout_s:
                    recovered = True
                    break

            if recovered:
                reconnect_et = pd.Timestamp.now(tz="America/New_York")
                print(
                    f"[ib-watchdog] IB recovered — data resumed after ~{elapsed:.0f}s gap",
                    flush=True,
                )
                self._write_session_comment(
                    f"IB recovered at {reconnect_et.strftime('%H:%M:%S ET')} "
                    f"after ~{elapsed:.0f}s gap "
                    f"({last_data_et.strftime('%H:%M:%S')} - {reconnect_et.strftime('%H:%M:%S ET')}). "
                    "Gap-fill required for this period."
                )
                # continue outer loop — watchdog stays active
            else:
                terminate_et = pd.Timestamp.now(tz="America/New_York")
                print(
                    f"[ib-watchdog] No recovery after {self._watchdog_termination_grace_s}s -- "
                    "treating connection as zombie, stopping event loop",
                    flush=True,
                )
                self._write_session_comment(
                    f"IB terminated at {terminate_et.strftime('%H:%M:%S ET')} "
                    f"after {self._watchdog_termination_grace_s}s grace period. "
                    f"Gap-fill required for "
                    f"{last_data_et.strftime('%H:%M:%S')} - {terminate_et.strftime('%H:%M:%S ET')}. "
                    "Process exiting with code 2."
                )
                self._disconnected_by_gateway = True
                if self._event_loop is not None:
                    self._event_loop.call_soon_threadsafe(self._event_loop.stop)
                break

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract=None, exception=None) -> None:
        """Callback wired to ib.errorEvent — logs IB connectivity events to comments.md.

        Only writes on the first 1100 per loss cycle (deduplicates repeated 1100s) and on 1102.
        All other error codes are ignored here; ib_insync prints them to stdout already.
        """
        if errorCode == 1100 and not self._ib_connectivity_lost:
            self._ib_connectivity_lost = True
            now_et = pd.Timestamp.now(tz="America/New_York")
            self._write_session_comment(
                f"IB connectivity lost (error 1100) at {now_et.strftime('%H:%M:%S ET')} — "
                f"watchdog monitoring data flow; {self._watchdog_timeout_s}s timeout."
            )
        elif errorCode == 1102 and self._ib_connectivity_lost:
            self._ib_connectivity_lost = False
            now_et = pd.Timestamp.now(tz="America/New_York")
            self._write_session_comment(
                f"IB connectivity restored (error 1102) at {now_et.strftime('%H:%M:%S ET')} — "
                f"data flow determines watchdog recovery (not this API callback)."
            )

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
        # Cancel market data subscriptions and stop the event loop.
        # stop() is called from the orchestrator thread, so we use call_soon_threadsafe
        # to run the cancellations safely inside the IB event loop thread.
        try:
            if self._event_loop and self._event_loop.is_running():
                def _cancel_and_stop():
                    for contract in (self._mnq_contract, self._mes_contract):
                        if contract and self._ib and self._ib.isConnected():
                            try:
                                self._ib.cancelMktData(contract)
                            except Exception:
                                pass
                    self._event_loop.stop()
                self._event_loop.call_soon_threadsafe(_cancel_and_stop)
        except Exception:
            pass
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass
        # Flush 1s bars from the last partial minute to the session file; event loop is
        # stopped and disconnected by this point so pending lists are stable.
        # First, move any in-progress partial-second bar into the pending list so it
        # is captured even if the event loop was stopped mid-second.
        for instrument, tick_bar_attr, pending_attr in (
            ("MNQ", "_mnq_tick_bar", "_mnq_1s_pending"),
            ("MES", "_mes_tick_bar", "_mes_1s_pending"),
        ):
            try:
                tick_bar = getattr(self, tick_bar_attr)
                if tick_bar is not None:
                    getattr(self, pending_attr).append(tick_bar)
                    setattr(self, tick_bar_attr, None)
            except Exception:
                pass
        for instrument in ("MNQ", "MES"):
            try:
                self._flush_1s_pending_to_session_file(instrument)
            except Exception:
                pass
        # Drain pending parquet writes before exit so no data is lost on shutdown.
        try:
            self._parquet_executor.shutdown(wait=True)
        except Exception:
            pass


def _count_expected_1m_bars(start: "pd.Timestamp", end: "pd.Timestamp") -> int:
    """Count expected 1m bars in [start, end) for CME NQ/MNQ, excluding maintenance and weekends."""
    if end <= start:
        return 0
    idx = pd.date_range(
        start.floor("1min"), end.floor("1min"),
        freq="1min", tz="America/New_York", inclusive="left",
    )
    if idx.empty:
        return 0
    wd = idx.weekday
    h  = idx.hour
    active = ~(
        (h == 17) |
        ((wd == 5) & (h >= 17)) |
        ((wd == 6) & (h < 18))
    )
    return int(active.sum())


def gap_fill_1m_ib(bar_data_dir: Path) -> None:
    """1m bar gap-fill from IB.

    Reads IB_HOST, IB_PORT, MNQ_CONID, MES_CONID from environment.
    Uses client_id=17 (distinct from all other IB clients in the system).
    Skips gracefully if required env vars are absent.

    Retries instruments that return 0 bars (e.g. IB pacing violation) until all
    succeed or a 30-min wall-clock deadline is reached, at which point it raises
    RuntimeError. Instruments that return partial bars are not retried — the WARN
    line flags incomplete coverage.

    Called from IbRealtimeSource.start() (before real-time subscriptions open) and
    from orchestrator.main in non-LIVE_TRADING mode.
    """
    import os, time as _time
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    mnq_conid = os.environ.get("MNQ_CONID")
    mes_conid = os.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print("[gap_fill_1m_ib] MNQ_CONID/MES_CONID not set — skipping", flush=True)
        return

    MAX_LOOKBACK_DAYS = 30

    now = pd.Timestamp.now(tz="America/New_York")

    def _safe_read(path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        try:
            return pd.read_parquet(path)
        except Exception:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def _start_ts_for(df: pd.DataFrame) -> pd.Timestamp:
        if df.empty:
            return now - pd.Timedelta(days=MAX_LOOKBACK_DAYS)
        lookback_start = now - pd.Timedelta(days=MAX_LOOKBACK_DAYS)
        recent = df[df.index >= lookback_start]
        if recent.empty:
            return lookback_start
        idx = recent.index
        for i in range(1, len(idx)):
            gap_min = (idx[i] - idx[i - 1]).total_seconds() / 60
            if gap_min > 90 and not _gap_is_expected(idx[i - 1], gap_min):
                return idx[i - 1]
        # Return the actual last bar — no midnight clip. The fill range may overlap
        # already-covered bars, but deduplication handles that safely.
        return df.index[-1]

    instruments = [
        ("MNQ", mnq_conid, "MNQ_1m.parquet"),
        ("MES", mes_conid, "MES_1m.parquet"),
    ]

    # _fetch_1m_chunked: fetch 1m bars from a specific historical window.
    # IBGatewaySource.fetch() for 1m CME futures uses endDateTime="" (most recent N days)
    # because IB rejects explicit endDateTime with days-based durationStr for this bar size.
    # However, seconds-based durationStr with explicit endDateTime IS accepted, so we use
    # that here to target specific gap windows (e.g. an intra-day RTH gap).
    _1M_CHUNK_S = 7200  # 2-hour chunks for 1m bars

    def _fetch_1m_chunked(ib, contract, gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> pd.DataFrame:
        from ib_insync import util as _util
        all_bars: list = []
        chunk_end = gap_end
        while chunk_end > gap_start:
            chunk_start = max(gap_start, chunk_end - pd.Timedelta(seconds=_1M_CHUNK_S))
            chunk_s = max(60, int((chunk_end - chunk_start).total_seconds()))
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                durationStr=f"{chunk_s} S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
            )
            if bars:
                all_bars.extend(bars)
            chunk_end = chunk_start
        if not all_bars:
            return pd.DataFrame()
        df_out = _util.df(all_bars).rename(columns={
            "date": "datetime", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }).set_index("datetime")
        if df_out.index.tzinfo is None:
            df_out.index = df_out.index.tz_localize("America/New_York")
        else:
            df_out.index = df_out.index.tz_convert("America/New_York")
        df_out = df_out[["Open", "High", "Low", "Close", "Volume"]].sort_index()
        return df_out[~df_out.index.duplicated(keep="last")]

    from ib_insync import IB as _IB, Contract as _IBContract, util as _util

    bar_data_dir.mkdir(parents=True, exist_ok=True)
    pending = {"MNQ", "MES"}
    deadline = _time.monotonic() + 30 * 60

    ib = _IB()
    try:
        ib.connect(host, port, clientId=17)
    except Exception as exc:
        print(f"[gap_fill_1m_ib] IB unavailable ({exc}) — skipping", flush=True)
        return

    try:
        while pending:
            for instrument, conid, fname in instruments:
                if instrument not in pending:
                    continue
                path = bar_data_dir / fname
                df = _safe_read(path)
                start_ts = _start_ts_for(df)
                if (now - start_ts).total_seconds() <= 120:
                    pending.discard(instrument)
                    continue
                gap_end_ts = now
                print(
                    f"[gap_fill_1m_ib] {instrument}: gap-filling "
                    f"{start_ts.strftime('%m-%d %H:%M')} -> {gap_end_ts.strftime('%m-%d %H:%M')} ...",
                    flush=True,
                )
                contract = _IBContract(conId=int(conid), exchange="CME")
                new_df = _fetch_1m_chunked(ib, contract, start_ts, gap_end_ts)
                actual = len(new_df) if not new_df.empty else 0
                expected = _count_expected_1m_bars(start_ts, now)
                if actual == 0:
                    print(f"[gap_fill_1m_ib] {instrument}: 0 new bars", flush=True)
                else:
                    combined = pd.concat([df, new_df]).sort_index()
                    combined = combined[~combined.index.duplicated(keep="last")]
                    tmp = path.with_name(f"{path.stem}.{uuid4().hex}.parquet.tmp")
                    combined.to_parquet(tmp, use_dictionary=False)
                    os.replace(tmp, path)
                    print(f"[gap_fill_1m_ib] {instrument}: +{len(new_df)} bars", flush=True)
                    # Re-scan for remaining unexpected gaps — a partial fill (e.g. IB only
                    # serves part of the window) should trigger a retry, not a silent discard.
                    next_start = _start_ts_for(combined)
                    if (now - next_start).total_seconds() <= 120:
                        pending.discard(instrument)
                    else:
                        print(
                            f"[gap_fill_1m_ib] {instrument}: gap remains at "
                            f"{next_start.strftime('%m-%d %H:%M')} - will retry",
                            flush=True,
                        )
                if expected > 5 and actual < int(0.8 * expected):
                    print(
                        f"[gap_fill_1m_ib] WARN: {instrument} incomplete fill — "
                        f"{actual}/{expected} bars ({100 * actual // expected}% coverage)",
                        flush=True,
                    )

            if not pending:
                break
            if _time.monotonic() >= deadline:
                print(
                    f"[gap_fill_1m_ib] WARN: 30-min cap reached, still incomplete: {pending} "
                    f"— proceeding with partial data",
                    flush=True,
                )
                return
            print(f"[gap_fill_1m_ib] incomplete for {pending} — retrying in 20s", flush=True)
            _time.sleep(20)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    check_parquet_gaps(bar_data_dir)


def _gap_is_expected(start: "pd.Timestamp", gap_min: float) -> bool:
    """Return True if a gap of gap_min minutes starting at start is a known no-trading window."""
    et = start.tz_convert("America/New_York")
    wd = et.weekday()   # 0=Mon … 6=Sun
    frac_h = et.hour + et.minute / 60.0

    # Weekend: Friday close → Sunday re-open (no data expected at all).
    # Cap at 75 h so a gap that extends past Monday 6 PM (> 3-day holiday weekend)
    # is still flagged — the Monday overnight session would be missing.
    if ((wd == 4 and frac_h >= 16.75) or wd == 5 or (wd == 6 and frac_h < 18.25)) and gap_min <= 75 * 60:
        return True
    # Daily CME maintenance 17:00-18:00 ET plus session-close buffer (16:45-18:15).
    # Cap at 75 min so a gap that starts in the maintenance window but runs well past
    # 18:00 (= missing overnight session) is still flagged as unexpected.
    if 16.75 <= frac_h < 18.25 and gap_min <= 75:
        return True
    # CME US bank holiday early close: market closes at noon ET, reopens at 18:00.
    # Pattern: gap starts between 12:00–13:00, lasts ≤360 min (to 18:00).
    if 12.0 <= frac_h < 13.0 and gap_min <= 360:
        return True
    return False


def check_parquet_gaps(bar_data_dir: "Path") -> None:
    """Scan the 4 main parquets for unexpected gaps in the last 48 h and print findings."""
    now = pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(hours=48)

    names_and_files = [
        ("MNQ_1m", bar_data_dir / "MNQ_1m.parquet"),
        ("MES_1m", bar_data_dir / "MES_1m.parquet"),
        ("MNQ_1s", bar_data_dir / "MNQ_1s.parquet"),
        ("MES_1s", bar_data_dir / "MES_1s.parquet"),
    ]

    all_ok = True
    for name, path in names_and_files:
        if not path.exists():
            print(f"[gap_check] WARN: {name}: parquet not found", flush=True)
            all_ok = False
            continue

        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            print(f"[gap_check] WARN: {name}: could not read parquet — {exc}", flush=True)
            all_ok = False
            continue

        if df.empty:
            print(f"[gap_check] WARN: {name}: parquet is empty", flush=True)
            all_ok = False
            continue

        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        recent_mask = idx >= cutoff
        if not recent_mask.any():
            print(f"[gap_check] WARN: {name}: no bars in last 48 h", flush=True)
            all_ok = False
            continue

        recent_idx = idx[recent_mask]
        gaps: list[tuple] = []
        for i in range(1, len(recent_idx)):
            gap_min = (recent_idx[i] - recent_idx[i - 1]).total_seconds() / 60.0
            if gap_min > 60 and not _gap_is_expected(recent_idx[i - 1], gap_min):
                gaps.append((recent_idx[i - 1], recent_idx[i], gap_min))

        for start, end, gap_min in gaps[:5]:
            start_et = start.tz_convert("America/New_York")
            end_et   = end.tz_convert("America/New_York")
            print(
                f"[gap_check] WARN: {name}: {gap_min:.0f}min gap "
                f"{start_et:%Y-%m-%d %H:%M} -> {end_et:%H:%M ET}",
                flush=True,
            )
            all_ok = False
        if len(gaps) > 5:
            print(f"[gap_check] WARN: {name}: ... and {len(gaps) - 5} more gap(s)", flush=True)

    if all_ok:
        print("[gap_check] OK: all parquets complete — no unexpected gaps found", flush=True)
