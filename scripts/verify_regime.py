"""Pull raw HL 1h + 4h candles for the key symbols and print recent bars so we
can verify the filter reads against actual price action."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.data import fetch_candles


def summarize(sym: str, interval: str, bars: int = 20):
    df = fetch_candles(sym, interval, bars + 50)
    if df.empty:
        print(f"{sym} {interval}: empty")
        return
    df = df.tail(bars).reset_index(drop=True)
    closes = df['close'].astype(float)

    # Quick trend diagnostics
    hma_len = 9
    wma1 = closes.rolling(hma_len // 2).apply(
        lambda x: np.average(x, weights=np.arange(1, len(x) + 1)), raw=True)
    wma2 = closes.rolling(hma_len).apply(
        lambda x: np.average(x, weights=np.arange(1, len(x) + 1)), raw=True)
    diff = 2 * wma1 - wma2
    hma = diff.rolling(int(np.sqrt(hma_len))).apply(
        lambda x: np.average(x, weights=np.arange(1, len(x) + 1)), raw=True)
    hma_slope = hma.diff()

    ema21 = closes.ewm(span=21, adjust=False).mean()
    ema50 = closes.ewm(span=50, adjust=False).mean()

    last_close = float(closes.iloc[-1])
    change_5 = (last_close / float(closes.iloc[-6]) - 1) * 100 if len(closes) >= 6 else 0.0
    change_10 = (last_close / float(closes.iloc[-11]) - 1) * 100 if len(closes) >= 11 else 0.0
    change_20 = (last_close / float(closes.iloc[0]) - 1) * 100

    print(f"\n=== {sym} {interval} (last {bars} bars) ===")
    print(f"  last close: {last_close:.5g}")
    print(f"  % chg last 5 bars:  {change_5:+.2f}%")
    print(f"  % chg last 10 bars: {change_10:+.2f}%")
    print(f"  % chg last {bars} bars: {change_20:+.2f}%")
    print(f"  HMA-9: {float(hma.iloc[-1]):.5g}   slope last 3: "
          f"{float(hma_slope.iloc[-3]):+.5f} {float(hma_slope.iloc[-2]):+.5f} {float(hma_slope.iloc[-1]):+.5f}")
    print(f"  EMA21: {float(ema21.iloc[-1]):.5g}   EMA50: {float(ema50.iloc[-1]):.5g}   "
          f"EMA21>EMA50: {float(ema21.iloc[-1]) > float(ema50.iloc[-1])}")
    # Last 10 closes
    print(f"  last 10 closes: " + " ".join(f"{c:.4g}" for c in closes.iloc[-10:].tolist()))


def main():
    for sym in ["BTC", "XRP", "HYPE", "SOL"]:
        summarize(sym, "1h", bars=20)
        summarize(sym, "4h", bars=15)


if __name__ == "__main__":
    main()
