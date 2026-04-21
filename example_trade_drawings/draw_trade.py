# draw_trade.py
# Renders a single SMT strategy trade as a candlestick chart.
#
# USAGE (standalone):
#   python draw_trade.py                          # draws built-in sample trade, saves chart
#   python draw_trade.py --date 2025-09-01        # draws the trade that fired on that session date
#   python draw_trade.py --date 2025-09-01 --show # also opens the chart window
#
# USAGE (from another script / agent):
#   from draw_trade import draw_trade, find_session_trade
#   trade, mnq_session = find_session_trade("2025-09-01")
#   draw_trade(trade, mnq_session, output_path="my_trade.png", show=True)
#
# draw_trade() INPUT CONTRACT
# ---------------------------
# trade_info: dict with keys:
#   direction    : "long" | "short"
#   entry_time   : pd.Timestamp (tz-aware ET)
#   entry_price  : float
#   take_profit  : float
#   stop_price   : float
#   tdo          : float
#   exit_time    : pd.Timestamp (tz-aware ET)
#   exit_price   : float
#   exit_action  : str  e.g. "exit_tp", "exit_stop", "exit_time"
#   divergence_bar: int  (0-indexed position in the session bars)
#   entry_bar    : int
#   smt_sweep_pts: float
#   smt_miss_pts : float
#
# mnq_bars: pd.DataFrame with DatetimeIndex (ET) and columns Open/High/Low/Close
#
# output_path: str | None  — path to save PNG; None = don't save
# show: bool               — whether to display the chart interactively
# context_bars_before: int — bars to show before divergence bar (default 10)
# context_bars_after : int — bars to show after exit bar (default 5)

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Candlestick rendering ────────────────────────────────────────────────────

def _draw_candles(ax: plt.Axes, bars: pd.DataFrame, x_offset: int = 0) -> None:
    """Draw OHLC candles on ax. bars must have Open/High/Low/Close columns."""
    width = 0.6
    for i, (ts, row) in enumerate(bars.iterrows()):
        x = i + x_offset
        is_bull = row.Close >= row.Open
        color = "#26a69a" if is_bull else "#ef5350"  # teal / red
        body_bot = min(row.Open, row.Close)
        body_top = max(row.Open, row.Close)
        body_h = max(body_top - body_bot, 0.01)

        # Body
        ax.bar(x, body_h, bottom=body_bot, width=width,
               color=color, edgecolor=color, linewidth=0.5, zorder=3)
        # Wick
        ax.plot([x, x], [row.Low, row.High], color=color, linewidth=0.8, zorder=2)


# ── Main drawing function ────────────────────────────────────────────────────

