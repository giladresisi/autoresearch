"""1m-backed facts source for the cycle-4 target study.

**Why 1m and not the 1s parquets production uses.** `<main>/2026-09/{MNQ,MES}_1s.parquet`
begin at 2026-05-01, so `bundle_for_boundary`'s 17-day lookback is empty at the start of the
study range: at the 2026-05-01 09:35 boundary the 1s path yields **4 levels**, the 1m path
**26**, because the 1m parquets carry history back to 2024-01-01. Building the study on 1s
would silently truncate the candidate universe across the first three weeks of the corpus —
silently, because a short lookback produces a small universe rather than an error.

Where both have history the two agree exactly, and that is verified rather than assumed
(`test_1m_and_1s_agree_where_both_have_history`): at 2026-08-13 and 2026-08-11 the level
sets, level prices and sweep timestamps are identical. That is expected rather than lucky —
sweeps are wick tests (`low <= level`) and a 1m bar's high/low is the max/min of its
constituent 1s bars, so the crossing minute survives the aggregation and only sub-minute
ordering is lost. `avg_range_1h` differs by ~0.1 points, from 1h aggregation of coarser bars.

1m is also ~8x faster per boundary (4.2s vs 38.7s), which is what makes an 87-session sweep
a coffee break rather than an afternoon.

**The slicing rule is not reimplemented here.** `bundle_for_boundary` is the ONE
boundary -> slice rule, shared verbatim by the online assembler and the offline bench; a
second copy of it is a failure this project has already paid for twice.
"""
from __future__ import annotations

import os

import pandas as pd

import paths
from agent.bench.facts import bundle_for_boundary
from derive_facts import TZ, load

CONTRACT_SUBDIR = "2026-09"


class StudyFacts:
    """Loads both tickers' 1m parquets once, then serves a `FactsBundle` at any boundary."""

    def __init__(self, main_dir: "str | None" = None, tickers=("MNQ", "MES")) -> None:
        self.main_dir = main_dir or os.path.join(paths.general_main_dir(), CONTRACT_SUBDIR)
        self.tickers = tuple(tickers)
        self._norm: dict = {}
        self._raw: dict = {}
        self._cache: dict = {}
        for tk in self.tickers:
            # Two views of the same file, exactly as ParquetFactsSource keeps them: the
            # normalised (maintenance-dropped, lowercase) frame compute_facts derives from,
            # and the raw one the ATH is taken off — dropping maintenance bars first can
            # move the maximum, so they are not interchangeable.
            self._norm[tk] = load(self.source_path(tk))
            self._raw[tk] = self._load_raw(self.source_path(tk))

    def source_path(self, ticker: str) -> str:
        return os.path.join(self.main_dir, f"{ticker}_1m.parquet")

    @staticmethod
    def _load_raw(path: str) -> pd.DataFrame:
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index)
        df.index = (df.index.tz_localize(TZ) if df.index.tz is None
                    else df.index.tz_convert(TZ))
        return df.sort_index()

    def bars_1m(self, ticker: str) -> pd.DataFrame:
        """The normalised 1m frame — lowercase o/h/l/c, tz-aware ET, maintenance dropped."""
        return self._norm[ticker]

    def bundle_at(self, boundary: pd.Timestamp):
        """The `FactsBundle` as of strictly BEFORE `boundary`.

        Exclusive by way of `bundle_for_boundary`, and that matters more than it looks: a
        candidate swept BY the move under study must still read unswept at move-start, or
        labelling consumes its own answer.

        Cached per boundary — a session asks for the same one several times and each build
        costs ~4 seconds.
        """
        key = boundary.value
        if key not in self._cache:
            bundle, _ = bundle_for_boundary(self._raw, self._norm, boundary,
                                            tickers=self.tickers)
            self._cache[key] = bundle
        return self._cache[key]
