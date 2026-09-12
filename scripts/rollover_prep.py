# scripts/rollover_prep.py
# Quarterly MNQ/MES contract rollover-prep: back-adjust the live parquets onto the new
# front month's price scale, freeze the old contract era under main/<YYYY-MM>/, and point
# the ledger + .env at the new contract.
#
# Why this is a module and not just prose in the parquet-check skill: the procedure has a
# load-bearing ORDER, and every ordering mistake fails SILENTLY (no exception, no bad exit
# code — just wrong prices in backtests weeks later). Encoding the order here means no
# caller can get it wrong, whichever entry point (skill, CLI, agent) starts the roll.
#
#   gap-fill (OLD conids)  ->  promote  ->  rollover-prep
#   ^^^^^^^^^^^^^^^^^^^^^      ^^^^^^^      ^^^^^^^^^^^^^
#   fills live through the     lands that   this module
#   old contract's LAST        final data
#   session                    in the OLD
#                              subfolder,
#                              which then
#                              freezes
#
# Run it AFTER promote, never before: _current_main_subdir() resolves to rows[0], so the
# moment this module writes the new ledger row, promote starts targeting the NEW subfolder.
# Promote after the roll and the old contract's final session lands in the new era's folder
# as raw, un-back-adjusted bars spliced onto a back-adjusted scale.

import json
import os
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

# When run as a file, Python sets sys.path[0] to scripts/ not the project root,
# so `data.*` imports fail. Insert the project root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from dotenv import load_dotenv

import paths

PARQUET_NAMES = ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]
OHLC_COLS = ["Open", "High", "Low", "Close"]
TICK = 0.25                     # MNQ and MES both tick 0.25; gaps are whole ticks
STATE_NAME = ".rollover_prep_state.json"
IB_CLIENT_ID = 19               # distinct from check_session_parquets (17) and gap_fill (10)
MAX_1M_LOOKBACK_DAYS = 14       # IB's empirical cap for 1m bars with endDateTime='' (data/sources.py)
ANCHOR_TOLERANCE = pd.Timedelta("1D")   # how far from the seam a common bar may sit

# CME month codes for the quarterly cycle (Mar/Jun/Sep/Dec).
_MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no IB, no filesystem)
# ---------------------------------------------------------------------------

def next_prep_date(expiry: str) -> str:
    """Saturday immediately before `expiry` (YYYY-MM-DD) — the next roll's prep date.

    Reproduces the historical rows: 2026-06-18 (Thu) -> 2026-06-13, 2026-09-18 (Fri) ->
    2026-09-12. An expiry that already falls on a Saturday steps back a full week rather
    than returning itself.
    """
    d = date.fromisoformat(expiry)
    back = (d.weekday() - 5) % 7 or 7   # Mon=0 .. Sat=5; 0 -> 7 so we never return `d`
    return (d - timedelta(days=back)).isoformat()


def contract_code(symbol: str, expiry: str) -> str:
    """CME contract code for a quarterly expiry, e.g. ("MNQ", "2026-12-18") -> "MNQZ6"."""
    d = date.fromisoformat(expiry)
    return f"{symbol}{_MONTH_CODE.get(d.month, '?')}{d.year % 10}"


def subfolder_for(expiry: str) -> str:
    """Main subfolder name for a contract era, e.g. "2026-12-18" -> "2026-12"."""
    return expiry[:7]


def today_str() -> str:
    """The date ROLLOVER_PREP_DATE is compared against — the CME session date, not a wall clock.

    `date.today()` reads the machine clock, which is Asia/Bangkok here: its date rolls over at
    11:00 ET, i.e. in the MIDDLE of a trading session, so a prep date would come due while the
    old contract's last session was still open. A plain ET date has the opposite problem — it
    still reads Friday late on Friday night, after that session has closed and the roll is
    genuinely ready.

    `cme_session_date` is the project's existing convention and lands exactly where the roll
    wants it: it advances to the prep date at 18:00 ET on the old contract's last session day —
    after its 17:00 ET close. That is the semantic of "the Saturday before expiry" throughout
    the ledger (the June roll's prep_date 2026-06-13 pairs with a 2026-06-12 16:59 ET boundary).
    """
    from session_times import session_date_str
    return session_date_str()


