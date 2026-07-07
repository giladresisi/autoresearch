"""Calibration sweep preparation (GIL-44).

For each systematic cut-point: slice both tickers (temp), run derive_facts.py -> facts.txt,
write context-at-cut.md + poc.md into calibration/cuts/<id>/, and append the ground-truth row
to <global>/gil44_calibration_truth.tsv (OUTSIDE the worktree — sweep agents never see it).

Cut selection: every trading day 2026-05-19 .. 2026-07-01 from the back-adjusted 2026-09
parquets, alternating cut times 09:28 / 13:00 ET, excluding dates already burned as POC cuts
(2026-06-25, 2026-06-30, 2026-07-02).
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

ET = "America/New_York"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(HERE)
MAIN = os.path.expanduser(r"~/projects/auto-co-trader/global/general/main/2026-09")
TRUTH_TSV = os.path.expanduser(r"~/projects/auto-co-trader/global/gil44_calibration_truth.tsv")
DERIVE = os.path.join(WORKTREE, "agent", "derive_facts.py")
TEMPLATE = os.path.join(HERE, "poc_template.md")

DATE_FROM = datetime.date(2026, 5, 19)
DATE_TO = datetime.date(2026, 7, 1)
EXCLUDE = {datetime.date(2026, 6, 23), datetime.date(2026, 6, 25),
           datetime.date(2026, 6, 30), datetime.date(2026, 7, 2)}  # POC-burned dates
CUT_TIMES = [datetime.time(9, 28), datetime.time(13, 0)]  # alternate by day index
LOOKBACK_DAYS = 17
NEXT_MOVE_HORIZON = pd.Timedelta(hours=4)
# Judgment-variance triplicates: every REPLICATE_EVERY-th cut (sorted) gets __r2/__r3 copies
# (same facts, own poc.md/output) so the scorer can measure cross-run judgment variance.
REPLICATE_EVERY = 7
N_REPLICAS = 3


def make_replicates(template: str):
    """Create __r2/__r3 sibling folders for every REPLICATE_EVERY-th cut. Idempotent;
    callable standalone via --replicates-only after a generation run."""
    cuts_root = os.path.join(HERE, "cuts")
    base_ids = sorted(d for d in os.listdir(cuts_root) if "__r" not in d)
    chosen = base_ids[::REPLICATE_EVERY]
    for cid in chosen:
        src = os.path.join(cuts_root, cid)
        for r in range(2, N_REPLICAS + 1):
            rid = f"{cid}__r{r}"
            dst = os.path.join(cuts_root, rid)
            if os.path.exists(dst):
                continue
            os.makedirs(dst)
            for fn in ("facts.txt", "context-at-cut.md"):
                shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
            with open(os.path.join(dst, "poc.md"), "w", encoding="utf-8") as f:
                f.write(template.replace("{CUT_ID}", rid))
        print(f"replicated {cid} -> __r2..__r{N_REPLICAS}")
    print(f"{len(chosen)} cuts triplicated ({len(chosen) * (N_REPLICAS - 1)} extra runs)")


def load_full(tk):
    df = pd.read_parquet(os.path.join(MAIN, f"{tk}_1s.parquet"))
    df.index = pd.to_datetime(df.index)
    df.index = df.index.tz_localize(ET) if df.index.tz is None else df.index.tz_convert(ET)
    return df.sort_index()


def trade_dates(df):
    td = (df.index + pd.Timedelta(hours=7)).date
    counts = pd.Series(td).value_counts()
    return sorted(d for d, n in counts.items() if d.weekday() < 5 and n >= 20000)


def excursions(post, ref):
    if len(post) == 0:
        return None
    up = float(post["High"].max()) - ref
    dn = ref - float(post["Low"].min())
    return {
        "up_exc": round(up, 2), "dn_exc": round(dn, 2),
        "up_ts": post["High"].idxmax().isoformat(), "dn_ts": post["Low"].idxmin().isoformat(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replicates-only", action="store_true",
                    help="only add __rN replicate folders to existing cuts/")
    args = ap.parse_args()
    template = open(TEMPLATE, encoding="utf-8").read()
    if args.replicates_only:
        make_replicates(template)
        return

    cuts_root = os.path.join(HERE, "cuts")
    if os.path.exists(cuts_root):
        shutil.rmtree(cuts_root)  # stale cuts from a previous docs/facts version
    mnq = load_full("MNQ")
    mes = load_full("MES")
    tds = [d for d in trade_dates(mnq)
           if DATE_FROM <= d <= DATE_TO and d not in EXCLUDE]

    rows = []
    for i, d in enumerate(tds):
        cut_t = CUT_TIMES[i % len(CUT_TIMES)]
        cut = pd.Timestamp(datetime.datetime.combine(d, cut_t), tz=ET)
        # require live data just before the cut (skip holidays/early closes)
        pre = mnq[(mnq.index >= cut - pd.Timedelta(minutes=10)) & (mnq.index < cut)]
        if len(pre) < 60:
            print(f"SKIP {d} {cut_t} (no data before cut)")
            continue
        cut_id = f"{d.isoformat()}_{cut_t.strftime('%H%M')}"
        out_dir = os.path.join(HERE, "cuts", cut_id)
        os.makedirs(out_dir, exist_ok=True)

        start = cut - pd.Timedelta(days=LOOKBACK_DAYS)
        ath_mnq = float(mnq[mnq.index < cut]["High"].max())
        ath_mes = float(mes[mes.index < cut]["High"].max())

        tmp = tempfile.mkdtemp(prefix=f"gil44_{cut_id}_")
        try:
            for tk, df in (("MNQ", mnq), ("MES", mes)):
                sl = df[(df.index >= start) & (df.index < cut)]
                sl.to_parquet(os.path.join(tmp, f"{tk}_1s_slice.parquet"))
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            r = subprocess.run(
                [sys.executable, DERIVE, "--dir", tmp,
                 "--ath-mnq", str(ath_mnq), "--ath-mes", str(ath_mes),
                 "--hist-dir", MAIN],
                capture_output=True, timeout=900, env=env)
            if r.returncode != 0:
                print(f"FAIL {cut_id}: derive_facts rc={r.returncode}\n{r.stderr[-500:]}")
                continue
            with open(os.path.join(out_dir, "facts.txt"), "wb") as f:
                f.write(r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        with open(os.path.join(out_dir, "context-at-cut.md"), "w", encoding="utf-8") as f:
            f.write(
                "# Hard-truth context at the end of the fact sheet\n\n"
                f"- MNQ `all_time_high` (back-adjusted, same basis): **{ath_mnq}**\n"
                f"- MES all-time high (same basis): **{ath_mes}**\n"
                "- Position state: **flat** (no open position, no resting orders).\n"
                "- Entry-suppressor counters: `failed_entries = 0`, `cautious_dist_shrinks = 0`.\n"
            )
        with open(os.path.join(out_dir, "poc.md"), "w", encoding="utf-8") as f:
            f.write(template.replace("{CUT_ID}", cut_id))

        # ---- ground truth (evaluator-only, outside worktree) ----
        cut_close = float(pre["Close"].iloc[-1])
        sess_end = pd.Timestamp(datetime.datetime.combine(d, datetime.time(16, 55)), tz=ET)
        post4 = mnq[(mnq.index >= cut) & (mnq.index <= min(cut + NEXT_MOVE_HORIZON, sess_end))]
        postd = mnq[(mnq.index >= cut) & (mnq.index <= sess_end)]
        e4 = excursions(post4, cut_close)
        ed = excursions(postd, cut_close)
        if e4 is None or ed is None or len(postd) < 600:
            # holiday early close (e.g. Juneteenth 13:00): no scoreable post-cut window
            print(f"SKIP {cut_id} (no post-cut window — early close)")
            shutil.rmtree(out_dir, ignore_errors=True)
            continue
        eod = float(postd["Close"].iloc[-1])
        rows.append({
            "cut_id": cut_id, "date": d.isoformat(), "cut_time": cut_t.strftime("%H:%M"),
            "cut_close": cut_close,
            "h4_up_exc": e4["up_exc"], "h4_dn_exc": e4["dn_exc"],
            "h4_up_ts": e4["up_ts"], "h4_dn_ts": e4["dn_ts"],
            "day_up_exc": ed["up_exc"], "day_dn_exc": ed["dn_exc"],
            "eod_close": eod, "eod_delta": round(eod - cut_close, 2) if eod else None,
        })
        print(f"OK {cut_id}  cut_close={cut_close}  h4 +{e4['up_exc']}/-{e4['dn_exc']}  "
              f"eod {round(eod - cut_close, 2) if eod else '?'}")

    pd.DataFrame(rows).to_csv(TRUTH_TSV, sep="\t", index=False)
    print(f"\n{len(rows)} cuts prepared under {os.path.join(HERE, 'cuts')}")
    print(f"ground truth -> {TRUTH_TSV}")
    make_replicates(template)


if __name__ == "__main__":
    main()
