"""One-shot probe: for each symbol, report how close it is to a whale_swing entry.

Reuses the live data/features pipeline. Output is a table sorted by proximity
so the top rows are the most likely next entries.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from config.deployer import load_all
from config.settings import load_config, is_tradeable_now
from core.data import DataManager
from strategies.whale_swing import WhaleSwingConfig


def _rsi_bounce_gap(rsi, r_prev, cfg, up_ok, dn_ok):
    long_gap = short_gap = None
    if up_ok:
        # Needs r_prev < oversold AND rsi >= oversold (prev bar only — can't
        # affect now). So we report current distance to oversold cross.
        long_gap = rsi - cfg.rsi_oversold  # positive = above the line, need dip
    if dn_ok:
        short_gap = cfg.rsi_overbought - rsi
    return long_gap, short_gap


def _bb_touch_gap(bar, prev, bb_lower, bb_upper, up_ok, dn_ok):
    long_gap = short_gap = None
    if up_ok and bb_lower > 0:
        # Needs prev.low <= bb_lower AND price > prev.high. Report price vs
        # bb_lower (negative = already touched).
        long_gap = (float(prev['low']) - bb_lower) / bb_lower * 100.0
    if dn_ok and bb_upper > 0:
        short_gap = (bb_upper - float(prev['high'])) / bb_upper * 100.0
    return long_gap, short_gap


def _ema_bounce_gap(bar, prev, e21, up_ok, dn_ok):
    long_gap = short_gap = None
    if up_ok and e21 > 0:
        long_gap = (float(prev['low']) - e21) / e21 * 100.0
    if dn_ok and e21 > 0:
        short_gap = (e21 - float(prev['high'])) / e21 * 100.0
    return long_gap, short_gap


def main():
    cfg = load_config()
    data = DataManager(cfg)
    deployed = load_all()

    rows = []
    for sym in cfg.instruments.keys():
        dep = deployed.get(sym)
        if not dep:
            continue
        scfg = WhaleSwingConfig.from_json(dep)
        try:
            data.refresh(sym)
        except Exception as e:
            rows.append((sym, f"fetch error: {e}", None, None, None))
            continue

        df = data.df_5m.get(sym)
        if df is None or df.empty or len(df) < 55:
            continue

        bar = df.iloc[-1]; prev = df.iloc[-2]
        price = float(bar['close'])
        rsi = float(bar['rsi']); r_prev = float(prev['rsi'])
        e21 = float(bar['ema_21']); e50 = float(bar['ema_50'])
        e200 = float(bar.get('ema_200', 0) or 0)
        slope = float(bar.get('ema_50_slope', 0) or 0)
        bb_lower = float(bar.get('bb_lower', 0) or 0)
        bb_upper = float(bar.get('bb_upper', 0) or 0)

        # 5m trend
        tf = scfg.trend_filter
        if tf == 'ema_cross':
            up_ok, dn_ok = e21 > e50, e21 < e50
        elif tf == 'ema_slope':
            up_ok, dn_ok = slope > 0, slope < 0
        elif tf == 'ema200':
            up_ok = price > e200 if e200 > 0 else True
            dn_ok = price < e200 if e200 > 0 else True
        else:
            up_ok, dn_ok = True, True

        # 1h filter (ema_cross only — close enough for proximity snapshot)
        if scfg.use_1h_filter:
            up_arr = data.up_1h.get(sym); dn_arr = data.dn_1h.get(sym)
            up_1h = bool(up_arr[-1]) if up_arr is not None and len(up_arr) else True
            dn_1h = bool(dn_arr[-1]) if dn_arr is not None and len(dn_arr) else True
            if not up_1h: up_ok = False
            if not dn_1h: dn_ok = False

        if scfg.direction == 'long_only': dn_ok = False
        elif scfg.direction == 'short_only': up_ok = False

        # Trading-hours gate
        tradeable, hours_reason = is_tradeable_now(sym)

        # Entry-specific proximity
        et = scfg.entry_type
        if et == 'rsi_bounce':
            lg, sg = _rsi_bounce_gap(rsi, r_prev, scfg, up_ok, dn_ok)
            metric = "RSI"
            extra = f"rsi={rsi:.1f} os={scfg.rsi_oversold:.0f} ob={scfg.rsi_overbought:.0f}"
        elif et == 'bb_touch':
            lg, sg = _bb_touch_gap(bar, prev, bb_lower, bb_upper, up_ok, dn_ok)
            metric = "BB %"
            extra = f"prevL={float(prev['low']):.4f} bbL={bb_lower:.4f} bbU={bb_upper:.4f}"
        elif et == 'ema_bounce':
            lg, sg = _ema_bounce_gap(bar, prev, e21, up_ok, dn_ok)
            metric = "EMA21 %"
            extra = f"prevL={float(prev['low']):.4f} e21={e21:.4f} r_prev={r_prev:.1f}"
        else:
            lg = sg = None
            metric = et
            extra = "(swing_pivot — not scored)"

        # Smallest magnitude gap wins the "closeness" score
        gaps = [g for g in (lg, sg) if g is not None]
        score = min((abs(g) for g in gaps), default=None)
        direction_hint = ""
        if lg is not None and (sg is None or abs(lg) <= abs(sg)):
            direction_hint = f"LONG gap={lg:+.2f}"
        elif sg is not None:
            direction_hint = f"SHORT gap={sg:+.2f}"

        gate = []
        if not tradeable: gate.append(f"hours:{hours_reason}")
        if not up_ok and not dn_ok: gate.append("trend:both blocked")
        elif not up_ok: gate.append("trend:long blocked")
        elif not dn_ok: gate.append("trend:short blocked")

        rows.append((sym, et, score, direction_hint, f"{metric} | {extra} | {'; '.join(gate) if gate else 'ok'}"))

    rows.sort(key=lambda r: (float('inf') if r[2] is None else r[2]))
    print(f"{'SYM':<10} {'ENTRY':<12} {'SCORE':>8}  {'DIR':<22}  DETAIL")
    print("-" * 130)
    for sym, et, score, dir_hint, detail in rows:
        sc = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
        print(f"{sym:<10} {et:<12} {sc:>8}  {dir_hint:<22}  {detail}")


if __name__ == "__main__":
    main()
