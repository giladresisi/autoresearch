"""
Generate an interactive HTML chart for a live trading session.
Overlays strategy events, key price levels, and SMT div marks on MNQ 1m candlesticks.
Reads from sessions/{date}/ — works mid-session (no events yet = shows bars + levels only).

The DATE argument is the TH (Asia/Bangkok) session date, which identifies the CME session
that opened at 18:00 ET on (DATE - 1 day) and closed at 17:00 ET on DATE.

Usage (run from automation root):
    python plot_session.py                # today's session (TH timezone)
    python plot_session.py 2026-05-28    # session that closed at 17:00 ET on 2026-05-28

Output: sessions/{date}/chart_{HH-MM}.html (timestamped at time of request)
"""

import datetime
import json
import os
import sys
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go

import paths
from session_times import session_date_str

_ET = ZoneInfo("America/New_York")

# DATE = TH session date = ET close date.
# Default: current session date in TH timezone.
DATE = sys.argv[1] if len(sys.argv) > 1 else session_date_str()

_NOW_ET = datetime.datetime.now(tz=_ET)
_REQUEST_TIME = _NOW_ET.strftime("%H-%M")
_REQUEST_TIME_LABEL = _NOW_ET.strftime("%H:%M ET")

SESSION_DIR = paths.sessions_dir() / DATE
MNQ_DOLLARS_PER_POINT_PER_CONTRACT = 2.0
DEFAULT_CONTRACTS = 2

# ── Price data ────────────────────────────────────────────────────────────────
# Session spans 18:00 ET on (DATE - 1 day) to 17:00 ET on DATE.
# DATE is treated as the ET close date (TH session date = ET close date in summer).
_session_close_date = datetime.date.fromisoformat(DATE)
_session_open_date = _session_close_date - datetime.timedelta(days=1)
_session_start = pd.Timestamp(
    datetime.datetime(_session_open_date.year, _session_open_date.month,
                      _session_open_date.day, 18, 0),
    tz=_ET,
)
_session_end = pd.Timestamp(
    datetime.datetime(_session_close_date.year, _session_close_date.month,
                      _session_close_date.day, 17, 0),
    tz=_ET,
)

df = pd.read_parquet(paths.general_main_dir() / "MNQ_1m.parquet")
# Ensure tz-aware comparison
if df.index.tz is None:
    df.index = df.index.tz_localize(_ET)
else:
    df.index = df.index.tz_convert(_ET)
day = df[(df.index >= _session_start) & (df.index <= _session_end)]

# ── Events ────────────────────────────────────────────────────────────────────
events_path = SESSION_DIR / "events.jsonl"
events = []
_SKIP_KINDS = {"liquidity-updated"}  # high-frequency housekeeping events, not plotted
if events_path.exists():
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            e = json.loads(line)
            if e.get("kind") not in _SKIP_KINDS:
                events.append(e)
for e in events:
    e["ts"] = pd.Timestamp(e["time"])


def _event_price(e: dict) -> float:
    """Return a display price for any event regardless of live vs regression field naming."""
    return float(e.get("_fill_price") or e.get("price") or
                 e.get("entry_price") or e.get("new_entry_price") or
                 e.get("stop_price") or 0.0)

EXIT_KINDS = {"stopped-out", "market-close", "end-of-session", "stop-exit"}

# ── Levels ────────────────────────────────────────────────────────────────────
levels_path = SESSION_DIR / "levels.json"
levels_data = json.loads(levels_path.read_text(encoding="utf-8")) if levels_path.exists() else {}
liquidities = levels_data.get("liquidities", [])
ath = levels_data.get("all_time_high")

named = {l["name"]: l["price"] for l in liquidities if l.get("kind") == "level" and "price" in l}
# Universe (B) prev-day/prev-week fixed levels: merge so the line-drawer renders them
# (grey-dot fallback style, window-clipped) and SMT marks land on their level.
for l in levels_data.get("liquidities_universe", []):
    if l.get("kind") == "level" and "price" in l:
        named.setdefault(l["name"], l["price"])
if ath is not None:
    named["ATH"] = ath

mids = {}
if "week_high" in named and "week_low" in named:
    mids["week_mid"] = (named["week_high"] + named["week_low"]) / 2
if "day_high" in named and "day_low" in named:
    mids["day_mid"] = (named["day_high"] + named["day_low"]) / 2

