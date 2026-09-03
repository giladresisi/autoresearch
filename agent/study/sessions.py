"""Session bar access for the cycle-4 target study.

Loads each ticker's 1m parquet ONCE and serves per-session windows from it. The
alternative -- re-reading a 900k-row parquet per session -- turns an 87-session sweep
from seconds into minutes for no benefit.

Three things here are load-bearing and easy to get wrong:

  **The contract subdirectory.** Since the June->September rollover the parquets are
  split per contract under `<main>/2026-09/`. The top-level `MNQ_1m.parquet` still
  exists and still reads cleanly -- it simply stops at 2026-06-12, so reading it yields
  an EMPTY window for most of the study range without raising anything. `source_path`
  is exposed purely so a test can pin which file is actually opened.

  **The session list is the intersection across tickers.** A date present for MNQ but
  short on MES is dropped rather than half-used: spec 3.1's whole premise is that the
  draw may belong to the other instrument, and a session missing that instrument would
  silently look like one with no cross-instrument explanation.

  **The session list is bounded to the study range.** The per-contract parquet carries
  history back to 2024-01-01 -- 687 sessions, not 87. Spec 9.1 fixes the study window
  at 2026-05-01 -> 2026-08-31, so the bound belongs here rather than in each caller,
  where one caller forgetting it would silently widen the corpus.

The `.date` of a 900k-row DatetimeIndex materialises 900k Python date objects and costs
~0.3s, so it is computed once per ticker at load and reused by every window call.
"""
from __future__ import annotations

import datetime
import os

import pandas as pd

import paths
from agent.facts.bars import resample

CONTRACT_SUBDIR = "2026-09"
TZ = "America/New_York"
RTH_START = (9, 30)
RTH_END = (13, 0)

# Spec 9.1's study range. Inclusive on both ends.
STUDY_START = datetime.date(2026, 5, 1)
STUDY_END = datetime.date(2026, 8, 31)


class SessionBars:
    def __init__(self, tickers=("MNQ", "MES")) -> None:
        self.tickers = tuple(tickers)
        self._frames: "dict[str, pd.DataFrame]" = {}
        self._dates: "dict[str, object]" = {}
        for tk in self.tickers:
            df = pd.read_parquet(self.source_path(tk)).sort_index()
            # The live parquets are already tz-aware ET, so the localize branch is
            # currently dead -- but a naive index spanning a fall-back transition would
            # raise AmbiguousTimeError without these, and the study range will grow.
            df.index = (df.index.tz_convert(TZ) if df.index.tz is not None
                        else df.index.tz_localize(TZ, ambiguous="infer",
                                                  nonexistent="shift_forward"))
            self._frames[tk] = df
            self._dates[tk] = df.index.date

    def source_path(self, ticker: str) -> str:
        return os.path.join(paths.general_main_dir(), CONTRACT_SUBDIR,
                            f"{ticker}_1m.parquet")

    def dates(self) -> "list[datetime.date]":
        """Weekday sessions inside the study range holding at least one 09:30-13:00 bar
        on EVERY ticker."""
        per_ticker = []
        for tk in self.tickers:
            df = self._frames[tk]
            t = df.index.time
            inside = df[(t >= datetime.time(*RTH_START)) & (t <= datetime.time(*RTH_END))]
            per_ticker.append({d for d in inside.index.date
                               if d.weekday() < 5 and STUDY_START <= d <= STUDY_END})
        common = set.intersection(*per_ticker) if per_ticker else set()
        return sorted(common)

    def window_1m(self, ticker: str, date) -> pd.DataFrame:
        df = self._frames.get(ticker)
        if df is None:
            return pd.DataFrame()
        day = df[self._dates[ticker] == date]
        if not len(day):
            return day
        t = day.index.time
        return day[(t >= datetime.time(*RTH_START)) & (t <= datetime.time(*RTH_END))]

    def window_5m(self, ticker: str, date) -> pd.DataFrame:
        w = self.window_1m(ticker, date)
        if not len(w):
            return w
        return resample(w, "5min")
