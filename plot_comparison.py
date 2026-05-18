"""
Overlay baseline vs current-branch trades on MNQ 1m candlesticks,
plus all SMT event markers (hypothesis, trend-broken, SMT divs, entry/exit signals).

Usage (run from project root):
    uv run python plot_comparison.py 2026-04-30
    uv run python plot_comparison.py 2026-04-30 2026-05-04 2026-05-14 2026-05-15
"""
import json
import sys
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

DATES = sys.argv[1:]
if not DATES:
    print("Usage: plot_comparison.py DATE [DATE ...]")
    sys.exit(1)

MNQ_DOLLARS_PER_POINT_PER_CONTRACT = 2.0
DEFAULT_CONTRACTS = 2

df_all = pd.read_parquet("data/MNQ_1m.parquet")

# ── Level styling (copied from plot_regression.py) ────────────────────────────
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
    "prev1_day_high":  ("P1H",     "#757575", "dot",   1.0),
    "prev1_day_low":   ("P1L",     "#757575", "dot",   1.0),
    "prev2_day_high":  ("P2H",     "#616161", "dot",   1.0),
    "prev2_day_low":   ("P2L",     "#616161", "dot",   1.0),
}
LEVEL_PRIORITY = list(LEVEL_STYLE.keys())

EXIT_KINDS = {"stopped-out", "market-close", "end-of-session"}

EXIT_MARKER_STYLE = {
    "stopped-out":    dict(symbol="x-thin",  color="#F44336", size=14),
    "market-close":   dict(symbol="square",  color="#9E9E9E", size=11),
    "end-of-session": dict(symbol="square",  color="#BDBDBD", size=11),
}
OTHER_MARKER_STYLE = {
    "new-stop-entry":    dict(symbol="triangle-right",      color="#2196F3", size=13),
    "move-stop-entry":   dict(symbol="triangle-right-open", color="#9C27B0", size=13),
    "stop-entry-filled": dict(symbol="star",                color="#4CAF50", size=17),
    "market-entry":      dict(symbol="circle",              color="#FF9800", size=15),
    "trend-broken":      dict(symbol="diamond-open",        color="#FF9800", size=13),
    "new-hypothesis":    dict(symbol="pentagon",            color="#E040FB", size=15),
}

_LEVEL_SCOPE = {
    "week_high": "week", "week_low": "week", "week_mid": "week",
    "day_high":  "day",  "day_low":  "day",  "day_mid":  "day",
    "ny_morning_high": "6hr", "ny_morning_low": "6hr",
    "ny_evening_high": "6hr", "ny_evening_low": "6hr",
    "london_high": "6hr", "london_low": "6hr",
    "asia_high": "6hr",  "asia_low": "6hr",
}


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for e in events:
        e["ts"] = pd.Timestamp(e["time"])
    return events


def _read_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    lines = content.split("\n")
    headers = lines[0].split("\t")
    return [dict(zip(headers, row.split("\t"))) for row in lines[1:] if row.strip()]


def _tsv_to_pairs(trades: list[dict]) -> list[dict]:
    pairs = []
    for t in trades:
        try:
            pairs.append({
                "entry_t":   pd.Timestamp(t["entry_time"]),
                "exit_t":    pd.Timestamp(t["exit_time"]),
                "ep":        float(t["entry_price"]),
                "xp":        float(t["exit_price"]),
                "pnl":       float(t["pnl_dollars"]),
                "pnl_pts":   float(t["pnl_points"]),
                "direction": t.get("direction", "up"),
                "reason":    t.get("exit_reason", ""),
            })
        except Exception:
            continue
    return pairs


def _closest_level_name(all_named: dict, lv: float) -> str | None:
    if not all_named:
        return None
    closest = min(all_named.items(), key=lambda x: abs(x[1] - lv))
    return closest[0] if abs(closest[1] - lv) <= 10 else None