all_named = {**named, **mids}

# (label, color, dash, linewidth)
LEVEL_STYLE: dict[str, tuple] = {
    "ATH":             ("ATH",     "#FF1744", "solid", 2.0),
    "TWO":             ("TWO",     "#00E676", "dash",  1.5),
    "TDO":             ("TDO",     "#69F0AE", "dash",  1.5),
    "week_high":       ("Wk H",    "#FFB300", "solid", 1.5),
    "week_low":        ("Wk L",    "#FFB300", "solid", 1.5),
    "week_mid":        ("Wk Mid",  "#FFD54F", "dot",   1.0),
    "day_high":        ("Day H",   "#40C4FF", "solid", 1.5),
    "day_low":         ("Day L",   "#40C4FF", "solid", 1.5),
    "day_mid":         ("Day Mid", "#80D8FF", "dot",   1.0),
    "london_high":     ("Lon H",   "#BDBDBD", "dot",   1.0),
    "london_low":      ("Lon L",   "#BDBDBD", "dot",   1.0),
    "ny_morning_high": ("NYM H",   "#9E9E9E", "dot",   1.0),
    "ny_morning_low":  ("NYM L",   "#9E9E9E", "dot",   1.0),
    "ny_evening_high": ("NYE H",   "#757575", "dot",   1.0),
    "ny_evening_low":  ("NYE L",   "#757575", "dot",   1.0),
    "asia_high":       ("Asia H",  "#616161", "dot",   1.0),
    "asia_low":        ("Asia L",  "#616161", "dot",   1.0),
}
LEVEL_PRIORITY = list(LEVEL_STYLE.keys())

# ── Pair fills to exits ───────────────────────────────────────────────────────
# Live stop-entry-filled events use stop_price (stop-loss), not the fill price.
# We track entry_price from the preceding new/move-stop-entry and inject _fill_price
# so the rest of the chart code can use _event_price() uniformly.
pairs = []
pending_fill = None
_last_order_price: float | None = None

for e in events:
    if e["kind"] == "new-stop-entry":
        _last_order_price = float(e.get("entry_price") or e.get("price") or 0)
    elif e["kind"] == "move-stop-entry":
        _last_order_price = float(e.get("new_entry_price") or e.get("entry_price") or e.get("price") or 0)
    if e["kind"] in ("stop-entry-filled", "market-entry"):
        pending_fill = e
        # Only inject _fill_price from the pending stop order if the event has neither
        # a direct price nor an entry_price (market-entry always carries entry_price).
        if "price" not in e and not e.get("entry_price") and _last_order_price:
            e["_fill_price"] = _last_order_price
        _last_order_price = None
    elif e["kind"] in ("cancel-stop-entry", "stop-entry-cancelled"):
        # Inject last known order price when the event carries no valid price of its own.
        # cancel-stop-entry (user-requested) often arrives with entry_price=0.0 because
        # IB may have already torn down the order before reporting its price.
        if _last_order_price and not float(e.get("price") or e.get("entry_price") or 0):
            e["_fill_price"] = _last_order_price
        _last_order_price = None
    elif e["kind"] in EXIT_KINDS and pending_fill is not None:
        direction_sign = 1 if pending_fill.get("direction", "up") in ("up", "long") else -1
        entry_slip = float(pending_fill.get("slippage", 0.0))
        raw_entry = float(pending_fill.get("_fill_price") or pending_fill.get("price") or
                          pending_fill.get("entry_price") or pending_fill.get("stop_price") or 0.0)
        entry_fill_price = raw_entry + direction_sign * entry_slip
        slip = float(e.get("slippage", 0.0))
        exit_fill_price = e["price"] - direction_sign * slip
        pnl_pts = round((exit_fill_price - entry_fill_price) * direction_sign, 2)
        pnl_usd = round(pnl_pts * MNQ_DOLLARS_PER_POINT_PER_CONTRACT * DEFAULT_CONTRACTS, 2)
        pairs.append({"fill": pending_fill, "exit": e,
                      "entry_fill_price": entry_fill_price,
                      "exit_fill_price": exit_fill_price,
                      "pnl_pts": pnl_pts, "pnl_usd": pnl_usd})
        pending_fill = None