def draw_trade(
    trade_info: dict,
    mnq_bars: pd.DataFrame,
    output_path: str | Path | None = "trade_chart.png",
    show: bool = False,
    context_bars_before: int = 10,
    context_bars_after: int = 5,
) -> Path | None:
    """Draw a candlestick chart for one SMT trade.

    Returns the saved file path, or None if output_path is None.
    """
    session_index = list(mnq_bars.index)

    div_bar   = trade_info["divergence_bar"]
    entry_bar = trade_info["entry_bar"]
    exit_ts   = trade_info["exit_time"]
    try:
        exit_bar = session_index.index(exit_ts)
    except ValueError:
        # Nearest match
        exit_bar = min(range(len(session_index)),
                       key=lambda i: abs(session_index[i] - exit_ts))

    # Window of bars to show
    start_bar = max(0, div_bar - context_bars_before)
    end_bar   = min(len(session_index) - 1, exit_bar + context_bars_after)
    window    = mnq_bars.iloc[start_bar:end_bar + 1]

    # Map global bar indices → x-positions in the window
    def to_x(bar_idx: int) -> int:
        return bar_idx - start_bar

    # ── Figure ───────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444466")
    ax.tick_params(colors="#aaaacc")
    ax.yaxis.label.set_color("#aaaacc")
    ax.xaxis.label.set_color("#aaaacc")

    _draw_candles(ax, window)

    # ── Horizontal levels ────────────────────────────────────────────────────
    direction   = trade_info["direction"]
    entry_p     = trade_info["entry_price"]
    tp_p        = trade_info["take_profit"]
    stop_p      = trade_info.get("original_stop", trade_info["stop_price"])
    trail_stop  = trade_info.get("trail_stop")
    tdo_p       = trade_info["tdo"]
    x_end       = len(window) - 1

    # TDO (dashed grey)
    ax.axhline(tdo_p, color="#888899", linestyle=":", linewidth=1.0, zorder=1)
    ax.text(x_end + 0.3, tdo_p, f" TDO {tdo_p:.2f}", va="center",
            color="#888899", fontsize=7.5)

    # Entry (white dashed)
    ax.axhline(entry_p, color="#ffffff", linestyle="--", linewidth=1.0, zorder=1)
    ax.text(x_end + 0.3, entry_p, f" Entry {entry_p:.2f}", va="center",
            color="#ffffff", fontsize=7.5)

    # TP (green dashed)
    ax.axhline(tp_p, color="#26a69a", linestyle="--", linewidth=1.0, zorder=1)
    ax.text(x_end + 0.3, tp_p, f" TP {tp_p:.2f}", va="center",
            color="#26a69a", fontsize=7.5)

    # Original stop (red dashed)
    ax.axhline(stop_p, color="#ef5350", linestyle="--", linewidth=1.0, zorder=1)
    ax.text(x_end + 0.3, stop_p, f" Stop {stop_p:.2f}", va="center",
            color="#ef5350", fontsize=7.5)

    # Trailing stop (orange dashed) — shown only when trail was active
    if trail_stop is not None:
        ax.axhline(trail_stop, color="#ffa726", linestyle="--", linewidth=1.0, zorder=1)
        ax.text(x_end + 0.3, trail_stop, f" Trail {trail_stop:.2f}", va="center",
                color="#ffa726", fontsize=7.5)

    # ── Vertical markers ─────────────────────────────────────────────────────
    x_div   = to_x(div_bar)
    x_entry = to_x(entry_bar)
    x_exit  = to_x(exit_bar)

    # Divergence bar — yellow vertical band
    ax.axvspan(x_div - 0.4, x_div + 0.4, alpha=0.18, color="#ffeb3b", zorder=0)
    ax.text(x_div, ax.get_ylim()[1], "DIV", ha="center", va="top",
            color="#ffeb3b", fontsize=8, fontweight="bold")

    # Entry bar — white triangle below/above
    entry_low  = window.iloc[x_entry]["Low"]
    entry_high = window.iloc[x_entry]["High"]
    if direction == "long":
        ax.annotate("▲ ENTRY", xy=(x_entry, entry_low), xytext=(x_entry, entry_low - 2),
                    ha="center", color="#ffffff", fontsize=8, fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#ffffff", lw=1.2))
    else:
        ax.annotate("▼ ENTRY", xy=(x_entry, entry_high), xytext=(x_entry, entry_high + 2),
                    ha="center", color="#ffffff", fontsize=8, fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#ffffff", lw=1.2))

    # Exit bar — coloured X marker
    exit_action = trade_info.get("exit_action", "")
    exit_price  = trade_info.get("exit_price", entry_p)
    exit_color  = "#26a69a" if exit_action == "exit_tp" else "#ef5350"
    ax.scatter(x_exit, exit_price, marker="X", s=120, color=exit_color,
               zorder=5, linewidths=0.8)
    ax.text(x_exit, exit_price, f"  EXIT\n  {exit_price:.2f}", va="center",
            color=exit_color, fontsize=7.5)

    # ── Shaded position region ───────────────────────────────────────────────
    ax.axvspan(x_entry, x_exit, alpha=0.06,
               color="#26a69a" if exit_action == "exit_tp" else "#ef5350", zorder=0)

    # ── X-axis: bar times ────────────────────────────────────────────────────
    tick_step = max(1, len(window) // 10)
    tick_xs   = list(range(0, len(window), tick_step))
    tick_lbls = [window.index[i].strftime("%H:%M") for i in tick_xs]
    ax.set_xticks(tick_xs)
    ax.set_xticklabels(tick_lbls, fontsize=8, color="#aaaacc")
    ax.set_xlim(-1, len(window) + 3)

    # ── Title & legend ───────────────────────────────────────────────────────
    date_str     = trade_info["entry_time"].strftime("%Y-%m-%d")
    entry_t_str  = trade_info["entry_time"].strftime("%H:%M")
    exit_t_str   = trade_info["exit_time"].strftime("%H:%M")
    bars_held    = exit_bar - entry_bar
    pnl_pts      = (exit_price - entry_p) * (-1 if direction == "short" else 1)
    pnl_usd      = pnl_pts * 2 * trade_info.get("contracts", 1)
    exit_lbl     = {"exit_tp": "TP ✓", "exit_stop": "STOP ✗", "exit_time": "TIME"}.get(
        exit_action, exit_action)

    ax.set_title(
        f"SMT Divergence  ·  MNQ1!  ·  {date_str}  ·  "
        f"{direction.upper()}  ·  Entry {entry_t_str} → Exit {exit_t_str}  "
        f"({bars_held} bars)  ·  {exit_lbl}  ·  "
        f"{'%+.2f' % pnl_pts} pts  /  {'%+.0f' % pnl_usd} USD",
        color="#e0e0ff", fontsize=9.5, pad=10,
    )

    legend_patches = [
        mpatches.Patch(color="#ffeb3b", alpha=0.5, label="Divergence bar"),
        mpatches.Patch(color="#ffffff", alpha=0.6, label="Entry"),
        mpatches.Patch(color="#26a69a", label="Take-profit / TDO"),
        mpatches.Patch(color="#ef5350", label="Stop"),
        mpatches.Patch(color="#888899", alpha=0.6, label="TDO level"),
    ]
    if trail_stop is not None:
        legend_patches.append(mpatches.Patch(color="#ffa726", label="Trailing stop (post-TP)"))
    ax.legend(handles=legend_patches, loc="upper left", fontsize=7.5,
              facecolor="#1a1a2e", edgecolor="#444466", labelcolor="#ccccee")

    ax.set_ylabel("MNQ price", color="#aaaacc", fontsize=9)
    fig.tight_layout()

    saved = None
    if output_path is not None:
        out = Path(output_path)
        fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        saved = out
        print(f"Chart saved -> {out}")

    if show:
        plt.show()

    plt.close(fig)
    return saved


# ── Trade finder ─────────────────────────────────────────────────────────────

def find_session_trade(session_date: str) -> tuple[dict, pd.DataFrame] | tuple[None, None]:
    """Run strategy on session_date and return (trade_info, mnq_session_bars).

    Returns (None, None) if no trade fired that session.
    trade_info is augmented with exit_time, exit_price, exit_action.
    """
    import strategy_smt as s

    _root = Path(__file__).parent.parent
    mnq = pd.read_parquet(_root / "data" / "historical" / "MNQ_1m.parquet")
    mes = pd.read_parquet(_root / "data" / "historical" / "MES_1m.parquet")

    day = pd.Timestamp(session_date, tz="America/New_York")
    session_start = day.replace(hour=9, minute=0)
    session_end   = day.replace(hour=13, minute=30)
    mnq_s = mnq[session_start:session_end]
    mes_s = mes[session_start:session_end]

    if len(mnq_s) < 30:
        return None, None

    tdo = s.compute_tdo(mnq_s, day.date())
    if tdo is None:
        return None, None

    signal = s.screen_session(mnq_s, mes_s, tdo)
    if signal is None:
        return None, None

    # Simulate position management to find exit
    pos = {
        "direction":   signal["direction"],
        "entry_price": signal["entry_price"],
        "entry_time":  signal["entry_time"],
        "stop_price":  signal["stop_price"],
        "take_profit": signal["take_profit"],
        "tdo":         signal["tdo"],
        "contracts":   1,
        "trailing":    False,
    }
    session_index = list(mnq_s.index)
    entry_idx = session_index.index(signal["entry_time"])

    exit_action = "exit_time"
    exit_ts     = session_index[-1]
    exit_bar    = mnq_s.loc[exit_ts]

    for ts in session_index[entry_idx + 1:]:
        bar = mnq_s.loc[ts]
        action = s.manage_position(pos, bar)
        if action != "hold":
            exit_action = action
            exit_ts     = ts
            exit_bar    = bar
            break

    # Resolve exit price — read stop_price from pos dict so trailing-stop mutations
    # are captured (manage_position mutates pos["stop_price"] in-place when trailing).
    direction = signal["direction"]
    tp_p      = signal["take_profit"]
    if exit_action == "exit_tp":
        exit_price = tp_p
    elif exit_action == "exit_stop":
        exit_price = pos["stop_price"]   # may be trailing stop, not original stop
    else:
        exit_price = exit_bar.Close

    trade_info = {
        **signal,
        "exit_time":      exit_ts,
        "exit_price":     exit_price,
        "exit_action":    exit_action,
        "contracts":      1,
        "entry_bar":      entry_idx,
        # stop_price in pos may have been mutated by trailing logic;
        # keep the original stop for the chart's initial stop line.
        "original_stop":  signal["stop_price"],
        "trail_stop":     pos["stop_price"] if pos["stop_price"] != signal["stop_price"] else None,
    }
    return trade_info, mnq_s


# ── CLI entry point ───────────────────────────────────────────────────────────

_SAMPLE_DATE = "2025-09-01"  # best default: 27-bar short trade


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw an SMT strategy trade chart")
    parser.add_argument("--date", default=_SAMPLE_DATE,
                        help="Session date YYYY-MM-DD (default: %(default)s)")
    parser.add_argument("--out", default="trade_chart.png",
                        help="Output PNG path (default: %(default)s)")
    parser.add_argument("--show", action="store_true",
                        help="Open the chart window interactively")
    args = parser.parse_args()

    trade, bars = find_session_trade(args.date)
    if trade is None:
        print(f"No trade fired on {args.date}. Try another date.")
        sys.exit(1)

    draw_trade(trade, bars, output_path=args.out, show=args.show)


if __name__ == "__main__":
    main()