def _div_label(e: dict, all_named: dict) -> str:
    tf   = e.get("timeframe", "?")
    typ  = {"wick": "W", "body": "H", "fill": "F"}.get(e.get("type", ""), e.get("type", "?")[:1].upper())
    side = "↑" if e.get("side") == "bullish" else "↓"
    mnq_lv = e.get("mnq_div_price")
    if mnq_lv is not None:
        name = _closest_level_name(all_named, mnq_lv)
        if name:
            lv_name = LEVEL_STYLE.get(name, (name,))[0]
            return f"{tf}{side}{typ}@{lv_name}"
    return f"{tf}{side}{typ}"


def _div_hover(e: dict, all_named: dict) -> str:
    parts = [
        "<b>SMT div</b>",
        f"tf: {e.get('timeframe')}",
        f"type: {e.get('type')}",
        f"side: {e.get('side')}",
        f"time: {e['ts'].strftime('%H:%M')}",
    ]
    mnq_lv = e.get("mnq_div_price")
    if mnq_lv is not None:
        parts.append(f"div_price: {mnq_lv}")
        if e.get("type") in ("wick", "body", "wick_sym", "body_sym"):
            name = _closest_level_name(all_named, mnq_lv)
            scope = _LEVEL_SCOPE.get(name, "") if name else ""
            if scope:
                parts.append(f"scope: {scope}")
    return "<br>".join(parts)


def _other_hover(e: dict, kind: str) -> str:
    parts = [f"<b>{kind}</b>", f"price: {e['price']}", f"time: {e['ts'].strftime('%H:%M')}"]
    if "direction" in e:
        parts.append(f"direction: {e['direction']}")
    if "stop" in e:
        stop = e["stop"]
        if kind == "stop-entry-filled":
            dist = abs(e["price"] - stop)
            parts.append(f"stop: {stop} ({dist:.2f} pts)")
        else:
            parts.append(f"stop: {stop}")
    if kind == "trend-broken":
        if e.get("broken_direction"):
            parts.append(f"was: {e['broken_direction']}")
        if e.get("level_name"):
            parts.append(f"broke level: {e['level_name']} @ {e['level_price']}")
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
        if cp_ini not in ("", None):
            parts.append(f"cautious_initial: {cp_ini} ({cpl_ini})" if cpl_ini else f"cautious_initial: {cp_ini}")
        for er in e.get("entry_ranges", []):
            parts.append(f"entry_{er['source']}: [{er['low']}, {er['high']}]")
        dr = e.get("direction_reason", {})
        if dr:
            rule = dr.get("rule", "?")
            if rule == "rule1":
                decided = f"swept {dr.get('fresh_touch_level','?')} fresh"
            elif rule == "rule2":
                decided = f"approaching {dr.get('approaching_level','?')} ({dr.get('approaching_dist','?')} pts)"
            elif rule == "rule2b":
                decided = f"last swept {dr.get('last_swept_level','?')}, mid={e.get('daily_mid','?')}"
            elif rule == "rule3_4":
                decided = f"bias score {dr.get('combined_score','?')}"
            elif rule == "rule5_trend":
                decided = f"global trend ({e.get('direction','?')})"
            else:
                decided = rule
            parts.append(f"decided_by: {decided}")
            parts.append(f"weekly: {dr.get('weekly_zone','?')} | daily: {dr.get('daily_zone','?')}")
            parts.append(f"smt_score: {dr.get('smt_score','?')}")
    return "<br>".join(parts)