def rollover_status(rows: list, env_prep_date: str | None, today: str) -> dict:
    """Classify where the quarterly roll stands, from the ledger + .env + today's date.

    `rows` is rollover_ledger.json (newest-first). `env_prep_date` is ROLLOVER_PREP_DATE.
    Returns {due, already_rolled, prep_date, reason}:
      already_rolled — a ledger row already covers the PENDING prep date. Normally false:
                       a successful roll writes its row and advances .env in the same pass,
                       so .env's date always runs one quarter ahead of the newest row. True
                       means the roll landed but .env was not advanced — re-running would
                       double-shift, so callers must refuse.
      due            — today >= ROLLOVER_PREP_DATE and no row covers it yet.
    ISO dates compare correctly as strings.
    """
    if not env_prep_date:
        return {"due": False, "already_rolled": False, "prep_date": None,
                "reason": "ROLLOVER_PREP_DATE not set in .env"}

    newest_prep = str(rows[0].get("prep_date", "")) if rows else ""
    already_rolled = bool(rows) and newest_prep >= env_prep_date
    if already_rolled:
        return {"due": False, "already_rolled": True, "prep_date": env_prep_date,
                "reason": f"ledger row prep_date={newest_prep} already covers pending "
                          f"ROLLOVER_PREP_DATE={env_prep_date} — the roll ran but .env was "
                          "not advanced; fix .env by hand rather than re-running the roll"}
    if today >= env_prep_date:
        return {"due": True, "already_rolled": False, "prep_date": env_prep_date,
                "reason": f"ROLLOVER_PREP_DATE={env_prep_date} has arrived "
                          f"(today={today}) and no ledger row covers it"}
    return {"due": False, "already_rolled": False, "prep_date": env_prep_date,
            "reason": f"not yet due — ROLLOVER_PREP_DATE={env_prep_date} (today={today})"}


def build_ledger_row(prep_date: str, expiry: str, gaps: dict, old_conids: dict,
                     new_conids: dict) -> dict:
    """Assemble the newest-first ledger row for a completed roll."""
    row = {
        "prep_date": prep_date,
        "subfolder": subfolder_for(expiry),
        "expiry": expiry,
    }
    for sym in ("mnq", "mes"):
        row[sym] = {
            "old_conid": int(old_conids[sym]),
            "new_conid": int(new_conids[sym]),
            "gap": float(gaps[sym]),
        }
    row["note"] = (
        f"{contract_code('MNQ', expiry)}/{contract_code('MES', expiry)}. Back-adjusted from "
        f"{subfolder_for(row['prep_date'])} by the per-symbol gap measured at the old contract's "
        f"last session close. Backtests dated >= prep_date use this subfolder; it is the "
        f"current parquet-check write target."
    )
    return row


def prepend_row(rows: list, row: dict) -> list:
    """Insert `row` as the NEWEST ledger entry.

    It goes at index 0, NOT appended: both scripts/check_session_parquets._current_main_subdir()
    (reads rows[0]) and backtest_smt._main_dir_for_date (first row with prep_date <= date)
    treat the array as newest-first. Appending is a silent no-op — every reader keeps
    resolving to the previous era with no error raised.
    """
    return [row] + list(rows)


def back_adjust(df: pd.DataFrame, gap: float) -> pd.DataFrame:
    """Shift OHLC by `gap`, leaving Volume untouched.

    A constant offset is P&L- and decision-neutral: every level, range and displacement the
    strategy measures is a difference of prices, so it is invariant under the shift. Volume
    must NOT move — it is not on the price scale.
    """
    out = df.copy()
    for col in OHLC_COLS:
        if col in out.columns:
            out[col] = out[col] + gap
    return out