# ── Zoom window ───────────────────────────────────────────────────────────────
if events:
    first_t = min(e["ts"] for e in events) - pd.Timedelta(minutes=30)
    last_t  = max(e["ts"] for e in events) + pd.Timedelta(minutes=30)
else:
    # No events yet: show 09:00–17:00 ET
    first_t = pd.Timestamp(f"{DATE} 09:00", tz="America/New_York")
    last_t  = pd.Timestamp(f"{DATE} 17:00", tz="America/New_York")

window = day[(day.index >= first_t) & (day.index <= last_t)]

# Snap event timestamps to the nearest prior bar so markers land exactly on candles.
# Events have sub-second offsets (e.g. 23:45:06) that fall between 1-minute bar boundaries,
# causing Plotly to float the marker in empty space between candles.
_bar_idx = window.index
def _snap_to_bar(ts: pd.Timestamp) -> pd.Timestamp:
    if _bar_idx.empty:
        return ts
    pos = _bar_idx.searchsorted(ts, side="right") - 1
    return _bar_idx[max(0, pos)]

def _snap_price_to_bar(ts_bar: pd.Timestamp, price: float) -> float:
    """Clamp price to bar's [Low, High] so markers sit on the candle rather than floating."""
    if window.empty or ts_bar not in window.index:
        return price
    bar = window.loc[ts_bar]
    if price > bar["High"]:
        return float(bar["High"])
    if price < bar["Low"]:
        return float(bar["Low"])
    return price

for _e in events:
    _e["ts_bar"] = _snap_to_bar(_e["ts"])

if window.empty:
    price_lo, price_hi = 0.0, 1.0
else:
    price_lo = window["Low"].min()
    price_hi = window["High"].max()
price_margin = (price_hi - price_lo) * 0.08

fig = go.Figure()

# ── Candlesticks ──────────────────────────────────────────────────────────────
fig.add_trace(go.Candlestick(
    x=window.index,
    open=window["Open"], high=window["High"],
    low=window["Low"],   close=window["Close"],
    name="MNQ 1m",
    increasing_line_color="#26a69a",
    decreasing_line_color="#ef5350",
))

# ── Price levels ──────────────────────────────────────────────────────────────
price_to_names: dict[float, list[str]] = {}
for name, price in all_named.items():
    if price_lo - price_margin <= price <= price_hi + price_margin:
        price_to_names.setdefault(price, []).append(name)

for price, names in sorted(price_to_names.items()):
    best = next((n for n in LEVEL_PRIORITY if n in names), names[0])
    label, color, dash, lw = LEVEL_STYLE.get(best, (best, "#9E9E9E", "dot", 1.0))
    combined = " / ".join(LEVEL_STYLE[n][0] for n in names if n in LEVEL_STYLE) or label
    fig.add_trace(go.Scatter(
        x=[first_t, last_t],
        y=[price, price],
        mode="lines+text",
        line=dict(color=color, dash=dash, width=lw),
        text=["", f" {combined} {price}"],
        textposition="top right",
        textfont=dict(size=9, color=color),
        name=combined,
        showlegend=False,
        hovertemplate=f"{combined}: {price}<extra></extra>",
    ))

# ── FVG rectangles ────────────────────────────────────────────────────────────
for liq in liquidities:
    if liq.get("kind") != "fvg":
        continue
    top, bot = liq["top"], liq["bottom"]
    if bot > price_hi + price_margin or top < price_lo - price_margin:
        continue
    fig.add_hrect(
        y0=bot, y1=top,
        fillcolor="rgba(255,235,59,0.08)",
        line_width=0.5, line_color="rgba(255,235,59,0.4)",
        annotation_text=liq["name"].split("_", 1)[1] if "_" in liq["name"] else liq["name"],
        annotation_position="right",
        annotation_font_size=8,
        annotation_font_color="rgba(255,235,59,0.6)",
    )

# ── Limit-order horizontal lines ──────────────────────────────────────────────
limit_x, limit_y = [], []
pending_t = pending_p = None
for e in events:
    if e["kind"] in ("new-stop-entry", "move-stop-entry"):
        if pending_t is not None:
            limit_x += [pending_t, e["ts_bar"], None]
            limit_y += [pending_p, pending_p, None]
        pending_t = e["ts_bar"]
        pending_p = float(e.get("entry_price") or e.get("new_entry_price") or e.get("price") or 0)
    elif e["kind"] in ("stop-entry-filled", "cancel-stop-entry", "stop-entry-cancelled", "market-entry"):
        if pending_t is not None:
            limit_x += [pending_t, e["ts_bar"], None]
            limit_y += [pending_p, pending_p, None]
        pending_t = pending_p = None