def _plot_date(date: str) -> Path:
    reg = Path(f"data/regression/{date}")

    day = df_all[df_all.index.date == pd.Timestamp(date).date()]

    # Levels
    levels_data = json.loads((reg / "levels.json").read_text()) if (reg / "levels.json").exists() else {}
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

    # Events (current branch only — these carry hypothesis/trend-broken/SMT divs)
    events = _read_events(reg / "events.jsonl")

    # Trade pairs
    base_pairs = _tsv_to_pairs(_read_trades(reg / "baseline_trades.tsv"))
    curr_pairs  = _tsv_to_pairs(_read_trades(reg / "trades.tsv"))

    # Window: from earliest event or trade to latest
    all_ts = [e["ts"] for e in events] + \
             [p["entry_t"] for p in base_pairs + curr_pairs] + \
             [p["exit_t"]  for p in base_pairs + curr_pairs]
    if all_ts:
        first_t = min(all_ts) - pd.Timedelta(minutes=30)
        last_t  = max(all_ts) + pd.Timedelta(minutes=30)
    else:
        first_t = day.index[0]  if len(day) else pd.Timestamp(date + " 09:30", tz="America/New_York")
        last_t  = day.index[-1] if len(day) else pd.Timestamp(date + " 16:00", tz="America/New_York")

    window = day[(day.index >= first_t) & (day.index <= last_t)]
    price_lo = window["Low"].min()
    price_hi = window["High"].max()
    price_margin = (price_hi - price_lo) * 0.08

    fig = go.Figure()

    # ── Candlesticks ──────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=window.index,
        open=window["Open"], high=window["High"],
        low=window["Low"],   close=window["Close"],
        name="MNQ 1m",
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    # ── Price levels ──────────────────────────────────────────────────────────
    price_to_names: dict[float, list[str]] = {}
    for name, price in all_named.items():
        if price_lo - price_margin <= price <= price_hi + price_margin:
            price_to_names.setdefault(price, []).append(name)

    for price, names in sorted(price_to_names.items()):
        best = next((n for n in LEVEL_PRIORITY if n in names), names[0])
        label, color, dash, lw = LEVEL_STYLE.get(best, (best, "#9E9E9E", "dot", 1.0))
        combined = " / ".join(LEVEL_STYLE[n][0] for n in names if n in LEVEL_STYLE) or label
        fig.add_trace(go.Scatter(
            x=[first_t, last_t], y=[price, price],
            mode="lines+text",
            line=dict(color=color, dash=dash, width=lw),
            text=["", f" {combined} {price}"],
            textposition="top right",
            textfont=dict(size=9, color=color),
            name=combined, showlegend=False,
            hovertemplate=f"{combined}: {price}<extra></extra>",
        ))

    # ── FVG rectangles ────────────────────────────────────────────────────────
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

    # ── Limit-order lines (current branch) ───────────────────────────────────
    limit_x, limit_y = [], []
    pending_t = pending_p = None
    for e in events:
        if e["kind"] in ("new-stop-entry", "move-stop-entry"):
            if pending_t is not None:
                limit_x += [pending_t, e["ts"], None]
                limit_y += [pending_p, pending_p, None]
            pending_t, pending_p = e["ts"], e["price"]
        elif e["kind"] in ("stop-entry-filled", "limit-entry-cancelled", "limit-entry-expired", "market-entry"):
            if pending_t is not None:
                limit_x += [pending_t, e["ts"], None]
                limit_y += [pending_p, pending_p, None]
            pending_t = pending_p = None

    if limit_x:
        fig.add_trace(go.Scatter(
            x=limit_x, y=limit_y, mode="lines", name="limit price",
            line=dict(dash="dot", color="#64B5F6", width=1.5),
            hoverinfo="skip",
        ))

    # ── Stop level lines (current branch) ────────────────────────────────────
    # Reconstruct current pairs from events to get stop prices
    evt_pairs = []
    pending_fill = None
    for e in events:
        if e["kind"] in ("stop-entry-filled", "market-entry"):
            pending_fill = e
        elif e["kind"] in EXIT_KINDS and pending_fill is not None:
            evt_pairs.append({"fill": pending_fill, "exit": e})
            pending_fill = None

    stop_x, stop_y, sp_x, sp_y, sp_hover = [], [], [], [], []
    for p in evt_pairs:
        stop_price = p["fill"].get("stop")
        if stop_price is None:
            continue
        stop_x += [p["fill"]["ts"], p["exit"]["ts"], None]
        stop_y += [stop_price, stop_price, None]
        sp_x.append(p["fill"]["ts"])
        sp_y.append(stop_price)
        sp_hover.append(f"<b>stop placed</b><br>level: {stop_price}<br>time: {p['fill']['ts'].strftime('%H:%M')}")

    if stop_x:
        fig.add_trace(go.Scatter(
            x=stop_x, y=stop_y, mode="lines", name="stop level",
            line=dict(dash="dash", color="#EF5350", width=1.5),
            hoverinfo="skip",
        ))
    if sp_x:
        fig.add_trace(go.Scatter(
            x=sp_x, y=sp_y, mode="markers", name="stop placed",
            marker=dict(symbol="line-ew", color="#EF5350", size=12, line=dict(width=2.5, color="#EF5350")),
            hovertemplate="%{customdata}<extra></extra>",
            customdata=sp_hover,
        ))

    # ── Baseline trade lines (blue dashed) ───────────────────────────────────
    base_pnl = sum(p["pnl"] for p in base_pairs)
    legend_shown = False
    for p in base_pairs:
        color = "#42A5F5" if p["pnl"] >= 0 else "#EF9A9A"
        fig.add_trace(go.Scatter(
            x=[p["entry_t"], p["exit_t"]], y=[p["ep"], p["xp"]],
            mode="lines",
            name=f"Baseline ({len(base_pairs)}T {'+'if base_pnl>=0 else ''}${base_pnl:.0f})",
            line=dict(color=color, width=2.5, dash="dash"),
            showlegend=not legend_shown,
            legendgroup="baseline",
            hovertemplate=(
                f"<b>Baseline</b><br>"
                f"entry: {p['ep']} @ {p['entry_t'].strftime('%H:%M')}<br>"
                f"exit: {p['xp']} @ {p['exit_t'].strftime('%H:%M')}<br>"
                f"reason: {p['reason']}<br>"
                f"pnl: {'+'if p['pnl']>=0 else ''}${p['pnl']:.0f}"
                "<extra></extra>"
            ),
        ))
        fig.add_trace(go.Scatter(
            x=[p["entry_t"], p["exit_t"]],
            y=[p["ep"], p["xp"]],
            mode="markers",
            marker=dict(symbol=["circle-open", "x-thin"], size=[9, 12],
                        color=color, line=dict(width=2, color=color)),
            showlegend=False, legendgroup="baseline", hoverinfo="skip",
        ))
        legend_shown = True

    # ── Current-branch open-position lines ───────────────────────────────────
    curr_pnl = sum(p["pnl"] for p in curr_pairs)
    legend_shown = False
    for p in curr_pairs:
        color = "#4CAF50" if p["pnl"] >= 0 else "#EF5350"
        fig.add_trace(go.Scatter(
            x=[p["entry_t"], p["exit_t"]], y=[p["ep"], p["xp"]],
            mode="lines",
            name=f"Current ({len(curr_pairs)}T {'+'if curr_pnl>=0 else ''}${curr_pnl:.0f})",
            line=dict(color=color, width=2),
            showlegend=not legend_shown,
            legendgroup="current",
            hoverinfo="skip",
        ))
        legend_shown = True

    # ── SMT divergence markers ────────────────────────────────────────────────
    div_events = [e for e in events if e.get("kind") == "smt-div"]
    for side_val, symbol, color in [("bullish", "triangle-up", "#4CAF50"),
                                     ("bearish", "triangle-down", "#EF5350")]:
        grp = [e for e in div_events if e.get("side") == side_val]
        if not grp:
            continue
        hover = [_div_hover(e, all_named) for e in grp]
        fig.add_trace(go.Scatter(
            x=[e["ts"] for e in grp], y=[e["price"] for e in grp],
            mode="markers+text",
            name=f"SMT div {side_val[:4]}",
            marker=dict(symbol=symbol, color=color, size=12, line=dict(width=1.5, color=color)),
            text=[_div_label(e, all_named) for e in grp],
            textposition="top center" if side_val == "bullish" else "bottom center",
            textfont=dict(size=9, color=color),
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover,
        ))

    # ── Exit markers (stopped-out, market-close, end-of-session) ─────────────
    pnl_by_exit = {(p["exit"]["time"], p["exit"]["kind"]): p for p in evt_pairs}
    for kind, style in EXIT_MARKER_STYLE.items():
        group = [e for e in events if e["kind"] == kind]
        if not group:
            continue
        texts, hover, colors = [], [], []
        for e in group:
            pair = pnl_by_exit.get((e["time"], e["kind"]))
            if pair:
                direction_sign = 1 if pair["fill"].get("direction", "up") == "up" else -1
                entry_slip = float(pair["fill"].get("slippage", 0.0))
                exit_slip  = float(e.get("slippage", 0.0))
                efp = pair["fill"]["price"] + direction_sign * entry_slip
                xfp = e["price"] - direction_sign * exit_slip
                pnl_pts = round((xfp - efp) * direction_sign, 2)
                pnl_usd = round(pnl_pts * MNQ_DOLLARS_PER_POINT_PER_CONTRACT * DEFAULT_CONTRACTS, 2)
                sign = "+" if pnl_pts >= 0 else ""
                label = f"{sign}{pnl_pts} ({sign}${pnl_usd:.0f})"
                colors.append("#4CAF50" if pnl_pts >= 0 else "#FF6B6B")
            else:
                label = ""
                colors.append(style["color"])
            texts.append(label)
            parts = [f"<b>{e['kind']}</b>", f"price: {e['price']}", f"time: {e['ts'].strftime('%H:%M')}"]
            if "close_reason" in e:
                parts.append(f"reason: {e['close_reason']}")
            hover.append("<br>".join(parts))
        fig.add_trace(go.Scatter(
            x=[e["ts"] for e in group], y=[e["price"] for e in group],
            mode="markers+text",
            name=kind.replace("-", " "),
            marker=dict(symbol=style["symbol"], color=style["color"],
                        size=style["size"], line=dict(width=2, color=style["color"])),
            text=texts, textposition="top right",
            textfont=dict(size=11, color=colors),
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover,
        ))

    # ── Other event markers (hypothesis, trend-broken, entries) ──────────────
    for kind, style in OTHER_MARKER_STYLE.items():
        group = [e for e in events if e["kind"] == kind]
        if not group:
            continue
        hover = [_other_hover(e, kind) for e in group]
        fig.add_trace(go.Scatter(
            x=[e["ts"] for e in group], y=[e["price"] for e in group],
            mode="markers", name=kind.replace("-", " "),
            marker=dict(symbol=style["symbol"], color=style["color"],
                        size=style["size"], line=dict(width=2, color=style["color"])),
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover,
        ))

    # ── Layout ────────────────────────────────────────────────────────────────
    session_hours = (last_t - first_t).total_seconds() / 3600
    chart_height  = max(700, min(1200, int(600 + session_hours * 40)))
    delta = curr_pnl - base_pnl
    delta_str = f"{'+'if delta>=0 else ''}${delta:.0f}"

    fig.update_layout(
        title=f"MNQ {date} — Baseline (blue dashed) vs Current (solid) | Δ={delta_str}",
        xaxis_title="Time (ET)",
        yaxis_title="Price",
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        height=chart_height,
        legend=dict(orientation="h", yanchor="bottom", y=-0.22),
        margin=dict(b=120, r=80),
        hovermode="x unified",
    )

    out = reg / "chart_comparison.html"
    fig.write_html(str(out), include_plotlyjs="cdn")
    print(f"Chart: {out.resolve()}")
    return out


for date in DATES:
    out = _plot_date(date)
    webbrowser.open(out.resolve().as_uri())
