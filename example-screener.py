#!/usr/bin/env python3
"""
screener.py — v2 (yfinance, dynamic universe)
------------------------------------
Refined from v1 post-mortems (IREN 2026-03-10, SLB 2026-03-11).
See screener_versions/v1/summary.md for full rule reasoning.
Universe: S&P 500 (Wikipedia) + Russell 1000 (iShares IWB) — fetched fresh each run.

Rules (in order applied):
  R1  — Exclude stocks with < 150 days history (SMA150 NaN)
  1   — Price above SMA150
  2   — Last 3 up days (close > prior close)
  3   — Last 2 days volume >= 0.85x MA30
  4   — CCI(20) < -50, rising 2 consecutive days
  5   — Price >= 8% below 7-day local high AND all-time high
  R4  — Entry candle upper wick < body
  R3  — Bounce not stalling at ceiling (last 3 highs cluster, all closes below)
  R2  — Stop anchors to significant pivot low (not noise zone)
  R6  — Stop zone <= 10 candle touches in last 90 days
  1.5x— Price >= 1.5x ATR14 above stop
  R5  — Nearest confirmed resistance >= 2x ATR above entry
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json, warnings
from datetime import datetime

warnings.filterwarnings('ignore')


def fetch_universe():
    """
    Fetch current S&P 500 (Wikipedia) + Russell 1000 (iShares IWB holdings).
    Falls back to a hardcoded minimal list if both sources fail.
    Returns a deduplicated list of ticker strings.
    """
    import requests, io
    from bs4 import BeautifulSoup

    headers = {'User-Agent': 'Mozilla/5.0'}
    tickers = []

    # ── S&P 500 from Wikipedia ──
    try:
        r = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                         headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        table = soup.find('table', {'id': 'constituents'})
        rows = table.find_all('tr')[1:]
        sp500 = [row.find_all('td')[0].text.strip().replace('.', '-') for row in rows]
        tickers += sp500
        print(f"  S&P 500:      {len(sp500)} tickers")
    except Exception as e:
        print(f"  S&P 500 fetch failed: {e}")

    # ── Russell 1000 from iShares IWB CSV ──
    try:
        url = ('https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/'
               '1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund')
        r = requests.get(url, headers=headers, timeout=15)
        lines = r.text.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith('Ticker'))
        df = pd.read_csv(io.StringIO('\n'.join(lines[start:])))
        df = df[df['Asset Class'] == 'Equity']
        r1000 = df['Ticker'].dropna().str.strip().tolist()
        r1000 = [t for t in r1000 if t.replace('-','').isalpha() and len(t) <= 5]
        tickers += r1000
        print(f"  Russell 1000: {len(r1000)} tickers")
    except Exception as e:
        print(f"  Russell 1000 fetch failed: {e}")

    # Deduplicate, preserve order
    seen, unique = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    if not unique:
        print("  WARNING: all sources failed, using fallback list")
        unique = ['AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','JPM','V','UNH']

    return unique



# ── Indicators ────────────────────────────────────────────────────────────────

def calc_cci(df, p=20):
    tp  = (df['High'] + df['Low'] + df['Close']) / 3
    sma = tp.rolling(p).mean()
    md  = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - np.mean(x))))
    return (tp - sma) / (0.015 * md)

def calc_atr14(df):
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low']  - df['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean()


# ── R2 + R6: Pivot-low-anchored stop ─────────────────────────────────────────

def find_pivot_lows(df, bars=4):
    pivots = []
    for i in range(bars, len(df) - bars):
        l = float(df['Low'].iloc[i])
        if all(l <= float(df['Low'].iloc[i+k]) for k in range(-bars, bars+1) if k != 0):
            pivots.append((i, l))
    return pivots

def zone_touch_count(df, level, lookback=90, band_pct=0.015):
    window = df.iloc[-lookback:]
    lo, hi = level * (1 - band_pct), level * (1 + band_pct)
    return int(sum(
        1 for i in range(len(window))
        if float(window['Low'].iloc[i]) <= hi and float(window['High'].iloc[i]) >= lo
    ))

def find_stop_price(df, close_now, atr):
    if len(df) < 60:
        return None
    window = df.iloc[-90:].copy().reset_index(drop=True)
    pivots = find_pivot_lows(window, bars=4)
    if not pivots:
        return None
    # Sort by price descending — nearest to current price first
    candidates = sorted(
        [(i, p) for i, p in pivots if close_now - p >= 1.5 * atr],
        key=lambda x: x[1], reverse=True
    )
    for _, pivot_price in candidates:
        # R6: reject if stop zone is high-density noise
        if zone_touch_count(df, pivot_price, lookback=90) > 10:
            continue
        # R2: confirm >= 1 prior historical touch (excl. last 5 bars)
        prior = df.iloc[-90:-5]
        lo, hi = pivot_price * 0.985, pivot_price * 1.015
        prior_touches = sum(
            1 for i in range(len(prior))
            if float(prior['Low'].iloc[i]) <= hi and float(prior['High'].iloc[i]) >= lo
        )
        if prior_touches < 1:
            continue
        stop = pivot_price - 0.3 * atr
        if close_now - stop < 1.5 * atr:
            continue
        return round(stop, 2)
    return None


# ── R3: Bounce stalling at ceiling ───────────────────────────────────────────

def is_stalling_at_ceiling(df, band_pct=0.03):
    last3_highs  = [float(df['High'].iloc[i])  for i in [-3, -2, -1]]
    last3_closes = [float(df['Close'].iloc[i]) for i in [-3, -2, -1]]
    h_max, h_min = max(last3_highs), min(last3_highs)
    return (h_max - h_min) / h_min <= band_pct and all(c < h_min for c in last3_closes)


# ── R5: Nearest pivot-high resistance >= 2x ATR ───────────────────────────────

def nearest_resistance_atr(df, close_now, atr, lookback=90):
    window = df.iloc[-lookback:].copy().reset_index(drop=True)
    bars, pivot_highs = 4, []
    for i in range(bars, len(window) - bars):
        h = float(window['High'].iloc[i])
        if h > close_now and all(
            h >= float(window['High'].iloc[i+k])
            for k in range(-bars, bars+1) if k != 0
        ):
            pivot_highs.append(h)
    if not pivot_highs:
        return None
    return (min(pivot_highs) - close_now) / atr


# ── Main screener ─────────────────────────────────────────────────────────────

def run_screener():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Fetching ticker universe...")
    TICKERS = fetch_universe()
    print(f"  Universe total: {len(TICKERS)} tickers")
    print(f"Downloading {len(TICKERS)} tickers via yfinance...")
    raw = yf.download(
        TICKERS, period="2y", interval="1d",
        group_by="ticker", auto_adjust=True,
        progress=False, threads=True
    )
    print("Download complete. Screening...\n")

    matches = []
    log_lines = []

    def log(msg):
        log_lines.append(msg)

    log(f"Screener v2 (yfinance) — {datetime.now().strftime('%Y-%m-%d %H:%M')} — {len(TICKERS)} tickers\n")

    for ticker in TICKERS:
        try:
            df = raw[ticker].dropna() if len(TICKERS) > 1 else raw.dropna()
            if len(df) < 40:
                continue
            df = df.copy()
            df['VM30']   = df['Volume'].rolling(30).mean()
            df['SMA150'] = df['Close'].rolling(150).mean()
            df['CCI']    = calc_cci(df)
            df['ATR14']  = calc_atr14(df)

            close_now = float(df['Close'].iloc[-1])
            atr       = float(df['ATR14'].iloc[-1])
            vm30      = float(df['VM30'].iloc[-1])

            if pd.isna(atr) or pd.isna(vm30) or vm30 == 0:
                continue

            # R1 — SMA150 must be calculable
            sma150 = float(df['SMA150'].iloc[-1])
            if pd.isna(sma150):
                log(f"  FAIL {ticker}: <150 days history (R1)")
                continue

            # 1 — Price above SMA150
            if close_now <= sma150:
                log(f"  FAIL {ticker}: below SMA150 ({sma150:.2f})")
                continue

            # 2 — Last 3 up days: each close > prior day's close
            if not all(float(df['Close'].iloc[i]) > float(df['Close'].iloc[i-1]) for i in [-1, -2, -3]):
                log(f"  FAIL {ticker}: not 3 up days")
                continue

            # 3 — Last 2 days volume >= 0.85x MA30
            vr1 = float(df['Volume'].iloc[-1]) / vm30
            vr2 = float(df['Volume'].iloc[-2]) / vm30
            if not (vr1 >= 0.85 and vr2 >= 0.85):
                log(f"  FAIL {ticker}: vol {vr1:.2f}/{vr2:.2f}x")
                continue

            # 4 — CCI(20) < -50, rising 2 consecutive days
            c0 = float(df['CCI'].iloc[-1])
            c1 = float(df['CCI'].iloc[-2])
            c2 = float(df['CCI'].iloc[-3])
            if pd.isna(c0) or not (c0 < -50 and c0 > c1 > c2):
                log(f"  FAIL {ticker}: CCI {c0:.1f}")
                continue

            # 5 — Price >= 8% below 7-day local high AND ATH
            local_high = float(df['High'].iloc[-8:-1].max())
            ath        = float(df['High'].max())
            pct_local  = (local_high - close_now) / local_high
            pct_ath    = (ath - close_now) / ath
            if not (pct_local >= 0.08 and pct_ath >= 0.08):
                log(f"  FAIL {ticker}: -{pct_local*100:.1f}% local / -{pct_ath*100:.1f}% ATH")
                continue

            # R4 — Entry candle upper wick < body
            last_c = float(df['Close'].iloc[-1])
            last_o = float(df['Open'].iloc[-1])
            last_h = float(df['High'].iloc[-1])
            body       = abs(last_c - last_o)
            upper_wick = last_h - max(last_c, last_o)
            if body == 0 or upper_wick >= body:
                log(f"  FAIL {ticker}: upper wick {upper_wick:.2f} >= body {body:.2f} (R4)")
                continue

            # R3 — Bounce not stalling at ceiling
            if is_stalling_at_ceiling(df):
                log(f"  FAIL {ticker}: stalling at ceiling (R3)")
                continue

            # R2 + R6 — Pivot-low-anchored stop
            stop = find_stop_price(df, close_now, atr)
            if stop is None:
                log(f"  FAIL {ticker}: no valid pivot-low stop (R2/R6)")
                continue

            # 1.5x ATR buffer to stop
            if close_now - stop < 1.5 * atr:
                log(f"  FAIL {ticker}: insufficient ATR buffer to stop")
                continue

            # R5 — Nearest resistance >= 2x ATR
            res_atr = nearest_resistance_atr(df, close_now, atr)
            if res_atr is not None and res_atr < 2.0:
                log(f"  FAIL {ticker}: nearest resistance {res_atr:.2f}x ATR (R5)")
                continue

            risk     = round(close_now - stop, 2)
            risk_pct = round((risk / close_now) * 100, 1)
            atr_to_stop = round(risk / atr, 2)

            matches.append({
                'ticker':       ticker,
                'close':        round(close_now, 2),
                'sma150':       round(sma150, 2),
                'pct_above_sma150': round((close_now / sma150 - 1) * 100, 1),
                'cci':          round(c0, 1),
                'cci_d1':       round(c1, 1),
                'cci_d2':       round(c2, 1),
                'vol_r1':       round(vr1, 2),
                'vol_r2':       round(vr2, 2),
                'pct_local':    round(pct_local * 100, 1),
                'pct_ath':      round(pct_ath * 100, 1),
                'local_high':   round(local_high, 2),
                'ath':          round(ath, 2),
                'atr14':        round(atr, 2),
                'upper_wick':   round(upper_wick, 2),
                'body':         round(body, 2),
                'stop':         stop,
                'risk':         risk,
                'risk_pct':     risk_pct,
                'atr_to_stop':  atr_to_stop,
                'res_atr':      round(res_atr, 2) if res_atr else None,
            })
            log(
                f"  MATCH {ticker} | ${close_now:.2f} | SMA150:{sma150:.2f} (+{(close_now/sma150-1)*100:.1f}%) | "
                f"CCI:{c0:.1f}↑{c1:.1f}↑{c2:.1f} | Vol:{vr1:.2f}x/{vr2:.2f}x | "
                f"-{pct_local*100:.1f}%/-{pct_ath*100:.1f}%ATH | "
                f"Stop:${stop} ({atr_to_stop}xATR) | Res:{res_atr:.2f}xATR"
            )

        except Exception as e:
            log(f"  ERR {ticker}: {e}")
            continue

    log(f"\nDONE: {len(matches)} match(es) from {len(TICKERS)} tickers\n")

    # Write log
    with open('/app/screener.log', 'w') as f:
        f.write('\n'.join(log_lines))

    # Write results
    with open('/app/screen_results.json', 'w') as f:
        json.dump({"timestamp": datetime.now().isoformat(), "matches": matches}, f, indent=2)

    return matches, len(TICKERS)


if __name__ == "__main__":
    matches, n_tickers = run_screener()

    print(f"\n{'='*65}")
    print(f"RESULTS — {len(matches)} match(es) from {n_tickers} tickers")
    print(f"{'='*65}\n")

    for m in matches:
        res_str = f"{m['res_atr']}x ATR" if m['res_atr'] else "n/a"
        print(f"✅ {m['ticker']} | ${m['close']}  (SMA150: {m['sma150']}, +{m['pct_above_sma150']}%)")
        print(f"   CCI: {m['cci']} ↑ {m['cci_d1']} ↑ {m['cci_d2']}")
        print(f"   Vol: {m['vol_r1']}x / {m['vol_r2']}x MA30")
        print(f"   -{m['pct_local']}% local high  |  -{m['pct_ath']}% ATH")
        print(f"   Candle: body={m['body']}  upper_wick={m['upper_wick']}")
        print(f"   🛑 Stop: ${m['stop']}  |  Risk: ${m['risk']} ({m['risk_pct']}%)  |  {m['atr_to_stop']}x ATR14")
        print(f"   📈 Nearest resistance: {res_str} above entry")
        print()

    if not matches:
        print("No matches today.")