if limit_x:
    fig.add_trace(go.Scatter(
        x=limit_x, y=limit_y,
        mode="lines", name="stop price",
        line=dict(dash="dot", color="#64B5F6", width=1.5),
        hoverinfo="skip",
    ))

# ── Stop level horizontal lines ───────────────────────────────────────────────
stop_x, stop_y = [], []
for p in pairs:
    stop_price = p["fill"].get("stop")
    if stop_price is None:
        continue
    stop_x += [p["fill"]["ts_bar"], p["exit"]["ts_bar"], None]
    stop_y += [stop_price, stop_price, None]

if stop_x:
    fig.add_trace(go.Scatter(
        x=stop_x, y=stop_y,
        mode="lines", name="stop level",
        line=dict(dash="dash", color="#EF5350", width=1.5),
        hoverinfo="skip",
    ))

# ── Stop placement markers ────────────────────────────────────────────────────
sp_x, sp_y, sp_hover = [], [], []
for p in pairs:
    stop_price = p["fill"].get("stop")
    if stop_price is None:
        continue
    sp_x.append(p["fill"]["ts_bar"])
    sp_y.append(stop_price)
    sp_hover.append(f"<b>stop placed</b><br>level: {stop_price}<br>time: {p['fill']['ts'].strftime('%H:%M:%S')}")

if sp_x:
    fig.add_trace(go.Scatter(
        x=sp_x, y=sp_y, mode="markers", name="stop placed",
        marker=dict(symbol="line-ew", color="#EF5350", size=12, line=dict(width=2.5, color="#EF5350")),
        hovertemplate="%{customdata}<extra></extra>",
        customdata=sp_hover,
    ))

# ── Open-position lines ───────────────────────────────────────────────────────
for p in pairs:
    color = "#4CAF50" if p["pnl_pts"] >= 0 else "#EF5350"
    fig.add_trace(go.Scatter(
        x=[p["fill"]["ts_bar"], p["exit"]["ts_bar"]],
        y=[p["entry_fill_price"], p["exit_fill_price"]],
        mode="lines", name="position",
        line=dict(color=color, width=2),
        showlegend=False, hoverinfo="skip",
    ))

# ── SMT divergence markers ────────────────────────────────────────────────────
div_events = [e for e in events if e.get("kind") == "smt-div"]

_LEVEL_SCOPE = {
    "week_high": "week", "week_low": "week", "week_mid": "week",
    "day_high":  "day",  "day_low":  "day",  "day_mid":  "day",
    "ny_morning_high": "6hr session", "ny_morning_low": "6hr session",
    "ny_evening_high": "6hr session", "ny_evening_low": "6hr session",
    "london_high":     "6hr session", "london_low":     "6hr session",
    "asia_high":       "6hr session", "asia_low":       "6hr session",
    "ATH": "ATH",
}

def _closest_level_name(lv: float) -> str | None:
    if not all_named:
        return None
    closest = min(all_named.items(), key=lambda x: abs(x[1] - lv))
    return closest[0] if abs(closest[1] - lv) <= 10 else None

def _div_scope(e: dict) -> "str | None":
    """Specific level / FVG name this SMT div fired against. Prefers the event's
    ref_name (emitted by the SMT V2 detector); falls back to the nearest named level
    by price for legacy events. FVG names render as fvg_1hr_<HHMM>."""
    name = e.get("ref_name")
    if not name:
        lv = e.get("mnq_div_price")
        name = _closest_level_name(lv) if lv is not None else None
    if not name:
        return None
    if name.startswith("fvg_"):
        parts = name.split("_")  # fvg_YYYYMMDD_HHMM_side
        return f"fvg_1hr_{parts[2]}" if len(parts) >= 3 else name
    return LEVEL_STYLE.get(name, (name,))[0]

