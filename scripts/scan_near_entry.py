"""One-shot scan: for each symbol, report how close the bot is to an entry.

Uses the live bot's DataManager + strategy configs so the output mirrors what
the bot itself sees. Prints one line per symbol with the gates that are PASS
or FAIL and the distance to trigger for the configured entry type.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import load_config, is_tradeable_now
from config.deployer import load_all
from core.data import DataManager
from strategies.whale_swing import WhaleSwingConfig


def latest(arr):
    return bool(arr[-1]) if arr is not None and len(arr) else True


def main():
    cfg = load_config()
    dm = DataManager(cfg)
    deployed = load_all()

    # Refresh BTC first so btc_1h_dir is populated for symbols that need it.
    symbols = list(cfg.instruments.keys())
    if "BTC" in symbols:
        symbols.remove("BTC")
        symbols = ["BTC"] + symbols

    rows = []
    for sym in symbols:
        dep = deployed.get(sym)
        if not dep:
            rows.append((sym, "NO CFG", ""))
            continue
        wcfg = WhaleSwingConfig.from_json(dep)
        try:
            dm.refresh(sym)
        except Exception as e:
            rows.append((sym, "FETCH FAIL", str(e)[:60]))
            continue
        df = dm.df_5m.get(sym)
        if df is None or df.empty or len(df) < 55:
            rows.append((sym, "DATA SHORT", f"len={0 if df is None else len(df)}"))
            continue

        bar = df.iloc[-1]; prev = df.iloc[-2]
        price = float(bar['close']); hi = float(bar['high']); lo = float(bar['low'])
        atr = float(bar['atr']); rsi = float(bar['rsi']); r_prev = float(prev['rsi'])
        e21 = float(bar['ema_21']); e50 = float(bar['ema_50'])
        slope = float(bar.get('ema_50_slope', 0) or 0)
        e200 = float(bar.get('ema_200', 0) or 0)
        bb_l = float(bar.get('bb_lower', 0) or 0); bb_u = float(bar.get('bb_upper', 0) or 0)

        # 5m trend
        if wcfg.trend_filter == 'ema_cross':
            up5, dn5 = e21 > e50, e21 < e50
        elif wcfg.trend_filter == 'ema_slope':
            up5, dn5 = slope > 0, slope < 0
        elif wcfg.trend_filter == 'ema200':
            up5 = price > e200 if e200 > 0 else True
            dn5 = price < e200 if e200 > 0 else True
        else:
            up5, dn5 = True, True

        # 1h filter
        fv = wcfg.trend_filter_1h
        if fv == "structure":
            up1, dn1 = latest(dm.up_struct_1h.get(sym)), latest(dm.dn_struct_1h.get(sym))
        elif fv == "both_agree":
            up1 = latest(dm.up_1h.get(sym)) and latest(dm.up_struct_1h.get(sym))
            dn1 = latest(dm.dn_1h.get(sym)) and latest(dm.dn_struct_1h.get(sym))
        elif fv == "hma_slope":
            up1, dn1 = latest(dm.up_hma_1h.get(sym)), latest(dm.dn_hma_1h.get(sym))
        elif fv == "sjm":
            up1, dn1 = latest(dm.up_sjm_1h.get(sym)), latest(dm.dn_sjm_1h.get(sym))
        else:
            up1, dn1 = latest(dm.up_1h.get(sym)), latest(dm.dn_1h.get(sym))

        up_ok = up5 and (up1 if wcfg.use_1h_filter else True)
        dn_ok = dn5 and (dn1 if wcfg.use_1h_filter else True)

        # 4h filter
        if wcfg.require_4h_agreement:
            up_ok = up_ok and latest(dm.up_struct_4h.get(sym))
            dn_ok = dn_ok and latest(dm.dn_struct_4h.get(sym))

        # Direction cap
        if wcfg.direction == 'long_only': dn_ok = False
        if wcfg.direction == 'short_only': up_ok = False

        # BTC 1h confirm
        btc_block = ""
        if wcfg.require_btc_1h_confirm and sym != "BTC":
            if dm.btc_1h_dir == 1:
                dn_ok = False
                btc_block = "btc=+1"
            elif dm.btc_1h_dir == -1:
                up_ok = False
                btc_block = "btc=-1"
            else:
                up_ok = False; dn_ok = False
                btc_block = "btc=0"

        # Hours gate
        tradeable, why = is_tradeable_now(sym)
        if not tradeable:
            rows.append((sym, f"HOURS CLOSED ({why})", ""))
            continue

        # Per entry_type: how close is the price to a trigger?
        et = wcfg.entry_type
        long_dist = short_dist = None
        trig_long = trig_short = False

        if et == 'rsi_bounce':
            trig_long = up_ok and r_prev < wcfg.rsi_oversold and rsi >= wcfg.rsi_oversold
            trig_short = dn_ok and r_prev > wcfg.rsi_overbought and rsi <= wcfg.rsi_overbought
            long_dist = f"rsi={rsi:.1f} (need prev<{wcfg.rsi_oversold}→cross up) prev={r_prev:.1f}"
            short_dist = f"rsi={rsi:.1f} (need prev>{wcfg.rsi_overbought}→cross dn) prev={r_prev:.1f}"
        elif et == 'bb_touch':
            trig_long = up_ok and float(prev['low']) <= bb_l and price > float(prev['high'])
            trig_short = dn_ok and float(prev['high']) >= bb_u and price < float(prev['low'])
            long_dist = f"bbL={bb_l:.4f} prevLow={float(prev['low']):.4f} price={price:.4f} needsBreakAbove={float(prev['high']):.4f}"
            short_dist = f"bbU={bb_u:.4f} prevHi={float(prev['high']):.4f} price={price:.4f} needsBreakBelow={float(prev['low']):.4f}"
        elif et == 'ema_bounce':
            trig_long = up_ok and float(prev['low']) <= e21 and price > e21 and r_prev < 45
            trig_short = dn_ok and float(prev['high']) >= e21 and price < e21 and r_prev > 55
            long_dist = f"ema21={e21:.4f} prevLow={float(prev['low']):.4f} price={price:.4f} rsiPrev={r_prev:.1f}/45"
            short_dist = f"ema21={e21:.4f} prevHi={float(prev['high']):.4f} price={price:.4f} rsiPrev={r_prev:.1f}/55"
        elif et == 'swing_pivot':
            if len(df) >= 5:
                p2 = df.iloc[-3]; p3 = df.iloc[-4]; p4 = df.iloc[-5]
                pivot_low = float(p3['low']) < float(p4['low']) and float(p3['low']) < float(p2['low'])
                pivot_hi  = float(p3['high']) > float(p4['high']) and float(p3['high']) > float(p2['high'])
                trig_long = up_ok and pivot_low and price > float(prev['high'])
                trig_short = dn_ok and pivot_hi and price < float(prev['low'])
                long_dist = f"pivotLow={pivot_low} needBreak>{float(prev['high']):.4f} price={price:.4f}"
                short_dist = f"pivotHi={pivot_hi} needBreak<{float(prev['low']):.4f} price={price:.4f}"

        # Build status
        dir_label = []
        if up_ok: dir_label.append("LONG-OK")
        if dn_ok: dir_label.append("SHORT-OK")
        if not dir_label: dir_label = ["blocked"]

        # All-filter cross-check: what does every 1h filter say right now?
        def _fmt(u, d): return f"{('U' if u else '-')}{('D' if d else '-')}"
        all_filters = []
        all_filters.append("ema=" + _fmt(latest(dm.up_1h.get(sym)), latest(dm.dn_1h.get(sym))))
        all_filters.append("str=" + _fmt(latest(dm.up_struct_1h.get(sym)), latest(dm.dn_struct_1h.get(sym))))
        all_filters.append("hma=" + _fmt(latest(dm.up_hma_1h.get(sym)), latest(dm.dn_hma_1h.get(sym))))
        all_filters.append("sjm=" + _fmt(latest(dm.up_sjm_1h.get(sym)), latest(dm.dn_sjm_1h.get(sym))))

        gates = []
        gates.append(f"5m:{('U' if up5 else '-')}{('D' if dn5 else '-')}")
        gates.append(f"1h*{fv}:{_fmt(up1, dn1)}")
        gates.append("all[" + " ".join(all_filters) + "]")
        if wcfg.require_4h_agreement:
            u4 = latest(dm.up_struct_4h.get(sym)); d4 = latest(dm.dn_struct_4h.get(sym))
            gates.append(f"4h:{_fmt(u4, d4)}")
        if btc_block: gates.append(btc_block)

        state = "🔥 FIRE" if (trig_long or trig_short) else "..."
        detail = long_dist if up_ok else (short_dist if dn_ok else (long_dist or ""))
        rows.append((sym, state, f"[{'|'.join(dir_label)}] gates={' '.join(gates)}  et={et}  {detail}"))

    # Print table
    print(f"\n{'SYMBOL':<10} {'STATE':<10} DETAIL")
    print("-" * 120)
    for sym, state, detail in rows:
        print(f"{sym:<10} {state:<10} {detail}")


if __name__ == "__main__":
    main()
