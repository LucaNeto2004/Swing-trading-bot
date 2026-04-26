"""One-shot: how close is each symbol to firing a whale_swing entry right now?

Loads deployed configs, fetches recent 5m candles, computes indicators, and
reports a proximity score per symbol based on entry_type:
 - rsi_bounce : delta between prev RSI and oversold/overbought thresholds
 - bb_touch   : distance (%) from prev low/high to bb_lower/bb_upper
 - ema_bounce : distance (%) of prev low/high from EMA21, plus RSI side
 - swing_pivot: whether a 4-bar pivot is forming and distance to breakout
"""
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from config.deployer import load_all
from config.settings import load_config, INSTRUMENTS
from core.data import DataManager
from strategies.whale_swing import WhaleSwingConfig


def main():
    cfg = load_config()
    deployed = load_all()
    symbols = [s for s in INSTRUMENTS.keys() if s in deployed]
    strategies = {s: WhaleSwingConfig.from_json(deployed[s]) for s in symbols}
    data = DataManager(cfg)
    for s in symbols:
        try:
            data.refresh(s)
        except Exception as e:
            print(f"{s}: refresh failed — {e}")

    print(f"\n{'SYM':<12} {'ET':<12} {'DIR':<5} {'1h':<5} {'TREND':<6} {'SIGNAL NEAR?':<50}")
    print("-" * 100)

    rows = []
    for sym, c in strategies.items():
        df5 = data.df_5m.get(sym)
        if df5 is None or len(df5) < 55:
            continue
        bar = df5.iloc[-1]; prev = df5.iloc[-2]
        price = float(bar['close']); rsi = float(bar['rsi']); r_prev = float(prev['rsi'])
        atr = float(bar['atr'])
        e21 = float(bar['ema_21']); e50 = float(bar['ema_50'])
        bb_lo = float(bar.get('bb_lower', 0) or 0); bb_hi = float(bar.get('bb_upper', 0) or 0)

        # trend-filter snapshot (5m)
        if c.trend_filter == 'ema_cross':
            up_ok, dn_ok = e21 > e50, e21 < e50
        elif c.trend_filter == 'ema_slope':
            slope = float(bar.get('ema_50_slope', 0) or 0)
            up_ok, dn_ok = slope > 0, slope < 0
        else:
            up_ok, dn_ok = True, True
        trend = "UP" if up_ok else ("DN" if dn_ok else "--")

        # 1h direction snapshot
        up1 = data.up_1h.get(sym); dn1 = data.dn_1h.get(sym)
        if up1 is not None and dn1 is not None:
            if bool(up1[-1]): h1 = "UP"
            elif bool(dn1[-1]): h1 = "DN"
            else: h1 = "--"
        else:
            h1 = "?"

        et = c.entry_type
        note = ""
        if et == 'rsi_bounce':
            # prev below oversold, latest crossing up = long fire; distance = (threshold - rsi) for long
            long_dist = c.rsi_oversold - rsi
            short_dist = rsi - c.rsi_overbought
            if r_prev < c.rsi_oversold and rsi < c.rsi_oversold:
                note = f"LONG ARMED | prev_rsi {r_prev:.1f}<{c.rsi_oversold:.0f}, rsi {rsi:.1f} (need >={c.rsi_oversold:.0f})"
            elif r_prev > c.rsi_overbought and rsi > c.rsi_overbought:
                note = f"SHORT ARMED | prev_rsi {r_prev:.1f}>{c.rsi_overbought:.0f}, rsi {rsi:.1f} (need <={c.rsi_overbought:.0f})"
            else:
                note = f"rsi {rsi:.1f}  (L thr {c.rsi_oversold:.0f}, S thr {c.rsi_overbought:.0f})"
        elif et == 'bb_touch':
            dl = (float(prev['low']) - bb_lo) / price * 100 if bb_lo else 99
            dh = (bb_hi - float(prev['high'])) / price * 100 if bb_hi else 99
            note = f"prev_low->bb_lower {dl:+.2f}%  prev_high->bb_upper {dh:+.2f}%"
        elif et == 'ema_bounce':
            dl = (float(prev['low']) - e21) / price * 100
            dh = (float(prev['high']) - e21) / price * 100
            note = f"prev_low vs EMA21 {dl:+.2f}%  prev_high vs EMA21 {dh:+.2f}%  rsi_prev {r_prev:.1f}"
        elif et == 'swing_pivot':
            p2 = df5.iloc[-3]; p3 = df5.iloc[-4]; p4 = df5.iloc[-5]
            low_pivot = float(p3['low']) < float(p4['low']) and float(p3['low']) < float(p2['low'])
            high_pivot = float(p3['high']) > float(p4['high']) and float(p3['high']) > float(p2['high'])
            br_up = (float(prev['high']) - price) / price * 100
            br_dn = (price - float(prev['low'])) / price * 100
            marks = []
            if low_pivot:  marks.append(f"LOW_PIV+break{br_up:+.2f}%")
            if high_pivot: marks.append(f"HIGH_PIV+break{br_dn:+.2f}%")
            note = " | ".join(marks) if marks else "no 3-bar pivot"
        rows.append((sym, et, c.direction[:5], h1, trend, note))

    # sort so ARMED rows show at top
    rows.sort(key=lambda r: 0 if 'ARMED' in r[5] else 1)
    for r in rows:
        print(f"{r[0]:<12} {r[1]:<12} {r[2]:<5} {r[3]:<5} {r[4]:<6} {r[5]}")


if __name__ == "__main__":
    main()