def _div_label(e: dict) -> str:
    tf   = e.get("timeframe", "?")
    # wick->W, body->H, all fill_* (fill_a/fill_b/fill_retrace)->F (first-char fallback).
    typ  = {"wick": "W", "body": "H", "fill": "F"}.get(e.get("type", ""), e.get("type", "?")[:1].upper())
    side = "↑" if e.get("side") == "bullish" else "↓"
    scope = _div_scope(e)
    return f"{tf}{side}{typ}@{scope}" if scope else f"{tf}{side}{typ}"

def _div_hover(e: dict) -> str:
    parts = [
        "<b>SMT div</b>",
        f"tf: {e.get('timeframe')}",
        f"type: {e.get('type')}",
        f"side: {e.get('side')}",
        f"time: {e['ts'].strftime('%H:%M:%S')}",
    ]
    if e.get("phase"):
        parts.append(f"phase: {e.get('phase')}")
    scope = _div_scope(e)
    if scope:
        parts.append(f"level: {scope}")
    mnq_lv = e.get("mnq_div_price")
    if mnq_lv is not None:
        parts.append(f"div_price: {mnq_lv}")
    return "<br>".join(parts)

for side_val, symbol, color in [("bullish", "triangle-up", "#4CAF50"), ("bearish", "triangle-down", "#EF5350")]:
    grp = [e for e in div_events if e.get("side") == side_val]
    if not grp:
        continue
    hover = [_div_hover(e) for e in grp]
    fig.add_trace(go.Scatter(
        x=[e["ts_bar"] for e in grp],
        y=[e["price"] for e in grp],
        mode="markers+text",
        name=f"SMT div {side_val[:4]}",
        marker=dict(symbol=symbol, color=color, size=12, line=dict(width=1.5, color=color)),
        text=[_div_label(e) for e in grp],
        textposition="top center" if side_val == "bullish" else "bottom center",
        textfont=dict(size=9, color=color),
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))

# ── Event markers ─────────────────────────────────────────────────────────────
EXIT_MARKER_STYLE = {
    "stopped-out":    dict(symbol="x-thin",  color="#F44336", size=14),
    "market-close":   dict(symbol="square",  color="#9E9E9E", size=11),
    "end-of-session": dict(symbol="square",  color="#BDBDBD", size=11),
    "stop-exit":      dict(symbol="diamond", color="#FF9800", size=11),
}
OTHER_MARKER_STYLE = {
    "new-stop-entry":    dict(symbol="triangle-right",      color="#2196F3", size=13),
    "move-stop-entry":   dict(symbol="triangle-right-open", color="#9C27B0", size=13),
    "cancel-stop-entry":    dict(symbol="x-open",              color="#FF9800", size=13),
    "stop-entry-cancelled": dict(symbol="x-open",              color="#FF9800", size=13),
    "new-stop-exit":      dict(symbol="triangle-left",       color="#FF5722", size=13),
    "move-stop-exit":     dict(symbol="triangle-left-open",  color="#FF5722", size=13),
    # update-stop-loss intentionally omitted from the plot: it is the broker-side echo
    # of the new-stop-exit/move-stop-exit fired ~1s earlier (already plotted at the real
    # trail price). On the *secondary* cautious the broker stop is parked ±1000 pts away
    # to disable the hard stop (the strategy manages the exit via cautious-secondary-break),
    # so plotting it at stop_price blew out the y-axis with off-chart markers.
    "stop-entry-filled":  dict(symbol="star",                color="#4CAF50", size=17),
    "market-entry":       dict(symbol="circle",              color="#FF9800", size=15),
    "trend-broken":       dict(symbol="diamond-open",        color="#FF9800", size=13),
    "new-hypothesis":     dict(symbol="pentagon",            color="#E040FB", size=15),
}

pnl_by_exit = {(p["exit"]["time"], p["exit"]["kind"]): p for p in pairs}

