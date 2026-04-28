"""
Generate an interactive HTML chart for a regression date.
Overlays SMT events, key price levels, and SMT div marks on MNQ 1m candlesticks.

Usage:
    python plot_regression.py           # reads first date from regression.md
    python plot_regression.py 2026-04-23
"""

import json
import sys
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go


def _date_from_regression_md() -> str:
    for raw in Path("regression.md").read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if line:
            return line.split(":")[0].strip()
    raise ValueError("No date found in regression.md")


DATE = sys.argv[1] if len(sys.argv) > 1 else _date_from_regression_md()

MNQ_DOLLARS_PER_POINT_PER_CONTRACT = 2.0
DEFAULT_CONTRACTS = 2

# ── Price data ────────────────────────────────────────────────────────────────
df = pd.read_parquet("data/MNQ_1m.parquet")
day = df[df.index.date == pd.Timestamp(DATE).date()]

# ── Events ────────────────────────────────────────────────────────────────────
events_path = Path(f"data/regression/{DATE}/events.jsonl")
events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
for e in events:
    e["ts"] = pd.Timestamp(e["time"])

EXIT_KINDS = {"stopped-out", "market-close", "end-of-session"}

# ── Levels ────────────────────────────────────────────────────────────────────
levels_path = Path(f"data/regression/{DATE}/levels.json")
levels_data = json.loads(levels_path.read_text()) if levels_path.exists() else {}
liquidities = levels_data.get("liquidities", [])
ath = levels_data.get("all_time_high")

named = {l["name"]: l["price"] for l in liquidities if l.get("kind") == "level" and "price" in l}
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
pairs = []
pending_fill = None
for e in events:
    if e["kind"] == "limit-entry-filled":
        pending_fill = e
    elif e["kind"] in EXIT_KINDS and pending_fill is not None:
        direction_sign = 1 if pending_fill.get("direction", "up") == "up" else -1
        pnl_pts = round((e["price"] - pending_fill["price"]) * direction_sign, 2)
        pnl_usd = round(pnl_pts * MNQ_DOLLARS_PER_POINT_PER_CONTRACT * DEFAULT_CONTRACTS, 2)
        pairs.append({"fill": pending_fill, "exit": e, "pnl_pts": pnl_pts, "pnl_usd": pnl_usd})
        pending_fill = None

# ── Zoom window ───────────────────────────────────────────────────────────────
first_t = min(e["ts"] for e in events) - pd.Timedelta(minutes=30)
last_t  = max(e["ts"] for e in events) + pd.Timedelta(minutes=30)
window  = day[(day.index >= first_t) & (day.index <= last_t)]

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
# Group identical prices to avoid duplicate lines.
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

# ── FVG rectangles (only if in visible range) ─────────────────────────────────
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
    if e["kind"] in ("new-limit-entry", "move-limit-entry"):
        if pending_t is not None:
            limit_x += [pending_t, e["ts"], None]
            limit_y += [pending_p, pending_p, None]
        pending_t, pending_p = e["ts"], e["price"]
    elif e["kind"] in ("limit-entry-filled", "limit-entry-cancelled", "limit-entry-expired"):
        if pending_t is not None:
            limit_x += [pending_t, e["ts"], None]
            limit_y += [pending_p, pending_p, None]
        pending_t = pending_p = None

if limit_x:
    fig.add_trace(go.Scatter(
        x=limit_x, y=limit_y,
        mode="lines", name="limit price",
        line=dict(dash="dot", color="#64B5F6", width=1.5),
        hoverinfo="skip",
    ))

# ── Stop level horizontal lines ───────────────────────────────────────────────
stop_x, stop_y = [], []
for p in pairs:
    stop_price = p["fill"].get("stop")
    if stop_price is None:
        continue
    stop_x += [p["fill"]["ts"], p["exit"]["ts"], None]
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
    sp_x.append(p["fill"]["ts"])
    sp_y.append(stop_price)
    sp_hover.append(f"<b>stop placed</b><br>level: {stop_price}<br>time: {p['fill']['ts'].strftime('%H:%M')}")

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
        x=[p["fill"]["ts"], p["exit"]["ts"]],
        y=[p["fill"]["price"], p["exit"]["price"]],
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

def _div_label(e: dict) -> str:
    tf   = e.get("timeframe", "?")
    typ  = {"wick": "W", "body": "H", "fill": "F"}.get(e.get("type", ""), e.get("type", "?")[:1].upper())
    side = "↑" if e.get("side") == "bullish" else "↓"
    mnq_lv = e.get("mnq_div_price")
    if mnq_lv is not None:
        name = _closest_level_name(mnq_lv)
        if name:
            lv_name = LEVEL_STYLE.get(name, (name,))[0]
            return f"{tf}{side}{typ}@{lv_name}"
    return f"{tf}{side}{typ}"

