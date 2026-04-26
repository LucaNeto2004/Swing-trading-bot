"""Snapshot ZEC state: current price, ATR, RSI, pivot status, and what entry/SL/TP
would be IF the swing_pivot long trigger fires on the next break of prev_high.
"""
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from config.deployer import load_all
from config.settings import load_config
from core.data import DataManager
from strategies.whale_swing import WhaleSwingConfig


def main():
    cfg = load_config()
    deployed = load_all()
    c = WhaleSwingConfig.from_json(deployed["ZEC"])
    data = DataManager(cfg)
    # need BTC refresh first so btc_1h_dir gets populated (ZEC requires btc 1h confirm)
    data.refresh("BTC")
    data.refresh("ZEC")
    df5 = data.df_5m["ZEC"]
    bar = df5.iloc[-1]; prev = df5.iloc[-2]
    p2 = df5.iloc[-3]; p3 = df5.iloc[-4]; p4 = df5.iloc[-5]

    price = float(bar['close']); atr = float(bar['atr'])
    rsi = float(bar['rsi'])
    prev_hi = float(prev['high']); prev_lo = float(prev['low'])

    # pivot state
    low_piv = float(p3['low']) < float(p4['low']) and float(p3['low']) < float(p2['low'])
    high_piv = float(p3['high']) > float(p4['high']) and float(p3['high']) > float(p2['high'])

    # 1h filter state
    up_e = bool(data.up_1h["ZEC"][-1]); dn_e = bool(data.dn_1h["ZEC"][-1])
    up_s = bool(data.up_struct_1h["ZEC"][-1]); dn_s = bool(data.dn_struct_1h["ZEC"][-1])
    up_1h = up_e and up_s  # both_agree
    dn_1h = dn_e and dn_s

    # simulate fired entry at prev_high + 1 tick worth of break
    trigger_price = prev_hi  # break above
    entry = trigger_price
    sl = entry - c.sl_atr * atr
    tp1 = entry + c.tp1_atr * atr
    tp2 = entry + c.tp2_atr * atr
    tp3 = entry + c.tp3_atr * atr
    risk_usd_per_unit = entry - sl
    r_mult = (tp1 - entry) / risk_usd_per_unit

    print(f"\n--- ZEC snapshot @ {bar['timestamp']} ---")
    print(f"  price now:       ${price:,.3f}")
    print(f"  prev 5m high:    ${prev_hi:,.3f}   (trigger for long breakout)")
    print(f"  prev 5m low:     ${prev_lo:,.3f}")
    print(f"  ATR (5m):        ${atr:,.3f}")
    print(f"  RSI (5m):        {rsi:.1f}")
    print(f"  3-bar pivot:     LOW={low_piv}   HIGH={high_piv}")
    print(f"  1h filter (both_agree): UP={up_1h}  DN={dn_1h}")
    print(f"  BTC 1h dir:      {data.btc_1h_dir}  (need +1 for ZEC long — require_btc_1h_confirm=True)")

    print(f"\n--- IF long triggers on next break above ${prev_hi:,.3f}: ---")
    print(f"  entry:           ${entry:,.3f}")
    print(f"  stop loss:       ${sl:,.3f}   ({c.sl_atr}× ATR, {(entry-sl)/entry*100:.2f}% below entry)")
    print(f"  TP1 (30% off):   ${tp1:,.3f}   ({c.tp1_atr}× ATR, {(tp1-entry)/entry*100:.2f}% up, R={r_mult:.2f})")
    print(f"  TP2 (30% off):   ${tp2:,.3f}   ({c.tp2_atr}× ATR, {(tp2-entry)/entry*100:.2f}% up)")
    print(f"  TP3 (20% off):   ${tp3:,.3f}   ({c.tp3_atr}× ATR, {(tp3-entry)/entry*100:.2f}% up)")
    print(f"  Last 20% rides structural SL / max_hold {c.max_hold_bars} bars (= {c.max_hold_bars//288}d)")

    move_needed = (prev_hi - price) / price * 100
    print(f"\n  move needed to arm: {move_needed:+.2f}% from here to break ${prev_hi:,.3f}")


if __name__ == "__main__":
    main()