def round_to_tick(value: float) -> float:
    """Round a measured gap to a whole tick (both contracts tick 0.25)."""
    return round(value / TICK) * TICK


# ---------------------------------------------------------------------------
# Ledger / state / .env I/O
# ---------------------------------------------------------------------------

def ledger_path() -> Path:
    return paths.general_main_dir() / "rollover_ledger.json"


def load_ledger() -> list:
    p = ledger_path()
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def write_ledger(rows: list) -> None:
    """Write the ledger atomically, backing up the previous copy first."""
    p = ledger_path()
    if p.exists():
        shutil.copy2(p, p.with_suffix(".json.bak"))
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _state_path() -> Path:
    return paths.general_main_dir() / STATE_NAME


def _load_state(prep_date: str) -> set:
    """Steps already completed for THIS prep date (empty for a fresh run).

    The back-adjust is destructive and in-place, so a crash midway must not re-shift the
    parquets on the next attempt. Each step records itself here as it lands; the file is
    removed once the roll completes.
    """
    p = _state_path()
    if not p.exists():
        return set()
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if str(st.get("prep_date")) != prep_date:
        return set()
    return set(st.get("done", []))


def _mark_step(prep_date: str, step: str, done: set) -> set:
    done = set(done) | {step}
    _state_path().write_text(
        json.dumps({"prep_date": prep_date, "done": sorted(done)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return done


def _clear_state() -> None:
    p = _state_path()
    if p.exists():
        p.unlink()


def env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _old_conids_from_ledger(rows: list) -> dict:
    """Conids of the contract being rolled AWAY from = the newest row's `new_conid` per symbol.

    Sourced from the ledger rather than `.env` because the operator edits `.env` to the new
    front month before the roll runs, which destroys the old numbers. The ledger is the durable
    record of which contract each era's data was fetched with.
    """
    if not rows:
        raise RuntimeError("rollover_ledger.json is empty — cannot determine the current conids.")
    out = {}
    for sym in ("mnq", "mes"):
        conid = (rows[0].get(sym) or {}).get("new_conid")
        if not conid:
            raise RuntimeError(
                f"Newest rollover_ledger.json row has no {sym}.new_conid — cannot determine the "
                "contract being rolled away from. Fill it in before rolling."
            )
        out[sym] = int(conid)
    return out


def read_env_value(key: str) -> str | None:
    """`key` from the environment, falling back to parsing `.env` WITHOUT mutating os.environ.

    The status checks must be side-effect free. `load_dotenv()` here injected the whole `.env`
    into the process as a side effect of merely *asking* whether a roll was due — silently
    re-populating conids a caller had deliberately unset, and leaking real config into anything
    that ran afterwards.
    """
    val = os.environ.get(key)
    if val is not None:
        return val
    p = env_path()
    if not p.exists():
        return None
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].split("#", 1)[0].strip()
    except OSError:
        return None
    return None


def env_conids() -> dict:
    """MNQ/MES conids currently set in the environment (0 when absent or unparseable)."""
    out = {}
    for sym, key in (("mnq", "MNQ_CONID"), ("mes", "MES_CONID")):
        try:
            out[sym] = int(os.environ.get(key, "0"))
        except ValueError:
            out[sym] = 0
    return out


def verify_env_conids(expected_new: dict, expiry: str) -> None:
    """Confirm `.env` already names the new front month; raise an actionable error if not.

    The operator owns the conid lines — the roll is gated on them having made that edit. But
    "I updated it" has to be CHECKED, not taken on trust: if the edit never landed (typo, wrong
    line, unsaved buffer) the roll would shift the parquets onto the new scale while every
    later fetch still returned old-contract prices — the same discontinuity this whole ordering
    exists to prevent, just mirrored. Reading `.env` here is confirmation, not inference: the
    roll never proceeds on the strength of the answer alone.
    """
    have = env_conids()
    wrong = {s: (have[s], expected_new[s]) for s in ("mnq", "mes") if have[s] != expected_new[s]}
    if not wrong:
        return
    lines = [
        "`.env` does not name the new front month yet — refusing to roll.",
        "Set these two lines in .env, then re-run:",
    ]
    for sym in ("mnq", "mes"):
        ticker = sym.upper()
        lines.append(
            f"  {ticker}_CONID={expected_new[sym]}   # {contract_code(ticker, expiry)} — "
            f"{date.fromisoformat(expiry).strftime('%B %Y')}, expires {expiry}"
        )
    for sym, (got, want) in wrong.items():
        lines.append(f"  ({sym.upper()}_CONID is currently {got or 'unset'}, expected {want})")
    lines.append("Leave ROLLOVER_PREP_DATE alone — the roll advances it once it completes.")
    raise RuntimeError("\n".join(lines))


def update_env_prep_date(next_prep: str) -> None:
    """Advance ROLLOVER_PREP_DATE only, preserving every other line.

    The conid lines are the operator's to edit and are already verified by the time this runs;
    the prep date is the COMPLETION marker and belongs to the code, so it can never claim a
    roll that did not finish. Written last, after the parquets and ledger are in place.
    """
    p = env_path()
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^ROLLOVER_PREP_DATE=.*$", f"ROLLOVER_PREP_DATE={next_prep}", text)
    shutil.copy2(p, p.parent / (p.name + ".preroll.bak"))
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# IB lookups
# ---------------------------------------------------------------------------

def resolve_next_quarterly(ib, symbol: str, current_expiry: str) -> tuple[int, str]:
    """Return (conid, expiry) of the first quarterly contract expiring after `current_expiry`.

    Deliberately NOT ContFuture: IB rolls its continuous contract at/near expiry, but the prep
    date is the Saturday BEFORE expiry — so on prep day ContFuture still resolves to the
    contract we are rolling away from. Enumerating contract details and stepping to the next
    expiry is correct regardless of IB's own roll timing.
    """
    from ib_insync import Future

    if not current_expiry:
        raise RuntimeError(
            f"Cannot resolve the next {symbol} contract: the current expiry is unknown "
            "(the newest rollover_ledger.json row has no `expiry`). Fill it in first."
        )
    details = ib.reqContractDetails(Future(symbol, exchange="CME", currency="USD"))
    cur = current_expiry.replace("-", "")
    candidates = []
    for d in details:
        last = str(d.contract.lastTradeDateOrContractMonth or "")
        if len(last) == 8 and last > cur:
            candidates.append((last, d.contract.conId))
    if not candidates:
        raise RuntimeError(
            f"No {symbol} contract found expiring after {current_expiry}. "
            "Check the IB connection and market-data subscriptions."
        )
    last, conid = sorted(candidates)[0]
    return int(conid), f"{last[:4]}-{last[4:6]}-{last[6:]}"


def seam_anchor(symbol: str) -> pd.Timestamp:
    """Timestamp the back-adjustment must be anchored at: the last bar of old-contract data.

    This is the SEAM — where shifted old-contract history meets future new-contract bars — so
    it is the only point at which measuring the gap makes the two sides join cleanly.
    """
    p = paths.general_live_dir() / f"{symbol.upper()}_1m.parquet"
    if not p.exists():
        raise RuntimeError(f"Cannot determine the seam: {p} is missing.")
    return pd.read_parquet(p).index[-1]


def _fetch_recent_1m(ib, conid: int, days: int = 4) -> pd.Series:
    """Most-recent 1m closes for a conid, as a tz-aware Series indexed by bar time.

    endDateTime MUST be '' — IB rejects an explicit endDateTime for CME equity-index futures
    1m bars (error 162 for a specific contract, 10339 for ContFuture). See data/sources.py.
    """
    from ib_insync import Contract as IBContract
    from ib_insync import util as ib_util

    bars = ib.reqHistoricalData(
        IBContract(conId=int(conid), exchange="CME"),
        endDateTime="",
        durationStr=f"{days} D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
    )
    if not bars:
        return pd.Series(dtype="float64")
    df = ib_util.df(bars)
    idx = pd.to_datetime(df["date"], utc=True).dt.tz_convert("America/New_York")
    return pd.Series(df["close"].to_numpy(), index=idx).sort_index()


def measure_gap(ib, old_conid: int, new_conid: int,
                anchor: pd.Timestamp) -> tuple[float, pd.Timestamp]:
    """(gap, boundary) where gap = new_front_close - old_conid_close at the data's SEAM.

    `anchor` is the last bar of old-contract data (see seam_anchor). Measuring at the latest
    bar the two contracts happen to share is only equivalent when the roll runs immediately
    after the old contract's final session — which is the intent, but not guaranteed. The old
    contract keeps trading until its own expiry (a week past the prep date), so both legs
    resume quoting at the next session open while the gate holds the parquets frozen at the
    seam. A roll that slips even one session would then anchor the shift at a bar that is not
    the seam, and the join between shifted history and future bars would be off by the carry
    drift between those two moments — small, silent, and permanent.

    So: take the latest common bar at or before the anchor, and refuse if that is more than a
    day away rather than quietly anchoring somewhere else.
    """
    now = pd.Timestamp.now(tz="America/New_York")
    days = int((now - anchor).total_seconds() // 86400) + 3
    days = max(2, min(days, MAX_1M_LOOKBACK_DAYS))

    old = _fetch_recent_1m(ib, old_conid, days)
    new = _fetch_recent_1m(ib, new_conid, days)
    common = old.index.intersection(new.index)
    if len(common) == 0:
        raise RuntimeError(
            f"No overlapping 1m bars between conids {old_conid} and {new_conid} — "
            "cannot measure the rollover gap."
        )

    at_or_before = common[common <= anchor]
    if len(at_or_before) == 0:
        raise RuntimeError(
            f"No common 1m bar at or before the seam {anchor} within the last {days} days "
            f"(conids {old_conid}/{new_conid}). The roll has been delayed past what IB will "
            "serve for 1m bars — resolve by hand."
        )
    boundary = at_or_before.max()
    if anchor - boundary > ANCHOR_TOLERANCE:
        raise RuntimeError(
            f"Nearest common 1m bar to the seam {anchor} is {boundary}, "
            f"{anchor - boundary} earlier — beyond the {ANCHOR_TOLERANCE} tolerance. "
            "Refusing to anchor the shift there."
        )
    return round_to_tick(float(new.loc[boundary]) - float(old.loc[boundary])), boundary


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

MAX_GAP_FRACTION = 0.03   # a quarterly carry gap runs ~1% of index level; 3% is a generous ceiling


def _sanity_check_gaps(gaps: dict, rows: list) -> None:
    """Reject an implausible measured gap before it silently shifts the whole history.

    The gap is applied to every bar ever recorded, so a bad measurement — IB handing back a
    stale or wrong-contract bar — corrupts the entire series with no visible seam. Matters most
    when the roll runs unattended: nobody is reading the number before it lands.

    A quarterly carry gap is ~1% of the index level (June→Sept was MNQ +293.25 on ~29k). Zero is
    rejected outright: it almost certainly means both legs resolved to the SAME contract.
    """
    live = paths.general_live_dir()
    for sym in ("mnq", "mes"):
        gap = gaps[sym]
        if gap == 0:
            raise RuntimeError(
                f"{sym.upper()} rollover gap measured as exactly 0 — the old and new legs almost "
                "certainly resolved to the same contract. Refusing to roll."
            )
        p = live / f"{sym.upper()}_1m.parquet"
        if not p.exists():
            continue
        level = float(pd.read_parquet(p)["Close"].iloc[-1])
        if abs(gap) > abs(level) * MAX_GAP_FRACTION:
            raise RuntimeError(
                f"{sym.upper()} rollover gap {gap:+.2f} is more than "
                f"{MAX_GAP_FRACTION:.0%} of the {level:.2f} price level — implausible for a "
                "quarterly carry. Refusing to roll; check the IB data by hand."
            )


def _preflight(status: dict) -> None:
    """Refuse to roll unless gap-fill + promote have already run for the old contract.

    Promote is what freezes the old era. Rolling first would strand the old contract's final
    session outside its own subfolder — the exact silent corruption this ordering prevents.
    """
    if status["already_rolled"]:
        raise RuntimeError(f"Refusing to roll: {status['reason']}")
    if not status["due"]:
        raise RuntimeError(f"Refusing to roll: {status['reason']}")

    live = paths.general_live_dir()
    rows = load_ledger()
    cur_sub = paths.general_main_dir() / str(rows[0]["subfolder"]) if rows else paths.general_main_dir()
    for name in PARQUET_NAMES:
        lp, mp = live / name, cur_sub / name
        if not lp.exists():
            raise RuntimeError(f"Live parquet missing: {lp}. Run `trade.py gap-fill` first.")
        if not mp.exists():
            raise RuntimeError(f"Main parquet missing: {mp}. Run `trade.py promote` first.")
        lt = pd.read_parquet(lp).index[-1]
        mt = pd.read_parquet(mp).index[-1]
        if lt != mt:
            raise RuntimeError(
                f"{name}: live ends {lt} but main/{cur_sub.name} ends {mt}. The old contract's "
                "final session is not frozen in its own subfolder yet — run `trade.py gap-fill` "
                "(with the OLD conids still in .env) and then `trade.py promote`, before rolling."
            )


def run_rollover_prep(today: str | None = None, dry_run: bool = False) -> dict:
    """Execute the quarterly roll. Assumes gap-fill + promote have already run.

    Step order is load-bearing — see the module docstring. In particular the new subfolder is
    fully populated BEFORE the ledger row is written, because both readers fall back to the
    flat legacy main dir when a row names a subfolder that does not exist yet.
    """
    load_dotenv(dotenv_path=env_path())
    today = today or today_str()

    rows = load_ledger()
    status = rollover_status(rows, os.environ.get("ROLLOVER_PREP_DATE"), today)
    _preflight(status)

    prep_date = status["prep_date"]
    cur_expiry = str(rows[0].get("expiry", "")) if rows else ""
    # Old conids come from the LEDGER, not .env: the operator updates the .env conid lines
    # BEFORE this runs, so by now .env names the new contract and the old numbers are gone.
    # The newest row's `new_conid` is the contract we are rolling away from.
    old_conids = _old_conids_from_ledger(rows)

    from ib_insync import IB
    ib = IB()
    # readonly: this never places orders, and it skips the open/completed-order bootstrap.
    # RequestTimeout is raised from the 4s default because the roll runs at weekends, when the
    # Gateway is often slow right after its daily restart.
    ib.RequestTimeout = 30
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    try:
        ib.connect(host, port, clientId=IB_CLIENT_ID, readonly=True)
    except Exception as exc:
        # A bare asyncio.TimeoutError stringifies to '', so the default message would be the
        # useless "ERROR:". The socket can accept while the Gateway is still logged out, so
        # "IB is reachable" is not the same as "IB will serve data".
        raise RuntimeError(
            f"Could not establish a usable IB session at {host}:{port} "
            f"({type(exc).__name__}{': ' + str(exc) if str(exc) else ''}). The port accepts "
            "connections but the Gateway is not serving requests — typically it is still "
            "restarting or logged out. Nothing has been changed; retry once IB is healthy."
        ) from exc
    try:
        new_conids, expiries, gaps, boundaries = {}, {}, {}, {}
        for sym, ticker in (("mnq", "MNQ"), ("mes", "MES")):
            conid, expiry = resolve_next_quarterly(ib, ticker, cur_expiry)
            # Anchor on the live parquet's last bar — the seam — not on whatever bar the two
            # contracts most recently share.
            gap, boundary = measure_gap(ib, old_conids[sym], conid, seam_anchor(ticker))
            new_conids[sym], expiries[sym], gaps[sym], boundaries[sym] = conid, expiry, gap, boundary
            print(f"[rollover] {ticker}: {contract_code(ticker, expiry)} conid={conid} "
                  f"expiry={expiry} gap={gap:+.2f} @ {boundary}", file=sys.stderr)
    finally:
        ib.disconnect()

    if expiries["mnq"] != expiries["mes"]:
        raise RuntimeError(
            f"MNQ expiry {expiries['mnq']} != MES expiry {expiries['mes']} — "
            "the two legs must roll to the same quarter. Resolve manually."
        )
    expiry = expiries["mnq"]
    next_prep = next_prep_date(expiry)
    new_sub = paths.general_main_dir() / subfolder_for(expiry)

    plan = {
        "prep_date": prep_date, "expiry": expiry, "next_prep_date": next_prep,
        "subfolder": new_sub.name, "gaps": gaps,
        "old_conids": old_conids, "new_conids": new_conids,
        "boundaries": {k: str(v) for k, v in boundaries.items()},
    }
    _sanity_check_gaps(gaps, rows)

    if dry_run:
        # The dry run is what the operator reads to learn which conids to put in .env, so it
        # must not require .env to already name them.
        plan["dry_run"] = True
        plan["env_conids_ok"] = env_conids() == new_conids
        return plan

    # Last gate before anything destructive: .env must already name the new front month.
    verify_env_conids(new_conids, expiry)

    done = _load_state(prep_date)
    live = paths.general_live_dir()

    # 1. Back-adjust the LIVE parquets in place (main/ subfolders stay raw and frozen).
    if "backadjust" not in done:
        for name in PARQUET_NAMES:
            sym = "mnq" if name.startswith("MNQ") else "mes"
            p = live / name
            shutil.copy2(p, p.with_suffix(".parquet.preroll.bak"))
            adjusted = back_adjust(pd.read_parquet(p), gaps[sym])
            tmp = p.with_suffix(".parquet.tmp")
            adjusted.to_parquet(tmp)
            os.replace(tmp, p)
            print(f"[rollover] back-adjusted live {name} by {gaps[sym]:+.2f}", file=sys.stderr)
        done = _mark_step(prep_date, "backadjust", done)

    # 2. Re-seed global.json's all_time_high onto the new price scale. A stale ATH silently
    #    disables rule2b's recovery guard and the strategy fades the trend (GIL-23).
    if "global_ath" not in done:
        gp = live / "global.json"
        if gp.exists():
            shutil.copy2(gp, gp.with_suffix(".json.preroll.bak"))
            g = json.loads(gp.read_text(encoding="utf-8"))
            new_ath = float(pd.read_parquet(live / "MNQ_1m.parquet")["High"].max())
            print(f"[rollover] global.json all_time_high {g.get('all_time_high')} -> {new_ath}",
                  file=sys.stderr)
            g["all_time_high"] = new_ath
            gp.write_text(json.dumps(g, indent=2) + "\n", encoding="utf-8")
        done = _mark_step(prep_date, "global_ath", done)

    # 3. Populate the new era's subfolder from the back-adjusted live set — BEFORE the ledger
    #    row names it, since both readers fall back to the flat main dir on a missing subfolder.
    if "subfolder" not in done:
        new_sub.mkdir(parents=True, exist_ok=True)
        for name in PARQUET_NAMES:
            shutil.copy2(live / name, new_sub / name)
        print(f"[rollover] populated main/{new_sub.name} from back-adjusted live", file=sys.stderr)
        done = _mark_step(prep_date, "subfolder", done)

    # 4. Prepend the ledger row — routing flips to the new era here, once its data is in place.
    if "ledger" not in done:
        row = build_ledger_row(prep_date, expiry, gaps, old_conids, new_conids)
        write_ledger(prepend_row(load_ledger(), row))
        print(f"[rollover] ledger row prepended: {row['subfolder']}", file=sys.stderr)
        done = _mark_step(prep_date, "ledger", done)

    # 5. .env last: advance the completion marker. The conid lines were the operator's edit and
    #    were verified before any of the above ran.
    if "env" not in done:
        update_env_prep_date(next_prep)
        print(f"[rollover] .env ROLLOVER_PREP_DATE={next_prep}", file=sys.stderr)
        done = _mark_step(prep_date, "env", done)

    _clear_state()
    return plan


def rollover_block_reason(today: str | None = None) -> str | None:
    """Why IB fetching must stop right now, or None when it may proceed.

    A pending roll has two phases and only the second one blocks:

      A. due, but the old contract's final session is not frozen in its own subfolder yet.
         The old-conid gap-fill is exactly what has to happen — allow it.
      B. due, and live == the current main subfolder (gap-fill AND promote have run). Every
         further fetch now risks landing on the wrong side of the price-scale switch, so stop
         until the roll completes.

    Phase B is detected with the same preflight the roll itself uses, so the gate opens and
    closes on one definition. Completing the roll writes the ledger row, which makes the status
    not-due — so the gate reopens by construction, with no separate approval flag to go stale.
    """
    try:
        rows = load_ledger()
        status = rollover_status(rows, read_env_value("ROLLOVER_PREP_DATE"), today or today_str())
        if not status["due"]:
            return None
        try:
            _preflight(status)
        except RuntimeError:
            return None          # phase A — the old-conid fetch is the next required step
    except Exception:
        return None              # never let the gate itself break the command it guards

    return (
        "\n"
        "=============== BLOCKED: CONTRACT ROLLOVER PENDING ===============\n"
        f"  {status['reason']}\n"
        "  The old contract's final session is already gap-filled and promoted, so the\n"
        "  next IB fetch would land on the wrong side of the price-scale switch.\n"
        "\n"
        "  To clear this:\n"
        "    1. uv run python trade.py rollover-prep --dry-run   (prints the new conids)\n"
        "    2. set MNQ_CONID / MES_CONID in .env to those values\n"
        "       (leave ROLLOVER_PREP_DATE alone — the roll advances it on completion)\n"
        "    3. uv run python trade.py rollover-prep\n"
        "\n"
        "  Step 3 verifies .env actually names the new front month before it shifts\n"
        "  anything, and reopens this gate by writing the ledger row.\n"
        "==================================================================\n"
    )


def due_banner(today: str | None = None) -> str | None:
    """One-line-per-line warning when the quarterly roll is due but has not run, else None.

    Called by `trade.py gap-fill` and `trade.py promote` so the roll cannot be missed by an
    operator (or agent) who never opened the parquet-check skill.
    """
    today = today or today_str()
    status = rollover_status(load_ledger(), read_env_value("ROLLOVER_PREP_DATE"), today)
    if not status["due"]:
        return None
    return (
        "\n"
        "==================== CONTRACT ROLLOVER DUE ====================\n"
        f"  {status['reason']}\n"
        "  Required order — each step depends on the one before it:\n"
        "    1. trade.py gap-fill   (with the OLD conids still in .env)\n"
        "    2. trade.py promote    (freezes the old era in its own subfolder)\n"
        "    3. trade.py rollover-prep\n"
        "  Do NOT edit MNQ_CONID/MES_CONID by hand before step 1, and do NOT\n"
        "  promote after step 3 — both silently corrupt the backtest parquets.\n"
        "  Details: .claude/skills/parquet-check/SKILL.md (rollover-prep).\n"
        "==============================================================\n"
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Quarterly MNQ/MES contract rollover-prep.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve conids and measure gaps, but change nothing.")
    ap.add_argument("--today", default=None, help="Override today's date (YYYY-MM-DD).")
    args = ap.parse_args()
    print(json.dumps(run_rollover_prep(today=args.today, dry_run=args.dry_run),
                     indent=2, default=str))
