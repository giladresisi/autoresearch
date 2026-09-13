"""Render sampled sessions with the skeleton overlaid -- the artifact for the human gate.

The gate asks a question a table cannot: does move-start land where a trader would say
the move began? That is a visual judgement, so it gets a chart. Deliberately plain --
5m candles as bars, the primary segment drawn as a line from move-start to move-end,
secondary segments dashed, counter segments faint.
"""
from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import matplotlib                                       # noqa: E402
matplotlib.use("Agg")                                   # headless; no display needed
import matplotlib.pyplot as plt                         # noqa: E402

from agent.study.sessions import SessionBars            # noqa: E402
from agent.study.skeleton import session_skeleton       # noqa: E402

_STYLE = {"primary": ("tab:blue", "-", 2.2),
          "secondary": ("tab:green", "--", 1.6),
          "counter": ("tab:gray", ":", 1.0)}


def plot_sessions(dates, out_dir: str) -> "list[str]":
    os.makedirs(out_dir, exist_ok=True)
    bars = SessionBars()
    written = []
    for d in dates:
        f = bars.window_5m("MNQ", d)
        if not len(f):
            continue
        sk = session_skeleton(f, d)

        fig, ax = plt.subplots(figsize=(14, 7))
        for ts, row in f.iterrows():
            ax.plot([ts, ts], [row["Low"], row["High"]], color="black", linewidth=0.8)
            ax.plot([ts, ts], [row["Open"], row["Close"]], color="black", linewidth=3.0)

        # ONE legend entry per role, not per segment. The corpus averages ~28 segments
        # per session, and a 28-entry legend covers a third of the price action -- which
        # defeats the only thing this chart exists for.
        counts, labelled = {}, set()
        for seg in sk.segments:
            counts[seg.role] = counts.get(seg.role, 0) + 1
        for seg in sk.segments:
            color, style, width = _STYLE[seg.role]
            if seg.role in labelled:
                label = None
            elif seg.role == "primary":
                label = f"primary {seg.direction} {seg.extent:.1f}pt"
            else:
                label = f"{seg.role} x{counts[seg.role]}"
            labelled.add(seg.role)
            ax.plot([seg.start_ts, seg.extreme_ts],
                    [seg.start_price, seg.extreme_price],
                    color=color, linestyle=style, linewidth=width, label=label)
            ax.scatter([seg.start_ts], [seg.start_price], color=color, s=45, zorder=5)
            ax.scatter([seg.extreme_ts], [seg.extreme_price], color=color, s=45,
                       marker="X", zorder=5)

        title = (f"{d}  MNQ 09:30-13:00  status={sk.status} dir={sk.direction} "
                 f"censored={sk.censored}  segments={len(sk.segments)}")
        ax.set_title(title)
        if sk.segments:
            ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8,
                      borderaxespad=0.0)
        ax.grid(alpha=0.25)
        fig.autofmt_xdate()
        path = os.path.join(out_dir, f"skeleton_{d}.png")
        fig.savefig(path, dpi=110, bbox_inches="tight")
        plt.close(fig)
        written.append(path)
    return written


if __name__ == "__main__":
    import datetime
    out = sys.argv[1] if len(sys.argv) > 1 else ".agents/session-skeleton/plots"
    ds = [datetime.date.fromisoformat(a) for a in sys.argv[2:]]
    if not ds:
        ds = SessionBars().dates()
    print("\n".join(plot_sessions(ds, out)))