for kind, style in EXIT_MARKER_STYLE.items():
    group = [e for e in events if e["kind"] == kind]
    if not group:
        continue
    texts, hover, colors = [], [], []
    for e in group:
        pair = pnl_by_exit.get((e["time"], e["kind"]))
        if pair:
            sign = "+" if pair["pnl_pts"] >= 0 else ""
            label = f"{sign}{pair['pnl_pts']} ({sign}${pair['pnl_usd']:.0f})"
            colors.append("#4CAF50" if pair["pnl_pts"] >= 0 else "#FF6B6B")
        else:
            label = ""
            colors.append(style["color"])
        texts.append(label)
        parts = [f"<b>{e['kind']}</b>", f"price: {e['price']}", f"time: {e['ts'].strftime('%H:%M:%S')}"]
        if pair:
            parts.append(f"pnl: {label}")
        if "close_reason" in e:
            parts.append(f"reason: {e['close_reason']}")
        hover.append("<br>".join(parts))

    fig.add_trace(go.Scatter(
        x=[e["ts_bar"] for e in group],
        y=[e["price"] for e in group],
        mode="markers+text",
        name=kind.replace("-", " "),
        marker=dict(symbol=style["symbol"], color=style["color"],
                    size=style["size"], line=dict(width=2, color=style["color"])),
        text=texts, textposition="top right",
        textfont=dict(size=11, color=colors),
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))

for kind, style in OTHER_MARKER_STYLE.items():
    group = [e for e in events if e["kind"] == kind]
    if not group:
        continue
    hover = []
    for e in group:
        ep = _event_price(e)
        parts = [f"<b>{e['kind']}</b>", f"price: {ep}", f"time: {e['ts'].strftime('%H:%M:%S')}"]
        if "direction" in e:
            parts.append(f"direction: {e['direction']}")
        # Live events use stop_price / entry_price / new_entry_price instead of stop / price
        if "entry_price" in e:
            parts.append(f"entry_price: {e['entry_price']}")
        if "new_entry_price" in e:
            parts.append(f"new_entry_price: {e['new_entry_price']}")
        if ("stop" in e or "stop_price" in e) and kind != "update-stop-loss":
            stop = e.get("stop") or e.get("stop_price")
            if kind == "stop-entry-filled":
                dist = abs(ep - stop) if ep and stop else "?"
                parts.append(f"stop: {stop} ({dist:.2f} pts)" if isinstance(dist, float) else f"stop: {stop}")
            else:
                parts.append(f"stop: {stop}")
        if "reason" in e:
            parts.append(f"reason: {e['reason']}")
        if kind in ("new-stop-exit", "move-stop-exit", "update-stop-loss"):
            if e.get("level"):
                parts.append(f"level: {e['level']}")
            if e.get("level_name"):
                parts.append(f"level_name: {e['level_name']}")
            if e.get("cautious_break_price"):
                parts.append(f"cautious_break_price: {e['cautious_break_price']}")
        if kind == "trend-broken":
            if e.get("broken_direction"):
                parts.append(f"was: {e['broken_direction']}")
            if e.get("level_name"):
                parts.append(f"broke level: {e['level_name']} @ {e['level_price']}")
            if "bar_low" in e:
                parts.append(f"bar low: {e['bar_low']}")
            if "bar_high" in e:
                parts.append(f"bar high: {e['bar_high']}")
        if kind == "new-hypothesis":
            if e.get("weekly_mid"):
                parts.append(f"weekly_mid: {e['weekly_mid']}")
            if e.get("daily_mid"):
                parts.append(f"daily_mid: {e['daily_mid']}")
            if e.get("last_liquidity"):
                parts.append(f"last_liquidity: {e['last_liquidity']}")
            cp_sec  = e.get("cautious_price_secondary", "")
            cpl_sec = e.get("cautious_price_secondary_level", "")
            cp_ini  = e.get("cautious_price_initial", "")
            cpl_ini = e.get("cautious_price_initial_level", "")
            if cp_sec not in ("", None):
                parts.append(f"cautious_secondary: {cp_sec} ({cpl_sec})" if cpl_sec else f"cautious_secondary: {cp_sec}")
            else:
                parts.append("cautious_secondary: none")
            if cp_ini not in ("", None):
                parts.append(f"cautious_initial: {cp_ini} ({cpl_ini})" if cpl_ini else f"cautious_initial: {cp_ini}")
            else:
                parts.append("cautious_initial: none")
            for er in e.get("entry_ranges", []):
                parts.append(f"entry_{er['source']}: [{er['low']}, {er['high']}]")
            dr = e.get("direction_reason", {})
            if dr:
                _rule = dr.get("rule", "?")
                if _rule == "rule1":
                    _lvl = dr.get("fresh_touch_level", "?")
                    _aln = dr.get("smt_alignment", "")
                    _decided = f"swept {_lvl} fresh" + (f", smt {_aln}" if _aln else "")
                elif _rule == "rule2":
                    _lvl = dr.get("approaching_level", "?")
                    _dist = dr.get("approaching_dist", "?")
                    _decided = f"approaching {_lvl} ({_dist} pts)"
                elif _rule == "rule2b":
                    _lvl = dr.get("last_swept_level", "?")
                    _mid = e.get("daily_mid", "?")
                    _decided = f"last swept {_lvl}, mid={_mid}"
                elif _rule == "rule3_4":
                    _sc = dr.get("combined_score", "?")
                    _decided = f"bias score {_sc}"
                elif _rule == "rule5_trend":
                    _dir = e.get("direction", "?")
                    _decided = f"global trend ({_dir})"
                else:
                    _decided = _rule
                parts.append(f"decided_by: {_decided}")
                parts.append(f"weekly: {dr.get('weekly_zone', '?')} | daily: {dr.get('daily_zone', '?')}")
                parts.append(f"smt_score: {dr.get('smt_score', '?')}")
                if dr.get("fresh_touch_level"):
                    parts.append(f"touched: {dr['fresh_touch_level']}  smt: {dr.get('smt_alignment', '?')}")
                if dr.get("approaching_level"):
                    parts.append(f"approaching: {dr['approaching_level']} ({dr.get('approaching_dist', '?')} pts)")
                if dr.get("combined_score") is not None:
                    parts.append(
                        f"pd: {dr.get('pd_score', '?')}  "
                        f"bos1h: {dr.get('bos_score_1hr', '?')}  "
                        f"bos4h: {dr.get('bos_score_4hr', '?')}  "
                        f"→ {dr.get('combined_score', '?')}"
                    )
        hover.append("<br>".join(parts))
    _price_snap_kinds = {
        "new-stop-entry", "move-stop-entry",
        "cancel-stop-entry", "stop-entry-cancelled",
        "new-hypothesis", "trend-broken",
        "market-entry", "stop-entry-filled",
    }
    if kind in _price_snap_kinds:
        ys = [_snap_price_to_bar(e["ts_bar"], _event_price(e)) for e in group]
    else:
        ys = [_event_price(e) for e in group]
    # Up/down hypotheses get directional arrow markers (distinct from SMT triangles).
    sym = ([("arrow-up" if e.get("direction") == "up" else "arrow-down") for e in group]
           if kind == "new-hypothesis" else style["symbol"])
    fig.add_trace(go.Scatter(
        x=[e["ts_bar"] for e in group],
        y=ys,
        mode="markers", name=kind.replace("-", " "),
        marker=dict(symbol=sym, color=style["color"],
                    size=style["size"], line=dict(width=2, color=style["color"])),
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))

