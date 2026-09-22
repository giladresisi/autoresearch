"""Disk side of the unnested HTF extremes (plan 40): which 1m file, whether it is fresh
enough, and the run-folder artifact.

Separate from `agent/facts/htf_extremes.py` because that module is pure and the
mechanism gate forbids it `paths`. Both paths read the SAME file family -- the 1m parquet
-- never the 1s->1m resample: the two disagree on ~5% of minutes since May and on some
weekly extremes (plan 40 insight 6), so "same source" has to mean one file family.

  live    `paths.general_live_dir()/{tk}_1m.parquet`
  replay  `main_dir_for_date(trade_date)/{tk}_1m.parquet` -- the per-contract era the
          replay's own bars come from, so the prices are on the same back-adjusted scale.

`as_of` is always the session open (`htf_extremes.session_as_of`), so a live restart
recomputes the identical list, and nothing at or after the open is read.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os

import pandas as pd

import paths
from agent.bench.facts import main_dir_for_date
from agent.facts.htf_extremes import (EXTREMES_START, TZ, HtfExtreme,
                                      compute_unnested_extremes, running_period_seed,
                                      seed_from_json, seed_to_json, session_as_of)

ARTIFACT = "htf_extremes.json"
TICKERS = ("MNQ", "MES")
SOURCES = ("live", "replay")

#: How far before the prior session's 17:00 close the file's last bar may end. Early
#: closes (13:00-13:15 ET) sit inside it; a whole missing session does not.
STALE_TOLERANCE = pd.Timedelta(hours=6)


class HtfSourceStale(RuntimeError):
    """The 1m file ends too early to compute the list as of the session open."""


def source_path(trade_date: str, ticker: str, source: str) -> str:
    if source == "live":
        return str(paths.general_live_dir() / f"{ticker}_1m.parquet")
    if source == "replay":
        return os.path.join(main_dir_for_date(str(trade_date)), f"{ticker}_1m.parquet")
    raise ValueError(f"source must be one of {SOURCES}, got {source!r}")


#: CME equity-index FULL closures (no session at all), as (month, day) every year plus
#: dated Good Fridays. Early closes are not listed: they sit inside STALE_TOLERANCE.
FULL_CLOSURES_MD = frozenset({(1, 1), (12, 25)})
GOOD_FRIDAYS = frozenset({datetime.date(2026, 4, 3), datetime.date(2027, 3, 26),
                          datetime.date(2028, 4, 14)})


def _is_closed(d: datetime.date) -> bool:
    return (d.weekday() >= 5 or (d.month, d.day) in FULL_CLOSURES_MD
            or d in GOOD_FRIDAYS)


def prior_session_close(as_of: pd.Timestamp) -> pd.Timestamp:
    """17:00 ET of the last TRADING day before the trade date whose session opens at
    `as_of` -- weekends and the full CME closures above are skipped."""
    d = (as_of + pd.Timedelta(hours=7)).date() - datetime.timedelta(days=1)
    while _is_closed(d):
        d -= datetime.timedelta(days=1)
    return pd.Timestamp(datetime.datetime.combine(d, datetime.time(17, 0)), tz=TZ)


def _read(path: str, as_of: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"{path}: index is not a DatetimeIndex")
    idx = df.index.tz_localize(TZ) if df.index.tz is None else df.index.tz_convert(TZ)
    df = df.set_axis(idx)
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()
    return df[df.index < as_of]


def load_session_extremes(trade_date, *, source: str, tolerance=STALE_TOLERANCE) -> dict:
    """{"MNQ": [HtfExtreme], "MES": [...], "seed": {tk: seed}, "meta": {...}}.

    Raises `HtfSourceStale` when a file's last bar before the open is earlier than the
    prior session's close minus `tolerance`, and whatever `read_parquet` raises for a
    missing or unreadable file. The CALLER decides whether that is fatal (it never is in
    live: the Executor then selects exactly as it did before plan 40)."""
    trade_date = str(trade_date)[:10]
    as_of = session_as_of(trade_date)
    need = prior_session_close(as_of) - tolerance
    out: dict = {"seed": {}, "meta": {"trade_date": trade_date, "as_of": as_of.isoformat(),
                                      "kind": source, "tickers": {}}}
    for tk in TICKERS:
        path = source_path(trade_date, tk, source)
        df = _read(path, as_of)
        last = df.index[-1] if len(df) else None
        if last is None or last < need:
            raise HtfSourceStale(
                f"{tk} {path}: last bar before the open {as_of} is {last}, "
                f"need >= {need} (prior close minus {tolerance})")
        ext = compute_unnested_extremes(df, as_of, ticker=tk)
        seed = running_period_seed(df, as_of, ticker=tk)
        out[tk] = ext
        out["seed"][tk] = seed
        out["meta"]["tickers"][tk] = {
            "path": path, "start": EXTREMES_START[tk].isoformat(),
            "n_rows": int(len(df)), "last_ts": last.isoformat(),
            "n_extremes": len(ext)}
    out["meta"]["sha"] = fingerprint(out)
    return out


def _content(ext: dict) -> dict:
    return {tk: {"extremes": [e.to_json() for e in ext.get(tk) or ()],
                 "seed": seed_to_json((ext.get("seed") or {}).get(tk))}
            for tk in TICKERS}


def fingerprint(ext: dict) -> str:
    """sha256 over the lists and seeds only -- what the Executor consumes. Equal lists
    from two files (live vs main era) have equal fingerprints."""
    blob = json.dumps(_content(ext), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def to_json(ext: dict) -> dict:
    return {"meta": ext.get("meta") or {}, **_content(ext)}


def from_json(d: dict) -> dict:
    out = {"meta": d.get("meta") or {}, "seed": {}}
    for tk in TICKERS:
        part = d.get(tk) or {}
        out[tk] = [HtfExtreme.from_json(r) for r in part.get("extremes") or ()]
        out["seed"][tk] = seed_from_json(part.get("seed"))
    return out


def write_artifact(state_dir, ext: dict) -> str:
    """Deterministic content (no wall clock): the same list writes the same bytes."""
    os.makedirs(str(state_dir), exist_ok=True)
    path = os.path.join(str(state_dir), ARTIFACT)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_json(ext), fh, sort_keys=True, indent=2)
    return path


def write_error_artifact(state_dir, trade_date, source: str, reason: str) -> str:
    """The run folder records WHY there is no list, rather than silently lacking one."""
    os.makedirs(str(state_dir), exist_ok=True)
    path = os.path.join(str(state_dir), ARTIFACT)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"meta": {"trade_date": str(trade_date)[:10], "kind": source,
                            "error": str(reason)}}, fh, sort_keys=True, indent=2)
    return path


def read_artifact(state_dir) -> "dict | None":
    path = os.path.join(str(state_dir), ARTIFACT)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if (d.get("meta") or {}).get("error"):
        return None
    return from_json(d)


def executor_context(ext: "dict | None", ticker: str = "MNQ") -> "dict | None":
    """What `Executor(htf_extremes=)` takes: {"as_of", "extremes", "seed"} for one ticker."""
    if not ext or ticker not in ext:
        return None
    as_of = pd.Timestamp((ext.get("meta") or {}).get("as_of")).tz_convert(TZ)
    return {"as_of": as_of, "extremes": list(ext.get(ticker) or ()),
            "seed": (ext.get("seed") or {}).get(ticker)}