def _div_hover(e: dict) -> str:
    parts = [
        f"<b>SMT div</b>",
        f"tf: {e.get('timeframe')}",
        f"type: {e.get('type')}",
        f"side: {e.get('side')}",
        f"time: {e['ts'].strftime('%H:%M')}",
    ]
    mnq_lv = e.get("mnq_div_price")
    mes_lv = e.get("mes_div_price")
    if mnq_lv is not None:
        parts.append(f"div_price: {mnq_lv}")
        if e.get("type") in ("wick", "body", "wick_sym", "body_sym"):
            name = _closest_level_name(mnq_lv)
            scope = _LEVEL_SCOPE.get(name, "") if name else ""
            if scope:
                parts.append(f"scope: {scope}")
    return "<br>".join(parts)

for side_val, symbol, color in [("bullish", "triangle-up", "#4CAF50"), ("bearish", "triangle-down", "#EF5350")]:
    grp = [e for e in div_events if e.get("side") == side_val]
    if not grp:
        continue
    hover = [_div_hover(e) for e in grp]
    fig.add_trace(go.Scatter(
        x=[e["ts"] for e in grp],
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

# ── Event markers (exits with P&L, others plain) ─────────────────────────────
EXIT_MARKER_STYLE = {
    "stopped-out":    dict(symbol="x-thin",  color="#F44336", size=14),
    "market-close":   dict(symbol="square",  color="#9E9E9E", size=11),
    "end-of-session": dict(symbol="square",  color="#BDBDBD", size=11),
}
OTHER_MARKER_STYLE = {
    "new-limit-entry":    dict(symbol="triangle-right",      color="#2196F3", size=13),
    "move-limit-entry":   dict(symbol="triangle-right-open", color="#9C27B0", size=13),
    "limit-entry-filled": dict(symbol="star",                color="#4CAF50", size=17),
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
        parts = [f"<b>{e['kind']}</b>", f"price: {e['price']}", f"time: {e['ts'].strftime('%H:%M')}"]
        if pair:
            parts.append(f"pnl: {label}")
        hover.append("<br>".join(parts))

    fig.add_trace(go.Scatter(
        x=[e["ts"] for e in group],
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
        parts = [f"<b>{e['kind']}</b>", f"price: {e['price']}", f"time: {e['ts'].strftime('%H:%M')}"]
        if "direction" in e:
            parts.append(f"direction: {e['direction']}")
        if "stop" in e:
            stop = e["stop"]
            if kind == "limit-entry-filled":
                dist = abs(e["price"] - stop)
                dir_sign = 1 if e.get("direction") == "up" else -1
                signed = dir_sign * (stop - e["price"])
                parts.append(f"stop: {stop} ({dist:.2f} pts)")
            else:
                parts.append(f"stop: {stop}")
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
            targets = e.get("targets", [])
            if targets:
                tgt_str = ", ".join(f"{t['name']}@{t['price']}" for t in targets)
                parts.append(f"targets: {tgt_str}")
            cp  = e.get("cautious_price", "")
            cpl = e.get("cautious_price_level", "")
            if cp not in ("", None):
                parts.append(f"cautious_price: {cp} ({cpl})" if cpl else f"cautious_price: {cp}")
            else:
                parts.append("cautious_price: none")
            for er in e.get("entry_ranges", []):
                parts.append(f"entry_{er['source']}: [{er['low']}, {er['high']}]")
        hover.append("<br>".join(parts))
    fig.add_trace(go.Scatter(
        x=[e["ts"] for e in group],
        y=[e["price"] for e in group],
        mode="markers", name=kind.replace("-", " "),
        marker=dict(symbol=style["symbol"], color=style["color"],
                    size=style["size"], line=dict(width=2, color=style["color"])),
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))

# ── Layout — height scales with session duration ──────────────────────────────
session_hours = (last_t - first_t).total_seconds() / 3600
chart_height  = max(700, min(1200, int(600 + session_hours * 40)))

fig.update_layout(
    title=f"SMT Events — MNQ {DATE} | {len(pairs)} trades | PnL: {'+'if sum(p['pnl_usd'] for p in pairs)>=0 else ''}${sum(p['pnl_usd'] for p in pairs):.0f}",
    xaxis_title="Time (ET)",
    yaxis_title="Price",
    xaxis_rangeslider_visible=False,
    template="plotly_dark",
    height=chart_height,
    legend=dict(orientation="h", yanchor="bottom", y=-0.22),
    margin=dict(b=120, r=80),
    hovermode="x unified",
)

out = Path(f"data/regression/{DATE}/chart.html")
fig.write_html(str(out), include_plotlyjs="cdn")
print(f"Chart: {out.resolve()}")
webbrowser.open(out.resolve().as_uri())