# ── Layout ────────────────────────────────────────────────────────────────────
session_hours = (last_t - first_t).total_seconds() / 3600
chart_height  = max(700, min(1200, int(600 + session_hours * 40)))

pnl_total = sum(p["pnl_usd"] for p in pairs)
pnl_str = f"{'+'if pnl_total >= 0 else ''}${pnl_total:.0f}"
if events:
    title = f"Live Session — MNQ {DATE} @ {_REQUEST_TIME_LABEL} | {len(pairs)} trade{'s' if len(pairs) != 1 else ''} | PnL: {pnl_str}"
else:
    title = f"Live Session — MNQ {DATE} @ {_REQUEST_TIME_LABEL} | No events yet"

fig.update_layout(
    title=title,
    xaxis_title="Time (ET)",
    yaxis_title="Price",
    xaxis_rangeslider_visible=False,
    xaxis_type="date",
    template="plotly_dark",
    height=chart_height,
    legend=dict(orientation="h", yanchor="bottom", y=-0.22),
    margin=dict(b=120, r=80),
    hovermode="x unified",
)

SESSION_DIR.mkdir(parents=True, exist_ok=True)
out = SESSION_DIR / f"chart_{_REQUEST_TIME}.html"
fig.write_html(str(out), include_plotlyjs="cdn")
print(f"Chart: {out.resolve()}")
# ACT_NO_BROWSER=1 suppresses the auto-open (used by the test suite so running tests
# never launches a real browser). Interactive use leaves it unset → chart opens as usual.
if not os.environ.get("ACT_NO_BROWSER"):
    webbrowser.open(out.resolve().as_uri())
